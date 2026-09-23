"""§10.11 perf-baseline periodic collector.

Collects a ``star-perf-baseline/v1`` snapshot on a bounded interval and
injects the subsystem measurements each holder owns — currently the IPC
command-latency ledger (p50/p95/p99). Registered as a governed periodic
flow (automation_core → PeriodicScheduler fallback), pausable under
resource regulation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_logger = logging.getLogger("gptbridge.perf_baseline")

DEFAULT_INTERVAL_S = 300.0


def _ipc_metrics() -> dict[str, Any] | None:
    try:
        from ipc.latency_ledger import snapshot

        snap = snapshot()
        return snap if snap.get("samples") else None
    except Exception:
        return None


def _workload_lane_metrics(app: Any) -> dict[str, Any] | None:
    try:
        from shared_layer.database.workload_lanes import get_lane_pool

        return get_lane_pool().stats()
    except Exception:
        return None


def _rag_metrics(app: Any) -> dict[str, Any] | None:
    """RAG stage-latency surface — only when the lazy RAG runtime was
    actually started (A586: never start RAG just to measure it)."""
    if (
        getattr(app, "rag_runtime", None) is None
        and getattr(app, "rag_orchestrator", None) is None
    ):
        return None
    try:
        from core_system.rag.observability import RAG_METRICS

        return RAG_METRICS.snapshot()
    except Exception:
        return None


def _observe_adaptive_plane() -> None:
    """P4 adaptive plane 第二訊號生產者：系統 CPU／RAM 水位。

    欄位級合併（observe_merge）——本生產者僅擁有 cpu_pct／ram_pct，
    不覆寫 maintenance controller 的 pg／lock／backlog 量測。
    失敗靜默：量測只是提示，不得影響快照主流程。
    """
    try:
        from shared_layer.performance import process_metrics
        from shared_layer.adaptive import LoadSignals, get_plane

        ram_pct = process_metrics.virtual_memory_percent()
        cpu_pct = process_metrics.cpu_percent(interval=None)
        get_plane().observe_merge(
            LoadSignals(
                cpu_pct=float(cpu_pct if cpu_pct >= 0 else 0.0),
                ram_pct=float(ram_pct if ram_pct is not None else 0.0),
            ),
            fields=("cpu_pct", "ram_pct"),
        )
    except Exception:
        pass


def _persist_latest(project_root: Any, snapshot: dict[str, Any]) -> None:
    """Refresh only the rolling ``latest`` pointer — a 5-minute cadence
    must not accumulate one timestamped snapshot per run (~288/day)."""
    import json
    from pathlib import Path

    state_dir = (
        Path(project_root) / "main-system" / "runtime" / "state"
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    tmp = state_dir / "perf-baseline-latest.tmp"
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(state_dir / "perf-baseline-latest.json")


def build_tick(app: Any):
    """Return an async tick collecting + persisting one baseline."""

    async def tick() -> None:
        from tasks.perf_baseline_snapshot import collect_baseline

        project_root = getattr(app, "project_root", None)
        if project_root is None:
            return
        snapshot = await asyncio.to_thread(
            collect_baseline, project_root
        )
        snapshot["ipc"] = _ipc_metrics()
        snapshot["workload_lanes"] = _workload_lane_metrics(app)
        snapshot["rag"] = _rag_metrics(app)
        await asyncio.to_thread(_persist_latest, project_root, snapshot)
        _observe_adaptive_plane()

    return tick


def register(app: Any, *, interval_s: float = DEFAULT_INTERVAL_S) -> bool:
    """Register the baseline collector through the governed flow surface.

    ``automation_core.register_flow`` is the single registration point
    (§1.1); the shared ``PeriodicScheduler`` is the fallback when no core
    exists. Returns True when a governed path accepted the job.
    """
    tick = build_tick(app)
    core = getattr(app, "automation_core", None)
    if core is not None:
        # 被拒（unlisted／kill switch）時不得改走私有迴圈——回傳
        # 註冊結果，由呼叫端留審計。
        return bool(
            core.register_flow(
                "perf-baseline", tick, interval_s=interval_s
            )
        )
    scheduler = getattr(app, "periodic_scheduler", None)
    if scheduler is not None:
        scheduler.register(
            "perf-baseline", interval_s, tick, pausable=True
        )
        return True
    return False


__all__ = ["DEFAULT_INTERVAL_S", "build_tick", "register"]
