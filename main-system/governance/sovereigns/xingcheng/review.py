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

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.review")

# A137/A139: payload keys that would turn a review into a system effect
_REVIEW_FORBIDDEN_KEYS = frozenset({
    "execute", "write", "delete", "modify",
    "system_target", "operation", "target_path",
})

# A319: aspects examined for every permission request
_PERMISSION_REVIEW_ASPECTS = (
    "codex", "identity", "scope", "purpose",
    "least-privilege", "separation", "expiry", "risk", "current-evidence",
)

# A337: deterministic classification categories
_CLASSIFY_KINDS = (
    "intent", "task", "code", "fault", "evidence", "result",
)

# Language-review capability (transferred from abolished language-review-sub-sovereign)
ALLOWED_LANGUAGES = ("python", "typescript", "cpp", "c", "csharp", "sql")
_LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "typescript": {".ts", ".tsx"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hh"},
    "c": {".c", ".h"},
    "csharp": {".cs"},
    "sql": {".sql"},
}
_FILE_LINE_WARNING_THRESHOLD = 1000


class XingchengReviewMixin:
    """Auxiliary review group — read-only, advisory, traceable."""

    _reviews: dict[str, dict[str, Any]]
    _pending_anomalies: list[dict[str, Any]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reviews = {}
        self._pending_anomalies = []

    def _iso_now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

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
        report_id = f"review-{len(self._reviews) + 1}"
        self._reviews[report_id] = {
            "kind": "global-review",
            "scope": scope,
            "anomalies": anomalies,
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
            anomalies.append({
                "type": "global-review-finding",
                "severity": severity,
                "component": item.get("component"),
                "state": state or None,
                "detail": item.get("detail"),
                "location": item.get("component"),
            })
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

    async def _adjudicate_language_review(self, request: SovereignRequest) -> SovereignOutcome:
        """Language conformance review (transferred capability, advisory only)."""
        language = str(request.payload.get("language") or "").lower()
        if language not in ALLOWED_LANGUAGES:
            return refusal_outcome("LANGUAGE_NOT_ALLOWED", self.verified_basis("A139"))
        files = request.payload.get("files") or []
        findings: list[dict[str, Any]] = []
        for entry in files:
            findings.extend(self._review_file(str(entry), language))
        review_id = f"language-review-{len(self._reviews) + 1}"
        self._reviews[review_id] = {
            "kind": "language-review",
            "language": language,
            "file_count": len(files),
            "findings": findings,
            "advisory": True,
            "reviewed_at": self._iso_now(),
        }
        return accepted_outcome(
            {
                "action": "language-review",
                "advisory": True,
                "review_id": review_id,
                "language": language,
                "file_count": len(files),
                "findings": findings,
            },
            self.verified_basis("A139", "A145"),
        )

    def _review_file(self, raw_path: str, language: str) -> list[dict[str, Any]]:
        """Read-only per-file conformance evidence."""
        findings: list[dict[str, Any]] = []
        path = Path(raw_path)
        if not path.exists():
            return [{
                "file": raw_path, "rule": "file-exists",
                "severity": "error", "detail": "file not found",
            }]
        expected = _LANGUAGE_EXTENSIONS[language]
        if path.suffix.lower() not in expected:
            findings.append({
                "file": raw_path,
                "rule": "language-extension",
                "severity": "warning",
                "detail": f"extension {path.suffix!r} not canonical for {language} ({sorted(expected)})",
            })
        try:
            line_count = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            line_count = -1
        if line_count > _FILE_LINE_WARNING_THRESHOLD:
            findings.append({
                "file": raw_path,
                "rule": "module-size",
                "severity": "warning",
                "detail": f"{line_count} lines exceeds {_FILE_LINE_WARNING_THRESHOLD}",
            })
        return findings

    async def _adjudicate_codex_read(self, request: SovereignRequest) -> SovereignOutcome:
        """A144/A435: 星澄-only official-entry codex read (review basis)."""
        scope = request.payload.get("scope") or "global-review"
        return accepted_outcome(
            {
                "codex_view": "official-entry",
                "mode": "read-only",
                "scope": scope,
                "session": "single-use",
                "audit": True,
                "permission_review": "exempt",
            },
            self.verified_basis("A435", "A144", "A145"),
        )

    async def _adjudicate_permission_review(self, request: SovereignRequest) -> SovereignOutcome:
        """A319: independent privileged read-only examination of permission request."""
        payload = request.payload
        aspects: dict[str, str] = {}
        aspects["codex"] = "present" if payload.get("basis") or payload.get("codex_ref") else "missing"
        aspects["identity"] = "present" if payload.get("actor") else "missing"
        aspects["scope"] = "present" if payload.get("scope") else "missing"
        aspects["purpose"] = "present" if payload.get("purpose") else "missing"
        aspects["least-privilege"] = "present" if payload.get("least_privilege") else "missing"
        aspects["separation"] = "present" if payload.get("separation") else "missing"
        aspects["expiry"] = "present" if payload.get("expiry") else "missing"
        aspects["risk"] = "present" if payload.get("risk") else "missing"
        aspects["current-evidence"] = "present" if payload.get("evidence") else "missing"
        missing = [k for k, v in aspects.items() if v == "missing"]
        if not payload.get("actor") or not payload.get("capability") or not payload.get("target"):
            finding = "require-change"
        elif aspects["current-evidence"] == "missing" or aspects["risk"] == "missing":
            finding = "deny-objection"
        elif missing:
            finding = "require-change"
        else:
            finding = "pass"
        review_id = f"permission-review-{len(self._reviews) + 1}"
        record = {
            "kind": "permission-review",
            "finding": finding,
            "aspects": aspects,
            "missing": missing,
            "subject": {
                "actor": payload.get("actor"),
                "capability": payload.get("capability"),
                "target": payload.get("target"),
            },
            "advisory": True,
            "confidential": True,
            "reviewed_at": self._iso_now(),
            "expires_at": payload.get("expiry"),
        }
        self._reviews[review_id] = record
        return accepted_outcome(
            {
                "action": "permission-review",
                "review_id": review_id,
                "finding": finding,
                "aspects": aspects,
                "evidence": record["subject"],
                "expiry": payload.get("expiry"),
                "note": "decision-sovereign may decide only after current 星澄 review",
            },
            self.verified_basis("A319"),
        )

    async def _adjudicate_inspect(self, request: SovereignRequest) -> SovereignOutcome:
        """A330: free-entry confidential read-only inspection."""
        layer = str(request.payload.get("layer") or "unspecified")
        inspection_id = f"inspect-{len(self._reviews) + 1}"
        view: dict[str, Any] = {}
        app_root = getattr(self.app, "project_root", None)
        if app_root:
            safe_name = "".join(c for c in layer if c.isalnum() or c in ("-", "_"))
            state_path = Path(app_root) / "main-system" / "runtime" / "state" / f"{safe_name}.json"
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    view = payload
            except (OSError, UnicodeError, json.JSONDecodeError):
                view = {}
        self._reviews[inspection_id] = {
            "kind": "layer-inspection",
            "layer": layer,
            "access": "read-only",
            "confidential": True,
            "view_keys": sorted(view.keys()),
            "inspected_at": self._iso_now(),
        }
        return accepted_outcome(
            {
                "action": "inspect",
                "inspection_id": inspection_id,
                "layer": layer,
                "access": "confidential-read-only",
                "mutating": False,
                "executing": False,
                "preapproval": "none-required",
                "continuity": "inspection-cannot-interrupt-or-alter-state",
                "view": view,
            },
            self.verified_basis("A330"),
        )

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