"""Command Contract Resolver — A224.

Loads and validates command_code_directory contracts for the gateway.

The authoritative directory is the governance codex
(``postgresql://local/gptbridge_codex`` —
``command_code_directory(command_code)``).  A legacy JSON directory
(``<project_root>/command_code_directory/*.json``) is still honoured for
compatibility.  Lookups re-check the sources so a codex amendment becomes
visible without rebuilding the resolver (fail closed when unreadable).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Final

# A224 contract gate — fail closed when the directory is unreadable
_CONTRACT_DIR_NAME: Final[str] = "command_code_directory"
_CODEX_DB_RELATIVE: Final[tuple[str, ...]] = (
    "governance_rule",
    "codex",
    "data",
    "governance_codex.sqlite3",
)


def _normalize_command_code(value: str) -> str:
    """Normalize a registered command code to the lookup form.

    The codex stores ``ALPHA_ONE``; callers ask for ``alpha-one`` or the
    namespaced wire form ``alpha:one-two`` — ``_`` and ``:`` are both
    treated as separators.
    """
    return str(value).strip().lower().replace("_", "-").replace(":", "-")


class CommandContractResolver:
    """Resolves and validates command contracts from the codex directory."""

    def __init__(self, project_root: Path | str | None = None) -> None:
        self._project_root = Path(project_root).resolve() if project_root else None
        self._contracts: dict[str, Any] = {}
        self._source_signature: tuple[Any, ...] = ()
        self._load_error: str | None = None
        if self._project_root:
            self._refresh(force=True)

    # ------------------------------------------------------------------
    # Loading / refresh
    # ------------------------------------------------------------------

    def _signature(self) -> tuple[Any, ...]:
        """Cheap change signature for both sources (mtime + size)."""
        if not self._project_root:
            return ()
        parts: list[Any] = []
        for path in (
            self._project_root.joinpath(*_CODEX_DB_RELATIVE),
            self._project_root / _CONTRACT_DIR_NAME,
        ):
            try:
                if path.is_file():
                    stat = path.stat()
                    parts.append((str(path), stat.st_mtime_ns, stat.st_size))
                elif path.is_dir():
                    latest = max(
                        (entry.stat().st_mtime_ns for entry in path.glob("*.json")),
                        default=0,
                    )
                    count = len(list(path.glob("*.json")))
                    parts.append((str(path), latest, count))
                else:
                    parts.append((str(path), 0, 0))
            except OSError:
                parts.append((str(path), -1, -1))
        return tuple(parts)

    def _refresh(self, *, force: bool = False) -> None:
        if not self._project_root:
            self._load_error = "no project root"
            return
        signature = self._signature()
        if not force and signature == self._source_signature:
            return
        self._source_signature = signature
        contracts: dict[str, Any] = {}
        errors: list[str] = []

        codex_db = self._project_root.joinpath(*_CODEX_DB_RELATIVE)
        if codex_db.is_file():
            try:
                with sqlite3.connect(
                    f"file:{codex_db.as_posix()}?mode=ro", uri=True
                ) as connection:
                    rows = connection.execute(
                        "SELECT command_code FROM command_code_directory"
                    ).fetchall()
                for (raw_code,) in rows:
                    code = _normalize_command_code(str(raw_code or ""))
                    if code:
                        contracts[code] = {"command_code": code}
            except sqlite3.Error as error:
                errors.append(f"codex contract directory unreadable: {error}")

        contract_dir = self._project_root / _CONTRACT_DIR_NAME
        if contract_dir.is_dir():
            for path in contract_dir.glob("*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        contracts[_normalize_command_code(path.stem)] = json.load(handle)
                except (OSError, ValueError) as error:
                    errors.append(f"failed to load {path}: {error}")

        self._contracts = contracts
        self._load_error = "; ".join(errors) if errors else None

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def load_error(self) -> str | None:
        return self._load_error

    def is_registered(self, command: str) -> bool:
        self._refresh()
        return _normalize_command_code(command) in self._contracts

    def get_contract(self, command: str) -> dict[str, Any] | None:
        self._refresh()
        return self._contracts.get(_normalize_command_code(command))


__all__ = ["CommandContractResolver"]
