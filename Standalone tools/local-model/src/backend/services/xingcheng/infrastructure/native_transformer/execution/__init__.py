"""星澄執行層（裝置 / 後端 / 記憶體管理）。

Lazy surface (PEP 562): torch is an on-demand dependency (A57/A615) —
importing this package or ``execution.auto_release`` must not require it.
Backend/memory attributes resolve on first access.
"""

from __future__ import annotations

_BACKEND_ATTRS = {
    "BackendCapabilities",
    "capabilities",
    "reset_capabilities",
    "resolve_device",
    "default_dtype",
    "enable_flash_attention",
    "set_gemm_backend",
}
_MEMORY_ATTRS = {
    "empty_tensor",
    "zeros_tensor",
    "device_memory_info",
    "memory_pressure",
    "cuda_cache_cleanup",
    "suggest_context_window",
}


def __getattr__(name: str):
    if name in _BACKEND_ATTRS:
        from . import backend

        return getattr(backend, name)
    if name in _MEMORY_ATTRS:
        from . import memory

        return getattr(memory, name)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
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
