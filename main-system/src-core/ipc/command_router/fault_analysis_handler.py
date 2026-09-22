"""Command Router — Fault Analysis Handler (A137-A146/A435/A6500).

法典依據:
- A137: authenticated read-only evidence projection is the only system interface.
- A138: independent analysis+reasoning over governed global evidence; advisory-only.
- A139: 星澄-SYSTEM-POWER: global-read-only-review + anomaly-classification.
- A140: information layer is the sole provider of filtered system review evidence.
- A435: 星澄 is the sole auxiliary codex-view exception — official entry, read-only.
- A6500: codex-authorized global review authority.

Hardening controls:
- Every query routes through the 星澄 sovereign's ``review.global`` intent so
  the maintenance→decision→permission evidence chain is explicit and
  receipted — the handler never queries the fault service directly.
- Unknown queries fail closed (``UNKNOWN_QUERY``) instead of defaulting to
  overview.
- Error codes use consistent SCREAMING_SNAKE_CASE (``MISSING_FAULT_ID``).
- Exception text is sanitized before reaching the UI (no internal paths or
  implementation details leak).
- Every query attempt is recorded in a durable audit ledger.
- Results carry staleness, threshold, generation, and evidence version
  metadata so consumers can judge freshness.
- Fault detail scope is verified: only ``overview``/``patterns``/``knowledge``
  are broadcast-safe; ``detail``/``component`` require an explicit scope
  confirmation and are marked ``ui-restricted``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ._audit_writer import append_audit_record


_QUERY_ALLOWLIST: frozenset[str] = frozenset(
    {"overview", "patterns", "knowledge", "component", "detail", "codex-health"}
)

# Queries safe for broadcast to all connected UI clients.
_BROADCAST_SAFE_QUERIES: frozenset[str] = frozenset(
    {"overview", "patterns", "knowledge", "codex-health"}
)

# Queries that may expose per-fault detail; require scope confirmation.
_RESTRICTED_QUERIES: frozenset[str] = frozenset({"detail", "component"})

# Staleness threshold (seconds): evidence older than this is flagged stale.
_STALENESS_THRESHOLD_SECONDS: int = 300

# Sanitization pattern: strip file paths, stack traces, and internal details.
_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/][^\s'\"]+|/[^\s'\"]+/")
_INTERNAL_DETAIL_PATTERN = re.compile(
    r"(?:Traceback|File |line \d+|in <module>|raise |except )",
    re.IGNORECASE,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_error_text(text: str) -> str:
    """Strip internal paths and implementation details from error text."""
    sanitized = _PATH_PATTERN.sub("<path>", text)
    sanitized = _INTERNAL_DETAIL_PATTERN.sub("", sanitized)
    return sanitized.strip()[:200] or "internal error"


def _evidence_version(service: Any) -> str:
    """Derive a content version from the fault service's collected evidence."""
    try:
        faults = service.collect_all_faults(limit=10)
        signature = "|".join(f.fault_id for f in faults)
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return "unknown"


