"""Regression tests for the lazy audit entry point (P0-2 import hygiene).

``governance_rule.execution.audit`` used to import the entire check
suite — ``audit_checks`` plus every ``audit_*`` module, ``git_tiers``,
``governance_policy`` and the permission-directory chain — at package
import time, and it touched the git-tier audit ledger on disk as an
import side effect.  That meant the commit gate's *native* phase paid
for the whole Python oracle just by reaching the ``native_audit_gate``
submodule.  These tests pin the lazy contract in fresh interpreters:

- importing the package (or a submodule) must not load ``audit_checks``;
- resolving ``audit_runtime_governance`` loads it on demand;
- ``audit_self_health`` (subprocess test collection) is only imported
  when the self-health barrier actually runs.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_PY = sys.executable


def _fresh_import(*statements: str) -> dict:
    """Run statements in a clean interpreter; return module snapshot."""
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        + "\n".join(statements)
        + "\nprint(json.dumps(sorted(sys.modules)))\n"
    )
    proc = subprocess.run(
        [_PY, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
        check=True,
    )
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_package_import_does_not_load_check_suite() -> None:
    modules = _fresh_import("import governance_rule.execution.audit")
    assert "governance_rule.execution.audit.audit_checks" not in modules
    assert "governance_rule.execution.audit.audit_artifacts" not in modules
    assert "governance_rule.execution.audit.audit_self_health" not in modules
    # The ledger path is derived locally; importing git_tiers for one
    # constant was the old eager chain.
    assert "governance_rule.execution.git_tiers" not in modules


def test_native_gate_submodule_stays_light() -> None:
    """The commit gate reaches ``native_audit_gate`` through the package —
    that path must not drag the Python oracle in."""
    modules = _fresh_import(
        "from governance_rule.execution.audit.native_audit_gate import "
        "run_native_audit_gate"
    )
    assert "governance_rule.execution.audit.audit_checks" not in modules
    assert "governance_rule.execution.audit.audit_artifacts" not in modules


def test_audit_runtime_governance_resolves_lazily() -> None:
    modules = _fresh_import(
        "from governance_rule.execution.audit import "
        "audit_runtime_governance",
        "assert callable(audit_runtime_governance)",
    )
    assert "governance_rule.execution.audit.audit_checks" in modules


def test_protected_sources_constant_resolves_lazily() -> None:
    modules = _fresh_import(
        "from governance_rule.execution.audit import "
        "REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES as sources",
        "assert sources",
    )
    assert "governance_rule.execution.audit.audit_protected" in modules


def test_self_health_module_imported_only_when_barrier_runs() -> None:
    """``include_self_health=False`` paths must never pull the
    subprocess-collecting self-health module."""
    modules = _fresh_import(
        "from pathlib import Path",
        "from governance_rule.execution.audit import audit_runtime_governance",
        "audit_runtime_governance(Path(" + repr(str(ROOT)) + "), "
        "include_self_health=False)",
    )
    assert "governance_rule.execution.audit.audit_self_health" not in modules
