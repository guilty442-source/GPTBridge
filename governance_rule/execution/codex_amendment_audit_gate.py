"""Automated Codex update — five-sovereign audit gate (proposed A537).

法典依據:
- A382: the non-disruptive amendment flow (12 ordered steps); a Codex
  amendment never restarts, disconnects or partially mutates the running
  system.
- A377/A381: the active sovereign roster is decision-sovereign,
  permission-sovereign, system-runtime-sovereign, automation-sovereign and
  XINGCHENG; the Codex itself is a rule-declaration layer, never a
  sovereign.
- A87: every change produces a new candidate, identity, lineage and content
  hash; recertification precedes effectiveness.
- A379/A175 (as amended by the governor's replacement decision): for Codex
  generations the unanimous five-sovereign audit certificate replaces the
  human-governor signature as the seal-closing condition; the explicit
  human amendment command (A382 step 1) remains, and no actor may fabricate
  evidence, receipts or certificates.
- A177: network access runs only through the governed information-layer
  path; Xingcheng's web search is the registerable loopback SearXNG tool.
- A446/A121: every audit outcome is receipted, independently verifiable and
  fail-closed; an unrecorded result is never a pass.

Governor-proposed ordinance (request currently staged in
``main-system/runtime/state/codex-amendment-request-codex-update-automation``):
the Codex update flow becomes automated, but no work division and no
execution may start until every one of the five active sovereigns has
audited the staged amendment and all five receipts passed.  Xingcheng's
audit uses the Xingcheng web-search (SearXNG) tool through the governed
channel.

This module is the mechanism only.  It never writes the Codex database,
never regenerates the mirror, never signs and never publishes; it collects
the five audit receipts, releases the division plan on unanimous pass, and
records metadata-only evidence.  The division plan itself remains owned by
the registered sub-sovereigns (A336/A337: no parallel automation
coordinator).

Governor runtime-budget amendment (request staged 2026-09-17 in
``main-system/runtime/state/codex-amendment-request-audit-test-runtime-budget``):
the entire audit flow carries a hard 30-second monotonic deadline and this
gate's governed test suite carries a hard 20-second budget; a cycle that
elapses either budget is denied fail-closed and its duration is recorded as
metadata-only evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

AUDIT_LEDGER_PATH = (
    Path(__file__).resolve().parent / "audit" / "codex_amendment_audit.jsonl"
)

MAX_FINDINGS = 20
DEFAULT_AUDIT_DEADLINE_SECONDS = 30.0

# Governor amendment (staged 2026-09-17): the ENTIRE amendment audit flow —
# every sovereign check, the unanimous join, certificate issuance and ledger
# recording — must finish inside a hard 30-second wall-clock deadline; its
# governed test suite must finish inside a hard 20-second budget.  Exceeding
# either budget is fail-closed: a slow audit is never a pass.
DEFAULT_AUDIT_FLOW_DEADLINE_SECONDS: Final[float] = 30.0
AMENDMENT_TEST_SUITE_BUDGET_SECONDS: Final[float] = 20.0

AUDIT_FLOW_DEADLINE_REASON: Final[str] = "AUDIT_FLOW_DEADLINE_EXCEEDED"

@dataclass(frozen=True)
class SovereignAuditSpec:
    """Explicit audit charter: division of labour plus each sovereign's duties.

    A receipt is a failed receipt when any ``required_evidence`` item is
    absent (fail-closed A446): an audit never passes on an assertion alone.
    """

    sovereign_id: str
    domain: str
    owner_sub_sovereign: str
    duties: tuple[str, ...]
    required_evidence: tuple[str, ...]
    forbidden: tuple[str, ...]
    post_audit_duties: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "sovereign_id": self.sovereign_id,
            "domain": self.domain,
            "owner_sub_sovereign": self.owner_sub_sovereign,
            "duties": list(self.duties),
            "required_evidence": list(self.required_evidence),
            "forbidden": list(self.forbidden),
            "post_audit_duties": list(self.post_audit_duties),
        }


SOVEREIGN_AUDIT_SPECS: tuple[SovereignAuditSpec, ...] = (
    SovereignAuditSpec(
        sovereign_id="decision-sovereign",
        domain="policy-precedence-and-decision-basis",
        owner_sub_sovereign="policy-architecture-sub-sovereign",
        duties=(
            "verify the amendment change class and required review level",
            "verify no conflicting active successor in the same scope",
            "verify the decision basis references exist and are current",
        ),
        required_evidence=(
            "amendment_class",
            "basis_references",
            "successor_scope_unique",
        ),
        forbidden=(
            "implicit precedence",
            "selecting among ambiguous successors",
        ),
        post_audit_duties=(
            "monitor precedence and successor uniqueness until publication",
            "re-derive the decision basis for the successor generation",
        ),
    ),
    SovereignAuditSpec(
        sovereign_id="permission-sovereign",
        domain="directory-identity-and-access-lifecycle",
        owner_sub_sovereign="directory-sub-sovereign",
        duties=(
            "verify directory rows, identity format and ownership",
            "verify lifecycle/effective parity and retirement markers",
            "verify test-flow and module registry references",
        ),
        required_evidence=(
            "directory_rows",
            "identity_lifecycle_parity",
            "testflow_references",
        ),
        forbidden=(
            "self-registering domain truth",
            "inventing domain truth",
        ),
        post_audit_duties=(
            "apply the directory/identity/test-flow updates",
            "re-certify the permission sovereign after publication",
        ),
    ),
    SovereignAuditSpec(
        sovereign_id="system-runtime-sovereign",
        domain="runtime-continuity-and-reader-generation",
        owner_sub_sovereign="runtime-state-sync-sub-sovereign",
        duties=(
            "verify the amendment causes no restart, stop or disconnect",
            "verify old readers drain on the prior generation and new readers use the published generation",
            "verify channel continuity and the health window",
        ),
        required_evidence=(
            "reader_generation_plan",
            "channel_continuity",
            "health_window",
        ),
        forbidden=(
            "forced reload of incompatible consumers",
            "mixed-generation reads",
        ),
        post_audit_duties=(
            "enforce the generation fence and reader drain during publication",
            "re-anchor authority and verify runtime continuity afterwards",
        ),
    ),
    SovereignAuditSpec(
        sovereign_id="automation-sovereign",
        domain="staging-seal-mirror-and-version-mechanics",
        owner_sub_sovereign="release-update-sync-sub-sovereign",
        duties=(
            "verify staged generation isolation and non-authoritative status",
            "verify seal roots recomputation per SEAL_CANONICAL_V1",
            "verify mirror part chain and assembled payload hash",
            "verify version identity increment and the rollback pointer",
        ),
        required_evidence=(
            "staging_isolation",
            "seal_roots",
            "mirror_chain",
            "version_identity",
            "rollback_pointer",
        ),
        forbidden=(
            "publishing partial generations",
            "computing roots over mixed generations",
        ),
        post_audit_duties=(
            "execute the automated staging/normalize/validate pipeline",
            "close the seal and issue the five-sovereign audit certificate",
        ),
    ),
    SovereignAuditSpec(
        sovereign_id="xingcheng",
        domain="external-evidence-via-xingcheng-web-search",
        owner_sub_sovereign="xingcheng-assistant",
        duties=(
            "verify external references and advisories via the Xingcheng web-search tool",
            "classify sources and confirm redaction of confidential material",
            "verify no direct network path outside the information layer",
        ),
        required_evidence=(
            "network_search",
            "source_classification",
            "redaction_check",
        ),
        forbidden=(
            "direct sockets or HTTP clients",
            "raw confidential content in audit evidence",
        ),
        post_audit_duties=(
            "refresh external evidence when drift is detected before publication",
            "re-verify source classification after publication",
        ),
    ),
)

SOVEREIGN_IDS: tuple[str, ...] = tuple(
    spec.sovereign_id for spec in SOVEREIGN_AUDIT_SPECS
)

SOVEREIGN_DOMAINS: tuple[tuple[str, str], ...] = tuple(
    (spec.sovereign_id, spec.domain) for spec in SOVEREIGN_AUDIT_SPECS
)

_SPEC_BY_ID: Mapping[str, SovereignAuditSpec] = {
    spec.sovereign_id: spec for spec in SOVEREIGN_AUDIT_SPECS
}

SOVEREIGN_ALIASES: Mapping[str, str] = {
    "星澄": "xingcheng",
    "runtime-sovereign": "system-runtime-sovereign",
}

NETWORK_AUDIT_SOVEREIGN = "xingcheng"


def _present(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (str, bytes, tuple, list, dict, set)) and not value:
        return False
    return True

CERTIFICATE_SCHEMA = "gptbridge.codex-amendment-certificate/v1"

DIVISION_ASSIGNMENTS: tuple[tuple[str, str], ...] = (
    ("staging-normalize-validate", "release-update-sync-sub-sovereign"),
    ("seal-mirror-and-successor-manifest", "release-update-sync-sub-sovereign"),
    ("directory-identity-and-testflow-update", "directory-sub-sovereign"),
    ("permission-recertification", "permission-sovereign"),
    ("runtime-reader-generation-and-reanchor", "runtime-state-sync-sub-sovereign"),
    ("external-evidence-refresh", "xingcheng-assistant"),
    ("seal-closure-and-certificate-issuance", "automation-sovereign"),
    ("audit-publication", "automatic-log-sync-sub-sovereign"),
)


def _normalize_sovereign(sovereign_id: object) -> str:
    value = str(sovereign_id or "").strip()
    return SOVEREIGN_ALIASES.get(value, value)


def _canonical_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class SovereignAuditReceipt:
    """Metadata-only audit receipt for one sovereign (A446/A435)."""

    sovereign_id: str
    domain: str
    ok: bool
    method: str
    evidence_hash: str
    owner_sub_sovereign: str = ""
    network_search: bool = False
    duration_ms: int = 0
    independent_verifier: str = ""
    findings: tuple[str, ...] = ()
    error: str = ""

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "sovereign_id": self.sovereign_id,
            "domain": self.domain,
            "owner_sub_sovereign": self.owner_sub_sovereign,
            "ok": self.ok,
            "method": self.method,
            "evidence_hash": self.evidence_hash,
            "network_search": self.network_search,
            "duration_ms": self.duration_ms,
        }
        if self.independent_verifier:
            record["independent_verifier"] = self.independent_verifier
        if self.findings:
            record["findings"] = list(self.findings)
        if self.error:
            record["error"] = self.error
        return record


@dataclass(frozen=True)
class AmendmentAuditResult:
    """Closed verdict of the five-sovereign gate for one amendment."""

    amendment_id: str
    ok: bool
    reason: str
    receipts: tuple[SovereignAuditReceipt, ...] = ()
    failed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    division_plan: tuple[dict[str, str], ...] | None = None
    certificate: dict[str, Any] | None = None
    audit_recorded: bool = False
    duration_ms: int = 0
    budget_ms: int = 0
    requester: str = ""
    requester_independent_verifier: str = ""
    self_audit_passed: bool | None = None

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "amendment_id": self.amendment_id,
            "ok": self.ok,
            "reason": self.reason,
            "failed": list(self.failed),
            "missing": list(self.missing),
            "audit_recorded": self.audit_recorded,
            "duration_ms": self.duration_ms,
            "budget_ms": self.budget_ms,
            "requester": self.requester,
            "division_released": self.division_plan is not None,
            "certificate_issued": self.certificate is not None,
            "charters": [spec.to_record() for spec in SOVEREIGN_AUDIT_SPECS],
            "receipts": [receipt.to_record() for receipt in self.receipts],
        }
        if self.requester_independent_verifier:
            record["requester_independent_verifier"] = (
                self.requester_independent_verifier
            )
        if self.self_audit_passed is not None:
            record["self_audit_passed"] = self.self_audit_passed
        if self.certificate is not None:
            record["certificate"] = dict(self.certificate)
        if self.division_plan is not None:
            record["division_plan"] = [dict(item) for item in self.division_plan]
        return record


def _division_plan(amendment_id: str) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "amendment_id": amendment_id,
            "work_item": work_item,
            "owner": owner,
            "released_by": "five-sovereign-audit-gate",
        }
        for work_item, owner in DIVISION_ASSIGNMENTS
    )


def _certificate(
    amendment_id: str,
    receipts: Sequence[SovereignAuditReceipt],
    predecessor: Mapping[str, Any] | None,
    requester: str = "",
) -> dict[str, Any]:
    """Issue the seal-closing certificate from the five audit receipts.

    Metadata only: identities, methods and evidence hashes — never Codex
    content.  The certificate hash is the deterministic identity of the
    verdict and is what the automation executor references when it closes
    the generation seal (replacing the former human-signature condition).
    When a sovereign originated the amendment, the requester and the
    independent verifier of its own-domain receipt are bound into the
    certificate so a self-audit can never be mistaken for a pass (A446/A390).
    """

    payload: dict[str, Any] = {
        "schema": CERTIFICATE_SCHEMA,
        "amendment_id": amendment_id,
        "verdict": "FIVE_SOVEREIGN_AUDIT_PASSED",
        "requester": requester,
        "predecessor": dict(predecessor or {}),
        "receipts": [
            {
                "sovereign_id": receipt.sovereign_id,
                "domain": receipt.domain,
                "owner_sub_sovereign": receipt.owner_sub_sovereign,
                "method": receipt.method,
                "evidence_hash": receipt.evidence_hash,
                "independent_verifier": receipt.independent_verifier,
            }
            for receipt in receipts
        ],
    }
    payload["certificate_hash"] = _canonical_hash(payload)
    return payload


def build_xingcheng_network_check(
    search: Callable[[str], Any] | None,
    queries: Sequence[str],
) -> Callable[[], Mapping[str, Any]]:
    """Build the Xingcheng audit check over the governed web-search path.

    ``search`` must be the information-layer callable bound to the
    Xingcheng web_search (SearXNG loopback) tool — never a direct socket,
    HTTP client or database connection (A177).  When no governed callable
    is wired the check fails closed instead of inventing evidence.
    """

    query_list = tuple(str(query).strip() for query in queries if str(query).strip())

    def check() -> Mapping[str, Any]:
        if search is None:
            return {
                "ok": False,
                "method": "xingcheng-web-search",
                "network_search": True,
                "error": "NETWORK_AUDIT_UNAVAILABLE",
            }
        findings: list[str] = []
        observations: list[Any] = []
        for query in query_list:
            result = search(query)
            observations.append(result)
            if isinstance(result, Mapping):
                ok = result.get("ok") is not False
                findings.append(
                    f"query:{query}:{'ok' if ok else 'failed'}"
                )
            else:
                findings.append(f"query:{query}:untyped")
        classification = sorted(
            {
                str(item.get("source") or "xingcheng-web-search")
                for item in observations
                if isinstance(item, Mapping)
            }
        ) or ["xingcheng-web-search"]
        return {
            "ok": bool(observations) and all(
                not isinstance(item, Mapping) or item.get("ok") is not False
                for item in observations
            ),
            "method": "xingcheng-web-search",
            "network_search": True,
            "findings": findings,
            "evidence": {
                "network_search": {
                    "queries": len(query_list),
                    "responses": len(observations),
                },
                "source_classification": classification,
                "redaction_check": "metadata-only",
            },
        }

    return check


class CodexAmendmentAuditGate:
    """Require one passing audit per active sovereign before any division.

    The gate is fail-closed and bounded: unknown actors are rejected, every
    one of the five sovereign domains must submit exactly one receipt, the
    Xingcheng receipt must prove its audit used the governed network search,
    and the division plan is released only on unanimous pass.  A cycle that
    cannot be recorded is not a pass (A446: unrecorded-result forbidden).

    Every sovereign audits by its registered standard charter
    (``SOVEREIGN_AUDIT_SPECS``: duties, required evidence, forbidden acts) —
    the same formal procedure for every amendment, whoever originated it.
    Governor rulings 2026-09-17: (1) because the duty audit is a standard
    conformance procedure rather than self-approval, an originating sovereign
    auditing its own duty domain is not a conflict; the request originator is
    disclosed in the receipt/certificate metadata, and an optional
    ``independent_verifier`` reference may record an independent challenge.
    (2) Stage order: when a sovereign originated the amendment, its own
    standard duty audit runs FIRST; the other sovereigns are dispatched only
    after that self-audit passes, and a failed self-audit denies the cycle as
    ``ORIGINATOR_SELF_AUDIT_FAILED`` without dispatching them.  What stays
    forbidden is skipping or altering the standard procedure, fabricating
    evidence, and executor self-verification of its own execution step
    (A446 VERIFY independent from EXECUTE).

    The whole flow is additionally bounded by ``flow_deadline_seconds`` (the
    governor's hard 30-second budget): the async join is cut off at the
    deadline, and a post-hoc monotonic guard denies any cycle whose measured
    wall-clock elapsed the budget, even when blocking synchronous checks kept
    the event loop busy.  An over-budget cycle never releases division and
    never issues a certificate.
    """

    def __init__(
        self,
        *,
        deadline_seconds: float | None = DEFAULT_AUDIT_DEADLINE_SECONDS,
        flow_deadline_seconds: float | None = DEFAULT_AUDIT_FLOW_DEADLINE_SECONDS,
        ledger_path: Path | None = None,
    ) -> None:
        self._deadline = deadline_seconds
        self._flow_deadline = flow_deadline_seconds
        self._ledger_path = Path(ledger_path) if ledger_path else AUDIT_LEDGER_PATH

    async def audit(
        self,
        amendment_id: str,
        checks: Mapping[str, Callable[[], Any]],
        *,
        predecessor: Mapping[str, Any] | None = None,
        requester: str | None = None,
    ) -> AmendmentAuditResult:
        started = time.monotonic()
        identifier = str(amendment_id or "").strip()
        raw_requester = str(requester or "").strip()
        requester_id = SOVEREIGN_ALIASES.get(raw_requester, raw_requester)
        if requester_id and requester_id not in SOVEREIGN_IDS:
            return self._deny(
                identifier,
                "UNKNOWN_AMENDMENT_REQUESTER",
                unknown=[requester_id],
                started=started,
                requester=raw_requester,
            )
        if not identifier:
            return self._deny(
                "", "AMENDMENT_ID_REQUIRED", started=started, requester=requester_id
            )
        normalized: dict[str, Callable[[], Any]] = {}
        unknown: list[str] = []
        for raw_id, check in checks.items():
            sovereign_id = _normalize_sovereign(raw_id)
            if sovereign_id not in SOVEREIGN_IDS or sovereign_id in normalized:
                unknown.append(str(raw_id))
                continue
            normalized[sovereign_id] = check
        missing = [item for item in SOVEREIGN_IDS if item not in normalized]
        if unknown:
            return self._deny(
                identifier,
                "UNKNOWN_AUDIT_ACTOR",
                unknown=unknown,
                started=started,
                requester=requester_id,
            )
        if missing:
            return self._deny(
                identifier,
                "MISSING_SOVEREIGN_AUDIT",
                missing=missing,
                started=started,
                requester=requester_id,
            )

        self_audit_passed: bool | None = None
        if requester_id:
            # Stage 1 (originator self-audit): the originating sovereign runs
            # its own standard duty audit first.  The other active sovereigns
            # are dispatched only after this receipt passes.
            self_receipt = await self._run_one(
                requester_id, normalized[requester_id]
            )
            self_audit_passed = self_receipt.ok
            if not self_receipt.ok:
                result = AmendmentAuditResult(
                    amendment_id=identifier,
                    ok=False,
                    reason="ORIGINATOR_SELF_AUDIT_FAILED",
                    receipts=(self_receipt,),
                    failed=(self_receipt.sovereign_id,),
                    duration_ms=self._elapsed_ms(started),
                    budget_ms=self._budget_ms(),
                    requester=requester_id,
                    requester_independent_verifier=self_receipt.independent_verifier,
                    self_audit_passed=False,
                )
                recorded = self._record(result)
                return replace(result, audit_recorded=recorded)
            try:
                others = tuple(
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(
                                self._run_one(
                                    sovereign_id, normalized[sovereign_id]
                                )
                                for sovereign_id in SOVEREIGN_IDS
                                if sovereign_id != requester_id
                            )
                        ),
                        timeout=self._remaining_flow_budget(started),
                    )
                )
            except asyncio.TimeoutError:
                return self._deny_flow_deadline(
                    identifier,
                    started,
                    receipts=(self_receipt,),
                    requester=requester_id,
                    self_audit_passed=True,
                )
            receipts = (self_receipt, *others)
        else:
            try:
                receipts = tuple(
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(
                                self._run_one(
                                    sovereign_id, normalized[sovereign_id]
                                )
                                for sovereign_id in SOVEREIGN_IDS
                            )
                        ),
                        timeout=self._flow_deadline,
                    )
                )
            except asyncio.TimeoutError:
                return self._deny_flow_deadline(
                    identifier, started, requester=requester_id
                )
        if self._flow_budget_exceeded(started):
            return self._deny_flow_deadline(
                identifier,
                started,
                receipts=receipts,
                requester=requester_id,
                self_audit_passed=self_audit_passed,
            )

        failed = tuple(
            receipt.sovereign_id for receipt in receipts if not receipt.ok
        )
        reason = "ALL_FIVE_SOVEREIGNS_AUDITED" if not failed else "SOVEREIGN_AUDIT_FAILED"
        requester_verifier = next(
            (
                receipt.independent_verifier
                for receipt in receipts
                if receipt.sovereign_id == requester_id
            ),
            "",
        )
        result = AmendmentAuditResult(
            amendment_id=identifier,
            ok=not failed,
            reason=reason,
            receipts=receipts,
            failed=failed,
            division_plan=_division_plan(identifier) if not failed else None,
            certificate=(
                _certificate(identifier, receipts, predecessor, requester_id)
                if not failed
                else None
            ),
            duration_ms=self._elapsed_ms(started),
            budget_ms=self._budget_ms(),
            requester=requester_id,
            requester_independent_verifier=requester_verifier,
            self_audit_passed=self_audit_passed,
        )
        recorded = self._record(result)
        if not result.ok:
            return result
        if not recorded:
            return replace(
                result,
                ok=False,
                reason="AUDIT_RECORD_FAILED",
                division_plan=None,
                audit_recorded=False,
            )
        return replace(result, audit_recorded=True)

    async def _run_one(
        self, sovereign_id: str, check: Callable[[], Any]
    ) -> SovereignAuditReceipt:
        started = time.perf_counter()
        payload: Mapping[str, Any] = {}
        error = ""
        try:
            outcome = check()
            if inspect.isawaitable(outcome):
                if self._deadline is not None:
                    outcome = await asyncio.wait_for(outcome, self._deadline)
                else:
                    outcome = await outcome
            payload = outcome if isinstance(outcome, Mapping) else {"ok": bool(outcome)}
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = "TimeoutError"
        except Exception as exc:  # noqa: BLE001 - fail closed, record the type
            error = type(exc).__name__
        duration_ms = int((time.perf_counter() - started) * 1000)

        spec = _SPEC_BY_ID[sovereign_id]
        domain = spec.domain
        ok = payload.get("ok") is True and not error
        method = str(payload.get("method") or f"{sovereign_id}-audit")
        network_search = payload.get("network_search") is True
        if sovereign_id == NETWORK_AUDIT_SOVEREIGN and not network_search:
            ok = False
            error = error or "NETWORK_AUDIT_PATH_REQUIRED"
        raw_evidence = payload.get("evidence")
        evidence_map = (
            dict(raw_evidence) if isinstance(raw_evidence, Mapping) else {}
        )
        missing_evidence = tuple(
            key
            for key in spec.required_evidence
            if not (
                network_search
                if key == "network_search"
                else _present(evidence_map.get(key))
            )
        )
        if missing_evidence and not error:
            ok = False
            error = f"AUDIT_EVIDENCE_INCOMPLETE:{','.join(missing_evidence)}"
        findings = tuple(
            str(item)[:200] for item in tuple(payload.get("findings") or ())[
                :MAX_FINDINGS
            ]
        )
        evidence_hash = str(payload.get("evidence_hash") or "").strip()
        if not evidence_hash:
            evidence_hash = _canonical_hash(
                {
                    "sovereign_id": sovereign_id,
                    "method": method,
                    "findings": findings,
                    "evidence": payload.get("evidence"),
                }
            )
        if not ok and not error:
            error = str(payload.get("error") or "AUDIT_NOT_PASSED")
        raw_verifier = str(payload.get("independent_verifier") or "").strip()
        independent_verifier = SOVEREIGN_ALIASES.get(raw_verifier, raw_verifier)
        return SovereignAuditReceipt(
            sovereign_id=sovereign_id,
            domain=domain,
            ok=ok,
            method=method,
            evidence_hash=evidence_hash,
            owner_sub_sovereign=spec.owner_sub_sovereign,
            network_search=network_search,
            duration_ms=duration_ms,
            independent_verifier=independent_verifier,
            findings=findings,
            error=error,
        )

    def _deny_flow_deadline(
        self,
        amendment_id: str,
        started: float,
        *,
        receipts: tuple[SovereignAuditReceipt, ...] = (),
        requester: str = "",
        self_audit_passed: bool | None = None,
    ) -> AmendmentAuditResult:
        """Deny a cycle that elapsed the hard whole-flow budget.

        Fail-closed: the amendment audit is not a pass when it is slower than
        the governor's deadline, even if every sovereign receipt passed.
        """
        result = AmendmentAuditResult(
            amendment_id=amendment_id,
            ok=False,
            reason=AUDIT_FLOW_DEADLINE_REASON,
            receipts=receipts,
            failed=tuple(
                receipt.sovereign_id for receipt in receipts if not receipt.ok
            ),
            duration_ms=self._elapsed_ms(started),
            budget_ms=self._budget_ms(),
            requester=requester,
            self_audit_passed=self_audit_passed,
        )
        recorded = self._record(result)
        return replace(result, audit_recorded=recorded)

    def _elapsed_ms(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _budget_ms(self) -> int:
        if self._flow_deadline is None:
            return 0
        return int(self._flow_deadline * 1000)

    def _flow_budget_exceeded(self, started: float) -> bool:
        return (
            self._flow_deadline is not None
            and (time.monotonic() - started) > self._flow_deadline
        )

    def _remaining_flow_budget(self, started: float) -> float | None:
        """Remaining whole-flow budget for the post-self-audit stage."""
        if self._flow_deadline is None:
            return None
        return max(0.0, self._flow_deadline - (time.monotonic() - started))

    def _deny(
        self,
        amendment_id: str,
        reason: str,
        *,
        unknown: Sequence[str] = (),
        missing: Sequence[str] = (),
        started: float | None = None,
        requester: str = "",
    ) -> AmendmentAuditResult:
        duration_ms = self._elapsed_ms(started) if started is not None else 0
        result = AmendmentAuditResult(
            amendment_id=amendment_id,
            ok=False,
            reason=reason,
            failed=tuple(unknown),
            missing=tuple(missing),
            duration_ms=duration_ms,
            budget_ms=self._budget_ms(),
            requester=requester,
            audit_recorded=self._record_payload(
                {
                    "amendment_id": amendment_id,
                    "ok": False,
                    "reason": reason,
                    "failed": list(unknown),
                    "missing": list(missing),
                    "receipts": [],
                    "division_released": False,
                    "duration_ms": duration_ms,
                    "budget_ms": self._budget_ms(),
                    "requester": requester,
                }
            ),
        )
        return result

    def _record(self, result: AmendmentAuditResult) -> bool:
        return self._record_payload(result.to_record())

    def _record_payload(self, payload: Mapping[str, Any]) -> bool:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "operation": "codex-amendment-five-sovereign-audit",
            **dict(payload),
        }
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self._ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
                )
                handle.flush()
            return True
        except OSError:
            return False


__all__ = [
    "AMENDMENT_TEST_SUITE_BUDGET_SECONDS",
    "AUDIT_FLOW_DEADLINE_REASON",
    "AUDIT_LEDGER_PATH",
    "AmendmentAuditResult",
    "CERTIFICATE_SCHEMA",
    "CodexAmendmentAuditGate",
    "DEFAULT_AUDIT_DEADLINE_SECONDS",
    "DEFAULT_AUDIT_FLOW_DEADLINE_SECONDS",
    "DIVISION_ASSIGNMENTS",
    "SOVEREIGN_AUDIT_SPECS",
    "SOVEREIGN_DOMAINS",
    "SOVEREIGN_IDS",
    "SovereignAuditReceipt",
    "SovereignAuditSpec",
    "build_xingcheng_network_check",
]
