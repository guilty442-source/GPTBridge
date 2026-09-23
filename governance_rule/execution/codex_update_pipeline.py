"""Automatic Codex update pipeline — isolate, change, wire, release, refresh.

Governor-directed order (2026-09-17): 先隔離 → 後執行變更 → 再執行接線 →
再解除隔離 → 前端連線刷新.

法典依據 (A382/A488 non-disruptive amendment flow; A537/A538 automatic
synchronization of updates, the five Chinese mirror parts and architecture
artifacts; A383 isolation; A446 bounded stages):
- ISOLATE copies the current generation into an isolated staging root and
  marks it non-authoritative; the live generation is never partially mutated.
- EXECUTE-CHANGE applies the prepared successor database inside the isolation,
  normalizes the version identity, validates the staged generation and records
  the mirror-quality evidence row in that same generation.
- WIRE atomically replaces the published database and mirror parts, restores
  read-only protection and produces the typed notification payload.
- RELEASE-ISOLATION verifies the published generation and releases the fence.
- FRONTEND-REFRESH emits the frontend connection refresh request so UI
  projections reconnect to the published authority generation.

The module never runs implicitly.  ``apply=False`` (default) stops after the
change was validated in isolation so the automation can be rehearsed; only the
governed executor runs ``apply=True`` and it writes only inside the explicit
codex root.  A rejected staging copy is deleted; a released one is retained
as the rollback snapshot with its evidence record.

Boundary: seal-root recomputation and revision/lineage/certification rows
stay owned by the amendment pipeline (A487/A537/A438); this module wires a
prepared successor, records the mirror-quality evidence row (so the
published generation passes the codex-integrity audit) and restores
read-only protection.  The flow uses no external signatures — the
unanimous five-sovereign audit certificate closes the seal.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from governance_rule.execution.chinese_codex_mirror import PART_NAMES
from governance_rule.execution.codex_postgresql import (
    authority_state,
    export_postgresql_codex,
    import_sqlite_predecessor,
    verify_sqlite_parity,
)
from governance_rule.execution.codex_mirror_writer import (
    MirrorRenderError,
    mirror_errors,
    record_mirror_quality_evidence,
    render_mirror_parts,
)
from governance_rule.execution.codex_update_validation import (
    foreign_key_violations,
    staged_generation_errors,
)

UPDATE_FLOW_IDENTITY: Final[str] = "A382/A488/A537/A538-isolate-change-wire-release-refresh"
UPDATE_PHASES: Final[tuple[str, ...]] = (
    "isolate",
    "execute-change",
    "wire",
    "release-isolation",
    "frontend-refresh",
)
DATABASE_NAME: Final[str] = "governance_codex.sqlite3"
ISOLATION_MARKER: Final[str] = "STAGE_ISOLATION.json"
RELEASED_MARKER: Final[str] = "STAGE_RELEASED.json"
REFRESH_REQUEST: Final[str] = "FRONTEND_REFRESH.json"
FRONTEND_REFRESH_CHANNELS: Final[tuple[str, ...]] = (
    "frontend",
    "backend",
    "ui-projection",
)


class CodexUpdateError(RuntimeError):
    """Fail-closed denial or failure inside one update phase."""

    def __init__(self, phase: str, reason: str) -> None:
        super().__init__(f"{phase}: {reason}")
        self.phase = phase
        self.reason = reason


@dataclass(frozen=True)
class PhaseRecord:
    phase: str
    ok: bool
    detail: str
    evidence: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class IsolatedStage:
    fence_id: str
    codex_root: Path
    staging_root: Path
    database: Path
    parts: tuple[Path, ...]
    source_version: str
    source_digests: Mapping[str, str]
    source_fk_violations: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class AutoUpdateResult:
    ok: bool
    applied: bool
    version: str
    phases: tuple[PhaseRecord, ...]
    refresh_pending: bool = False


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set_read_only(path: Path, read_only: bool = True) -> None:
    """Set or clear the read-only attribute using Windows API.

    On Windows, os.chmod with stat.S_IWRITE is not reliable for the
    FILE_ATTRIBUTE_READONLY flag. Use ctypes to call SetFileAttributesW
    directly for consistent behaviour across platforms.
    """
    try:
        import ctypes
        from ctypes import wintypes

        FILE_ATTRIBUTE_READONLY = 0x00000001
        kernel32 = ctypes.windll.kernel32

        path_str = str(path.resolve())
        attrs = kernel32.GetFileAttributesW(path_str)
        if attrs == 0xFFFFFFFF:
            raise OSError(f"GetFileAttributesW failed for {path_str}")

        if read_only:
            new_attrs = attrs | FILE_ATTRIBUTE_READONLY
        else:
            new_attrs = attrs & ~FILE_ATTRIBUTE_READONLY

        if not kernel32.SetFileAttributesW(path_str, new_attrs):
            raise OSError(f"SetFileAttributesW failed for {path_str}")
    except (ImportError, AttributeError, OSError):
        # Fallback to os.chmod for non-Windows or if ctypes fails
        mode = path.stat().st_mode
        os.chmod(path, mode & ~stat.S_IWRITE if read_only else mode | stat.S_IWRITE)


def _atomic_replace(source: Path, target: Path) -> None:
    """temp -> fsync -> replace -> read-only, never leaving a half file."""
    temporary = target.with_name(target.name + ".staging-tmp")
    with temporary.open("wb") as handle:
        handle.write(source.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    if target.exists():
        _set_read_only(target, False)
    # Windows readers may hold the target without delete sharing for a
    # short window (indexers, watchers); retry briefly before failing.
    last_error: OSError | None = None
    for _ in range(10):
        try:
            os.replace(temporary, target)
            last_error = None
            break
        except PermissionError as error:
            last_error = error
            time.sleep(0.5)
    if last_error is not None:
        try:
            temporary.unlink()
        except OSError:
            pass
        if target.exists():
            _set_read_only(target, True)
        raise last_error
    _set_read_only(target, True)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_version(database: Path) -> str:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='codex_version'"
        ).fetchone()
        return str(row[0]).strip() if row else ""
    finally:
        connection.close()


def _source_foreign_key_violations(database: Path) -> tuple[tuple[str, ...], ...]:
    """Baseline: the live generation's acknowledged legacy violations."""
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return foreign_key_violations(connection)
    finally:
        connection.close()


