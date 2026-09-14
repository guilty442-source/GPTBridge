"""Governance rule coordination — wires the Governance Codex (法典) into the
main system under the System Sovereign.

The Governance Codex is the HIGHEST rule layer: a pure, immutable, declarative
canon.  It holds no executable functions and can never be mutated or patched
(`mutability="immutable-sealed"`, `function="none"`,
`amendment="explicit-versioned-full-replacement-only"`).

The sovereign surfaces the codex as the supreme authority at the decision level
only — it is read-only and never mutates governance.  The legacy (pre-codex)
``governance_policy`` is surfaced separately as a read-only ``legacy_functional``
detail; per the codex it is no longer a functional top rule, and its executable
components are retired in a later parallel pass (not removed here).
"""

from __future__ import annotations

from typing import Any

from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot
from governance_rule.execution.codex_reconcile import bounded_lookup
from governance_rule.execution.codex_repository import format_codex_version
from governance_rule.execution.codex_dual_key import mint_dual_key_grant
from governance_rule.execution.codex_session import open_review_session
from governance_rule.permission_directory.governance_policy import (
    DEFAULT_ACTIVE_GOVERNANCE_RULES,
    GOVERNANCE_POLICY,
    GOVERNANCE_RULE_CATALOG,
)


class GovernanceRuleCoordination:
    """Read-only, decision-level surface for the Governance Codex under the
    sovereign.  The codex is the supreme rule layer; the pre-codex policy is
    only a read-only functional detail."""

    def __init__(self, app: Any) -> None:
        self.app = app

    # ------------------------------------------------------------------
    # Coordination surface
    # ------------------------------------------------------------------

    def coordination_status(self) -> dict[str, Any]:
        """Snapshot of the supreme Governance Codex plus legacy detail."""

        # A435 REVIEW_SESSION: the coordination surface reads the full
        # codex projection through the official entry (identity + purpose
        # + scope + nonce + expiry + metadata-only audit).
        # A174 two-key boundary: a full-codex snapshot is privileged and
        # requires a grant countersigned by the entry owner.
        grant = mint_dual_key_grant(
            operation="codex-open:review-session",
            primary_actor="governance-coordination",
            secondary_actor="permission-sovereign",
            purpose="coordination",
            scope=("codex:full",),
            access_class="review-session",
        )
        with open_review_session(
            "governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
            dual_key_grant=grant,
        ) as session:
            codex = session.snapshot()
        active_rules = self._active_rules()

        return {
            "authority": "codex-supreme",
            "rule_layer": "codex",
            "codex_schema": codex.schema,
            "codex_version": format_codex_version(codex.codex_version),
            "authority_rank": codex.preamble.authority_rank,
            "binding_scope": codex.preamble.binding_scope,
            "function": codex.savings.function,
            "mutability": codex.savings.mutability,
            "amendment": codex.savings.amendment,
            "interpretation": codex.savings.interpretation,
            "conflict_resolution": codex.savings.conflict_resolution,
            "sections": [
                {"index": s.index, "title": s.title} for s in codex.sections
            ],
            "principles": [
                {"id": p.id, "statement": p.statement}
                for p in codex.principles
            ],
            "articles": [
                {
                    "id": a.id,
                    "section": a.section,
                    "subject": a.subject,
                    "rule": a.rule,
                    "prohibition": a.prohibition,
                    "exception": a.exception,
                }
                for a in codex.articles
            ],
            "edicts": [
                {"id": e.id, "area": e.area, "edict": e.edict}
                for e in codex.edicts
            ],
            "sovereigns": [
                {
                    "id": s.id,
                    "name": s.name,
                    "rank": s.rank,
                    "duties": list(s.duties),
                    "powers": list(s.powers),
                    "prohibitions": list(s.prohibitions),
                    "basis": s.basis,
                }
                for s in codex.sovereigns
            ],
            "active_rule": active_rules,
            "catalog": list(GOVERNANCE_RULE_CATALOG),
            "directory": {
                "approved_tools": list(code_rule_directory_snapshot().approved_tool_ids),
                "approved_capabilities": list(
                    code_rule_directory_snapshot().approved_capability_names
                ),
                "approved_actions": list(
                    code_rule_directory_snapshot().approved_action_names
                ),
                "initial_code_version": code_rule_directory_snapshot().initial_code_version,
            },
            "legacy_functional": {
                "note": (
                    "pre-codex governance_policy surfaced read-only; not a top rule "
                    "and retired in a later parallel pass"
                ),
                "authority_version": GOVERNANCE_POLICY.authority_version,
                "decision": "read-only",
                "function": False,
            },
            "chinese_reference": {
                "status": "synchronized-non-authoritative-reference",
                "scope": "human-reference-only",
                "principles_count": len(codex.principles),
                "articles_count": len(codex.articles),
                "edicts_count": len(codex.edicts),
                "sovereigns_count": len(codex.sovereigns),
            },
            "decision": "read-only",
        }

    def orchestration_status(self) -> dict[str, Any]:
        """Unified subsystem view for the sovereign orchestration report."""

        identity = bounded_lookup(
            "governance-coordination",
            purpose="status",
            scope=("codex:identity",),
            reader=lambda ctx: ctx.codex_identity(),
        )
        return {
            "name": "governance-rules",
            "authority": "codex-supreme",
            "rule_layer": "codex",
            "codex_version": identity["codex_version_text"],
            "active_rule": self._active_rules(),
            "state": "sealed",
            "function": "none",
            "decision": "read-only",
            "delegation": "governed-executor-only",
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _active_rules(self) -> list[str]:
        """Active rule names from the app, falling back to the sealed catalog."""

        value = getattr(self.app, "governance_rules", None)
        if isinstance(value, (list, tuple)) and value:
            return [str(item) for item in value]
        return list(DEFAULT_ACTIVE_GOVERNANCE_RULES)


__all__ = ["GovernanceRuleCoordination"]
