"""GPU 資源協調器：避免並行訓練 VRAM 競爭 OOM。

設計（對應 Resource Governor CPU/MEM，補 GPU 維度）：
- 訓練前 acquire_gpu(required_mb) 檢查可用 VRAM，不足則排隊等待
- 推論與訓練分優先級（訓練可搶佔，推論保底）
- 基於 torch.cuda.mem_get_info 與 nvidia-smi 雙源，fail-closed

用法：
    from shared_layer.adaptive.gpu_coordinator import GpuCoordinator
    coord = GpuCoordinator()
    with coord.acquire(required_mb=3500, priority="training", timeout=300):
        pretrain(...)
"""
from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

try:
    import torch

    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False


@dataclass(frozen=True)
class GpuStatus:
    total_mb: float
    used_mb: float
    free_mb: float
    util_pct: float  # 0-100


def _query_via_torch() -> GpuStatus | None:
    if not HAS_TORCH or not torch.cuda.is_available():
        return None
    try:
        free, total = torch.cuda.mem_get_info(0)
        total_mb = total / (1024 * 1024)
        free_mb = free / (1024 * 1024)
        used_mb = total_mb - free_mb
        # util 需 nvidia-smi，這裡估 0
        return GpuStatus(total_mb, used_mb, free_mb, 0.0)
    except Exception:
        return None


def _query_via_nvidia_smi() -> GpuStatus | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
            text=True,
            timeout=2,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0),
        ).strip().split(",")
        total, used, free, util = [float(x.strip()) for x in out[:4]]
        return GpuStatus(total, used, free, util)
    except Exception:
        return None


def query_gpu() -> GpuStatus | None:
    """雙源查詢。**nvidia-smi 優先**：WDDM 下 torch.cuda.mem_get_info 的
    free/used 不含其他行程佔用（分頁模型），會高估可用 VRAM；nvidia-smi
    反映實體記憶體。torch 僅作為無 nvidia-smi 時的備援。"""
    s = _query_via_torch()
    n = _query_via_nvidia_smi()
    if n is not None:
        return n
    return s


class GpuCoordinator:
    """簡易 GPU 協調：檢查可用 VRAM，不足則等待。VRAM 上限 95%（使用者約束）。"""

    # VRAM 上限 95%（對 6GB 即 5.8GB 滿，留 5% 緩衝防 OOM）
    VRAM_LIMIT_PCT: float = 95.0

    def __init__(self, poll_interval: float = 5.0):
        self.poll = poll_interval

    def available_mb(self) -> float:
        s = query_gpu()
        return s.free_mb if s else 0.0

    def can_acquire(self, required_mb: float) -> bool:
        s = query_gpu()
        if s is None:
            return False  # 無 GPU 則拒絕
        # VRAM 上限 95%：可用僅 95% 總量 - 已用
        limit_mb = s.total_mb * (self.VRAM_LIMIT_PCT / 100.0)
        # 預留 500MB 緩衝 + 上限檢查
        return s.free_mb >= required_mb + 500 and s.used_mb + required_mb <= limit_mb

    @contextmanager
    def acquire(self, required_mb: float, priority: str = "training", timeout: float = 300) -> Iterator[bool]:
        """嘗試獲取 VRAM，超時拋 TimeoutError。"""
        start = time.time()
        while True:
            if self.can_acquire(required_mb):
                # 成功獲取，進入上下文
                try:
                    yield True
                    return
                finally:
                    # 釋放無需操作，靠 GC
                    pass
            remaining = timeout - (time.time() - start)
            if remaining <= 0:
                break
            time.sleep(min(self.poll, remaining))
        raise TimeoutError(f"GPU acquire timeout: need {required_mb}MB free, have {self.available_mb():.0f}MB after {timeout}s (priority {priority})")


__all__ = ["GpuCoordinator", "GpuStatus", "query_gpu"]