def isolate_generation(codex_root: str | Path, staging_root: str | Path) -> IsolatedStage:
    """Phase 1: copy the live generation into a non-authoritative isolation."""
    root = Path(codex_root).resolve()
    staging = Path(staging_root).resolve()
    if not root.is_dir():
        raise CodexUpdateError("isolate", f"codex root not found: {root}")
    if staging == root or staging.is_relative_to(root):
        raise CodexUpdateError("isolate", "staging root must be outside the codex root")
    database = root / "data" / DATABASE_NAME

    staging.mkdir(parents=True, exist_ok=True)
    isolated_database = staging / DATABASE_NAME
    if database.is_file():
        shutil.copy2(database, isolated_database)
    elif _same_path(root, _canonical_codex_root()):
        # Canonical root post-cutover (A173): the live authority is the
        # PostgreSQL schema, so the staged working copy is a
        # non-authoritative export of it.
        export_postgresql_codex(isolated_database)
    else:
        raise CodexUpdateError("isolate", f"codex database not found: {database}")
    _set_read_only(isolated_database, False)

    parts: list[Path] = []
    for name in PART_NAMES:
        source = root / name
        if not source.is_file():
            raise CodexUpdateError("isolate", f"mirror part not found: {name}")
        target = staging / name
        shutil.copy2(source, target)
        _set_read_only(target, False)
        parts.append(target)

    digests = {DATABASE_NAME: _digest(isolated_database)}
    digests.update({name: _digest(root / name) for name in PART_NAMES})
    fence_id = str(uuid.uuid4())
    stage = IsolatedStage(
        fence_id=fence_id,
        codex_root=root,
        staging_root=staging,
        database=isolated_database,
        parts=tuple(parts),
        source_version=_read_version(isolated_database),
        source_digests=digests,
        source_fk_violations=_source_foreign_key_violations(isolated_database),
    )
    _write_json(
        staging / ISOLATION_MARKER,
        {
            "fence_id": fence_id,
            "flow": UPDATE_FLOW_IDENTITY,
            "state": "staged-non-authoritative",
            "source_version": stage.source_version,
            "source_digests": digests,
        },
    )
    return stage


def _normalize_version(database: Path, version: str | None) -> None:
    if not version:
        return
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='codex_version'", (version,)
        )
        connection.commit()
    finally:
        connection.close()


def _validate_and_render(stage: IsolatedStage, version: str | None) -> list[str]:
    errors = list(
        staged_generation_errors(
            stage.database.as_posix(),
            version=str(version).strip() if version else None,
            baseline_violations=stage.source_fk_violations,
        )
    )
    if errors:
        return errors
    try:
        render_mirror_parts(
            stage.database, stage.staging_root, template_root=stage.codex_root
        )
        record_mirror_quality_evidence(stage.database, stage.staging_root)
        render_mirror_parts(
            stage.database, stage.staging_root, template_root=stage.codex_root
        )
        errors.extend(mirror_errors(stage.database, stage.staging_root))
    except (MirrorRenderError, OSError, ValueError, sqlite3.Error) as error:
        errors.append(f"staged mirror rendering failed: {error}")
    return errors


