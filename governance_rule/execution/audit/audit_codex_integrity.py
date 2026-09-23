"""Codex text-integrity and mirror-quality audit checks.

法典依據:
- A537/A538: automatic Codex updates, the five Chinese mirror parts and the
  architecture artifacts synchronize atomically; the runtime deadlines must
  hold for the published generation.
- A379/A383: the Codex and its Chinese mirror are protected authorities whose
  integrity is verification-gated; a green seal must never hide unreadable law.
- A446: an unrecorded result is never a pass, so the mirror-quality evidence
  row for the current generation is required and independently recomputed.

The 2026-09-17 incident (A537/A538 published with Chinese text replaced by
U+003F while the audit stayed green) is the concrete defect these checks close.
"""

from __future__ import annotations

from pathlib import Path

from governance_rule.execution.chinese_codex_mirror import load_chinese_codex_parts
from governance_rule.execution.codex_update_validation import (
    find_replacement_damage,
    mirror_quality_metrics,
)
from governance_rule.execution.codex_repository import codex_readonly_connection

CODEX_DATABASE_RELATIVE = Path("governance_rule") / "codex" / "data" / "governance_codex.sqlite3"
CODEX_ROOT_RELATIVE = Path("governance_rule") / "codex"


def _codex_connection_for(root: Path):
    """Codex read for one root: a root carrying the legacy sqlite file is a
    fixture/staging tree and is read directly (non-authoritative); every
    other root reads the live PostgreSQL authority (A279)."""
    import sqlite3
    from contextlib import closing

    candidate = root / CODEX_DATABASE_RELATIVE
    if candidate.is_file():
        return closing(
            sqlite3.connect(
                f"file:{candidate.as_posix()}?mode=ro&immutable=1", uri=True
            )
        )
    return codex_readonly_connection()


def check_codex_text_integrity(root: Path, errors: list[str]) -> None:
    """Fail when an active provision carries replacement-character damage."""
    try:
        with _codex_connection_for(root) as connection:
            for finding in find_replacement_damage(connection):
                errors.append(
                    f"codex text replacement damage (replacement-character loss): {finding}"
                )
    except Exception as error:
        errors.append(f"codex text integrity check failed: {error}")


def check_codex_mirror_quality(root: Path, errors: list[str]) -> None:
    """Recompute mirror quality and require a matching PASS evidence row."""
    codex_root = root / CODEX_ROOT_RELATIVE
    try:
        mirror = load_chinese_codex_parts(codex_root)
    except (OSError, ValueError) as error:
        errors.append(f"chinese mirror is invalid: {error}")
        return
    metrics = mirror_quality_metrics(mirror["tables"])
    version = str(mirror["codex_version"])
    if metrics["question_loss_field_count"]:
        errors.append(
            "chinese mirror replacement damage: "
            f"{metrics['question_loss_field_count']} fields lost"
        )
    try:
        with _codex_connection_for(root) as connection:
            errors.extend(_evidence_errors(connection, version, metrics))
    except Exception as error:
        errors.append(f"codex mirror quality check failed: {error}")


def _evidence_errors(
    connection,
    version: str,
    metrics: dict[str, int],
) -> tuple[str, ...]:
    """Require a PASS evidence row whose counts match the recomputation."""
    try:
        row = connection.execute(
            "SELECT part_count, replacement_character_count, "
            "question_loss_field_count, chain_valid, assembled_hash_valid, result "
            "FROM chinese_mirror_quality_evidence WHERE version_identity=?",
            (version,),
        ).fetchone()
    except Exception as error:
        return (f"mirror quality evidence is missing or unreadable: {error}",)
    if row is None:
        return (
            f"mirror quality evidence is missing for the current codex version: {version}",
        )
    part_count, replacement_count, loss_fields, chain_valid, hash_valid, result = row
    errors: list[str] = []
    if str(result) != "PASS" or not int(chain_valid or 0) or not int(hash_valid or 0):
        errors.append(f"mirror quality evidence is not PASS for {version}: {result}")
    if int(part_count or 0) != 5:
        errors.append(f"mirror quality evidence records {part_count} parts instead of 5")
    if int(replacement_count or 0) != metrics["replacement_character_count"]:
        errors.append(
            "mirror quality evidence replacement count is stale: "
            f"{replacement_count} != {metrics['replacement_character_count']}"
        )
    if int(loss_fields or 0) != metrics["question_loss_field_count"]:
        errors.append(
            "mirror quality evidence loss-field count is stale: "
            f"{loss_fields} != {metrics['question_loss_field_count']}"
        )
    return tuple(errors)


__all__ = ["check_codex_mirror_quality", "check_codex_text_integrity"]
