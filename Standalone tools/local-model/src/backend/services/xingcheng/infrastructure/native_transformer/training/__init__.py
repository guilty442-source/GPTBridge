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
from .distill import (
    DEFAULT_TEACHER_MODELS,
    DistillationTopic,
    build_distillation_snapshot,
    build_grounded_topics,
    generate_distillation_examples,
)
from .dpo import (
    DpoConfig,
    dpo_loss,
    dpo_train,
    dpo_train_from_repository,
    load_preference_pairs,
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
from .sft import (
    SFTConfig,
    SFTDataset,
    collate_sft,
    encode_sft_example,
    evaluate_sft,
    make_sft_loader,
    read_sft_jsonl,
    sft_text,
    sft_train,
)
from .trainer import Trainer, TrainingConfig, make_dataloader

__all__ = [
    "CorpusDocument",
    "DEFAULT_TEACHER_MODELS",
    "DistillationTopic",
    "DpoConfig",
    "PretrainConfig",
    "SFTConfig",
    "SFTDataset",
    "TextDataset",
    "Trainer",
    "TrainingConfig",
    "build_corpus",
    "build_distillation_snapshot",
    "build_grounded_topics",
    "build_optimizer",
    "collate_batch",
    "collate_sft",
    "generate_distillation_examples",
    "dpo_loss",
    "dpo_train",
    "dpo_train_from_repository",
    "encode_documents",
    "encode_sft_example",
    "evaluate",
    "evaluate_sft",
    "iter_documents",
    "load_preference_pairs",
    "make_dataloader",
    "make_sft_loader",
    "pack_blocks",
    "pretrain",
    "read_corpus",
    "read_sft_jsonl",
    "sft_text",
    "sft_train",
]
