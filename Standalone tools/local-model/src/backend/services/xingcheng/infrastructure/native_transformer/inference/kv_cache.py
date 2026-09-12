"""KV Cache：推論期快取每層的 K / V，避免重算 prefix。

對應技術棧中的 KV Cache 自研 kernel（Triton 路徑留待瓶頸下沉）。
第一版以 PyTorch 張量管理，預留形狀與配置策略。
"""

from __future__ import annotations

import torch

from ..config import XingChengConfig


class KVCache:
    """逐層 KV Cache 容器。"""

    def __init__(
        self,
        config: XingChengConfig,
        batch_size: int,
        max_seq_len: int,
        device: torch.device,
        dtype: torch.dtype,
    ) -> None:
        self.config = config
        self.batch_size = batch_size
        self.max_seq_len = max_seq_len
        self.device = device
        self.dtype = dtype
        self.layers: list[tuple[torch.Tensor, torch.Tensor]] = []
        self._init_layers()

    def _init_layers(self) -> None:
        cfg = self.config
        for _ in range(cfg.num_hidden_layers):
            k = torch.zeros(
                self.batch_size, cfg.num_key_value_heads, self.max_seq_len, cfg.head_dim,
                device=self.device, dtype=self.dtype,
            )
            v = torch.zeros_like(k)
            self.layers.append((k, v))

    def __len__(self) -> int:
        return len(self.layers)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        return self.layers[idx]

    def slice(self, seq_len: int) -> list[tuple[torch.Tensor, torch.Tensor]]:
        """取出每層前 seq_len 個位置作為 past KV。"""
        return [(k[:, :, :seq_len, :], v[:, :, :seq_len, :]) for k, v in self.layers]

    def update(self, layer_idx: int, new_k: torch.Tensor, new_v: torch.Tensor, start: int) -> None:
        """將新算出的 K/V 寫入 cache 的 start 位置之後。"""
        s = new_k.size(2)
        k, v = self.layers[layer_idx]
        k[:, :, start : start + s, :] = new_k
        v[:, :, start : start + s, :] = new_v

    def reset(self) -> None:
        for k, v in self.layers:
            k.zero_()
            v.zero_()


__all__ = ["KVCache"]
