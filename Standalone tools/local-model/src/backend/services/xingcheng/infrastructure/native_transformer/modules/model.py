"""星澄 Transformer 模型本體 + LM Head。

XingChengModel：純 backbone（embedding + N 層 block + final norm）。
XingChengForCausalLM：包裝 backbone + LM Head + forward 回傳 logits / loss。
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..config import XingChengConfig
from ..execution.backend import capabilities, set_gemm_backend
from ..kernels import build_rope_tables
from .embedding import XingChengEmbeddings
from .norm import XingChengNorm
from .transformer_block import XingChengBlock


class XingChengModel(nn.Module):
    """星澄 Transformer 解碼器 backbone。"""

    def __init__(self, config: XingChengConfig) -> None:
        super().__init__()
        self.config = config
        self.embeddings = XingChengEmbeddings(config)
        self.layers = nn.ModuleList(
            [XingChengBlock(config) for _ in range(config.num_hidden_layers)]
        )
        self.final_norm = XingChengNorm(config)
        # RoPE 表（lazy 建立，依裝置）
        self._cos: torch.Tensor | None = None
        self._sin: torch.Tensor | None = None

    # ── RoPE 表管理 ─────────────────────────────────────────────
    def _ensure_rope_tables(self, device: torch.device, dtype: torch.dtype) -> None:
        cfg = self.config
        if cfg.position_embedding_type != "rope":
            self._cos = None
            self._sin = None
            return
        if (
            self._cos is None
            or self._cos.device != device
            or self._cos.dtype != dtype
            or self._cos.size(0) < cfg.max_position_embeddings
        ):
            self._cos, self._sin = build_rope_tables(
                cfg.head_dim,
                cfg.max_position_embeddings,
                theta=cfg.rope_theta,
                device=device,
                dtype=dtype,
            )

    def forward(
        self,
        input_ids: torch.Tensor,
        *,
        position_ids: torch.Tensor | None = None,
        attention_mask: torch.Tensor | None = None,
        kv_caches: list[tuple[torch.Tensor, torch.Tensor]] | None = None,
        use_cache: bool = False,
    ) -> tuple[torch.Tensor, list[tuple[torch.Tensor, torch.Tensor]] | None]:
        cfg = self.config
        device = input_ids.device
        embeds = self.embeddings(input_ids, position_ids=position_ids)
        dtype = embeds.dtype
        self._ensure_rope_tables(device, dtype)

        # Attention mask 轉成 additive mask（SDPA 期望）
        sdpa_mask = _build_sdpa_mask(attention_mask, embeds, device, dtype)

        hidden_states = embeds
        new_caches: list[tuple[torch.Tensor, torch.Tensor]] | None = [] if use_cache else None
        for i, layer in enumerate(self.layers):
            kv_cache = kv_caches[i] if (kv_caches is not None and i < len(kv_caches)) else None
            hidden_states, new_kv = layer(
                hidden_states,
                cos=self._cos,
                sin=self._sin,
                position_ids=position_ids,
                attention_mask=sdpa_mask,
                kv_cache=kv_cache,
                use_cache=use_cache,
            )
            if use_cache and new_caches is not None:
                new_caches.append(new_kv)
        hidden_states = self.final_norm(hidden_states)
        return hidden_states, new_caches


class XingChengForCausalLM(nn.Module):
    """星澄 Causal LM：backbone + LM Head + (可選) loss。"""

    def __init__(self, config: XingChengConfig) -> None:
        super().__init__()
        self.config = config
        self.model = XingChengModel(config)
        if config.tie_word_embeddings:
            self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
            self.lm_head.weight = self.model.embeddings.word_embeddings.weight  # 綁定
        else:
            self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
            nn.init.normal_(
                self.lm_head.weight, mean=0.0, std=config.initializer_range
            )
        # 提示 GEMM 後端
        self._backend: str | None = None

    def forward(
        self,
        input_ids: torch.Tensor,
        *,
        position_ids: torch.Tensor | None = None,
        attention_mask: torch.Tensor | None = None,
        labels: torch.Tensor | None = None,
        kv_caches: list[tuple[torch.Tensor, torch.Tensor]] | None = None,
        use_cache: bool = False,
    ) -> dict[str, torch.Tensor]:
        if self._backend is None:
            self._backend = set_gemm_backend(input_ids.device)
        hidden_states, new_caches = self.model(
            input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            kv_caches=kv_caches,
            use_cache=use_cache,
        )
        logits = self.lm_head(hidden_states)
        out: dict[str, torch.Tensor] = {"logits": logits}
        if labels is not None:
            out["loss"] = _causal_lm_loss(logits, labels, self.config.pad_token_id)
        if new_caches is not None:
            out["kv_caches"] = new_caches  # type: ignore[assignment]
        return out

    # ── 便利方法 ────────────────────────────────────────────────
    @torch.no_grad()
    def num_parameters(self, only_trainable: bool = True) -> int:
        return sum(
            p.numel() for p in self.parameters() if (p.requires_grad or not only_trainable)
        )

    def capabilities(self) -> dict:
        return capabilities().summary()


def _build_sdpa_mask(
    attention_mask: torch.Tensor | None,
    embeds: torch.Tensor,
    device: torch.device,
    dtype: torch.dtype,
) -> torch.Tensor | None:
    """將 (B, S) padding mask 轉成 (B, 1, S, S) additive mask。"""
    if attention_mask is None:
        return None
    # attention_mask: (B, S)，1 為有效、0 為 padding
    b, s = attention_mask.shape
    mask = attention_mask[:, None, None, :].to(dtype=dtype, device=device)
    additive = (1.0 - mask) * torch.finfo(dtype).min
    # 廣播為 (B, 1, S, S)
    return additive.expand(b, 1, s, s)


def _causal_lm_loss(
    logits: torch.Tensor,
    labels: torch.Tensor,
    pad_token_id: int,
) -> torch.Tensor:
    """標準自回歸交叉熵（shift by 1）。"""
    shift_logits = logits[..., :-1, :].contiguous()
    shift_labels = labels[..., 1:].contiguous()
    return F.cross_entropy(
        shift_logits.view(-1, shift_logits.size(-1)),
        shift_labels.view(-1),
        ignore_index=pad_token_id,
    )


__all__ = ["XingChengModel", "XingChengForCausalLM"]