def execute_staged_change(
    stage: IsolatedStage,
    *,
    prepared_database: str | Path | None = None,
    version: str | None = None,
) -> PhaseRecord:
    """Phase 2: apply the prepared successor and validate the staged change."""
    if prepared_database is not None:
        prepared = Path(prepared_database).resolve()
        if not prepared.is_file():
            raise CodexUpdateError(
                "execute-change", f"prepared database not found: {prepared}"
            )
        _set_read_only(stage.database, False)
        shutil.copyfile(prepared, stage.database)
    _normalize_version(stage.database, version)
    errors = _validate_and_render(stage, version)
    return PhaseRecord(
        phase="execute-change",
        ok=not errors,
        detail="staged generation validated" if not errors else "staged generation rejected",
        evidence={
            "fence_id": stage.fence_id,
            "version": version or stage.source_version,
            "errors": tuple(errors),
        },
    )


def _canonical_codex_root() -> Path:
    return Path(__file__).resolve().parents[2] / "governance_rule" / "codex"


def _same_path(left: Path, right: Path) -> bool:
    return str(left).casefold() == str(right).casefold()


def wire_generation(stage: IsolatedStage) -> PhaseRecord:
    """Phase 3: atomically publish the staged database and mirror parts."""
    published: dict[str, str] = {}
    for name in PART_NAMES:
        if not (stage.staging_root / name).is_file():
            raise CodexUpdateError("wire", f"staged mirror part missing: {name}")
    # The PostgreSQL authority is the single live codex: it may only be
    # wired from the canonical codex root.  Synthetic roots (tests,
    # rehearsals) must never republish the shared authority.
    canonical = _same_path(stage.codex_root, _canonical_codex_root())
    if canonical:
        postgres_state: Any = import_sqlite_predecessor(stage.database)
        postgres_parity: Any = verify_sqlite_parity(stage.database)
        if postgres_parity["result"] != "PASS":
            raise CodexUpdateError("wire", "PostgreSQL codex parity verification failed")
    else:
        postgres_state = {"skipped": "non-canonical-codex-root"}
        postgres_parity = {"result": "SKIPPED"}
    published_database = stage.codex_root / "data" / DATABASE_NAME
    if canonical and not published_database.is_file():
        # A173 residue deletion: the canonical codex root no longer carries a
        # sqlite file — the PostgreSQL import above IS the publication.
        pass
    else:
        _atomic_replace(stage.database, published_database)
    published[DATABASE_NAME] = _digest(stage.database)
    for name in PART_NAMES:
        staged_part = stage.staging_root / name
        _atomic_replace(staged_part, stage.codex_root / name)
        published[name] = _digest(stage.codex_root / name)
    return PhaseRecord(
        phase="wire",
        ok=True,
        detail="published generation wired atomically",
        evidence={
            "fence_id": stage.fence_id,
            "published": published,
            "postgresql": postgres_state,
            "postgresql_parity": postgres_parity,
        },
    )


def _published_database(stage: IsolatedStage) -> Path:
    """The published generation to verify: the codex-root sqlite when it
    exists, otherwise the staged copy that was imported into PostgreSQL."""
    published = stage.codex_root / "data" / DATABASE_NAME
    if published.is_file():
        return published
    return stage.database


def _published_version(stage: IsolatedStage) -> str:
    if _same_path(stage.codex_root, _canonical_codex_root()):
        try:
            return str(authority_state().get("codex_version") or "")
        except Exception:
            pass
    return _read_version(stage.codex_root / "data" / DATABASE_NAME)


def _published_generation_errors(stage: IsolatedStage) -> tuple[str, ...]:
    codex_root = stage.codex_root
    database = _published_database(stage)
    errors = list(
        staged_generation_errors(
            database.as_posix(), baseline_violations=stage.source_fk_violations
        )
    )
    published_paths: list[Path] = [codex_root / name for name in PART_NAMES]
    if database != stage.database:
        # The codex-root sqlite exists and is a published artifact; the
        # staged scratch copy is exempt from the read-only check.
        published_paths.insert(0, database)
    for path in published_paths:
        if path.stat().st_mode & stat.S_IWRITE:
            errors.append(f"published artifact is writable: {path.name}")
    errors.extend(mirror_errors(database, codex_root, label="published"))
    return tuple(errors)


