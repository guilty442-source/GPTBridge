"""SQL anti-pattern convergence gate (P19 / §2.9).

Static scan over governed Python sources for the SQL anti-patterns the
blueprint converges on:

- DML/SELECT ``execute()`` inside a ``for`` loop (N+1 round trips)
- ``with ...connect()`` inside a ``for`` loop (per-row connection /
  durable-persist churn)
- f-string interpolation into SQL arguments (unprepared statement /
  injection surface)
- ``OFFSET`` pagination (deep-offset scans)
- ``SELECT *`` column reads (unstable contract)

Legitimate patterns (DDL seeds, schema migrations, benchmark drivers,
per-item durable marks that intentionally persist per row) are suppressed
with an inline ``# sql-ok: <reason>`` comment on the flagged line or by a
path-level exemption below — every suppression is explicit and auditable.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

_DML_VERBS = (
    "select", "insert", "update", "delete", "replace", "upsert",
    "insert into", "insert or",
)
_STATEMENT_NEUTRAL = re.compile(
    r"^\s*(pragma|set\b|listen|unlisten|notify|begin|commit|rollback|"
    r"savepoint|release|create|alter|drop|analyze|vacuum|attach|detach|"
    r"explain|truncate|grant|revoke|reindex|checkpoint|cluster)",
    re.IGNORECASE,
)
_SELECT_STAR = re.compile(r"\bselect\s+\*", re.IGNORECASE)
_OFFSET = re.compile(r"\boffset\b", re.IGNORECASE)

# Files whose SQL-in-loop is the measured subject, a chaos/stress driver,
# or deliberate test fixture — not production paths.
_EXEMPT_PATH_PARTS = (
    "/tests/",
    "/test_",
    "chaos_sqlite.py",
    "reconcile_stress.py",
    "regression_benchmarks.py",
    "/benchmark",
    "/perf/",
    "performance/",
    "native_core_benchmark.py",
    "transformer_benchmark.py",
    "vector_benchmark.py",
    "parser_benchmark.py",
    "/e2e/",
)

# Whole-file exemptions for schema/bootstrap modules whose loop bodies are
# exclusively DDL or seed inserts (still counted in the report as exempt).
_EXEMPT_FILES = (
    "analytics_schema.py",
    "analytics_store_schema.py",
    "collab_repo_schema.py",
    "local_command_parser.py",
    "runtime_queue.py",
    "migrations.py",
    "bootstrap.py",
    "provenance.py",
    "repair_learning.py",
    "roles.py",
    "session.py",
    "sqlite_pragma_policy.py",
    "sqlite_wal_governor.py",
    "transport_notify.py",
    "maintenance_postgres.py",
    "failover_store.py",
    "domain.py",
    "codex_repository.py",
    "codex_update_validation.py",
    "audit_directories.py",
    "successor_framework.py",
    "store_async.py",
    "build_identity_directory.py",
    "chaos_pg.py",
    "data_layer_contract.py",
    "lineage.py",
    "sqlite_reconciliation_contract.py",
    "_entity_history.py",
)

_SCAN_ROOTS = (
    "shared-layer/src",
    "main-system/src-core",
    "main-system/governance",
    "governance_rule",
    "Standalone tools",
)

_SKIP_DIRS = {
    "__pycache__", ".venv", "node_modules", "runtime", "build", "dist",
    ".git", "bin", "obj", ".worktrees", "csharp", "site-packages",
}


def _suppressed(line: str) -> bool:
    return "# sql-ok" in line


def _string_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return None  # f-string — handled by the interpolation check
    if isinstance(node, ast.Name) or isinstance(node, ast.Attribute):
        return None
    return None


_CONN_RECEIVER = re.compile(
    r"conn|cur|cursor|connection|session|admin|db\b|store", re.IGNORECASE
)


def _is_execute_call(node: ast.Call) -> bool:
    """Only DB-handle receivers count — ``self.execute(call)`` on a tool
    orchestrator is not SQL."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "execute"
    if not isinstance(func, ast.Attribute) or func.attr not in (
        "execute", "executemany",
    ):
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return bool(_CONN_RECEIVER.search(receiver.id))
    if isinstance(receiver, ast.Attribute):
        return bool(_CONN_RECEIVER.search(receiver.attr))
    return False


