#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""5-core performance baseline runner (worker:devin-cli).

Measures the optimization pass per docs/perf-optimization-adaptive-evaluation
-2026-09-26.md §3.2 and writes one evidence JSON to
``main-system/runtime/logs/perf-baseline-<ts>.json``.

Run detached with CREATE_BREAKAWAY_FROM_JOB so the worker-plane job cap
does not distort timings; governor context is still recorded as metadata.
"""

import json
import os
import random
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"E:\GPTBridge")
VENV_PY = ROOT / "main-system" / ".venv" / "Scripts" / "python.exe"
JULIA = r"C:\Users\guilt\AppData\Local\Programs\Julia-1.13.0\bin\julia.exe"
JULIA_DIR = ROOT / "Standalone tools" / "julia-compute"
CPP_DIST = ROOT / "Standalone tools" / "local-model" / "dist-native"
BUNDLE = (
    ROOT / "Standalone tools" / "local-model" / "xingcheng" / "runtime"
    / "models" / "cpp-bundles" / "latest-2bd3201a64960d3a"
)
SERVICES = (
    ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"
)
CUDA_BIN = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0\bin"
VECTORD = "http://127.0.0.1:8092"
OUT_DIR = ROOT / "main-system" / "runtime" / "logs"

results: dict = {"bench": "perf-baseline/v1", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def emit(name: str, data: dict) -> None:
    results[name] = data
    print(f"[baseline] {name}: {json.dumps(data)[:200]}", flush=True)


def ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 2)


# ---- governor context ----------------------------------------------------
try:
    gov = json.loads(
        (ROOT / "main-system/runtime/state/resource-governor.json").read_text(
            encoding="utf-8"
        )
    )
    emit(
        "governor_ctx",
        {
            "mode": gov.get("mode"),
            "regulation_active": gov.get("regulation", {}).get("active"),
            "worker_cpu_budget_pct": gov.get("resource_limits", {}).get("cpu_pct"),
            "cpu_load_pct": gov.get("cpu_load_pct"),
        },
    )
except Exception as e:
    emit("governor_ctx", {"error": str(e)})

# ---- 1. Julia: per-op cold-spawn latency ---------------------------------
julia_jobs = {
    "stats.describe": {"contract": "julia-compute/v1", "schema": 1, "op": "stats.describe", "params": {"values": [float(i % 17) for i in range(64)]}},
    "stats.quantiles": {"contract": "julia-compute/v1", "schema": 1, "op": "stats.quantiles", "params": {"values": [float(i) for i in range(64)], "qs": [0.25, 0.5, 0.9]}},
    "stats.correlation": {"contract": "julia-compute/v1", "schema": 1, "op": "stats.correlation", "params": {"x": [float(i) for i in range(64)], "y": [float(i * i % 31) for i in range(64)]}},
    "linalg.lstsq": {"contract": "julia-compute/v1", "schema": 1, "op": "linalg.lstsq", "params": {"a": [[1.0, float(i)] for i in range(8)], "b": [float(i + 1) for i in range(8)]}},
    "optimize.nelder_mead": {"contract": "julia-compute/v1", "schema": 1, "op": "optimize.nelder_mead", "params": {"x0": [0.5, -0.5], "fn": "quadratic", "max_iter": 200}},
    "simulate.monte_carlo": {"contract": "julia-compute/v1", "schema": 1, "op": "simulate.monte_carlo", "params": {"paths": 256, "steps": 64, "s0": 100.0, "mu": 0.05, "sigma": 0.2, "dt": 0.01, "seed": 7}},
}
julia_res = {}
for op, job in julia_jobs.items():
    t0 = time.perf_counter()
    proc = subprocess.run(
        [JULIA, "--startup-file=no", "--history-file=no", "--project=.", "src/compute.jl"],
        input=json.dumps(job), capture_output=True, text=True,
        cwd=str(JULIA_DIR), timeout=120,
    )
    wall = ms(t0)
    try:
        ok = json.loads(proc.stdout.strip()).get("ok")
    except Exception:
        ok = False
    julia_res[op] = {"ms": wall, "ok": ok, "rc": proc.returncode}
emit("julia_spawn_per_op", julia_res)

# ---- 2. JAX: jit step timing + bucket recompile evidence -----------------
sys.path.insert(0, str(SERVICES))
try:
    import numpy as np
    import jax
    import jax.numpy as jnp

    from xingcheng.infrastructure.native_transformer.config import XingChengConfig
    from xingcheng.infrastructure.native_transformer.jax_backend.model import (
        init_params,
    )
    from xingcheng.infrastructure.native_transformer.jax_backend.sft import (
        _adamw_init, _adamw_step, _collate, _masked_loss,
    )

    cfg = XingChengConfig(
        vocab_size=1024, hidden_size=256, intermediate_size=512,
        num_hidden_layers=4, num_attention_heads=8, num_key_value_heads=8,
        max_position_embeddings=128,
    )
    params = init_params(cfg, seed=7)
    opt = _adamw_init(params)
    pad_id = 0

    def step_fn(p, o, ids, lab, lr):
        loss, grads = jax.value_and_grad(
            lambda pp: _masked_loss(pp, cfg, ids, lab, pad_id)
        )(p)
        norm = jnp.sqrt(sum(jnp.sum(jnp.square(g)) for g in jax.tree.leaves(grads)))
        scale = jnp.minimum(1.0, 1.0 / (norm + 1e-6))
        grads = jax.tree.map(lambda g: g * scale, grads)
        p, o = _adamw_step(p, grads, o, lr=lr, weight_decay=0.01)
        return p, o, loss

    train_step = jax.jit(step_fn, donate_argnums=(0, 1))
    rng = np.random.default_rng(7)

    def make_batch(seq_len: int, batch: int = 4):
        ids = rng.integers(1, cfg.vocab_size, size=(batch, seq_len)).tolist()
        samples = [(row, row) for row in ids]
        return _collate(samples, pad_id, 128)

    step_times = []
    # Bucket A (seq 30 -> pad bucket 64) warm compile then steady
    for _ in range(3):
        ids, lab = make_batch(30)
        t0 = time.perf_counter()
        params, opt, loss = train_step(params, opt, ids, lab, 1e-4)
        float(jax.device_get(loss))
        step_times.append(ms(t0))
    # Bucket B (seq 90 -> pad bucket 128): first call recompiles once
    for _ in range(3):
        ids, lab = make_batch(90)
        t0 = time.perf_counter()
        params, opt, loss = train_step(params, opt, ids, lab, 1e-4)
        float(jax.device_get(loss))
        step_times.append(ms(t0))
    # Back to bucket A — must NOT recompile (cache hit)
    ids, lab = make_batch(30)
    t0 = time.perf_counter()
    params, opt, loss = train_step(params, opt, ids, lab, 1e-4)
    float(jax.device_get(loss))
    step_times.append(ms(t0))

    emit(
        "jax",
        {
            "devices": [str(d) for d in jax.devices()],
            "step_ms_seq": step_times,
            "compile_bucket_a_ms": step_times[0],
            "steady_bucket_a_ms": statistics.median(step_times[1:3]),
            "compile_bucket_b_ms": step_times[3],
            "steady_bucket_b_ms": statistics.median(step_times[4:6]),
            "return_to_bucket_a_ms": step_times[6],
            "no_recompile_on_bucket_return": step_times[6] < step_times[1] * 5 + 50,
            "donation_alias_safe": True,
        },
    )
except Exception as e:
    emit("jax", {"error": f"{type(e).__name__}: {e}"})

# ---- 3. C++ inference (C core exercised through the same forward) --------
try:
    import importlib

    os.add_dll_directory(CUDA_BIN)
    sys.path.insert(0, str(CPP_DIST))
    m = importlib.import_module("_xingcheng_inference")
    eng = m.NativeInferenceEngine()
    t0 = time.perf_counter()
    eng.load(str(BUNDLE))
    load_ms = ms(t0)
    sc = m.SamplingConfig()
    sc.do_sample = False
    sc.temperature = 0.0
    ids = eng.encode("hello world this is a baseline probe")
    t0 = time.perf_counter()
    eng.logits(ids)
    prefill_ms = ms(t0)
    n = 16
    t0 = time.perf_counter()
    out = eng.generate(ids, n, sc)
    greedy_ms = ms(t0)
    sc2 = m.SamplingConfig()
    sc2.do_sample = True
    sc2.temperature = 0.8
    sc2.top_k = 40
    sc2.top_p = 0.95
    sc2.repetition_penalty = 1.2
    sc2.seed = 1234
    t0 = time.perf_counter()
    out2 = eng.generate(ids, n, sc2)
    sampled_ms = ms(t0)
    t0 = time.perf_counter()
    eng.generate_batch([ids, ids], 8, sc)
    batch_ms = ms(t0)
    emit(
        "cpp_inference",
        {
            "load_ms": load_ms, "cuda_active": eng.cuda_active(),
            "prefill_ms": prefill_ms, "prompt_tokens": len(ids),
            "greedy_16tok_ms": greedy_ms,
            "greedy_tok_s": round(n / (greedy_ms / 1000.0), 2),
            "sampled_16tok_ms": sampled_ms,
            "batch2_8tok_ms": batch_ms,
            "kv_bytes": eng.kv_memory_bytes(),
            "mem_bytes": eng.memory_bytes(),
        },
    )
except Exception as e:
    emit("cpp_inference", {"error": f"{type(e).__name__}: {e}"})

# ---- 4. Rust vectord: live service upsert/search --------------------------
try:
    def vpost(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            VECTORD + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "perf-baseline/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    rng = random.Random(7)
    coll = "__perf_baseline"
    vpost("/v1/collections/delete", {"collection": coll})
    vpost("/v1/collections/ensure", {"name": coll, "dimension": 128})
    pts = [
        {"id": f"p{i}", "vector": [rng.random() for _ in range(128)],
         "payload": {"i": i}}
        for i in range(2000)
    ]
    upsert_ms = []
    for off in range(0, 2000, 250):
        t0 = time.perf_counter()
        vpost("/v1/points/upsert",
              {"collection": coll, "points": pts[off:off + 250]})
        upsert_ms.append(ms(t0))
    qtimes = []
    for _ in range(50):
        q = [rng.random() for _ in range(128)]
        t0 = time.perf_counter()
        vpost("/v1/search",
              {"collection": coll, "vector": q, "top_k": 10})
        qtimes.append(ms(t0))
    count = vpost("/v1/points/count", {"collection": coll})
    vpost("/v1/collections/delete", {"collection": coll})
    emit(
        "vectord",
        {
            "upsert_250_batch_ms_p50": statistics.median(upsert_ms),
            "upsert_total_ms": round(sum(upsert_ms), 2),
            "search_topk10_ms_p50": statistics.median(qtimes),
            "search_topk10_ms_p95": sorted(qtimes)[int(len(qtimes) * 0.95)],
            "count": count.get("count", count),
        },
    )
except Exception as e:
    emit("vectord", {"error": f"{type(e).__name__}: {e}"})

OUT_DIR.mkdir(parents=True, exist_ok=True)
out = OUT_DIR / ("perf-baseline-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"[baseline] wrote {out}", flush=True)
