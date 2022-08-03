import math
from contextlib import suppress
from typing import Callable

import torch
from colossalai import nn as col_nn
from colossalai.context import ParallelMode
from colossalai.core import global_context as gpc
from colossalai.logging import get_dist_logger
from colossalai.nn.layer.utils import CheckpointModule, divide
from colossalai.nn.layer.wrapper import PipelineSharedModuleWrapper
from colossalai.pipeline.utils import partition_uniform
from colossalai.utils import get_current_device
from titans.decorator import no_support
from titans.layer.block import GPTBlock
from titans.layer.embedding import GPTEmbedding
from titans.layer.head import GPTLMHead
from titans.loss.lm_loss import GPTLMLoss
from torch import dtype, nn

__all__ = ['Quant_GPT', 'GPTLMLoss', 'quant_gpt2_micro', 'quant_gpt2_small', 'quant_gpt2_medium', 'quant_gpt2_large', 'quant_gpt2_xl', 'quant_gpt2_8B', 'quant_gpt3']

# Torch Datatypes: https://pytorch.org/docs/stable/tensor_attributes.html#torch.dtype


@no_support(['sp', 'moe'])
class Quant_GPT(nn.Module):
    """
    The GPT2 Model transformer with a language modeling head on top (linear layer with weights tied to the input
    embeddings).

    Args:
        vocab_size(int): The size of dictionary, defaults to 50304.
        max_position_embeddings(int): The max value of positional embeddings, defaults to 1024.
        hidden_size(int): Hidden size of the transformer blocks, defaults to 768.
        num_heads(int): The number of heads in transformer blocks, defaults to 12.
        depth(int): The number of transformer layers, defaults to 12.
        mlp_ratio(float): The ratio used in mlp layer, defaults to 4.0.
        dropout(float): The ratio used to construct dropout modules, which indicates the percentage of parameters should be casted to zero, defaults to 0.1.
        embedding_dropout(float): The ratio used to construct embedding dropout modules, which indicates the percentage of parameters should be casted to zero, defaults to 0.1.
        attention_dropout(float): The ratio used to construct attention dropout modules, which indicates the percentage of parameters should be casted to zero, defaults to 0.1.
        layernorm_epsilon(float): The argument used to construct layernorm modules, defaults to 1e-5.
        activation(Callable): The activation function used in model, defaults to nn.functional.gelu.
        padding_idx(int): The length to be padded for each batch, defaults to None.
        dtype (:class:`torch.dtype`): The dtype of parameters, defaults to None.
        bias (bool): If set to ``False``, the layer will not learn an additive bias, defaults to ``True``.
        apply_post_layernorm(bool): If set to "True", the residual value will be record after layernorm modules, defaults to ``False``.
        fuse_scale_mask_softmax(bool): If set to "True", FuseScaleMaskSoftmax will be used in self-attention layer, defaults to ``False``.
        checkpoint(bool): If set to "True", checkpoint feature will be activated to save memory, defaults to ``False``.
        activation_offload(bool): If set to "True", offload feature will be activated during checkpointing, defaults to ``False``.
    """

    def __init__(self,
                 vocab_size: int = 50304,
                 max_position_embeddings: int = 1024,
                 hidden_size: int = 768,
                 num_heads: int = 12,
                 depth: int = 12,
                 mlp_ratio: float = 4.0,
                 dropout: float = 0.1,
                 embedding_dropout: float = 0.1,
                 attention_dropout: float = 0.1,
                 layernorm_epsilon: float = 1e-5,
                 activation: Callable = nn.functional.gelu,
                 padding_idx: int = None,
                #  dtype: dtype = None,     # TODO: check what the default dtype ype is on nn.Module
                 embed_dtype: dtype = torch.float16,     # WARNING: Adding int8 for extreme comparison.
                 decoder_dtype: dtype = None,     # WARNING: Adding int8 for extreme comparison.
                 layernorm_dtype: dtype = None,     # WARNING: Adding int8 for extreme comparison.
                 head_dtype: dtype = None,     # WARNING: Adding int8 for extreme comparison.
                 bias: bool = True,
                 apply_post_layernorm: bool = False,
                 fuse_scale_mask_softmax: bool = False,
                 checkpoint: bool = False,
                 activation_offload: bool = False) -> None:
        
        # Class variables
        self.embed_dtype = embed_dtype
        self.decoder_dtype = decoder_dtype
        self.layernorm_dtype = layernorm_dtype
        self.head_dtype = head_dtype
        self.bias = bias
        
        super().__init__()
        self.embed = GPTEmbedding(embedding_dim=hidden_size,
                                  vocab_size=vocab_size,
                                  max_position_embeddings=max_position_embeddings,
                                  padding_idx=padding_idx,
                                  dropout=embedding_dropout,
                                  dtype=embed_dtype)
        self.blocks = nn.ModuleList([
            GPTBlock(hidden_size=hidden_size,
                     num_heads=num_heads,
                     mlp_ratio=mlp_ratio,
                     activation=activation,
                     attention_dropout=attention_dropout,
                     dropout=dropout,
                     layernorm_epsilon=layernorm_epsilon,
                     dtype=decoder_dtype,
                     bias=bias,
                     apply_post_layernorm=apply_post_layernorm,
                     fuse_scale_mask_softmax=fuse_scale_mask_softmax,
                     checkpoint=checkpoint,
                     activation_offload=activation_offload) for _ in range(depth)
        ])
        
        # self.layer_norm = []
        self.embed_layer_norm = []
        self.head_layer_norm = []
        self.decoder_layer_norm = []

        self.norm = col_nn.LayerNorm(normalized_shape=hidden_size, eps=layernorm_epsilon, dtype=layernorm_dtype)

        self.head = GPTLMHead(
            hidden_size=hidden_size,
            vocab_size=vocab_size,
            embedding_layer=self.embed,
        # word_embeeding_weight=self.embed.word_embedding_weight,
            dtype=head_dtype)

    def forward(self, input_ids, attention_mask=None):

        # the size of input_ids is (BATCH_SIZE, SEQ_LEN)
        x = self.embed(input_ids)
        # the size of x after embed layer is (BATCH_SIZE, SEQ_LEN, HIDDEN_SIZE)

        ## NEW CASTING WORK
        x = x.to(dtype=self.decoder_dtype)
        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # Adapted from huggingface
        if attention_mask is not None:
            batch_size = input_ids.shape[0]
            attention_mask = attention_mask.view(batch_size, -1)
            attention_mask = col_nn.partition_batch(attention_mask)
            attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
            attention_mask = attention_mask.to(dtype=x.dtype)    # fp16 compatibility
            attention_mask = (1.0 - attention_mask) * -10000.0

        # the size of x in blocks is (BATCH_SIZE, SEQ_LEN, HIDDEN_SIZE)
        for block in self.blocks:
            x, attention_mask = block(x, attention_mask)

        # with suppress(Exception): print("X. dtype before attention head step 👇")
        # with suppress(Exception): print("X/embed: ", x.dtype)
        # with suppress(Exception): print("Norm in total: ", self.norm)
        # with suppress(Exception): print("self.norm.weight.dtype: ", self.norm.weight.dtype)
        # with suppress(Exception): print("self.norm.bias.dtype: ", self.norm.bias.dtype)
        # with suppress(Exception): print("self.head.dtype: ", self.head.dtype)
        # with suppress(Exception): print("self.head.dense.dtype: ", self.head.weight.dtype)
        # with suppress(Exception): print("self.head.dtype: ", self.head.dense)
        # with suppress(Exception): print("self.head.dtype: ", self.head.dense.dtype)
        # with suppress(Exception): print("self.head.bias: ", self.head.bias)
        # with suppress(Exception): print("self.head.word_embedding_weight.dtype: ", self.head.word_embedding_weight.dtype)
        
        x = self.head(self.norm(x))
        # the size of x is (BATCH_SIZE, SEQ_LEN, VOCAB_SIZE)

        return x

