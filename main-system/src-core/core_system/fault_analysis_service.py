"""Fault analysis service — facade.

This module provides the FaultAnalysisService class.  Implementation
details live in submodules:

  * :mod:`core_system.fault_analysis_service_types` — data structures.
  * :mod:`core_system.fault_analysis_service_collectors` — collectors mixin.

Read-only fault evidence aggregation and pattern detection for Xingcheng
(A174/A6500 codex-authorized global review authority).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .fault_analysis_service_types import (
    _iso_now,
    _AUTOMATIC_REPAIR_ROOT,
    _RUNTIME_STATE_ROOT,
    _SYSTEM_RESCUE_ROOT,
    _QUARANTINE_DIR,
    FaultSummary,
    FaultPattern,
)
from .fault_analysis_service_collectors import FaultAnalysisCollectorsMixin


class FaultAnalysisService(FaultAnalysisCollectorsMixin):
    """Read-only fault evidence aggregation and pattern detection."""

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
        severity_counts: dict[str, int] = {}
        for f in faults:
            severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1
        outcome_counts: dict[str, int] = {}
        for f in faults:
            outcome_counts[f.repair_outcome] = outcome_counts.get(f.repair_outcome, 0) + 1
        source_counts: dict[str, int] = {}
        for f in faults:
            source_counts[f.source] = source_counts.get(f.source, 0) + 1
        boot_state = self._read_boot_core_state()
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
_service_lock = threading.Lock()


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
