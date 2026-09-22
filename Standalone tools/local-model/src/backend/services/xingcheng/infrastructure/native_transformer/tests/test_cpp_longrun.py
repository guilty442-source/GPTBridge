"""P1-1① 長時驗證 harness（窗口型；預設僅 smoke）。

合約：引擎在持續推論負載下——
  1. 數值不漂移：固定 prompt 的 logits scaled diff 相對首測基線 < 1e-9
     （同組態 kernel 具決定性；CUDA/bf16/fp8 路徑同此界）。
  2. 無記憶體洩漏：尾段 RSS 均值相對前段增幅 ≤ 32MB（allocator 噪聲餘量）。
  3. 無退化輸出：logits 全 finite、generate 不為空、無例外中斷。
  4. 吞吐可重現：記錄每輪 tps 序列供證據留存。

執行模式（皆經 env，無 env 即 smoke）：
  XINGCHENG_LONGRUN_SECONDS  目標牆鐘秒數（真實窗口跑 3600+；預設 smoke 只跑
                             XINGCHENG_LONGRUN_ITERS 輪）。
  XINGCHENG_LONGRUN_ITERS    固定輪數（smoke 預設 8；設 SECONDS 時忽略）。
  XINGCHENG_LONGRUN_EVIDENCE 證據 JSON 輸出路徑（每次執行必寫）。
  CUDA／KV／bf16／fp8 路徑由既有 XINGCHENG_CPP_CUDA_* 環境變數決定——
  harness 不自行設定，量測的是操作者指定的組態。

無 extension／無 bundle → graceful skip。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
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
_PARITY_EVERY = 8
_LEAK_BOUND_MB = 32.0
_DRIFT_BOUND = 1e-9


@pytest.fixture(scope="module")
def ext():
    try:
        mod = cpp_runtime.load_extension()
    except Exception:
        pytest.skip("cpp extension unavailable")
    return mod


def test_cpp_longrun(ext, tmp_path):
    if not _BUNDLE.is_dir():
        pytest.skip("bundle unavailable")
    try:
        import psutil
    except ImportError:
        pytest.skip("psutil unavailable")

    seconds = float(os.environ.get("XINGCHENG_LONGRUN_SECONDS", "0") or 0)
    iters = int(os.environ.get("XINGCHENG_LONGRUN_ITERS", "8") or 8)
    evidence_path = Path(
        os.environ.get("XINGCHENG_LONGRUN_EVIDENCE")
        or (tmp_path / "longrun-evidence.json")
    )

    eng = ext.NativeInferenceEngine()
    eng.load(str(_BUNDLE))
    cfg = ext.SamplingConfig()
    proc = psutil.Process()

    baseline = eng.logits(_IDS)
    rss_mb: list[float] = []
    tps: list[float] = []
    drifts: list[float] = []
    failures: list[str] = []
    started = time.perf_counter()
    iteration = 0
    try:
        while True:
            iteration += 1
            t0 = time.perf_counter()
            out = eng.generate(_IDS, 8, cfg)
            dt = time.perf_counter() - t0
            if not out:
                failures.append(f"iter {iteration}: empty generate")
            tps.append(len(out) / dt if dt > 0 else 0.0)
            rss_mb.append(proc.memory_info().rss / (1024 * 1024))
            if iteration % _PARITY_EVERY == 0:
                cur = eng.logits(_IDS)
                if any(not math.isfinite(v) for v in cur):
                    failures.append(f"iter {iteration}: non-finite logits")
                    break
                drifts.append(
                    max(abs(a - b) / (1.0 + abs(b)) for a, b in zip(cur, baseline))
                )
            if seconds > 0:
                if time.perf_counter() - started >= seconds:
                    break
            elif iteration >= iters:
                break
    finally:
        eng.unload()

    elapsed = time.perf_counter() - started
    head = rss_mb[: max(1, len(rss_mb) // 5)] or rss_mb[:1]
    tail = rss_mb[-max(1, len(rss_mb) // 5) :] or rss_mb[-1:]
    rss_growth = (sum(tail) / len(tail)) - (sum(head) / len(head))

    evidence = {
        "schema": "p1-1-longrun/v1",
        "bundle": _BUNDLE.name,
        "env": {
            k: os.environ.get(k)
            for k in (
                "XINGCHENG_CPP_CUDA",
                "XINGCHENG_CPP_CUDA_KV",
                "XINGCHENG_CPP_CUDA_BF16",
                "XINGCHENG_CPP_CUDA_FP8",
                "XINGCHENG_CPP_KV_INT8",
            )
        },
        "iterations": iteration,
        "elapsed_s": round(elapsed, 3),
        "tokens_generated": sum(1 for _ in ()) or iteration * 8,
        "tps": {
            "min": round(min(tps), 3) if tps else 0.0,
            "mean": round(sum(tps) / len(tps), 3) if tps else 0.0,
            "max": round(max(tps), 3) if tps else 0.0,
        },
        "rss_mb": {
            "first": round(rss_mb[0], 1) if rss_mb else 0.0,
            "last": round(rss_mb[-1], 1) if rss_mb else 0.0,
            "peak": round(max(rss_mb), 1) if rss_mb else 0.0,
            "tail_minus_head_mean": round(rss_growth, 2),
        },
        "drift_scaled": {
            "checks": len(drifts),
            "max": max(drifts) if drifts else 0.0,
        },
        "failures": failures,
        "bounds": {
            "drift_scaled": _DRIFT_BOUND,
            "rss_growth_mb": _LEAK_BOUND_MB,
        },
    }
    evidence["passed"] = (
        not failures
        and all(d < _DRIFT_BOUND for d in drifts)
        and rss_growth <= _LEAK_BOUND_MB
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert not failures, failures
    assert all(d < _DRIFT_BOUND for d in drifts), f"drift {max(drifts):.3e}"
    assert rss_growth <= _LEAK_BOUND_MB, f"rss growth {rss_growth:.1f}MB"