# example specifying dtypes
# Quant_GPT(embed_dtype=torch.float16, head_dtype=torch.float32, layernorm_dtype=torch.float32, decoder_dtype=torch.float32)

def _create_gpt_model(**model_kwargs):
    model = Quant_GPT(**model_kwargs)
    return model


def quant_gpt2_micro(**kwargs):
    model_kwargs = dict(hidden_size=768, depth=4, num_heads=4, **kwargs)
    return _create_gpt_model(**model_kwargs)

#### STANDARD MODELS (colossalai-implemented) ####

def quant_gpt2_small(**kwargs):
    model_kwargs = dict(hidden_size=768, depth=12, num_heads=12, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_medium(**kwargs):
    model_kwargs = dict(hidden_size=1024, depth=24, num_heads=8, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_large(**kwargs):
    model_kwargs = dict(hidden_size=1536, depth=36, num_heads=12, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_xl(**kwargs):
    model_kwargs = dict(hidden_size=1600, depth=48, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_2B(**kwargs):
    model_kwargs = dict(hidden_size=2048, depth=40, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_3B(**kwargs):
    model_kwargs = dict(hidden_size=2304, depth=48, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_4B(**kwargs):
    model_kwargs = dict(hidden_size=2304, depth=64, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_6B(**kwargs):
    model_kwargs = dict(hidden_size=4096, depth=30, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_8B(**kwargs):
    model_kwargs = dict(hidden_size=3072, depth=72, num_heads=24, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_12B(**kwargs):
    model_kwargs = dict(hidden_size=4096, depth=60, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_15B(**kwargs):
    model_kwargs = dict(hidden_size=4096, depth=78, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_18B(**kwargs):
    model_kwargs = dict(hidden_size=4096, depth=90, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_20B(**kwargs):
    model_kwargs = dict(hidden_size=8192, depth=25, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_24B(**kwargs):
    model_kwargs = dict(hidden_size=8192, depth=30, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_28B(**kwargs):
    model_kwargs = dict(hidden_size=8192, depth=35, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_32B(**kwargs):
    model_kwargs = dict(hidden_size=8192, depth=40, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_36B(**kwargs):
    model_kwargs = dict(hidden_size=8192, depth=45, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt2_40B(**kwargs):
    model_kwargs = dict(hidden_size=8192, depth=50, num_heads=16, **kwargs)
    return _create_gpt_model(**model_kwargs)


def quant_gpt3(**kwargs):
    model_kwargs = dict(hidden_size=12288, depth=96, num_heads=96, **kwargs)
    return _create_gpt_model(**model_kwargs)
