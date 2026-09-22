"""星澄執行後端與裝置管理。

對應技術棧：
  ATen / Dispatcher / Torch C++ Backend
    → 高效能數學與 Kernel (BLAS / oneDNN / cuBLASLt / cuDNN / FlashAttention)
    → Triton（PyTorch 生態內自研 kernel 選項）
    → CPU / NVIDIA GPU / (Apple MPS)

  （Gluon / CUDA C++ / PTX / SASS 屬後續自研推論引擎範疇，
   不在本模型範圍 — 主線裁決 2026-09-19）

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
                from torch.nn.attention import SDPBackend

                info["flash_backend"] = True
                # kernel 是否真實編譯存在（Windows wheel 常缺 flash kernel）
                info["flash_kernel"] = _probe_sdpa_kernel(SDPBackend.FLASH_ATTENTION)
            except Exception:
                info["flash_backend"] = False
                info["flash_kernel"] = False
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


# ── CPU 執行緒策略（R8：訓練／推論統一入口）─────────────────────
def cpu_thread_budget(
    role: str = "inference",
    *,
    configured: int = 0,
    cores: int | None = None,
) -> int:
    """依角色回傳 CPU 執行緒預算（不修改全域狀態）。

    - ``inference``：預設 min(4, cores//4)——常駐服務保守讓路。
    - ``training``：預設 min(8, cores//2)——批次工作可用較多核心。
    ``configured`` > 0 時為顯式覆寫（上限 16）。
    """
    n = int(cores or os.cpu_count() or 8)
    if int(configured) > 0:
        return max(1, min(16, int(configured)))
    if str(role) == "training":
        return max(1, min(8, n // 2))
    return max(1, min(4, n // 4))


def apply_cpu_thread_budget(
    role: str = "inference",
    *,
    configured: int = 0,
) -> int:
    """套用 CPU 執行緒預算（set_num_threads + interop=1）；回傳實際預算。

    非 CPU 裝置仍應呼叫——PyTorch 的 host 端算子（dataloader、
    tokenize、dispatch 前處理）都吃 CPU 執行緒。
    """
    budget = cpu_thread_budget(role, configured=configured)
    torch.set_num_threads(budget)
    try:
        torch.set_num_interop_threads(1)
    except Exception:  # interop 只能設定一次；重複設定忽略
        pass
    return budget


def enable_flash_attention(enabled: bool = True) -> None:
    """全域啟用 / 停用 SDPA 內部 FlashAttention 調度。"""
    try:
        torch.backends.cuda.enable_flash_sdp(enabled)
        torch.backends.cuda.enable_mem_efficient_sdp(enabled)
        torch.backends.cuda.enable_math_sdp(True)
    except Exception:
        pass


_TRITON_KERNELS_ENABLED = str(
    os.environ.get("XINGCHENG_TRITON_KERNELS", "1")
).strip().casefold() not in {"0", "false", "no", "off"}


def triton_kernels_enabled() -> bool:
    """自研 Triton kernel 是否啟用。

    RMSNorm／SwiGLU／RoPE 已通過 CUDA 上逐運算元與模型層級 parity；
    需要退回 PyTorch 時可設 ``XINGCHENG_TRITON_KERNELS=0`` 或呼叫
    ``set_triton_kernels(False)``。需要梯度的訓練路徑仍自動回退 PyTorch。
    """
    return _TRITON_KERNELS_ENABLED


def set_triton_kernels(enabled: bool) -> None:
    global _TRITON_KERNELS_ENABLED
    _TRITON_KERNELS_ENABLED = bool(enabled)


def _probe_sdpa_kernel(backend_enum: Any) -> bool:
    """強制單一後端執行小型 SDPA，確認 kernel 是否真正可用。

    flag 開啟不代表 kernel 已編譯（例如 Windows 官方 wheel 未含
    FlashAttention）；此探針以真實執行判定，失敗一律回 ``False``。
    """
    try:
        import warnings

        from torch.nn.attention import sdpa_kernel

        q = torch.zeros(1, 2, 16, 32, device="cuda", dtype=torch.bfloat16)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with sdpa_kernel(backend_enum):
                torch.nn.functional.scaled_dot_product_attention(q, q, q, is_causal=True)
        torch.cuda.synchronize()
        return True
    except Exception:
        return False


def _probe_dispatched_sdpa_backend() -> str:
    """以 profiler 觀察一次代表性 SDPA 呼叫，回報實際派發的 kernel。

    稽核要求「透過實際執行紀錄確認所選後端」——dispatcher 依
    dtype / shape / mask 逐次決定，此處以常見推論形狀（bf16, causal）
    取樣；結果為該形狀下的真實 kernel，非理論上界。
    """
    try:
        from torch.profiler import ProfilerActivity, profile

        q = torch.zeros(1, 2, 16, 32, device="cuda", dtype=torch.bfloat16)
        with profile(activities=[ProfilerActivity.CUDA]) as prof:
            torch.nn.functional.scaled_dot_product_attention(q, q, q, is_causal=True)
            torch.cuda.synchronize()
        names = " ".join(
            e.key
            for e in prof.key_averages()
            if (getattr(e, "self_device_time_total", 0) or 0) > 0
        ).lower()
        if "flash" in names:
            return "flash_attention"
        if "cudnn" in names:
            return "cudnn_attention"
        if "fmha" in names or "efficient_attention" in names:
            return "mem_efficient"
        if "softmax" in names or "gemm" in names:
            return "math"
        return "unknown"
    except Exception:
        return "unknown"


_sdpa_actual_cache: dict[str, Any] | None = None


def describe_sdpa_backends() -> dict[str, Any]:
    """回報 SDPA 各後端的真實可用性與本機實際派發結果。

    稽核要求「不得以 API 呼叫成功視為 FlashAttention 已啟用」——
    旗標只代表後端未被停用，不代表 kernel 已編譯或被派發。
    本函式以強制探針判定各 kernel 是否存在，再以 profiler 觀察
    代表性呼叫的真實派發；``flash_active`` 僅在 profiler 實際
    觀測到 flash kernel 時為真。
    """
    global _sdpa_actual_cache
    if _sdpa_actual_cache is not None:
        return _sdpa_actual_cache
    cuda = torch.cuda.is_available()
    flags: dict[str, bool] = {}
    for name, getter in (
        ("flash_sdp", lambda: torch.backends.cuda.flash_sdp_enabled()),
        ("mem_efficient_sdp", lambda: torch.backends.cuda.mem_efficient_sdp_enabled()),
        ("math_sdp", lambda: torch.backends.cuda.math_sdp_enabled()),
        ("cudnn_sdp", lambda: getattr(torch.backends.cuda, "cudnn_sdp_enabled", lambda: False)()),
    ):
        try:
            flags[name] = bool(getter())
        except Exception:
            flags[name] = False
    if not cuda:
        expected = "math" if flags.get("math_sdp", True) else "unknown"
        _sdpa_actual_cache = {
            "device": "cpu",
            "flags": flags,
            "kernel_compiled": {},
            "expected_backend": expected,
            "flash_active": False,
            "note": "CPU 無 FlashAttention；SDPA 走 math / oneDNN 路徑",
        }
        return _sdpa_actual_cache
    kernel_compiled: dict[str, bool] = {}
    try:
        from torch.nn.attention import SDPBackend

        kernel_compiled = {
            "flash_attention": _probe_sdpa_kernel(SDPBackend.FLASH_ATTENTION),
            "cudnn_attention": _probe_sdpa_kernel(SDPBackend.CUDNN_ATTENTION),
            "mem_efficient": _probe_sdpa_kernel(SDPBackend.EFFICIENT_ATTENTION),
        }
    except Exception:
        kernel_compiled = {}
    expected = _probe_dispatched_sdpa_backend()
    _sdpa_actual_cache = {
        "device": "cuda",
        "flags": flags,
        "kernel_compiled": kernel_compiled,
        "expected_backend": expected,
        "flash_active": expected == "flash_attention",
        "note": "expected_backend 為 profiler 實測派發結果；旗標僅代表後端未被停用",
    }
    return _sdpa_actual_cache


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
    "cpu_thread_budget",
    "apply_cpu_thread_budget",
    "describe_sdpa_backends",
    "enable_flash_attention",
    "set_gemm_backend",
]
