"""Fault diagnostics data — governed read-only evidence collection for 星澄.

Provides Xingcheng with grounded fault-investigation capability:

  - Matches a symptom against the authoritative ``fault_code_directory``
    and ``maintenance_manual_directory`` in the sealed governance codex
    (opened read-only/immutable; xingcheng is a registered read audience).
  - Reads main-system runtime state files (boot core, readiness, IPC
    connection state, repair coordination) as read-only evidence.
  - Reads the A195 transactional outbox tail for recent committed state
    transitions.

Boundary: diagnosis is read-only and advisory.  Repair execution stays
with main-system central repair through the governed channel; Xingcheng
reports findings and governed remediation steps only.
"""

from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Final


_CODEX_RELATIVE: Final[tuple[str, ...]] = (
    "governance_rule", "codex", "data", "governance_codex.sqlite3",
)
_STATE_RELATIVE: Final[tuple[str, ...]] = (
    "main-system", "runtime", "state",
)

# Bounded evidence reads — diagnostics must stay cheap enough to run inside
# a chat turn.
_MAX_STATE_JSON_BYTES: Final[int] = 64_000
_MAX_REPAIR_REQUESTS: Final[int] = 5
_MAX_OUTBOX_EVENTS: Final[int] = 10
_MAX_MATCHED_CODES: Final[int] = 5
_MAX_MANUALS: Final[int] = 4

_FAULT_KEYWORDS: Final[tuple[str, ...]] = (
    "故障", "錯誤", "失敗", "異常", "當機", "斷線", "連不上", "連線中斷",
    "啟動失敗", "無法啟動", "啟動不了", "開不起來", "打不開", "不能動",
    "壞掉", "掛掉", "卡住", "沒有回應", "沒反應", "無回應", "查故障",
    "找錯", "排錯", "除錯", "為什麼", "怎麼壞", "哪裡壞", "出問題",
    "error", "failed", "failure", "crash", "down", "cannot connect",
    "not responding", "timeout", "timed out", "disconnect",
)
_UPPER_SNAKE_TOKEN: Final = re.compile(r"[A-Z][A-Z0-9_]{3,}")

# JSON state files Xingcheng may read as diagnostic evidence
# (other_module_data: read-only per access policy).
_RUNTIME_STATE_FILES: Final[tuple[str, ...]] = (
    "boot-core.json",
    "runtime-readiness.json",
    "ipc-connection-state.json",
    "repair-coordination.json",
    "repair-requests.json",
)

# Keys surfaced per state file — bounded, no raw secrets.  State files
# nest their live payload under a top-level "snapshot" object; extraction
# flattens one level so these names match the actual schema.
_BOOT_CORE_KEYS: Final[tuple[str, ...]] = (
    "pid", "backend_pid", "status", "state", "restarts",
    "restart_count", "last_error", "last_exit", "backend_healthy",
    "updated_at",
)
_READINESS_KEYS: Final[tuple[str, ...]] = (
    "overall_ready", "runtime_state", "backend_runtime_ready",
    "governance_ready", "dependencies_ready", "authenticated_ipc_connected",
    "startup_dead", "updated_at",
)
_IPC_STATE_KEYS: Final[tuple[str, ...]] = (
    "backend_process_alive", "backend_http_healthy", "frontend_connected",
    "overall_state", "consecutive_dead", "probe_count", "last_change_at",
    "updated_at",
)

# ------------------------------------------------------------------
# Fault localization vocabulary
# ------------------------------------------------------------------

_MAX_SUSPECTS: Final[int] = 5

# Governed layer names → symptom aliases.  Aliases stay specific enough
# to avoid over-triggering on generic fault keywords.
_LOCATION_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "ipc-channel": (
        "ipc", "websocket", "socket", "連線通道", "通訊通道",
        "連不上", "連線中斷", "斷線", "無法連線",
    ),
    "main-backend": (
        "main-system", "main.py", "後端", "主系統", "backend",
        "主程式", "母工具",
    ),
    "boot-core": (
        "boot-core", "boot_core", "啟動核心", "開機核心",
    ),
    "startup-pipeline": (
        "startup", "啟動管線", "啟動流程", "啟動失敗",
        "無法啟動", "啟動不了", "開不起來", "打不開",
    ),
    "governance": (
        "governance", "法典", "治理", "codex", "治理層",
    ),
    "frontend": (
        "electron", "視窗", "前端", "畫面", "介面", "視窗關閉",
    ),
}

# runtime-readiness.json flag → governed location entity.
_READINESS_FLAG_LOCATIONS: Final[dict[str, str]] = {
    "governance_ready": "governance",
    "dependencies_ready": "dependency-set",
    "authenticated_ipc_connected": "ipc-channel",
    "backend_runtime_ready": "main-backend",
}

# boot-core status values that are themselves fault evidence.
_BOOT_FAULT_STATUSES: Final[frozenset[str]] = frozenset({
    "backend-restarting",
    "restart-budget-exhausted",
    "startup-phase-blocked",
    "spawn-failed",
    "failed",
    "error",
})


def _read_json_bounded(path: Path) -> Any:
    try:
        raw = path.read_bytes()[:_MAX_STATE_JSON_BYTES]
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
