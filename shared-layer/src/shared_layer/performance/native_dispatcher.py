"""Native Core Dispatcher — Python/native dispatch with fallback (A219).

Dispatches compute-heavy calls to the compiled native core when:
    1. The native extension is available (built and importable)
    2. The workload exceeds the dispatch threshold (boundary crossing
       cost is only justified for non-trivial work)
    3. Parity is preserved (same output as Python fallback)

If any condition fails, the Python fallback is used.  The Python
fallback is permanently available (A219/E184).

Dispatch thresholds are evidence-based: they come from the benchmark
framework's measurement of boundary crossing cost vs. native compute
savings.  A threshold of 0 means "always dispatch if available"; a
threshold of N means "only dispatch when input size >= N".

Safety rules (A221/E186):
    - Python owns all memory; the C++ binding borrows raw pointers + length.
    - No C++ exceptions cross the C ABI boundary.
    - No unbounded allocation; no per-request thread pool.
    - No cross-runtime free.
    - GIL released only during pure compute (never during conversion).
"""
from __future__ import annotations

import math
import re
from typing import Any, Sequence

try:
    import numpy as np
except ImportError:  # pragma: no cover — fallback when numpy unavailable
    np = None  # type: ignore[assignment]

def _as_float64_array(data: Any):  # type: ignore[no-untyped-def]
    """Zero-copy view when already float64 ndarray, otherwise copy once."""
    if np is None:
        raise RuntimeError("numpy is required for native dispatch")
    if isinstance(data, np.ndarray) and data.dtype == np.float64:
        return data
    # For list/ndarray of other dtype, allow copy; avoid ValueError from copy=False on list
    try:
        return np.asarray(data, dtype=np.float64, copy=False)
    except ValueError:
        return np.asarray(data, dtype=np.float64)

# --- Native extension loader (lazy, with fallback) ---

_NATIVE = None
_NATIVE_TRIED = False


def _load_native() -> Any:
    """Try to load the native extension.  Returns None if unavailable."""
    global _NATIVE, _NATIVE_TRIED
    if _NATIVE_TRIED:
        return _NATIVE
    _NATIVE_TRIED = True
    try:
        # The extension is built next to the binding in the native package.
        # We import it relative to this module's location.
        import importlib
        import sys
        import pathlib

        # Prefer the governed build output; the package copy may be locked by a
        # running process on Windows, while packaged layouts can omit dist-native.
        _here = pathlib.Path(__file__).resolve()
        _project = _here.parents[4]
        _native_pkg = _project / "main-system" / "src-core" / "core_system" / "native"
        _candidates = [
            _project / "main-system" / "dist-native",
            _native_pkg,
        ]
        for cand in _candidates:
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            try:
                _NATIVE = importlib.import_module("_sovereign_native")
                return _NATIVE
            except ImportError:
                continue
    except Exception:
        pass
    return _NATIVE


def native_available() -> bool:
    """Whether the compiled native core is loaded."""
    return _load_native() is not None


# --- Dispatch thresholds ---
# These are initial conservative thresholds based on the V1 benchmark.
# They will be tightened as benchmark evidence accumulates.
# A threshold of 0 means "always dispatch if available".
# A threshold of N means "only dispatch when input size >= N".

DISPATCH_THRESHOLDS: dict[str, int] = {
    "parser.token_estimate": 0,        # always dispatch (cheap boundary)
    "parser.batch_token_estimate": 10,  # batch only worth it for 10+ items
    "vector.dot": 128,                  # 128+ dims to justify boundary
    "vector.l2_norm": 128,
    "vector.cosine_similarity": 128,
    "vector.batch_dot": 8,              # 8+ pairs to justify batch boundary
    "transformer.matmul": 8,            # 8+ rows/cols to justify boundary
    "transformer.softmax": 8,
    "transformer.rmsnorm": 4,           # 4+ rows to justify norm boundary
    "transformer.rope": 64,             # 64+ sequence positions; smaller inputs lose to boundary cost
    "transformer.attention": 4,          # 4+ rows to justify attention boundary
}


