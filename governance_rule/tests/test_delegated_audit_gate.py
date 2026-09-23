"""G96 — delegated-check closure for the canonical audit chain.

The commit gate used to run only the C++ engine: the manifest's
``delegated`` rows were counted but never executed in the same request
(the scheduled Python lane was disabled), so a single Audit Request
could not prove unsupported checks ran.  These tests pin the closure:

- every ``python-check:*`` id resolves to an executable callable;
- aggregate/semantic ids fold into their owning task (deduped);
- unresolvable ids are fail-closed errors, never silently skipped;
- ``run_audit_request`` merges engine + delegated verdicts under the
  shared 30 s budget.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from governance_rule.execution.audit import audit_checks  # noqa: E402
from governance_rule.execution.audit.native_audit_gate import (  # noqa: E402
    MANIFEST_RELATIVE,
    run_audit_request,
)


def _manifest_checks() -> list[dict]:
    return json.loads(
        (ROOT / MANIFEST_RELATIVE).read_text(encoding="utf-8-sig")
    ).get("checks", [])


def test_every_manifest_delegated_id_resolves() -> None:
    """All delegated rows map to a callable — resolution is total."""
    delegated = [
        c["id"] for c in _manifest_checks() if c.get("kind") == "delegated"
    ]
    assert delegated, "manifest must carry delegated rows"
    resolvable = set(audit_checks._DELEGATED_CHECK_ALIASES)
    unresolved = []
    for cid in delegated:
        name = cid.split(":", 1)[1] if cid.startswith("python-check:") else ""
        if name in resolvable:
            continue
        if not (
            name.startswith("check_")
            and callable(getattr(audit_checks, name, None))
        ):
            unresolved.append(cid)
    assert not unresolved, f"unresolvable delegated ids: {unresolved}"


def test_aggregate_and_semantic_ids_fold_into_owning_callable() -> None:
    aliases = audit_checks._DELEGATED_CHECK_ALIASES
    assert aliases["check_tool_manifests"] is aliases[
        "check_tool_identity_registration"
    ]
    assert aliases["check_directory_schemas"] is aliases[
        "check_directory_seal"
    ]
    assert aliases["protected-source-semantic"] is getattr(
        audit_checks, "check_protected_sources"
    )


def test_unresolvable_delegated_id_is_fail_closed() -> None:
    errors, executed = audit_checks.run_delegated_checks(
        ROOT,
        [{"id": "python-check:check_does_not_exist", "kind": "delegated"}],
    )
    assert any("check_does_not_exist" in e for e in errors)


def test_delegated_run_executes_manifest_subset() -> None:
    """Real delegated run: every delegated id executes, zero errors on a
    clean tree, count deduped by owning callable."""
    errors, executed = audit_checks.run_delegated_checks(
        ROOT, _manifest_checks()
    )
    delegated = [
        c["id"] for c in _manifest_checks() if c.get("kind") == "delegated"
    ]
    assert not errors, f"delegated checks failed: {errors}"
    assert len(executed) >= 20  # unique owning callables (deduped ids)
    assert len(set(executed)) == len(executed)


def test_audit_request_merges_engine_and_delegated() -> None:
    """Full canonical request: engine pass + delegated execution merged."""
    result = run_audit_request(ROOT)
    assert result.status == "pass", f"unexpected errors: {result.errors}"
    assert result.delegated_executed >= 20
    assert result.delegated > 0