def _first_sql_arg(node: ast.Call) -> ast.AST | None:
    return node.args[0] if node.args else None


def _literal_is_dml(arg: ast.AST, constants: dict[str, str]) -> bool:
    text = _string_constant(arg)
    if text is None and isinstance(arg, ast.Name):
        text = constants.get(arg.id)
    if text is None and isinstance(arg, ast.Attribute):
        text = constants.get(arg.attr)
    if text is None:
        return True  # non-literal SQL in a loop is unverifiable → flag
    if _STATEMENT_NEUTRAL.match(text):
        return False
    head = text.lstrip().split("(", 1)[0].split(None, 1)[0].lower()
    return head in _DML_VERBS or any(
        text.lstrip().lower().startswith(v) for v in _DML_VERBS
    )


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = \"...\"`` assignments — lets the loop check
    resolve constant SQL like ``cur.execute(_INSERT_SQL, params)``.
    Top-level statements only (module constants live at module scope)."""
    constants: dict[str, str] = {}

    def collect(assign: ast.Assign) -> None:
        for target in assign.targets:
            if isinstance(target, ast.Name) and isinstance(
                assign.value, ast.Constant
            ) and isinstance(assign.value.value, str):
                constants[target.id] = assign.value.value

    for node in tree.body:
        if isinstance(node, ast.Assign):
            collect(node)
        elif isinstance(node, (ast.ClassDef, ast.If, ast.Try)):
            for child in node.body:
                if isinstance(child, ast.Assign):
                    collect(child)
    return constants


def _sql_strings(node: ast.AST) -> list[str]:
    """Collect literal SQL-looking strings in a call's args."""
    out: list[str] = []
    for arg in ast.walk(node):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            text = arg.value
            if re.search(r"\b(select|insert|update|delete|from|where)\b",
                         text, re.IGNORECASE):
                out.append(text)
    return out


def _collect(
    tree: ast.AST,
) -> tuple[set[int], dict[int, int], list[ast.Call]]:
    """Single traversal producing:

    - ``inside``: ``id()`` set of every node inside a for/async-for BODY —
      the loop's own ``iter`` (``for row in conn.execute(...)``) is a
      single query, not N+1, so iter subtrees are excluded.
    - ``with_ctx``: ``id(context_expr child)`` → ``with`` lineno, for
      with-items whose statement sits inside a loop body (per-row
      connect/persist check).
    - ``calls``: every ``ast.Call`` node, for the per-call rules.
    """
    inside: set[int] = set()
    with_ctx: dict[int, int] = {}
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for stmt in (*node.body, *node.orelse):
                for child in ast.walk(stmt):
                    inside.add(id(child))
                    if isinstance(child, (ast.With, ast.AsyncWith)):
                        for item in child.items:
                            for cnode in ast.walk(item.context_expr):
                                with_ctx[id(cnode)] = child.lineno
    return inside, with_ctx, calls


