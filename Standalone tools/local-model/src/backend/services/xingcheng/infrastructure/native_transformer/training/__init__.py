"""星澄訓練層：語料 / Optimizer / Dataset / Trainer / 預訓練。

對應技術棧：
  - Autograd 自動求導 / Backward Graph / Gradient 計算
  - Optimizer：AdamW（PyTorch 內建，背後走 ATen / Dispatcher）
  - 訓練流程：Computation Graph 管理 + 參數更新
"""

from __future__ import annotations

from .corpus import (
    CorpusDocument,
    build_corpus,
    iter_documents,
    read_corpus,
)
from .optimizer import build_optimizer
from .data import TextDataset, collate_batch
from .pretrain import (
    PretrainConfig,
    encode_documents,
    evaluate,
    pack_blocks,
    pretrain,
)
from .trainer import Trainer, TrainingConfig, make_dataloader

__all__ = [
    "CorpusDocument",
    "PretrainConfig",
    "TextDataset",
    "Trainer",
    "TrainingConfig",
    "build_corpus",
    "build_optimizer",
    "collate_batch",
    "encode_documents",
    "evaluate",
    "iter_documents",
    "make_dataloader",
    "pack_blocks",
    "pretrain",
    "read_corpus",
]
