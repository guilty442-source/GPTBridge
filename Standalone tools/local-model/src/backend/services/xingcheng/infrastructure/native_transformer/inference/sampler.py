"""Sampler：greedy / temperature / top-k / top-p / argmax。

對應技術棧中的 Sampling 自研 kernel（Triton 路徑留待瓶頸下沉）。
第一版以 PyTorch 張量運算實作，支援重複懲罰。
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.nn.functional as F


@dataclass
class SamplingConfig:
    do_sample: bool = False
    temperature: float = 1.0
    top_k: int = 0
    top_p: float = 1.0
    repetition_penalty: float = 1.0
    eos_token_id: int = 2
    pad_token_id: int = 0


class Sampler:
    """從 logits 取樣下一個 token。"""

    def __init__(self, config: SamplingConfig) -> None:
        self.config = config

    @torch.no_grad()
    def sample(self, logits: torch.Tensor, *, prev_tokens: torch.Tensor | None = None) -> torch.Tensor:
        """logits: (B, vocab) → next_tokens: (B,)。"""
        cfg = self.config
        logits = logits.clone()

        if cfg.repetition_penalty != 1.0 and prev_tokens is not None:
            logits = _apply_repetition_penalty(logits, prev_tokens, cfg.repetition_penalty)

        if not cfg.do_sample or cfg.temperature <= 0:
            return torch.argmax(logits, dim=-1)

        if cfg.temperature != 1.0:
            logits = logits / cfg.temperature

        if cfg.top_k > 0:
            logits = _top_k_filter(logits, cfg.top_k)
        if cfg.top_p < 1.0:
            logits = _top_p_filter(logits, cfg.top_p)

        probs = torch.softmax(logits, dim=-1)
        return torch.multinomial(probs, num_samples=1).squeeze(-1)


def _top_k_filter(logits: torch.Tensor, k: int) -> torch.Tensor:
    # logits: (B, V)
    k = min(k, logits.size(-1))
    topk_vals, _ = torch.topk(logits, k, dim=-1)        # (B, k)
    threshold = topk_vals[:, -1:]                        # (B, 1)
    mask = logits < threshold                            # (B, V)
    return logits.masked_fill(mask, float("-inf"))


def _top_p_filter(logits: torch.Tensor, p: float) -> torch.Tensor:
    sorted_logits, sorted_idx = torch.sort(logits, descending=True, dim=-1)  # (B, V)
    cum_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)        # (B, V)
    remove = cum_probs > p
    remove[..., 1:] = remove[..., :-1].clone()
    remove[..., 0] = False
    sorted_logits = sorted_logits.masked_fill(remove, float("-inf"))
    # scatter 回原順序
    return torch.gather(sorted_logits, -1, sorted_idx.argsort(-1))


def _apply_repetition_penalty(
    logits: torch.Tensor, prev_tokens: torch.Tensor, penalty: float
) -> torch.Tensor:
    for b in range(logits.size(0)):
        for tok in prev_tokens[b].tolist():
            if logits[b, tok] > 0:
                logits[b, tok] /= penalty
            else:
                logits[b, tok] *= penalty
    return logits


__all__ = ["Sampler", "SamplingConfig"]
