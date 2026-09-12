"""Weight-only 量化：將 nn.Linear 權重量化為 INT8/INT4，推論時動態反量化。

對應技術棧中的 Quantization / Dequantization 自研 kernel（Triton → PyTorch）。
第一版以 per-tensor 對稱量化為主，per-channel / group-wise 留待後續。
"""

from __future__ import annotations

import torch
import torch.nn as nn

from ..kernels import quantize_per_tensor, dequantize_per_tensor


class QuantizedLinear(nn.Module):
    """動態反量化的量化 Linear 層。"""

    def __init__(
        self,
        in_features: int,
        out_features: int,
        bias: bool = True,
        n_bits: int = 8,
    ) -> None:
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.n_bits = n_bits
        # 量化權重（INT8 容器）+ scale
        self.register_buffer("weight_q", torch.zeros(out_features, in_features, dtype=torch.int8))
        self.register_buffer("weight_scale", torch.ones(1))
        if bias:
            self.register_buffer("bias", torch.zeros(out_features))
        else:
            self.bias = None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        w = dequantize_per_tensor(self.weight_q, self.weight_scale, out_dtype=x.dtype)
        return torch.nn.functional.linear(x, w, self.bias)

    @classmethod
    def from_linear(cls, linear: nn.Linear, n_bits: int = 8) -> "QuantizedLinear":
        ql = cls(linear.in_features, linear.out_features, bias=linear.bias is not None, n_bits=n_bits)
        q, scale = quantize_per_tensor(linear.weight.detach(), n_bits=n_bits)
        ql.weight_q.copy_(q)
        ql.weight_scale.copy_(scale)
        if linear.bias is not None:
            ql.bias.copy_(linear.bias.detach())
        return ql


def quantize_model(model: nn.Module, n_bits: int = 8) -> nn.Module:
    """將模型內所有 nn.Linear 替換為 QuantizedLinear。"""
    for name, child in model.named_children():
        if isinstance(child, nn.Linear) and name != "lm_head":
            setattr(model, name, QuantizedLinear.from_linear(child, n_bits=n_bits))
        else:
            quantize_model(child, n_bits=n_bits)
    return model


def dequantize_model(model: nn.Module) -> nn.Module:
    """將 QuantizedLinear 還原為 nn.Linear（用於精確驗證或混合精度）。"""
    for name, child in model.named_children():
        if isinstance(child, QuantizedLinear):
            w = dequantize_per_tensor(child.weight_q, child.weight_scale, out_dtype=torch.float32)
            linear = nn.Linear(child.in_features, child.out_features, bias=child.bias is not None)
            linear.weight = nn.Parameter(w)
            if child.bias is not None:
                linear.bias = nn.Parameter(child.bias.clone().to(torch.float32))
            setattr(model, name, linear)
        else:
            dequantize_model(child)
    return model


__all__ = ["QuantizedLinear", "quantize_model", "dequantize_model"]