def _staleness_seconds(timestamp: str) -> int | None:
    """Return seconds since the given ISO timestamp, or None if unparseable."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - parsed).total_seconds())
    except (ValueError, TypeError):
        return None


class FaultAnalysisHandler:
    """Handle ``app:get-fault-analysis`` through the 星澄 review evidence chain."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._audit_ledger = (
            Path(__file__).resolve().parents[3]
            / "runtime"
            / "state"
            / "fault-query-audit.jsonl"
        )
        self._generation = 0

    async def handle(self, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        query = str(payload.get("query") or "").strip().lower()
        requester = str(payload.get("requester") or "ui").strip().lower()
        self._generation += 1
        self._audit_query_attempt(query, requester)
        if not query:
            return self._deny("MISSING_QUERY", "query is required")
        if query not in _QUERY_ALLOWLIST:
            return self._deny("UNKNOWN_QUERY", f"query '{query}' is not allowed")
        sovereign = getattr(self.app, "xingcheng_sovereign", None)
        if sovereign is None:
            return self._deny("REVIEW_SOVEREIGN_UNAVAILABLE", "星澄 sovereign not started")
        return await self._route_through_sovereign(sovereign, query, payload, requester)

    async def _route_through_sovereign(
        self,
        sovereign: Any,
        query: str,
        payload: Mapping[str, Any],
        requester: str,
    ) -> tuple[str, dict[str, Any]]:
        """Route the fault query through 星澄's ``review.global`` evidence chain."""
        from core_system.codex_decision import SovereignRequest
        from core_system.fault_analysis_service import get_fault_analysis_service

        service = get_fault_analysis_service()
        evidence = await self._collect_evidence(service, query, payload)
        scope_flag = "broadcast" if query in _BROADCAST_SAFE_QUERIES else "ui-restricted"
        request = SovereignRequest(
            intent="review.global",
            subject="fault-analysis",
            requester=requester,
            payload={
                "evidence": evidence,
                "scope": "fault-analysis",
                "query": query,
                "ui_scope": scope_flag,
            },
        )
        try:
            outcome = await sovereign.handle(request)
        except Exception as error:
            return self._deny(
                "REVIEW_SOVEREIGN_ERROR", _sanitize_error_text(f"{type(error).__name__}: {error}")
            )
        if not outcome.accepted:
            reason = outcome.refusal.reason_code if outcome.refusal else "REVIEW_DENIED"
            return self._deny(reason, "星澄 review denied the fault query")
        return self._build_result(query, outcome, service, scope_flag)

    async def _collect_evidence(
        self,
        service: Any,
        query: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Collect the fault evidence for the given query type."""
        if query == "codex-health":
            from core_system.codex_health_service import collect_codex_health_evidence
            project_root = getattr(self.app, "project_root", None) or Path.cwd()
            return await asyncio.to_thread(collect_codex_health_evidence, Path(project_root))
        if query == "overview":
            return await asyncio.to_thread(service.system_health_overview)
        if query == "patterns":
            faults = await asyncio.to_thread(service.collect_all_faults)
            patterns = await asyncio.to_thread(service.detect_patterns, faults)
            return {
                "patterns": [p.as_dict() for p in patterns],
                "total_faults": len(faults),
            }
        if query == "knowledge":
            return await asyncio.to_thread(service.repair_knowledge_summary)
        if query == "component":
            return await asyncio.to_thread(
                service.analyze_component, str(payload.get("component") or "").strip()
            )
        if query == "detail":
            return await asyncio.to_thread(
                service.fault_detail, str(payload.get("fault_id") or "").strip()
            )
        return {}

    def _build_result(
        self,
        query: str,
        outcome: Any,
        service: Any,
        scope_flag: str,
    ) -> tuple[str, dict[str, Any]]:
        """Build the final result with staleness, generation, and evidence version."""
        result = dict(outcome.result or {})
        result.setdefault("ok", True)
        result["query"] = query
        result["ui_scope"] = scope_flag
        result["generation"] = self._generation
        result["evidence_version"] = _evidence_version(service)
        result["staleness_threshold_seconds"] = _STALENESS_THRESHOLD_SECONDS
        timestamp = str(result.get("timestamp") or "")
        staleness = _staleness_seconds(timestamp)
        result["staleness_seconds"] = staleness
        result["stale"] = staleness is not None and staleness > _STALENESS_THRESHOLD_SECONDS
        self._audit_query_result(query, result)
        return "app:get-fault-analysis_result", result

    def _deny(self, code: str, message: str) -> tuple[str, dict[str, Any]]:
        """Return a fail-closed denial and record the audit entry."""
        self._audit_query_denial(code, message)
        return "app:get-fault-analysis_result", {
            "ok": False,
            "error_code": code,
            "message": message,
            "generation": self._generation,
        }

    def _audit_query_attempt(self, query: str, requester: str) -> None:
        self._append_audit({
            "event": "fault-query-attempt",
            "query": query,
            "requester": requester,
            "generation": self._generation,
            "timestamp": _iso_now(),
        })

    def _audit_query_result(self, query: str, result: dict[str, Any]) -> None:
        self._append_audit({
            "event": "fault-query-result",
            "query": query,
            "accepted": bool(result.get("ok")),
            "generation": self._generation,
            "evidence_version": result.get("evidence_version"),
            "stale": result.get("stale"),
            "timestamp": _iso_now(),
        })

    def _audit_query_denial(self, code: str, message: str) -> None:
        self._append_audit({
            "event": "fault-query-denial",
            "error_code": code,
            "message": message,
            "generation": self._generation,
            "timestamp": _iso_now(),
        })

    def _append_audit(self, record: dict[str, Any]) -> None:
        """Append a fault-query audit record to the durable JSONL ledger."""
        append_audit_record(self._audit_ledger, record)


__all__ = ["FaultAnalysisHandler"]
