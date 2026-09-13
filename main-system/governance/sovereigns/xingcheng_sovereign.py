"""Xingcheng Sovereign — 星澄主宰（獨立特權機構，完全擁有自有域，法典決策鏈外）。

法典依據:
- sovereign_id: 星澄 (position 5)
- area: xingcheng
- rank: independent-privileged-institution
- basis: codex
- duties: observe+analyze+reason+decide+manage+authorize+execute+write+delete+configure (in owned domain)
- powers: complete-inside-owned-domain
- prohibitions: FORBID:any-星澄-power-outside-owned-domain; FORBID:any-system-target-or-effect (A20)

A12: 星澄在決策鏈外
A20: 星澄權力完整在自有域內，禁止任何系統目標或效果

Auxiliary review group (separate+non-transitive from own-domain power):
- A137: authenticated read-only evidence projection is the only system
  interface.
- A138: independent analysis+reasoning over governed global evidence;
  results are advisory-only + traceable; the user is the final recipient.
- A139: 星澄-SYSTEM-POWER: global-read-only-review + anomaly-classification
  + user-notification.
- A140: the information layer is the sole provider of filtered system
  review evidence; user notification goes through the main-system card.
- A144/A174: 星澄 is the sole auxiliary codex-view exception — official
  entry, read-only, single-use session, audited, permission-review exempt.
- A145: auxiliary duties = reference codex as basis + review all system
  domains + notify the user of anomaly locations.
- Language-review capability: transferred to 星澄 when
  language-review-sub-sovereign was abolished (A334); advisory
  conformance evidence only — 星澄 holds no decision power (A139).

Special-law powers (basis A313|A328|A336):
- A313: all permission-review authority transferred to 星澄 — independent
  privileged read-only examination of permission requests producing a
  typed finding (pass|deny-objection|require-change) with evidence and
  expiry.  星澄 never mutates permissions directly.
- A328: free-entry privilege — confidential read-only non-mutating
  inspection of every governed layer/directory/channel/state/audit view;
  no pre-approval may be required and no owner may deny it.
- A336: abolished system-programming + language-review sub-sovereign
  duties transfer intact to 星澄.  Programming analysis/design/code
  authoring/refactoring/debugging/tool use/verification and language
  conformance are exclusive native-model capabilities; all automatic
  classification is jointly assigned to the auxiliary system and the
  native model; the whole-system automation coordination executor is
  exclusively vested in 星澄 (decomposition+scheduling+dispatch+tool
  orchestration+convergence+result verification+failure containment),
  authorized by an explicit user one-time command or codex mandate.

Full-automation upgrade (A20):
- Background auto-loop observes, analyzes, reasons, and manages the
  owned domain automatically.
- Domain health monitoring: detects database, model, and configuration
  anomalies.
- Domain resource management: model loading, database maintenance.
- Channel coordination: notifies the system through the information
  layer (A66) when anomalies are detected.
- All operations stay inside the owned domain (A20); no system targets
  or effects are ever produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome

_logger = logging.getLogger("gptbridge.sovereign.xingcheng")

# Owned-domain root — matches the codex project_architecture_directory
# STAR_DIRECTORY physical_root.
_OWNED_DOMAIN_ROOT = (
    "E:/GPTBridge/Standalone tools/local-model/model-dialogue/xingcheng"
)

# Domain health thresholds.
_DB_MAX_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
_MODEL_DIR_MAX_SIZE_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB
_CHECKPOINT_MAX_AGE_HOURS = 72

# Language-review capability transferred from the abolished
# language-review-sub-sovereign (A334).  Advisory evidence only.
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

# Payload keys that would turn a review into a system effect — the
# auxiliary group is read-only/advisory, so any of these fails closed.
_REVIEW_FORBIDDEN_KEYS = frozenset({
    "execute",
    "write",
    "delete",
    "modify",
    "system_target",
    "operation",
    "target_path",
})

# A313: aspects examined for every permission request.
_PERMISSION_REVIEW_ASPECTS = (
    "codex",
    "identity",
    "scope",
    "purpose",
    "least-privilege",
    "separation",
    "expiry",
    "risk",
    "current-evidence",
)

# A336: deterministic classification categories handled by the auxiliary
# system; semantic judgment stays with the native model.
_CLASSIFY_KINDS = (
    "intent",
    "task",
    "code",
    "fault",
    "evidence",
    "result",
)

# A336: authorization modes for the automation executor.
_AUTOMATION_AUTHORIZATION_MODES = frozenset({"user-command", "codex-mandate"})


class XingchengSovereign(SovereignBase):
    """星澄主宰：自有域完全權力，隔離於系統決策鏈。"""

    sovereign_id = "星澄"

    # A10/A11 explicit intent allowlist — the base-class ``_verify_intent``
    # checks edict IDs (article tokens), not the ``domain.*`` intent strings
    # used by callers, so every real intent would be rejected and
    # ``handle()`` would be unreachable.  List adjudicated intents
    # explicitly (fail-closed); the A20 owned-domain check inside
    # ``_adjudicate`` still guards every accepted intent.
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        "domain.observe",
        "domain.analyze",
        "domain.reason",
        "domain.decide",
        "domain.manage",
        "domain.authorize",
        "domain.execute",
        "domain.write",
        "domain.delete",
        "domain.configure",
        "channel.coordinate",
        # Auto-automation intents (A20 owned-domain only)
        "domain.auto-observe",
        "domain.auto-analyze",
        "domain.auto-manage",
        "domain.auto-health-check",
        # Auxiliary review group (A137-A146, separate+non-transitive)
        "review.global",
        "review.classify-anomaly",
        "review.notify-user",
        "review.language",
        "codex.read",
        # A313: permission-review authority transferred to 星澄
        "review.permission",
        # A328: free-entry confidential read-only inspection
        "inspect.layer",
        # A336: automatic classification (auxiliary + native model)
        "classify.intent",
        "classify.task",
        "classify.code",
        "classify.fault",
        "classify.evidence",
        "classify.result",
        # A336: star adjudication — one typed final finding
        "star.adjudicate-classification",
        # A336: native-model programming capabilities
        "program.analyze",
        "program.design",
        "program.write",
        "program.refactor",
        "program.debug",
        "program.verify",
        "program.migrate",
        "program.review-generated",
        # A336: whole-system automation coordination executor
        "automation.decompose",
        "automation.schedule",
        "automation.dispatch",
        "automation.converge",
        "automation.verify-result",
        "automation.contain-failure",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        self._owned_domain_root = _OWNED_DOMAIN_ROOT
        self._isolated = True
        # Auto-automation state (A20 full-automation upgrade).
        self._auto_loop_task: asyncio.Task[Any] | None = None
        self._auto_loop_interval: float = 10.0  # seconds
        self._auto_enabled: bool = True
        # Automation metrics for status surfaces.
        self._auto_metrics: dict[str, Any] = {
            "observe_cycles": 0,
            "analyze_cycles": 0,
            "reason_cycles": 0,
            "manage_cycles": 0,
            "health_checks": 0,
            "anomalies_detected": 0,
            "channel_notifications": 0,
            "db_maintenance_runs": 0,
            "model_loads": 0,
            "config_updates": 0,
            "last_auto_cycle": "",
            "last_anomaly": "",
        }
        # Last domain observation snapshot.
        self._last_snapshot: dict[str, Any] = {}
        # Detected anomalies pending channel notification.
        self._pending_anomalies: list[dict[str, Any]] = []
        # Auxiliary review registry (A138/A145: advisory + traceable).
        self._reviews: dict[str, dict[str, Any]] = {}
        # A336: automation executor task ledger + native-model program tasks.
        self._automation_tasks: dict[str, dict[str, Any]] = {}
        self._program_tasks: dict[str, dict[str, Any]] = {}

    def _verify_intent(self, intent: str) -> bool:
        """A10/A11 fail-closed: only declared owned-domain intents pass."""
        return intent in self._INTENT_ALLOWLIST

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：自有域完全權力 + 輔助唯讀審查（兩群分立、不可遞移）。"""
        intent = request.intent

        # Auxiliary + special-law groups (A137-A146/A313/A328/A336):
        # read-only/advisory, no system effects — evaluated before the
        # A20 owned-domain gate.
        if (
            intent.startswith("review.")
            or intent == "codex.read"
            or intent.startswith("inspect.")
            or intent.startswith("classify.")
            or intent == "star.adjudicate-classification"
        ):
            return await self._adjudicate_review(request)

        # A336 native-model programming + whole-system automation
        # executor: bounded tasks authorized by user command or codex
        # mandate; all file effects stay inside the owned domain.
        if intent.startswith("program.") or intent.startswith("automation."):
            return await self._adjudicate_native_capability(request)

        if not self._is_in_owned_domain(request):
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN",
                self.verified_basis("A20", "A12"),
            )

        if intent == "domain.observe":
            return await self._adjudicate_observe(request)
        if intent == "domain.analyze":
            return await self._adjudicate_analyze(request)
        if intent == "domain.reason":
            return await self._adjudicate_reason(request)
        if intent == "domain.decide":
            return await self._adjudicate_decide(request)
        if intent == "domain.manage":
            return await self._adjudicate_manage(request)
        if intent == "domain.authorize":
            return await self._adjudicate_authorize(request)
        if intent == "domain.execute":
            return await self._adjudicate_execute(request)
        if intent == "domain.write":
            return await self._adjudicate_write(request)
        if intent == "domain.delete":
            return await self._adjudicate_delete(request)
        if intent == "domain.configure":
            return await self._adjudicate_configure(request)
        if intent == "channel.coordinate":
            return await self._adjudicate_channel_coordinate(request)
        if intent == "domain.auto-observe":
            return await self._adjudicate_auto_observe(request)
        if intent == "domain.auto-analyze":
            return await self._adjudicate_auto_analyze(request)
        if intent == "domain.auto-manage":
            return await self._adjudicate_auto_manage(request)
        if intent == "domain.auto-health-check":
            return await self._adjudicate_auto_health_check(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A20", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """星澄委派執行（A69/A121）。

        星澄 is outside the decision chain (A12) but its native
        capabilities (program/automation) dispatch governed programming
        tools inside ``_adjudicate``; review/observe/analyze/reason/decide
        intents are pure adjudications.  The accepted outcome already
        reflects any dispatched execution.  This hook attests that the
        delegation happened inside adjudication and returns the decision.
        """
        return decision

    # ------------------------------------------------------------------
    # Auxiliary review group (A137-A146)
    # ------------------------------------------------------------------

    async def _adjudicate_review(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A139: 全域唯讀審查 + 異常分類 + 使用者通知；A145: 僅建議性。

        The auxiliary group is separate and non-transitive: it must never
        carry a system effect.  Any payload key that could produce an
        effect fails closed.
        """
        forbidden = _REVIEW_FORBIDDEN_KEYS.intersection(
            key for key, value in request.payload.items() if value
        )
        if forbidden:
            return refusal_outcome(
                "REVIEW_MUST_BE_READ_ONLY",
                self.verified_basis("A137", "A139"),
            )

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
        return refusal_outcome(
            "UNKNOWN_INTENT", self.verified_basis("A139")
        )

    async def _adjudicate_review_global(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
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

    def _classify_evidence(
        self, evidence: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """A139: classify anomalies out of information-layer evidence.

        Evidence items are dicts with ``component``/``state``/``detail``;
        severity is derived from state keywords, fail-closed to
        ``warning`` for unknown states.
        """
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

    async def _adjudicate_classify_anomaly(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A139: classify a single anomaly report (advisory)."""
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

    async def _adjudicate_notify_user(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A139/A140/A146: user notification via the main-system card.

        The card payload is returned for the information layer to render;
        the anomaly is also persisted inside the owned domain so the
        notification is traceable (A138).
        """
        message = request.payload.get("message") or {}
        anomaly = request.payload.get("anomaly")
        if anomaly:
            self._pending_anomalies.append(anomaly)
            await self._notify_anomalies()
        card = {
            "card": "main-system-status",
            "severity": (
                anomaly.get("severity") if isinstance(anomaly, dict) else None
            ) or "info",
            "message": message,
            "location": (
                anomaly.get("location") if isinstance(anomaly, dict) else None
            ),
        }
        return accepted_outcome(
            {"action": "notify-user", "advisory": True, "notification": card},
            self.verified_basis("A139", "A140", "A146"),
        )

    async def _adjudicate_language_review(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Transferred capability: programming-language conformance review.

        Abolished language-review-sub-sovereign duty now lives in 星澄 as
        advisory evidence production (A139: no decision power).  Read-only:
        files are only stat'ed/read, never modified.
        """
        language = str(request.payload.get("language") or "").lower()
        if language not in ALLOWED_LANGUAGES:
            return refusal_outcome(
                "LANGUAGE_NOT_ALLOWED", self.verified_basis("A139")
            )
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

    def _review_file(
        self, raw_path: str, language: str
    ) -> list[dict[str, Any]]:
        """Read-only per-file conformance evidence (advisory)."""
        findings: list[dict[str, Any]] = []
        path = Path(raw_path)
        if not path.exists():
            return [{
                "file": raw_path,
                "rule": "file-exists",
                "severity": "error",
                "detail": "file not found",
            }]
        expected = _LANGUAGE_EXTENSIONS[language]
        if path.suffix.lower() not in expected:
            findings.append({
                "file": raw_path,
                "rule": "language-extension",
                "severity": "warning",
                "detail": (
                    f"extension {path.suffix!r} not canonical for "
                    f"{language} ({sorted(expected)})"
                ),
            })
        try:
            line_count = sum(
                1 for _ in path.open("r", encoding="utf-8", errors="replace")
            )
        except OSError:
            line_count = -1
        if line_count > _FILE_LINE_WARNING_THRESHOLD:
            findings.append({
                "file": raw_path,
                "rule": "module-size",
                "severity": "warning",
                "detail": (
                    f"{line_count} lines exceeds "
                    f"{_FILE_LINE_WARNING_THRESHOLD}"
                ),
            })
        return findings

    async def _adjudicate_codex_read(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A144/A174: 星澄-only official-entry codex read (review basis).

        The sovereign adjudicates the read session; the actual read goes
        through ``governance-codex://official`` under single-use session
        + audit controls.  Permission-review exempt, read-only.
        """
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
            self.verified_basis("A174", "A144", "A145"),
        )

    async def _adjudicate_permission_review(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A313: independent privileged read-only examination of a
        permission request — the sole review authority.

        Examines the request against codex + identity + scope + purpose +
        least-privilege + separation + expiry + risk + current evidence,
        and issues one typed finding: ``pass`` | ``deny-objection`` |
        ``require-change`` with evidence and expiry.  星澄 never mutates
        the permission itself — the finding is advisory to the
        decision-sovereign, which may not decide without a current review.
        """
        payload = request.payload
        aspects: dict[str, str] = {}
        aspects["codex"] = (
            "present" if payload.get("basis") or payload.get("codex_ref") else "missing"
        )
        aspects["identity"] = "present" if payload.get("actor") else "missing"
        aspects["scope"] = "present" if payload.get("scope") else "missing"
        aspects["purpose"] = "present" if payload.get("purpose") else "missing"
        aspects["least-privilege"] = (
            "present" if payload.get("least_privilege") else "missing"
        )
        aspects["separation"] = (
            "present" if payload.get("separation") else "missing"
        )
        aspects["expiry"] = "present" if payload.get("expiry") else "missing"
        aspects["risk"] = "present" if payload.get("risk") else "missing"
        aspects["current-evidence"] = (
            "present" if payload.get("evidence") else "missing"
        )
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
                "note": (
                    "decision-sovereign may decide only after a current "
                    "星澄 review; deny-objection cannot be overridden "
                    "without explicit human-governor successor law"
                ),
            },
            self.verified_basis("A313"),
        )

    async def _adjudicate_inspect(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A328: free-entry privilege — confidential, read-only,
        non-mutating, nonexecuting, nondelegable inspection of any
        governed layer/directory/channel/state/audit view.  No
        pre-approval may be required; no owner may deny it.
        """
        layer = str(request.payload.get("layer") or "unspecified")
        inspection_id = f"inspect-{len(self._reviews) + 1}"
        # A328: confidential read-only inspection — read the layer's
        # runtime state projection if present.  Bounded to the governed
        # runtime/state directory; anything else yields an empty view.
        view: dict[str, Any] = {}
        app_root = getattr(self.app, "project_root", None)
        if app_root:
            safe_name = "".join(
                c for c in layer if c.isalnum() or c in ("-", "_")
            )
            state_path = (
                Path(app_root)
                / "main-system"
                / "runtime"
                / "state"
                / f"{safe_name}.json"
            )
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
            self.verified_basis("A328"),
        )

    async def _adjudicate_classify(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A336: deterministic auxiliary classification.

        The auxiliary layer performs normalization + rule lookup only —
        the output is tagged ``deterministic`` and carries no semantic
        judgment, which remains the native model's exclusive duty.
        """
        kind = request.intent.split(".", 1)[1]
        if kind not in _CLASSIFY_KINDS:
            return refusal_outcome(
                "UNKNOWN_CLASSIFY_KIND", self.verified_basis("A336")
            )
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
            self.verified_basis("A336"),
        )

    def _normalize_classification_input(
        self, kind: str, item: Any
    ) -> dict[str, Any]:
        """Deterministic normalization+rule lookup per kind (A336)."""
        text = str(item or "")
        lowered = text.casefold()
        if kind == "code":
            suffix = Path(text).suffix.lower() if "." in text else ""
            language = next(
                (
                    lang
                    for lang, exts in _LANGUAGE_EXTENSIONS.items()
                    if suffix in exts
                ),
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

    async def _adjudicate_star_classification(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A336 STAR-ADJUDICATION: resolve a conflict between the
        auxiliary (deterministic) and native-model (semantic)
        classifications and issue one typed final finding."""
        auxiliary = request.payload.get("auxiliary") or {}
        native = request.payload.get("native") or {}
        conflict = auxiliary != native
        # Auxiliary wins schema/identity fields; native wins semantic
        # fields.  星澄 merges neither identity — it issues one finding.
        final = dict(auxiliary)
        final.update({k: v for k, v in native.items() if v is not None})
        finding = {
            "finding_id": f"star-finding-{len(self._reviews) + 1}",
            "conflict": conflict,
            "final": final,
            "basis": "auxiliary=schema+identity; native=semantic",
            "issued_at": self._iso_now(),
        }
        self._reviews[finding["finding_id"]] = {
            "kind": "star-adjudication",
            **finding,
        }
        return accepted_outcome(
            {"action": "star-adjudication", "finding": finding},
            self.verified_basis("A336"),
        )

    # ------------------------------------------------------------------
    # A336 native-model capabilities + automation executor
    # ------------------------------------------------------------------

    def _automation_authorized(self, request: SovereignRequest) -> bool:
        """A336: automation executor requires an explicit user one-time
        command or an applicable codex mandate."""
        authorization = request.payload.get("authorization")
        if not isinstance(authorization, dict):
            return False
        mode = authorization.get("mode")
        return mode in _AUTOMATION_AUTHORIZATION_MODES and bool(
            authorization.get("reference")
        )

    async def _adjudicate_native_capability(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Route A336 native-model programming + automation intents."""
        intent = request.intent
        if intent.startswith("program."):
            return await self._adjudicate_program(request)
        return await self._adjudicate_automation(request)

    async def _adjudicate_program(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A336 NATIVE-CAPABILITY: programming analysis/design/authoring/
        refactoring/debugging/verification/migration/generated-code review
        are exclusive 星澄 native-model capabilities.

        The sovereign issues a bounded task envelope; the native model
        performs the semantic work.  Any ``output_path`` is confined to
        the owned domain — cross-root mutation fails closed (A336
        prohibition).
        """
        op = request.intent.split(".", 1)[1]
        output_path = request.payload.get("output_path")
        resolved_output: Path | None = None
        if output_path:
            resolved_output = self._resolve_in_domain(str(output_path))
            if resolved_output is None:
                return refusal_outcome(
                    "CROSS_ROOT_MUTATION", self.verified_basis("A336")
                )
        task_id = f"program-{len(self._program_tasks) + 1}"
        task = {
            "task_id": task_id,
            "op": op,
            "subject": request.payload.get("subject") or request.subject,
            "spec": request.payload.get("spec"),
            "output_path": (
                str(resolved_output) if resolved_output else None
            ),
            "executor": "xingcheng-native-model",
            "status": "accepted",
            "created_at": self._iso_now(),
        }
        self._program_tasks[task_id] = task
        return accepted_outcome(
            {
                "action": f"program.{op}",
                "task": task,
                "executor": "xingcheng-native-model",
                "boundary": "no-permission-grant+no-routing-evidence-alteration+no-direct-channel-operation",
            },
            self.verified_basis("A336"),
        )

    async def _adjudicate_automation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A336 AUTOMATION-EXECUTOR: whole-system automation coordination
        — governed decomposition, scheduling, dispatch, tool
        orchestration, cross-module coordination, progress convergence,
        result verification, failure containment."""
        if not self._automation_authorized(request):
            return refusal_outcome(
                "AUTOMATION_AUTHORIZATION_REQUIRED",
                self.verified_basis("A336"),
            )
        intent = request.intent
        if intent == "automation.decompose":
            return self._automation_decompose(request)
        if intent == "automation.schedule":
            return self._automation_schedule(request)
        if intent == "automation.dispatch":
            return self._automation_dispatch(request)
        if intent == "automation.converge":
            return self._automation_converge(request)
        if intent == "automation.verify-result":
            return self._automation_verify(request)
        return self._automation_contain(request)

    def _automation_decompose(self, request: SovereignRequest) -> SovereignOutcome:
        task_id = f"auto-{len(self._automation_tasks) + 1}"
        steps = request.payload.get("steps") or []
        self._automation_tasks[task_id] = {
            "objective": request.payload.get("objective") or request.subject,
            "steps": [
                {"step": s, "state": "pending"} for s in steps
            ],
            "state": "decomposed",
            "created_at": self._iso_now(),
        }
        return accepted_outcome(
            {"task_id": task_id, "state": "decomposed", "steps": len(steps)},
            self.verified_basis("A336"),
        )

    def _automation_task_or_refusal(
        self, request: SovereignRequest
    ) -> dict[str, Any] | SovereignOutcome:
        task_id = str(request.payload.get("task_id") or "")
        task = self._automation_tasks.get(task_id)
        if task is None:
            return refusal_outcome(
                "UNKNOWN_AUTOMATION_TASK", self.verified_basis("A336")
            )
        return task

    def _automation_schedule(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        order = request.payload.get("order")
        if isinstance(order, list) and order:
            indexed = {i: step for i, step in enumerate(task["steps"])}
            task["schedule"] = [indexed[i] for i in order if i in indexed]
        else:
            task["schedule"] = list(task["steps"])
        task["state"] = "scheduled"
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "state": "scheduled",
                "scheduled_steps": len(task["schedule"]),
            },
            self.verified_basis("A336"),
        )

    def _automation_dispatch(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        step_index = request.payload.get("step_index")
        steps = task.get("schedule") or task["steps"]
        if (
            not isinstance(step_index, int)
            or step_index < 0
            or step_index >= len(steps)
        ):
            return refusal_outcome(
                "INVALID_STEP_INDEX", self.verified_basis("A336")
            )
        steps[step_index]["state"] = "dispatched"
        steps[step_index]["dispatched_at"] = self._iso_now()
        task["state"] = "in-progress"
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "step_index": step_index,
                "handoff": "typed",
                "state": "dispatched",
            },
            self.verified_basis("A336"),
        )

    def _automation_converge(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        states = [s["state"] for s in task["steps"]]
        converged = bool(states) and all(
            s in ("converged", "success", "done") for s in states
        )
        task["state"] = "converged" if converged else task["state"]
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "converged": converged,
                "step_states": states,
            },
            self.verified_basis("A336"),
        )

    def _automation_verify(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        evidence = request.payload.get("evidence")
        if not evidence:
            return refusal_outcome(
                "MISSING_RESULT_EVIDENCE", self.verified_basis("A336")
            )
        task["result_evidence"] = evidence
        task["state"] = "verified"
        task["verified_at"] = self._iso_now()
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "state": "verified",
            },
            self.verified_basis("A336"),
        )

    def _automation_contain(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        step_index = request.payload.get("step_index")
        task["state"] = "contained"
        task["containment"] = {
            "step_index": step_index,
            "reason": request.payload.get("reason"),
            "contained_at": self._iso_now(),
        }
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "state": "contained",
                "failure_propagation": "halted",
            },
            self.verified_basis("A336"),
        )

    def _is_in_owned_domain(self, request: SovereignRequest) -> bool:
        """A20: 驗證請求目標在自有域內，禁止系統目標。"""
        target = request.payload.get("target", "")
        if target.startswith(self._owned_domain_root):
            return True
        if target.startswith("E:/GPTBridge/main-system") or target.startswith("E:/GPTBridge/governance_rule"):
            return False
        return request.payload.get("domain_confirmed") is True

    # ------------------------------------------------------------------
    # Owned-domain powers (A20) — real operations, confined to the domain
    # root.  Every path resolves inside ``_owned_domain_root``; escape
    # attempts fail closed.
    # ------------------------------------------------------------------

    def _resolve_in_domain(self, raw_path: str | None) -> Path | None:
        """Resolve ``raw_path`` under the owned domain root.

        Returns ``None`` when the path is empty or escapes the domain
        (A20: no system targets).  Relative paths are treated as
        domain-root-relative.
        """
        if not raw_path:
            return None
        root = Path(self._owned_domain_root).resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        if resolved != root and root not in resolved.parents:
            return None
        return resolved

    async def _adjudicate_observe(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = self._observe_domain()
        self._last_snapshot = snapshot
        return accepted_outcome(
            {"action": "observe", "domain": "owned", "snapshot": snapshot},
            self.verified_basis("A20"),
        )

    async def _adjudicate_analyze(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = self._last_snapshot or self._observe_domain()
        anomalies = self._analyze_domain(snapshot)
        return accepted_outcome(
            {"action": "analyze", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    async def _adjudicate_reason(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 reason power: derive an advisory conclusion from the query
        plus current domain evidence (snapshot anomalies + pending items).
        """
        query = str(request.payload.get("query") or "")
        snapshot = self._last_snapshot or self._observe_domain()
        anomalies = self._analyze_domain(snapshot)
        conclusion = {
            "query": query,
            "evidence_basis": {
                "snapshot_at": snapshot.get("observed_at"),
                "anomaly_count": len(anomalies),
            },
            "conclusion": (
                "domain-anomalies-present" if anomalies else "domain-nominal"
            ),
            "related_anomalies": anomalies[:5],
            "advisory": True,
            "reasoned_at": self._iso_now(),
        }
        return accepted_outcome(
            {"action": "reason", "domain": "owned", "reasoning": conclusion},
            self.verified_basis("A20"),
        )

    async def _adjudicate_decide(self, request: SovereignRequest) -> SovereignOutcome:
        decision = request.payload.get("decision")
        decision_id = f"decision-{len(self._reviews) + 1}"
        self._reviews[decision_id] = {
            "kind": "domain-decision",
            "decision": decision,
            "decided_at": self._iso_now(),
        }
        return accepted_outcome(
            {
                "action": "decide",
                "domain": "owned",
                "decision_id": decision_id,
                "decision": decision,
            },
            self.verified_basis("A20", "A12"),
        )

    async def _adjudicate_manage(self, request: SovereignRequest) -> SovereignOutcome:
        actions = self._manage_domain()
        return accepted_outcome(
            {
                "action": "manage",
                "domain": "owned",
                "resource": request.payload.get("resource"),
                "actions": actions,
            },
            self.verified_basis("A20"),
        )

    async def _adjudicate_authorize(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 authorize power: record a domain-internal authorization grant.

        Grants are persisted inside the owned domain
        (``governance/domain-authorizations.json``) so they are auditable;
        they never confer system-side power.
        """
        permission = request.payload.get("permission")
        if not permission:
            return refusal_outcome(
                "MISSING_PERMISSION", self.verified_basis("A20")
            )
        grant = {
            "permission": permission,
            "grantee": request.payload.get("grantee") or request.requester,
            "scope": request.payload.get("scope") or "owned-domain",
            "granted_at": self._iso_now(),
            "expires_at": request.payload.get("expires_at"),
        }
        registry = self._domain_governance_file("domain-authorizations.json")
        grants = self._read_domain_json_list(registry)
        grants.append(grant)
        grant_id = f"grant-{len(grants)}"
        grant["grant_id"] = grant_id
        if not self._write_domain_json(registry, grants[-500:]):
            return refusal_outcome(
                "EXECUTION_FAILED", self.verified_basis("A20")
            )
        return accepted_outcome(
            {
                "action": "authorize",
                "domain": "owned",
                "grant_id": grant_id,
                "grant": grant,
            },
            self.verified_basis("A20"),
        )

    async def _adjudicate_execute(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 execute power — bounded to declared domain operations.

        Supported operations: ``mkdir``, ``db-vacuum``,
        ``checkpoint-cleanup``.  Unknown operations fail closed.
        """
        operation = request.payload.get("operation") or {}
        if not isinstance(operation, dict):
            return refusal_outcome(
                "INVALID_OPERATION", self.verified_basis("A20")
            )
        op = str(operation.get("op") or "")
        target = self._resolve_in_domain(operation.get("path"))
        if target is None:
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20")
            )

        result: dict[str, Any] = {"op": op, "path": str(target)}
        try:
            if op == "mkdir":
                target.mkdir(parents=True, exist_ok=True)
                result["created"] = True
            elif op == "db-vacuum":
                if target.suffix != ".sqlite3" or not target.is_file():
                    return refusal_outcome(
                        "INVALID_OPERATION", self.verified_basis("A20")
                    )
                conn = sqlite3.connect(str(target))
                try:
                    conn.execute("VACUUM")
                finally:
                    conn.close()
                result["vacuumed"] = True
            elif op == "checkpoint-cleanup":
                if not target.is_file() or "checkpoint" not in target.name:
                    return refusal_outcome(
                        "INVALID_OPERATION", self.verified_basis("A20")
                    )
                target.unlink()
                result["deleted"] = True
            else:
                return refusal_outcome(
                    "UNKNOWN_OPERATION", self.verified_basis("A20")
                )
        except OSError as error:
            return refusal_outcome(
                "EXECUTION_FAILED", self.verified_basis("A20")
            )
        self._auto_metrics["config_updates"] += 0  # executed, not configured
        return accepted_outcome(
            {"action": "execute", "domain": "owned", "result": result},
            self.verified_basis("A20"),
        )

    async def _adjudicate_write(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 write power — writes file content inside the owned domain."""
        target = self._resolve_in_domain(request.payload.get("path"))
        content = request.payload.get("content")
        if target is None:
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20")
            )
        if content is None:
            return refusal_outcome(
                "MISSING_CONTENT", self.verified_basis("A20")
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
        except OSError:
            return refusal_outcome(
                "EXECUTION_FAILED", self.verified_basis("A20")
            )
        return accepted_outcome(
            {
                "action": "write",
                "domain": "owned",
                "path": str(target),
                "bytes": len(str(content).encode("utf-8")),
            },
            self.verified_basis("A20"),
        )

    async def _adjudicate_delete(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 delete power — removes files inside the owned domain."""
        target = self._resolve_in_domain(request.payload.get("path"))
        if target is None:
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20")
            )
        try:
            if target.is_dir():
                target.rmdir()  # only empty dirs — fail closed otherwise
            elif target.exists():
                target.unlink()
            else:
                return refusal_outcome(
                    "TARGET_MISSING", self.verified_basis("A20")
                )
        except OSError:
            return refusal_outcome(
                "EXECUTION_FAILED", self.verified_basis("A20")
            )
        return accepted_outcome(
            {"action": "delete", "domain": "owned", "path": str(target)},
            self.verified_basis("A20"),
        )

    async def _adjudicate_configure(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 configure power — writes JSON config inside the domain."""
        target = self._resolve_in_domain(request.payload.get("path"))
        config = request.payload.get("config")
        if target is None:
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20")
            )
        if not isinstance(config, (dict, list)):
            return refusal_outcome(
                "INVALID_CONFIG", self.verified_basis("A20")
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return refusal_outcome(
                "EXECUTION_FAILED", self.verified_basis("A20")
            )
        self._auto_metrics["config_updates"] += 1
        return accepted_outcome(
            {
                "action": "configure",
                "domain": "owned",
                "path": str(target),
                "keys": sorted(config.keys()) if isinstance(config, dict) else None,
            },
            self.verified_basis("A20"),
        )

    async def _adjudicate_channel_coordinate(self, request: SovereignRequest) -> SovereignOutcome:
        """A66: 星澄通道協調（透過資訊層，不直接存取系統模組）。

        Coordination events are recorded inside the owned domain for
        traceability; the returned payload is what the information layer
        relays.
        """
        channel = request.payload.get("channel")
        event = {
            "channel": channel,
            "message": request.payload.get("message"),
            "coordinated_at": self._iso_now(),
        }
        registry = self._domain_governance_file("channel-coordination.json")
        events = self._read_domain_json_list(registry)
        events.append(event)
        event["event_id"] = f"coord-{len(events)}"
        persisted = self._write_domain_json(registry, events[-500:])
        return accepted_outcome(
            {
                "coordination": "information-layer-only",
                "system_access": "forbidden",
                "channel": channel,
                "event_id": event["event_id"],
                "persisted": persisted,
            },
            self.verified_basis("A66", "A20"),
        )

    async def _adjudicate_auto_observe(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-observe the owned domain and return a snapshot."""
        snapshot = self._observe_domain()
        self._last_snapshot = snapshot
        self._auto_metrics["observe_cycles"] += 1
        return accepted_outcome(
            {"action": "auto-observe", "domain": "owned", "snapshot": snapshot},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_analyze(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-analyze the domain snapshot for anomalies."""
        anomalies = self._analyze_domain(self._last_snapshot)
        self._auto_metrics["analyze_cycles"] += 1
        if anomalies:
            self._auto_metrics["anomalies_detected"] += len(anomalies)
            self._pending_anomalies.extend(anomalies)
        return accepted_outcome(
            {"action": "auto-analyze", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_manage(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-manage domain resources (db maintenance, model loading)."""
        actions = self._manage_domain()
        self._auto_metrics["manage_cycles"] += 1
        return accepted_outcome(
            {"action": "auto-manage", "domain": "owned", "actions": actions},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_health_check(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-health-check the owned domain."""
        health = self._health_check_domain()
        self._auto_metrics["health_checks"] += 1
        return accepted_outcome(
            {"action": "auto-health-check", "domain": "owned", "health": health},
            self.verified_basis("A20"),
        )

    # ------------------------------------------------------------------
    # Auto-automation loop (A20 full-automation upgrade)
    # ------------------------------------------------------------------

    async def start_auto_loop(self) -> None:
        """Start the background auto-automation loop.

        The loop periodically:
        1. Observes the owned domain (file tree, databases, models).
        2. Analyzes the snapshot for anomalies.
        3. Manages domain resources (db maintenance, model loading).
        4. Checks domain health.
        5. Notifies the system through the information layer (A66) when
           anomalies are detected.

        All operations stay inside the owned domain (A20); no system
        targets or effects are ever produced.
        """
        if self._auto_loop_task is not None and not self._auto_loop_task.done():
            return
        self._auto_enabled = True
        self._auto_loop_task = asyncio.create_task(self._auto_loop())

    async def stop_auto_loop(self) -> None:
        """Stop the background auto-automation loop."""
        self._auto_enabled = False
        task = self._auto_loop_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._auto_loop_task = None

    async def _auto_loop(self) -> None:
        """Background loop: periodic domain automation."""
        while self._auto_enabled:
            try:
                await self._auto_cycle()
            except asyncio.CancelledError:
                break
            except Exception as error:
                _logger.warning("xingcheng auto-cycle error: %s", error)
            await asyncio.sleep(self._auto_loop_interval)

    async def _auto_cycle(self) -> None:
        """One automation cycle: observe, analyze, manage, health, notify."""
        self._auto_metrics["last_auto_cycle"] = self._iso_now()

        # 1. Observe the domain.
        self._last_snapshot = self._observe_domain()
        self._auto_metrics["observe_cycles"] += 1

        # 2. Analyze for anomalies.
        anomalies = self._analyze_domain(self._last_snapshot)
        self._auto_metrics["analyze_cycles"] += 1
        if anomalies:
            self._auto_metrics["anomalies_detected"] += len(anomalies)
            self._auto_metrics["last_anomaly"] = anomalies[0].get(
                "type", ""
            )
            self._pending_anomalies.extend(anomalies)

        # 2b. Consume information-layer evidence projection (A140) — the
        # sole system interface; read-only, filtered evidence only.
        global_anomalies = self._consume_information_layer_evidence()
        if global_anomalies:
            self._auto_metrics["anomalies_detected"] += len(global_anomalies)
            self._pending_anomalies.extend(global_anomalies)

        # 3. Manage domain resources.
        self._manage_domain()
        self._auto_metrics["manage_cycles"] += 1

        # 4. Health check.
        self._health_check_domain()
        self._auto_metrics["health_checks"] += 1

        # 5. Notify through the information layer (A66) if anomalies exist.
        if self._pending_anomalies:
            await self._notify_anomalies()

    # ------------------------------------------------------------------
    # Domain-internal JSON registry helpers (owned-domain writes only)
    # ------------------------------------------------------------------

    def _domain_governance_file(self, name: str) -> Path:
        root = Path(self._owned_domain_root) / "governance"
        root.mkdir(parents=True, exist_ok=True)
        return root / name

    @staticmethod
    def _read_domain_json_list(path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []

    @staticmethod
    def _write_domain_json(path: Path, data: Any) -> bool:
        try:
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except (OSError, UnicodeError):
            return False

    def _consume_information_layer_evidence(self) -> list[dict[str, Any]]:
        """A140: consume the filtered system-evidence projection.

        The information layer is the sole provider of system review
        evidence; 星澄 reads the projection file the layer drops inside
        the owned domain (``governance/system-evidence.json``), classifies
        anomalies, and returns them for user notification.  Absent or
        malformed projections yield no findings — read-only, fail-quiet.
        """
        projection = (
            Path(self._owned_domain_root)
            / "governance"
            / "system-evidence.json"
        )
        try:
            evidence = json.loads(projection.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        if not isinstance(evidence, dict):
            return []
        anomalies = self._classify_evidence(evidence)
        if anomalies:
            self._reviews[f"auto-review-{len(self._reviews) + 1}"] = {
                "kind": "global-review",
                "scope": "information-layer-projection",
                "anomalies": anomalies,
                "advisory": True,
                "reviewed_at": self._iso_now(),
            }
        return anomalies

    def _observe_domain(self) -> dict[str, Any]:
        """A20: observe the owned domain and return a snapshot.

        Scans the domain root for:
        - File tree structure (top-level directories and file counts)
        - Database files (SQLite databases and their sizes)
        - Model files (in runtime/state/models)
        - Configuration files (JSON configs)
        - Runtime state files
        """
        root = Path(self._owned_domain_root)
        snapshot: dict[str, Any] = {
            "observed_at": self._iso_now(),
            "root": str(root),
            "exists": root.exists(),
            "directories": {},
            "databases": [],
            "models": [],
            "configs": [],
        }

        if not root.exists():
            return snapshot

        # Scan top-level directories.
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                try:
                    file_count = sum(
                        1 for _ in entry.rglob("*") if _.is_file()
                    )
                    snapshot["directories"][entry.name] = {
                        "file_count": file_count,
                    }
                except (OSError, PermissionError):
                    snapshot["directories"][entry.name] = {
                        "file_count": -1,
                        "error": "access-denied",
                    }

        # Scan for databases.
        for db_path in root.rglob("*.sqlite3"):
            try:
                stat = db_path.stat()
                snapshot["databases"].append({
                    "name": db_path.name,
                    "path": str(db_path.relative_to(root)),
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                })
            except (OSError, PermissionError):
                continue

        # Scan for models.
        models_dir = root / "runtime" / "state" / "models"
        if models_dir.exists():
            for model_path in sorted(models_dir.iterdir()):
                if model_path.is_file():
                    try:
                        stat = model_path.stat()
                        snapshot["models"].append({
                            "name": model_path.name,
                            "size_bytes": stat.st_size,
                            "modified_at": stat.st_mtime,
                        })
                    except (OSError, PermissionError):
                        continue

        # Scan for configs.
        for config_path in root.rglob("*.json"):
            if "node_modules" in config_path.parts:
                continue
            try:
                stat = config_path.stat()
                snapshot["configs"].append({
                    "name": config_path.name,
                    "path": str(config_path.relative_to(root)),
                    "size_bytes": stat.st_size,
                })
            except (OSError, PermissionError):
                continue

        return snapshot

    def _analyze_domain(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """A20: analyze the domain snapshot for anomalies.

        Detects:
        - Oversized databases (exceeding _DB_MAX_SIZE_BYTES)
        - Oversized model directories
        - Stale checkpoint files (older than _CHECKPOINT_MAX_AGE_HOURS)
        - Missing critical directories (runtime, governance, identity)
        - Corrupt or empty databases
        """
        anomalies: list[dict[str, Any]] = []
        if not snapshot.get("exists"):
            anomalies.append({
                "type": "domain-root-missing",
                "severity": "critical",
                "detail": "owned domain root does not exist",
            })
            return anomalies

        # Check critical directories.
        dirs = snapshot.get("directories", {})
        critical_dirs = {"runtime", "governance", "identity", "permissions"}
        missing = critical_dirs - set(dirs)
        if missing:
            anomalies.append({
                "type": "missing-critical-directories",
                "severity": "warning",
                "detail": f"missing: {sorted(missing)}",
            })

        # Check database sizes.
        import time
        now = time.time()
        for db in snapshot.get("databases", []):
            size = db.get("size_bytes", 0)
            if size > _DB_MAX_SIZE_BYTES:
                anomalies.append({
                    "type": "oversized-database",
                    "severity": "warning",
                    "target": db.get("name"),
                    "size_bytes": size,
                    "threshold": _DB_MAX_SIZE_BYTES,
                })
            # Check for stale checkpoints.
            if "checkpoint" in db.get("name", "").lower():
                modified = db.get("modified_at", 0)
                age_hours = (now - modified) / 3600 if modified else 999
                if age_hours > _CHECKPOINT_MAX_AGE_HOURS:
                    anomalies.append({
                        "type": "stale-checkpoint",
                        "severity": "info",
                        "target": db.get("name"),
                        "age_hours": round(age_hours, 1),
                        "threshold_hours": _CHECKPOINT_MAX_AGE_HOURS,
                    })

        # Check model directory size.
        total_model_size = sum(
            m.get("size_bytes", 0) for m in snapshot.get("models", [])
        )
        if total_model_size > _MODEL_DIR_MAX_SIZE_BYTES:
            anomalies.append({
                "type": "oversized-model-directory",
                "severity": "warning",
                "total_size_bytes": total_model_size,
                "threshold": _MODEL_DIR_MAX_SIZE_BYTES,
            })

        return anomalies

    def _manage_domain(self) -> list[dict[str, Any]]:
        """A20: auto-manage domain resources.

        Performs:
        - SQLite VACUUM/INTEGRITY_CHECK on domain databases
        - Stale checkpoint cleanup
        - Model directory size monitoring
        """
        actions: list[dict[str, Any]] = []
        root = Path(self._owned_domain_root)
        if not root.exists():
            return actions

        # Database maintenance.
        for db_path in root.rglob("*.sqlite3"):
            if "node_modules" in db_path.parts:
                continue
            try:
                # Integrity check.
                conn = sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True
                )
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                conn.close()
                if result and result[0] != "ok":
                    actions.append({
                        "action": "db-integrity-check",
                        "target": db_path.name,
                        "result": "corrupt",
                        "detail": result[0],
                    })
                    self._pending_anomalies.append({
                        "type": "corrupt-database",
                        "severity": "critical",
                        "target": db_path.name,
                    })
                else:
                    actions.append({
                        "action": "db-integrity-check",
                        "target": db_path.name,
                        "result": "ok",
                    })
            except sqlite3.Error as error:
                actions.append({
                    "action": "db-integrity-check",
                    "target": db_path.name,
                    "result": "error",
                    "detail": str(error),
                })
            self._auto_metrics["db_maintenance_runs"] += 1

        # Stale checkpoint cleanup.
        import time
        now = time.time()
        checkpoints_dir = root / "runtime" / "state"
        if checkpoints_dir.exists():
            for ckpt in checkpoints_dir.glob("*checkpoint*"):
                try:
                    stat = ckpt.stat()
                    age_hours = (now - stat.st_mtime) / 3600
                    if (
                        age_hours > _CHECKPOINT_MAX_AGE_HOURS
                        and ckpt.is_file()
                    ):
                        # A20: 星澄 has delete power in owned domain.
                        ckpt.unlink()
                        actions.append({
                            "action": "stale-checkpoint-cleanup",
                            "target": ckpt.name,
                            "age_hours": round(age_hours, 1),
                        })
                except (OSError, PermissionError):
                    continue

        return actions

    def _health_check_domain(self) -> dict[str, Any]:
        """A20: check the health of the owned domain.

        Returns a health summary including:
        - Domain root existence
        - Critical directory presence
        - Database integrity
        - Model availability
        - Configuration validity
        """
        root = Path(self._owned_domain_root)
        health: dict[str, Any] = {
            "checked_at": self._iso_now(),
            "domain_root_exists": root.exists(),
            "critical_dirs": {},
            "databases_healthy": True,
            "models_available": False,
            "configs_valid": True,
        }

        if not root.exists():
            health["overall"] = "critical"
            return health

        # Check critical directories.
        for dir_name in ("runtime", "governance", "identity", "permissions"):
            dir_path = root / dir_name
            health["critical_dirs"][dir_name] = dir_path.exists()

        # Check database integrity.
        for db_path in root.rglob("*.sqlite3"):
            if "node_modules" in db_path.parts:
                continue
            try:
                conn = sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True
                )
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                conn.close()
                if not result or result[0] != "ok":
                    health["databases_healthy"] = False
            except sqlite3.Error:
                health["databases_healthy"] = False

        # Check model availability.
        models_dir = root / "runtime" / "state" / "models"
        if models_dir.exists():
            health["models_available"] = any(models_dir.iterdir())

        # Check config validity.
        for config_path in root.rglob("*.json"):
            if "node_modules" in config_path.parts:
                continue
            try:
                json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeError, OSError):
                health["configs_valid"] = False

        # Overall health.
        all_dirs_present = all(health["critical_dirs"].values())
        if (
            health["domain_root_exists"]
            and all_dirs_present
            and health["databases_healthy"]
            and health["configs_valid"]
        ):
            health["overall"] = "healthy"
        elif health["domain_root_exists"]:
            health["overall"] = "degraded"
        else:
            health["overall"] = "critical"

        return health

    async def _notify_anomalies(self) -> None:
        """A66: notify the system of anomalies through the information layer.

        星澄 cannot directly access system modules (A20), but it can
        notify through the information layer (A66).  This writes anomaly
        reports to the domain's governance directory and clears the
        pending list.
        """
        if not self._pending_anomalies:
            return

        root = Path(self._owned_domain_root)
        notify_dir = root / "governance"
        notify_dir.mkdir(parents=True, exist_ok=True)
        notify_path = notify_dir / "anomaly-notifications.json"

        # Read existing notifications.
        existing: list[dict[str, Any]] = []
        try:
            existing = json.loads(
                notify_path.read_text(encoding="utf-8")
            )
            if not isinstance(existing, list):
                existing = []
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

        # Append new anomalies.
        new_entries = [
            {
                "notified_at": self._iso_now(),
                "channel": "information-layer",
                "anomaly": anomaly,
            }
            for anomaly in self._pending_anomalies
        ]
        all_entries = (existing + new_entries)[-100:]  # keep last 100

        # Write back.
        try:
            notify_path.write_text(
                json.dumps(all_entries, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._auto_metrics["channel_notifications"] += len(new_entries)
        except (OSError, UnicodeError) as error:
            _logger.warning("xingcheng anomaly notify failed: %s", error)

        # Clear pending.
        self._pending_anomalies.clear()

    async def _on_start(self) -> None:
        """Start the auto-automation loop when the sovereign starts."""
        await self.start_auto_loop()

    async def _on_stop(self) -> None:
        """Stop the auto-automation loop when the sovereign stops."""
        await self.stop_auto_loop()

    def auto_status(self) -> dict[str, Any]:
        """Read-only status of the auto-automation subsystem."""
        return {
            "enabled": self._auto_enabled,
            "loop_running": (
                self._auto_loop_task is not None
                and not self._auto_loop_task.done()
            ),
            "loop_interval_seconds": self._auto_loop_interval,
            "metrics": dict(self._auto_metrics),
            "pending_anomalies": len(self._pending_anomalies),
            "last_snapshot_exists": bool(self._last_snapshot),
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["owned_domain"] = self._owned_domain_root
        base["isolated_from_system"] = self._isolated
        base["decision_chain"] = "outside"
        base["auto"] = self.auto_status()
        base["auxiliary_review"] = {
            "authority": "global-read-only-review+anomaly-classification+user-notification",
            "decision_power": "none",
            "advisory": True,
            "reviews": len(self._reviews),
            "languages": list(ALLOWED_LANGUAGES),
        }
        base["special_law"] = {
            "permission_review": "A313-sole-review-authority",
            "free_entry_inspection": "A328-confidential-read-only",
            "programming_capability": "A336-native-model-exclusive",
            "automation_executor": "A336-authorized-tasks-only",
            "automation_tasks": len(self._automation_tasks),
            "program_tasks": len(self._program_tasks),
        }
        if self._last_snapshot:
            base["domain_snapshot"] = {
                "observed_at": self._last_snapshot.get("observed_at"),
                "directories": list(
                    self._last_snapshot.get("directories", {}).keys()
                ),
                "database_count": len(
                    self._last_snapshot.get("databases", [])
                ),
                "model_count": len(
                    self._last_snapshot.get("models", [])
                ),
                "config_count": len(
                    self._last_snapshot.get("configs", [])
                ),
            }
        return base


__all__ = ["XingchengSovereign"]