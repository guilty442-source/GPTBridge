"""Fault analysis service — collectors mixin.

Provides the fault evidence collector methods for the
FaultAnalysisService class.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import time
from typing import Any

from .fault_analysis_service_types import FaultSummary

_logger = logging.getLogger("gptbridge.fault_analysis")


def _is_recent_utc(timestamp: str, cutoff_epoch: float) -> bool:
    """True when an ISO-8601 UTC timestamp is newer than the cutoff epoch.

    Unparseable or empty timestamps are treated as recent (fail-closed: the
    evidence keeps presenting until a valid recovery signal exists).
    """
    text = str(timestamp or "").strip()
    if not text or cutoff_epoch <= 0:
        return True
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() >= cutoff_epoch
    except ValueError:
        return True

# Repair-request statuses that are terminal: the request is retired and
# must not be presented as a live pending fault.  The durable record stays
# in the information layer as evidence.
_TERMINAL_REPAIR_REQUEST_STATUSES = frozenset(
    {
        "expired",
        "resolved",
        "cancelled",
        "canceled",
        "dismissed",
        "superseded",
        "reconciled-non-actionable",
        # Decided requests: the sovereign chain finished (denied or
        # executed to completion).  Failure evidence still surfaces via
        # the learning-store collector — presenting the request itself
        # as pending would double-count it.
        "denied-not-repairable",
        "denied-permission",
        "denied-audit-conflict",
        "denied-no-decision-sovereign",
        "approved-repair-completed",
        "approved-repair-failed",
        "completed",
        "failed",
    }
)

# Learning-store remedy written by fault-message reconciliation for
# non-actionable evidence (expired/unclassifiable faults).  It is failure
# evidence for learning, not a live fault, and must never be re-presented
# as an unresolved fault.
_NON_ACTIONABLE_OUTCOME_REMEDY = "no-action-required"

# The newest failure outcomes are scanned and filtered (non-actionable
# remedy + explicitly absorbed evidence) until this many presentable
# faults are collected, so a large historical backlog can never crowd a
# live fault out of the projection.
_MAX_REPAIR_LEARNING_FAULTS = 200
_REPAIR_OUTCOME_SCAN_LIMIT = 5000

# A crash-quarantine record is presented as a live fault only while it is
# fresh.  Older records are historical evidence: the tool either recovered,
# was stopped on demand, or was handled by the repair chain — showing a
# days-old crash as an unresolved high fault would keep the fault surface
# permanently red.  Activly crash-looping tools keep producing fresh records.
_QUARANTINE_PRESENTATION_WINDOW_HOURS = 0.5


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
        """Collect repair outcome history (current learning-store schema)."""
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
                    "SELECT outcome_id, signature_hash, remedy, ok, "
                    "detail_json, recorded_at FROM repair_outcomes "
                    "WHERE ok = 0 ORDER BY recorded_at DESC LIMIT ?",
                    (_REPAIR_OUTCOME_SCAN_LIMIT,),
                ).fetchall()
                absorbed_ids = self._absorbed_learning_outcome_ids(connection)
            except sqlite3.OperationalError:
                rows = []
                absorbed_ids = set()
            finally:
                connection.close()
            for outcome_id, sig, remedy, ok, detail_json, recorded_at in rows:
                if str(remedy or "") == _NON_ACTIONABLE_OUTCOME_REMEDY:
                    continue
                # Historical failure evidence explicitly absorbed by a
                # reconciliation marker: kept as evidence, never presented
                # as a live fault.
                if str(outcome_id or "") in absorbed_ids:
                    continue
                detail: dict[str, Any] = {}
                try:
                    parsed = json.loads(detail_json or "{}")
                    if isinstance(parsed, dict):
                        detail = parsed
                except (TypeError, ValueError):
                    detail = {}
                failure_code = str(detail.get("failure_code") or "")
                faults.append(FaultSummary(
                    fault_id=f"outcome-{outcome_id}",
                    fault_type="repair-learning",
                    source=f"tool:{detail.get('target_tool_id') or 'unknown'}",
                    timestamp=str(recorded_at or ""),
                    severity=self._severity_from_code(failure_code),
                    error_class=str(detail.get("error_class") or sig or ""),
                    error_message=str(detail.get("message") or ""),
                    target_entity=str(detail.get("target_tool_id") or ""),
                    repair_action=str(remedy or ""),
                    repair_outcome="failure",
                    raw_evidence=detail,
                ))
                if len(faults) >= _MAX_REPAIR_LEARNING_FAULTS:
                    break
        except Exception as exc:
            _logger.debug("fault_analysis_learning_skip err=%s", exc)
        return faults

    def _absorbed_learning_outcome_ids(
        self, connection: sqlite3.Connection
    ) -> set[str]:
        """Failure-evidence ids absorbed by reconciliation markers."""
        try:
            from tasks.repair_learning import absorbed_outcome_ids
        except Exception:
            return set()
        return absorbed_outcome_ids(
            connection, remedy=_NON_ACTIONABLE_OUTCOME_REMEDY
        )

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
                if (
                    str(req.get("status") or "")
                    in _TERMINAL_REPAIR_REQUEST_STATUSES
                ):
                    continue
                faults.append(FaultSummary(
                    fault_id=(
                        f"repair-req-"
                        f"{req.get('id') or req.get('request_id') or hash(str(req))}"
                    ),
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

    def _latest_tool_registrations(self) -> dict[str, str]:
        """Latest successful tool registration per tool (isolation audit).

        A quarantine record older than the tool's latest healthy ``register``
        event describes a crash that has already been recovered; it stays on
        disk as evidence but must not be re-presented as a live fault.
        """
        registrations: dict[str, str] = {}
        audit_path = self._state_root / "tool-isolation-audit.jsonl"
        if not audit_path.is_file():
            return registrations
        try:
            for line in audit_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(entry.get("event", "")) != "register":
                    continue
                tool_id = str(entry.get("tool_id", "") or "")
                timestamp = str(entry.get("timestamp", "") or "")
                if tool_id and timestamp > registrations.get(tool_id, ""):
                    registrations[tool_id] = timestamp
        except OSError:
            return registrations
        return registrations

    def _tool_is_retired(self, tool_id: str) -> bool:
        """True when the tool's manifest declares retirement/disablement.

        A retired tool that exits (or refuses to run) is not a fault: its
        lifecycle was ended by a governance decision, so a crash record must
        not keep the fault surface red.  Missing/unreadable manifests are
        treated as active (fail-open to presenting evidence).
        """
        if not tool_id or tool_id == "unknown":
            return False
        manifest = (
            pathlib.Path(self.project_root)
            / "Standalone tools"
            / tool_id
            / "manifest.json"
        )
        if not manifest.is_file():
            # No governed manifest: the tool was decommissioned/removed from
            # the current architecture (e.g. retired elsewhere in the tree),
            # so its old crash records are historical evidence.
            return True
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        lifecycle = data.get("lifecycle") or {}
        status = str(lifecycle.get("status", "") if isinstance(lifecycle, dict) else "")
        return status == "retired" or data.get("enabled") is False

    def _collect_quarantine_records(self) -> list[FaultSummary]:
        """Collect tool crash quarantine records.

        Resolution-aware: a quarantine record for a tool that has since
        re-registered healthy is historical evidence, not a live fault.
        """
        faults: list[FaultSummary] = []
        if not self._quarantine_dir.is_dir():
            return faults
        registrations = self._latest_tool_registrations()
        cutoff = time.time() - _QUARANTINE_PRESENTATION_WINDOW_HOURS * 3600.0
        for record_path in self._quarantine_dir.glob("*.json"):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                tool_id = str(record.get("tool_id", "unknown"))
                if self._tool_is_retired(tool_id):
                    continue
                quarantined_at = str(record.get("timestamp", "") or "")
                latest_register = registrations.get(tool_id, "")
                if latest_register and quarantined_at and latest_register > quarantined_at:
                    continue
                if not _is_recent_utc(quarantined_at, cutoff):
                    continue
                faults.append(FaultSummary(
                    fault_id=f"quarantine-{tool_id}-{record_path.stem}",
                    fault_type="quarantine",
                    source=f"tool:{tool_id}",
                    timestamp=quarantined_at,
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
