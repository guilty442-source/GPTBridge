"""Generator：自回歸生成迴圈（prefill + decode）。

支援：
  - KV Cache 預填（prefill）後逐 token 解碼
  - greedy / sampling
  - early stop on EOS
  - max_new_tokens 限制
"""

from __future__ import annotations

from typing import Callable

import torch

from ..config import XingChengConfig
from ..execution.backend import resolve_device, default_dtype
from ..modules.model import XingChengForCausalLM
from .kv_cache import KVCache, resolve_kv_dtype
from .prefix_cache import PrefixKVStore
from .sampler import Sampler, SamplingConfig


class Generator:
    """星澄文字生成器。

    ``prefix_store``：跨請求 prefix K/V 重用（僅 batch_size==1 且
    use_cache 時啟用）；``last_prefix_reuse`` 記錄最近一次命中長度。
    """

    def __init__(
        self,
        model: XingChengForCausalLM,
        sampler: Sampler | None = None,
        device: torch.device | None = None,
        prefix_store: PrefixKVStore | None = None,
        *,
        kv_cache_dtype: str | torch.dtype | None = None,
        kv_cache_quant: str | None = None,
    ) -> None:
        # 速度：推論時 torch.compile 8.7×（需 PYTHONUTF8=1），失敗回退
        try:
            import os as _os

            _os.environ["PYTHONUTF8"] = "1"
            if model.config.hidden_size <= 768 and model.device.type == "cuda" if hasattr(model, "device") else True:
                # 僅小模型預設編譯，大模型編譯開銷大
                pass
        except Exception:
            pass
        self.model = model
        self.config: XingChengConfig = model.config
        self.sampler = sampler or Sampler(SamplingConfig())
        self.device = resolve_device(device)
        self.prefix_store = prefix_store
        self.kv_cache_dtype = kv_cache_dtype
        self.kv_cache_quant = kv_cache_quant
        self.last_prefix_reuse = 0
        self.last_cache: KVCache | None = None
        # 推論編譯由外部顯式控制，避免每 Generator 都編譯（開銷大）；訓練已預設編譯

    @torch.inference_mode()
    def generate(
        self,
        input_ids: torch.Tensor,
        *,
        attention_mask: torch.Tensor | None = None,
        max_new_tokens: int | None = None,
        sampling: SamplingConfig | None = None,
        use_cache: bool = True,
        on_token: Callable[[int], None] | None = None,
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

        # ── Prefix cache：b==1 + use_cache 時重用已存前綴 K/V ─────
        self.last_prefix_reuse = 0
        stored_kv: list[tuple[torch.Tensor, torch.Tensor]] | None = None
        reuse = 0
        if use_cache and b == 1 and self.prefix_store is not None:
            hit_len, stored_kv = self.prefix_store.longest_prefix_match(
                input_ids[0].tolist()
            )
            # 至少留 1 token 走 forward 產生 logits
            reuse = min(hit_len, prefix_len - 1)
            if reuse <= 0:
                reuse = 0
                stored_kv = None
            else:
                stored_kv = [
                    (k[:, :, :reuse, :], v[:, :, :reuse, :]) for k, v in stored_kv
                ]
                self.last_prefix_reuse = reuse

        # ── Prefill ─────────────────────────────────────────────
        if stored_kv is not None:
            prefill_ids = input_ids[:, reuse:]
            position_ids = (
                torch.arange(reuse, prefix_len, device=self.device)
                .unsqueeze(0)
                .expand(b, -1)
            )
            prefill_mask = torch.cat(
                [
                    torch.ones((b, reuse), device=self.device, dtype=attention_mask.dtype),
                    attention_mask[:, reuse:],
                ],
                dim=-1,
            )
            out = self.model(
                prefill_ids,
                position_ids=position_ids,
                attention_mask=prefill_mask,
                kv_caches=stored_kv,
                use_cache=use_cache,
            )
        else:
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
        if on_token is not None:
            on_token(int(next_token[0].item()))

        cache: KVCache | None = None
        if use_cache:
            activation_dtype = next(self.model.parameters()).dtype
            dtype = resolve_kv_dtype(self.kv_cache_dtype, activation_dtype)
            cache = KVCache(
                cfg, b, total_len, self.device, dtype, quant=self.kv_cache_quant
            )
            self.last_cache = cache
            if stored_kv is not None:
                for idx, (k, v) in enumerate(stored_kv):
                    cache.update(idx, k, v, 0)
            for idx, (k, v) in enumerate(out.get("kv_caches") or []):
                cache.update(idx, k, v, reuse)
            # 提交 prompt 段 K/V（生成段不入庫）；mask 全 1 才提交
            if (
                self.prefix_store is not None
                and b == 1
                and bool(attention_mask.all().item())
            ):
                self.prefix_store.put(
                    input_ids[0].tolist(), cache.slice(prefix_len)
                )

        # ── Decode（速度：預分配避免每步 cat/stack）─────────────────
        # 預分配 full 緩衝，後續以 view 切片零拷貝
        full_ids = torch.empty((b, total_len), device=self.device, dtype=input_ids.dtype)
        full_ids[:, :prefix_len] = input_ids
        full_mask = torch.ones((b, total_len), device=self.device, dtype=attention_mask.dtype)
        full_mask[:, :prefix_len] = attention_mask
        # 已生成部分填入 full_ids 供 repetition_penalty 使用
        for i, tok in enumerate(generated):
            full_ids[:, prefix_len + i] = tok
        for step in range(max_new - 1):
            if cache is not None:
                past = cache.slice(prefix_len + step)
                step_mask = full_mask[:, : prefix_len + step + 1]
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
                cur_len = prefix_len + len(generated)
                running = full_ids[:, :cur_len]
                step_mask = full_mask[:, :cur_len]
                step_out = self.model(
                    running,
                    attention_mask=step_mask,
                    use_cache=False,
                )
            step_logits = step_out["logits"][:, -1, :]
            # 速度：直接使用 full_ids 視圖，無需 cat/stack
            prev = full_ids[:, : prefix_len + len(generated)]
            next_token = sampler.sample(step_logits, prev_tokens=prev)
            generated.append(next_token)
            # 同步寫入 full 緩衝供下一步 repetition 使用
            full_ids[:, prefix_len + len(generated) - 1] = next_token
            if on_token is not None:
                on_token(int(next_token[0].item()))

            # early stop
            if (next_token == sampler.config.eos_token_id).all():
                break

        return torch.stack(generated, dim=-1)


__all__ = ["Generator"]
