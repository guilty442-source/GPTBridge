"""Optimizer 建構：AdamW 為預設，可選 Lion / SGD。

對應技術棧：PyTorch Autograd + ATen Dispatcher 負責梯度計算與參數更新。
"""

from __future__ import annotations

from typing import Iterable

import torch
import torch.optim as optim

from ..config import XingChengConfig


def build_optimizer(
    params: Iterable[torch.nn.Parameter],
    config: XingChengConfig,
    *,
    lr: float = 3e-4,
    weight_decay: float = 0.01,
    betas: tuple[float, float] = (0.9, 0.999),
    eps: float = 1e-8,
    kind: str = "adamw",
) -> torch.optim.Optimizer:
    if kind == "adamw":
        return optim.AdamW(params, lr=lr, weight_decay=weight_decay, betas=betas, eps=eps)
    if kind == "sgd":
        return optim.SGD(params, lr=lr, weight_decay=weight_decay, momentum=0.9)
    raise ValueError(f"未知 optimizer kind: {kind}")


__all__ = ["build_optimizer"]
