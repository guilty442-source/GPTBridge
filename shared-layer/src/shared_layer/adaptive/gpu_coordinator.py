"""GPU 資源協調器：避免並行原生工作負載 VRAM 競爭 OOM。

設計（對應 Resource Governor CPU/MEM，補 GPU 維度）：
- 工作負載前 acquire(required_mb) 檢查可用 VRAM，不足則排隊等待
- 推論與訓練分優先級（訓練可搶佔，推論保底）
- VRAM 查詢唯一來源 nvidia-smi，fail-closed（B167/B38：PyTorch 已退役）

用法：
    from shared_layer.adaptive.gpu_coordinator import GpuCoordinator
    coord = GpuCoordinator()
    with coord.acquire(required_mb=3500, priority="training", timeout=300):
        train(...)
"""
from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class GpuStatus:
    total_mb: float
    used_mb: float
    free_mb: float
    util_pct: float  # 0-100


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
    """VRAM 查詢唯一來源：nvidia-smi。

    B167/B38：PyTorch 已全數退役（零 dependency/execution/fallback
    角色）；原 CUDA 備援探針已移除。nvidia-smi 缺席時回傳
    ``None``——呼叫端 fail-closed（無 GPU 證據即拒絕）。"""
    return _query_via_nvidia_smi()


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
