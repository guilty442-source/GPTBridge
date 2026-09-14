"""Deterministic runtime-contract checks for the governance audit (A69/A185).

These catch defects that previously surfaced only at runtime:

* sovereign base ordering (mixin implementations must be reachable),
* the official codex entry requiring a single-use session nonce (A174),
* no bare multi-argument ``verified_basis`` calls (the module helper takes
  one iterable; ``self.verified_basis(*refs)`` varargs stay legal).
"""

from __future__ import annotations

import ast
from pathlib import Path


def _sovereign_sources(root: Path) -> list[Path]:
    return sorted((root / "main-system" / "governance" / "sovereigns").rglob("*.py"))


def _check_sovereign_mro(root: Path, errors: list[str]) -> None:
    for source in _sovereign_sources(root):
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [
                base.id
                for base in node.bases
                if isinstance(base, ast.Name)
            ]
            if "SovereignBase" not in bases:
                continue
            first_mixin = next(
                (index for index, name in enumerate(bases) if name.endswith("Mixin")),
                None,
            )
            if first_mixin is not None and bases.index("SovereignBase") < first_mixin:
                errors.append(
                    "sovereign base precedes mixins (mixin methods unreachable): "
                    f"{source.name}:{node.lineno} {node.name}"
                )


def _check_codex_entry_security(root: Path, errors: list[str]) -> None:
    source = root / "governance_rule" / "execution" / "codex_official.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError) as error:
        errors.append(f"official codex entry unreadable: {error}")
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "official_sovereign":
            continue
        names = [argument.arg for argument in node.args.kwonlyargs]
        names.extend(argument.arg for argument in node.args.args)
        if "session_nonce" not in names:
            errors.append(
                "official_sovereign must require a single-use session nonce (A174)"
            )


def _check_verified_basis_arity(root: Path, errors: list[str]) -> None:
    governance = root / "main-system" / "governance"
    for source in sorted(governance.rglob("*.py")):
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "verified_basis"):
                continue
            if len(node.args) > 1:
                errors.append(
                    "verified_basis takes one iterable, not positional refs: "
                    f"{source.name}:{node.lineno}"
                )
            elif len(node.args) == 1 and isinstance(node.args[0], ast.Constant):
                errors.append(
                    "verified_basis takes one iterable, not a bare string: "
                    f"{source.name}:{node.lineno}"
                )


def check_runtime_contracts(root: Path, errors: list[str]) -> None:
    """Run the deterministic runtime-contract checks (A69/A174/A185)."""
    _check_sovereign_mro(root, errors)
    _check_codex_entry_security(root, errors)
    _check_verified_basis_arity(root, errors)


__all__ = ["check_runtime_contracts"]
