"""Token + Position Embedding。"""

from __future__ import annotations

import torch
import torch.nn as nn

from ..config import XingChengConfig


class XingChengEmbeddings(nn.Module):
    """Token embedding + 選用 learned / RoPE 位置。

    RoPE 模式下不建立位置 embedding（位置資訊在 Attention 內透過旋轉編碼）。
    """

    def __init__(self, config: XingChengConfig) -> None:
        super().__init__()
        self.config = config
        self.word_embeddings = nn.Embedding(
            config.vocab_size, config.hidden_size, padding_idx=config.pad_token_id
        )
        if config.position_embedding_type == "learned":
            self.position_embeddings = nn.Embedding(
                config.max_position_embeddings, config.hidden_size
            )
        else:
            self.position_embeddings = None
        self.dropout = nn.Dropout(config.embed_dropout)
        self._init_weights()

    def _init_weights(self) -> None:
        nn.init.normal_(self.word_embeddings.weight, mean=0.0, std=self.config.initializer_range)
        if self.word_embeddings.padding_idx is not None:
            with torch.no_grad():
                self.word_embeddings.weight[self.word_embeddings.padding_idx].fill_(0.0)
        if self.position_embeddings is not None:
            nn.init.normal_(
                self.position_embeddings.weight, mean=0.0, std=self.config.initializer_range
            )

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor | None = None,
    ) -> torch.Tensor:
        embeddings = self.word_embeddings(input_ids)
        if self.position_embeddings is not None:
            seq_len = input_ids.size(-1)
            if position_ids is None:
                position_ids = torch.arange(seq_len, device=input_ids.device).unsqueeze(0)
            embeddings = embeddings + self.position_embeddings(position_ids)
        return self.dropout(embeddings)


__all__ = ["XingChengEmbeddings"]
