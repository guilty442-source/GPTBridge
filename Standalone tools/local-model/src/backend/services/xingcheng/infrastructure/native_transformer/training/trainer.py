"""Trainer：訓練迴圈（Autograd 前向 → backward → optimizer step）。

對應技術棧：
  Computation Graph 管理 → Autograd → Backward Graph → Gradient → Optimizer
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

import torch
from torch.utils.data import DataLoader

from ..config import XingChengConfig
from ..execution.backend import resolve_device, default_dtype
from ..modules.model import XingChengForCausalLM
from .data import collate_batch
from .optimizer import build_optimizer

log = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    lr: float = 3e-4
    weight_decay: float = 0.01
    batch_size: int = 4
    epochs: int = 1
    max_steps: int | None = None
    grad_clip: float = 1.0
    warmup_steps: int = 0
    log_every: int = 10
    optimizer: str = "adamw"
    device: str | torch.device | None = None
    dtype: torch.dtype | None = None
    use_amp: bool = True
    seed: int = 42


class Trainer:
    """星澄輕量訓練器。"""

    def __init__(
        self,
        model: XingChengForCausalLM,
        config: TrainingConfig,
        train_config: XingChengConfig,
    ) -> None:
        self.model = model
        self.tcfg = config
        self.train_config = train_config
        self.device = resolve_device(config.device)
        self.dtype = config.dtype or default_dtype(self.device)
        self.use_amp = bool(
            config.use_amp and self.device.type in {"cuda", "mps"}
        )
        model.to(self.device)
        self.optimizer = build_optimizer(
            model.parameters(), train_config,
            lr=config.lr, weight_decay=config.weight_decay, kind=config.optimizer,
        )
        self.scaler = self._make_scaler()
        self._step = 0
        torch.manual_seed(config.seed)

    def _make_scaler(self):
        if not self.use_amp or self.dtype != torch.float16:
            return None
        try:
            return torch.amp.GradScaler(self.device.type)
        except (AttributeError, TypeError):  # pragma: no cover - 舊版 torch
            return torch.cuda.amp.GradScaler()

    def _autocast(self):
        if not self.use_amp:
            return torch.autocast(device_type=self.device.type, enabled=False)
        return torch.autocast(device_type=self.device.type, dtype=self.dtype)

    def _lr_scale(self) -> float:
        if self.tcfg.warmup_steps <= 0:
            return 1.0
        return min(1.0, self._step / self.tcfg.warmup_steps)

    def train_step(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> float:
        input_ids = input_ids.to(self.device, non_blocking=self.device.type == "cuda")
        attention_mask = attention_mask.to(
            self.device, non_blocking=self.device.type == "cuda"
        )
        labels = input_ids.clone()
        labels[attention_mask == 0] = self.train_config.pad_token_id

        with self._autocast():
            out = self.model(input_ids, attention_mask=attention_mask, labels=labels)
            loss = out["loss"]
        if self.scaler is not None:
            self.scaler.scale(loss).backward()
            if self.tcfg.grad_clip > 0:
                self.scaler.unscale_(self.optimizer)
        else:
            loss.backward()
        if self.tcfg.grad_clip > 0:
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.tcfg.grad_clip)

        # warmup lr scale
        scale = self._lr_scale()
        for pg in self.optimizer.param_groups:
            pg["lr"] = self.tcfg.lr * scale
        if self.scaler is not None:
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            self.optimizer.step()
        self.optimizer.zero_grad(set_to_none=True)
        self._step += 1
        return float(loss.detach().item())

    def fit(self, dataloader: DataLoader) -> dict[str, Any]:
        self.model.train()
        history: list[float] = []
        max_steps = self.tcfg.max_steps
        for epoch in range(self.tcfg.epochs):
            for batch in dataloader:
                input_ids, attn = batch
                loss = self.train_step(input_ids, attn)
                history.append(loss)
                if self.tcfg.log_every > 0 and (self._step % self.tcfg.log_every == 0):
                    log.info("step %d loss=%.4f", self._step, loss)
                if max_steps is not None and self._step >= max_steps:
                    return {"losses": history, "steps": self._step, "final_loss": history[-1]}
        return {
            "losses": history,
            "steps": self._step,
            "final_loss": history[-1] if history else float("nan"),
        }


def make_dataloader(
    dataset,
    batch_size: int,
    pad_id: int,
    *,
    shuffle: bool = True,
    num_workers: int = 0,
    pin_memory: bool | None = None,
    prefetch_factor: int = 2,
    persistent_workers: bool = True,
) -> DataLoader:
    workers = max(0, int(num_workers))
    kwargs: dict[str, Any] = {
        "batch_size": batch_size,
        "shuffle": shuffle,
        "num_workers": workers,
        "collate_fn": partial(collate_batch, pad_id=pad_id),
        "pin_memory": torch.cuda.is_available() if pin_memory is None else pin_memory,
    }
    if workers > 0:
        kwargs["prefetch_factor"] = max(1, int(prefetch_factor))
        kwargs["persistent_workers"] = bool(persistent_workers)
    return DataLoader(dataset, **kwargs)


__all__ = ["Trainer", "TrainingConfig", "make_dataloader"]
