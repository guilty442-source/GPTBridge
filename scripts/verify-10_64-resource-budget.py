#!/usr/bin/env python3
"""P0 §10.64 ① — worker 總帳 30min p95 驗收驗證器。

讀取 ``main-system/runtime/logs/resource-governor.jsonl`` 的 per-cycle
``sample`` 樣本（``--log-samples`` 產出），計算 CPU / RAM p95 並對照
WORKER_CPU_BUDGET=10% / WORKER_RAM_BUDGET=30% 判定。

用法：
  python scripts/verify-10_64-resource-budget.py
  python scripts/verify-10_64-resource-budget.py --window 90 --json

窗長預設 90（20s 間隔 × 90 = 30min）。若 log 未滿 90 筆則以現有窗計算，
並在報告註明 ``window_full=false``（等待累積）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PROJECT_ROOT / "main-system" / "runtime" / "logs" / "resource-governor.jsonl"
BUDGET_CPU = 10.0
BUDGET_RAM = 30.0
DEFAULT_WINDOW = 90


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = math.ceil(0.95 * len(ordered)) - 1
    k = max(0, min(k, len(ordered) - 1))
    return float(ordered[k])


def load_samples(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    out: list[dict] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("action") == "sample" and "worker_ledger" in obj:
            out.append(obj)
    return out


def verify(window: int = DEFAULT_WINDOW) -> dict:
    samples = load_samples(LOG_PATH)
    total = len(samples)
    sliced = samples[-window:] if total else []
    cpus = [float(s["worker_ledger"]["cpu_pct"]) for s in sliced]
    rams = [float(s["worker_ledger"]["ram_pct"]) for s in sliced]
    cpu_p95 = _p95(cpus)
    ram_p95 = _p95(rams)
    cpu_max = max(cpus) if cpus else None
    ram_max = max(rams) if rams else None
    reg_entered = sum(1 for s in sliced if s.get("regulation", {}).get("active"))
    # also count regulation-entered / released actions in raw log
    raw_actions = 0
    if LOG_PATH.is_file():
        for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
                if o.get("action") in ("regulation-entered", "regulation-released"):
                    raw_actions += 1
            except Exception:
                pass
    cpu_ok = cpu_p95 is not None and cpu_p95 <= BUDGET_CPU
    ram_ok = ram_p95 is not None and ram_p95 <= BUDGET_RAM
    window_full = total >= window
    verdict = "PASS" if (cpu_ok and ram_ok) else "FAIL" if sliced else "INCOMPLETE"
    # if window not full, still PASS when current p95 already under budget,
    # but caller must note window_full=false
    return {
        "log": str(LOG_PATH),
        "budget": {"cpu_pct": BUDGET_CPU, "ram_pct": BUDGET_RAM},
        "window": window,
        "window_full": window_full,
        "total_samples": total,
        "window_samples": len(sliced),
        "cpu_p95": cpu_p95,
        "cpu_max": cpu_max,
        "ram_p95": ram_p95,
        "ram_max": ram_max,
        "regulation_active_in_window": reg_entered,
        "regulation_actions_total": raw_actions,
        "cpu_ok": cpu_ok,
        "ram_ok": ram_ok,
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify §10.64 worker budget p95")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="window size (samples)")
    ap.add_argument("--json", action="store_true", help="output raw JSON")
    args = ap.parse_args(argv)
    report = verify(window=args.window)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # human summary line
        print(f"# verdict={report['verdict']} cpu_p95={report['cpu_p95']} ram_p95={report['ram_p95']} window_full={report['window_full']}")
    return 0 if report["verdict"] == "PASS" else 1 if report["verdict"] == "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
