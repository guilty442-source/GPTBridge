"""Git performance telemetry and slow-operation detection (specs 110-111).

Every ``GitRepository.run`` invocation already records ``duration_ms`` in the
audit ledger.  This module adds aggregations (p50/p95/p99 per operation) and a
low-volume slow-marker file used by the supervisor health surface:

    <common-git>/gptbridge-automation/git-perf-stats.json
    <common-git>/gptbridge-automation/git-perf-slow.jsonl

Design rules:
  - never store full stdout/stderr — only durations and op names;
  - slow markers are advisory only; they never abort a legitimate operation;
  - read/write of the stats file is best-effort and crash-safe (tmp+replace).

Also implements spec 108 ``batch_ref_snapshot`` — a single ``for-each-ref``
call that returns ref metadata (name, oid, upstream, objecttype) in one shot,
replacing per-branch ``rev-parse`` round trips.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

# Slow thresholds in milliseconds (spec 111).
SLOW_THRESHOLDS_MS: dict[str, int] = {
    "status": 1000,
    "commit": 5000,
    "merge": 10000,
    "merge-base": 1000,
    "diff": 1000,
    "log": 1000,
    "rev-parse": 800,
    "for-each-ref": 800,
    "worktree": 1000,
    "audit-append": 500,
    "add": 3000,
}

# Stats window: keep at most this many samples per operation (rolling).
MAX_SAMPLES_PER_OP: int = 2048


def _op_label(command: str) -> str:
    """Map a git command string to a slow-threshold operation label."""
    cmd = command.strip().lower()
    for token in ("worktree list", "worktree"):
        if cmd.startswith(token):
            return "worktree"
    for token in ("merge-base", "merge", "status", "diff", "log",
                  "rev-parse", "for-each-ref", "commit", "add"):
        if cmd.startswith(token):
            return token
    return "other"


def _stats_path(repo_path: Path) -> Path | None:
    candidate = repo_path / ".git"
    if candidate.is_file():
        try:
            raw = candidate.read_text(encoding="utf-8").strip()
            if raw.startswith("gitdir:"):
                git_dir = Path(raw[7:].strip())
                common = git_dir.parent.parent
                return common / "gptbridge-automation" / "git-perf-stats.json"
        except OSError:
            return None
    if candidate.is_dir():
        return candidate / "gptbridge-automation" / "git-perf-stats.json"
    return None


def _slow_path(repo_path: Path) -> Path | None:
    stats = _stats_path(repo_path)
    return stats.with_name("git-perf-slow.jsonl") if stats else None


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = min(n - 1, max(0, int(pct / 100.0 * n)))
    return sorted_values[idx]


def record(repo_path: Path, command: str, duration_ms: int) -> dict[str, Any]:
    """Persist one git operation sample + optional slow marker (best-effort)."""
    path = _stats_path(repo_path)
    if path is None:
        return {}
    op = _op_label(command)
    try:
        state: dict[str, Any] = {}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {"ops": {}, "slow": 0}
        ops = state.setdefault("ops", {})
        samples = ops.setdefault(op, [])
        samples.append(int(duration_ms))
        if len(samples) > MAX_SAMPLES_PER_OP:
            del samples[: len(samples) - MAX_SAMPLES_PER_OP]
        ops[op] = samples
        state["slow"] = int(state.get("slow", 0))
        threshold = SLOW_THRESHOLDS_MS.get(op, 10_000)
        slow_marker = duration_ms >= threshold
        if slow_marker:
            state["slow"] += 1
            slow_path = _slow_path(repo_path)
            if slow_path is not None:
                slow_path.parent.mkdir(parents=True, exist_ok=True)
                marker = {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "command": command[:300],
                    "op": op,
                    "duration_ms": int(duration_ms),
                    "threshold_ms": threshold,
                }
                with slow_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(marker, ensure_ascii=False) + "\n")
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return {"op": op, "slow": slow_marker, "threshold_ms": threshold}
    except OSError:
        return {}


def snapshot(repo_path: Path) -> dict[str, Any]:
    """Aggregate p50/p95/p99 per operation (smallest footprint)."""
    path = _stats_path(repo_path)
    if path is None or not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, Any] = {"slow_total": int(state.get("slow", 0)), "ops": {}}
    for op, samples in (state.get("ops", {}) or {}).items():
        ordered = sorted(float(s) for s in samples)
        result["ops"][op] = {
            "samples": len(ordered),
            "p50_ms": round(_percentile(ordered, 50), 1),
            "p95_ms": round(_percentile(ordered, 95), 1),
            "p99_ms": round(_percentile(ordered, 99), 1),
            "max_ms": round(ordered[-1], 1) if ordered else 0,
        }
    return result


def batch_ref_snapshot(repo) -> list[dict[str, str]]:
    """One ``for-each-ref`` call returning all ref metadata (spec 108).

    Returns [{name, oid, upstream, type}] — replaces per-branch ``rev-parse``
    round trips.  ``repo`` is a ``GitRepository``.
    """
    result = repo.run(
        [
            "for-each-ref",
            "--format=%(refname) %(objectname) %(upstream) %(objecttype)",
        ]
    )
    rows: list[dict[str, str]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split(" ", 3)
        if len(parts) >= 3:
            rows.append(
                {
                    "name": parts[0],
                    "oid": parts[1],
                    "upstream": parts[2],
                    "type": parts[3] if len(parts) > 3 else "",
                }
            )
        elif parts:
            rows.append({"name": parts[0], "oid": "", "upstream": "", "type": ""})
    return rows


__all__ = [
    "MAX_SAMPLES_PER_OP",
    "SLOW_THRESHOLDS_MS",
    "batch_ref_snapshot",
    "record",
    "snapshot",
]