def get_threshold(capability: str) -> int:
    """Get the dispatch threshold for a capability."""
    return DISPATCH_THRESHOLDS.get(capability, 0)


def should_dispatch(capability: str, input_size: int) -> bool:
    """Whether to dispatch to native based on input size and threshold."""
    if not native_available():
        return False
    threshold = get_threshold(capability)
    return input_size >= threshold


# --- Parser: Python fallback implementations ---

_TOKEN_RE = re.compile(r"\b\w+\b|[^\w\s]")


def python_token_estimate(text: str) -> int:
    """Python fallback: count tokens (words + punctuation)."""
    return len(_TOKEN_RE.findall(text))


def python_batch_token_estimate(texts: Sequence[str]) -> list[int]:
    """Python fallback: batch token estimation."""
    return [python_token_estimate(t) for t in texts]


def native_token_estimate(text: str) -> int:
    """Native dispatch: token estimate (if available)."""
    n = _load_native()
    if n is None:
        return python_token_estimate(text)
    return int(n.parser_token_estimate(text))


def native_batch_token_estimate(texts: Sequence[str]) -> list[int]:
    """Native dispatch: batch token estimate (if available)."""
    n = _load_native()
    if n is None or len(texts) < get_threshold("parser.batch_token_estimate"):
        return python_batch_token_estimate(texts)
    return [int(x) for x in n.parser_batch_token_estimate(list(texts))]


def token_estimate(text: str) -> int:
    """Dispatch token estimate to native or Python fallback."""
    if should_dispatch("parser.token_estimate", len(text)):
        return native_token_estimate(text)
    return python_token_estimate(text)


def batch_token_estimate(texts: Sequence[str]) -> list[int]:
    """Dispatch batch token estimate to native or Python fallback."""
    if should_dispatch("parser.batch_token_estimate", len(texts)):
        return native_batch_token_estimate(texts)
    return python_batch_token_estimate(texts)


# --- Vector: Python fallback implementations ---

def python_dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Python fallback: dot product."""
    return sum(x * y for x, y in zip(a, b))


def python_l2_norm(a: Sequence[float]) -> float:
    """Python fallback: L2 norm."""
    return math.sqrt(sum(x * x for x in a))


def python_cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Python fallback: cosine similarity."""
    dot_val = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot_val / (norm_a * norm_b)


def native_dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Native dispatch: dot product (if available)."""
    n = _load_native()
    if n is None:
        return python_dot(a, b)
    return float(n.vector_dot(_as_float64_array(a), _as_float64_array(b)))


def native_l2_norm(a: Sequence[float]) -> float:
    """Native dispatch: L2 norm (if available)."""
    n = _load_native()
    if n is None:
        return python_l2_norm(a)
    return float(n.vector_l2_norm(_as_float64_array(a)))


def native_cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Native dispatch: cosine similarity (if available)."""
    n = _load_native()
    if n is None:
        return python_cosine_similarity(a, b)
    return float(n.vector_cosine_similarity(
        _as_float64_array(a), _as_float64_array(b)))


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Dispatch dot product to native or Python fallback."""
    if should_dispatch("vector.dot", len(a)):
        return native_dot(a, b)
    return python_dot(a, b)


def l2_norm(a: Sequence[float]) -> float:
    """Dispatch L2 norm to native or Python fallback."""
    if should_dispatch("vector.l2_norm", len(a)):
        return native_l2_norm(a)
    return python_l2_norm(a)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Dispatch cosine similarity to native or Python fallback."""
    if should_dispatch("vector.cosine_similarity", len(a)):
        return native_cosine_similarity(a, b)
    return python_cosine_similarity(a, b)


# --- Transformer: Python fallback implementations ---

def python_matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    """Python fallback: matrix multiply C = A * B."""
    m = len(a)
    k = len(a[0]) if m > 0 else 0
    n = len(b[0]) if len(b) > 0 else 0
    c = [[0.0] * n for _ in range(m)]
    for i in range(m):
        for p in range(k):
            a_val = a[i][p]
            for j in range(n):
                c[i][j] += a_val * b[p][j]
    return c


