"""10.64-2 long-duration engine/GPU auto-release validation harness.

Standalone process (never touches the running INT-10 backend):
load the real pinned checkpoint through ``native_engine_for`` (which
registers the engine with ``AutoReleaseManager``), generate once, then
stop touching it and sample RSS/VRAM until the manager evicts the
idle engine. Repeat for N cycles and measure return-to-baseline.

Usage:
    python scripts/soak-10_64-engine-release.py [--cycles 2] [--idle 300]
        [--settle 60] [--out PATH]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "Standalone tools/local-model/src/backend/services"
sys.path.insert(0, str(SERVICES))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer/src"))  # gpu_coordinator import path

import torch  # noqa: E402

from shared_layer.performance import process_metrics  # noqa: E402

from xingcheng.infrastructure.native_engine import (  # noqa: E402
    _engine_cache,
    native_engine_for,
)
from xingcheng.infrastructure.native_transformer.execution.auto_release import (  # noqa: E402
    get_manager,
)

PROMPT = "你是誰？請簡短介紹自己。"


def _sample(pid: int) -> dict:
    gc.collect()
    row = {
        "t": round(time.time(), 1),
        "rss_mb": round(
            process_metrics.process_working_set_bytes(pid) / 1e6, 1
        ),
        "engine_cached": bool(_engine_cache),
    }
    if torch.cuda.is_available():
        row["vram_alloc_mb"] = round(torch.cuda.memory_allocated() / 1e6, 1)
        row["vram_reserved_mb"] = round(torch.cuda.memory_reserved() / 1e6, 1)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--idle", type=int, default=300,
                        help="auto-release idle seconds (production default 300)")
    parser.add_argument("--settle", type=int, default=60,
                        help="post-release settle sampling seconds")
    parser.add_argument("--max-minutes", type=float, default=40.0)
    parser.add_argument("--out", default=str(
        ROOT / "governance_rule/execution/audit/convergence/10_64-engine-release.json"))
    args = parser.parse_args()

    pid = os.getpid()
    deadline = time.time() + args.max_minutes * 60
    mgr = get_manager()
    mgr.idle = args.idle

    samples: list[dict] = []
    events: list[dict] = []

    def snap(tag: str) -> dict:
        row = _sample(pid)
        row["phase"] = tag
        samples.append(row)
        return row

    baseline = snap("baseline")
    cycles = []

    for i in range(args.cycles):
        if time.time() > deadline:
            events.append({"event": "deadline", "cycle": i})
            break
        engine = native_engine_for()
        loaded = snap(f"cycle{i}-loaded")
        out = engine.generate(prompt=PROMPT, max_tokens=8)
        events.append({
            "event": "generate",
            "cycle": i,
            "ok": bool(out.get("ok")),
            "error": out.get("error_code") or "",
        })
        del engine

        # wait for idle eviction (idle + check interval)
        released_at = None
        while time.time() < deadline:
            time.sleep(15)
            row = snap(f"cycle{i}-waiting")
            if not row["engine_cached"]:
                released_at = row["t"]
                events.append({"event": "released", "cycle": i, "t": row["t"]})
                break
        if released_at is None:
            events.append({"event": "release-timeout", "cycle": i})
            break

        settle_end = time.time() + args.settle
        while time.time() < settle_end:
            time.sleep(10)
            snap(f"cycle{i}-settle")

        settled = samples[-1]
        cycles.append({
            "cycle": i,
            "baseline_rss_mb": baseline["rss_mb"],
            "loaded_rss_mb": loaded["rss_mb"],
            "released_rss_mb": settled["rss_mb"],
            "baseline_vram_mb": baseline.get("vram_alloc_mb"),
            "loaded_vram_mb": loaded.get("vram_alloc_mb"),
            "released_vram_mb": settled.get("vram_alloc_mb"),
            "released_vram_reserved_mb": settled.get("vram_reserved_mb"),
        })

    mgr.shutdown()
    report = {
        "acceptance": "§10.64 ② engine/GPU idle auto-release long-duration validation",
        "cycles": cycles,
        "events": events,
        "samples": samples,
        "cuda": torch.cuda.is_available(),
        "passed": bool(cycles) and all(
            c["released_rss_mb"] < c["loaded_rss_mb"] for c in cycles
        ),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "cycles": cycles}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
