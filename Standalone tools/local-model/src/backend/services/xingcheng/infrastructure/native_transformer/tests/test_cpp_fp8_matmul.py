"""P1-1③殘 fp8 GEMM kernel 驗收（opt-in XINGCHENG_CPP_CUDA_FP8 路徑）。

合約：row-major C[m,n] = A[m,k] @ B[k,n]；B 為轉置權重以 e4m3
device-resident 快取（VRAM 為 bf16 一半、fp64 八分之一）；A 轉 fp32、
fp32 累加。sm_86 無硬體 fp8——此為權重儲存壓縮的評估路徑，
非吞吐主張。正確性以「B e4m3 量化後之 fp64 精確積」為參照
（scaled err < 0.01）；輸入量化包絡另記（逐列精確上界 max|a|·Σ|Δb|＋5% fp32 鬆量——誤差全數由
權重量化解釋，ref 近零時 scaled 指標失真故不用）。

bf16 與 fp8 互斥精度域——兩者同時請求於 load 時 fail-closed
（CUDA_PRECISION_CONFLICT），非優先序規則。

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


def _fp8_e4m3_table() -> np.ndarray:
    """All non-negative finite e4m3 values (bias 7, S.1111.111=NaN excluded)."""
    vals = [m / 8.0 * 2**-6 for m in range(1, 8)]  # subnormals
    for e in range(-6, 9):
        for m in range(8):
            if e == 8 and m == 7:
                continue  # NaN encoding
            vals.append((1.0 + m / 8.0) * 2**e)
    return np.array(sorted(vals))


_FP8_POS = _fp8_e4m3_table()


def _to_fp8(x: np.ndarray) -> np.ndarray:
    """Round fp64 -> e4m3 (nearest, satfinite) -> back as fp64 values."""
    x = np.asarray(x, dtype=np.float64)
    sign = np.sign(x)
    ax = np.minimum(np.abs(x), _FP8_POS.max())
    idx = np.searchsorted(_FP8_POS, ax)
    lo = np.clip(idx - 1, 0, len(_FP8_POS) - 1)
    hi = np.clip(idx, 0, len(_FP8_POS) - 1)
    pick_hi = np.abs(_FP8_POS[hi] - ax) <= np.abs(ax - _FP8_POS[lo])
    return sign * np.where(pick_hi, _FP8_POS[hi], _FP8_POS[lo])


@pytest.fixture(scope="module")
def ext():
    try:
        mod = cpp_runtime.load_extension()
    except Exception:
        pytest.skip("cpp extension unavailable")
    if not mod._cuda_fp8_available():
        pytest.skip("no CUDA device / kernels unavailable")
    return mod


_SHAPES = [(33, 17, 9), (64, 64, 64), (1, 768, 768), (128, 256, 128)]


def test_fp8_matmul_parity_vs_fp8_reference(ext):
    rng = np.random.default_rng(42)
    for m, k, n in _SHAPES:
        a = rng.uniform(-2.0, 2.0, (m, k)).astype(np.float32).astype(np.float64)
        b = rng.uniform(-2.0, 2.0, (k, n))
        ref = a @ _to_fp8(b)  # A is fp32 on device; fp64 product of fp32 vals is exact-enough reference
        got = np.asarray(
            ext._probe_matmul_fp8(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
        ).reshape(m, n)
        scaled = np.abs(got - ref) / (1.0 + np.abs(ref))
        assert float(scaled.max()) < 0.01, (
            f"m={m} k={k} n={n} scaled err {scaled.max():.6f}"
        )


def test_fp8_precision_envelope_vs_fp64(ext):
    """e4m3 權重量化包絡：誤差須完全落在輸入量化可解釋範圍。

    |got - ref| ≤ max|a|·Σ|b - fp8(b)|（逐列精確上界）＋ fp32 累加
    鬆量——誤差全部源自權重量化而非 kernel 缺陷。"""
    rng = np.random.default_rng(7)
    for m, k, n in _SHAPES:
        a = rng.uniform(-2.0, 2.0, (m, k))
        b = rng.uniform(-2.0, 2.0, (k, n))
        ref = a @ b
        got = np.asarray(
            ext._probe_matmul_fp8(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
        ).reshape(m, n)
        dq = np.abs(b - _to_fp8(b)).sum(axis=0)          # per-output-col quant bound
        bound = float(np.abs(a).max()) * dq * 1.05 + 1e-9  # +5% fp32 slack
        err = np.abs(got - ref)
        assert float((err - bound[None, :]).max()) < 0.0, (
            f'm={m} k={k} n={n}: err exceeds quantisation bound by '
            f'{(err - bound[None, :]).max():.4f}'
        )


def test_fp8_deterministic_and_weight_cache(ext):
    rng = np.random.default_rng(9)
    m, k, n = 32, 48, 24
    a = rng.uniform(-1.0, 1.0, (m, k))
    b = rng.uniform(-1.0, 1.0, (k, n))
    first = ext._probe_matmul_fp8(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
    second = ext._probe_matmul_fp8(a.ravel().tolist(), m, k, b.ravel().tolist(), n)
    assert first == second  # cached fp8 weight path, bit-identical


def test_fp8_bf16_mutually_exclusive(ext):
    """兩精度域同時請求 → load fail-closed，非隱式優先序。"""
    import os

    bundle = (
        Path(__file__).resolve().parents[7]
        / "xingcheng" / "runtime" / "models" / "cpp-bundles"
        / "final-2611ce99f5f73f79"
    )
    if not bundle.is_dir():
        pytest.skip("bundle unavailable")
    saved = {k: os.environ.get(k) for k in
             ("XINGCHENG_CPP_CUDA", "XINGCHENG_CPP_CUDA_BF16",
              "XINGCHENG_CPP_CUDA_FP8")}
    try:
        os.environ["XINGCHENG_CPP_CUDA"] = "1"
        os.environ["XINGCHENG_CPP_CUDA_BF16"] = "1"
        os.environ["XINGCHENG_CPP_CUDA_FP8"] = "1"
        with pytest.raises(RuntimeError, match="CUDA_PRECISION_CONFLICT"):
            ext.NativeInferenceEngine().load(str(bundle))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
