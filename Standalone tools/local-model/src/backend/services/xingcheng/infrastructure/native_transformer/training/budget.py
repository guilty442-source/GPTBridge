"""§2.7-8 訓練中資源超支即停：executor 內嵌預算計時的步邊界檢查。

三個訓練器（pretrain／sft／dpo）在每一步完成後呼叫
:func:`check_train_budget`；超限即跳出迴圈——final checkpoint 照常落地、
summary 記錄 ``stopped_reason``，由受管層（executor／self-learning）
決定後續（預設 fail-closed：不評估、不啟用）。預算 0＝不設限，
與既有 bounded config key 慣例一致。
"""

from __future__ import annotations

import time
from typing import Any

import torch

STOP_TIME_BUDGET = "time-budget-exceeded"
STOP_VRAM_BUDGET = "vram-budget-exceeded"
STOP_GPU_TIME_BUDGET = "gpu-time-budget-exceeded"


def check_train_budget(
    config: Any,
    device: torch.device,
    started: float,
    *,
    gpu_seconds: float | None = None,
) -> str | None:
    """回傳停止原因字串；未超限回傳 ``None``。

    - ``max_train_seconds``：wall-clock 訓練預算（秒），量測自 ``started``。
    - ``max_train_vram_mb``：本程序 CUDA 配置量上限（MiB）；CPU 裝置不檢查。
    - ``max_train_gpu_seconds``：§2.7-4 每循環 GPU 時間預算——訓練器逐步
      累計 GPU 活躍秒數（僅 CUDA 裝置計入）傳入 ``gpu_seconds``；
      未提供或 CPU 訓練時不檢查。
    """
    budget_s = float(getattr(config, "max_train_seconds", 0) or 0)
    if budget_s > 0 and (time.time() - started) > budget_s:
        return STOP_TIME_BUDGET
    gpu_budget_s = float(getattr(config, "max_train_gpu_seconds", 0) or 0)
    if gpu_budget_s > 0 and gpu_seconds is not None and gpu_seconds > gpu_budget_s:
        return STOP_GPU_TIME_BUDGET
    vram_mb = int(getattr(config, "max_train_vram_mb", 0) or 0)
    if vram_mb > 0 and device.type == "cuda":
        allocated_mb = torch.cuda.memory_allocated(device) / (1024 * 1024)
        if allocated_mb > vram_mb:
            return STOP_VRAM_BUDGET
    return None


__all__ = [
    "STOP_GPU_TIME_BUDGET",
    "STOP_TIME_BUDGET",
    "STOP_VRAM_BUDGET",
    "check_train_budget",
]
