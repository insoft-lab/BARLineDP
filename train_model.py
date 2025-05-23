import copy
import os, argparse
import random
import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F
from tqdm import tqdm
from sklearn.utils import compute_class_weight
from transformers import RobertaConfig, RobertaTokenizer, RobertaModel
from torch.utils.data import Dataset, DataLoader
from sklearn.metrics import roc_auc_score
from BARLineDP import *
from my_util import *


class InputFeatures(object):
    def __init__(self, input_ids, label, line_label):
        self.input_ids = input_ids
        self.label = label
        self.line_label = line_label


class TextDataset(Dataset):
    def __init__(self, tokenizer, args, datasets, labels, line_labels):
        self.examples = []
        labels = torch.FloatTensor(labels)
        for dataset, label, line_label in zip(datasets, labels, line_labels):
            dataset_ids = [convert_examples_to_features(item, tokenizer, args) for item in dataset]
            self.examples.append(InputFeatures(dataset_ids, label, line_label))

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, i):
        return torch.tensor(self.examples[i].input_ids), self.examples[i].label, \
            torch.FloatTensor(self.examples[i].line_label)


def convert_examples_to_features(item, tokenizer, args):
    code = ' '.join(item)
    code_tokens = tokenizer.tokenize(code)[:args.block_size - 2]
    source_tokens = [tokenizer.cls_token] + code_tokens + [tokenizer.sep_token]
    source_ids = tokenizer.convert_tokens_to_ids(source_tokens)
    padding_length = args.block_size - len(source_ids)
    source_ids += [tokenizer.pad_token_id] * padding_length
    return source_ids


