"""P1-1④ 生產啟用評估 harness（窗口型；腳本，非 pytest 收集項）。

對 `XINGCHENG_CPP_RUNTIME` 啟用前所需的證據矩陣：每個加速組態
載入引擎 → 固定 prompt logits parity（vs CPU fp64 基線）→ greedy
generate 決定性對照 → 定速 generate 取 tps → RSS → unload。

組態（順序即報告順序）：
  cpu         基線（無 env）
  cuda        XINGCHENG_CPP_CUDA=1（cuBLAS f64 GEMM）
  cuda-kv     CUDA + KV=1（device-resident KV＋線上 softmax 注意力）
  cuda-bf16   CUDA + BF16=1（bf16 GEMM＋device 權重快取）
  cuda-fp8    CUDA + FP8=1（e4m3 權重儲存＋fp32 累加）

另驗證 fail-closed 契約：BF16=1+FP8=1 → CUDA_PRECISION_CONFLICT；
KV=1 無 CUDA → CUDA_KV_UNAVAILABLE。

用法：
  python cpp_production_eval.py [--evidence PATH] [--gen-tokens N] [--bench-tokens N]

證據 JSON（`p1-1-production-eval/v1`）預設寫入
governance_rule/execution/audit/convergence/p1-1-production-eval-<date>.json。

無 extension／bundle／CUDA 裝置時對應組態記 skipped（不 fail）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_WSROOT = Path(__file__).resolve().parents[9]
_SHARED = _WSROOT / "shared-layer" / "src"
for _p in (_WSROOT, _SHARED):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
del _ROOT, _SHARED, _WSROOT, _p

from native_transformer import cpp_runtime  # noqa: E402

_BUNDLE = (
    Path(__file__).resolve().parents[7]
    / "xingcheng"
    / "runtime"
    / "models"
    / "cpp-bundles"
    / "final-2611ce99f5f73f79"
)
_IDS = [1, 2, 3, 42, 100, 999, 7]
_ENV_KEYS = (
    "XINGCHENG_CPP_CUDA",
    "XINGCHENG_CPP_CUDA_KV",
    "XINGCHENG_CPP_CUDA_BF16",
    "XINGCHENG_CPP_CUDA_FP8",
    "XINGCHENG_CPP_KV_INT8",
)
_CONFIGS = [
    ("cpu", {}),
    ("cuda", {"XINGCHENG_CPP_CUDA": "1"}),
    ("cuda-kv", {"XINGCHENG_CPP_CUDA": "1", "XINGCHENG_CPP_CUDA_KV": "1"}),
    ("cuda-bf16", {"XINGCHENG_CPP_CUDA": "1", "XINGCHENG_CPP_CUDA_BF16": "1"}),
    ("cuda-fp8", {"XINGCHENG_CPP_CUDA": "1", "XINGCHENG_CPP_CUDA_FP8": "1"}),
]


def _scaled_diff(a, b):
    return max(abs(x - y) / (1.0 + abs(y)) for x, y in zip(a, b))


def _run_config(ext, name, env, gen_tokens, bench_tokens, metrics):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(env)
    try:
        eng = ext.NativeInferenceEngine()
        eng.load(str(_BUNDLE))
    except Exception as exc:  # fail-closed surfaces here
        return {"config": name, "status": "load-failed", "error": str(exc)[:200]}
    try:
        logits = eng.logits(_IDS)
        finite = all(math.isfinite(v) for v in logits)
        cfg = ext.SamplingConfig()
        gen = eng.generate(_IDS, gen_tokens, cfg)
        t0 = time.perf_counter()
        bench = eng.generate(_IDS, bench_tokens, cfg)
        dt = time.perf_counter() - t0
        rss = (
            metrics.process_working_set_bytes(os.getpid()) / (1024 * 1024)
            if metrics
            else None
        )
        return {
            "config": name,
            "status": "ok" if finite else "non-finite-logits",
            "logits_finite": finite,
            "logits": logits,
            "gen_tokens": gen,
            "bench_tps": round(len(bench) / dt, 3) if dt > 0 else 0.0,
            "rss_mb": round(rss, 1) if rss is not None else None,
        }
    except Exception as exc:
        return {"config": name, "status": "run-failed", "error": str(exc)[:200]}
    finally:
        try:
            eng.unload()
        except Exception:
            pass


def _check_fail_closed(ext):
    checks = {}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(
        {
            "XINGCHENG_CPP_CUDA": "1",
            "XINGCHENG_CPP_CUDA_BF16": "1",
            "XINGCHENG_CPP_CUDA_FP8": "1",
        }
    )
    try:
        ext.NativeInferenceEngine().load(str(_BUNDLE))
        checks["bf16_fp8_mutex"] = "FAIL: no error"
    except RuntimeError as exc:
        checks["bf16_fp8_mutex"] = (
            "ok" if "CUDA_PRECISION_CONFLICT" in str(exc) else f"unexpected:{exc}"
        )
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["XINGCHENG_CPP_CUDA_KV"] = "1"
    try:
        ext.NativeInferenceEngine().load(str(_BUNDLE))
        checks["kv_without_cuda"] = "FAIL: no error"
    except RuntimeError as exc:
        checks["kv_without_cuda"] = (
            "ok" if "CUDA_KV_UNAVAILABLE" in str(exc) else f"unexpected:{exc}"
        )
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--gen-tokens", type=int, default=16)
    ap.add_argument("--bench-tokens", type=int, default=24)
    args = ap.parse_args()

    ext = cpp_runtime.load_extension()
    if not _BUNDLE.is_dir():
        print("bundle unavailable; nothing to do")
        return 2
    from shared_layer.performance import process_metrics

    metrics = process_metrics if process_metrics.metrics_available() else None

    results = [
        _run_config(ext, n, e, args.gen_tokens, args.bench_tokens, metrics)
        for n, e in _CONFIGS
    ]
    fail_closed = _check_fail_closed(ext)

    base = next((r for r in results if r["config"] == "cpu"), None)
    base_logits = base.get("logits") if base else None
    base_gen = base.get("gen_tokens") if base else None
    base_tps = base.get("bench_tps") if base else None

    entries = []
    for r in results:
        entry = {
            k: v for k, v in r.items() if k not in ("logits", "gen_tokens")
        }
        if r.get("status") == "ok" and base_logits and r.get("logits"):
            entry["parity_scaled_vs_cpu"] = _scaled_diff(r["logits"], base_logits)
        if r.get("gen_tokens") is not None and base_gen is not None:
            entry["greedy_tokens_equal_cpu"] = r["gen_tokens"] == base_gen
        if base_tps and r.get("bench_tps"):
            entry["tps_ratio_vs_cpu"] = round(r["bench_tps"] / base_tps, 3)
        entries.append(entry)

    report = {
        "schema": "p1-1-production-eval/v1",
        "bundle": _BUNDLE.name,
        "gen_tokens": args.gen_tokens,
        "bench_tokens": args.bench_tokens,
        "fail_closed": fail_closed,
        "configs": entries,
    }
    ok = (
        all(v == "ok" for v in fail_closed.values())
        and all(
            e["status"] == "ok" for e in entries if e["config"] == "cpu"
        )
        and all(e.get("logits_finite", True) for e in entries)
    )
    report["all_ok"] = ok

    out = Path(args.evidence) if args.evidence else (
        Path(__file__).resolve().parents[9]
        / "governance_rule"
        / "execution"
        / "audit"
        / "convergence"
        / f"p1-1-production-eval-{time.strftime('%Y%m%d', time.gmtime())}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + chr(10),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"evidence -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