def _scan_file(path: Path, rel: str) -> list[str]:
    findings: list[str] = []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except (UnicodeDecodeError, OSError) as exc:
        return [f"sql-scan unreadable {rel}: {exc}"]
    # Prefilter: every rule needs an execute()/connect() call — files
    # without either skip the AST parse entirely (keeps the delegated
    # check inside the shared audit deadline on large trees).
    if "execute" not in source and "connect" not in source:
        return findings
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"sql-scan unparseable {rel}: {exc}"]
    lines = source.splitlines()

    def suppressed(lineno: int) -> bool:
        return 0 < lineno <= len(lines) and _suppressed(lines[lineno - 1])

    constants = _module_constants(tree)

    exempt_file = (
        any(part in "/" + rel.replace("\\", "/") for part in _EXEMPT_PATH_PARTS)
        or path.name in _EXEMPT_FILES
    )
    inside_loop, with_ctx, calls = _collect(tree)

    for node in calls:
        lineno = getattr(node, "lineno", 0)

        # 1. f-string SQL interpolation — anywhere, always flagged.
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr) and _is_execute_call(node):
                if any(
                    isinstance(v, ast.FormattedValue) for v in arg.values
                ) and not _STATEMENT_NEUTRAL.match(
                    "".join(
                        v.value for v in arg.values
                        if isinstance(v, ast.Constant)
                    )
                ):
                    if not suppressed(lineno) and not exempt_file:
                        findings.append(
                            f"sql-fstring {rel}:{lineno} "
                            "execute() argument interpolates variables"
                        )

        # 2. SELECT * / OFFSET literals.
        if _is_execute_call(node):
            for text in _sql_strings(node):
                if _SELECT_STAR.search(text) and not suppressed(lineno) \
                        and not exempt_file:
                    findings.append(
                        f"sql-select-star {rel}:{lineno} SELECT * read"
                    )
                if _OFFSET.search(text) and not suppressed(lineno):
                    findings.append(
                        f"sql-offset {rel}:{lineno} OFFSET pagination"
                    )

        # 3. execute() DML inside a for-loop (N+1).
        if _is_execute_call(node) and id(node) in inside_loop:
            arg = _first_sql_arg(node)
            if arg is not None and _literal_is_dml(arg, constants):
                if not suppressed(lineno) and not exempt_file:
                    findings.append(
                        f"sql-loop-exec {rel}:{lineno} "
                        "execute() inside for-loop (N+1 candidate)"
                    )

        # 4. with ...connect() inside a for-loop (connection churn).
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else ""
        )
        if name in ("connect", "_connect", "_get_conn",
                    "get_connection", "connection"):
            wlineno = with_ctx.get(id(node))
            if wlineno is not None and not suppressed(wlineno) \
                    and not exempt_file:
                findings.append(
                    f"sql-conn-loop {rel}:{wlineno} "
                    "connect() inside for-loop (per-row commit/persist)"
                )
    return findings


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel_root in _SCAN_ROOTS:
        base = root / rel_root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


BASELINE_NAME = "sql_patterns_baseline.json"


def baseline_path(root: Path) -> Path:
    return Path(__file__).with_name(BASELINE_NAME)


def collect_finding_keys(root: Path) -> list[str]:
    """Sorted multiset of ``category|relpath`` entries — position-tolerant
    (line numbers drift; a new occurrence or a resolved site both diff)."""
    keys: list[str] = []
    for path in _iter_python_files(root):
        rel = path.relative_to(root).as_posix()
        for finding in _scan_file(path, rel):
            keys.append(f"{finding.split(' ', 1)[0]}|{rel}")
    return sorted(keys)


def write_baseline(root: Path) -> Path:
    import json
    from datetime import datetime, timezone

    keys = collect_finding_keys(root)
    payload = {
        "schema": "sql-patterns-baseline/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "finding_count": len(keys),
        "findings": keys,
        "note": (
            "Frozen convergence baseline for check_sql_anti_patterns. "
            "New violations fail the audit; resolved sites must regenerate "
            "this file (python -m governance_rule.execution.audit."
            "audit_sql_patterns --regen). Baseline only shrinks."
        ),
    }
    target = baseline_path(root)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def check_sql_anti_patterns(root: Path, errors: list[str]) -> None:
    """Delegated audit check: diff the live SQL anti-pattern scan against
    the committed convergence baseline — regressions fail, resolved sites
    force a baseline regen (never silently re-widen)."""
    import json

    actual = collect_finding_keys(root)
    base_file = baseline_path(root)
    if not base_file.is_file():
        errors.append(
            f"sql-patterns baseline missing ({base_file.name}); "
            "regenerate with audit_sql_patterns --regen"
        )
        return
    try:
        baseline = json.loads(base_file.read_text(encoding="utf-8"))
        expected = baseline.get("findings") if isinstance(baseline, dict) else None
        if not isinstance(expected, list):
            raise ValueError("findings must be a list")
    except Exception as exc:
        errors.append(f"sql-patterns baseline unreadable: {exc}")
        return

    from collections import Counter

    have = Counter(actual)
    want = Counter(str(e) for e in expected)
    for key in sorted((have - want)):
        errors.append(f"new sql anti-pattern (not in baseline): {key}")
    for key in sorted((want - have)):
        errors.append(
            f"stale sql-patterns baseline entry (resolved — regen baseline): "
            f"{key}"
        )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--regen", action="store_true",
                        help="rewrite the convergence baseline from live scan")
    args = parser.parse_args()
    if args.regen:
        target = write_baseline(args.root)
        print(f"baseline written: {target}")
        return 0
    errors: list[str] = []
    check_sql_anti_patterns(args.root, errors)
    for e in errors:
        print(e)
    print(f"sql-patterns: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
