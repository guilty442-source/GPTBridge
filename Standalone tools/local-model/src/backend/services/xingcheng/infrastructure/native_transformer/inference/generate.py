"""Generator：自回歸生成迴圈（prefill + decode）。

支援：
  - KV Cache 預填（prefill）後逐 token 解碼
  - greedy / sampling
  - early stop on EOS
  - max_new_tokens 限制
"""

from __future__ import annotations

import torch

from ..config import XingChengConfig
from ..execution.backend import resolve_device, default_dtype
from ..modules.model import XingChengForCausalLM
from .kv_cache import KVCache
from .sampler import Sampler, SamplingConfig


class Generator:
    """星澄文字生成器。"""

    def __init__(
        self,
        model: XingChengForCausalLM,
        sampler: Sampler | None = None,
        device: torch.device | None = None,
    ) -> None:
        self.model = model
        self.config: XingChengConfig = model.config
        self.sampler = sampler or Sampler(SamplingConfig())
        self.device = resolve_device(device)

    @torch.no_grad()
    def generate(
        self,
        input_ids: torch.Tensor,
        *,
        attention_mask: torch.Tensor | None = None,
        max_new_tokens: int | None = None,
        sampling: SamplingConfig | None = None,
        use_cache: bool = True,
    ) -> torch.Tensor:
        cfg = self.config
        max_new = max_new_tokens or cfg.max_new_tokens
        sampler = Sampler(sampling) if sampling is not None else self.sampler
        input_ids = input_ids.to(self.device)
        b, prefix_len = input_ids.shape

        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids)

        # ── Prefill ─────────────────────────────────────────────
        out = self.model(
            input_ids,
            attention_mask=attention_mask,
            use_cache=use_cache,
        )
        logits = out["logits"][:, -1, :]
        next_token = sampler.sample(logits, prev_tokens=input_ids)
        generated = [next_token]

        kv_caches = out.get("kv_caches")
        position = prefix_len

        # ── Decode ──────────────────────────────────────────────
        for _ in range(max_new - 1):
            cur = next_token.unsqueeze(-1)
            cur_mask = torch.ones_like(cur)
            step_out = self.model(
                cur,
                attention_mask=cur_mask,
                kv_caches=kv_caches if use_cache else None,
                use_cache=use_cache,
            )
            step_logits = step_out["logits"][:, -1, :]
            next_token = sampler.sample(step_logits, prev_tokens=input_ids)
            generated.append(next_token)
            position += 1

            # early stop
            if (next_token == sampler.config.eos_token_id).all():
                break

        return torch.stack(generated, dim=-1)


__all__ = ["Generator"]
