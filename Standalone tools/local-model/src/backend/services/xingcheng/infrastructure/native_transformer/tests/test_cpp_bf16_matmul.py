"""P1-1③ bf16 GEMM kernel 驗收（opt-in XINGCHENG_CPP_CUDA_BF16 路徑）。

合約：row-major C[m,n] = A[m,k] @ B[k,n]；B 為轉置權重以 bf16
device-resident 快取（VRAM 為 fp32 一半、fp64 四分之一）；fp32 累加。
正確性以「bf16 量化輸入之 fp64 精確積」為參照——誤差只剩 fp32 累加
項（scaled |x-y|/(1+|x|) < 0.01）；輸入量化本身造成的誤差為此
opt-in 路徑的既有精度包絡，另以鬆散界限記錄（< 0.25 scaled）。

無 kernels TU／無 CUDA 裝置 → graceful skip（與 stale-pyd skip 同例）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
del _ROOT

import numpy as np
import pytest

from native_transformer import cpp_runtime


def _to_bf16(x: np.ndarray) -> np.ndarray:
    """Round fp64 -> bf16 (round-half-even) -> back as fp64 values."""
    f32 = np.asarray(x, dtype=np.float32)
    u = f32.view(np.uint32)
    r = (u + np.uint32(0x7FFF) + ((u >> np.uint32(16)) & np.uint32(1))) & np.uint32(0xFFFF0000)
    return r.view(np.float32).astype(np.float64)


@pytest.fixture(scope="module")
def ext():
    try:
        mod = cpp_runtime.load_extension()
    except Exception:
        pytest.skip("cpp extension unavailable")
    if not hasattr(mod, "_cuda_bf16_available") or not hasattr(
        mod, "_probe_matmul_bf16"
    ):
        pytest.skip("kernels TU not linked (XINGCHENG_CUDA_KERNELS off)")
    if not mod._cuda_bf16_available():
        pytest.skip("no CUDA device / kernels unavailable")
    return mod


_SHAPES = [(33, 17, 9), (64, 64, 64), (1, 768, 768), (128, 256, 128), (256, 512, 256)]


def test_bf16_matmul_parity_vs_bf16_reference(ext):
    rng = np.random.default_rng(42)
    for m, k, n in _SHAPES:
        a = rng.uniform(-2.0, 2.0, (m, k))
        b = rng.uniform(-2.0, 2.0, (k, n))
        ref = _to_bf16(a) @ _to_bf16(b)
        got = np.asarray(
            ext._probe_matmul_bf16(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
        ).reshape(m, n)
        scaled = np.abs(got - ref) / (1.0 + np.abs(ref))
        assert float(scaled.max()) < 0.01, (
            f"m={m} k={k} n={n} scaled err {scaled.max():.6f}"
        )


def test_bf16_precision_envelope_vs_fp64(ext):
    """bf16 量化輸入的既有精度包絡：與真 fp64 參考的鬆散上界。"""
    rng = np.random.default_rng(7)
    for m, k, n in _SHAPES:
        a = rng.uniform(-2.0, 2.0, (m, k))
        b = rng.uniform(-2.0, 2.0, (k, n))
        ref = a @ b
        got = np.asarray(
            ext._probe_matmul_bf16(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
        ).reshape(m, n)
        scaled = np.abs(got - ref) / (1.0 + np.abs(ref))
        assert float(scaled.max()) < 0.25


def test_bf16_deterministic_and_weight_cache(ext):
    rng = np.random.default_rng(9)
    m, k, n = 32, 48, 24
    a = rng.uniform(-1.0, 1.0, (m, k))
    b = rng.uniform(-1.0, 1.0, (k, n))
    first = ext._probe_matmul_bf16(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
    second = ext._probe_matmul_bf16(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
    assert first == second  # cached bf16 weight path, bit-identical
