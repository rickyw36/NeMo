# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

"""Transformer based language model."""
import torch

from nemo.collections.nlp.modules.common.megatron.fused_layer_norm import get_layer_norm
from nemo.collections.nlp.modules.common.megatron.layer_type import LayerType
from nemo.collections.nlp.modules.common.megatron.module import MegatronModule
from nemo.collections.nlp.modules.common.megatron.transformer import ParallelTransformer
from nemo.collections.nlp.modules.common.megatron.utils import (
    ApexGuardDefaults,
    attn_mask_postprocess,
    build_attention_mask_3d,
)

try:
    from apex.transformer.enums import AttnMaskType, ModelType
    from apex.normalization import MixedFusedRMSNorm

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False
    # fake missing classes with None attributes
    AttnMaskType = ApexGuardDefaults()
    ModelType = ApexGuardDefaults()

__all__ = ["MegatronPerceiverEncoderModule"]


class MegatronPerceiverEncoderModule(MegatronModule):
    """Transformer encoder model.
    """

    def __init__(
        self,
        init_method,
        output_layer_init_method,
        hidden_size,
        ffn_hidden_size,
        num_layers,
        num_attention_heads,
        apply_query_key_layer_scaling=True,
        kv_channels=None,
        pre_process=True,
        post_process=True,
        use_cpu_initialization=False,
        encoder_attn_mask_type=AttnMaskType.padding,
        hidden_dropout=0.1,
        attention_dropout=0.1,
        position_embedding_type='learned_absolute',
        relative_attention_num_buckets=32,
        relative_attention_max_distance=128,
        precision=16,
        fp32_residual_connection=False,
        activations_checkpoint_method=None,
        activations_checkpoint_num_layers=1,
        layernorm_epsilon=1e-5,
        bias_activation_fusion=True,
        bias_dropout_add_fusion=True,
        masked_softmax_fusion=True,
        persist_layer_norm=False,
        openai_gelu=False,
        onnx_safe=False,
        activation='gelu',
        bias=True,
        normalization='layernorm',
        transformer_block_type='pre_ln',
        headscale=False,
        parent_model_type=ModelType.encoder_or_decoder,
        hidden_steps=32,
        num_self_attention_per_cross_attention=1,
    ):
        super(MegatronPerceiverEncoderModule, self).__init__()

        self.pre_process = pre_process
        self.post_process = post_process
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.init_method = init_method
        self.model_attn_mask_type = encoder_attn_mask_type
        self.hidden_dropout = hidden_dropout
        self.output_layer_init_method = output_layer_init_method
        self.parent_model_type = parent_model_type
        self.normalization = normalization
        self.transformer_block_type = transformer_block_type
        self.hidden_steps = hidden_steps
        self.num_self_attention_per_cross_attention = num_self_attention_per_cross_attention
        self.num_attention_heads = num_attention_heads
        self.apply_query_key_layer_scaling = apply_query_key_layer_scaling
        self.kv_channels = kv_channels
        self.ffn_hidden_size = ffn_hidden_size
        self.precision = precision
        self.fp32_residual_connection = fp32_residual_connection
        self.activations_checkpoint_method = activations_checkpoint_method
        self.activations_checkpoint_num_layers = activations_checkpoint_num_layers
        self.layernorm_epsilon = layernorm_epsilon
        self.bias_activation_fusion = bias_activation_fusion
        self.bias_dropout_add_fusion = bias_dropout_add_fusion
        self.masked_softmax_fusion = masked_softmax_fusion
        self.persist_layer_norm = persist_layer_norm
        self.openai_gelu = openai_gelu
        self.onnx_safe = onnx_safe
        self.activation = activation
        self.bias = bias
        self.relative_attention_num_buckets = relative_attention_num_buckets
        self.relative_attention_max_distance = relative_attention_max_distance
        self.headscale = headscale
        self.hidden_dropout = hidden_dropout
        self.attention_dropout = attention_dropout
        self.position_embedding_type = position_embedding_type
        self.use_cpu_initialization = use_cpu_initialization
        self.normalization = normalization
        self.parent_model_type = parent_model_type
        self.transformer_block_type = transformer_block_type

        assert self.num_self_attention_per_cross_attention >= 1
        assert self.hidden_steps >= 1

        self.init_hidden = torch.nn.Parameter(torch.nn.init.xavier_normal_(torch.empty(hidden_steps, hidden_size)))

        self.cross_attn_layers = torch.nn.ModuleList([self._build_cross_attn_layer() for _ in range(self.num_layers)])
        self.self_attn_layers = torch.nn.ModuleList(
            [
                self._build_self_attn_layer()
                for _ in range(self.num_layers * self.num_self_attention_per_cross_attention)
            ]
        )
        if normalization == 'layernorm':
            self.final_layernorm = get_layer_norm(hidden_size, layernorm_epsilon, persist_layer_norm)
        else:
            self.final_layernorm = MixedFusedRMSNorm(hidden_size, layernorm_epsilon)

    def _build_cross_attn_layer(self):
        return ParallelTransformer(
            layer_type=LayerType.decoder,
            init_method=self.init_method,
            output_layer_init_method=self.output_layer_init_method,
            num_layers=1,
            hidden_size=self.hidden_size,
            num_attention_heads=self.num_attention_heads,
            apply_query_key_layer_scaling=self.apply_query_key_layer_scaling,
            kv_channels=self.kv_channels,
            ffn_hidden_size=self.ffn_hidden_size,
            self_attn_mask_type=self.model_attn_mask_type,
            pre_process=self.pre_process,
            post_process=False,  # This is to avoid the final layernorm and transpose.
            precision=self.precision,
            fp32_residual_connection=self.fp32_residual_connection,
            activations_checkpoint_method=self.activations_checkpoint_method,
            activations_checkpoint_num_layers=self.activations_checkpoint_num_layers,
            layernorm_epsilon=self.layernorm_epsilon,
            hidden_dropout=self.hidden_dropout,
            attention_dropout=self.attention_dropout,
            position_embedding_type=self.position_embedding_type,
            relative_attention_num_buckets=self.relative_attention_num_buckets,
            relative_attention_max_distance=self.relative_attention_max_distance,
            use_cpu_initialization=self.use_cpu_initialization,
            bias_activation_fusion=self.bias_activation_fusion,
            bias_dropout_fusion=self.bias_dropout_add_fusion,
            masked_softmax_fusion=self.masked_softmax_fusion,
            persist_layer_norm=self.persist_layer_norm,
            openai_gelu=self.openai_gelu,
            onnx_safe=self.onnx_safe,
            activation=self.activation,
            bias=self.bias,
            normalization=self.normalization,
            model_type=self.parent_model_type,
            transformer_block_type=self.transformer_block_type,
            headscale=self.headscale,
        )

    def _build_self_attn_layer(self):
        return ParallelTransformer(
            layer_type=LayerType.encoder,
            init_method=self.init_method,
            output_layer_init_method=self.output_layer_init_method,
            num_layers=1,
            hidden_size=self.hidden_size,
            num_attention_heads=self.num_attention_heads,
            apply_query_key_layer_scaling=self.apply_query_key_layer_scaling,
            kv_channels=self.kv_channels,
            ffn_hidden_size=self.ffn_hidden_size,
            self_attn_mask_type=self.model_attn_mask_type,
            pre_process=self.pre_process,
            post_process=False,  # This is to avoid the final layernorm and transpose.
            precision=self.precision,
            fp32_residual_connection=self.fp32_residual_connection,
            activations_checkpoint_method=self.activations_checkpoint_method,
            activations_checkpoint_num_layers=self.activations_checkpoint_num_layers,
            layernorm_epsilon=self.layernorm_epsilon,
            hidden_dropout=self.hidden_dropout,
            attention_dropout=self.attention_dropout,
            position_embedding_type=self.position_embedding_type,
            relative_attention_num_buckets=self.relative_attention_num_buckets,
            relative_attention_max_distance=self.relative_attention_max_distance,
            use_cpu_initialization=self.use_cpu_initialization,
            bias_activation_fusion=self.bias_activation_fusion,
            bias_dropout_fusion=self.bias_dropout_add_fusion,
            masked_softmax_fusion=self.masked_softmax_fusion,
            persist_layer_norm=self.persist_layer_norm,
            openai_gelu=self.openai_gelu,
            onnx_safe=self.onnx_safe,
            activation=self.activation,
            bias=self.bias,
            normalization=self.normalization,
            model_type=self.parent_model_type,
            transformer_block_type=self.transformer_block_type,
            headscale=self.headscale,
        )

    def set_input_tensor(self, input_tensor):
        """ See megatron.model.transformer.set_input_tensor()"""
        # TODO: Fix this when adding support for Pipeline Parallel.
        pass

    def forward(
        self, enc_input, enc_attn_mask, layer_past=None, get_key_value=False,
    ):
        # convert to Megatron mask
        latent_attention_mask = torch.ones(enc_input.size(0), self.hidden_steps).to(enc_input.device)

        # First convert from 2D (B x T) to 3D (B x T x T)
        # Next convert to 4D (B x 1 x T x T) - unsqueeze(1) is for the head dim.
        latent_attention_mask_4d = attn_mask_postprocess(
            build_attention_mask_3d(
                source_mask=latent_attention_mask,
                target_mask=latent_attention_mask,
                attn_mask_type=AttnMaskType.padding,
            )
        )
        enc_dec_attn_mask_4d = attn_mask_postprocess(
            build_attention_mask_3d(
                source_mask=latent_attention_mask, target_mask=enc_attn_mask, attn_mask_type=AttnMaskType.padding,
            )
        )

        hidden_states = self.init_hidden.unsqueeze(0).expand(enc_input.size(0), -1, -1)  # sequence x batch x dim
        for i in range(self.num_layers):
            residual = hidden_states

            hidden_states = self.cross_attn_layers[i](
                hidden_states=hidden_states,
                attention_mask=latent_attention_mask_4d,
                enc_dec_attn_mask=enc_dec_attn_mask_4d,
                encoder_output=enc_input,
            ).transpose(
                1, 0
            )  # Need to transpose at the end becase pre-process is False
            for j in range(self.num_self_attention_per_cross_attention):
                hidden_states = self.self_attn_layers[i * self.num_self_attention_per_cross_attention + j](
                    hidden_states=hidden_states, attention_mask=latent_attention_mask_4d,
                ).transpose(
                    1, 0
                )  # Need to transpose at the end becase pre-process is False

            hidden_states += residual

        return self.final_layernorm(hidden_states)  # Need to transpose at the end becase pre-process is False
