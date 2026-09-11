"""Fault diagnostics — governed read-only evidence collection for 星澄.

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

# Keys surfaced per state file — bounded, no raw secrets.
_BOOT_CORE_KEYS: Final[tuple[str, ...]] = (
    "pid", "backend_pid", "status", "state", "restarts",
    "restart_count", "last_error", "updated_at",
)
_READINESS_KEYS: Final[tuple[str, ...]] = (
    "overall_ready", "runtime_state", "backend_runtime_ready",
    "governance_ready", "dependencies_ready", "authenticated_ipc_connected",
    "startup_dead", "updated_at",
)
_IPC_STATE_KEYS: Final[tuple[str, ...]] = (
    "active_connections", "connection_count", "authenticated",
    "updated_at", "last_event",
)


def _read_json_bounded(path: Path) -> Any:
    try:
        raw = path.read_bytes()[:_MAX_STATE_JSON_BYTES]
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


class FaultDiagnostics:
    """Read-only fault-diagnosis evidence collector for Xingcheng."""

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self.codex_path = self.project_root.joinpath(*_CODEX_RELATIVE)
        self.state_dir = self.project_root.joinpath(*_STATE_RELATIVE)

    # ------------------------------------------------------------------
    # Fault-question detection
    # ------------------------------------------------------------------

    @staticmethod
    def looks_like_fault(text: str) -> bool:
        """Heuristic gate: does this question ask about a system fault?"""
        candidate = str(text or "")
        if not candidate.strip():
            return False
        lowered = candidate.casefold()
        return any(keyword in lowered or keyword in candidate
                   for keyword in _FAULT_KEYWORDS)

    # ------------------------------------------------------------------
    # Governed directory reads (read-only codex)
    # ------------------------------------------------------------------

    def _codex_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self.codex_path.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )

    def fault_code_directory(self) -> list[dict[str, Any]]:
        """All registered fault codes from the authoritative directory."""
        if not self.codex_path.is_file():
            return []
        try:
            with self._codex_connection() as db:
                rows = db.execute(
                    "SELECT fault_code, canonical_name, domain, severity, "
                    "meaning, trigger, retryability, remediation "
                    "FROM fault_code_directory"
                ).fetchall()
            return [
                {
                    "fault_code": str(row[0]),
                    "canonical_name": str(row[1] or ""),
                    "domain": str(row[2] or ""),
                    "severity": str(row[3] or ""),
                    "meaning": str(row[4] or ""),
                    "trigger": str(row[5] or ""),
                    "retryability": str(row[6] or ""),
                    "remediation": str(row[7] or ""),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []

    def maintenance_manuals(self) -> list[dict[str, Any]]:
        """All governed maintenance-manual rows."""
        if not self.codex_path.is_file():
            return []
        try:
            with self._codex_connection() as db:
                rows = db.execute(
                    "SELECT manual_code, target_entity, applicable_fault_codes, "
                    "diagnosis, preconditions, ordered_steps, verification, "
                    "rollback, stop_conditions, risk_level, required_permission "
                    "FROM maintenance_manual_directory"
                ).fetchall()
            return [
                {
                    "manual_code": str(row[0]),
                    "target_entity": str(row[1] or ""),
                    "applicable_fault_codes": [
                        code
                        for code in str(row[2] or "").split("|")
                        if code
                    ],
                    "diagnosis": str(row[3] or ""),
                    "preconditions": str(row[4] or ""),
                    "ordered_steps": str(row[5] or ""),
                    "verification": str(row[6] or ""),
                    "rollback": str(row[7] or ""),
                    "stop_conditions": str(row[8] or ""),
                    "risk_level": str(row[9] or ""),
                    "required_permission": str(row[10] or ""),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------------
    # Symptom → fault-code matching
    # ------------------------------------------------------------------

    def match_fault_codes(
        self,
        symptom: str,
        *,
        limit: int = _MAX_MATCHED_CODES,
    ) -> list[dict[str, Any]]:
        """Score directory rows against a free-text symptom description."""
        directory = self.fault_code_directory()
        if not directory or not str(symptom or "").strip():
            return []
        text = str(symptom)
        lowered = text.casefold()
        upper_tokens = set(_UPPER_SNAKE_TOKEN.findall(text))
        # Word-ish tokens for substring matching against canonical names
        # and trigger descriptions.
        text_tokens = {
            token
            for token in re.split(r"[^0-9A-Za-z一-鿿]+", lowered)
            if len(token) >= 2
        }

        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in directory:
            score = 0.0
            if entry["fault_code"] in upper_tokens:
                score += 10.0
            canonical_words = set(entry["canonical_name"].split("-"))
            score += 1.5 * len(canonical_words & text_tokens)
            for field in ("meaning", "trigger", "domain"):
                value = str(entry[field]).casefold()
                if not value:
                    continue
                if value in lowered:
                    score += 2.0
                    continue
                score += 0.5 * len(
                    {tok for tok in value.split() if tok} & text_tokens
                )
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1]["fault_code"]))
        return [entry for _score, entry in scored[: max(1, limit)]]

    def manuals_for(
        self,
        fault_codes: list[str],
        *,
        limit: int = _MAX_MANUALS,
    ) -> list[dict[str, Any]]:
        """Maintenance-manual rows whose applicable codes intersect."""
        wanted = {str(code) for code in fault_codes if str(code).strip()}
        if not wanted:
            return []
        return [
            manual
            for manual in self.maintenance_manuals()
            if wanted & set(manual["applicable_fault_codes"])
        ][: max(1, limit)]

    # ------------------------------------------------------------------
    # Runtime evidence (read-only state files + outbox tail)
    # ------------------------------------------------------------------

    def runtime_state_evidence(self) -> dict[str, Any]:
        """Bounded snapshots of main-system runtime state files."""
        evidence: dict[str, Any] = {}
        for name in _RUNTIME_STATE_FILES:
            path = self.state_dir / name
            if not path.is_file():
                evidence[name] = {"available": False}
                continue
            data = _read_json_bounded(path)
            if data is None:
                evidence[name] = {"available": False, "error": "unreadable"}
                continue
            if name == "boot-core.json" and isinstance(data, dict):
                evidence[name] = {
                    key: data[key] for key in _BOOT_CORE_KEYS if key in data
                } or {"available": True}
            elif name == "runtime-readiness.json" and isinstance(data, dict):
                evidence[name] = {
                    key: data[key] for key in _READINESS_KEYS if key in data
                } or {"available": True}
            elif name == "ipc-connection-state.json" and isinstance(data, dict):
                evidence[name] = {
                    key: data[key] for key in _IPC_STATE_KEYS if key in data
                } or {"available": True}
            elif name == "repair-requests.json":
                if isinstance(data, list):
                    evidence[name] = {"recent": data[-_MAX_REPAIR_REQUESTS:]}
                else:
                    evidence[name] = data
            else:
                evidence[name] = data if isinstance(data, dict) else {
                    "available": True
                }
        return evidence

    def outbox_tail(
        self,
        *,
        limit: int = _MAX_OUTBOX_EVENTS,
    ) -> list[dict[str, Any]]:
        """Recent committed outbox events (A195) — newest first."""
        path = self.state_dir / "state-outbox.sqlite3"
        if not path.is_file():
            return []
        try:
            db = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True
            )
            try:
                rows = db.execute(
                    "SELECT sequence, entity_type, entity_id, operation, "
                    "authoritative_revision, committed_at "
                    "FROM outbox_events ORDER BY sequence DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            finally:
                db.close()
            return [
                {
                    "sequence": int(row[0]),
                    "entity_type": str(row[1] or ""),
                    "entity_id": str(row[2] or ""),
                    "operation": str(row[3] or ""),
                    "authoritative_revision": int(row[4] or 0),
                    "committed_at": str(row[5] or ""),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------------
    # Full diagnosis
    # ------------------------------------------------------------------

    def diagnose(self, symptom: str) -> dict[str, Any]:
        """Produce a governed, read-only diagnosis evidence pack.

        The pack grounds Xingcheng's answer in the authoritative fault-code
        directory and governed maintenance manuals, plus bounded runtime
        evidence — never guesses and never executes repairs.
        """
        text = str(symptom or "").strip()[:2_000]
        matched = self.match_fault_codes(text)
        manuals = self.manuals_for(
            [entry["fault_code"] for entry in matched]
        )
        return {
            "ok": True,
            "schema": "xingcheng-fault-diagnosis/v1",
            "symptom": text,
            "matched_fault_codes": matched,
            "matched_fault_code_ids": [m["fault_code"] for m in matched],
            "maintenance_manuals": manuals,
            "suggested_ordered_steps": [
                {
                    "manual_code": manual["manual_code"],
                    "target_entity": manual["target_entity"],
                    "diagnosis": manual["diagnosis"],
                    "preconditions": manual["preconditions"],
                    "ordered_steps": manual["ordered_steps"],
                    "verification": manual["verification"],
                    "stop_conditions": manual["stop_conditions"],
                    "risk_level": manual["risk_level"],
                    "required_permission": manual["required_permission"],
                }
                for manual in manuals
            ],
            "runtime_evidence": self.runtime_state_evidence(),
            "recent_state_events": self.outbox_tail(),
            "authority": {
                "mode": "diagnosis-read-only",
                "execution": False,
                "repair_owner": "main-system-central-repair",
                "repair_channel": "governed-execution-channel",
                "governance_source_access": "direct-read-only-authoritative",
            },
        }


__all__ = ["FaultDiagnostics"]
