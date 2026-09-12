"""星澄記憶體管理工具。

對應技術棧中的 Memory Management / Stream Management。
本模組在 Python 層提供：
  - 裝置感知的空張量配置
  - KV Cache 記憶體預留
  - CUDA 顯存使用量查詢
  - 記憶體壓力下的逐級降級協調
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import torch

from .backend import capabilities, resolve_device

log = logging.getLogger(__name__)


def empty_tensor(
    *shape: int,
    dtype: torch.dtype | None = None,
    device: str | torch.device | None = None,
    pin_memory: bool | None = None,
) -> torch.Tensor:
    """配置未初始化張量；CUDA 路徑使用 caching allocator。"""
    dev = resolve_device(device)
    if dtype is None:
        dtype = torch.float32
    pinned = bool(pin_memory) if pin_memory is not None else dev.type == "cpu"
    return torch.empty(shape, dtype=dtype, device=dev, pin_memory=pinned)


def zeros_tensor(
    *shape: int,
    dtype: torch.dtype | None = None,
    device: str | torch.device | None = None,
) -> torch.Tensor:
    dev = resolve_device(device)
    if dtype is None:
        dtype = torch.float32
    return torch.zeros(shape, dtype=dtype, device=dev)


def device_memory_info(device: str | torch.device | None = None) -> dict[str, float]:
    """回傳裝置記憶體資訊（GB）。CPU 回推測值。"""
    dev = resolve_device(device)
    info = {"device": str(dev), "total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0}
    if dev.type == "cuda":
        try:
            free, total = torch.cuda.mem_get_info(dev)
            info["total_gb"] = total / (1024 ** 3)
            info["used_gb"] = (total - free) / (1024 ** 3)
            info["free_gb"] = free / (1024 ** 3)
        except Exception:
            pass
    elif dev.type == "cpu":
        try:
            import psutil  # type: ignore
            vm = psutil.virtual_memory()
            info["total_gb"] = vm.total / (1024 ** 3)
            info["used_gb"] = vm.used / (1024 ** 3)
            info["free_gb"] = vm.available / (1024 ** 3)
        except Exception:
            pass
    return info


def memory_pressure(device: str | torch.device | None = None) -> float:
    """0.0 ~ 1.0 的記憶體壓力指標。"""
    info = device_memory_info(device)
    total = info["total_gb"]
    if total <= 0:
        return 0.0
    return min(1.0, info["used_gb"] / total)


@contextmanager
def cuda_cache_cleanup() -> Iterator[None]:
    """進入時清空 CUDA cache；離開時再次清空，便於精確測量。"""
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    try:
        yield
    finally:
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()


def suggest_context_window(
    base: int,
    *,
    pressure: float | None = None,
    min_window: int = 2_048,
    steps: tuple[int, ...] = (2_048, 4_096, 8_192, 16_384, 32_768, 65_536),
) -> int:
    """依記憶體壓力逐級調整 context window（與既有 runtime 行為一致）。"""
    if pressure is None:
        pressure = memory_pressure()
    if pressure >= 0.85:
        return min(base, min_window)
    if pressure >= 0.7:
        return min(base, steps[1] if len(steps) > 1 else min_window)
    return base


__all__ = [
    "empty_tensor",
    "zeros_tensor",
    "device_memory_info",
    "memory_pressure",
    "cuda_cache_cleanup",
    "suggest_context_window",
]
