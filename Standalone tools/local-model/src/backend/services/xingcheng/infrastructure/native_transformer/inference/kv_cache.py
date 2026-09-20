"""KV Cache：推論期快取每層的 K / V，避免重算 prefix。

支援三種儲存精度：
  - ``none``：與模型激活同 dtype（float32 / float16 / bfloat16）
  - ``int8``：INT8 K/V 搭配 **per-token、per-head** scale

量化正確性原則：每個 token 的尺度各自保存，絕不以單一 scale 覆蓋
整個序列——長上下文的生成品質必須與 FP 版本一致（差異在容忍度內）。
"""

from __future__ import annotations

import torch

from ..config import XingChengConfig

_QUANT_MODES = ("none", "int8")


def resolve_kv_dtype(value: str | torch.dtype | None, fallback: torch.dtype) -> torch.dtype:
    """解析 KV cache dtype；``None`` 時沿用模型激活 dtype。"""
    if value is None:
        return fallback
    if isinstance(value, torch.dtype):
        return value
    name = str(value).strip().casefold()
    mapping = {
        "float32": torch.float32,
        "fp32": torch.float32,
        "float16": torch.float16,
        "fp16": torch.float16,
        "half": torch.float16,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
    }
    if name not in mapping:
        raise ValueError(f"KV_CACHE_DTYPE_UNSUPPORTED:{value}")
    return mapping[name]


class KVCache:
    """逐層 KV Cache 容器（FP 或正規化 INT8）。"""

    def __init__(
        self,
        config: XingChengConfig,
        batch_size: int,
        max_seq_len: int,
        device: torch.device,
        dtype: torch.dtype,
        *,
        quant: str | None = None,
    ) -> None:
        self.config = config
        self.batch_size = int(batch_size)
        self.max_seq_len = int(max_seq_len)
        self.device = device
        self.dtype = dtype
        mode = (quant if quant is not None else getattr(config, "kv_cache_quant", "none")) or "none"
        self.quant = str(mode).strip().casefold()
        if self.quant not in _QUANT_MODES:
            raise ValueError(f"KV_CACHE_QUANT_UNSUPPORTED:{quant}")
        self.is_quantized = self.quant == "int8"
        self.layers: list[tuple[torch.Tensor, torch.Tensor]] = []
        self.scales: list[tuple[torch.Tensor, torch.Tensor]] | None = (
            [] if self.is_quantized else None
        )
        self._init_layers()

    # ── 初始化 ─────────────────────────────────────────────────
    def _init_layers(self) -> None:
        cfg = self.config
        for _ in range(cfg.num_hidden_layers):
            if self.is_quantized:
                k = torch.zeros(
                    self.batch_size, cfg.num_key_value_heads, self.max_seq_len,
                    cfg.head_dim, device=self.device, dtype=torch.int8,
                )
                v = torch.zeros_like(k)
                self.layers.append((k, v))
                assert self.scales is not None
                self.scales.append(
                    (
                        torch.ones(
                            self.batch_size, cfg.num_key_value_heads,
                            self.max_seq_len, device=self.device, dtype=torch.float32,
                        ),
                        torch.ones(
                            self.batch_size, cfg.num_key_value_heads,
                            self.max_seq_len, device=self.device, dtype=torch.float32,
                        ),
                    )
                )
            else:
                k = torch.zeros(
                    self.batch_size, cfg.num_key_value_heads, self.max_seq_len,
                    cfg.head_dim, device=self.device, dtype=self.dtype,
                )
                v = torch.zeros_like(k)
                self.layers.append((k, v))

    # ── 讀取 ───────────────────────────────────────────────────
    @staticmethod
    def _dequantize(block: torch.Tensor, scale: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
        return (block.to(torch.float32) * scale.unsqueeze(-1)).to(dtype)

    def __len__(self) -> int:
        return len(self.layers)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        k, v = self.layers[idx]
        if not self.is_quantized:
            return k, v
        assert self.scales is not None
        k_s, v_s = self.scales[idx]
        return (
            self._dequantize(k, k_s, self.dtype),
            self._dequantize(v, v_s, self.dtype),
        )

    def slice(self, seq_len: int) -> list[tuple[torch.Tensor, torch.Tensor]]:
        """取出每層前 seq_len 個位置作為 past KV。"""
        out: list[tuple[torch.Tensor, torch.Tensor]] = []
        for idx, (k, v) in enumerate(self.layers):
            if not self.is_quantized:
                out.append((k[:, :, :seq_len, :], v[:, :, :seq_len, :]))
                continue
            assert self.scales is not None
            k_s, v_s = self.scales[idx]
            out.append(
                (
                    self._dequantize(k[:, :, :seq_len, :], k_s[:, :, :seq_len], self.dtype),
                    self._dequantize(v[:, :, :seq_len, :], v_s[:, :, :seq_len], self.dtype),
                )
            )
        return out

    # ── 寫入 ───────────────────────────────────────────────────
    def update(
        self, layer_idx: int, new_k: torch.Tensor, new_v: torch.Tensor, start: int
    ) -> None:
        """將新算出的 K/V 寫入 cache 的 start 位置之後（含 per-token scale）。"""
        s = int(new_k.size(2))
        if s <= 0:
            return
        if self.is_quantized:
            assert self.scales is not None
            k_q, k_s = self._quantize_block(new_k)
            v_q, v_s = self._quantize_block(new_v)
            k, v = self.layers[layer_idx]
            k[:, :, start : start + s, :] = k_q
            v[:, :, start : start + s, :] = v_q
            scale_k, scale_v = self.scales[layer_idx]
            scale_k[:, :, start : start + s] = k_s
            scale_v[:, :, start : start + s] = v_s
            return
        k, v = self.layers[layer_idx]
        k[:, :, start : start + s, :] = new_k.to(k.dtype)
        v[:, :, start : start + s, :] = new_v.to(v.dtype)

    @staticmethod
    def _quantize_block(block: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """per-(batch, head, token) 對稱量化；回傳 (int8, scale)。"""
        value = block.to(torch.float32)
        abs_max = value.abs().amax(dim=-1).clamp_min(1e-8)
        scale = abs_max / 127.0
        quantized = torch.clamp(
            (value / scale.unsqueeze(-1)).round(), -127.0, 127.0
        ).to(torch.int8)
        return quantized, scale

    # ── 其他 ───────────────────────────────────────────────────
    def reset(self) -> None:
        for k, v in self.layers:
            k.zero_()
            v.zero_()
        if self.is_quantized and self.scales is not None:
            for scale_k, scale_v in self.scales:
                scale_k.fill_(1.0)
                scale_v.fill_(1.0)

    def memory_bytes(self) -> int:
        total = 0
        for k, v in self.layers:
            total += k.numel() * k.element_size() + v.numel() * v.element_size()
        if self.is_quantized and self.scales is not None:
            for scale_k, scale_v in self.scales:
                total += scale_k.numel() * scale_k.element_size()
                total += scale_v.numel() * scale_v.element_size()
        return total

    def describe(self) -> dict[str, object]:
        return {
            "quant": self.quant,
            "dtype": str(self.dtype).replace("torch.", ""),
            "layers": len(self.layers),
            "max_seq_len": self.max_seq_len,
            "memory_bytes": self.memory_bytes(),
        }


__all__ = ["KVCache", "resolve_kv_dtype"]
