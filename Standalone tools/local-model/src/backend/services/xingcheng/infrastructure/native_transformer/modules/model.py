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
            [XingChengBlock(config, layer_idx=i) for i in range(config.num_hidden_layers)]
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
    ) -> tuple[torch.Tensor, list[tuple[torch.Tensor, torch.Tensor]] | None, torch.Tensor | None]:
        cfg = self.config
        device = input_ids.device
        embeds = self.embeddings(input_ids, position_ids=position_ids)
        dtype = embeds.dtype
        self._ensure_rope_tables(device, dtype)

        kv_len = embeds.size(1)
        if kv_caches:
            kv_len += kv_caches[0][0].size(2)
        sdpa_mask = _build_sdpa_mask(attention_mask, embeds, kv_len, device, dtype)

        hidden_states = embeds
        new_caches: list[tuple[torch.Tensor, torch.Tensor]] | None = [] if use_cache else None
        aux_losses: list[torch.Tensor] = []
        for i, layer in enumerate(self.layers):
            kv_cache = kv_caches[i] if (kv_caches is not None and i < len(kv_caches)) else None
            # 向下相容：舊 Block 回傳 2 值，新 Block 回傳 3 值（含 aux_loss）
            result = layer(
                hidden_states,
                cos=self._cos,
                sin=self._sin,
                position_ids=position_ids,
                attention_mask=sdpa_mask,
                kv_cache=kv_cache,
                use_cache=use_cache,
            )
            if len(result) == 3:
                hidden_states, new_kv, aux = result
                if aux is not None:
                    aux_losses.append(aux)
            else:  # 舊介面
                hidden_states, new_kv = result  # type: ignore[misc]
            if use_cache and new_caches is not None:
                new_caches.append(new_kv)
        hidden_states = self.final_norm(hidden_states)
        aux_loss: torch.Tensor | None = None
        if aux_losses:
            aux_loss = torch.stack(aux_losses).mean()
        return hidden_states, new_caches, aux_loss


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
        backbone = self.model(
            input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            kv_caches=kv_caches,
            use_cache=use_cache,
        )
        # 向下相容 2/3 回傳
        if len(backbone) == 3:
            hidden_states, new_caches, aux_loss = backbone
        else:
            hidden_states, new_caches = backbone  # type: ignore[misc]
            aux_loss = None
        logits = self.lm_head(hidden_states)
        out: dict[str, torch.Tensor] = {"logits": logits}
        if aux_loss is not None:
            out["aux_loss"] = aux_loss
        if labels is not None:
            ce = _causal_lm_loss(logits, labels, self.config.pad_token_id)
            if aux_loss is not None:
                out["loss"] = ce + float(self.config.moe_aux_loss_weight) * aux_loss
                out["ce_loss"] = ce
            else:
                out["loss"] = ce
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
    kv_len: int,
    device: torch.device,
    dtype: torch.dtype,
) -> torch.Tensor | None:
    """將 (B, S) padding mask 合併因果限制，轉成 (B, 1, S_q, S_k) additive mask。

    解碼時 ``attention_mask`` 需覆蓋完整 KV 長度；長度為 1 時視為全部有效。
    """
    if attention_mask is None:
        return None
    b, q_len = embeds.size(0), embeds.size(1)
    offset = kv_len - q_len
    if offset < 0:
        raise ValueError("KV_CACHE_LONGER_THAN_QUERY")
    keys = torch.arange(kv_len, device=device).unsqueeze(0)
    queries = torch.arange(q_len, device=device).unsqueeze(1) + offset
    allowed = (keys <= queries).view(1, 1, q_len, kv_len).expand(b, 1, q_len, kv_len)
    key_mask = attention_mask[:, None, None, :].to(device=device, dtype=torch.bool)
    if key_mask.size(-1) not in (1, kv_len):
        raise ValueError("ATTENTION_MASK_LENGTH_MISMATCH")
    allowed = allowed & key_mask
    # 整列被遮時保留第一個 key，避免 softmax 產生 NaN
    empty_rows = ~allowed.any(dim=-1, keepdim=True)
    allowed = allowed | (empty_rows & (keys == 0).view(1, 1, 1, kv_len))
    additive = torch.zeros(b, 1, q_len, kv_len, device=device, dtype=dtype)
    return additive.masked_fill(~allowed, torch.finfo(dtype).min)


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
