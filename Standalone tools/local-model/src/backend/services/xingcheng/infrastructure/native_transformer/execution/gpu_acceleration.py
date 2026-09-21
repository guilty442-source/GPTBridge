"""GPU 加速策略中心：訓練與推論的統一加速決策。

設計原則（呼應「訓練 Python+PyTorch 主線，推論 Python/C++ 雙路徑」）：
- 訓練：Python+PyTorch 主線，透過 ATen/cuBLASLt/cuDNN/FlashAttention/Triton 加速，不下沉至自研 CUDA C++
- 推論：Python 路徑（PyTorch/Triton）為預設，C++ 路徑（native/ pybind11）為高吞吐選項，共用權重
- 正確性優先：加速不改變語意（logits 差異 <1e-3），fail-closed 回退

對應硬體：RTX 3050 6GB SM 8.6 Tensor Core（實測 has_tensor_core=True flash=True）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import torch

from .backend import capabilities, describe_sdpa_backends, set_gemm_backend


@dataclass(frozen=True)
class GpuAccelerationPlan:
    """單次 run 的加速決議（訓練/推論共用）。"""

    device: torch.device
    use_bf16: bool
    use_flash: bool
    use_triton: bool
    use_fused_adamw: bool
    use_torch_compile: bool
    gemm_backend: str  # cublaslt / onednn / mps / aten
    expected_sdpa: str  # flash_attention / mem_efficient / math

    def summary(self) -> dict[str, Any]:
        return {
            "device": str(self.device),
            "use_bf16": self.use_bf16,
            "use_flash": self.use_flash,
            "use_triton": self.use_triton,
            "use_fused_adamw": self.use_fused_adamw,
            "use_torch_compile": self.use_torch_compile,
            "gemm_backend": self.gemm_backend,
            "expected_sdpa": self.expected_sdpa,
        }


def plan_for_training(device: torch.device | str | None = None, use_compile: bool = False) -> GpuAccelerationPlan:
    """訓練加速決議：BF16 + fused + Flash + Triton（若可用）。"""
    from .backend import resolve_device

    dev = resolve_device(device)
    cap = capabilities()
    sdpa = describe_sdpa_backends()
    # 精度：CUDA 優先 BF16（無需 scaler），否則 FP32
    use_bf16 = dev.type == "cuda" and cap.has_cuda
    # Flash：僅 CUDA 且 flash_active
    use_flash = dev.type == "cuda" and bool(sdpa.get("flash_active"))
    # Triton：僅 CUDA 且已安裝
    use_triton = cap.has_triton and cap.has_cuda
    # fused AdamW：僅 CUDA 且 PyTorch 2.13+ 支援
    use_fused = dev.type == "cuda"
    # GEMM 後端
    gemm = set_gemm_backend(dev)
    expected = sdpa.get("expected_backend", "math")
    return GpuAccelerationPlan(dev, use_bf16, use_flash, use_triton, use_fused, use_compile, gemm, expected)


def plan_for_inference(device: torch.device | str | None = None, prefer_cpp: bool = False) -> GpuAccelerationPlan:
    """推論加速決議：Python 路徑同訓練，C++ 路徑額外標記（native/）。"""
    p = plan_for_training(device, use_compile=False)
    # 推論不使用 fused（無 optimizer），但 C++ 路徑需額外標記
    # 此處僅記錄偏好，實際調度由 Native Dispatch 依裝置與權重選擇
    if prefer_cpp and p.device.type == "cuda":
        # C++ 路徑：仍用相同 GEMM/Flash，但 kernel 改走 native/ 的 CUDA C++ 實作
        return GpuAccelerationPlan(p.device, p.use_bf16, p.use_flash, False, False, False, p.gemm_backend, p.expected_sdpa)
    return GpuAccelerationPlan(p.device, p.use_bf16, p.use_flash, p.use_triton, False, False, p.gemm_backend, p.expected_sdpa)


def verify_correctness(baseline_logits: torch.Tensor, accelerated_logits: torch.Tensor, tol: float = 1e-3) -> bool:
    """正確性：加速前後 logits 差異 < tol 視為語意等價。"""
    if baseline_logits.shape != accelerated_logits.shape:
        return False
    diff = (baseline_logits - accelerated_logits).abs().max().item()
    return diff < tol


__all__ = ["GpuAccelerationPlan", "plan_for_training", "plan_for_inference", "verify_correctness"]
