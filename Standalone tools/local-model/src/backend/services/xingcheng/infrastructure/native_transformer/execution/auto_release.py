"""自動釋放資源：模型/KV/Tokenizer 在閒置或壓力下自動卸載。

設計（對應 lifecycle LOADED→UNLOADED）：
- 閒置超時：上次推論後 N 分鐘無請求，自動釋放 GPU/CPU（正確性：權重仍在 checkpoint，可重載）
- 壓力觸發：memory_pressure >80% 或 VRAM >95% 時，立即釋放最久未用者
- 顯式釋放：Generator/Model 的 close() 或 del 時，經弱引用與 contextmanager 保證釋放
- 速度：釋放後重載需 1-2s（從 checkpoint），權衡 VRAM 節省
"""

from __future__ import annotations

import threading
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Iterator

try:
    import torch
except ImportError:  # torch is an on-demand dep — the manager must still
    torch = None  # type: ignore[assignment]  # import without it (A57)

DEFAULT_IDLE_SECONDS = 300  # 5 分鐘閒置自動卸載
DEFAULT_CHECK_INTERVAL = 60  # 每 60s 檢查


class ResidencyState:
    """Bounded model resource lifecycle states (PERF-08)."""

    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"
    EVICTING = "EVICTING"
    LOADING = "LOADING"


class AutoReleaseManager:
    """追蹤可釋放資源，閒置或壓力時自動釋放。"""

    def __init__(self, idle_seconds: int = DEFAULT_IDLE_SECONDS):
        self.idle = idle_seconds
        self._resources: dict[str, dict[str, Any]] = {}  # id -> {obj_ref, last_used, release_fn, size_mb}
        self._states: dict[str, str] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._start_timer()

    def _start_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(DEFAULT_CHECK_INTERVAL, self._check)
        self._timer.daemon = True
        self._timer.start()

    def register(self, key: str, obj: Any, release_fn: Callable[[Any], None], size_mb: float = 0) -> None:
        """註冊資源，obj 以弱引用持有，避免阻止 GC。"""
        with self._lock:
            # 弱引用：若外部已釋放，此處不阻止
            try:
                ref = weakref.ref(obj)
            except TypeError:
                # 不可弱引用的對象（如 torch.Tensor），直接持有但標記
                ref = lambda: obj  # type: ignore
            self._resources[key] = {
                "ref": ref,
                "last_used": time.time(),
                "release_fn": release_fn,
                "size_mb": size_mb,
            }
            self._states[key] = ResidencyState.HOT

    def begin_load(self, key: str) -> None:
        """Mark a resource as loading before expensive model construction."""
        with self._lock:
            self._states[key] = ResidencyState.LOADING

    def load_failed(self, key: str) -> None:
        """Remove a failed load without leaving a phantom resident entry."""
        with self._lock:
            self._states.pop(key, None)

    def state(self, key: str) -> str | None:
        with self._lock:
            return self._states.get(key)

    def touch(self, key: str) -> None:
        """Mark a resource HOT when it is actively requested."""
        with self._lock:
            if key in self._resources:
                self._resources[key]["last_used"] = time.time()
                self._states[key] = ResidencyState.HOT

    def mark_warm(self, key: str) -> None:
        """Mark a loaded resource reusable but not actively executing."""
        with self._lock:
            if key in self._resources:
                self._states[key] = ResidencyState.WARM

    def release(self, key: str) -> bool:
        """顯式釋放。"""
        with self._lock:
            info = self._resources.pop(key, None)
            if info is None:
                self._states.pop(key, None)
                return False
            self._states[key] = ResidencyState.EVICTING
            obj = info["ref"]()
            if obj is not None:
                try:
                    info["release_fn"](obj)
                except Exception:
                    pass
            self._states.pop(key, None)
            return True

    def _check(self) -> None:
        """定時檢查：閒置或壓力釋放。"""
        try:
            now = time.time()
            try:
                from .memory import device_memory_info, memory_pressure

                pressure = memory_pressure()
            except ImportError:
                pressure = 0.0  # torch absent → no memory telemetry
                device_memory_info = None
            # 查詢 VRAM
            vram_pressure = 0.0
            try:
                info = device_memory_info("cuda") if device_memory_info else {}
                if info.get("total_gb", 0) > 0:
                    vram_pressure = info["used_gb"] / info["total_gb"]
            except Exception:
                pass

            to_release: list[str] = []
            with self._lock:
                for k, v in list(self._resources.items()):
                    idle = now - v["last_used"]
                    # 閒置超時 或 壓力 >80% (RAM) / 95% (VRAM) 立即釋放
                    if idle > self.idle or pressure > 0.8 or vram_pressure > 0.95:
                        self._states[k] = ResidencyState.COLD
                        to_release.append(k)

            for k in to_release:
                self.release(k)
        finally:
            self._start_timer()

    def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


# 全域單例
_global_manager: AutoReleaseManager | None = None
_global_lock = threading.Lock()

def get_manager() -> AutoReleaseManager:
    global _global_manager
    with _global_lock:
        if _global_manager is None:
            _global_manager = AutoReleaseManager()
        return _global_manager

@contextmanager
def auto_release_context(key: str, obj: Any, release_fn: Callable[[Any], None], size_mb: float = 0) -> Iterator[None]:
    """上下文：進入時註冊，離開時觸摸（更新時間），超時後自動釋放。"""
    mgr = get_manager()
    mgr.register(key, obj, release_fn, size_mb)
    try:
        yield
        mgr.mark_warm(key)
    finally:
        # 不立即釋放，靠閒置檢查；若需立即釋放，調用 mgr.release(key)
        pass

# 便捷釋放函式
def release_model(model: Any) -> None:
    """釋放模型：移至 CPU 並清空 CUDA cache（正確性：權重仍在 checkpoint）。"""
    try:
        model.to("cpu")
        # 刪除優化器狀態等
        for p in model.parameters():
            if p.grad is not None:
                p.grad = None
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

def release_kv_cache(cache: Any) -> None:
    """釋放 KV cache：重置並歸還池。"""
    try:
        cache.reset()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

__all__ = ["ResidencyState", "AutoReleaseManager", "get_manager", "auto_release_context", "release_model", "release_kv_cache"]
