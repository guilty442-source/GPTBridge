"""SQL Machine Schema Registry — 正式機器模式註冊。

所有治理契約以 Machine Schema 形式登記，生成 closure evidence。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .sql_concurrency import (
    TransactionPolicySchema,
    IsolationPolicySchema,
    RevisionControlSchema,
    IdempotencyPolicySchema,
    PoolPolicySchema,
    SessionBindingSchema,
    MigrationExecutionSchema,
    OutboxContractSchema,
    InboxDedupSchema,
    ClosureEvidence,
    SqlAuthorityClosure,
    SqlSchemaMigrationClosure,
    InformationSqlClosure,
    SqlTransactionConcurrencyClosure,
    SqlGovernanceClosure,
)

_logger = logging.getLogger("gptbridge.sql.schema_registry")


class MachineSchemaRegistry:
    """Registers and validates all SQL governance machine schemas."""

    def __init__(self) -> None:
        self._schemas: dict[str, Any] = {}

    def register_all(self) -> dict[str, Any]:
        """Register all governance machine schemas."""
        schemas = {
            "transaction_policy": TransactionPolicySchema(),
            "isolation_policy": IsolationPolicySchema(),
            "revision_control": RevisionControlSchema(),
            "idempotency_policy": IdempotencyPolicySchema(),
            "pool_policy": PoolPolicySchema(),
            "session_binding": SessionBindingSchema(),
            "migration_execution": MigrationExecutionSchema(),
            "outbox_contract": OutboxContractSchema(),
            "inbox_dedup": InboxDedupSchema(),
        }

        for code, schema in schemas.items():
            self._schemas[schema.schema_code] = schema
            _logger.info("MachineSchemaRegistry: registered %s", schema.schema_code)

        return schemas

    def get_schema(self, schema_code: str) -> Any:
        return self._schemas.get(schema_code)

    def validate_instance(self, schema_code: str, instance: dict[str, Any]) -> tuple[bool, list[str]]:
        """Validate an instance against its machine schema."""
        schema = self._schemas.get(schema_code)
        if not schema:
            return False, [f"Unknown schema: {schema_code}"]

        errors = []
        for field in schema.required_fields:
            if field not in instance:
                errors.append(f"Missing required field: {field}")
        return len(errors) == 0, errors

    def compute_schema_hash(self, schema: Any) -> str:
        """Compute deterministic hash of schema definition."""
        # Serialize schema definition (excluding runtime fields)
        definition = {
            "schema_code": schema.schema_code,
            "schema_kind": schema.schema_kind,
            "semantic_owner": schema.semantic_owner,
            "required_fields": list(schema.required_fields),
            "status": schema.status,
        }
        content = json.dumps(definition, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()


def build_closure_evidence(
    transaction_policy_hash: str,
    isolation_policy_hash: str,
    revision_control_hash: str,
    idempotency_policy_hash: str,
    pool_policy_hash: str,
    session_binding_hash: str,
    migration_execution_hash: str,
    migration_receipt_chain_hash: str,
    outbox_contract_hash: str,
    inbox_contract_hash: str,
    test_evidence_hash: str,
    open_findings: list[str] = None,
    result: str = "PENDING",
) -> ClosureEvidence:
    """Build closure evidence for SQL governance (A506)."""
    return ClosureEvidence(
        closure_id=f"sql-governance-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        codex_version="A500-A506",
        transaction_policy_hash=transaction_policy_hash,
        isolation_policy_hash=isolation_policy_hash,
        revision_control_hash=revision_control_hash,
        idempotency_policy_hash=idempotency_policy_hash,
        pool_policy_hash=pool_policy_hash,
        session_binding_hash=session_binding_hash,
        migration_execution_hash=migration_execution_hash,
        migration_receipt_chain_hash=migration_receipt_chain_hash,
        outbox_contract_hash=outbox_contract_hash,
        inbox_contract_hash=inbox_contract_hash,
        test_evidence_hash=test_evidence_hash,
        open_findings=open_findings or [],
        result=result,
    )


def evaluate_sql_governance_closure(
    authority_result: str,
    schema_migration_result: str,
    information_result: str,
    transaction_concurrency_result: str,
) -> SqlGovernanceClosure:
    """Evaluate overall SQL governance closure."""
    return SqlGovernanceClosure(
        authority=SqlAuthorityClosure(result=authority_result),
        schema_migration=SqlSchemaMigrationClosure(result=schema_migration_result),
        information=InformationSqlClosure(result=information_result),
        transaction_concurrency=SqlTransactionConcurrencyClosure(result=transaction_concurrency_result),
    )


MACHINE_SCHEMA_REGISTRY = MachineSchemaRegistry()


__all__ = [
    "MachineSchemaRegistry",
    "MACHINE_SCHEMA_REGISTRY",
    "build_closure_evidence",
    "evaluate_sql_governance_closure",
]