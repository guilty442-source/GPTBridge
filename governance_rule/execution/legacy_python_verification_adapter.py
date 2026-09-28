"""LegacyPythonVerificationAdapter — the sole legal pytest spawn point.

Codex (test-runner-register-and-python-retirement-transition +
test-suite-native-runner-and-legacy-verification-transition +
python-minimum-three-domain-transition):

- DEVELOPMENT-VERIFICATION only: on-demand, bounded, never resident,
  process exits after the suite — no own scheduler, no release authority,
  no Codex/Git authority.
- Every spawn normalizes to TEST_RESULT_V1 and emits evidence.
- Scope is fail-closed against PYTEST_RETIREMENT_INVENTORY: a test file
  absent from the inventory is a *new* Python test (FORBID) and the run
  returns ERROR instead of executing it.
- Only ``inventory_class == "H"`` (genuine governance semantic) rows may
  execute.  Module-class rows are migration sources pending native
  replacement — they have no legal Python executor and fail closed.

CLI:

    python -m governance_rule.execution.legacy_python_verification_adapter \
        --scope main-system/tests/test_foo.py [--scope test_foo.py::test_x] \
        [--timeout-s 300] [--workers 2] [--evidence-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CONTRACT = "TEST_RESULT_V1"
_ADAPTER_ID = "LegacyPythonVerificationAdapter"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_INVENTORY_PATH = (
    _PROJECT_ROOT
    / "governance_rule"
    / "execution"
    / "audit"
    / "pytest_retirement_inventory.json"
)
_DEFAULT_EVIDENCE_DIR = (
    _PROJECT_ROOT / "main-system" / "runtime" / "logs" / "verification"
)
_VENV_PYTHON = (
    _PROJECT_ROOT / "main-system" / ".venv" / "Scripts" / "python.exe"
)

# Bounded execution envelope — the adapter owns no scheduler, so these are
# hard ceilings, not tunables.
_MAX_WORKERS = 4
_MAX_TIMEOUT_S = 900
_DEFAULT_TIMEOUT_S = 300

# pytest.ini registered test scopes (kept in sync with the root ini) plus
# governed tool ``tests/`` directories declared via manifest test_targets.
_ALLOWED_TEST_ROOTS = (
    "governance_rule/tests",
    "governance_rule/execution/git_tiers/tests",
    "main-system/tests",
    "shared-layer/tests",
)


def _in_allowed_roots(relative: str) -> bool:
    if any(
        relative.startswith(root + "/") for root in _ALLOWED_TEST_ROOTS
    ):
        return True
    # Independent-tool test trees: Standalone tools/<tool>/**/tests/**
    return relative.startswith("Standalone tools/") and "/tests/" in (
        "/" + relative
    )

_CASE_NORM = {
    "passed": "PASS",
    "failure": "FAIL",
    "error": "FAIL",
    "skipped": "BLOCKED",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_inventory() -> dict[str, Any]:
    try:
        return json.loads(_INVENTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"PYTEST_RETIREMENT_INVENTORY unreadable: {exc}"
        ) from None


def _resolve_scope_files(scopes: list[str]) -> tuple[list[str], list[str]]:
    """Resolve scope entries to repo-relative test file paths.

    Returns (files, rejected): each scope may be a test file path or a
    ``path::nodeid`` selector.  Anything outside the registered test roots
    is rejected fail-closed.
    """
    files: list[str] = []
    rejected: list[str] = []
    for scope in scopes:
        path_part = scope.split("::", 1)[0]
        candidate = (_PROJECT_ROOT / path_part).resolve()
        try:
            relative = candidate.relative_to(_PROJECT_ROOT).as_posix()
        except ValueError:
            rejected.append(scope)
            continue
        if not _in_allowed_roots(relative):
            rejected.append(scope)
            continue
        if not candidate.is_file():
            rejected.append(scope)
            continue
        if relative not in files:
            files.append(relative)
    return files, rejected


def _inventory_gate(
    files: list[str], inventory: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Return (unregistered, non_governance_class) source files.

    Codex limits the adapter to inventory-class-H genuine governance
    semantic tests; every other row is a migration-only source with no
    legal Python executor.
    """
    rows = {
        str(row.get("source_test")): row
        for row in inventory.get("rows", [])
        if isinstance(row, dict)
    }
    unregistered = [f for f in files if f not in rows]
    non_governance = [
        f
        for f in files
        if f in rows and rows[f].get("inventory_class") != "H"
    ]
    return unregistered, non_governance


def _parse_junit(junit_path: Path) -> list[dict[str, Any]]:
    """Normalize junit-xml cases to TEST_RESULT_V1 rows."""
    cases: list[dict[str, Any]] = []
    try:
        tree = ET.parse(junit_path)
    except (OSError, ET.ParseError):
        return cases
    for case in tree.getroot().iter("testcase"):
        nodeid = f"{case.get('classname', '')}::{case.get('name', '')}"
        status = "PASS"
        detail = ""
        for child in case:
            tag = child.tag.rpartition("}")[2]
            if tag in ("failure", "error"):
                status = _CASE_NORM.get(tag, "FAIL")
                detail = (child.get("message") or "")[:500]
                break
            if tag == "skipped":
                status = "BLOCKED"
                detail = (child.get("message") or "")[:500]
        cases.append(
            {
                "id": nodeid.lstrip(":"),
                "status": status,
                "detail": detail,
                "duration_ms": int(float(case.get("time") or 0) * 1000),
            }
        )
    return cases


