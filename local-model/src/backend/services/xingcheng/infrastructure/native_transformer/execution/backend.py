"""星澄執行後端與裝置管理。

對應技術棧：
  ATen / Dispatcher / Torch C++ Backend
    → 高效能數學與 Kernel (BLAS / oneDNN / cuBLASLt / cuDNN / FlashAttention)
    → Triton / Gluon / CUDA
    → PTX / SASS
    → CPU / NVIDIA GPU / (Apple MPS)

本模組在 Python 層偵測可用後端，並提供統一的裝置 / dtype / backend 描述物件，
讓上層模組可依硬體能力選擇最佳路徑（例如 Attention 自動使用 FlashAttention、
GEMM 自動走 cuBLASLt、自研 kernel 走 Triton）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import torch

log = logging.getLogger(__name__)


# ── 後端能力偵測 ────────────────────────────────────────────────
def _detect_cuda() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False}
    if not torch.cuda.is_available():
        return info
    info["available"] = True
    try:
        info["device_count"] = torch.cuda.device_count()
        info["device_name"] = torch.cuda.get_device_name(0)
        info["capability"] = torch.cuda.get_device_capability(0)
        major, minor = info["capability"]
        # Tensor Core 起算：sm_70+ (V100)；cuBLASLt / FlashAttention 需要 sm_80+
        info["has_tensor_core"] = major >= 7
        info["flash_attention_capable"] = major >= 8
        props = torch.cuda.get_device_properties(0)
        info["total_memory_gb"] = props.total_memory / (1024 ** 3)
    except Exception as exc:  # pragma: no cover - 防禦性
        log.warning("CUDA 偵測失敗: %s", exc)
    return info


def _detect_mps() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False}
    if not hasattr(torch.backends, "mps") or not torch.backends.mps.is_available():
        return info
    info["available"] = True
    return info


def _detect_triton() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False}
    try:
        import triton  # noqa: F401
        import triton.language as tl  # noqa: F401
        info["available"] = True
        info["version"] = getattr(triton, "__version__", "unknown")
    except Exception:
        pass
    return info


def _detect_flash_attention() -> dict[str, Any]:
    """偵測 PyTorch 內建 SDPA 是否能調度 FlashAttention / mem-efficient kernel。"""
    info: dict[str, Any] = {"available": False, "via_sdpa": False}
    try:
        # SDPA 是 FlashAttention 的官方入口（PyTorch 2.0+）
        _ = torch.nn.functional.scaled_dot_product_attention
        info["via_sdpa"] = True
        info["available"] = True
        if torch.cuda.is_available():
            try:
                from torch.nn.attention import SDPBackend  # noqa: F401
                info["flash_backend"] = True
            except Exception:
                info["flash_backend"] = False
    except Exception:
        pass
    return info


@dataclass(frozen=True)
class BackendCapabilities:
    """可用後端能力快照。"""

    cuda: dict[str, Any]
    mps: dict[str, Any]
    triton: dict[str, Any]
    flash_attention: dict[str, Any]
    cpu_threads: int
    torch_version: str

    @property
    def has_cuda(self) -> bool:
        return bool(self.cuda.get("available"))

    @property
    def has_mps(self) -> bool:
        return bool(self.mps.get("available"))

    @property
    def has_triton(self) -> bool:
        return bool(self.triton.get("available"))

    @property
    def has_flash_attention(self) -> bool:
        return bool(self.flash_attention.get("available"))

    @property
    def has_tensor_core(self) -> bool:
        return bool(self.cuda.get("has_tensor_core", False))

    def preferred_device(self) -> torch.device:
        if self.has_cuda:
            return torch.device("cuda")
        if self.has_mps:
            return torch.device("mps")
        return torch.device("cpu")

    def summary(self) -> dict[str, Any]:
        return {
            "torch_version": self.torch_version,
            "cuda": self.cuda,
            "mps": self.mps,
            "triton": self.triton,
            "flash_attention": self.flash_attention,
            "cpu_threads": self.cpu_threads,
            "preferred_device": str(self.preferred_device()),
        }


_capabilities: BackendCapabilities | None = None


def capabilities() -> BackendCapabilities:
    """取得並快取後端能力。"""
    global _capabilities
    if _capabilities is None:
        _capabilities = BackendCapabilities(
            cuda=_detect_cuda(),
            mps=_detect_mps(),
            triton=_detect_triton(),
            flash_attention=_detect_flash_attention(),
            cpu_threads=max(1, os.cpu_count() or 1),
            torch_version=torch.__version__,
        )
        log.info("星澄後端能力: %s", _capabilities.summary())
    return _capabilities


def reset_capabilities() -> None:
    """測試用：清除能力快照。"""
    global _capabilities
    _capabilities = None


# ── 裝置選擇工具 ────────────────────────────────────────────────
def resolve_device(device: str | torch.device | None) -> torch.device:
    """將 None / "auto" 解析為實際裝置。"""
    if device is None or str(device) == "auto":
        return capabilities().preferred_device()
    return torch.device(device)


def default_dtype(device: torch.device | None = None) -> torch.dtype:
    """依裝置選擇預設 dtype：CUDA / MPS 用 bfloat16，CPU 用 float32。"""
    dev = resolve_device(device)
    if dev.type in ("cuda", "mps"):
        return torch.bfloat16
    return torch.float32


def enable_flash_attention(enabled: bool = True) -> None:
    """全域啟用 / 停用 SDPA 內部 FlashAttention 調度。"""
    try:
        torch.backends.cuda.enable_flash_sdp(enabled)
        torch.backends.cuda.enable_mem_efficient_sdp(enabled)
        torch.backends.cuda.enable_math_sdp(True)
    except Exception:
        pass


def set_gemm_backend(device: torch.device) -> str:
    """提示 GEMM 後端選擇；PyTorch 會依裝置自動調度 cuBLASLt / oneDNN / BLAS。"""
    cap = capabilities()
    if device.type == "cuda" and cap.has_cuda:
        # cuBLASLt 在 sm_70+ 與 Tensor Core 路徑上為首選
        try:
            torch.backends.cuda.matmul.allow_tf32 = cap.has_tensor_core
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass
        return "cublaslt"
    if device.type == "cpu":
        # oneDNN 為 PyTorch CPU 預設數學後端
        return "onednn"
    if device.type == "mps":
        return "mps"
    return "aten"


__all__ = [
    "BackendCapabilities",
    "capabilities",
    "reset_capabilities",
    "resolve_device",
    "default_dtype",
    "enable_flash_attention",
    "set_gemm_backend",
]
