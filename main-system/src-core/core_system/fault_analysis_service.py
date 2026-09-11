"""Fault analysis service — read-only fault evidence aggregation and pattern detection.

Provides Xingcheng (星澄) with a structured, read-only view of system fault
evidence for global review and anomaly explanation.  This service aggregates
fault data from multiple sources:

  * **Automatic repair knowledge** — repair recipes and learned patterns
  * **Repair run history** — per-tool repair execution records
  * **Repair learning** — error signatures, outcomes, and promoted recipes
  * **Crash diagnosis records** — boot-core crash tracebacks and signals
  * **Repair requests** — pending and completed governed repair requests
  * **System health snapshots** — boot-core state and health probe results
  * **Tool crash quarantine** — isolated tool crash records
  * **Audit records** — system-rescue audit trail
  * **Runtime logs** — system-rescue log entries

The service is **strictly read-only**.  It never executes repairs, writes
state, or modifies any data.  All analysis is derived from existing
artifacts on disk.

Per A174/A6500: Xingcheng has codex-authorized global review authority.
This service implements the data aggregation surface for fault analysis
within that authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

_logger = logging.getLogger("gptbridge.fault_analysis")

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

_AUTOMATIC_REPAIR_ROOT: Final[tuple[str, ...]] = (
    "main-system", "data", "automatic-repair",
)
_RUNTIME_STATE_ROOT: Final[tuple[str, ...]] = (
    "main-system", "runtime", "state",
)
_SYSTEM_RESCUE_ROOT: Final[tuple[str, ...]] = (
    "system-rescue", "data",
)
_QUARANTINE_DIR: Final[tuple[str, ...]] = (
    "main-system", "runtime", "state", "tool-crash-quarantine",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------

@dataclass
class FaultSummary:
    """Aggregated fault summary for a single fault incident."""
    fault_id: str
    fault_type: str  # crash | repair | health | quarantine | audit
    source: str  # boot-core | tool:{id} | system-rescue | ...
    timestamp: str
    severity: str  # critical | high | medium | low | info
    error_class: str
    error_message: str
    target_entity: str
    repair_action: str
    repair_outcome: str  # success | failure | pending | skipped | none
    raw_evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fault_id": self.fault_id,
            "fault_type": self.fault_type,
            "source": self.source,
            "timestamp": self.timestamp,
            "severity": self.severity,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "target_entity": self.target_entity,
            "repair_action": self.repair_action,
            "repair_outcome": self.repair_outcome,
            "raw_evidence": self.raw_evidence,
        }


@dataclass
class FaultPattern:
    """A recurring fault pattern detected across multiple incidents."""
    pattern_id: str
    error_signature: str
    error_class: str
    occurrence_count: int
    first_seen: str
    last_seen: str
    affected_entities: list[str]
    common_repair_action: str
    success_rate: float  # 0.0 to 1.0
    severity_trend: str  # increasing | stable | decreasing | unknown
    sample_fault_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "error_signature": self.error_signature,
            "error_class": self.error_class,
            "occurrence_count": self.occurrence_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "affected_entities": self.affected_entities,
            "common_repair_action": self.common_repair_action,
            "success_rate": round(self.success_rate, 3),
            "severity_trend": self.severity_trend,
            "sample_fault_ids": self.sample_fault_ids,
        }


# ------------------------------------------------------------------
# Fault analysis service
# ------------------------------------------------------------------

class FaultAnalysisService:
    """Read-only fault evidence aggregation and pattern detection.

    All methods are synchronous and designed to be called from async
    code via ``asyncio.to_thread``.
    """

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self._repair_root = self.project_root.joinpath(*_AUTOMATIC_REPAIR_ROOT)
        self._state_root = self.project_root.joinpath(*_RUNTIME_STATE_ROOT)
        self._rescue_root = self.project_root.joinpath(*_SYSTEM_RESCUE_ROOT)
        self._quarantine_dir = self.project_root.joinpath(*_QUARANTINE_DIR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect_all_faults(self, *, limit: int = 500) -> list[FaultSummary]:
        """Collect fault evidence from all sources, sorted by recency."""
        faults: list[FaultSummary] = []
        faults.extend(self._collect_repair_runs())
        faults.extend(self._collect_repair_learning())
        faults.extend(self._collect_crash_diagnosis())
        faults.extend(self._collect_repair_requests())
        faults.extend(self._collect_quarantine_records())
        faults.extend(self._collect_audit_records())
        faults.sort(key=lambda f: f.timestamp, reverse=True)
        return faults[:limit]

    def detect_patterns(self, faults: list[FaultSummary] | None = None) -> list[FaultPattern]:
        """Detect recurring fault patterns from collected evidence."""
        if faults is None:
            faults = self.collect_all_faults()
        return self._detect_patterns(faults)

    def system_health_overview(self) -> dict[str, Any]:
        """Return a high-level system health overview from fault evidence."""
        faults = self.collect_all_faults(limit=100)
        patterns = self.detect_patterns(faults)
        # Severity distribution.
        severity_counts: dict[str, int] = {}
        for f in faults:
            severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1
        # Outcome distribution.
        outcome_counts: dict[str, int] = {}
        for f in faults:
            outcome_counts[f.repair_outcome] = outcome_counts.get(f.repair_outcome, 0) + 1
        # Source distribution.
        source_counts: dict[str, int] = {}
        for f in faults:
            source_counts[f.source] = source_counts.get(f.source, 0) + 1
        # Boot-core state.
        boot_state = self._read_boot_core_state()
        # Repair recipes count.
        recipes = self._read_repair_recipes()
        return {
            "timestamp": _iso_now(),
            "total_faults_collected": len(faults),
            "total_patterns_detected": len(patterns),
            "severity_distribution": severity_counts,
            "outcome_distribution": outcome_counts,
            "source_distribution": source_counts,
            "boot_core_status": boot_state.get("status", "unknown"),
            "boot_core_restarts": boot_state.get("restarts", 0),
            "boot_core_last_exit": boot_state.get("last_exit", {}),
            "repair_recipes_count": len(recipes),
            "top_patterns": [p.as_dict() for p in patterns[:5]],
            "recent_faults": [f.as_dict() for f in faults[:10]],
        }

    def fault_detail(self, fault_id: str) -> dict[str, Any] | None:
        """Return detailed evidence for a specific fault ID."""
        faults = self.collect_all_faults(limit=1000)
        for f in faults:
            if f.fault_id == fault_id:
                return f.as_dict()
        return None

    def repair_knowledge_summary(self) -> dict[str, Any]:
        """Summarize the repair knowledge base (recipes + learned patterns)."""
        recipes = self._read_repair_recipes()
        learned = self._read_learned_recipes()
        return {
            "timestamp": _iso_now(),
            "total_recipes": len(recipes),
            "total_learned": len(learned),
            "recipes": recipes,
            "learned_recipes": learned,
        }

    def analyze_component(self, component: str) -> dict[str, Any]:
        """Analyze faults for a specific component (tool_id or 'main-system')."""
        faults = self.collect_all_faults(limit=1000)
        component_faults = [
            f for f in faults
            if component in f.target_entity or component in f.source
        ]
        patterns = self.detect_patterns(component_faults)
        return {
            "component": component,
            "timestamp": _iso_now(),
            "total_faults": len(component_faults),
            "total_patterns": len(patterns),
            "severity_distribution": self._severity_dist(component_faults),
            "outcome_distribution": self._outcome_dist(component_faults),
            "patterns": [p.as_dict() for p in patterns],
            "recent_faults": [f.as_dict() for f in component_faults[:20]],
        }

    # ------------------------------------------------------------------
    # Fault collectors
    # ------------------------------------------------------------------

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
        # Only collect fault-related audit entries.
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

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def _detect_patterns(self, faults: list[FaultSummary]) -> list[FaultPattern]:
        """Group faults by error signature and detect recurring patterns."""
        groups: dict[str, list[FaultSummary]] = {}
        for f in faults:
            sig = self._error_signature(f)
            groups.setdefault(sig, []).append(f)
        patterns: list[FaultPattern] = []
        for sig, group in groups.items():
            if len(group) < 1:
                continue
            entities = list({f.target_entity for f in group if f.target_entity})
            outcomes = [f.repair_outcome for f in group]
            successes = sum(1 for o in outcomes if o == "success")
            total_with_outcome = sum(1 for o in outcomes if o in ("success", "failure"))
            success_rate = successes / total_with_outcome if total_with_outcome else 0.0
            timestamps = sorted(f.timestamp for f in group if f.timestamp)
            actions = [f.repair_action for f in group if f.repair_action]
            common_action = max(set(actions), key=actions.count) if actions else ""
            severity_trend = self._detect_severity_trend(group)
            patterns.append(FaultPattern(
                pattern_id=hashlib.sha256(sig.encode()).hexdigest()[:12],
                error_signature=sig,
                error_class=group[0].error_class,
                occurrence_count=len(group),
                first_seen=timestamps[0] if timestamps else "",
                last_seen=timestamps[-1] if timestamps else "",
                affected_entities=entities,
                common_repair_action=common_action,
                success_rate=success_rate,
                severity_trend=severity_trend,
                sample_fault_ids=[f.fault_id for f in group[:5]],
            ))
        patterns.sort(key=lambda p: p.occurrence_count, reverse=True)
        return patterns

    def _error_signature(self, fault: FaultSummary) -> str:
        """Produce a normalized error signature for grouping."""
        err_class = re.sub(r"\d+", "#", fault.error_class)
        err_msg = re.sub(r"\d+", "#", fault.error_message[:200])
        return f"{fault.fault_type}|{err_class}|{err_msg}"

    def _detect_severity_trend(self, group: list[FaultSummary]) -> str:
        """Detect whether severity is increasing, stable, or decreasing."""
        if len(group) < 3:
            return "unknown"
        severity_order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        sorted_group = sorted(group, key=lambda f: f.timestamp)
        recent = sorted_group[-len(group) // 3:]
        older = sorted_group[:len(group) // 3]
        recent_avg = sum(severity_order.get(f.severity, 0) for f in recent) / len(recent)
        older_avg = sum(severity_order.get(f.severity, 0) for f in older) / len(older)
        if recent_avg > older_avg + 0.5:
            return "increasing"
        if recent_avg < older_avg - 0.5:
            return "decreasing"
        return "stable"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_repair_runs_db(self, db_path: Path, tool_id: str) -> list[FaultSummary]:
        """Read repair run records from a single tool's SQLite database."""
        faults: list[FaultSummary] = []
        connection = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3,
        )
        try:
            # Try common schema variations.
            try:
                rows = connection.execute(
                    "SELECT id, failure_code, error_class, error_message, "
                    "repair_action, outcome, timestamp, target_file "
                    "FROM repair_runs ORDER BY timestamp DESC LIMIT 100"
                ).fetchall()
            except sqlite3.OperationalError:
                try:
                    rows = connection.execute(
                        "SELECT id, failure_code, '', '', "
                        "repair_action, status, timestamp, '' "
                        "FROM repair_runs ORDER BY timestamp DESC LIMIT 100"
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            for row in rows:
                rid, fail_code, err_class, err_msg, action, outcome, ts, target = row
                faults.append(FaultSummary(
                    fault_id=f"repair-{tool_id}-{rid}",
                    fault_type="repair",
                    source=f"tool:{tool_id}",
                    timestamp=str(ts or ""),
                    severity=self._severity_from_code(str(fail_code or "")),
                    error_class=str(err_class or fail_code or ""),
                    error_message=str(err_msg or ""),
                    target_entity=str(target or tool_id),
                    repair_action=str(action or ""),
                    repair_outcome=self._normalize_outcome(str(outcome or "")),
                    raw_evidence={"failure_code": fail_code, "run_id": rid},
                ))
        finally:
            connection.close()
        return faults

    def _read_repair_recipes(self) -> list[dict[str, Any]]:
        """Read repair recipes from the knowledge base."""
        recipes_path = self._repair_root / "knowledge" / "recipes.json"
        if not recipes_path.is_file():
            return []
        try:
            data = json.loads(recipes_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recipes = data.get("recipes", [])
            else:
                recipes = data
            return recipes if isinstance(recipes, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _read_learned_recipes(self) -> list[dict[str, Any]]:
        """Read learned recipes from the repair learning database."""
        db_path = self._repair_root / "repair-learning.sqlite3"
        if not db_path.is_file():
            return []
        try:
            connection = sqlite3.connect(
                f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3,
            )
            try:
                rows = connection.execute(
                    "SELECT signature_hash, error_class, message_pattern, "
                    "failure_code, remedy_hint, promotion_count, created_at "
                    "FROM learned_recipes ORDER BY promotion_count DESC LIMIT 50"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                connection.close()
            return [
                {
                    "signature_hash": r[0],
                    "error_class": r[1],
                    "message_pattern": r[2],
                    "failure_code": r[3],
                    "remedy_hint": r[4],
                    "promotion_count": r[5],
                    "created_at": r[6],
                }
                for r in rows
            ]
        except Exception:
            return []

    def _read_boot_core_state(self) -> dict[str, Any]:
        """Read boot-core state for crash/restart information."""
        state_path = self._state_root / "boot-core.json"
        if not state_path.is_file():
            return {}
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _severity_from_code(self, failure_code: str) -> str:
        """Map a failure code to a severity level."""
        code = failure_code.upper()
        if any(m in code for m in ("TAMPER", "CRASH", "CORRUPT", "FATAL")):
            return "critical"
        if any(m in code for m in ("DISCONNECT", "FAILED", "ERROR", "SYNTAX")):
            return "high"
        if any(m in code for m in ("MISSING", "STALE", "INCOMPATIBLE", "NOT_READY")):
            return "medium"
        if any(m in code for m in ("WARNING", "DEGRADED")):
            return "low"
        return "info"

    def _normalize_outcome(self, outcome: str) -> str:
        """Normalize an outcome string to a standard set."""
        o = outcome.lower().strip()
        if o in ("success", "succeeded", "ok", "passed", "repaired"):
            return "success"
        if o in ("failure", "failed", "error"):
            return "failure"
        if o in ("pending", "in_progress", "running", "queued"):
            return "pending"
        if o in ("skipped", "skip", "not_applicable"):
            return "skipped"
        if o in ("quarantined", "isolated"):
            return "quarantined"
        return o or "none"

    def _severity_dist(self, faults: list[FaultSummary]) -> dict[str, int]:
        dist: dict[str, int] = {}
        for f in faults:
            dist[f.severity] = dist.get(f.severity, 0) + 1
        return dist

    def _outcome_dist(self, faults: list[FaultSummary]) -> dict[str, int]:
        dist: dict[str, int] = {}
        for f in faults:
            dist[f.repair_outcome] = dist.get(f.repair_outcome, 0) + 1
        return dist


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_service: FaultAnalysisService | None = None
_service_lock = __import__("threading").Lock()


def get_fault_analysis_service(project_root: Path | str | None = None) -> FaultAnalysisService:
    """Return the singleton FaultAnalysisService."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                root = project_root or Path(__file__).resolve().parents[3]
                _service = FaultAnalysisService(root)
    return _service


__all__ = [
    "FaultSummary",
    "FaultPattern",
    "FaultAnalysisService",
    "get_fault_analysis_service",
]