def _verdict(cases: list[dict[str, Any]], exit_code: int) -> str:
    if any(c["status"] == "FAIL" for c in cases):
        return "FAIL"
    if not cases:
        return "INCOMPLETE"  # zero cases is not evidence of success
    if exit_code != 0:
        return "FAIL"
    return "PASS"


def run(
    scopes: list[str],
    *,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    workers: int = 2,
    evidence_dir: Path = _DEFAULT_EVIDENCE_DIR,
) -> dict[str, Any]:
    """Spawn one bounded pytest run and normalize to TEST_RESULT_V1."""
    started = _now_utc()
    clock = time.monotonic()
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    junit_path = evidence_dir / f"junit-{uuid.uuid4().hex[:12]}.xml"

    files, rejected = _resolve_scope_files(scopes)
    inventory = _load_inventory()
    unregistered, non_governance = _inventory_gate(files, inventory)

    def _finish(
        verdict: str,
        cases: list[dict[str, Any]],
        detail: str = "",
        exit_code: int = 0,
    ) -> dict[str, Any]:
        result = {
            "contract": _CONTRACT,
            "adapter": _ADAPTER_ID,
            "runner": "pytest",
            "verdict": verdict,
            "detail": detail,
            "scope": list(scopes),
            "files": files,
            "rejected_scope": rejected,
            "unregistered_files": unregistered,
            "non_governance_files": non_governance,
            "totals": {
                "cases": len(cases),
                "pass": sum(1 for c in cases if c["status"] == "PASS"),
                "fail": sum(1 for c in cases if c["status"] == "FAIL"),
                "blocked": sum(1 for c in cases if c["status"] == "BLOCKED"),
            },
            "exit_code": exit_code,
            "started_at_utc": started,
            "finished_at_utc": _now_utc(),
            "duration_ms": int((time.monotonic() - clock) * 1000),
            "cases": cases,
        }
        payload = json.dumps(result, ensure_ascii=False, indent=1)
        result["evidence_sha256"] = hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()
        evidence_path = evidence_dir / (
            f"test-result-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}Z"
            f"-{uuid.uuid4().hex[:8]}.json"
        )
        evidence_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        result["evidence_path"] = str(evidence_path)
        return result

    if rejected or unregistered or non_governance:
        return _finish(
            "ERROR",
            [],
            detail=(
                "scope rejected: unregistered/out-of-roots "
                f"{sorted(set(rejected + unregistered))} or non-class-H "
                f"{sorted(non_governance)} — only inventory-class-H "
                "governance semantic tests may execute through this "
                "adapter; module-class rows await native replacement"
            ),
        )
    if not files:
        return _finish("ERROR", [], detail="no test files in scope")

    timeout_s = min(max(1, int(timeout_s)), _MAX_TIMEOUT_S)
    workers = min(max(1, int(workers)), _MAX_WORKERS)
    command = [
        str(_VENV_PYTHON if _VENV_PYTHON.is_file() else sys.executable),
        "-m",
        "pytest",
        *files,
        "-n",
        str(workers),
        f"--junitxml={junit_path}",
        "-q",
    ]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # no __pycache__ residue
    # Session chokepoint marker: root conftest.py refuses collection
    # unless the run was spawned through this adapter.
    env["GPTBRIDGE_LEGACY_VERIFICATION_ADAPTER"] = "1"
    try:
        proc = subprocess.run(
            command,
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cases = _parse_junit(junit_path)
        verdict = _verdict(cases, proc.returncode)
        return _finish(
            verdict,
            cases,
            detail=(proc.stdout or "")[-2000:],
            exit_code=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return _finish(
            "TIMEOUT",
            _parse_junit(junit_path),
            detail=f"exceeded {timeout_s}s — children terminated",
        )
    finally:
        junit_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="legacy-python-verification")
    parser.add_argument(
        "--scope",
        action="append",
        required=True,
        help="test file path or path::nodeid (repeatable, bounded)",
    )
    parser.add_argument("--timeout-s", type=int, default=_DEFAULT_TIMEOUT_S)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=_DEFAULT_EVIDENCE_DIR,
    )
    args = parser.parse_args(argv)
    try:
        result = run(
            list(args.scope),
            timeout_s=args.timeout_s,
            workers=args.workers,
            evidence_dir=args.evidence_dir,
        )
    except RuntimeError as exc:
        print(f"{_ADAPTER_ID}: ERROR {exc}")
        return 2
    print(
        f"{_ADAPTER_ID}: verdict={result['verdict']} "
        f"cases={result['totals']['cases']} "
        f"pass={result['totals']['pass']} "
        f"fail={result['totals']['fail']} "
        f"blocked={result['totals']['blocked']} "
        f"evidence={result['evidence_path']}"
    )
    if result["verdict"] == "PASS":
        return 0
    return 1 if result["verdict"] in ("FAIL", "INCOMPLETE", "TIMEOUT") else 2


if __name__ == "__main__":
    raise SystemExit(main())
