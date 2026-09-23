"""§10.11 全專案效能基準採集（`star-perf-baseline/v1`）。

即刻開始蒐集可量測的基準數據——寫入
``runtime/state/perf-baseline-latest.json`` 與
``runtime/state/perf-baseline-<timestamp>.json`` 快照。

原則：**先量測再優化**；量不到的欄位記 ``None``，不造假。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.perf_baseline")

BASELINE_VERSION = "star-perf-baseline/v1"


def _git_timing(repo_root: Path, args: list[str]) -> Optional[float]:
    """量測一個唯讀 git 指令的耗時（ms）；失敗回 None。"""
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            timeout=30,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (time.monotonic() - started) * 1000.0


def _process_metrics() -> dict[str, Any]:
    """本 process 的資源量測（P24：native 優先，psutil 為殘留 fallback）。"""
    try:
        from shared_layer.performance import process_metrics
    except ImportError:
        return {"metrics": None}
    pid = os.getpid()
    rss = process_metrics.process_working_set_bytes(pid)
    threads = process_metrics.process_num_threads(pid)
    # open_files() enumerates handles and can hard-crash (access violation)
    # on Windows under handle churn; num_handles() gives the same signal
    # without enumeration. POSIX falls back to open_files.
    open_handles = process_metrics.process_num_handles(pid)
    return {
        "rss_mb": round(rss / 1_048_576, 1) if rss >= 0 else None,
        "cpu_percent": process_metrics.process_cpu_percent(pid, interval=0.1),
        "threads": threads if threads >= 0 else None,
        "open_handles": open_handles if open_handles >= 0 else None,
    }


def _gpu_metrics() -> Optional[dict[str, Any]]:
    try:
        from shared_layer.adaptive.gpu_coordinator import query_gpu
    except ImportError:
        return None
    status = query_gpu()
    if status is None:
        return None
    return {
        "total_mb": status.total_mb,
        "used_mb": status.used_mb,
        "free_mb": status.free_mb,
        "utilization": getattr(status, "util_pct", None)
        or getattr(status, "utilization", None),
    }


def _registry_metrics(project_root: Path) -> dict[str, Any]:
    """Process Registry 摘要（§10.10）。"""
    path = (
        project_root / "main-system" / "runtime" / "state"
        / "process-registry.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"registry": None}
    processes = data.get("processes", [])
    return {
        "registered": len(processes),
        "active": sum(
            1
            for p in processes
            if p.get("shutdown_state") not in ("exited", "failed")
        ),
    }


def collect_baseline(
    project_root: str | Path,
    *,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """蒐集一份全系統效能快照。

    ``extra`` 可注入子系統量測（如 IPC 延遲、RAG 階段耗時、
    boot-to-READY 秒數）——由呼叫端在有能力量測時提供。
    """
    root = Path(project_root).resolve()
    snapshot: dict[str, Any] = {
        "baseline_version": BASELINE_VERSION,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project_root": str(root),
        "process": _process_metrics(),
        "gpu": _gpu_metrics(),
        "process_registry": _registry_metrics(root),
        "git": {
            "status_ms": _git_timing(root, ["status", "--porcelain"]),
            "diff_stat_ms": _git_timing(root, ["diff", "--stat", "HEAD"]),
        },
        "workload_lanes": None,   # 由持有 WorkloadLanePool 的呼叫端注入
        "rag": None,              # 由 pipeline 持有者注入
        "ipc": None,              # 由 IPC server 持有者注入
        "extra": extra or {},
    }
    return snapshot


def persist_baseline(
    project_root: str | Path, snapshot: dict[str, Any]
) -> Path:
    """寫快照檔＋更新 latest 指標檔。"""
    state_dir = (
        Path(project_root) / "main-system" / "runtime" / "state"
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["captured_at"].replace(":", "").replace("-", "")
    latest = state_dir / "perf-baseline-latest.json"
    target = state_dir / f"perf-baseline-{stamp}.json"
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    target.write_text(payload + "\n", encoding="utf-8")
    latest.write_text(payload + "\n", encoding="utf-8")
    return target
