"""星澄原生推論的 Native Dispatch 層（架構圖：Native Dispatch → Python / native）。

依 ``shared_layer.performance.native_dispatcher``（A219）的既定契約派送：
原生計算核心可用、工作量超過門檻、且與 Python 結果一致時走 native；
否則一律回退純 Python（A219/E184 永久可用）。

一致性（parity）每個形狀只驗證一次並快取，之後不再重算 Python 版本，
因此派送不會讓推論付出雙倍成本。
"""

from __future__ import annotations

import os
from typing import Any

import torch
import torch.nn.functional as F

DISPATCH_ENV = "XINGCHENG_NATIVE_DISPATCH"
_TRUTHY = {"1", "true", "yes", "on"}
_VERIFIED_SHAPES: set[tuple[Any, ...]] = set()
_VERIFIED_NORM_SHAPES: set[tuple[Any, ...]] = set()
_VERIFIED_ROPE_SHAPES: set[tuple[Any, ...]] = set()


def _dispatcher() -> Any | None:
    try:
        from shared_layer.performance import native_dispatcher as module
    except ImportError:
        return None
    return module


def dispatch_enabled() -> bool:
    """派送預設開啟；設 ``XINGCHENG_NATIVE_DISPATCH=0`` 可強制純 Python。"""
    raw = str(os.environ.get(DISPATCH_ENV) or "").strip().casefold()
    if not raw:
        return True
    return raw in _TRUTHY


def native_available() -> bool:
    module = _dispatcher()
    if module is None:
        return False
    try:
        return bool(module.native_available())
    except Exception:  # pragma: no cover - 防禦性
        return False


def should_dispatch(capability: str, size: int) -> bool:
    if not dispatch_enabled():
        return False
    module = _dispatcher()
    if module is None:
        return False
    try:
        return bool(module.should_dispatch(capability, int(size)))
    except Exception:  # pragma: no cover - 防禦性
        return False


def _rmsnorm_reference(
    hidden_states: torch.Tensor, weight: torch.Tensor, eps: float
) -> torch.Tensor:
    x = hidden_states.detach().to(torch.float64)
    variance = x.pow(2).mean(dim=-1, keepdim=True)
    return x * torch.rsqrt(variance + float(eps)) * weight.detach().to(torch.float64)


