"""Formal rule machine evaluation engine (A437-A449 formal rules).

法典依據:
- A445 FORMALIZATION: every machine-enforced invariant binds one
  ``formal_rule_code`` carrying typed inputs, predicate, decision, severity,
  evidence schema, precedence scope, low-cost path and controlling
  provision; evaluation records rule code, input hashes, result, evidence
  ids, policy version and timestamp (PROOF).
- A445 PRECEDENCE: exact special-law scope > later explicit successor >
  subordinate ordinance > general article.  Unresolved conflict is
  INCOMPLETE_EVIDENCE — never left to checker interpretation.
- A445 DEDUPLICATION: one formal rule owns each repeated boundary; other
  provisions reference its code.
- A435 BOUNDED_MACHINE_LOOKUP: rule definitions are read from the official
  codex (PostgreSQL authority) through the governed read-only repository
  connection; registry content is non-content identity/status/binding data.

This module is the single machine consumer of ``formal_rule_registry`` and
``formal_rule_mapping``.  Each active MACHINE_ENFORCED formal rule resolves
to exactly one predicate evaluator; evaluation emits a typed evidence record
per A445 PROOF.  A rule with no evaluator is a FAIL (never treated PASS).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg

from governance_rule.execution.codex_repository import (
    CODEX_DATABASE_PATH,
    codex_readonly_connection,
)

# ---------------------------------------------------------------------------
# Contract types (A445 machine contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormalRule:
    """One active row of ``formal_rule_registry``."""

    rule_code: str
    controlling_provision_id: str
    required_inputs: tuple[str, ...]
    predicate: str
    pass_decision: str
    fail_decision: str
    severity: str
    evidence_schema: str
    low_cost_path: str
    precedence_scope: str
    version_identity: str
    status: str

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> "FormalRule":
        inputs = tuple(
            part
            for part in re.split(r"[|\s]+", str(row.get("required_inputs", "")))
            if part
        )
        return cls(
            rule_code=str(row.get("rule_code", "")),
            controlling_provision_id=str(row.get("controlling_provision_id", "")),
            required_inputs=inputs,
            predicate=str(row.get("predicate", "")),
            pass_decision=str(row.get("pass_decision", "PASS")),
            fail_decision=str(row.get("fail_decision", "FAIL")),
            severity=str(row.get("severity", "high")),
            evidence_schema=str(row.get("evidence_schema", "")),
            low_cost_path=str(row.get("low_cost_path", "")),
            precedence_scope=str(row.get("precedence_scope", "")),
            version_identity=str(row.get("version_identity", "")),
            status=str(row.get("status", "active")),
        )


@dataclass(frozen=True)
class FormalOutcome:
    """A445 PROOF record: rule_code, input hash, result, evidence, version."""

    rule_code: str
    controlling_provision_id: str
    predicate: str
    decision: str
    passed: bool
    severity: str
    evidence_schema: str
    precedence_scope: str
    evidence_id: str
    input_excerpt: str
    reason: str
    policy_version: str
    recorded_at_utc: str

    def as_dict(self) -> dict[str, str]:
        return {
            "rule_code": self.rule_code,
            "controlling_provision_id": self.controlling_provision_id,
            "predicate": self.predicate,
            "decision": self.decision,
            "passed": "true" if self.passed else "false",
            "severity": self.severity,
            "evidence_schema": self.evidence_schema,
            "precedence_scope": self.precedence_scope,
            "evidence_id": self.evidence_id,
            "input_excerpt": self.input_excerpt,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "recorded_at_utc": self.recorded_at_utc,
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def input_hash(facts: Mapping[str, Any]) -> str:
    """Canonical hash of typed inputs (A445 PROOF input hashes)."""
    payload = json.dumps(
        {str(k): _normalize(v) for k, v in sorted(facts.items())},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items())}
    return value


# ---------------------------------------------------------------------------
# Rule registry loading (A435 bounded lookup, governed read-only connection)
# ---------------------------------------------------------------------------

FORMAL_RULE_REGISTRY = "formal_rule_registry"
FORMAL_RULE_MAPPING = "formal_rule_mapping"

# A rule leaves enforcement only through an explicit terminal lifecycle; a
# rule declared with parity debt (``declared-pending-evaluator-parity``)
# remains in force and still requires a machine evaluator (A445).
_TERMINAL_RULE_STATUSES = frozenset(
    {"retired", "superseded", "withdrawn", "inactive"}
)


def _registry_rows(
    connection: Any, table: str
) -> tuple[dict[str, str], ...]:
    columns = [
        column[1] for column in connection.execute(f"PRAGMA table_info({table})")
    ]
    return tuple(
        dict(zip(columns, row))
        for row in connection.execute(f"SELECT * FROM {table}")  # sql-ok: PRAGMA-driven all-columns contract snapshot
    )


@dataclass(frozen=True)
class FormalRuleSet:
    """Snapshot of the formal rule contracts from the official codex."""

    rules: tuple[FormalRule, ...] = ()
    mappings: tuple[dict[str, str], ...] = ()

    def active_rules(self) -> tuple[FormalRule, ...]:
        """In-force rules: every row whose lifecycle is not terminal.

        Declared rules awaiting evaluator parity are still in force — the
        evaluator is required and a rule without one is never treated PASS
        (A445 FORBID:missing-formal-rule-treated-PASS).  Filtering on a
        single historic status value would silently disable enforcement.
        """
        return tuple(
            rule
            for rule in self.rules
            if str(rule.status).strip().lower() not in _TERMINAL_RULE_STATUSES
        )

    def rule(self, rule_code: str) -> FormalRule | None:
        for rule in self.rules:
            if rule.rule_code == rule_code:
                return rule
        return None

    def mapped_provision_ids(self) -> tuple[str, ...]:
        return tuple(
            str(row.get("provision_id", ""))
            for row in self.mappings
            if str(row.get("mapping_status", "")) == "VERIFIED"
        )


def _ensure_evaluators_loaded() -> None:
    """Ensure evaluator module is loaded and registrations finalized."""
    if not _EVALUATORS:
        import governance_rule.execution.formal_rules.evaluators as evaluators_module
        evaluators_module.finalize_registrations()


def load_formal_rules(
    database: Path = CODEX_DATABASE_PATH,
) -> FormalRuleSet:
    """Read formal rule registry + mapping from the official codex.

    Uses the governed read-only repository connection (A279/A435).  On any
    unreadable state the set degrades to empty (fail-closed callers treat
    a missing rule as FAIL).
    """
    _ensure_evaluators_loaded()
    try:
        with codex_readonly_connection(database) as connection:
            rules = tuple(
                FormalRule.from_row(row)
                for row in _registry_rows(connection, FORMAL_RULE_REGISTRY)
            )
            mappings = _registry_rows(connection, FORMAL_RULE_MAPPING)
        return FormalRuleSet(rules=rules, mappings=mappings)
    except (OSError, psycopg.Error, ValueError, KeyError, RuntimeError):
        return FormalRuleSet()


# ---------------------------------------------------------------------------
# Predicate evaluator registry (one evaluator per MACHINE_ENFORCED rule)
# ---------------------------------------------------------------------------

# Evaluator signature: facts -> (passed, decision, reason)
PredicateEvaluator = Callable[[Mapping[str, Any]], tuple[bool, str, str]]

_EVALUATORS: dict[str, PredicateEvaluator] = {}


def register_rule(
    rule_code: str,
) -> Callable[[PredicateEvaluator], PredicateEvaluator]:
    """Register the machine predicate for one formal rule code (A445)."""

    def decorator(evaluator: PredicateEvaluator) -> PredicateEvaluator:
        if rule_code in _EVALUATORS:
            raise RuntimeError(f"formal rule evaluator already registered: {rule_code}")
        _EVALUATORS[rule_code] = evaluator
        return evaluator

    return decorator


def registered_rule_codes() -> frozenset[str]:
    return frozenset(_EVALUATORS)


def evaluate_rule(
    rule: FormalRule,
    facts: Mapping[str, Any],
    *,
    now: str | None = None,
) -> FormalOutcome:
    """Evaluate one formal rule against typed facts (A445 PROOF).

    A rule with no registered evaluator is never PASS: the decision is the
    rule's fail decision and evidence id is ``missing-evaluator``.
    """
    evaluator = _EVALUATORS.get(rule.rule_code)
    missing = evaluator is None
    if missing:
        passed, decision, reason = (
            False,
            rule.fail_decision,
            f"no machine evaluator registered for {rule.rule_code}",
        )
    else:
        try:
            passed, decision, reason = evaluator(facts)
        except Exception as error:  # noqa: BLE001 — evaluators must fail closed
            passed, decision, reason = (
                False,
                rule.fail_decision,
                f"evaluator raised {error.__class__.__name__}: {error}",
            )
    evidence_id = (
        "missing-evaluator"
        if missing
        else hashlib.sha256(
            (rule.rule_code + ":" + input_hash(facts)).encode("utf-8")
        ).hexdigest()[:20]
    )
    excerpt = json.dumps(
        {k: _normalize(v) for k, v in list(facts.items())[:8]},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return FormalOutcome(
        rule_code=rule.rule_code,
        controlling_provision_id=rule.controlling_provision_id,
        predicate=rule.predicate,
        decision=str(decision),
        passed=bool(passed),
        severity=rule.severity,
        evidence_schema=rule.evidence_schema,
        precedence_scope=rule.precedence_scope,
        evidence_id=evidence_id,
        input_excerpt=excerpt,
        reason=str(reason),
        policy_version=rule.version_identity,
        recorded_at_utc=now or _utc_now(),
    )


def evaluate_all(
    ruleset: FormalRuleSet | None = None,
    facts_by_rule: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    database: Path = CODEX_DATABASE_PATH,
) -> tuple[FormalOutcome, ...]:
    """Evaluate every active formal rule.

    ``facts_by_rule`` keys are rule codes; rules without supplied facts are
    evaluated with empty facts (reported against the missing-input reason).
    """
    ruleset = ruleset or load_formal_rules(database)
    facts_by_rule = dict(facts_by_rule or {})
    outcomes: list[FormalOutcome] = []
    for rule in ruleset.active_rules():
        facts = facts_by_rule.get(rule.rule_code, {})
        outcomes.append(evaluate_rule(rule, facts))
    return tuple(outcomes)


def missing_evaluator_codes(ruleset: FormalRuleSet | None = None) -> tuple[str, ...]:
    """Formal rule codes with no machine predicate (A445 forbid: missing rule PASS)."""
    ruleset = ruleset or load_formal_rules()
    return tuple(
        rule.rule_code
        for rule in ruleset.active_rules()
        if rule.rule_code not in _EVALUATORS
    )


__all__ = [
    "FORMAL_RULE_MAPPING",
    "FORMAL_RULE_REGISTRY",
    "FormalOutcome",
    "FormalRule",
    "FormalRuleSet",
    "evaluate_all",
    "evaluate_rule",
    "input_hash",
    "load_formal_rules",
    "missing_evaluator_codes",
    "register_rule",
    "registered_rule_codes",
]