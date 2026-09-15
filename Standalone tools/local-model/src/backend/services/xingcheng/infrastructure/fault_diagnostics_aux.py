"""Auxiliary fault-diagnostic evidence for 星澄.

Extends the state-file evidence with live sources that carry the actual
fault text: the backend log tail, the tool-crash quarantine directory,
and the central repair-learning store (recurring signatures and recent
repair outcomes).  All reads stay read-only and bounded.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fault_diagnostics_data import (
    _LOG_SIGNAL_PATTERN,
    _MAX_ERROR_SIGNATURES,
    _MAX_LOG_LINES,
    _MAX_LOG_TAIL_BYTES,
    _MAX_QUARANTINE_ENTRIES,
    _MAX_RECENT_OUTCOMES,
    _QUARANTINE_DIR_NAME,
    _QUARANTINE_KEYS,
    _REPAIR_LEARNING_RELATIVE,
    _UPPER_SNAKE_TOKEN,
    _read_json_bounded,
)


class FaultDiagnosticsAuxMixin:
    """Live-text and crash/learning evidence readers (read-only)."""

    project_root: Path
    state_dir: Path

    def boot_log_tail(self) -> dict[str, Any]:
        """Signal lines from the tail of the backend output log.

        Surfaces the actual error text the system emitted — the strongest
        grounding for quoting a concrete failure in an answer.
        """
        path = self.state_dir / "boot-output.log"
        if not path.is_file():
            return {"available": False, "lines": []}
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - _MAX_LOG_TAIL_BYTES))
                raw = handle.read()
        except OSError:
            return {"available": False, "lines": []}
        lines = [
            line.strip()
            for line in raw.decode("utf-8", errors="replace").splitlines()
            if line.strip() and _LOG_SIGNAL_PATTERN.search(line)
        ]
        lines = [line[:240] for line in lines][-_MAX_LOG_LINES:]
        return {
            "available": True,
            "lines": lines,
            "truncated": size > _MAX_LOG_TAIL_BYTES,
        }

    def log_fault_tokens(self, log_tail: dict[str, Any]) -> list[str]:
        """UPPER_SNAKE tokens inside log signal lines — candidate codes."""
        tokens: list[str] = []
        for line in log_tail.get("lines") or []:
            for token in _UPPER_SNAKE_TOKEN.findall(str(line)):
                if token not in tokens:
                    tokens.append(token)
        return tokens[:10]

    def quarantined_tools(self) -> list[dict[str, Any]]:
        """Tools currently parked in the crash quarantine directory.

        Each quarantine file is direct evidence that a governed tool
        process died and was isolated — a high-weight localization signal.
        """
        directory = self.state_dir / _QUARANTINE_DIR_NAME
        if not directory.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        try:
            paths = sorted(directory.glob("*.json"))[:_MAX_QUARANTINE_ENTRIES]
        except OSError:
            return []
        for path in paths:
            data = _read_json_bounded(path)
            if isinstance(data, dict):
                entries.append(
                    {
                        key: data[key]
                        for key in _QUARANTINE_KEYS
                        if key in data
                    }
                )
            else:
                entries.append({"tool_id": path.stem.rsplit("-", 1)[0]})
        return entries

    def repair_learning_tail(self) -> dict[str, Any]:
        """Recurring error signatures and recent failed repair outcomes.

        ``error_signatures.occurrence_count`` marks faults that keep
        coming back; ``repair_outcomes.ok = 0`` marks repairs that ran
        and failed — both distinguish a transient from a chronic fault.
        """
        path = self.project_root.joinpath(*_REPAIR_LEARNING_RELATIVE)
        if not path.is_file():
            return {"available": False}
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                signatures = self._signature_rows(db)
                failures = self._failure_rows(db)
                recipes = db.execute(
                    "SELECT COUNT(*) FROM learned_recipes"
                ).fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return {"available": False}
        return {
            "available": True,
            "recurring_signatures": signatures,
            "recent_failures": failures,
            "learned_recipe_count": int(recipes[0] or 0) if recipes else 0,
        }

    @staticmethod
    def _signature_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            rows = db.execute(
                "SELECT failure_code, error_class, occurrence_count, "
                "last_seen, file_context, target_tool_id "
                "FROM error_signatures "
                "ORDER BY occurrence_count DESC, last_seen DESC LIMIT ?",
                (_MAX_ERROR_SIGNATURES,),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "failure_code": str(row[0] or ""),
                "error_class": str(row[1] or ""),
                "occurrence_count": int(row[2] or 0),
                "last_seen": str(row[3] or ""),
                "file_context": str(row[4] or ""),
                "target_tool_id": str(row[5] or ""),
            }
            for row in rows
        ]

    @staticmethod
    def _failure_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
        try:
            rows = db.execute(
                "SELECT signature_hash, remedy, recorded_at "
                "FROM repair_outcomes WHERE ok = 0 "
                "ORDER BY recorded_at DESC LIMIT ?",
                (_MAX_RECENT_OUTCOMES,),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "signature_hash": str(row[0] or "")[:16],
                "remedy": str(row[1] or ""),
                "recorded_at": str(row[2] or ""),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Anomaly producers for the auxiliary evidence classes
    # ------------------------------------------------------------------

    def _aux_anomalies(
        self, aux: dict[str, Any], add: Any
    ) -> None:
        self._pending_action_anomalies(aux.get("pending_actions"), add)
        self._watcher_anomalies(aux.get("watcher"), add)
        self._startup_anomalies(aux.get("startup"), add)
        self._quarantine_anomalies(aux.get("quarantined_tools"), add)
        self._learning_anomalies(aux.get("repair_learning"), add)

    @staticmethod
    def _pending_action_anomalies(pending: Any, add: Any) -> None:
        if not isinstance(pending, list):
            return
        for item in pending:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "") != "awaiting-confirmation":
                continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            owner = str(detail.get("owner") or "") or "assistant-panel"
            code = str(detail.get("failure_code") or item.get("kind") or "pending")
            add(
                "pending-actions.json",
                f"awaiting-confirmation: {code} ({str(item.get('summary') or '')[:80]})",
                owner,
                3.5,
            )

    @staticmethod
    def _watcher_anomalies(watcher: Any, add: Any) -> None:
        if not isinstance(watcher, dict):
            return
        try:
            failures = int(watcher.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
        if failures > 0:
            add(
                "hot-reload-watcher.json",
                f"consecutive_failures={failures}",
                "ipc-channel",
                min(2.0 + 0.5 * failures, 5.0),
            )
        errors = watcher.get("errors")
        if isinstance(errors, list) and errors:
            add(
                "hot-reload-watcher.json",
                f"watcher_errors={str(errors[0])[:80]}",
                "ipc-channel",
                3.0,
            )

    @staticmethod
    def _startup_anomalies(startup: Any, add: Any) -> None:
        if not isinstance(startup, dict):
            return
        if startup.get("ok") is False:
            add(
                "startup-generation.json",
                "startup generation failed",
                "startup-pipeline",
                6.0,
            )
        for phase_id in startup.get("failed_phases") or []:
            add(
                "startup-generation.json",
                f"failed_phase={phase_id}",
                "startup-pipeline",
                4.5,
            )

    @staticmethod
    def _quarantine_anomalies(quarantined: Any, add: Any) -> None:
        if not isinstance(quarantined, list):
            return
        for entry in quarantined:
            if not isinstance(entry, dict):
                continue
            tool_id = str(entry.get("tool_id") or "unknown-tool")
            exit_code = entry.get("exit_code")
            add(
                "tool-crash-quarantine",
                f"quarantined: {tool_id} exit={exit_code}",
                "tool-runtime",
                5.0,
            )

    @staticmethod
    def _learning_anomalies(learning: Any, add: Any) -> None:
        if not isinstance(learning, dict) or learning.get("available") is not True:
            return
        for signature in learning.get("recurring_signatures") or []:
            if not isinstance(signature, dict):
                continue
            count = int(signature.get("occurrence_count") or 0)
            if count < 2:
                continue
            entity = (
                str(signature.get("target_tool_id") or "").strip()
                or str(signature.get("failure_code") or "").strip()
                or "main-backend"
            )
            add(
                "repair-learning.sqlite3",
                f"recurring {signature.get('failure_code')} x{count}",
                entity,
                min(2.0 + 0.5 * count, 6.0),
            )
        failures = learning.get("recent_failures") or []
        if failures:
            add(
                "repair-learning.sqlite3",
                f"recent_repair_failures={len(failures)}",
                "main-backend",
                min(2.0 + 0.5 * len(failures), 4.0),
            )


__all__ = ["FaultDiagnosticsAuxMixin"]
