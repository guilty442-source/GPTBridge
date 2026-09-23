from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .training_repo_schema import TransformerTrainingSchemaMixin
from .transformer_adapter_registry import TransformerAdapterRegistryMixin
from .transformer_training_repository_audit import (
    TransformerTrainingAuditMixin,
)
from .transformer_training_repository_datasets import (
    TransformerTrainingDatasetsMixin,
)
from .transformer_training_repository_jobs import TransformerTrainingJobsMixin


_STATUS_TABLE_NAMES = (
    "transformer_training_dataset",
    "transformer_training_dataset_example",
    "transformer_training_job",
    "transformer_adapter_candidate",
    "transformer_adapter_evaluation",
    "transformer_adapter_release",
    "transformer_runtime_model_state",
    "transformer_training_audit_event",
)


class TransformerTrainingRepository(
    TransformerAdapterRegistryMixin,
    TransformerTrainingJobsMixin,
    TransformerTrainingDatasetsMixin,
    TransformerTrainingAuditMixin,
    TransformerTrainingSchemaMixin,
):
    """Isolated persistence for governed Transformer adapter training.

    Role databases remain the owners of approved language examples.  This
    database stores immutable snapshot references, training jobs, adapter
    candidates, evaluations, releases and a hash-chained audit trail.  It does
    not grant the running model authority to replace its own base weights.
    """

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve() / "xingcheng"
        self.database_path = (
            self.tool_root / "runtime" / "state" / self.DATABASE_NAME
        ).resolve()
        if not self.database_path.is_relative_to(self.tool_root):
            raise PermissionError("TRANSFORMER_TRAINING_DATABASE_SCOPE_DENIED")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def database_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            integrity = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            tables = {
                table: int(
                    connection.execute(  # sql-ok: fixed/introspected identifiers
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in _STATUS_TABLE_NAMES
            }
            state = connection.execute(
                "SELECT singleton_id, base_model_id, runtime_model_id, "
                "active_adapter_id, previous_adapter_id, "
                "automatic_weight_replacement, updated_at "
                "FROM transformer_runtime_model_state "
                "WHERE singleton_id = 1"
            ).fetchone()
        audit = self.verify_audit_chain()
        return {
            "ok": integrity.casefold() == "ok" and audit["ok"],
            "engine": "local-sqlite3-degraded",
            "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
            "canonical_central_engine": "postgresql",
            "canonical": False,
            "authority": "non-canonical-reconciliation-required",
            "reconciliation_required": True,
            "schema": "star-transformer-training-database/v1",
            "schema_version": user_version,
            "path": str(self.database_path),
            "size_bytes": self.database_path.stat().st_size,
            "sqlite_integrity": integrity,
            "tables": tables,
            "audit_chain": audit,
            "runtime_model_state": dict(state) if state is not None else {},
            "base_weights_immutable": True,
            "automatic_weight_replacement": False,
            "role_database_ownership_preserved": True,
        }

    def maintain(self) -> dict[str, Any]:
        before = self.database_status()
        with self._connect() as connection:
            connection.execute("PRAGMA optimize")
            last_maintenance = connection.execute(
                """
                SELECT created_at FROM transformer_training_audit_event
                WHERE event_type = 'database-maintained'
                ORDER BY sequence DESC LIMIT 1
                """
            ).fetchone()
            should_record = last_maintenance is None
            if last_maintenance is not None:
                try:
                    last_at = datetime.fromisoformat(str(last_maintenance[0]))
                    should_record = (
                        datetime.now(timezone.utc) - last_at
                    ).total_seconds() >= 86_400
                except ValueError:
                    should_record = True
            if should_record or before["ok"] is not True:
                self._append_audit(
                    connection,
                    event_type="database-maintained",
                    entity_type="training-database",
                    entity_id=self.DATABASE_NAME,
                    payload={
                        "schema_version": self.SCHEMA_VERSION,
                        "integrity_before": before["sqlite_integrity"],
                        "audit_chain_before": before["audit_chain"]["ok"],
                    },
                )
        return self.database_status()


__all__ = ["TransformerTrainingRepository"]
