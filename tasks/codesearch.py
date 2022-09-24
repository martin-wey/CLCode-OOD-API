import logging
from typing import Tuple

import datasets
import numpy as np
import omegaconf
import torch
import transformers
from more_itertools import chunked
from scipy.spatial.distance import cdist
from torch.utils.data import DataLoader, TensorDataset
from tqdm import tqdm

logger = logging.getLogger(__name__)


def compute_ranks(src_representations: np.ndarray,
                  tgt_representations: np.ndarray,
                  distance_metric: str = 'cosine') -> Tuple[np.array, np.array]:
    distances = cdist(src_representations, tgt_representations,
                      metric=distance_metric)
    # By construction the diagonal contains the correct elements
    correct_elements = np.expand_dims(np.diag(distances), axis=-1)
    return np.sum(distances <= correct_elements, axis=-1), distances


def train(cfg: omegaconf.DictConfig,
          model: transformers.RobertaModel,
          tokenizer: transformers.PreTrainedTokenizerFast,
          train_dataset: datasets.Dataset,
          valid_dataset: datasets.Dataset) -> None:
    pass


def evaluate():
    pass


def test(cfg: omegaconf.DictConfig,
         model: transformers.RobertaModel,
         tokenizer: transformers.PreTrainedTokenizerFast,
         test_dataset: datasets.Dataset) -> None:
    def tokenize(batch, max_length):
        return tokenizer(
            batch,
            padding='max_length',
            max_length=max_length,
            truncation=True,
            return_tensors='np',
        ).input_ids

    logger.info('Tokenizing codes and queries.')
    test_dataset = test_dataset.map(
        lambda batch: {'code_tokenized': tokenize(batch['original_string'], cfg.run.max_code_length)}, batched=True,
        num_proc=4)
    test_dataset = test_dataset.map(
        lambda batch: {'docstring_tokenized': tokenize(batch['docstring'], cfg.run.max_query_length)}, batched=True,
        num_proc=4)
    test_dataset = test_dataset.remove_columns(
        [col for col in test_dataset.column_names if col not in ['code_tokenized', 'docstring_tokenized']])
    test_dataset = test_dataset.shuffle(seed=cfg.run.seed)

    # Because we have huge batch size at test (e.g., 1000), we transform tensors into np arrays
    #   otherwise it wouldn't fit in memory
    data = np.array(list(zip(test_dataset['code_tokenized'], test_dataset['docstring_tokenized'])), dtype=np.object)

    sum_mrr = 0.0
    num_batches = 0
    batched_data = chunked(data, cfg.run.test_batch_size)
    for batch_data in tqdm(batched_data, desc='Iteration', total=len(data) // cfg.run.test_batch_size):
        if len(batch_data) < cfg.run.test_batch_size:
            break  # the last batch is smaller than the others, exclude.
        num_batches += 1

        batch_data_list = list(zip(*batch_data))
        code_query_dataset = TensorDataset(torch.tensor(batch_data_list[0]), torch.tensor(batch_data_list[1]))
        minibatch_loader = DataLoader(code_query_dataset, batch_size=cfg.run.train_batch_size, pin_memory=True)

        code_representations = []
        query_representations = []
        for mini_batch in minibatch_loader:
            code_inputs = mini_batch[0].to(cfg.device)
            query_inputs = mini_batch[1].to(cfg.device)

            code_reps = model(code_inputs)[1]
            query_reps = model(query_inputs)[1]

            code_representations.append(code_reps.cpu().detach().numpy())
            query_representations.append(query_reps.cpu().detach().numpy())

        code_representations = np.concatenate(code_representations, axis=0)
        query_representations = np.concatenate(query_representations, axis=0)
        assert len(code_representations) == len(query_representations) == cfg.run.test_batch_size

        ranks, distances = compute_ranks(code_representations, query_representations)
        sum_mrr += np.mean(1.0 / ranks)

    mrr = sum_mrr / num_batches
    logger.info(f'Test MRR: {round(mrr, 4)}')
