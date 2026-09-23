"""Dev-test semantic evidence ledger — non-gate, but bound.

pytest / xUnit / TypeScript dev-time tests are never release-gate
evidence (``push_gate`` runs only the native suite).  But governance
semantics — decision, permission, fail-closed — are proven *only* by
these tests, so their results are recorded with the same binding
discipline as gate artifacts:

- ``source_revision``: git HEAD at run time;
- ``contract_hash``: SHA-256 over the executed test files' content —
  the semantic contract actually exercised;
- append-only JSONL ledger next to the capability/audit ledgers.

Non-gate does not mean invalid: a fresh PASS entry is real semantic
evidence for its exact content+revision.  And missing/stale evidence
means "unverified" — never silently "verified".
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

LEDGER_RELATIVE = (
    Path("governance_rule") / "execution" / "audit" / "dev_test_evidence.jsonl"
)
SCHEMA = "dev-test-evidence/v1"
_FAILED_NODES_CAP = 200
_GIT_TIMEOUT_S = 10.0

_ledger_locks: dict[Path, threading.Lock] = {}
_ledger_locks_guard = threading.Lock()


def _ledger_lock(path: Path) -> threading.Lock:
    with _ledger_locks_guard:
        return _ledger_locks.setdefault(path, threading.Lock())


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hashes(root: Path, files: Iterable[Path]) -> dict[str, str]:
    """relpath → sha256(content) for every executed test file."""
    hashes: dict[str, str] = {}
    for path in files:
        try:
            rel = path.resolve().relative_to(root.resolve())
            hashes[rel.as_posix()] = _sha256_bytes(path.read_bytes())
        except (OSError, ValueError):
            continue
    return hashes


def _contract_hash(files: Mapping[str, str]) -> str:
    """One hash binding the whole executed set: sorted relpath:sha256."""
    canon = "\n".join(f"{rel}:{digest}" for rel, digest in sorted(files.items()))
    return _sha256_bytes(canon.encode("utf-8"))


def _source_revision(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "").strip()
    return out or None


def ledger_path(root: Path) -> Path:
    return Path(root) / LEDGER_RELATIVE


def record_test_run(
    root: Path,
    *,
    kind: str,
    files: Mapping[str, str],
    verdict: str,
    totals: Mapping[str, int],
    failed_nodes: Sequence[str] = (),
    duration_s: float = 0.0,
    command: Sequence[str] = (),
    semantic_scope: Sequence[str] = (),
    note: str = "",
    ledger: Path | None = None,
) -> dict[str, Any]:
    """Append one bound test-run entry to the evidence ledger.

    ``files`` is the relpath→sha256 map of the executed test files; the
    entry's ``contract_hash`` derives from it.  ``gate_role`` is pinned
    to ``non-gate`` — dev-test evidence can never masquerade as release
    evidence.
    """
    entry = {
        "schema": SCHEMA,
        "kind": kind,
        "gate_role": "non-gate",
        "source_revision": _source_revision(Path(root)),
        "contract_hash": _contract_hash(files),
        "files": dict(files),
        "totals": dict(totals),
        "failed_nodes": [str(n) for n in failed_nodes][:_FAILED_NODES_CAP],
        "verdict": verdict,
        "duration_s": round(float(duration_s), 3),
        "command": [str(c) for c in command],
        "semantic_scope": [str(s) for s in semantic_scope],
        "note": note,
        "recorded_at_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    path = ledger or ledger_path(Path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    with _ledger_lock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    return entry


def _parse_junit(xml_path: Path) -> tuple[dict[str, int], list[str]]:
    """Extract totals + failed node ids from a pytest junit report."""
    totals = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    failed: list[str] = []
    try:
        tree = ET.parse(xml_path)
    except (OSError, ET.ParseError):
        return totals, ["<junit-xml-unparseable>"]
    for case in tree.getroot().iter("testcase"):
        totals["tests"] += 1
        node = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("failure") is not None:
            totals["failed"] += 1
            failed.append(node)
        elif case.find("error") is not None:
            totals["errors"] += 1
            failed.append(node)
        elif case.find("skipped") is not None:
            totals["skipped"] += 1
        else:
            totals["passed"] += 1
    return totals, failed


def _collect_test_files(root: Path, testpaths: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for rel in testpaths:
        path = Path(root) / rel
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("test_*.py")))
    return files


def record_pytest_run(
    root: Path,
    testpaths: Sequence[str],
    *,
    budget_s: float = 600.0,
    semantic_scope: Sequence[str] = (),
    ledger: Path | None = None,
) -> dict[str, Any]:
    """Run pytest over ``testpaths`` and record the bound result.

    The run itself is bounded; a timeout, spawn failure or missing
    report is still recorded — fail-visible evidence, never silence.
    """
    root = Path(root)
    files = _file_hashes(root, _collect_test_files(root, testpaths))
    import tempfile

    xml_path = Path(
        tempfile.mktemp(prefix="dev-test-junit-", suffix=".xml")
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={xml_path}",
        *testpaths,
    ]
    started = time.monotonic()
    verdict = "error"
    note = ""
    try:
        proc = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=budget_s,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        verdict = "pass" if proc.returncode == 0 else "fail"
        if proc.returncode not in (0, 1):
            note = f"pytest exited {proc.returncode}"
    except subprocess.TimeoutExpired:
        verdict = "timeout"
        note = f"pytest exceeded {budget_s}s"
    except OSError as error:
        note = f"pytest spawn failed: {error}"
    duration = time.monotonic() - started
    totals, failed = _parse_junit(xml_path) if xml_path.is_file() else (
        {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
        [],
    )
    try:
        xml_path.unlink(missing_ok=True)
    except OSError:
        pass
    return record_test_run(
        root,
        kind="pytest",
        files=files,
        verdict=verdict,
        totals=totals,
        failed_nodes=failed,
        duration_s=duration,
        command=command,
        semantic_scope=semantic_scope,
        note=note,
        ledger=ledger,
    )


def semantic_evidence_status(
    root: Path,
    required_files: Sequence[str],
    *,
    ledger: Path | None = None,
) -> dict[str, Any]:
    """Freshness verdict for required semantic test files.

    A file has fresh evidence when some PASS ledger entry carries the
    file's *current* content hash under the *current* HEAD.  Anything
    else is ``stale`` (ran against older content/revision) or ``missing``
    (never evidenced) — the caller must report incomplete verification,
    not presumed validity and not presumed invalidity.
    """
    root = Path(root)
    revision = _source_revision(root)
    current = _file_hashes(root, [root / rel for rel in required_files])
    entries: list[dict[str, Any]] = []
    path = ledger or ledger_path(root)
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                entries.append(rec)
    except OSError:
        entries = []
    fresh: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    for rel, digest in sorted(current.items()):
        proven = any(
            rec.get("verdict") == "pass"
            and rec.get("source_revision") == revision
            and rec.get("files", {}).get(rel) == digest
            for rec in entries
        )
        if proven:
            fresh.append(rel)
        elif any(
            rec.get("files", {}).get(rel) is not None for rec in entries
        ):
            stale.append(rel)
        else:
            missing.append(rel)
    return {
        "schema": "dev-test-evidence-status/v1",
        "source_revision": revision,
        "files_total": len(current),
        "fresh": fresh,
        "stale": stale,
        "missing": missing,
        "complete": not stale and not missing and bool(current),
    }


def _main(argv: list[str]) -> int:
    root = Path.cwd()
    if "--run" in argv:
        idx = argv.index("--run")
        paths = argv[idx + 1 :]
        if not paths:
            print("usage: --run <testpath...>", file=sys.stderr)
            return 2
        entry = record_pytest_run(root, paths)
        print(
            f"dev-test-evidence: {entry['verdict']} "
            f"tests={entry['totals'].get('tests', 0)} "
            f"rev={str(entry['source_revision'])[:12]} "
            f"contract={entry['contract_hash'][:12]}"
        )
        return 0 if entry["verdict"] == "pass" else 1
    if "--status" in argv:
        idx = argv.index("--status")
        paths = argv[idx + 1 :]
        files = _collect_test_files(root, paths)
        rels = [
            p.resolve().relative_to(root.resolve()).as_posix()
            for p in files
        ]
        status = semantic_evidence_status(root, rels)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["complete"] else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
