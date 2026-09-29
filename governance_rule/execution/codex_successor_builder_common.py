"""Successor builder shared vocabulary: constants, error/result types, file utilities (G69)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from governance_rule.execution.codex_postgresql_stage import open_codex_store
from governance_rule.execution.codex_update_validation import (
    foreign_key_violations,
)


CANDIDATE_MANIFEST_SCHEMA: Final[str] = "gptbridge-codex-candidate-manifest/v1"
FORMAL_RULE_REGISTRY: Final[str] = "formal_rule_registry"
SUCCESSOR_SENTINELS: Final[frozenset[str]] = frozenset(
    {
        "successor",
        "<successor>",
        "<successor-version>",
        "successor_version",
        "next-authoritative-utc-second",
    }
)


class SuccessorBuildError(RuntimeError):
    """Fail-closed candidate construction denial."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SuccessorBuildResult:
    ok: bool
    request_id: str
    output_database: str
    manifest_path: str
    candidate_sha256: str = ""
    applied: tuple[Mapping[str, Any], ...] = ()
    deferred: tuple[Mapping[str, Any], ...] = ()
    errors: tuple[str, ...] = ()
    seal_preview: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "output_database": self.output_database,
            "manifest_path": self.manifest_path,
            "candidate_sha256": self.candidate_sha256,
            "applied": [dict(item) for item in self.applied],
            "deferred": [dict(item) for item in self.deferred],
            "errors": list(self.errors),
            "seal_preview": dict(self.seal_preview or {}),
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_database(source: Path, output: Path) -> None:
    """Copy the governed ``.sql`` artifact — the serializable form of a
    staged generation (mutations replay through ``open_artifact``)."""
    shutil.copy2(source, output)
    # The canonical artifact is published read-only; the candidate must be
    # writable so the write-back materialization can dump over it.
    output.chmod(0o666)


def _source_foreign_key_violations(
    source: Path,
) -> tuple[tuple[str, ...], ...]:
    with open_codex_store(source) as connection:
        return foreign_key_violations(connection)
