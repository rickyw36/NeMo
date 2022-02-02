# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
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

import re
from typing import Any, Dict, Optional

import torch
import torch.nn as nn
from apex.transformer import parallel_state, tensor_parallel
from omegaconf.dictconfig import DictConfig
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.data.language_modeling.megatron.data_samplers import (
    MegatronPretrainingRandomSampler,
    MegatronPretrainingSampler,
)
from nemo.collections.nlp.data.language_modeling.megatron.dataset_utils import build_train_valid_test_datasets
from nemo.collections.nlp.models.language_modeling.megatron.lm_encoder_decoder_model import MegatronLMEncoderDecoderModule
from nemo.collections.nlp.models.nlp_model import NLPModel
from nemo.collections.nlp.modules.common.megatron.clip_grads import clip_grad_norm_fp32
from nemo.collections.nlp.modules.common.megatron.megatron_init import (
    initialize_model_parallel_for_nemo,
    set_jit_fusion_options,
)
from nemo.collections.nlp.modules.common.megatron.utils import (
    average_losses_across_data_parallel_group,
    make_inference_attention_mask_3d,
    make_inference_history_mask_3d,
)
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer
from nemo.utils import AppState, logging

__all__ = ["MegatronT5Model"]


class MegatronT5Model(MegatronLMEncoderDecoderModule):
    """
    Megatron T5 pretraining
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        super().__init__(cfg, trainer=trainer)

    def _build_vocab(self):
        # T5-related construction
        self.num_sentinel_tokens = self.cfg.tokenizer.num_sentinel_tokens
        self._add_special_tokens_to_tokenizer()

        super()._build_vocab


    def _add_special_tokens_to_tokenizer(self):
        if self.cfg.tokenizer.library == 'huggingface' or self.cfg.tokenizer.library == 'megatron':
            additional_tokens = {
                'additional_special_tokens': [f'<extra_id_{i}>' for i in range(self.num_sentinel_tokens)]
            }
            self.tokenizer.add_special_tokens(additional_tokens)

        if self.cfg.tokenizer.library == 'sentencepiece':
            # Need to add cls, sep, mask tokens to the tokenizer if they don't exist.
            # If cls, sep and mask are not attributes of the tokenizer, add it.
            if not hasattr(self.tokenizer, 'cls_token'):
                self.tokenizer.add_special_tokens({'cls_token': '<cls>'})
            if not hasattr(self.tokenizer.tokenizer, 'sep_id'):
                self.tokenizer.add_special_tokens({'sep_token': '<sep>'})
            if not hasattr(self.tokenizer.tokenizer, 'mask_id'):
                self.tokenizer.add_special_tokens({'mask_token': '<mask>'})

            # bos, eos, pad and unk may be present in the provided spm .model file, if they are, use it.
            if not hasattr(self.tokenizer, 'pad_token'):
                if hasattr(self.tokenizer.tokenizer, 'pad_id') and self.tokenizer.tokenizer.pad_id() > 0:
                    self.tokenizer.pad_token = self.tokenizer.tokenizer.id_to_piece(self.tokenizer.tokenizer.pad_id())
                else:
                    self.tokenizer.add_special_tokens({'pad_token': '<pad>'})
            else:
                self.tokenizer.add_special_tokens({'pad_token': '<pad>'})

            if not hasattr(self.tokenizer, 'bos_token'):
                if hasattr(self.tokenizer.tokenizer, 'bos_id') and self.tokenizer.tokenizer.bos_id() > 0:
                    self.tokenizer.bos_token = self.tokenizer.tokenizer.id_to_piece(self.tokenizer.tokenizer.bos_id())
                else:
                    self.tokenizer.add_special_tokens({'bos_token': '<bos>'})
            else:
                self.tokenizer.add_special_tokens({'bos_token': '<s>'})

            if not hasattr(self.tokenizer, 'eos_token'):
                if hasattr(self.tokenizer.tokenizer, 'eos_id') and self.tokenizer.tokenizer.eos_id() > 0:
                    self.tokenizer.eos_token = self.tokenizer.tokenizer.id_to_piece(self.tokenizer.tokenizer.eos_id())
                else:
                    self.tokenizer.add_special_tokens({'eos_token': '<eos>'})
            else:
                self.tokenizer.add_special_tokens({'eos_token': '</s>'})

            # Special check to see if <extra_id_{}> is already present in the tokenizer. If it is, only modify the additional_special_tokens function.
            for i in range(self.num_sentinel_tokens):
                if f'▁<extra_id_{i}>' in self.tokenizer.vocab:
                    self.tokenizer.special_token_to_id[f'<extra_id_{i}>'] = self.tokenizer.text_to_ids(
                        f'<extra_id_{i}>'
                    )[0]
                else:
                    self.tokenizer.add_special_tokens([f'<extra_id_{i}>'])


    def build_train_valid_test_datasets(self):
        logging.info('Building T5 datasets.')
        if self.cfg.data.seq_length_dec < self.cfg.data.seq_length * self.cfg.data.masked_lm_prob:
            raise ValueError(
                f"Cannot have decoder max sequence length ({self.cfg.data.seq_length_dec}) less than encoder sequence length ({self.cfg.data.seq_length}) * masked_lm_prob ({self.cfg.data.masked_lm_prob})"
            )
        global_batch_size = self.trainer.world_size * self.cfg.micro_batch_size / self.cfg.tensor_model_parallel_size
        eval_iters = (self.trainer.max_steps // self.trainer.val_check_interval + 1) * self.trainer.limit_val_batches
        test_iters = self.trainer.limit_test_batches
        train_valid_test_num_samples = [
            self.trainer.max_steps * global_batch_size,
            eval_iters * global_batch_size,
            test_iters * global_batch_size,
        ]
        self._train_ds, self._validation_ds, self._test_ds = build_train_valid_test_datasets(
            cfg=self.cfg,
            trainer=self.trainer,
            tokenizer=self.tokenizer,
            data_prefix=self.cfg.data.data_prefix,
            data_impl=self.cfg.data.data_impl,
            splits_string=self.cfg.data.splits_string,
            train_valid_test_num_samples=train_valid_test_num_samples,
            max_seq_length=self.cfg.data.seq_length,
            max_seq_length_dec=self.cfg.data.seq_length_dec,
            masked_lm_prob=self.cfg.data.masked_lm_prob,
            short_seq_prob=self.cfg.data.short_seq_prob,
            seed=self.cfg.seed,
            skip_warmup=self.cfg.data.skip_warmup,
            dataset_type='t5',
        )
        logging.info(f'Length of train dataset: {len(self._train_ds)}')
        logging.info(f'Length of val dataset: {len(self._validation_ds)}')
        logging.info(f'Length of test dataset: {len(self._test_ds)}')
        logging.info(f'Finished building T5 datasets.')
        return self._train_ds, self._validation_ds, self._test_ds

    def list_available_models():
        pass
