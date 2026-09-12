"""Dataset / Collate：將文字轉成模型可吃的張量批次。"""

from __future__ import annotations

from typing import List, Sequence

import torch
from torch.utils.data import Dataset

from ..tokenizer import XingChengTokenizer


class TextDataset(Dataset):
    """逐筆文字 → token id 序列（含 BOS / EOS）。"""

    def __init__(
        self,
        texts: Sequence[str],
        tokenizer: XingChengTokenizer,
        max_length: int = 256,
    ) -> None:
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.samples: List[List[int]] = [
            tokenizer.encode(t, add_bos=True, add_eos=True, max_length=max_length)
            for t in texts
        ]

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> List[int]:
        return self.samples[idx]


def collate_batch(
    batch: Sequence[List[int]],
    pad_id: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    """將不等長序列 padding 成 (B, S)，回傳 (input_ids, attention_mask)。"""
    max_len = max(len(seq) for seq in batch)
    input_ids = torch.full((len(batch), max_len), pad_id, dtype=torch.long)
    attention_mask = torch.zeros((len(batch), max_len), dtype=torch.long)
    for i, seq in enumerate(batch):
        input_ids[i, : len(seq)] = torch.tensor(seq, dtype=torch.long)
        attention_mask[i, : len(seq)] = 1
    return input_ids, attention_mask


__all__ = ["TextDataset", "collate_batch"]
