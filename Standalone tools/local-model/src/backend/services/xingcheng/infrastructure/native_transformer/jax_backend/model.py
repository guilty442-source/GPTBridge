"""星澄 JAX dense decoder：參數樹初始化與純函數 forward。

參數樹（pytree，全部 ``jnp.float32``）::

    {
      "tok_emb": [V, H],
      "blocks": [ {                      # × num_hidden_layers
          "attn_norm": [H],
          "q_w": [H, nH*Dh], "k_w": [H, nKV*Dh], "v_w": [H, nKV*Dh],
          "o_w": [nH*Dh, H],
          "mlp_norm": [H],
          "w1": [H, I], "w3": [H, I], "w2": [I, H],   # SwiGLU
      }, ... ],
      "final_norm": [H],
      "lm_head": [H, V] | None          # None = tied to tok_emb
    }

僅支援主線配置：norm_type=rmsnorm、position=rope、hidden_act=silu、
use_swiglu=True、attention_bias=False、use_moe=False。非主線配置在
``init_params`` fail-closed（``JAX_BACKEND_UNSUPPORTED``）。
"""

from __future__ import annotations

from typing import Any

import jax
import jax.numpy as jnp


class JaxBackendUnsupported(ValueError):
    """非主線模型配置：JAX 後端 fail-closed。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"JAX_BACKEND_UNSUPPORTED: {reason}")


def _check_supported(config: Any) -> None:
    if str(getattr(config, "norm_type", "rmsnorm")) != "rmsnorm":
        raise JaxBackendUnsupported(f"norm_type={getattr(config, 'norm_type', None)}")
    if str(getattr(config, "position_embedding_type", "rope")) != "rope":
        raise JaxBackendUnsupported(
            f"position_embedding_type={getattr(config, 'position_embedding_type', None)}"
        )
    if not bool(getattr(config, "use_swiglu", True)):
        raise JaxBackendUnsupported("use_swiglu=False")
    if str(getattr(config, "hidden_act", "silu")) != "silu":
        raise JaxBackendUnsupported(f"hidden_act={getattr(config, 'hidden_act', None)}")
    if bool(getattr(config, "use_moe", False)):
        raise JaxBackendUnsupported("use_moe=True (MoE remains torch lineage)")
    if bool(getattr(config, "attention_bias", False)):
        raise JaxBackendUnsupported("attention_bias=True")
    if str(getattr(config, "quantization", "none")) not in ("", "none"):
        raise JaxBackendUnsupported(
            f"quantization={getattr(config, 'quantization', None)}"
        )


def init_params(config: Any, seed: int = 0) -> dict[str, Any]:
    """依 XingChengConfig 初始化參數樹（std=initializer_range 高斯）。"""
    _check_supported(config)
    h = int(config.hidden_size)
    inter = int(config.intermediate_size)
    layers = int(config.num_hidden_layers)
    n_head = int(config.num_attention_heads)
    n_kv = int(config.num_key_value_heads)
    head_dim = int(config.head_dim) or h // n_head
    vocab = int(config.vocab_size)
    std = float(getattr(config, "initializer_range", 0.02))

    key = jax.random.PRNGKey(int(seed))
    keys = jax.random.split(key, 1 + layers * 7 + 1)

    def w(shape: tuple[int, ...], k: Any) -> jnp.ndarray:
        return jax.random.normal(k, shape, dtype=jnp.float32) * std

    params: dict[str, Any] = {"tok_emb": w((vocab, h), keys[0]), "blocks": []}
    cursor = 1
    for _ in range(layers):
        block = {
            "attn_norm": jnp.ones((h,), dtype=jnp.float32),
            "q_w": w((h, n_head * head_dim), keys[cursor]),
            "k_w": w((h, n_kv * head_dim), keys[cursor + 1]),
            "v_w": w((h, n_kv * head_dim), keys[cursor + 2]),
            "o_w": w((n_head * head_dim, h), keys[cursor + 3]),
            "mlp_norm": jnp.ones((h,), dtype=jnp.float32),
            "w1": w((h, inter), keys[cursor + 4]),
            "w3": w((h, inter), keys[cursor + 5]),
            "w2": w((inter, h), keys[cursor + 6]),
        }
        params["blocks"].append(block)
        cursor += 7
    params["final_norm"] = jnp.ones((h,), dtype=jnp.float32)
    params["lm_head"] = (
        None
        if bool(getattr(config, "tie_word_embeddings", True))
        else w((h, vocab), keys[cursor])
    )
    return params


def _rms_norm(x: jnp.ndarray, weight: jnp.ndarray, eps: float) -> jnp.ndarray:
    ms = jnp.mean(jnp.square(x), axis=-1, keepdims=True)
    return x * jax.lax.rsqrt(ms + eps) * weight


def _rope(x: jnp.ndarray, theta: float) -> jnp.ndarray:
    """x: [B, T, nHead, Dh] → rotated。pairwise rotate-half 變體。"""
    b, t, nh, dh = x.shape
    half = dh // 2
    freq = 1.0 / (theta ** (jnp.arange(half, dtype=jnp.float32) * 2.0 / dh))
    pos = jnp.arange(t, dtype=jnp.float32)
    angle = pos[:, None] * freq[None, :]  # [T, half]
    cos = jnp.cos(angle)[None, :, None, :]
    sin = jnp.sin(angle)[None, :, None, :]
    x1, x2 = x[..., :half], x[..., half:]
    return jnp.concatenate([x1 * cos - x2 * sin, x2 * cos + x1 * sin], axis=-1)


def _attention(
    block: dict[str, Any],
    x: jnp.ndarray,
    *,
    n_head: int,
    n_kv: int,
    head_dim: int,
    rope_theta: float,
) -> jnp.ndarray:
    b, t, _ = x.shape
    q = (x @ block["q_w"]).reshape(b, t, n_head, head_dim)
    k = (x @ block["k_w"]).reshape(b, t, n_kv, head_dim)
    v = (x @ block["v_w"]).reshape(b, t, n_kv, head_dim)
    q = _rope(q, rope_theta)
    k = _rope(k, rope_theta)
    if n_kv != n_head:
        k = jnp.repeat(k, n_head // n_kv, axis=2)
        v = jnp.repeat(v, n_head // n_kv, axis=2)
    # [B, nH, T, Dh]
    q = jnp.transpose(q, (0, 2, 1, 3))
    k = jnp.transpose(k, (0, 2, 1, 3))
    v = jnp.transpose(v, (0, 2, 1, 3))
    scores = (q @ jnp.swapaxes(k, -1, -2)) / jnp.sqrt(jnp.float32(head_dim))
    mask = jnp.tril(jnp.ones((t, t), dtype=bool))
    scores = jnp.where(mask[None, None, :, :], scores, jnp.finfo(jnp.float32).min)
    probs = jax.nn.softmax(scores, axis=-1)
    out = jnp.transpose(probs @ v, (0, 2, 1, 3)).reshape(b, t, n_head * head_dim)
    return out @ block["o_w"]


def forward_logits(
    params: dict[str, Any],
    config: Any,
    input_ids: jnp.ndarray,
) -> jnp.ndarray:
    """input_ids: [B, T] int32 → logits [B, T, V]。"""
    _check_supported(config)
    h = int(config.hidden_size)
    n_head = int(config.num_attention_heads)
    n_kv = int(config.num_key_value_heads)
    head_dim = int(config.head_dim) or h // n_head
    eps = float(config.rms_norm_eps)
    theta = float(getattr(config, "rope_theta", 10_000.0))

    x = params["tok_emb"][input_ids]
    for block in params["blocks"]:
        normed = _rms_norm(x, block["attn_norm"], eps)
        x = x + _attention(
            block, normed, n_head=n_head, n_kv=n_kv,
            head_dim=head_dim, rope_theta=theta,
        )
        normed = _rms_norm(x, block["mlp_norm"], eps)
        gate = jax.nn.silu(normed @ block["w1"])
        x = x + ((gate * (normed @ block["w3"])) @ block["w2"])
    x = _rms_norm(x, params["final_norm"], eps)
    head = params["lm_head"]
    if head is None:
        # tied embeddings：tok_emb [V,H] 共享為 LM head → x @ W^T
        return x @ jnp.swapaxes(params["tok_emb"], -1, -2)
    return x @ head
