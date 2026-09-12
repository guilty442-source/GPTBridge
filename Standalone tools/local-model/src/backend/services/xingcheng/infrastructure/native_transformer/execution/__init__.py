"""星澄執行層（裝置 / 後端 / 記憶體管理）。"""

from __future__ import annotations

from .backend import (
    BackendCapabilities,
    capabilities,
    reset_capabilities,
    resolve_device,
    default_dtype,
    enable_flash_attention,
    set_gemm_backend,
)
from .memory import (
    empty_tensor,
    zeros_tensor,
    device_memory_info,
    memory_pressure,
    cuda_cache_cleanup,
    suggest_context_window,
)

__all__ = [
    "BackendCapabilities",
    "capabilities",
    "reset_capabilities",
    "resolve_device",
    "default_dtype",
    "enable_flash_attention",
    "set_gemm_backend",
    "empty_tensor",
    "zeros_tensor",
    "device_memory_info",
    "memory_pressure",
    "cuda_cache_cleanup",
    "suggest_context_window",
]
