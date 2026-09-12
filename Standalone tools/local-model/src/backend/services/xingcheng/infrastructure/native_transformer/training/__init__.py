"""星澄訓練層：Optimizer / Dataset / Trainer。

對應技術棧：
  - Autograd 自動求導 / Backward Graph / Gradient 計算
  - Optimizer：AdamW（PyTorch 內建，背後走 ATen / Dispatcher）
  - 訓練流程：Computation Graph 管理 + 參數更新
"""

from __future__ import annotations

from .optimizer import build_optimizer
from .data import TextDataset, collate_batch
from .trainer import Trainer, TrainingConfig, make_dataloader

__all__ = [
    "build_optimizer",
    "TextDataset",
    "collate_batch",
    "Trainer",
    "TrainingConfig",
    "make_dataloader",
]
