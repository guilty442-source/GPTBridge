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
        total_len = prefix_len + max_new
        if total_len > cfg.max_position_embeddings:
            raise ValueError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS")
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids)
        attention_mask = attention_mask.to(self.device)

        # ── Prefill ─────────────────────────────────────────────
        position_ids = (
            torch.arange(prefix_len, device=self.device).unsqueeze(0).expand(b, -1)
        )
        out = self.model(
            input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            use_cache=use_cache,
        )
        logits = out["logits"][:, -1, :]
        next_token = sampler.sample(logits, prev_tokens=input_ids)
        generated = [next_token]

        cache: KVCache | None = None
        if use_cache:
            dtype = next(self.model.parameters()).dtype
            cache = KVCache(cfg, b, total_len, self.device, dtype)
            for idx, (k, v) in enumerate(out.get("kv_caches") or []):
                cache.update(idx, k, v, 0)

        # ── Decode ──────────────────────────────────────────────
        for step in range(max_new - 1):
            if cache is not None:
                past = cache.slice(prefix_len + step)
                step_mask = torch.cat(
                    [attention_mask, torch.ones((b, step + 1), device=self.device)],
                    dim=-1,
                )
                step_position_ids = torch.full(
                    (b, 1), prefix_len + step, device=self.device, dtype=torch.long
                )
                step_out = self.model(
                    next_token.unsqueeze(-1),
                    position_ids=step_position_ids,
                    attention_mask=step_mask,
                    kv_caches=past,
                    use_cache=True,
                )
                for idx, (k, v) in enumerate(step_out["kv_caches"]):
                    cache.update(idx, k, v, prefix_len + step)
            else:
                running = torch.cat(
                    [input_ids, torch.stack(generated, dim=-1)], dim=-1
                )
                step_mask = torch.cat(
                    [
                        attention_mask,
                        torch.ones(
                            (b, running.size(1) - prefix_len), device=self.device
                        ),
                    ],
                    dim=-1,
                )
                step_out = self.model(
                    running,
                    attention_mask=step_mask,
                    use_cache=False,
                )
            step_logits = step_out["logits"][:, -1, :]
            prev = torch.cat([input_ids, torch.stack(generated, dim=-1)], dim=-1)
            next_token = sampler.sample(step_logits, prev_tokens=prev)
            generated.append(next_token)

            # early stop
            if (next_token == sampler.config.eos_token_id).all():
                break

        return torch.stack(generated, dim=-1)


__all__ = ["Generator"]
