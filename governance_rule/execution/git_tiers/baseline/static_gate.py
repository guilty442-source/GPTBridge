"""Static architecture gate for the frozen Git runtime (codex article 529).

The gate is *additive*: the git-tier plane already carries a measured set of
legacy call sites (direct subprocess Git in support modules, ``confirmed=True``
on pre-migration production paths, the legacy approval environment).  The
frozen rule is "新增量 = 0": the current call-site set is snapshotted into the
golden baseline (``gptbridge_git_baseline_v1.json`` -> ``static_allowlist``)
and any *new* occurrence fails the gate.

Categories (article 529 / 495):

    direct_git        subprocess/Popen/os.system execution of ``git`` outside
                      the sanctioned driver (``git_repository.py``) plus the
                      test-only fixture/benchmark allowlist
    audit_writers     direct writes to governed audit ledgers outside the
                      EvidenceManager surface (``__init__.audit_log`` +
                      ``audit_chain``)
    confirmed_bool    ``confirmed=True`` on the production authorization path
    legacy_env        ``GOVERNANCE_CONFIRM`` / ``GOVERNANCE_AUTHORITY_APPROVAL``
    duplicate_parsers status parsers outside ``porcelain.py``
    duplicate_locks   file-lock primitives outside ``process_lock.py`` /
                      ``audit_chain`` (whose byte-range lock is the audit
                      append lock)
    dependency_direction  git_tiers importing scripts/main-system/shared-layer
                      or UI/main-system importing git_tiers internals

Scanning is AST-based (Python files only) so comments and docstrings never
count as violations.  The scanner is read-only and never imports the scanned
modules.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[4]
GIT_TIERS = PROJECT_ROOT / "governance_rule" / "execution" / "git_tiers"
SCRIPTS = PROJECT_ROOT / "scripts"

#: article 536 direct-git freeze allowlist — A375 allows only the driver and
#: a bootstrap incapable of repository operations.  The fault-injection
#: framework is test-only; snapshot.py / disaster_recovery.py are legacy
#: production call sites and must count as *illegal* for the freeze verdict
#: even though the additive gate snapshots them.
FREEZE_DIRECT_GIT_ALLOWED: tuple[str, ...] = (
    "git_tiers/git_repository.py",
    "git_tiers/fault_injection.py",
    "baseline/",
    "tests/",
    "scripts/git-perf-benchmark.py",
)

#: modules allowed to execute git directly (driver + test-only framework +
#: bootstrap-incapable-of-repository-operations benchmark)
SANCTIONED_DIRECT_GIT: tuple[str, ...] = (
    "git_tiers/git_repository.py",
    "git_tiers/snapshot.py",
    "git_tiers/disaster_recovery.py",
    "git_tiers/fault_injection.py",
    "baseline/",
    "tests/",
    "scripts/git-perf-benchmark.py",
)

#: modules allowed to write governed audit ledgers
SANCTIONED_AUDIT_WRITERS: tuple[str, ...] = (
    "git_tiers/__init__.py",
    "git_tiers/audit_chain.py",
    "git_tiers/audit_records.py",
    "git_tiers/registry_migration_engine.py",
    "baseline/",
    "tests/",
)

#: modules allowed to parse ``git status`` output
SANCTIONED_PARSERS: tuple[str, ...] = (
    "git_tiers/porcelain.py",
    "git_tiers/self_commit.py",
    "git_tiers/snapshot.py",
    "git_tiers/disaster_recovery.py",
    "baseline/",
    "tests/",
)

#: lock primitives allowed outside ``process_lock.py``
SANCTIONED_LOCKS: tuple[str, ...] = (
    "git_tiers/locks.py",
    "git_tiers/process_lock.py",
    "git_tiers/audit_chain.py",
    "baseline/",
    "tests/",
)

_GIT_EXEC_FUNCS = frozenset(
    {"run", "Popen", "check_output", "check_call", "call", "system"}
)
_CONFIRMED_RE = re.compile(r"confirmed\s*=\s*True")
_ENV_RE = re.compile(r"GOVERNANCE_CONFIRM|GOVERNANCE_AUTHORITY_APPROVAL")
_PORCELAIN_RE = re.compile(r"porcelain", re.IGNORECASE)
_LOCK_RE = re.compile(
    r"^\s*class\s+\w*Lock\s*[\(:]|msvcrt\.locking|flock\("
)
_OUT_OF_TIER_IMPORT_RE = re.compile(
    r"^(?:import|from)\s+.*(?:scripts|main_system|main-system|shared_layer)"
)
_TIER_IMPORT_RE = re.compile(
    r"^(?:import|from)\s+governance_rule\.execution\.git_tiers"
)


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _is_sanctioned(rel: str, allowlist: Iterable[str]) -> bool:
    """True when the relative path is allowlisted.

    A trailing ``/`` entry matches any path containing ``/<name>/`` (used
    for the nested test directories); a plain entry matches the exact path
    or a path ending with ``/<entry>``.
    """
    rel = rel.replace("\\", "/")
    for entry in allowlist:
        entry = entry.replace("\\", "/")
        if entry.endswith("/"):
            marker = "/" + entry.rstrip("/") + "/"
            if marker in "/" + rel.lstrip("/") or rel.startswith(entry):
                return True
        elif rel == entry or rel.endswith("/" + entry):
            return True
    return False


def _py_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _is_tests(rel: str) -> bool:
    return "/tests/" in f"/{rel}" or rel.startswith("tests/")


def _first_arg_is_git(node: ast.Call) -> bool:
    if not node.args:
        return False
    first = node.args[0]

    def literal(value: ast.AST) -> bool:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value.strip().lower() == "git"
        if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
            head = value.elts[0]
            return (
                isinstance(head, ast.Constant) and head.value == "git"
            )
        return False

    return literal(first)


def scan() -> dict[str, Any]:
    """Return the measured violation sets per category."""
    direct: list[str] = []
    illegal_direct: list[str] = []
    confirmed: list[str] = []
    env: list[str] = []
    parsers: list[str] = []
    locks: list[str] = []
    out_imports: list[str] = []
    audit_writers: list[str] = []
    tier_imports_from_outside: list[str] = []

    for path in _py_files(GIT_TIERS, *sorted(SCRIPTS.glob("git-*.py"))):
        rel = _rel(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        additive_ok = _is_sanctioned(rel, SANCTIONED_DIRECT_GIT)
        freeze_ok = _is_sanctioned(rel, FREEZE_DIRECT_GIT_ALLOWED)
        if not additive_ok or not freeze_ok:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = (
                        func.attr
                        if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else ""
                    )
                    if name in _GIT_EXEC_FUNCS and _first_arg_is_git(node):
                        site = f"{rel}:{node.lineno}:{name}"
                        if not additive_ok:
                            direct.append(site)
                        if not freeze_ok:
                            illegal_direct.append(site)
        text = path.read_text(encoding="utf-8", errors="replace")
        line_scans_ok = not (_is_tests(rel) or "/baseline/" in f"/{rel}")
        if line_scans_ok:
            for i, line in enumerate(text.splitlines(), 1):
                if _CONFIRMED_RE.search(line):
                    confirmed.append(f"{rel}:{i}")
                if _ENV_RE.search(line):
                    env.append(f"{rel}:{i}")
        if not _is_sanctioned(rel, SANCTIONED_PARSERS):
            for i, line in enumerate(text.splitlines(), 1):
                if _PORCELAIN_RE.search(line) and re.search(
                    r"def\s+|\.split|parse", line
                ):
                    parsers.append(f"{rel}:{i}")
        if not _is_sanctioned(rel, SANCTIONED_LOCKS):
            for i, line in enumerate(text.splitlines(), 1):
                if _LOCK_RE.search(line):
                    locks.append(f"{rel}:{i}:{line.strip()[:90]}")
        if not _is_sanctioned(rel, SANCTIONED_AUDIT_WRITERS) and line_scans_ok:
            for i, line in enumerate(text.splitlines(), 1):
                if re.search(
                    r"git_tier_audit|merge_queue\.jsonl|AUDIT_LEDGER_PATH", line
                ):
                    audit_writers.append(f"{rel}:{i}")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if _OUT_OF_TIER_IMPORT_RE.match(stripped) and rel.startswith(
                "governance_rule/execution/git_tiers/"
            ):
                out_imports.append(f"{rel}:{i}:{stripped[:110]}")

    # reverse direction: UI / main-system importing git_tiers internals
    for base in (
        PROJECT_ROOT / "main-system" / "src-ui",
        PROJECT_ROOT / "main-system" / "src-core",
    ):
        for path in _py_files(base):
            rel = _rel(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if _TIER_IMPORT_RE.match(line.strip()):
                    tier_imports_from_outside.append(f"{rel}:{i}")

    categories = {
        "direct_git": sorted(direct),
        "illegal_direct_git": sorted(illegal_direct),
        "audit_writers": sorted(audit_writers),
        "confirmed_bool": sorted(confirmed),
        "legacy_env": sorted(env),
        "duplicate_parsers": sorted(parsers),
        "duplicate_locks": sorted(locks),
        "dependency_direction": sorted(out_imports + tier_imports_from_outside),
    }
    return {
        "categories": categories,
        "counts": {name: len(items) for name, items in categories.items()},
        "scanned_roots": ["governance_rule/execution/git_tiers", "scripts/git-*.py",
                          "main-system/src-ui", "main-system/src-core"],
    }


def scan_direct_git_all() -> dict[str, list[str]]:
    """All direct-git call sites per file (sanctioned modules included).

    Used for the complexity budget (how many modules still execute git);
    test-only files are excluded so the number reflects production code.
    """
    per_file: dict[str, list[str]] = {}
    for path in _py_files(GIT_TIERS, *sorted(SCRIPTS.glob("git-*.py"))):
        rel = _rel(path)
        if _is_tests(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else ""
                )
                if name in _GIT_EXEC_FUNCS and _first_arg_is_git(node):
                    hits.append(f"{rel}:{node.lineno}:{name}")
        if hits:
            per_file[rel] = hits
    return per_file


def compare_with_allowlist(
    scan_result: dict[str, Any], allowlist: dict[str, list[str]]
) -> dict[str, Any]:
    """Return per-category new (not allowlisted) occurrences."""
    new: dict[str, list[str]] = {}
    baseline_counts: dict[str, int] = {}
    for category, items in scan_result["categories"].items():
        frozen = set(allowlist.get(category, []))
        baseline_counts[category] = len(frozen)
        added = [item for item in items if item not in frozen]
        if added:
            new[category] = added
    return {
        "new_violations": new,
        "new_total": sum(len(v) for v in new.values()),
        "baseline_counts": baseline_counts,
        "current_counts": scan_result["counts"],
        "verdict": "PASS" if not new else "FAIL",
    }


__all__ = [
    "GIT_TIERS",
    "PROJECT_ROOT",
    "SANCTIONED_AUDIT_WRITERS",
    "SANCTIONED_DIRECT_GIT",
    "SANCTIONED_LOCKS",
    "SANCTIONED_PARSERS",
    "compare_with_allowlist",
    "scan",
]
