"""Prefix Cache：跨請求重用 prompt 前綴的 K/V。

對話場景中 system prompt 與歷史輪次在多請求間重複——本模組把
已算過的 prefix K/V 以「token 前綴」為鍵保存，下次請求命中時
只對**新增尾段**做 prefill，省掉重複算量。

規則：

- 只服務 ``batch_size == 1`` 且 ``use_cache`` 的請求（多 batch 的
  padding/長度差異讓 prefix 語意不安全）；
- 命中必須是**完整前綴**（逐 token 比對），部份命中從命中點續算；
- 只提交「prompt 段」的 K/V，生成段不進 cache（內容每次不同）；
- 有界 LRU（``max_entries``）；entry 存的是 detach+clone 的張量，
  與工作 KVCache 互不干擾；
- 模型 / tokenizer / 裝置 / dtype 任一不同即視為無效（鍵含指紋）。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Any, Iterable, Sequence

import torch


def _fingerprint(tokens: Sequence[int], *, tag: str) -> str:
    digest = hashlib.sha256(tag.encode("utf-8"))
    digest.update(bytes(len(tokens).to_bytes(8, "little")))
    for token in tokens:
        digest.update(int(token).to_bytes(4, "little", signed=False))
    return digest.hexdigest()


class PrefixKVStore:
    """prefix token 序列 → 每層 (K, V) 張量清單的有界快取。"""

    def __init__(self, max_entries: int = 8, *, tag: str = "") -> None:
        self.max_entries = max(1, int(max_entries))
        self.tag = str(tag)
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._keys: dict[str, list[int]] = {}  # digest -> token list
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def _put_tokens(self, tokens: Sequence[int]) -> str:
        return _fingerprint(tokens, tag=self.tag)

    def put(
        self,
        tokens: Sequence[int],
        layer_kv: Sequence[tuple[torch.Tensor, torch.Tensor]],
    ) -> str:
        """登錄一段 prefix 的 K/V（detach+clone）。"""
        token_list = [int(t) for t in tokens]
        key = self._put_tokens(token_list)
        stored = [
            (k.detach().clone(), v.detach().clone()) for k, v in layer_kv
        ]
        self._entries[key] = {
            "layer_kv": stored,
            "length": len(token_list),
            "created_at": time.time(),
        }
        self._keys[key] = token_list
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            old_key, _ = self._entries.popitem(last=False)
            self._keys.pop(old_key, None)
        return key

    def longest_prefix_match(
        self, tokens: Sequence[int]
    ) -> tuple[int, list[tuple[torch.Tensor, torch.Tensor]] | None]:
        """找與 ``tokens`` 共同前綴最長的快取項。

        回傳 ``(命中長度, layer_kv)``；無命中回 ``(0, None)``。
        比對以 token 為單位，命中長度即該 entry 的完整長度
        （entry 只在整條皆為前綴時算命中）。
        """
        token_list = [int(t) for t in tokens]
        best_key: str | None = None
        best_len = 0
        for key, cached_tokens in self._keys.items():
            length = len(cached_tokens)
            if length == 0 or length > len(token_list):
                continue
            if cached_tokens == token_list[:length] and length > best_len:
                best_key = key
                best_len = length
        if best_key is None:
            self.misses += 1
            return 0, None
        self.hits += 1
        entry = self._entries[best_key]
        self._entries.move_to_end(best_key)
        return best_len, entry["layer_kv"]

    def drop(self, tokens: Sequence[int]) -> bool:
        key = self._put_tokens([int(t) for t in tokens])
        existed = self._entries.pop(key, None) is not None
        self._keys.pop(key, None)
        return existed

    def clear(self) -> None:
        self._entries.clear()
        self._keys.clear()

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
        }


__all__ = ["PrefixKVStore"]
