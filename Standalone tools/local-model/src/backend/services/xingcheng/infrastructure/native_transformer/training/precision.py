"""星澄訓練精度策略：依硬體能力選擇 BF16 / FP16 / FP32。

規則（``resolve_precision(device, requested)``）：

- ``auto``：CUDA → BF16（支援時）否則 FP16；MPS / CPU → FP32。
- ``bf16``：CUDA 或支援 bf16 autocast 的裝置；CPU 允許但通常較慢。
- ``fp16``：僅 CUDA（需 ``GradScaler`` 防梯度下溢）。
- ``fp32``：任何裝置，關閉 autocast。

消費端使用 ``plan.autocast()`` 包住 forward、``plan.scaler()``
取得 ``GradScaler``（不需 scaler 時回傳 ``None``——呼叫端可無條件走
``scale→backward→step→update`` 慣用式，見 ``pretrain`` / ``sft``）。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import torch

_PRECISIONS = frozenset({"auto", "bf16", "fp16", "fp32"})


@dataclass(frozen=True)
class PrecisionPlan:
    """單次訓練 run 的精度決議結果。"""

    name: str
    dtype: torch.dtype | None  # None = 不 autocast（fp32）
    device_type: str
    needs_scaler: bool

    @property
    def enabled(self) -> bool:
        return self.dtype is not None

    @contextmanager
    def autocast(self) -> Iterator[None]:
        if self.dtype is None:
            yield
        else:
            with torch.autocast(device_type=self.device_type, dtype=self.dtype):
                yield

    def scaler(self) -> Any:
        if not self.needs_scaler:
            return None
        try:
            return torch.amp.GradScaler(self.device_type)
        except (AttributeError, TypeError):  # pragma: no cover - 舊版 torch
            return torch.cuda.amp.GradScaler()


def _cuda_bf16_supported() -> bool:
    try:
        return bool(torch.cuda.is_bf16_supported())
    except Exception:
        return False


def resolve_precision(
    device: torch.device | str,
    requested: str = "auto",
) -> PrecisionPlan:
    """依裝置與請求解析精度；不支援的組合 fail-closed 回 FP32。"""
    dev = torch.device(device)
    want = str(requested or "auto").strip().casefold()
    if want not in _PRECISIONS:
        raise ValueError(f"PRECISION_UNKNOWN:{requested}")

    if want == "auto":
        if dev.type == "cuda":
            want = "bf16" if _cuda_bf16_supported() else "fp16"
        else:
            want = "fp32"

    if want == "bf16":
        if dev.type in ("cuda", "cpu", "mps"):
            return PrecisionPlan("bf16", torch.bfloat16, dev.type, False)
        return PrecisionPlan("fp32", None, dev.type, False)
    if want == "fp16":
        if dev.type == "cuda":
            return PrecisionPlan("fp16", torch.float16, "cuda", True)
        # 非 CUDA 不接受 fp16（無 scaler 保護易下溢）→ 退回 fp32
        return PrecisionPlan("fp32", None, dev.type, False)
    return PrecisionPlan("fp32", None, dev.type, False)


__all__ = ["PrecisionPlan", "resolve_precision"]