def python_softmax(input_2d: Sequence[Sequence[float]]) -> list[list[float]]:
    """Python fallback: softmax over last dimension."""
    import math
    result = []
    for row in input_2d:
        max_val = max(row) if row else 0.0
        exps = [math.exp(x - max_val) for x in row]
        sum_exp = sum(exps)
        if sum_exp == 0.0:
            uniform = 1.0 / len(row) if row else 0.0
            result.append([uniform] * len(row))
        else:
            result.append([e / sum_exp for e in exps])
    return result


def python_rmsnorm(
    input_2d: Sequence[Sequence[float]],
    weight: Sequence[float],
    eps: float,
) -> list[list[float]]:
    """Python fallback: RMSNorm over the last dimension."""
    result: list[list[float]] = []
    for row in input_2d:
        variance = sum(x * x for x in row) / len(row) if row else 0.0
        inv_rms = 1.0 / math.sqrt(variance + eps)
        result.append([x * inv_rms * w for x, w in zip(row, weight)])
    return result


def python_rope(
    input_4d: Sequence[Sequence[Sequence[Sequence[float]]]],
    cos_table: Sequence[Sequence[Sequence[float]]],
    sin_table: Sequence[Sequence[Sequence[float]]],
) -> list[list[list[list[float]]]]:
    """Python fallback: RoPE over [B,H,S,D] with [B,S,D] tables."""
    output: list[list[list[list[float]]]] = []
    for b, batch in enumerate(input_4d):
        batch_out: list[list[list[float]]] = []
        for h, head in enumerate(batch):
            head_out: list[list[float]] = []
            for s, row in enumerate(head):
                half = len(row) // 2
                rotated = [0.0] * len(row)
                for i in range(half):
                    x1, x2 = row[i], row[i + half]
                    c, sn = cos_table[b][s][i], sin_table[b][s][i]
                    rotated[i] = x1 * c - x2 * sn
                    rotated[i + half] = x1 * sn + x2 * c
                head_out.append(rotated)
            batch_out.append(head_out)
        output.append(batch_out)
    return output


def python_scaled_dot_product_attention(
    q: Sequence[Sequence[float]],
    k: Sequence[Sequence[float]],
    v: Sequence[Sequence[float]],
) -> list[list[float]]:
    """Python fallback: scaled dot-product attention."""
    import math
    q_rows = len(q)
    d_k = len(q[0]) if q_rows > 0 else 0
    k_rows = len(k)
    d_v = len(v[0]) if len(v) > 0 else 0
    scale = 1.0 / math.sqrt(d_k) if d_k > 0 else 0.0

    # scores = Q * K^T * scale
    scores = [[0.0] * k_rows for _ in range(q_rows)]
    for i in range(q_rows):
        for j in range(k_rows):
            dot_val = sum(q[i][d] * k[j][d] for d in range(d_k))
            scores[i][j] = dot_val * scale

    # weights = softmax(scores)
    weights = python_softmax(scores)

    # output = weights * V
    output = [[0.0] * d_v for _ in range(q_rows)]
    for i in range(q_rows):
        for d in range(d_v):
            output[i][d] = sum(weights[i][j] * v[j][d] for j in range(k_rows))
    return output


def native_matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    """Native dispatch: matmul (if available)."""
    n = _load_native()
    if n is None:
        return python_matmul(a, b)
    result = n.transformer_matmul(
        _as_float64_array(a),
        _as_float64_array(b),
    )
    return result.tolist()


def native_softmax(input_2d: Sequence[Sequence[float]]) -> list[list[float]]:
    """Native dispatch: softmax (if available)."""
    n = _load_native()
    if n is None:
        return python_softmax(input_2d)
    result = n.transformer_softmax(_as_float64_array(input_2d))
    return result.tolist()


def native_rmsnorm(
    input_2d: Sequence[Sequence[float]],
    weight: Sequence[float],
    eps: float,
) -> list[list[float]]:
    """Native dispatch: RMSNorm (if available)."""
    n = _load_native()
    if n is None:
        return python_rmsnorm(input_2d, weight, eps)
    result = n.transformer_rmsnorm(
        _as_float64_array(input_2d),
        _as_float64_array(weight),
        float(eps),
    )
    return result.tolist()


