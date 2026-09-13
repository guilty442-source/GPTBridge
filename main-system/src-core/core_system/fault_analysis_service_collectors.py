"""Fault analysis service — collectors mixin.

Provides the fault evidence collector methods for the
FaultAnalysisService class.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from .fault_analysis_service_types import FaultSummary

_logger = logging.getLogger("gptbridge.fault_analysis")


class FaultAnalysisCollectorsMixin:
    """Fault evidence collector methods for FaultAnalysisService."""

    def _collect_repair_runs(self) -> list[FaultSummary]:
        """Collect repair run records from per-tool SQLite databases."""
        faults: list[FaultSummary] = []
        if not self._repair_root.is_dir():
            return faults
        for tool_dir in self._repair_root.iterdir():
            if not tool_dir.is_dir():
                continue
            db_path = tool_dir / "automatic-repair.sqlite3"
            if not db_path.is_file():
                continue
            tool_id = tool_dir.name
            try:
                faults.extend(self._read_repair_runs_db(db_path, tool_id))
            except Exception as exc:
                _logger.debug("fault_analysis_repair_runs_skip tool=%s err=%s", tool_id, exc)
        return faults

    def _collect_repair_learning(self) -> list[FaultSummary]:
        """Collect repair learning records (error signatures + outcomes)."""
        faults: list[FaultSummary] = []
        db_path = self._repair_root / "repair-learning.sqlite3"
        if not db_path.is_file():
            return faults
        try:
            connection = sqlite3.connect(
                f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3,
            )
            try:
                rows = connection.execute(
                    "SELECT signature_hash, error_class, message_pattern, "
                    "failure_code, file_context, target_tool_id, "
                    "outcome, attempted_action, timestamp "
                    "FROM repair_learning ORDER BY timestamp DESC LIMIT 200"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                connection.close()
            for row in rows:
                sig, err_class, msg_pattern, fail_code, file_ctx, tool_id, outcome, action, ts = row
                faults.append(FaultSummary(
                    fault_id=f"learning-{sig}",
                    fault_type="repair-learning",
                    source=f"tool:{tool_id or 'unknown'}",
                    timestamp=str(ts or ""),
                    severity=self._severity_from_code(str(fail_code)),
                    error_class=str(err_class or ""),
                    error_message=str(msg_pattern or ""),
                    target_entity=str(tool_id or ""),
                    repair_action=str(action or ""),
                    repair_outcome=self._normalize_outcome(str(outcome or "")),
                    raw_evidence={
                        "signature_hash": sig,
                        "failure_code": fail_code,
                        "file_context": file_ctx,
                    },
                ))
        except Exception as exc:
            _logger.debug("fault_analysis_learning_skip err=%s", exc)
        return faults

    def _collect_crash_diagnosis(self) -> list[FaultSummary]:
        """Collect crash diagnosis records from boot-core state."""
        faults: list[FaultSummary] = []
        boot_state = self._read_boot_core_state()
        last_exit = boot_state.get("last_exit", {})
        if last_exit and last_exit.get("exit_code") not in (0, None):
            faults.append(FaultSummary(
                fault_id=f"crash-{boot_state.get('backend_pid', 'unknown')}",
                fault_type="crash",
                source="boot-core",
                timestamp=str(boot_state.get("updated_at", "")),
                severity="critical" if boot_state.get("status") == "failed" else "high",
                error_class=str(last_exit.get("error_type", "UnknownExit")),
                error_message=str(last_exit.get("reason", "")),
                target_entity="main-system",
                repair_action=str(last_exit.get("repair_action", "")),
                repair_outcome=str(last_exit.get("repair_outcome", "pending")),
                raw_evidence=last_exit,
            ))
        return faults

    def _collect_repair_requests(self) -> list[FaultSummary]:
        """Collect repair request signals from the information layer."""
        faults: list[FaultSummary] = []
        requests_path = self._state_root / "repair-requests.json"
        if not requests_path.is_file():
            return faults
        try:
            data = json.loads(requests_path.read_text(encoding="utf-8"))
            requests = data if isinstance(data, list) else data.get("requests", [])
            for req in requests:
                if not isinstance(req, dict):
                    continue
                faults.append(FaultSummary(
                    fault_id=f"repair-req-{req.get('id', hash(str(req)))}",
                    fault_type="repair-request",
                    source=str(req.get("owner", "unknown")),
                    timestamp=str(req.get("requested_at", "")),
                    severity=self._severity_from_code(str(req.get("failure_code", ""))),
                    error_class=str(req.get("failure_code", "")),
                    error_message=str(req.get("reason", "")),
                    target_entity=str(req.get("target_entity", "")),
                    repair_action=str(req.get("repair_plan", {}).get("action", "")),
                    repair_outcome="pending",
                    raw_evidence=req,
                ))
        except (OSError, json.JSONDecodeError):
            pass
        return faults

    def _collect_quarantine_records(self) -> list[FaultSummary]:
        """Collect tool crash quarantine records."""
        faults: list[FaultSummary] = []
        if not self._quarantine_dir.is_dir():
            return faults
        for record_path in self._quarantine_dir.glob("*.json"):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                faults.append(FaultSummary(
                    fault_id=f"quarantine-{record.get('tool_id', 'unknown')}-{record_path.stem}",
                    fault_type="quarantine",
                    source=f"tool:{record.get('tool_id', 'unknown')}",
                    timestamp=str(record.get("timestamp", "")),
                    severity="high",
                    error_class="ToolCrash",
                    error_message=f"Tool crashed with exit code {record.get('exit_code', 'unknown')}",
                    target_entity=str(record.get("tool_id", "")),
                    repair_action="quarantine",
                    repair_outcome="quarantined",
                    raw_evidence=record,
                ))
            except (OSError, json.JSONDecodeError):
                pass
        return faults

    def _collect_audit_records(self) -> list[FaultSummary]:
        """Collect audit records from system-rescue."""
        faults: list[FaultSummary] = []
        audit_root = self._rescue_root / "audit"
        if not audit_root.is_dir():
            return faults
        for audit_file in audit_root.rglob("*.json"):
            try:
                record = json.loads(audit_file.read_text(encoding="utf-8"))
                if isinstance(record, list):
                    for entry in record:
                        self._audit_entry_to_fault(entry, faults)
                elif isinstance(record, dict):
                    self._audit_entry_to_fault(record, faults)
            except (OSError, json.JSONDecodeError):
                pass
        return faults

    def _audit_entry_to_fault(self, entry: dict[str, Any], faults: list[FaultSummary]) -> None:
        """Convert an audit entry to a FaultSummary if it represents a fault."""
        operation = str(entry.get("operation", ""))
        if not any(marker in operation for marker in ("repair", "crash", "fault", "error", "fail")):
            return
        faults.append(FaultSummary(
            fault_id=f"audit-{entry.get('id', hash(str(entry)))}",
            fault_type="audit",
            source=str(entry.get("actor", "system-rescue")),
            timestamp=str(entry.get("timestamp", "")),
            severity="info",
            error_class=str(entry.get("operation", "")),
            error_message=str(entry.get("result", "")),
            target_entity=str(entry.get("target", "")),
            repair_action="",
            repair_outcome=str(entry.get("status", "")),
            raw_evidence=entry,
        ))
