"""Registry-migration prepare / backup / execute mixin (A185 split).

Extracted from ``registry_migration_engine.py`` (source-size contract):
step application + preparation, verified backup handling, journal
construction and the public ``dry_run`` / ``migrate`` / ``recover`` /
``_execute`` flow (backup -> migrate -> validate -> atomic replace;
failure leaves the old file intact).  Mixed into
``registry_migration_engine.RegistryMigrationEngine``.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

from .registry_migrations import (
    STEPS_BY_TARGET,
    MigrationStep,
    SchemaError,
    canonical_digest,
    deep_copy,
    detect_schema,
)
from .registry_migration_support import (
    AUTOMATION_STATE_SUBDIR,
    TARGET_ORDER,
    Classification,
    MigrationError,
    MigrationReport,
    RecoveryReport,
    StepRecord,
    _PreparedTarget,
    _TARGET_SCHEMA,
    _atomic_write,
    _json_text,
    _now_iso,
)


class MigrationOpsMixin:
    """Preparation / backup / journal / execution surface."""

    # -- preparation ----------------------------------------------------

    def _apply_steps(
        self, target: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], list[StepRecord], int, int]:
        steps: tuple[MigrationStep, ...] = STEPS_BY_TARGET[target]
        target_schema = _TARGET_SCHEMA[target]
        data = deep_copy(payload)
        schema = detect_schema(data, default=steps[0].old_schema)
        if schema > target_schema:
            raise SchemaError(
                f"{target} schema {schema} newer than target {target_schema}"
            )
        records: list[StepRecord] = []
        while schema < target_schema:
            step = next(
                (candidate for candidate in steps if candidate.old_schema == schema),
                None,
            )
            if step is None:
                raise SchemaError(
                    f"no {target} migration step from schema {schema}"
                )
            input_digest = canonical_digest(data)
            data = step.apply(data)
            new_schema = detect_schema(data, default=step.new_schema)
            if new_schema <= schema:
                raise SchemaError(
                    f"{step.migration_id} did not advance schema"
                )
            records.append(
                StepRecord(
                    migration_id=step.migration_id,
                    target=target,
                    old_schema=schema,
                    new_schema=new_schema,
                    input_digest=input_digest,
                    output_digest=canonical_digest(data),
                    timestamp=_now_iso(),
                )
            )
            schema = new_schema
        source_generation = int(payload.get("generation", 0) or 0)
        generation = source_generation + 1
        data["generation"] = generation
        if records:
            records[-1].output_digest = canonical_digest(data)
        return data, records, source_generation, generation

    def _prepare(self, targets: list[str]) -> list[_PreparedTarget]:
        prepared: list[_PreparedTarget] = []
        for target in targets:
            path = self.path_for(target)
            source, _, error = self._read_payload(path)
            if source is None:
                raise MigrationError(f"{target} state is unreadable: {error}")
            migrated, records, source_generation, generation = (
                self._apply_steps(target, source)
            )
            if not records:
                continue
            validation = self._validate(target, migrated)
            if validation:
                raise MigrationError(
                    f"{target} migration failed validation: "
                    + "; ".join(validation)
                )
            invariants = self._invariant(target, source, migrated)
            if invariants:
                raise MigrationError(
                    f"{target} migration violates invariants: "
                    + "; ".join(invariants)
                )
            prepared.append(
                _PreparedTarget(
                    target=target,
                    path=path,
                    source=source,
                    migrated=migrated,
                    records=records,
                    source_schema=detect_schema(
                        source,
                        default=STEPS_BY_TARGET[target][0].old_schema,
                    ),
                    target_schema=_TARGET_SCHEMA[target],
                    source_generation=source_generation,
                    generation=generation,
                )
            )
        return prepared

    # -- backups --------------------------------------------------------

    def _backup_base(self, target: str, generation: int) -> Path:
        if target == "registry":
            return self.state_dir / f"registry.backup.{generation}"
        return self.queue_dir / f"queue.backup.{generation}"

    def _backup_path(self, target: str, generation: int) -> Path:
        base = self._backup_base(target, generation)
        candidate = base
        counter = 1
        while candidate.exists() or candidate.with_name(
            candidate.name + ".meta.json"
        ).exists():
            candidate = base.with_name(f"{base.name}.{counter}")
            counter += 1
        return candidate

    def _write_backup(self, item: _PreparedTarget) -> None:
        backup_path = self._backup_path(item.target, item.source_generation)
        _atomic_write(backup_path, _json_text(item.source))
        meta = {
            "backup_file": backup_path.name,
            "backup_digest": canonical_digest(item.source),
            "source_schema": item.source_schema,
            "source_generation": item.source_generation,
            "target_schema": item.target_schema,
            "migration_ids": [record.migration_id for record in item.records],
            "created_at": _now_iso(),
        }
        _atomic_write(
            backup_path.with_name(backup_path.name + ".meta.json"),
            _json_text(meta),
        )
        item.backup_path = backup_path
        item.backup_digest = canonical_digest(item.source)

    def _backup_candidates(self, target: str) -> list[Path]:
        directory = self.state_dir if target == "registry" else self.queue_dir
        if not directory.is_dir():
            return []
        prefix = "registry.backup." if target == "registry" else "queue.backup."
        return [
            path
            for path in directory.glob(f"{prefix}*")
            if path.is_file() and not path.name.endswith(".meta.json")
        ]

    def _verified_backup(
        self, target: str, journal: dict[str, Any]
    ) -> Optional[tuple[dict[str, Any], Path, dict[str, Any]]]:
        expected = ((journal.get("targets") or {}).get(target) or {}).get(
            "input_digest"
        )
        candidates = sorted(
            self._backup_candidates(target),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            payload, digest, error = self._read_payload(path)
            if payload is None:
                continue
            meta, _, _ = self._read_payload(
                path.with_name(path.name + ".meta.json")
            )
            if meta is None or meta.get("backup_digest") != digest:
                continue
            if expected and digest != expected:
                continue
            return payload, path, meta
        return None

    # -- journal --------------------------------------------------------

    def _build_journal(self, prepared: list[_PreparedTarget]) -> dict[str, Any]:
        return {
            "phase": "PREPARED",
            "created_at": _now_iso(),
            "state_dir": str(self.state_dir),
            "targets": {
                item.target: {
                    "path": str(item.path),
                    "source_schema": item.source_schema,
                    "target_schema": item.target_schema,
                    "input_digest": canonical_digest(item.source),
                    "output_digest": canonical_digest(item.migrated),
                    "source_generation": item.source_generation,
                    "generation": item.generation,
                    "backup": str(item.backup_path) if item.backup_path else "",
                    "backup_digest": item.backup_digest,
                }
                for item in prepared
            },
            "steps": [
                record.to_dict()
                for item in prepared
                for record in item.records
            ],
        }

    # -- public api -----------------------------------------------------

    def dry_run(self) -> MigrationReport:
        """Copy state to a sandbox, migrate/validate/check invariants there."""
        with tempfile.TemporaryDirectory(
            prefix="gbt-registry-migration-"
        ) as tmp:
            sandbox = Path(tmp) / AUTOMATION_STATE_SUBDIR
            for target in TARGET_ORDER:
                source = self.path_for(target)
                if not source.is_file():
                    continue
                destination = sandbox / source.relative_to(self.state_dir)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            sandbox_engine = type(self)(sandbox)
            report = sandbox_engine._execute()
            report.dry_run = True
            if report.ok:
                report.status = "dry-run"
            return report

    def migrate(self) -> MigrationReport:
        """Formal run: backup -> migrate -> validate -> atomic replace."""
        return self._execute()

    def recover(self) -> RecoveryReport:
        """Classify every state file; restore PARTIAL from a verified backup."""
        classifications = self.classify()
        journal = self._load_journal()
        actions: dict[str, str] = {}
        errors: list[str] = []
        for target, info in classifications.items():
            if info.classification is Classification.OLD_VALID:
                actions[target] = "KEEP_OLD"
                continue
            if info.classification is Classification.NEW_VALID:
                actions[target] = "KEEP_NEW"
                continue
            backup = self._verified_backup(target, journal)
            if backup is None:
                actions[target] = "QUARANTINED"
                errors.append(
                    f"{target}: PARTIAL without verified backup "
                    f"({info.reason}); new data not adopted"
                )
                continue
            payload, backup_path, _ = backup
            _atomic_write(self.path_for(target), _json_text(payload))
            actions[target] = "RESTORED"
            self._append_ledger(
                [
                    StepRecord(
                        migration_id=f"recovery-restore-{target}",
                        target=target,
                        old_schema=info.schema or 0,
                        new_schema=detect_schema(payload),
                        input_digest=info.digest,
                        output_digest=canonical_digest(payload),
                        timestamp=_now_iso(),
                        status="RESTORED",
                    )
                ]
            )
            journal.setdefault("recovery", {})[target] = {
                "action": "RESTORED",
                "backup": str(backup_path),
                "restored_at": _now_iso(),
            }
        self._cleanup_temp_files()
        if journal.get("recovery"):
            try:
                self._write_journal(journal)
            except OSError:
                pass
        return RecoveryReport(
            classifications={
                target: info.classification.value
                for target, info in classifications.items()
            },
            actions=actions,
            errors=errors,
        )

    def _cleanup_temp_files(self) -> None:
        for path in (self.registry_path, self.queue_path):
            if not path.parent.is_dir():
                continue
            for tmp in path.parent.glob(f"{path.name}.*.tmp"):
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # -- execution ------------------------------------------------------

    def _execute(self) -> MigrationReport:
        classifications = self.classify()
        flat = {
            target: info.classification.value
            for target, info in classifications.items()
        }
        if not classifications:
            return MigrationReport(
                status="blocked",
                errors=["no registry/queue state files found"],
            )
        if "registry" not in classifications:
            return MigrationReport(
                status="blocked",
                classifications=flat,
                errors=["registry.json is missing"],
            )
        partial = {
            target: info
            for target, info in classifications.items()
            if info.classification is Classification.PARTIAL
        }
        if partial:
            return MigrationReport(
                status="blocked",
                classifications=flat,
                errors=[
                    f"{target}: PARTIAL ({info.reason}); "
                    "run recover() before migrating"
                    for target, info in partial.items()
                ],
            )
        pending = [
            target
            for target in TARGET_ORDER
            if target in classifications
            and classifications[target].classification is Classification.OLD_VALID
        ]
        if not pending:
            return MigrationReport(
                status="noop", noop=True, classifications=flat
            )
        try:
            prepared = self._prepare(pending)
        except (MigrationError, SchemaError) as error:
            return MigrationReport(
                status="failed",
                classifications=flat,
                errors=[str(error)],
            )
        if not prepared:
            return MigrationReport(
                status="noop", noop=True, classifications=flat
            )
        try:
            for item in prepared:
                self._write_backup(item)
            journal = self._build_journal(prepared)
            self._write_journal(journal)
        except OSError as error:
            return MigrationReport(
                status="failed",
                classifications=flat,
                errors=[f"backup/journal write failed: {error}"],
            )
        applied: list[dict[str, Any]] = []
        try:
            for item in prepared:
                _atomic_write(item.path, _json_text(item.migrated))
                self._append_ledger(item.records)
                applied.extend(record.to_dict() for record in item.records)
        except OSError as error:
            journal["phase"] = "FAILED"
            journal["error"] = str(error)
            journal["failed_at"] = _now_iso()
            try:
                self._write_journal(journal)
            except OSError:
                pass
            return MigrationReport(
                status="failed",
                classifications=flat,
                steps=applied,
                backups={
                    item.target: str(item.backup_path)
                    for item in prepared
                    if item.backup_path
                },
                errors=[f"atomic replace failed; old state preserved: {error}"],
            )
        warnings: list[str] = []
        journal["phase"] = "COMPLETED"
        journal["completed_at"] = _now_iso()
        try:
            self._write_journal(journal)
        except OSError as error:
            warnings.append(f"journal finalize failed: {error}")
        return MigrationReport(
            status="migrated",
            classifications=flat,
            steps=applied,
            backups={
                item.target: str(item.backup_path)
                for item in prepared
                if item.backup_path
            },
            warnings=warnings,
        )


__all__ = ["MigrationOpsMixin"]