def native_rope(
    input_4d: Sequence[Sequence[Sequence[Sequence[float]]]],
    cos_table: Sequence[Sequence[Sequence[float]]],
    sin_table: Sequence[Sequence[Sequence[float]]],
) -> list[list[list[list[float]]]]:
    """Native dispatch: RoPE (if available)."""
    n = _load_native()
    if n is None:
        return python_rope(input_4d, cos_table, sin_table)
    result = n.transformer_rope(
        _as_float64_array(input_4d),
        _as_float64_array(cos_table),
        _as_float64_array(sin_table),
    )
    return result.tolist()


def native_scaled_dot_product_attention(
    q: Sequence[Sequence[float]],
    k: Sequence[Sequence[float]],
    v: Sequence[Sequence[float]],
) -> list[list[float]]:
    """Native dispatch: scaled dot-product attention (if available)."""
    n = _load_native()
    if n is None:
        return python_scaled_dot_product_attention(q, k, v)
    result = n.transformer_scaled_dot_product_attention(
        _as_float64_array(q),
        _as_float64_array(k),
        _as_float64_array(v),
    )
    return result.tolist()


def matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    """Dispatch matmul to native or Python fallback."""
    m = len(a)
    if should_dispatch("transformer.matmul", m):
        return native_matmul(a, b)
    return python_matmul(a, b)


def softmax(input_2d: Sequence[Sequence[float]]) -> list[list[float]]:
    """Dispatch softmax to native or Python fallback."""
    rows = len(input_2d)
    if should_dispatch("transformer.softmax", rows):
        return native_softmax(input_2d)
    return python_softmax(input_2d)


def rmsnorm(
    input_2d: Sequence[Sequence[float]],
    weight: Sequence[float],
    eps: float,
) -> list[list[float]]:
    """Dispatch RMSNorm to native or Python fallback."""
    rows = len(input_2d)
    if should_dispatch("transformer.rmsnorm", rows):
        return native_rmsnorm(input_2d, weight, eps)
    return python_rmsnorm(input_2d, weight, eps)


def rope(
    input_4d: Sequence[Sequence[Sequence[Sequence[float]]]],
    cos_table: Sequence[Sequence[Sequence[float]]],
    sin_table: Sequence[Sequence[Sequence[float]]],
) -> list[list[list[list[float]]]]:
    """Dispatch RoPE to native or Python fallback."""
    seq_len = len(input_4d[0][0]) if input_4d and input_4d[0] else 0
    if should_dispatch("transformer.rope", seq_len):
        return native_rope(input_4d, cos_table, sin_table)
    return python_rope(input_4d, cos_table, sin_table)


def scaled_dot_product_attention(
    q: Sequence[Sequence[float]],
    k: Sequence[Sequence[float]],
    v: Sequence[Sequence[float]],
) -> list[list[float]]:
    """Dispatch scaled dot-product attention to native or Python fallback."""
    q_rows = len(q)
    if should_dispatch("transformer.attention", q_rows):
        return native_scaled_dot_product_attention(q, k, v)
    return python_scaled_dot_product_attention(q, k, v)


__all__ = [
    "native_available",
    "should_dispatch",
    "get_threshold",
    "DISPATCH_THRESHOLDS",
    # Parser
    "token_estimate",
    "batch_token_estimate",
    "python_token_estimate",
    "python_batch_token_estimate",
    "native_token_estimate",
    "native_batch_token_estimate",
    # Vector
    "dot",
    "l2_norm",
    "cosine_similarity",
    "python_dot",
    "python_l2_norm",
    "python_cosine_similarity",
    "native_dot",
    "native_l2_norm",
    "native_cosine_similarity",
    # Transformer
    "matmul",
    "softmax",
    "scaled_dot_product_attention",
    "python_matmul",
    "python_softmax",
    "python_rmsnorm",
    "python_rope",
    "python_scaled_dot_product_attention",
    "native_matmul",
    "native_softmax",
    "native_rmsnorm",
    "native_rope",
    "native_scaled_dot_product_attention",
    "rmsnorm",
    "rope",
]
