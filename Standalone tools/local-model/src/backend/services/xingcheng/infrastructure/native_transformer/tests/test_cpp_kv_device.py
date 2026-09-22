"""P1-1② device-resident KV 驗收（opt-in XINGCHENG_CPP_CUDA_KV 路徑）。

合約：device KV 為 host paged pool 的 write-through fp64 鏡像
（僅 slot 0）；注意力在 CUDA online-softmax kernel 內完成，
因果界 position_offset+s、tile 掃描——語義與 host 路徑一致，
parity 應近位元級（跨裝置 exp 的 ulp 差異，界 <1e-9 scaled）。

fail-closed：KV=1 無 CUDA=1 → CUDA_KV_UNAVAILABLE；
KV=1 + KV_INT8=1 → CUDA_KV_UNSUPPORTED_CONFIG（int8 無 device
格式，不靜默降為異精度路徑）。

無 kernels TU／無 CUDA 裝置／無 bundle → graceful skip。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
del _ROOT

import pytest

from native_transformer import cpp_runtime

_BUNDLE = (
    Path(__file__).resolve().parents[7]
    / "xingcheng"
    / "runtime"
    / "models"
    / "cpp-bundles"
    / "final-2611ce99f5f73f79"
)
_IDS = [1, 2, 3, 42, 100, 999, 7]


@pytest.fixture(scope="module")
def ext():
    try:
        mod = cpp_runtime.load_extension()
    except Exception:
        pytest.skip("cpp extension unavailable")
    if not mod._cuda_kv_available():
        pytest.skip("no CUDA device / kernels unavailable")
    return mod


@pytest.fixture(autouse=True)
def _clean_env():
    saved = {
        k: os.environ.get(k)
        for k in (
            "XINGCHENG_CPP_CUDA",
            "XINGCHENG_CPP_CUDA_KV",
            "XINGCHENG_CPP_KV_INT8",
        )
    }
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _load(ext):
    eng = ext.NativeInferenceEngine()
    eng.load(str(_BUNDLE))
    return eng


def test_kv_fail_closed_without_cuda(ext):
    if not _BUNDLE.is_dir():
        pytest.skip("bundle unavailable")
    os.environ.pop("XINGCHENG_CPP_CUDA", None)
    os.environ["XINGCHENG_CPP_CUDA_KV"] = "1"
    with pytest.raises(RuntimeError, match="CUDA_KV_UNAVAILABLE"):
        _load(ext)


def test_kv_fail_closed_with_int8(ext):
    if not _BUNDLE.is_dir():
        pytest.skip("bundle unavailable")
    os.environ["XINGCHENG_CPP_CUDA"] = "1"
    os.environ["XINGCHENG_CPP_KV_INT8"] = "1"
    os.environ["XINGCHENG_CPP_CUDA_KV"] = "1"
    with pytest.raises(RuntimeError, match="CUDA_KV_UNSUPPORTED_CONFIG"):
        _load(ext)


def test_kv_device_parity_and_generate(ext):
    """device-KV 注意力 vs host 注意力（同 cuBLAS f64 GEMM）。"""
    if not _BUNDLE.is_dir():
        pytest.skip("bundle unavailable")
    os.environ["XINGCHENG_CPP_CUDA"] = "1"
    os.environ["XINGCHENG_CPP_CUDA_KV"] = "0"
    eng = _load(ext)
    ref = eng.logits(_IDS)
    cfg = ext.SamplingConfig()
    g_ref = eng.generate(_IDS, 4, cfg)
    eng.unload()

    os.environ["XINGCHENG_CPP_CUDA_KV"] = "1"
    eng = _load(ext)
    dev = eng.logits(_IDS)
    g_dev = eng.generate(_IDS, 4, cfg)
    eng.unload()

    scale = max(abs(a - b) / (1.0 + abs(b)) for a, b in zip(ref, dev))
    assert scale < 1e-9, f"scaled diff {scale:.3e}"
    assert g_ref == g_dev  # greedy decode bit-identical
