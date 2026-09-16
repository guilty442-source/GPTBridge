"""Xingcheng Sovereign — Codex-vs-Implementation Drift Review (A137-A146).

星澄輔助審查：比對法典宣告與實作差異，並把結果顯示在星澄輔助系統的
使用者呈現面（pending-action 通知面 + 異常追蹤）。整個流程唯讀、僅
諮詢（A137/A138）；顯示寫入走 A139 的 user-notification 路徑，不產生
任何系統效果。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core_system.codex_decision import accepted_outcome

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.codex-drift")

_DRIFT_NOTICE_ID = "codex-drift-report"
_DRIFT_NOTICE_TTL_HOURS = 72
# Auto-loop cadence: one drift review every N manage cycles.
DRIFT_CHECK_EVERY_CYCLES = 60
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


class XingchengCodexDriftMixin:
    """Codex-declared vs implemented-structure comparison (advisory)."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    sovereign_id: str
    _last_drift_report: dict[str, Any]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_drift_report = {}

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def compare_codex_implementation(self) -> dict[str, Any]:
        """Diff codex declarations against the live implementation state."""
        from ...registries import (
            all_children,
            module_assignment_registry,
            parent_of,
            resolve_sovereign,
            supersession_registry,
        )

        findings: list[dict[str, Any]] = []
        declared = all_children()
        materialized = 0
        for parent_id, child_ids in declared.items():
            parent = resolve_sovereign(self.app, parent_id)
            registry = getattr(parent, "_sub_sovereigns", {}) if parent else {}
            materialized += len(registry)
            findings.extend(
                self._child_drift(parent_id, child_ids, registry)
            )
        findings.extend(self._module_drift())
        findings.extend(
            self._supersession_drift(resolve_sovereign, supersession_registry)
        )
        return {
            "checked_at": self._iso_now(),
            "declared": {
                "children": sum(len(v) for v in declared.values()),
                "parents": len(declared),
                "module_assignments": len(module_assignment_registry()),
            },
            "observed": {"materialized_children": materialized},
            "drift": findings,
            "drift_count": len(findings),
            "clean": not findings,
        }

    def _child_drift(
        self,
        parent_id: str,
        declared_ids: tuple[str, ...],
        registry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Hierarchy drift for one codex parent."""
        from ...registries import parent_of

        findings: list[dict[str, Any]] = []
        declared = set(declared_ids)
        for child_id in sorted(declared - set(registry)):
            findings.append(
                {
                    "type": "missing-child",
                    "severity": "warning",
                    "parent": parent_id,
                    "child": child_id,
                    "detail": "codex-declared child not materialized",
                }
            )
        for child_id, child in registry.items():
            if child_id not in declared:
                findings.append(
                    {
                        "type": "unregistered-child",
                        "severity": "critical",
                        "parent": parent_id,
                        "child": child_id,
                        "detail": "materialized child has no active codex row",
                    }
                )
                continue
            actual = getattr(child, "parent_sovereign_id", "")
            if actual and actual != parent_of(child_id):
                findings.append(
                    {
                        "type": "parent-mismatch",
                        "severity": "critical",
                        "parent": parent_id,
                        "child": child_id,
                        "detail": (
                            f"instance declares parent {actual!r}, "
                            f"codex parent is {parent_of(child_id)!r}"
                        ),
                    }
                )
        return findings

    def _module_drift(self) -> list[dict[str, Any]]:
        """Executable-module drift: unresolvable refs + absent managers."""
        findings: list[dict[str, Any]] = []
        try:
            from core_system.sovereign_stack_executor_constants import (
                _CHILD_CLASSES,
                _resolve_child_class,
            )
        except ImportError:
            return findings
        for child_id, class_ref in _CHILD_CLASSES.items():
            try:
                _resolve_child_class(class_ref)
            except (ImportError, AttributeError) as error:
                findings.append(
                    {
                        "type": "unresolvable-module",
                        "severity": "critical",
                        "child": child_id,
                        "detail": f"{class_ref}: {error}",
                    }
                )
        from ...registries import module_assignment_registry, resolve_sovereign

        for row in module_assignment_registry():
            if row.get("status") != "active":
                continue
            manager = row.get("managing_sub_sovereign") or ""
            if manager and resolve_sovereign(self.app, manager) is None:
                findings.append(
                    {
                        "type": "assignment-unmaterialized",
                        "severity": "info",
                        "child": manager,
                        "detail": (
                            f"module {row.get('module_architecture_code')} "
                            "manager not materialized"
                        ),
                    }
                )
        return findings

    def _supersession_drift(
        self, resolve_sovereign: Any, supersession_registry: Any
    ) -> list[dict[str, Any]]:
        """Retired codex identities still materialized at runtime."""
        findings: list[dict[str, Any]] = []
        for row in supersession_registry():
            if row.get("status") != "active":
                continue
            predecessor = row.get("predecessor_identity") or ""
            if predecessor and resolve_sovereign(self.app, predecessor):
                findings.append(
                    {
                        "type": "retired-identity-active",
                        "severity": "critical",
                        "child": predecessor,
                        "detail": (
                            f"superseded by {row.get('successor_identity')} "
                            "but still materialized"
                        ),
                    }
                )
        return findings

    # ------------------------------------------------------------------
    # Auxiliary-system display
    # ------------------------------------------------------------------

    def run_codex_drift_check(self) -> dict[str, Any]:
        """Compare + display the report on the auxiliary surface."""
        report = self.compare_codex_implementation()
        self._last_drift_report = report
        self._display_drift_report(report)
        return report

    def _display_drift_report(self, report: dict[str, Any]) -> None:
        """A139 user-notification: one stable drift notice on the surface."""
        from core_system.auto_action_policy import (
            record_pending_action,
            remove_pending_actions,
        )

        root = Path(getattr(self, "_project_root", "."))
        drift = report.get("drift") or []
        if not drift:
            remove_pending_actions(
                root,
                [_DRIFT_NOTICE_ID],
                actor=self.sovereign_id,
                reason="codex-implementation-consistent",
            )
            return
        record_pending_action(
            root,
            kind="codex-drift",
            action_id=_DRIFT_NOTICE_ID,
            summary=(
                f"法典與實作差異 {report['drift_count']} 項"
                f"（{self._drift_summary(drift)}）"
            ),
            detail={
                "findings": drift[:20],
                "drift_count": report["drift_count"],
                "checked_at": report["checked_at"],
                "advisory": True,
            },
            binding=self._drift_binding(drift),
        )
        for finding in drift:
            self._pending_anomalies.append(
                {"type": "codex-drift", **finding}
            )

    @staticmethod
    def _drift_binding(drift: list[dict[str, Any]]) -> dict[str, Any]:
        worst = max(
            (_SEVERITY_RANK.get(f.get("severity"), 0) for f in drift),
            default=0,
        )
        return {
            "scope": "codex-implementation-drift",
            "target": "governance-codex",
            "proposed_method": "review-only",
            "risk": next(k for k, v in _SEVERITY_RANK.items() if v == worst),
            "expires_at": (
                datetime.now(timezone.utc)
                + timedelta(hours=_DRIFT_NOTICE_TTL_HOURS)
            ).isoformat(),
        }

    @staticmethod
    def _drift_summary(drift: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for finding in drift:
            key = str(finding.get("type") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))

    async def _adjudicate_codex_drift(
        self, request: Any
    ) -> Any:
        """Auxiliary review intent: on-demand drift comparison + display."""
        report = self.run_codex_drift_check()
        review_id = f"codex-drift-{len(self._reviews) + 1}"
        self._reviews[review_id] = {
            "kind": "codex-drift",
            "advisory": True,
            "confidential": True,
            "drift_count": report["drift_count"],
            "clean": report["clean"],
            "reviewed_at": report["checked_at"],
        }
        if report["drift"]:
            await self._notify_anomalies()
        return accepted_outcome(
            {
                "action": "codex-drift-review",
                "advisory": True,
                "review_id": review_id,
                "displayed_on": "xingcheng-auxiliary-surface",
                **report,
            },
            self.verified_basis("A139", "A140", "A145"),
        )

    def drift_status(self) -> dict[str, Any]:
        """Read-only projection for status surfaces."""
        report = self._last_drift_report
        return {
            "checked": bool(report),
            "checked_at": report.get("checked_at", ""),
            "clean": report.get("clean"),
            "drift_count": report.get("drift_count", 0),
            "notice_id": _DRIFT_NOTICE_ID,
        }


__all__ = ["XingchengCodexDriftMixin", "DRIFT_CHECK_EVERY_CYCLES"]
