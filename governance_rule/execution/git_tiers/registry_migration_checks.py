"""Registry-migration classification / validation / invariants (A185 split).

Extracted from ``registry_migration_engine.py`` (source-size contract):
the OLD_VALID / NEW_VALID / PARTIAL crash taxonomy, the schema-shape and
field-type validators and the migration-contract invariants (queue
ordering/priority/SHA/state preservation; no ``failed`` promotion).
Mixed into ``registry_migration_engine.RegistryMigrationEngine``.
"""
from __future__ import annotations

from typing import Any, Optional

from .registry_migrations import (
    STEPS_BY_TARGET,
    TARGET_QUEUE_SCHEMA,
    TARGET_REGISTRY_SCHEMA,
    SchemaError,
    detect_schema,
)
from .registry_migration_support import (
    TARGET_ORDER,
    Classification,
    TargetClassification,
    _TARGET_SCHEMA,
    _is_int,
    _is_number,
    _queue_view,
    _registry_view,
)


class MigrationChecksMixin:
    """Crash classification + contract validation surface."""

    # -- classification -------------------------------------------------

    def classify(self) -> dict[str, TargetClassification]:
        journal = self._load_journal()
        result: dict[str, TargetClassification] = {}
        for target in TARGET_ORDER:
            path = self.path_for(target)
            if not path.is_file():
                continue
            result[target] = self._classify_one(target, journal)
        return result

    def _classify_one(
        self, target: str, journal: dict[str, Any]
    ) -> TargetClassification:
        path = self.path_for(target)
        payload, digest, error = self._read_payload(path)
        if payload is None:
            return TargetClassification(
                target, Classification.PARTIAL, None, "", error
            )
        try:
            schema: Optional[int] = detect_schema(payload)
        except SchemaError as exc:
            return TargetClassification(
                target, Classification.PARTIAL, None, digest, str(exc)
            )
        target_schema = _TARGET_SCHEMA[target]
        expected = (
            (journal.get("targets") or {}).get(target) or {}
            if journal
            else {}
        )
        if expected:
            if digest == expected.get("output_digest"):
                errors = self._validate(target, payload)
                errors.extend(self._invariant_from_backup(target, payload))
                if not errors:
                    return TargetClassification(
                        target,
                        Classification.NEW_VALID,
                        schema,
                        digest,
                        "matches recorded migration output",
                    )
                return TargetClassification(
                    target,
                    Classification.PARTIAL,
                    schema,
                    digest,
                    "; ".join(errors),
                )
            if digest == expected.get("input_digest"):
                return TargetClassification(
                    target,
                    Classification.OLD_VALID,
                    schema,
                    digest,
                    "migration not applied (recorded input)",
                )
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                "digest matches neither recorded input nor output",
            )
        if schema == target_schema:
            errors = self._validate(target, payload)
            if not errors:
                return TargetClassification(
                    target, Classification.NEW_VALID, schema, digest, ""
                )
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                "; ".join(errors),
            )
        first_schema = STEPS_BY_TARGET[target][0].old_schema
        if schema < first_schema:
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                f"schema {schema} below migration chain",
            )
        if schema < target_schema and self._shape_ok(target, payload):
            return TargetClassification(
                target,
                Classification.OLD_VALID,
                schema,
                digest,
                "legacy schema, migration pending",
            )
        if schema > target_schema:
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                "schema newer than engine target; refusing to downgrade",
            )
        return TargetClassification(
            target,
            Classification.PARTIAL,
            schema,
            digest,
            "legacy schema fails shape check",
        )

    def _invariant_from_backup(
        self, target: str, payload: dict[str, Any]
    ) -> list[str]:
        backup = self._verified_backup(target, self._load_journal())
        if backup is None:
            return []
        source = backup[0]
        return self._invariant(target, source, payload)

    # -- validation -----------------------------------------------------

    def _validate(self, target: str, payload: dict[str, Any]) -> list[str]:
        if target == "registry":
            return self._validate_registry(payload)
        return self._validate_queue(payload)

    @staticmethod
    def _validate_registry(payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            schema = detect_schema(payload, default=0)
        except SchemaError as exc:
            return [str(exc)]
        if schema != TARGET_REGISTRY_SCHEMA:
            errors.append(
                f"registry schema {schema} != {TARGET_REGISTRY_SCHEMA}"
            )
        if "generation" in payload and not _is_int(payload["generation"]):
            errors.append("registry generation must be an integer")
        for key in ("pid", "sync_cycles"):
            if key in payload and not _is_int(payload[key]):
                errors.append(f"registry {key} must be an integer")
        for key in ("started_at", "sync_interval", "health_interval"):
            if key in payload and not _is_number(payload[key]):
                errors.append(f"registry {key} must be a number")
        if "health" in payload and not isinstance(payload["health"], dict):
            errors.append("registry health must be a mapping")
        children = payload.get("children")
        if not isinstance(children, list):
            errors.append("registry children must be a list")
            return errors
        for index, child in enumerate(children):
            if not isinstance(child, dict):
                errors.append(f"registry child {index} must be a mapping")
                continue
            for key in ("pid", "restarts"):
                if key in child and not _is_int(child[key]):
                    errors.append(f"registry child {index} {key} must be integer")
            if "started_at" in child and not _is_number(child["started_at"]):
                errors.append(f"registry child {index} started_at must be number")
            if "watcher_id" in child and not isinstance(child["watcher_id"], str):
                errors.append(f"registry child {index} watcher_id must be string")
            if "state" in child and not isinstance(child["state"], str):
                errors.append(f"registry child {index} state must be string")
        return errors

    @staticmethod
    def _validate_queue(payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            schema = detect_schema(payload, default=0)
        except SchemaError as exc:
            return [str(exc)]
        if schema != TARGET_QUEUE_SCHEMA:
            errors.append(
                f"queue schema {schema} != {TARGET_QUEUE_SCHEMA}"
            )
        if "generation" in payload and not _is_int(payload["generation"]):
            errors.append("queue generation must be an integer")
        next_sequence = payload.get("next_sequence")
        if not _is_int(next_sequence) or int(next_sequence) < 1:
            errors.append("queue next_sequence must be an integer >= 1")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            errors.append("queue entries must be a list")
            return errors
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"queue entry {index} must be a mapping")
                continue
            for key in ("source_commit", "base_main_commit", "status"):
                value = entry.get(key)
                if not isinstance(value, str) or not value:
                    errors.append(f"queue entry {index} {key} must be a string")
            if "priority" in entry and not _is_int(entry["priority"]):
                errors.append(f"queue entry {index} priority must be integer")
            if "enqueue_sequence" in entry and not _is_int(
                entry["enqueue_sequence"]
            ):
                errors.append(
                    f"queue entry {index} enqueue_sequence must be integer"
                )
            if "state" in entry and not isinstance(entry["state"], str):
                errors.append(f"queue entry {index} state must be string")
            if "transaction_id" in entry and not isinstance(
                entry["transaction_id"], str
            ):
                errors.append(
                    f"queue entry {index} transaction_id must be string"
                )
        return errors

    @staticmethod
    def _shape_ok(target: str, payload: dict[str, Any]) -> bool:
        if target == "registry":
            children = payload.get("children", [])
            return isinstance(children, list) and all(
                isinstance(child, dict) for child in children
            )
        entries = payload.get("entries", [])
        return isinstance(entries, list) and all(
            isinstance(entry, dict) for entry in entries
        )

    # -- invariants -----------------------------------------------------

    def _invariant(
        self,
        target: str,
        source: dict[str, Any],
        migrated: dict[str, Any],
    ) -> list[str]:
        if target == "registry":
            return self._invariant_registry(source, migrated)
        return self._invariant_queue(source, migrated)

    @staticmethod
    def _invariant_registry(
        source: dict[str, Any], migrated: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        if _registry_view(source) != _registry_view(migrated):
            errors.append(
                "registry content changed outside the migration contract"
            )
        if len(source.get("children", [])) != len(migrated.get("children", [])):
            errors.append("registry children count changed")
        missing = set(source) - set(migrated)
        if missing:
            errors.append(f"registry lost keys: {sorted(missing)}")
        for index, (before, after) in enumerate(
            zip(source.get("children", []), migrated.get("children", []))
        ):
            if isinstance(before, dict) and isinstance(after, dict):
                lost = set(before) - set(after)
                if lost:
                    errors.append(
                        f"registry child {index} lost keys: {sorted(lost)}"
                    )
        return errors

    @staticmethod
    def _invariant_queue(
        source: dict[str, Any], migrated: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        if _queue_view(source) != _queue_view(migrated):
            errors.append(
                "queue content changed outside the migration contract "
                "(ordering/priority/SHA/state must be preserved)"
            )
        source_entries = source.get("entries", [])
        migrated_entries = migrated.get("entries", [])
        if len(source_entries) != len(migrated_entries):
            errors.append("queue entry count changed")
        missing = set(source) - set(migrated)
        if missing:
            errors.append(f"queue lost keys: {sorted(missing)}")
        for before, after in zip(source_entries, migrated_entries):
            if isinstance(before, dict) and isinstance(after, dict):
                lost = set(before) - set(after)
                if lost:
                    errors.append(
                        f"queue entry {after.get('queue_id')} lost keys: "
                        f"{sorted(lost)}"
                    )
            for key in ("source_commit", "base_main_commit"):
                if before.get(key) != after.get(key):
                    errors.append(
                        f"queue entry {after.get('queue_id')}: {key} changed"
                    )
            if before.get("status") != after.get("status"):
                errors.append(
                    f"queue entry {after.get('queue_id')}: status changed"
                )
            if after.get("status") == "failed" and after.get("state") not in {
                "failed",
                "",
            }:
                errors.append(
                    f"queue entry {after.get('queue_id')}: failed promoted"
                )
        return errors


__all__ = ["MigrationChecksMixin"]
