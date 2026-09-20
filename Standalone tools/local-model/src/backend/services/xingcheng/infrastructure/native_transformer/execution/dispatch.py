"""星澄原生推論的 Native Dispatch 層（架構圖：Native Dispatch → Python / C++）。

依 ``shared_layer.performance.native_dispatcher``（A219）的既定契約派送：
原生 C++ 核心可用、工作量超過門檻、且與 Python 結果一致時走 C++；
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
_VERIFIED_SHAPES: set[tuple[int, int, int]] = set()


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
    """以 C++ 原語組合因果注意力：matmul → 遮罩 → softmax → matmul。

    全程使用原生核心的 ``transformer_matmul`` / ``transformer_softmax``，
    因果限制由 Python 在分數矩陣上施加（C++ 核心不處理遮罩）。
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
    """派送 attention 到 C++ 核心；回傳 ``None`` 代表由呼叫端走 PyTorch。

    派送條件（全部成立才派送）：flag 開啟、C++ 核心可用、超過門檻、
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


def summary() -> dict[str, Any]:
    """目前派送狀態（供健康檢查與稽核輸出）。"""
    module = _dispatcher()
    return {
        "dispatch_enabled": dispatch_enabled(),
        "native_available": native_available(),
        "backend": "c++-native-core" if native_available() else "python-pytorch",
        "verified_shapes": verified_shapes(),
        "thresholds": dict(getattr(module, "DISPATCH_THRESHOLDS", {})) if module else {},
    }


__all__ = [
    "DISPATCH_ENV",
    "dispatch_enabled",
    "native_attention",
    "native_available",
    "should_dispatch",
    "summary",
    "verified_shapes",
]
