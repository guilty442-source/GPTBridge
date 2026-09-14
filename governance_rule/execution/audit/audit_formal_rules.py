"""Formal rule and implementation-obligation audit checks (A445/A292).

These checks connect the formal governance registries to the single runtime
audit gate:

* ``check_formal_rules`` — every active ``MACHINE_ENFORCED`` formal rule
  bound to a ``VERIFIED`` mapping must have a machine predicate evaluator
  registered.  A rule without an evaluator is never treated PASS
  (A445 FORBID:missing-formal-rule-treated-PASS); a mapping row whose
  provision id is not active in the registry is reported.
* ``check_implementation_obligations`` — every implementation obligation
  must be declared in the registry with owner, target state, due date and
  acceptance evidence; an overdue non-terminal obligation is an error
  (A292 escalation at due-at).

Both checks read through the governed read-only repository connection and
degrade to findings on an unreadable codex.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from governance_rule.execution.formal_rules import (
    FORMAL_RULE_MAPPING,
    FORMAL_RULE_REGISTRY,
    load_formal_rules,
    missing_evaluator_codes,
)
from governance_rule.execution.formal_rules.obligations import (
    LIFECYCLE_MAIN,
    LIFECYCLE_TERMINAL,
    load_obligations,
)

_REQUIRED_OBLIGATION_CODES = frozenset(
    {
        "OBL_LAYERED_ARCHITECTURE",
        "OBL_ARCHITECTURE_CATALOG",
        "OBL_TEST_SUITE_DIRECTORY",
        "OBL_TOP_LEVEL_PATHS",
        "OBL_ANTI_JAILBREAK",
        "OBL_SYSTEM_RELIABILITY",
        "OBL_星澄_AUDIT",
        "OBL_CHANNEL_ANOMALY_ISOLATION",
        "OBL_NATIVE_PROMOTION_RECORD_CHECKER",
        "OBL_LANGUAGE_DEPENDENCY_DAG_GATE",
    }
)


def check_formal_rules(root: Path, errors: list[str]) -> None:
    """Every active MACHINE_ENFORCED formal rule has a machine predicate.

    A445 FORBID:missing-formal-rule-treated-PASS — a rule with no evaluator
    (or an unregistered mapping) is a hard finding.
    """
    ruleset = load_formal_rules()
    if not ruleset.rules:
        errors.append(
            f"formal rule registry is empty or unreadable ({FORMAL_RULE_REGISTRY})"
        )
        return
    missing = missing_evaluator_codes(ruleset)
    if missing:
        errors.append(
            "formal rules without machine evaluator: "
            + ", ".join(sorted(missing))
        )
    mapped = set(ruleset.mapped_provision_ids())
    active = {rule.controlling_provision_id for rule in ruleset.active_rules()}
    for provision_id in sorted(mapped):
        if provision_id not in active:
            errors.append(
                f"formal rule mapping {provision_id} not present in active registry"
            )
    for rule in ruleset.active_rules():
        if rule.controlling_provision_id not in mapped:
            errors.append(
                f"active formal rule {rule.rule_code} (A{rule.controlling_provision_id}) "
                "missing VERIFIED mapping row"
            )


def check_implementation_obligations(root: Path, errors: list[str]) -> None:
    """Every mandated obligation is declared, owned, dated and not overdue.

    A292: at due-at an unfulfilled obligation is escalated and blocks the
    affected activation and verified release; a missing registry entry or a
    past-due non-terminal state is a hard finding.
    """
    obligations = load_obligations()
    if not obligations:
        errors.append("implementation obligations registry is empty or unreadable")
        return
    seen = {item.obligation_code for item in obligations}
    for code in sorted(_REQUIRED_OBLIGATION_CODES):
        if code not in seen:
            errors.append(f"required implementation obligation missing: {code}")
    now = datetime.now(timezone.utc)
    for item in obligations:
        if not item.implementation_owner:
            errors.append(
                f"obligation {item.obligation_code} has no implementation owner"
            )
        if not item.target_state:
            errors.append(
                f"obligation {item.obligation_code} has no declared target state"
            )
        if not item.acceptance_evidence:
            errors.append(
                f"obligation {item.obligation_code} lacks acceptance evidence "
                "(A288: no completion without evidence)"
            )
        if item.current_state not in LIFECYCLE_MAIN + LIFECYCLE_TERMINAL:
            errors.append(
                f"obligation {item.obligation_code} has unregistered state "
                f"{item.current_state!r}"
            )
        if item.is_overdue(now):
            errors.append(
                f"obligation {item.obligation_code} is overdue (due {item.due_at_utc}) "
                "and not completed/superseded"
            )


__all__ = [
    "check_formal_rules",
    "check_implementation_obligations",
]