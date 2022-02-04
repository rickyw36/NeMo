# Copyright 2018 The Google AI Language Team Authors and
# The HuggingFace Inc. team.
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Some code of this file was adapted from the HuggingFace library available at
# https://github.com/huggingface/transformers

from typing import Dict, List, Optional, Union

import numpy as np
import torch

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.collections.nlp.data.language_modeling.megatron.t5_dataset import (
    make_attention_mask_3d,
    make_history_mask_3d,
)
from nemo.core.classes import Dataset
from nemo.core.neural_types import CategoricalValuesType, ChannelType, MaskType, NeuralType, RegressionValuesType
from nemo.utils import logging
import csv

__all__ = ['TextToTextPTuneDataset', 'TextToTextXNliDataset']

class InputExample(object):
    """A single training/test example for simple sequence classification.

    Args:
        guid: Unique id for the example.
        text_a: The untokenized text of the first sequence.
        For single sequence tasks, only this sequence must be specified.
        text_b: The untokenized text of the second
        sequence. Only must be specified for sequence pair tasks.
        label:The label of the example. This should be
        specified for train and dev examples, but not for test examples.
    """

    def __init__(self, guid: int, text_a: str, text_b: str = None, label: str = None):
        """Constructs a InputExample."""
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label

    def __repr__(self):
        return (
            f"InputExample(guid='{self.guid}', text_a='{self.text_a}', text_b='{self.text_b}', label='{self.label}')"
        )


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def __init__(self, data_type: str, task_type: str):
        self.data_type = data_type
        self.task_type = task_type

    def get_examples(self, data_path):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    def get_task_type(self):
        return self.task_type

    def get_ptune_query(self, text_a: str, text_b: str, prompt_token_id: int, max_seq_len: int ,templates: List[int], tokenizer: TokenizerSpec):
        raise NotImplemented()
 
    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with open(input_file, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                # if sys.version_info[0] == 2:
                #     line = list(unicode(cell, 'utf-8') for cell in line)
                lines.append(line)
            return lines


class MnliProcessor(DataProcessor):
    """Processor for the MultiNLI data set (GLUE version)."""

    def __init__(self, data_type: str, task_type: str):
        super().__init__(data_type, task_type)

    def get_examples(self, data_path):
        """See base class."""
        return self._create_examples(self._read_tsv(data_path), self.data_type)

    def get_labels(self):
        """See base class."""
        return ["contradiction", "entailment", "neutral"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, line[0])
            text_a = line[8]
            text_b = line[9]
            label = line[-1]
            examples.append(InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples

    def get_t5_prompted_query(self, text_a, text_b):
        return f"mnli hypothesis: {text_a} premise: {text_b}"

    def get_ptune_query(self,
                        text_a: str,
                        text_b: str,
                        prompt_token_id: int,
                        max_seq_len: int,
                        templates: List[int],
                        tokenizer: TokenizerSpec):
        full_sentence = f"mnli hypothesis: {text_a} premise: {text_b}"
        input_token_ids = tokenizer.text_to_ids(full_sentence)
        cut = 0
        if len(input_token_ids) + sum(templates) > max_seq_len:
            logging.warning("Input sequence is longer than the LM model max seq, will cut it off to fit")
            cut = len(input_token_ids) + sum(templates) - max_seq_len
        return [prompt_token_id] * templates[0] + input_token_ids[cut:] + [prompt_token_id] * templates[1]

    def label2string(self, label):
        return label


class XNliProcessor(MnliProcessor):
    """Processor for the XNLI data set (GLUE version)."""

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, line[0])
            text_a = line[6]
            text_b = line[7]
            label = line[1]
            examples.append(InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples

processors = {
    "mnli": MnliProcessor,
    "xnli": XNliProcessor,
}


class TaskDataset(Dataset):
    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        """Returns definitions of module output ports.
               """
        return {
            'input_ids': NeuralType(('B', 'T'), ChannelType()),
            'segment_ids': NeuralType(('B', 'T'), ChannelType()),
            'input_mask': NeuralType(('B', 'T'), MaskType()),
            "labels": NeuralType(
                tuple('B'), RegressionValuesType() if self.task_name == 'sts-b' else CategoricalValuesType()
            ),
        }

    def __init__(
        self,
        file_name: str,
        task_name: str,
        data_type: str,
        tokenizer: TokenizerSpec,
    ):
        """
        Processes Task datasets
        Args:
            file_name: path to file
            task_name: task name
            tokenizer: such as AutoTokenizer
            max_seq_length: max sequence length minus 2 for [CLS] and [SEP]
            use_cache: whether to use data cache
        """
        logging.info(f'Processing {file_name}')
        self.tokenizer = tokenizer
        if task_name not in processors:
            raise ValueError(f'{task_name} not supported. Choose from {processors.keys()}')

        self.processor = processors[task_name](data_type, task_name)
        self.label_list = self.processor.get_labels()
        self.examples = self.processor.get_examples(file_name)

    def __len__(self):
        return len(self.features)

    def __getitem__(self, idx):
        feature = self.features[idx]
        return (
            np.array(feature.input_ids),
            np.array(feature.segment_ids),
            np.array(feature.input_mask, dtype=np.long),
            np.array(feature.label_id),
        )


class TextToTextPTuneDataset(TaskDataset):
    """Multiple Task Dataset in a text-to-text format."""

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return

    def __init__(
        self,
        file_name: str,
        task_name: str,
        data_type: str,
        tokenizer: TokenizerSpec,
        templates: List[int],
        pseudo_token_id: int,
        max_seq_length: int,
        max_seq_length_decoder: int = 128,
    ):
        """
        Processes TextToText PTuning Dataset
        Args:
            file_name: path to file
            task_name: nlp task name
            data_type: train/dev/test
            tokenizer: such as AutoTokenizer
            templates: virtual token template, list of integers
            max_seq_length: max sequence length for encoder
            max_seq_length_decoder: max seq length for decoder
        """
        super().__init__(file_name, task_name, data_type, tokenizer)
        self.max_seq_length = max_seq_length
        self.max_seq_length_decoder = max_seq_length_decoder
        self.templates = templates
        self.pseudo_token_id = pseudo_token_id
        self.features = self.convert_examples_to_features()

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        enc_query, dec_input, labels = self.features[idx]
        return {'text_enc': enc_query, 'text_dec': dec_input, 'labels': labels}

    def collate_fn(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]

        max_dec_input_length = max([len(item) for item in dec_input])
        max_enc_query_length = max([len(item) for item in enc_query])
        max_label_length = max([len(item) for item in labels])

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        dec_input = [item + [self.tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = make_attention_mask_3d(enc_query, enc_query, self.tokenizer.pad_id).long()
        dec_mask = make_attention_mask_3d(dec_input, dec_input, self.tokenizer.pad_id)
        dec_mask = (dec_mask * make_history_mask_3d(dec_input)).long()
        enc_dec_mask = make_attention_mask_3d(dec_input, enc_query, self.tokenizer.pad_id).long()

        return {
            'text_enc': enc_query,
            'text_dec': dec_input,
            'labels': labels,
            'loss_mask': loss_mask,
            'enc_mask': enc_mask,
            'dec_mask': dec_mask,
            'enc_dec_mask': enc_dec_mask,
        }

    def convert_examples_to_features(self):
        """
        Converts examples into Text-to-Text batches to be used with a model like T5.
        Inputs are prefixed with a text prompt that indicates the task to perform.
        """
        features = []
        for ex_index, example in enumerate(self.examples):
            if ex_index % 10000 == 0:
                logging.info(f"Writing example {ex_index} of {len(self.examples)}")

            enc_query = self.processor.get_ptune_query(example.text_a, example.text_b, self.pseudo_token_id, self.max_seq_length, self.templates, self.tokenizer)
            if len(enc_query) > self.max_seq_length:
                enc_query = enc_query[: self.max_seq_length]
            cut = 0
            dec_content_ids = self.tokenizer.text_to_ids(self.processor.label2string(example.label))
            if len(dec_content_ids) + 2 > self.max_seq_length_decoder:
                cut = len(dec_content_ids) + 2 - self.max_seq_length_decoder
            dec_query = (
                [self.tokenizer.bos_id]
                + dec_content_ids[:(len(dec_content_ids) - cut)]
                + [self.tokenizer.eos_id]
            )

            dec_input = dec_query[:-1]
            labels = dec_query[1:]

            features.append([enc_query, dec_input, labels])

        return features


class TextToTextXNliDataset(TextToTextPTuneDataset):

    def __getitem__(self, idx):
        enc_query, dec_input, labels, lang = self.features[idx]
        return {'text_enc': enc_query, 'text_dec': dec_input,
                'labels': labels, 'lang': lang}

    def collate_fn(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]
        lang = [item['lang'] for item in batch]

        max_dec_input_length = max([len(item) for item in dec_input])
        max_enc_query_length = max([len(item) for item in enc_query])
        max_label_length = max([len(item) for item in labels])

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        dec_input = [item + [self.tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = make_attention_mask_3d(enc_query, enc_query, self.tokenizer.pad_id).long()
        dec_mask = make_attention_mask_3d(dec_input, dec_input, self.tokenizer.pad_id)
        dec_mask = (dec_mask * make_history_mask_3d(dec_input)).long()
        enc_dec_mask = make_attention_mask_3d(dec_input, enc_query, self.tokenizer.pad_id).long()

        return {
            'text_enc': enc_query,
            'text_dec': dec_input,
            'labels': labels,
            'loss_mask': loss_mask,
            'enc_mask': enc_mask,
            'dec_mask': dec_mask,
            'enc_dec_mask': enc_dec_mask,
            "lang": lang,
        }

    def convert_examples_to_features(self):
        """
        Converts examples into Text-to-Text batches to be used with a model like T5.
        Inputs are prefixed with a text prompt that indicates the task to perform.
        """
        features = []
        for ex_index, example in enumerate(self.examples):
            if ex_index % 10000 == 0:
                logging.info(f"Writing example {ex_index} of {len(self.examples)}")

            enc_query = self.processor.get_ptune_query(example.text_a, example.text_b, self.pseudo_token_id, self.max_seq_length, self.templates, self.tokenizer)
            cut = 0
            dec_content_ids = self.tokenizer.text_to_ids(self.processor.label2string(example.label))
            if len(dec_content_ids) + 2 > self.max_seq_length_decoder:
                cut = len(dec_content_ids) + 2 - self.max_seq_length_decoder
            dec_query = (
                [self.tokenizer.bos_id]
                + dec_content_ids[:(len(dec_content_ids) - cut)]
                + [self.tokenizer.eos_id]
            )
            dec_input = dec_query[:-1]
            labels = dec_query[1:]

            features.append([enc_query, dec_input, labels, example.guid.split('-')[1]])
        return features