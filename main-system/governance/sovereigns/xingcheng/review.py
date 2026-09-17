"""Xingcheng Sovereign — Auxiliary Review Module (A137-A146).

Read-only/advisory review capabilities transferred to 星澄.
Separate and non-transitive from owned-domain powers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .._base import SovereignBase, SovereignRequest, SovereignOutcome
from core_system.codex_decision import accepted_outcome, refusal_outcome
from .review_language import XingchengLanguageReviewMixin
from .review_inspect import XingchengInspectMixin

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.review")

from .review_constants import (
    _REVIEW_FORBIDDEN_KEYS,
    _PERMISSION_REVIEW_ASPECTS,
    _CLASSIFY_KINDS,
    ALLOWED_LANGUAGES,
    _LANGUAGE_EXTENSIONS,
    _FILE_LINE_WARNING_THRESHOLD,
)


class XingchengReviewMixin(XingchengLanguageReviewMixin, XingchengInspectMixin):
    """Auxiliary review group — read-only, advisory, traceable."""

    _reviews: dict[str, dict[str, Any]]
    _pending_anomalies: list[dict[str, Any]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reviews = {}
        self._pending_anomalies = []

    def _verify_review_read_only(self, request: SovereignRequest) -> SovereignOutcome | None:
        """Fail-closed: auxiliary review must be read-only."""
        forbidden = _REVIEW_FORBIDDEN_KEYS.intersection(
            key for key, value in request.payload.items() if value
        )
        if forbidden:
            return refusal_outcome(
                "REVIEW_MUST_BE_READ_ONLY",
                self.verified_basis("A137", "A139"),
            )
        return None

    async def adjudicate_review(self, request: SovereignRequest) -> SovereignOutcome:
        """Route auxiliary review intents."""
        block = self._verify_review_read_only(request)
        if block:
            return block

        intent = request.intent
        if intent == "review.global":
            return await self._adjudicate_review_global(request)
        if intent == "review.classify-anomaly":
            return await self._adjudicate_classify_anomaly(request)
        if intent == "review.notify-user":
            return await self._adjudicate_notify_user(request)
        if intent == "review.language":
            return await self._adjudicate_language_review(request)
        if intent == "review.codex-drift":
            return await self._adjudicate_codex_drift(request)
        if intent == "review.permission":
            return await self._adjudicate_permission_review(request)
        if intent == "inspect.layer":
            return await self._adjudicate_inspect(request)
        if intent == "codex.read":
            return await self._adjudicate_codex_read(request)
        if intent.startswith("classify."):
            return await self._adjudicate_classify(request)
        if intent == "star.adjudicate-classification":
            return await self._adjudicate_star_classification(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A139"))

    async def _adjudicate_review_global(self, request: SovereignRequest) -> SovereignOutcome:
        """A139/A145: review filtered global evidence; advisory report."""
        evidence = request.payload.get("evidence") or {}
        scope = request.payload.get("scope") or "all-system-domains"
        anomalies = self._classify_evidence(evidence)
        patterns = self._detect_anomaly_patterns(anomalies)
        report_id = f"review-{len(self._reviews) + 1}"
        self._reviews[report_id] = {
            "kind": "global-review",
            "scope": scope,
            "anomalies": anomalies,
            "patterns": patterns,
            "advisory": True,
            "reviewed_at": self._iso_now(),
        }
        if anomalies:
            self._pending_anomalies.extend(anomalies)
        return accepted_outcome(
            {
                "action": "global-review",
                "advisory": True,
                "report_id": report_id,
                "scope": scope,
                "anomalies": anomalies,
                "patterns": patterns,
            },
            self.verified_basis("A139", "A145", "A140"),
        )

    def _classify_evidence(self, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        """A139: classify anomalies from information-layer evidence."""
        anomalies: list[dict[str, Any]] = []
        items = evidence.get("items")
        if not isinstance(items, list):
            return anomalies
        for item in items:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "").lower()
            if state in {"healthy", "normal", "ok", "ready"}:
                continue
            if state in {"critical", "failed", "corrupt", "down"}:
                severity = "critical"
            elif state in {"unknown", "unreachable", "missing"}:
                severity = "warning"
            else:
                severity = "info" if state in {"degraded", "reviewing"} else "warning"

            # Enhanced classification with component type
            component_type = item.get("type", "unknown")
            component = item.get("component")

            anomaly = {
                "type": "global-review-finding",
                "severity": severity,
                "component": component,
                "component_type": component_type,
                "state": state or None,
                "detail": item.get("detail"),
                "location": item.get("component"),
                "source": item.get("source", "information-layer"),
            }

            # Add metrics if present
            if "metrics" in item:
                anomaly["metrics"] = item["metrics"]

            anomalies.append(anomaly)
        return anomalies

    async def _adjudicate_classify_anomaly(self, request: SovereignRequest) -> SovereignOutcome:
        anomaly = request.payload.get("anomaly") or {}
        classification = self._classify_evidence({"items": [anomaly]})
        result = classification[0] if classification else {
            "type": "global-review-finding",
            "severity": "info",
            "component": anomaly.get("component") if isinstance(anomaly, dict) else None,
        }
        return accepted_outcome(
            {"action": "classify-anomaly", "advisory": True, "result": result},
            self.verified_basis("A139", "A138"),
        )

    async def _adjudicate_notify_user(self, request: SovereignRequest) -> SovereignOutcome:
        """A139/A140/A146: user notification via main-system card."""
        message = request.payload.get("message") or {}
        anomaly = request.payload.get("anomaly")
        if anomaly:
            self._pending_anomalies.append(anomaly)
            await self._notify_anomalies()
        card = {
            "card": "main-system-status",
            "severity": anomaly.get("severity") if isinstance(anomaly, dict) else None or "info",
            "message": message,
            "location": anomaly.get("location") if isinstance(anomaly, dict) else None,
        }
        return accepted_outcome(
            {"action": "notify-user", "advisory": True, "notification": card},
            self.verified_basis("A139", "A140", "A146"),
        )

    async def _notify_anomalies(self) -> None:
        """Persist anomalies in owned domain for traceability."""
        if not self._pending_anomalies:
            return
        try:
            anomaly_path = Path(self._owned_domain_root) / "anomalies.jsonl"
            anomaly_path.parent.mkdir(parents=True, exist_ok=True)
            for a in self._pending_anomalies:
                anomaly_path.write_text(
                    json.dumps(a, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
        except OSError:
            pass
        self._pending_anomalies.clear()

    def _detect_anomaly_patterns(self, anomalies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Detect recurring patterns in anomalies (A139 advisory)."""
        patterns = []
        if not anomalies:
            return patterns

        # Group by component
        by_component: dict[str, list[dict]] = {}
        for a in anomalies:
            comp = a.get("component") or "unknown"
            by_component.setdefault(comp, []).append(a)

        for comp, comp_anomalies in by_component.items():
            if len(comp_anomalies) >= 3:
                # Recurring issue
                severities = [a.get("severity") for a in comp_anomalies]
                if severities.count("critical") >= 2:
                    patterns.append({
                        "type": "recurring-critical",
                        "component": comp,
                        "count": len(comp_anomalies),
                        "severity": "critical",
                        "detail": f"Component {comp} has {len(comp_anomalies)} anomalies with {severities.count('critical')} critical",
                    })
                elif len(comp_anomalies) >= 5:
                    patterns.append({
                        "type": "recurring-degraded",
                        "component": comp,
                        "count": len(comp_anomalies),
                        "severity": "warning",
                        "detail": f"Component {comp} has {len(comp_anomalies)} anomalies",
                    })

            # Check for escalating severity
            severity_rank = {"info": 0, "warning": 1, "critical": 2}
            ranked = sorted([severity_rank.get(a.get("severity"), 0) for a in comp_anomalies])
            if len(ranked) >= 3 and ranked[-1] > ranked[0]:
                patterns.append({
                    "type": "escalating-severity",
                    "component": comp,
                    "detail": f"Severity escalating from {ranked[0]} to {ranked[-1]}",
                })

        return patterns

    async def _adjudicate_classify(self, request: SovereignRequest) -> SovereignOutcome:
        """A337: deterministic auxiliary classification."""
        kind = request.intent.split(".", 1)[1]
        if kind not in _CLASSIFY_KINDS:
            return refusal_outcome("UNKNOWN_CLASSIFY_KIND", self.verified_basis("A337"))
        item = request.payload.get("item")
        normalized = self._normalize_classification_input(kind, item)
        record = {
            "kind": f"classify-{kind}",
            "classification": normalized,
            "classifier": "auxiliary-deterministic",
            "classified_at": self._iso_now(),
        }
        self._reviews[f"classify-{len(self._reviews) + 1}"] = record
        return accepted_outcome(
            {
                "action": f"classify-{kind}",
                "classification": normalized,
                "classifier": "auxiliary-deterministic",
                "semantic_judgment": "native-model-exclusive",
            },
            self.verified_basis("A337"),
        )

    def _normalize_classification_input(self, kind: str, item: Any) -> dict[str, Any]:
        text = str(item or "")
        lowered = text.casefold()
        if kind == "code":
            suffix = Path(text).suffix.lower() if "." in text else ""
            language = next(
                (lang for lang, exts in _LANGUAGE_EXTENSIONS.items() if suffix in exts),
                "unknown",
            )
            return {"language": language, "extension": suffix or None}
        if kind == "fault":
            state = lowered
            severity = "info"
            if any(k in state for k in ("critical", "fail", "corrupt", "down")):
                severity = "critical"
            elif any(k in state for k in ("unknown", "missing", "unreachable", "timeout")):
                severity = "warning"
            elif any(k in state for k in ("degrad", "slow", "retry")):
                severity = "info"
            return {"severity": severity, "raw": text[:200]}
        if kind == "intent":
            verb = lowered.split(".", 1)[0] if "." in lowered else lowered.split("-", 1)[0]
            return {"verb": verb or None, "raw": text[:200]}
        return {"kind": kind, "raw": text[:200], "normalized": lowered[:200]}

    async def _adjudicate_star_classification(self, request: SovereignRequest) -> SovereignOutcome:
        """A337 STAR-ADJUDICATION: resolve auxiliary vs native-model classification."""
        auxiliary = request.payload.get("auxiliary") or {}
        native = request.payload.get("native") or {}
        conflict = auxiliary != native
        final = dict(auxiliary)
        final.update({k: v for k, v in native.items() if v is not None})
        finding = {
            "finding_id": f"star-finding-{len(self._reviews) + 1}",
            "conflict": conflict,
            "final": final,
            "basis": "auxiliary=schema+identity; native=semantic",
            "issued_at": self._iso_now(),
        }
        self._reviews[finding["finding_id"]] = {"kind": "star-adjudication", **finding}
        return accepted_outcome(
            {"action": "star-adjudication", "finding": finding},
            self.verified_basis("A337"),
        )