def release_isolation(stage: IsolatedStage) -> PhaseRecord:
    """Phase 4: verify the published generation and release the fence."""
    errors = _published_generation_errors(stage)
    marker = stage.staging_root / ISOLATION_MARKER
    if marker.exists():
        marker.unlink()
    _write_json(
        stage.staging_root / RELEASED_MARKER,
        {
            "fence_id": stage.fence_id,
            "flow": UPDATE_FLOW_IDENTITY,
            "state": "released",
            "version": _published_version(stage),
            "errors": errors,
        },
    )
    return PhaseRecord(
        phase="release-isolation",
        ok=not errors,
        detail="isolation fence released" if not errors else "release verification failed",
        evidence={"fence_id": stage.fence_id, "errors": errors},
    )


def frontend_refresh(
    stage: IsolatedStage,
    notify: Callable[[Mapping[str, object]], Any] | None = None,
) -> PhaseRecord:
    """Phase 5: emit the frontend connection refresh request."""
    version = _published_version(stage)
    payload: dict[str, object] = {
        "contract": "codex-generation-published",
        "flow": UPDATE_FLOW_IDENTITY,
        "fence_id": stage.fence_id,
        "version": version,
        "channels": list(FRONTEND_REFRESH_CHANNELS),
        "action": "reconnect-and-reload-authority",
    }
    delivered = False
    if notify is not None:
        notify(payload)
        delivered = True
    _write_json(stage.staging_root / REFRESH_REQUEST, payload)
    return PhaseRecord(
        phase="frontend-refresh",
        ok=True,
        detail=(
            "refresh request delivered through the governed hook"
            if delivered
            else "refresh request recorded; no governed notification hook wired"
        ),
        evidence={"version": version, "delivered": delivered},
    )


def _discard_staging(stage: IsolatedStage) -> None:
    shutil.rmtree(stage.staging_root, ignore_errors=True)


def run_auto_update(
    codex_root: str | Path,
    staging_root: str | Path,
    *,
    prepared_database: str | Path | None = None,
    version: str | None = None,
    apply: bool = False,
    notify: Callable[[Mapping[str, object]], Any] | None = None,
) -> AutoUpdateResult:
    """Run the directed update order; stop fail-closed at the first failure."""
    phases: list[PhaseRecord] = []
    try:
        stage = isolate_generation(codex_root, staging_root)
    except CodexUpdateError as error:
        phases.append(PhaseRecord(error.phase, False, error.reason))
        return AutoUpdateResult(False, False, "", tuple(phases))
    phases.append(
        PhaseRecord(
            "isolate",
            True,
            "live generation isolated; staged copy is non-authoritative",
            {"fence_id": stage.fence_id, "source_version": stage.source_version},
        )
    )
    change = execute_staged_change(
        stage, prepared_database=prepared_database, version=version
    )
    phases.append(change)
    if not change.ok:
        _discard_staging(stage)
        return AutoUpdateResult(False, False, stage.source_version, tuple(phases))
    return _finish_update(stage, phases, version, apply, notify)


def _finish_update(
    stage: IsolatedStage,
    phases: list[PhaseRecord],
    version: str | None,
    apply: bool,
    notify: Callable[[Mapping[str, object]], Any] | None,
) -> AutoUpdateResult:
    """Dry-run stop, or wire + release + frontend refresh when applying."""
    target_version = version or stage.source_version
    if not apply:
        _discard_staging(stage)
        return AutoUpdateResult(True, False, target_version, tuple(phases))
    wire = wire_generation(stage)
    phases.append(wire)
    if not wire.ok:
        return AutoUpdateResult(False, True, target_version, tuple(phases))
    release = release_isolation(stage)
    phases.append(release)
    refresh = frontend_refresh(stage, notify)
    phases.append(refresh)
    return AutoUpdateResult(
        ok=wire.ok and release.ok,
        applied=True,
        version=target_version,
        phases=tuple(phases),
        refresh_pending=not bool(refresh.evidence.get("delivered")),
    )


__all__ = [
    "AutoUpdateResult",
    "CodexUpdateError",
    "DATABASE_NAME",
    "FRONTEND_REFRESH_CHANNELS",
    "IsolatedStage",
    "PART_NAMES",
    "PhaseRecord",
    "UPDATE_FLOW_IDENTITY",
    "UPDATE_PHASES",
    "execute_staged_change",
    "frontend_refresh",
    "isolate_generation",
    "release_isolation",
    "run_auto_update",
    "wire_generation",
]
