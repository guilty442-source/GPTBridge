"""native_kernel — optional C++ acceleration hook for the semantic index.

Per A35 the hybrid stack keeps Python for governed orchestration and C++ for
core compute / native performance.  This module loads a locally-built
``_rag_native.pyd`` (C++ extension) when present and transparently falls back
to pure-Python math otherwise, so the system stays stdlib-only and self-hosting
(A37/E23) while still being able to use the native kernel.
"""

from __future__ import annotations

from typing import Any

_NATIVE: Any | None = None
try:
    from . import _rag_native as _NATIVE  # type: ignore

    _AVAILABLE = True
except ImportError:  # pragma: no cover - local build optional
    _AVAILABLE = False


def available() -> bool:
    return bool(_AVAILABLE)


def dot_vectors(left: list[float], right: list[float]) -> float:
    native = _NATIVE
    if native is not None and hasattr(native, "dot"):
        try:
            return float(native.dot(left, right))
        except Exception:  # pragma: no cover - fall back on any ABI mismatch
            pass
    return float(sum(a * b for a, b in zip(left, right)))


__all__ = ["available", "dot_vectors"]