def set_seed(seed=42):
    random.seed(seed)
    os.environ['PYHTONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True


def collate_fn(batch):
    file_data = [data_list for data_list in batch]
    return file_data


def get_loss_weight(labels, weight_dict, mode=None):
    label_list = labels.numpy().squeeze().tolist()
    weight_list = []

    if len(labels) == 1:
        label_list = [label_list]

    for lab in label_list:
        if lab == 0:
            weight_list.append(weight_dict['clean'])
        else:
            weight_list.append(weight_dict['defect'])

    weight_tensor = torch.tensor(weight_list).reshape(-1, 1)
    return weight_tensor


def kld(p, q, discounts):
    return (p * torch.log2(p / q) * discounts).sum()


def jsd(pred, gt, epsilon=1e-8):
    top1_pred = F.softmax(pred, dim=-1)
    top1_gt = F.softmax(gt, dim=-1)
    m = 0.5 * (top1_pred + top1_gt + epsilon)
    sorted_indices = torch.argsort(gt, dim=-1, descending=True)
    ranks = torch.argsort(sorted_indices, dim=-1) + 1
    discounts = 1.0 / torch.log2(ranks.float() + 1)
    jsd = 0.5 * (kld(top1_pred, m, discounts) + kld(top1_gt, m, discounts))
    return jsd


def train_model(args, dataset_name):
    actual_save_model_dir = args.save_model_dir + dataset_name + '/'

    if not os.path.exists(actual_save_model_dir):
        os.makedirs(actual_save_model_dir)

    if not os.path.exists(args.loss_dir):
        os.makedirs(args.loss_dir)

    train_rel = all_train_releases[dataset_name]
    valid_rel = all_eval_releases[dataset_name][0]

    train_df = get_df(train_rel)
    valid_df = get_df(valid_rel)

    train_code3d, train_label, train_line_label = get_code3d_and_label(train_df, True, args.max_train_LOC)
    valid_code3d, valid_label, valid_line_label = get_code3d_and_label(valid_df, True, args.max_train_LOC)

    sample_weights = compute_class_weight(class_weight='balanced', classes=np.unique(train_label), y=train_label)

    weight_dict = {}
    weight_dict['defect'] = np.max(sample_weights)
    weight_dict['clean'] = np.min(sample_weights)

    MODEL_CLASSES = {'roberta': (RobertaConfig, RobertaModel, RobertaTokenizer)}
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(args.config_name if args.config_name else args.model_name_or_path,
                                          cache_dir=args.cache_dir if args.cache_dir else None)
    tokenizer = tokenizer_class.from_pretrained(args.tokenizer_name,
                                                do_lower_case=args.do_lower_case,
                                                cache_dir=args.cache_dir if args.cache_dir else None)
    if args.block_size <= 0:
        args.block_size = tokenizer.max_len_single_sentence
    args.block_size = min(args.block_size, tokenizer.max_len_single_sentence)
    if args.model_name_or_path:
        codebert = model_class.from_pretrained(args.model_name_or_path,
                                               from_tf=bool('.ckpt' in args.model_name_or_path),
                                               config=config,
                                               cache_dir=args.cache_dir if args.cache_dir else None)
    else:
        codebert = model_class(config)

    model = BARLineDP(
        embed_dim=args.embed_dim,
        gru_hidden_dim=args.gru_hidden_dim,
        gru_num_layers=args.gru_num_layers,
        bafn_output_dim=args.bafn_hidden_dim,
        dropout=args.dropout,
        device=args.device
    )

    codebert.to(args.device)
    model.to(args.device)

    x_train_vec = TextDataset(tokenizer, args, train_code3d, train_label, train_line_label)
    x_valid_vec = TextDataset(tokenizer, args, valid_code3d, valid_label, valid_line_label)

    train_dl = DataLoader(x_train_vec, shuffle=True, batch_size=args.batch_size, drop_last=True, collate_fn=collate_fn)
    valid_dl = DataLoader(x_valid_vec, shuffle=False, batch_size=args.batch_size, drop_last=False,
                          collate_fn=collate_fn)

    optimizer = optim.Adam(params=filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr)
    criterion_file = nn.BCEWithLogitsLoss()
    sig = nn.Sigmoid()

    best_auc = 0
    best_epoch = 0
    best_model = None

    train_loss_all_epochs = []
    val_loss_all_epochs = []
    val_auc_all_epochs = []

    model.zero_grad()
    for epoch in range(1, args.num_epochs + 1):
        train_losses = []
        val_losses = []

        model.train()
        for step, batch in tqdm(enumerate(train_dl), total=len(train_dl), desc='Train Loop'):
            inputs = [item[0] for item in batch]
            labels = [item[1] for item in batch]
            line_labels = [item[2] for item in batch]

            labels = torch.tensor(labels)

            cov_inputs = []
            with torch.no_grad():
                for item in inputs:
                    cov_inputs.append(
                        codebert(item.to(args.device), attention_mask=item.to(args.device).ne(1)).pooler_output
                    )

            weight_tensor_file = get_loss_weight(labels, weight_dict, 'file')
            criterion_file.weight = weight_tensor_file.to(args.device)

            output, line_atts = model(cov_inputs)
            file_loss = criterion_file(output, labels.reshape(args.batch_size, 1).to(args.device))

            att_mins = line_atts.min(dim=1, keepdim=True).values
            att_maxs = line_atts.max(dim=1, keepdim=True).values
            line_atts = (line_atts - att_mins) / (att_maxs - att_mins)

            cnt = 0
            train_line_losses = []
            for att, line_label, file_label in zip(line_atts, line_labels, labels):
                if file_label == 0. or not any(x == 1.0 for x in line_label):
                    continue
                cnt += 1
                att = att[:len(line_label)]
                top_k = int(0.2 * len(line_label))
                sorted_att, sorted_indices = att.sort(descending=True, dim=-1)
                sorted_att = sorted_att[:top_k]
                sorted_indices = sorted_indices[:top_k]
                line_label = line_label[sorted_indices].to(args.device)
                line_loss = jsd(sorted_att, line_label)
                train_line_losses.append(line_loss)

            if len(train_line_losses) > 0:
                line_loss = torch.mean(torch.stack(train_line_losses), dim=0)
            else:
                line_loss = 0.

            k = args.k * (cnt / len(labels))
            loss = (1 - k) * file_loss + k * line_loss
            train_losses.append(loss.item())

            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
            nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
            torch.cuda.empty_cache()

        train_loss_all_epochs.append(np.mean(train_losses))
        outputs = []
        outputs_labels = []

        with torch.no_grad():
            criterion_file.weight = None
            model.eval()

            for step, batch in tqdm(enumerate(valid_dl), total=len(valid_dl), desc='Valid Loop'):
                inputs = [item[0] for item in batch]
                labels = [item[1] for item in batch]
                line_labels = [item[2] for item in batch]

                labels = torch.tensor(labels)

                cov_inputs = []
                for item in inputs:
                    cov_inputs.append(
                        codebert(item.to(args.device), attention_mask=item.to(args.device).ne(1)).pooler_output
                    )

                output, line_atts = model(cov_inputs)
                outputs.append(sig(output))
                outputs_labels.append(labels)
                val_file_loss = criterion_file(output, labels.reshape(len(labels), 1).to(args.device))

                att_mins = line_atts.min(dim=1, keepdim=True).values
                att_maxs = line_atts.max(dim=1, keepdim=True).values
                line_atts = (line_atts - att_mins) / (att_maxs - att_mins)

                cnt = 0
                val_line_losses = []
                for att, line_label, file_label in zip(line_atts, line_labels, labels):
                    if file_label == 0. or not any(x == 1.0 for x in line_label):
                        continue
                    cnt += 1
                    att = att[:len(line_label)]
                    top_k = int(0.2 * len(line_label))
                    sorted_att, sorted_indices = att.sort(descending=True, dim=-1)
                    sorted_att = sorted_att[:top_k]
                    sorted_indices = sorted_indices[:top_k]
                    line_label = line_label[sorted_indices].to(args.device)
                    val_line_loss = jsd(sorted_att, line_label)
                    val_line_losses.append(val_line_loss)

                if len(val_line_losses) > 0:
                    val_line_loss = torch.mean(torch.stack(val_line_losses), dim=0)
                else:
                    val_line_loss = 0.

                k = args.k * (cnt / len(labels))
                val_loss = (1 - k) * val_file_loss + k * val_line_loss
                val_losses.append(val_loss.item())

        val_loss_all_epochs.append(np.mean(val_losses))

        y_prob = torch.cat(outputs)
        y_gt = torch.cat(outputs_labels)

        valid_auc = roc_auc_score(y_gt, y_prob.to('cpu'))
        val_auc_all_epochs.append(valid_auc)

        if valid_auc >= best_auc:
            best_model = copy.deepcopy(model)
            best_auc = valid_auc
            best_epoch = epoch

        print('Training at Epoch ' + str(epoch) + ' with training loss ' + str(np.mean(train_losses)))
        print('Validation at Epoch ' + str(epoch) + ' with validation loss ' + str(np.mean(val_losses)),
              ' AUC ' + str(valid_auc))

        if epoch % args.num_epochs == 0:
            print('The training step of ' + dataset_name + ' is finished!')
            torch.save({'epoch': best_epoch,
                        'model_state_dict': best_model.state_dict(),
                        'optimizer_state_dict': optimizer.state_dict()},
                       actual_save_model_dir + 'best_model.pth')

        loss_df = pd.DataFrame()
        loss_df['epoch'] = np.arange(1, len(train_loss_all_epochs) + 1)
        loss_df['train_loss'] = train_loss_all_epochs
        loss_df['valid_loss'] = val_loss_all_epochs
        loss_df['valid_auc'] = val_auc_all_epochs
        loss_df.to_csv(args.loss_dir + dataset_name + '-loss_record.csv', index=False)


def main():
    arg = argparse.ArgumentParser()

    arg.add_argument('-file_lvl_gt', type=str, default='../datasets/preprocessed_data/',
                     help='the directory of preprocessed data')
    arg.add_argument('-save_model_dir', type=str, default='output/model/BARLineDP/',
                     help='the save directory of model')
    arg.add_argument('-loss_dir', type=str, default='output/loss/BARLineDP/',
                     help='the loss directory of model')

    arg.add_argument('-batch_size', type=int, default=16, help='batch size per GPU/CPU for training/evaluation')
    arg.add_argument('-num_epochs', type=int, default=10, help='total number of training epochs to perform')
    arg.add_argument('-embed_dim', type=int, default=768, help='the input dimension of Bi-GRU')
    arg.add_argument('-gru_hidden_dim', type=int, default=64, help='hidden size of GRU')
    arg.add_argument('-gru_num_layers', type=int, default=1, help='number of GRU layer')
    arg.add_argument('-bafn_hidden_dim', type=int, default=256, help='output dimension of BAFN')
    arg.add_argument('-max_grad_norm', type=int, default=5, help='max gradient norm')
    arg.add_argument('-max_train_LOC', type=int, default=1000, help='max LOC of training/validation data')
    arg.add_argument('-use_layer_norm', type=bool, default=True, help='weather to use layer normalization')
    arg.add_argument('-dropout', type=float, default=0.2, help='dropout rate')
    arg.add_argument('-lr', type=float, default=0.001, help='learning rate')
    arg.add_argument('-k', type=float, default=0.2, help='balance hyperparameter')
    arg.add_argument('-seed', type=int, default=0, help='random seed for initialization')
    arg.add_argument('-weight_decay', type=float, default=0.0, help='weight decay whether apply some')

    arg.add_argument('-model_type', type=str, default='roberta', help='the token embedding model')
    arg.add_argument('-model_name_or_path', type=str, default='../microsoft/codebert-base',
                     help='the model checkpoint for weights initialization')
    arg.add_argument('-config_name', type=str, default=None,
                     help='optional pretrained config name or path if not the same as model_name_or_path')
    arg.add_argument('-tokenizer_name', type=str, default='../microsoft/codebert-base',
                     help='optional pretrained tokenizer name or path if not the same as model_name_or_path')
    arg.add_argument('-cache_dir', type=str, default=None,
                     help='optional directory to store the pre-trained models')
    arg.add_argument('-block_size', type=int, default=100,
                     help='the training dataset will be truncated in block of this size for training')
    arg.add_argument('-do_lower_case', action='store_true', help='set this flag if you are using an uncased model')

    args = arg.parse_args()
    args.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    set_seed(args.seed)

    dataset_names = list(all_releases.keys())
    for dataset_name in dataset_names:
        train_model(args, dataset_name)


if __name__ == "__main__":
    main()