def _rmsnorm_via_native(
    hidden_states: torch.Tensor, weight: torch.Tensor, eps: float, module: Any
) -> torch.Tensor | None:
    try:
        rows = int(hidden_states.numel() // hidden_states.size(-1))
        cols = int(hidden_states.size(-1))
        x = hidden_states.detach().to(torch.float64).reshape(rows, cols).contiguous()
        w = weight.detach().to(torch.float64).contiguous()
        result = module.rmsnorm(x.tolist(), w.tolist(), float(eps))
        return torch.tensor(result, dtype=torch.float64).reshape(hidden_states.shape)
    except Exception:  # pragma: no cover - 任何 ABI 問題都回退
        return None


def native_rmsnorm(
    hidden_states: torch.Tensor, weight: torch.Tensor, eps: float
) -> torch.Tensor | None:
    """派送 RMSNorm 到原生 C 核心；``None`` 代表由呼叫端走 PyTorch。"""
    if hidden_states.device.type != "cpu" or weight.device.type != "cpu":
        return None
    if hidden_states.requires_grad or weight.requires_grad:
        return None
    if hidden_states.dim() < 1 or hidden_states.size(-1) != weight.numel():
        return None
    rows = int(hidden_states.numel() // hidden_states.size(-1))
    if not should_dispatch("transformer.rmsnorm", rows):
        return None
    module = _dispatcher()
    if module is None:
        return None

    shape_key = ("rmsnorm", rows, int(hidden_states.size(-1)))
    if shape_key in _VERIFIED_NORM_SHAPES:
        candidate = _rmsnorm_via_native(hidden_states, weight, eps, module)
        return candidate.to(hidden_states.dtype) if candidate is not None else None

    expected = _rmsnorm_reference(hidden_states, weight, eps)
    candidate = _rmsnorm_via_native(hidden_states, weight, eps, module)
    if candidate is None or not torch.allclose(candidate, expected, atol=1e-8, rtol=1e-7):
        return None
    _VERIFIED_NORM_SHAPES.add(shape_key)
    return candidate.to(hidden_states.dtype)


def _rope_tables(
    x: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    position_ids: torch.Tensor | None,
) -> tuple[torch.Tensor, torch.Tensor] | None:
    try:
        batch, _, seq_len, head_dim = x.shape
        if position_ids is not None:
            cos = cos[position_ids]
            sin = sin[position_ids]
        elif cos.dim() == 2:
            cos = cos.unsqueeze(0).expand(batch, -1, -1)
            sin = sin.unsqueeze(0).expand(batch, -1, -1)
        cos = cos[:, :seq_len, :].to(torch.float64).contiguous()
        sin = sin[:, :seq_len, :].to(torch.float64).contiguous()
        if cos.shape != (batch, seq_len, head_dim) or sin.shape != cos.shape:
            return None
        return cos, sin
    except Exception:  # pragma: no cover - 防禦性
        return None


def _rope_reference(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
    x64 = x.detach().to(torch.float64)
    half = x.size(-1) // 2
    x1 = x64[..., :half]
    x2 = x64[..., half:]
    c = cos.unsqueeze(1)[..., :half]
    s = sin.unsqueeze(1)[..., :half]
    return torch.cat([x1 * c - x2 * s, x1 * s + x2 * c], dim=-1)


def _rope_via_native(
    x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor, module: Any
) -> torch.Tensor | None:
    try:
        x64 = x.detach().to(torch.float64).contiguous()
        result = module.rope(x64.tolist(), cos.tolist(), sin.tolist())
        return torch.tensor(result, dtype=torch.float64).reshape(x.shape)
    except Exception:  # pragma: no cover - 任何 ABI 問題都回退
        return None


def native_rope(
    q: torch.Tensor,
    k: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    position_ids: torch.Tensor | None = None,
) -> tuple[torch.Tensor, torch.Tensor] | None:
    """派送 RoPE 到原生 C 核心；``None`` 代表由呼叫端走 PyTorch。"""
    if q.device.type != "cpu" or k.device.type != "cpu":
        return None
    if q.requires_grad or k.requires_grad:
        return None
    if q.dim() != 4 or k.dim() != 4 or q.size(-1) % 2 != 0:
        return None
    if q.shape[0] != k.shape[0] or q.shape[2] != k.shape[2] or q.shape[3] != k.shape[3]:
        return None
    if not should_dispatch("transformer.rope", int(q.size(2))):
        return None
    module = _dispatcher()
    if module is None:
        return None

    tables = _rope_tables(q, cos, sin, position_ids)
    if tables is None:
        return None
    cos64, sin64 = tables
    shape_key = (
        "rope", int(q.size(0)), int(q.size(1)), int(k.size(1)),
        int(q.size(2)), int(q.size(3)), position_ids is not None,
    )
    if shape_key in _VERIFIED_ROPE_SHAPES:
        q_out = _rope_via_native(q, cos64, sin64, module)
        k_out = _rope_via_native(k, cos64, sin64, module)
        if q_out is None or k_out is None:
            return None
        return q_out.to(q.dtype), k_out.to(k.dtype)

    expected_q = _rope_reference(q, cos64, sin64)
    expected_k = _rope_reference(k, cos64, sin64)
    candidate_q = _rope_via_native(q, cos64, sin64, module)
    candidate_k = _rope_via_native(k, cos64, sin64, module)
    if (
        candidate_q is None or candidate_k is None
        or not torch.allclose(candidate_q, expected_q, atol=1e-8, rtol=1e-7)
        or not torch.allclose(candidate_k, expected_k, atol=1e-8, rtol=1e-7)
    ):
        return None
    _VERIFIED_ROPE_SHAPES.add(shape_key)
    return candidate_q.to(q.dtype), candidate_k.to(k.dtype)


def _causal_reference(
    query: torch.Tensor, key: torch.Tensor, value: torch.Tensor, scale: float
) -> torch.Tensor:
    """純 Python/PyTorch 的因果注意力（作為 parity 基準與回退路徑）。"""
    scores = torch.matmul(query, key.transpose(-1, -2)) * float(scale)
    q_len, k_len = query.size(-2), key.size(-2)
    causal = torch.ones(q_len, k_len, dtype=torch.bool, device=query.device).tril()
    scores = scores.masked_fill(~causal, -1.0e30)
    return torch.matmul(torch.softmax(scores, dim=-1), value)


def _causal_via_native(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    scale: float,
    module: Any,
) -> torch.Tensor | None:
    """以原生 C 原語組合因果注意力：matmul → 遮罩 → softmax → matmul。

    全程使用原生核心的 ``transformer_matmul`` / ``transformer_softmax``，
    因果限制由 Python 在分數矩陣上施加（原生核心不處理遮罩）。
    """
    batch, heads, q_len, head_dim = query.shape
    k_len = key.size(2)
    if k_len != q_len:
        return None
    value_dim = value.size(-1)
    try:
        q = query.detach().to(torch.float64).reshape(batch * heads, q_len, head_dim)
        k = key.detach().to(torch.float64).reshape(batch * heads, k_len, head_dim)
        v = value.detach().to(torch.float64).reshape(batch * heads, k_len, value_dim)
    except Exception:  # pragma: no cover - 防禦性
        return None

    k_t = k.transpose(-1, -2).contiguous()
    causal = torch.ones(q_len, k_len, dtype=torch.bool).tril()
    # 遮罩值必須有限：C++ softmax 以指數計算，-inf 會產生 NaN
    masked_value = -1.0e30
    rows: list[torch.Tensor] = []
    try:
        for index in range(batch * heads):
            scores = torch.tensor(
                module.matmul(q[index].tolist(), k_t[index].tolist()),
                dtype=torch.float64,
            )
            scores = scores * float(scale)
            scores = scores.masked_fill(~causal, masked_value)
            weights = torch.tensor(
                module.softmax(scores.tolist()), dtype=torch.float64
            )
            out = torch.tensor(
                module.matmul(weights.tolist(), v[index].tolist()),
                dtype=torch.float64,
            )
            rows.append(out)
    except Exception:  # pragma: no cover - 任何 ABI 問題都回退
        return None
    return torch.stack(rows, dim=0).reshape(
        batch, heads, q_len, value_dim
    ).to(query.dtype)


def native_attention(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    *,
    scale: float,
    is_causal: bool,
    attention_mask: torch.Tensor | None,
) -> torch.Tensor | None:
    """派送 attention 到原生計算核心；回傳 ``None`` 代表由呼叫端走 PyTorch。

    派送條件（全部成立才派送）：flag 開啟、原生核心可用、超過門檻、
    無 mask、因果、無梯度、CPU、批次頭形狀一致，且該形狀已通過 parity。
    """
    if not is_causal or attention_mask is not None:
        return None
    if query.device.type != "cpu":
        return None
    # 有梯度需求時必須留在 PyTorch autograd 圖內（C++ 路徑不提供 backward）
    if any(tensor.requires_grad for tensor in (query, key, value)):
        return None
    # attention 傳入形狀為 (B, H, S, D)
    if query.dim() != 4 or key.dim() != 4 or value.dim() != 4:
        return None
    heads = int(query.size(1))
    if key.size(1) != heads or value.size(1) != heads:
        return None
    q_len = int(query.size(-2))
    if not should_dispatch("transformer.attention", q_len):
        return None
    module = _dispatcher()
    if module is None:
        return None

    shape_key = (heads, q_len, int(query.size(-1)))
    if shape_key in _VERIFIED_SHAPES:
        return _causal_via_native(query, key, value, scale, module)

    expected = _causal_reference(query, key, value, scale)
    candidate = _causal_via_native(query, key, value, scale, module)
    if candidate is None:
        return None
    if not torch.allclose(candidate, expected, atol=1e-5, rtol=1e-4):
        return None
    _VERIFIED_SHAPES.add(shape_key)
    return candidate


def verified_shapes() -> list[tuple[int, int, int]]:
    return sorted(_VERIFIED_SHAPES)


def verified_rmsnorm_shapes() -> list[tuple[Any, ...]]:
    return sorted(_VERIFIED_NORM_SHAPES)


def verified_rope_shapes() -> list[tuple[Any, ...]]:
    return sorted(_VERIFIED_ROPE_SHAPES)


def summary() -> dict[str, Any]:
    """目前派送狀態（供健康檢查與稽核輸出）。"""
    module = _dispatcher()
    return {
        "dispatch_enabled": dispatch_enabled(),
        "native_available": native_available(),
        "backend": "native-compute-core" if native_available() else "python-pytorch",
        "verified_shapes": verified_shapes(),
        "verified_rmsnorm_shapes": verified_rmsnorm_shapes(),
        "verified_rope_shapes": verified_rope_shapes(),
        "thresholds": dict(getattr(module, "DISPATCH_THRESHOLDS", {})) if module else {},
    }


__all__ = [
    "DISPATCH_ENV",
    "dispatch_enabled",
    "native_attention",
    "native_available",
    "native_rmsnorm",
    "native_rope",
    "should_dispatch",
    "summary",
    "verified_rmsnorm_shapes",
    "verified_rope_shapes",
    "verified_shapes",
]
