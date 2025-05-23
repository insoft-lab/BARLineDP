# Enhancing Line-Level Defect Prediction Using Bilinear Attention Fusion and Ranking Optimization

This paper has been accepted in the journal of Empirical Software Engineering (EMSE 2025).

## Datasets
The datasets utilized in our study are sourced from Wattanakriengkrai et al., containing 32 releases across 9 software projects. For access to the datasets, please refer to this [github](https://github.com/awsm-research/line-level-defect-prediction).

For each software project, we use the initial release to train BARLineDP model. The second release is used as validation set, and the other releases are used as testing sets. For example, there are 5 releases in ActiveMQ (e.g., R1, R2, R3, R4, R5), R1 is used as training set, R2 is used as validation set, and R3 - R5 are used as testing sets.

## Environment Setup
### Python Environment Setup
The implementation codes are running in the following environment setups.
- `python == 3.9.13`
- `pytorch == 1.12.1`
- `numpy == 1.23.2`
- `pandas == 1.5.3`
- `transformers == 4.20.1`
- `joblib == 1.2.0`
- `more-itertools == 9.1.0`
- `rdkit == 2023.3.1`
- `scikit-learn == 1.1.2`
- `tqdm == 4.64.1`

### R Environment Setup
Download the following packages: `tidyverse`, `gridExtra`, `ModelMetrics`, `caret`, `reshape2`, `pROC`, `effsize`, and `progress`.

## Experiment
### Experimental Setup
The following parameters are used to train our BARLineDP model.
- `batch_size` = 16
- `num_epochs` = 10
- `embed_dim` = 768
- `gru_hidden_dim` = 64
- `gru_num_layers` = 1
- `bafn_hidden_dim` = 256
- `dropout` = 0.2
- `lr (learning rate)` = 0.001
- `k (balance hyperparameter)` = 0.2 if within-domain else 0.3

### Code Preprocessing
1. Download the datasets from the [github](https://github.com/awsm-research/line-level-defect-prediction) and keep them in `datasets/original/`.

2. Run the following command to prepare data for model training. The output will be stored in `datasets/preprocessed_data/`.

	`python code_preprocessing.py`

### BARLineDP Model Training
To train BARLineDP model for each project, run the following command. The trained models will be saved in `output/model/BARLineDP/`, and the loss will be saved in `output/loss/BARLineDP/`.

	python train_model.py

### Prediction Generation
To make a prediction within software projects, run the following command. 

	python generate_within_prediction.py

The generated outputs of within-prediction are stored in `output/prediction/BARLineDP/within-release/`.

To make a prediction across software projects, run the following command.
	
	python generate_cross_prediction.py

The generated outputs of cross-prediction are stored in `output/prediction/BARLineDP/cross-release/`.

### Get the Evaluation Results
To obtain file-level and line-level defect prediction results within WPDP and CPDP scenarios, you need to set the absolute working path for the script to run first, and then execute the following command.

	Rscript get_results.R

The results are stored in `output/result/BARLineDP/`.