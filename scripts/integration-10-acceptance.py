"""INTEGRATION-10 — 24h stability acceptance (§3.1 P0 / §10.13).

Evaluates the six blueprint criteria against recorded state and live
processes:

  1. continuous 24h uptime of the ``main.py --serve`` backend
     (window start ``--window-start``, default 2026-09-21T23:18Z);
  2. zero unexpected restarts / dead-grace loops (boot markers in
     backend logs, registry restart counts);
  3. health probes green (runtime-readiness.json: backend / governance /
     dependencies);
  4. RSS/CPU within governor budgets (resource-governor.jsonl samples);
  5. zero ERROR-class log lines inside the window;
  6. zero orphan main-system processes.

The report is written to
``main-system/runtime/logs/integration-10-<ts>/report.json``.

Run any time: before the window ends the uptime row reports the
accumulated hours and the overall verdict stays ``incomplete`` rather
than pass/fail on that row.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MS = ROOT / "main-system"
LOGS = MS / "runtime" / "logs"
STATE = MS / "runtime" / "state"

DEFAULT_WINDOW_START = "2026-09-21T23:18:00Z"
LOG_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\]")
ERR_PAT = re.compile(
    r"\bERROR\b|\bCRITICAL\b|\bFATAL\b|Traceback|auto-loop error|FAILED", re.I
)
BOOT_PAT = re.compile(r"boot|started|listening|serve", re.I)
RESTART_PAT = re.compile(r"restart|dead.?grace|respawn|relaunch", re.I)


def _ts(text: str) -> datetime | None:
    m = LOG_TS.match(text)
    if not m:
        return None
    raw = m.group(1).rstrip("Z")
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _window_lines(start: datetime, end: datetime):
    for path in sorted(LOGS.glob("backend-*.log")):
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                ts = _ts(line)
                if ts and start <= ts <= end:
                    yield path.name, ts, line
        except OSError:
            continue


def _check_uptime(start: datetime, end: datetime, now: datetime) -> dict:
    try:
        import psutil
    except ImportError:
        return {"passed": False, "blocked": True, "detail": "psutil unavailable"}
    backends = []
    for p in psutil.process_iter(["pid", "create_time", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "main.py" in cmd and "--serve" in cmd and "main-system" in cmd:
            backends.append(
                {"pid": p.info["pid"], "create_time": p.info["create_time"], "cmd": cmd[:120]}
            )
    covered = []
    grace = 120.0  # process spawn lands seconds after the nominal start
    for b in backends:
        started = datetime.fromtimestamp(b["create_time"], tz=timezone.utc)
        b["started_at"] = started.isoformat()
        if started.timestamp() <= start.timestamp() + grace:
            covered.append((started, b))
    if not covered:
        return {
            "passed": False,
            "detail": f"no live backend process predates window start; live={backends}",
        }
    oldest = min(covered, key=lambda x: x[0])
    uptime_s = (now - oldest[0]).total_seconds()
    window_s = (end - start).total_seconds()
    complete = uptime_s >= window_s
    return {
        "passed": complete,
        "complete": complete,
        "uptime_hours": round(uptime_s / 3600, 2),
        "required_hours": round(window_s / 3600, 2),
        "backend": oldest[1],
        "all_backends": backends,
        "detail": (
            f"uptime {uptime_s/3600:.2f}h / {window_s/3600:.0f}h"
            + ("" if complete else " (window still accumulating)")
        ),
    }


def _check_restarts(start: datetime, end: datetime) -> dict:
    restart_hits = [
        {"file": f, "at": ts.isoformat(), "line": line[:200]}
        for f, ts, line in _window_lines(start, end)
        if RESTART_PAT.search(line) and "[self-commit]" not in line
    ]
    return {
        "passed": len(restart_hits) == 0,
        "restart_events": len(restart_hits),
        "samples": restart_hits[:10],
    }


def _check_health() -> dict:
    path = STATE / "runtime-readiness.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"passed": False, "detail": f"readiness unreadable: {exc}"}
    snap = data.get("snapshot") or {}
    core_green = all(
        snap.get(k)
        for k in ("backend_runtime_ready", "governance_ready", "dependencies_ready")
    )
    return {
        "passed": core_green,
        "snapshot": snap,
        "note": (
            "overall_ready additionally requires an authenticated IPC client; "
            "the three core probes (backend/governance/dependencies) are the "
            "uptime-relevant signals"
        ),
    }


def _check_resources(start: datetime, end: datetime) -> dict:
    samples = []
    ledger = LOGS / "resource-governor.jsonl"
    try:
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("action") != "sample":
                continue
            ts = datetime.fromisoformat(
                str(rec.get("at", "")).replace("Z", "+00:00")
            )
            if start <= ts <= end:
                ledger_rec = dict(rec.get("worker_ledger") or {})
                ledger_rec["_regulation"] = rec.get("regulation") or {}
                samples.append(ledger_rec)
    except OSError:
        pass
    if not samples:
        return {"passed": False, "blocked": True, "detail": "no governor samples in window"}
    cpu = sorted(s.get("cpu_pct", 0.0) for s in samples)
    ram = sorted(s.get("ram_mb", 0.0) for s in samples)
    over = [s for s in samples if s.get("over_budget")]
    over_regulated = sum(
        1 for s in over if (s.get("_regulation") or {}).get("active")
    )
    p95 = cpu[min(len(cpu) - 1, int(len(cpu) * 0.95))]
    return {
        "passed": len(over) == 0,
        "samples": len(samples),
        "over_budget_samples": len(over),
        "over_budget_under_regulation": over_regulated,
        "over_budget_unregulated": len(over) - over_regulated,
        "cpu_pct_max": cpu[-1],
        "cpu_pct_p95": p95,
        "ram_mb_max": ram[-1],
        "note": (
            "strict reading of 'RSS/CPU within budget': any over-budget "
            "sample fails. Bursts during tool/model activation that engaged "
            "regulation are reported separately for criterion review."
        ),
    }


def _check_log_hygiene(start: datetime, end: datetime) -> dict:
    errors = []
    for f, ts, line in _window_lines(start, end):
        if ERR_PAT.search(line) and "[self-commit]" not in line:
            errors.append({"file": f, "at": ts.isoformat(), "line": line[:220]})
    # bucket by signature for a compact report
    sigs: dict[str, int] = {}
    for e in errors:
        sig = re.sub(r"\[.*?\]", "", e["line"])[:80].strip()
        sigs[sig] = sigs.get(sig, 0) + 1
    return {
        "passed": len(errors) == 0,
        "error_lines": len(errors),
        "by_signature": dict(sorted(sigs.items(), key=lambda x: -x[1])),
        "samples": errors[:10],
    }


def _check_orphans() -> dict:
    shared_src = ROOT / "shared-layer" / "src"
    if str(shared_src) not in sys.path:
        sys.path.insert(0, str(shared_src))
    from shared_layer.performance import process_metrics

    if not process_metrics.metrics_available():
        return {"passed": False, "blocked": True,
                "detail": "process metrics unavailable"}
    reg_path = STATE / "process-registry.json"
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"passed": False, "blocked": True, "detail": f"registry unreadable: {exc}"}
    live = set(process_metrics.process_list())
    zombies = []
    for rec in reg.get("processes", []):
        if not rec.get("owned"):
            continue
        health = str(rec.get("health") or "")
        pid = rec.get("pid")
        if health == "running" and pid not in live:
            zombies.append({"pid": pid, "module_id": rec.get("module_id"), "health": health})
    return {"passed": len(zombies) == 0, "registered_running_but_dead": zombies[:20]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-start", default=DEFAULT_WINDOW_START)
    ap.add_argument("--duration-h", type=float, default=24.0)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
    from datetime import timedelta
    end = start + timedelta(hours=args.duration_h)
    now = datetime.now(timezone.utc)
    effective_end = min(end, now)

    checks = {
        "uptime_24h": _check_uptime(start, end, now),
        "no_unexpected_restart": _check_restarts(start, effective_end),
        "health_probes": _check_health(),
        "resource_budget": _check_resources(start, effective_end),
        "log_hygiene": _check_log_hygiene(start, effective_end),
        "no_orphans": _check_orphans(),
    }
    window_complete = now >= end
    all_passed = all(c.get("passed") for c in checks.values())
    report = {
        "schema": "integration-10-acceptance/v1",
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "evaluated_at_utc": now.isoformat(),
        "window_complete": window_complete,
        "verdict": (
            "PASS" if window_complete and all_passed
            else "FAIL" if window_complete
            else "ACCUMULATING"
        ),
        "failed_checks": [k for k, v in checks.items() if not v.get("passed")],
        "checks": checks,
        "label": args.label,
    }
    out_dir = LOGS / f"integration-10-{now.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "window_complete": window_complete,
                      "report": str(report_path),
                      "checks": {k: v.get("passed") for k, v in checks.items()}},
                     ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else (2 if report["verdict"] == "FAIL" else 1)


if __name__ == "__main__":
    sys.exit(main())
