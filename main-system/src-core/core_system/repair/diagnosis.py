"""Repair Engine — Diagnosis Engine (Phase 1).

Deterministic rule-based diagnosis with versioned rules.
Converts telemetry into structured diagnosis codes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

_logger = logging.getLogger("gptbridge.repair.diagnosis")


class DiagnosisCode(str, Enum):
    """Standardized diagnosis codes."""
    PG_POOL_SATURATION = "PG_POOL_SATURATION"
    PG_LOCK_CONTENTION = "PG_LOCK_CONTENTION"
    PG_WAL_PRESSURE = "PG_WAL_PRESSURE"
    PG_LONG_RUNNING_TX = "PG_LONG_RUNNING_TX"
    PG_IDLE_IN_TRANSACTION = "PG_IDLE_IN_TRANSACTION"
    PG_SLOW_QUERIES = "PG_SLOW_QUERIES"
    PG_CONNECTION_LEAK = "PG_CONNECTION_LEAK"
    SQLITE_WAL_STALLED = "SQLITE_WAL_STALLED"
    SQLITE_BUSY_STORM = "SQLITE_BUSY_STORM"
    SQLITE_CORRUPTION_RISK = "SQLITE_CORRUPTION_RISK"
    RECONCILE_BACKLOG = "RECONCILE_BACKLOG"
    RECONCILE_STALLED = "RECONCILE_STALLED"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    MIGRATION_PENDING = "MIGRATION_PENDING"
    RLS_DRIFT = "RLS_DRIFT"
    RLS_MISSING = "RLS_MISSING"
    BYPASSRLS_DETECTED = "BYPASSRLS_DETECTED"
    UNEXPECTED_PUBLIC_GRANT = "UNEXPECTED_PUBLIC_GRANT"
    QDRANT_METADATA_MISMATCH = "QDRANT_METADATA_MISMATCH"
    QDRANT_LAG = "QDRANT_LAG"
    QDRANT_UNAVAILABLE = "QDRANT_UNAVAILABLE"
    BACKUP_UNVERIFIED = "BACKUP_UNVERIFIED"
    BACKUP_STALE = "BACKUP_STALE"
    AUTHORITY_CONFLICT = "AUTHORITY_CONFLICT"
    PERMISSION_DRIFT = "PERMISSION_DRIFT"
    OUTBOX_STALLED = "OUTBOX_STALLED"
    INBOX_DEDUP_FAILURE = "INBOX_DEDUP_FAILURE"
    TRANSPORT_BACKLOG = "TRANSPORT_BACKLOG"
    EMBEDDING_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
    RERANKER_UNAVAILABLE = "RERANKER_UNAVAILABLE"


class Severity(str, Enum):
    """Diagnosis severity."""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


@dataclass(frozen=True)
class DiagnosisRule:
    """Versioned diagnosis rule (deterministic, no ML)."""
    rule_id: str
    rule_version: str
    diagnosis_code: DiagnosisCode
    severity: Severity
    inputs: tuple[str, ...]                    # Required telemetry inputs
    thresholds: dict[str, float]               # Numeric thresholds
    hysteresis: dict[str, float] = field(default_factory=dict)  # Hysteresis values
    cooldown_seconds: int = 300                # Minimum time between firings
    condition: str = ""                        # Human-readable condition
    confidence_source: str = "RULE"            # RULE | HEURISTIC | MODEL

    def evaluate(self, telemetry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Evaluate rule against telemetry. Returns (triggered, context)."""
        # Check all required inputs present
        for inp in self.inputs:
            if inp not in telemetry:
                return False, {"missing_input": inp}

        # Evaluate thresholds
        for key, threshold in self.thresholds.items():
            value = telemetry.get(key)
            if value is None:
                return False, {f"missing_threshold_input": key}
            # Support both > and < thresholds via key suffix
            if key.endswith("_gt") and value <= threshold:
                return False, {f"below_threshold": key, "value": value, "threshold": threshold}
            if key.endswith("_lt") and value >= threshold:
                return False, {f"above_threshold": key, "value": value, "threshold": threshold}
            if key.endswith("_eq") and value != threshold:
                return False, {f"not_equal": key, "value": value, "expected": threshold}

        # Check hysteresis (prevent flapping)
        for key, hyst in self.hysteresis.items():
            value = telemetry.get(key)
            if value is not None and abs(value - threshold) < hyst:
                return False, {f"within_hysteresis": key, "value": value, "hysteresis": hyst}

        return True, {"telemetry_snapshot": {k: telemetry.get(k) for k in self.inputs}}


class DiagnosisEngine:
    """Deterministic rule-based diagnosis engine."""

    def __init__(self) -> None:
        self._rules: dict[str, DiagnosisRule] = {}
        self._last_fired: dict[str, float] = {}  # rule_id -> timestamp
        self._register_default_rules()

    def _register_default_rules(self) -> None:
        """Register default diagnosis rules."""
        rules = [
            # PostgreSQL Pool Saturation
            DiagnosisRule(
                rule_id="PG_POOL_SATURATION_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.PG_POOL_SATURATION,
                severity=Severity.CRITICAL,
                inputs=("pg_pool_waiters", "pg_pool_acquire_latency_p95"),
                thresholds={"pg_pool_waiters_gt": 5, "pg_pool_acquire_latency_p95_gt": 1000},  # ms
                hysteresis={"pg_pool_waiters": 2, "pg_pool_acquire_latency_p95": 200},
                cooldown_seconds=60,
                condition="pool_waiters > 5 AND acquire_latency_p95 > 1000ms FOR 30s",
            ),
            # PostgreSQL Lock Contention
            DiagnosisRule(
                rule_id="PG_LOCK_CONTENTION_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.PG_LOCK_CONTENTION,
                severity=Severity.CRITICAL,
                inputs=("pg_blocking_pids", "pg_max_lock_wait_ms"),
                thresholds={"pg_blocking_pids_gt": 0, "pg_max_lock_wait_ms_gt": 5000},
                hysteresis={"pg_blocking_pids": 1, "pg_max_lock_wait_ms": 1000},
                cooldown_seconds=30,
                condition="blocking_pids > 0 AND max_lock_wait > 5s",
            ),
            # PostgreSQL WAL Pressure
            DiagnosisRule(
                rule_id="PG_WAL_PRESSURE_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.PG_WAL_PRESSURE,
                severity=Severity.WARNING,
                inputs=("pg_wal_size_mb", "pg_wal_growth_rate_mb_per_min"),
                thresholds={"pg_wal_size_mb_gt": 2048, "pg_wal_growth_rate_mb_per_min_gt": 50},
                hysteresis={"pg_wal_size_mb": 256, "pg_wal_growth_rate_mb_per_min": 10},
                cooldown_seconds=300,
                condition="WAL > 2GB AND growth > 50MB/min",
            ),
            # PostgreSQL Long Running Transaction
            DiagnosisRule(
                rule_id="PG_LONG_RUNNING_TX_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.PG_LONG_RUNNING_TX,
                severity=Severity.WARNING,
                inputs=("pg_longest_tx_age_seconds",),
                thresholds={"pg_longest_tx_age_seconds_gt": 300},
                hysteresis={"pg_longest_tx_age_seconds": 60},
                cooldown_seconds=120,
                condition="longest_tx_age > 5min",
            ),
            # PostgreSQL Idle in Transaction
            DiagnosisRule(
                rule_id="PG_IDLE_IN_TRANSACTION_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.PG_IDLE_IN_TRANSACTION,
                severity=Severity.WARNING,
                inputs=("pg_idle_in_tx_count", "pg_max_idle_tx_age_seconds"),
                thresholds={"pg_idle_in_tx_count_gt": 0, "pg_max_idle_tx_age_seconds_gt": 30},
                cooldown_seconds=60,
                condition="idle_in_transaction > 30s",
            ),
            # PostgreSQL Slow Queries
            DiagnosisRule(
                rule_id="PG_SLOW_QUERIES_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.PG_SLOW_QUERIES,
                severity=Severity.WARNING,
                inputs=("pg_slow_query_count_5min", "pg_slow_query_p95_ms"),
                thresholds={"pg_slow_query_count_5min_gt": 10, "pg_slow_query_p95_ms_gt": 2000},
                cooldown_seconds=180,
                condition="slow_queries > 10/5min AND p95 > 2s",
            ),
            # SQLite WAL Stalled
            DiagnosisRule(
                rule_id="SQLITE_WAL_STALLED_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.SQLITE_WAL_STALLED,
                severity=Severity.WARNING,
                inputs=("sqlite_wal_size_mb", "sqlite_wal_age_seconds"),
                thresholds={"sqlite_wal_size_mb_gt": 100, "sqlite_wal_age_seconds_gt": 300},
                cooldown_seconds=180,
                condition="WAL > 100MB AND age > 5min",
            ),
            # SQLite Busy Storm
            DiagnosisRule(
                rule_id="SQLITE_BUSY_STORM_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.SQLITE_BUSY_STORM,
                severity=Severity.CRITICAL,
                inputs=("sqlite_busy_count_5min", "sqlite_busy_rate_per_sec"),
                thresholds={"sqlite_busy_count_5min_gt": 50, "sqlite_busy_rate_per_sec_gt": 10},
                cooldown_seconds=30,
                condition="busy events > 50/5min OR rate > 10/s",
            ),
            # Reconcile Backlog
            DiagnosisRule(
                rule_id="RECONCILE_BACKLOG_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.RECONCILE_BACKLOG,
                severity=Severity.WARNING,
                inputs=("reconcile_pending", "reconcile_rate_per_min", "reconcile_latency_p95_ms"),
                thresholds={"reconcile_pending_gt": 100, "reconcile_latency_p95_ms_gt": 5000},
                hysteresis={"reconcile_pending": 20},
                cooldown_seconds=180,
                condition="pending > 100 AND latency > 5s",
            ),
            # Reconcile Stalled
            DiagnosisRule(
                rule_id="RECONCILE_STALLED_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.RECONCILE_STALLED,
                severity=Severity.CRITICAL,
                inputs=("reconcile_pending", "reconcile_rate_per_min"),
                thresholds={"reconcile_pending_gt": 500, "reconcile_rate_per_min_lt": 1},
                cooldown_seconds=60,
                condition="pending > 500 AND rate < 1/min",
            ),
            # Schema Drift
            DiagnosisRule(
                rule_id="SCHEMA_DRIFT_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.SCHEMA_DRIFT,
                severity=Severity.CRITICAL,
                inputs=("schema_current_hash", "schema_expected_hash"),
                thresholds={"schema_hash_mismatch_eq": 1},
                cooldown_seconds=0,  # No cooldown for drift
                condition="current_schema_hash != expected_schema_hash",
            ),
            # Migration Pending
            DiagnosisRule(
                rule_id="MIGRATION_PENDING_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.MIGRATION_PENDING,
                severity=Severity.WARNING,
                inputs=("pending_migrations",),
                thresholds={"pending_migrations_gt": 0},
                cooldown_seconds=3600,
                condition="pending_migrations > 0",
            ),
            # RLS Drift
            DiagnosisRule(
                rule_id="RLS_DRIFT_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.RLS_DRIFT,
                severity=Severity.CRITICAL,
                inputs=("rls_expected_tables", "rls_actual_tables"),
                thresholds={"rls_missing_count_gt": 0},
                cooldown_seconds=0,
                condition="expected RLS tables != actual RLS tables",
            ),
            # RLS Missing
            DiagnosisRule(
                rule_id="RLS_MISSING_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.RLS_MISSING,
                severity=Severity.EMERGENCY,
                inputs=("rls_force_missing_count",),
                thresholds={"rls_force_missing_count_gt": 0},
                cooldown_seconds=0,
                condition="FORCE RLS missing on any table",
            ),
            # BYPASSRLS Detected
            DiagnosisRule(
                rule_id="BYPASSRLS_DETECTED_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.BYPASSRLS_DETECTED,
                severity=Severity.EMERGENCY,
                inputs=("bypassrls_role_count",),
                thresholds={"bypassrls_role_count_gt": 0},
                cooldown_seconds=0,
                condition="BYPASSRLS role detected",
            ),
            # Unexpected Public Grant
            DiagnosisRule(
                rule_id="UNEXPECTED_PUBLIC_GRANT_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.UNEXPECTED_PUBLIC_GRANT,
                severity=Severity.CRITICAL,
                inputs=("unexpected_public_grants",),
                thresholds={"unexpected_public_grants_gt": 0},
                cooldown_seconds=0,
                condition="unexpected PUBLIC grant detected",
            ),
            # Qdrant Metadata Mismatch
            DiagnosisRule(
                rule_id="QDRANT_METADATA_MISMATCH_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.QDRANT_METADATA_MISMATCH,
                severity=Severity.CRITICAL,
                inputs=("qdrant_mismatch_count",),
                thresholds={"qdrant_mismatch_count_gt": 0},
                cooldown_seconds=60,
                condition="Qdrant metadata mismatch detected",
            ),
            # Qdrant Lag
            DiagnosisRule(
                rule_id="QDRANT_LAG_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.QDRANT_LAG,
                severity=Severity.WARNING,
                inputs=("qdrant_replication_lag_seconds",),
                thresholds={"qdrant_replication_lag_seconds_gt": 30},
                cooldown_seconds=120,
                condition="replication lag > 30s",
            ),
            # Outbox Stalled
            DiagnosisRule(
                rule_id="OUTBOX_STALLED_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.OUTBOX_STALLED,
                severity=Severity.WARNING,
                inputs=("outbox_pending", "outbox_oldest_age_seconds"),
                thresholds={"outbox_pending_gt": 100, "outbox_oldest_age_seconds_gt": 300},
                cooldown_seconds=180,
                condition="outbox pending > 100 AND oldest > 5min",
            ),
            # Backup Unverified
            DiagnosisRule(
                rule_id="BACKUP_UNVERIFIED_V1",
                rule_version="1.0",
                diagnosis_code=DiagnosisCode.BACKUP_UNVERIFIED,
                severity=Severity.WARNING,
                inputs=("backup_last_verified_age_hours",),
                thresholds={"backup_last_verified_age_hours_gt": 24},
                cooldown_seconds=3600,
                condition="backup not verified in 24h",
            ),
        ]
        for rule in rules:
            self.register_rule(rule)

    def register_rule(self, rule: DiagnosisRule) -> None:
        """Register a diagnosis rule."""
        key = f"{rule.rule_id}_v{rule.rule_version}"
        self._rules[key] = rule
        _logger.debug("DiagnosisEngine: registered rule %s", key)

    def diagnose(self, telemetry: dict[str, Any]) -> list["DiagnosisResult"]:
        """Run all rules against telemetry. Returns list of diagnoses."""
        results = []
        now = time.time()

        for key, rule in self._rules.items():
            # Check cooldown
            last_fired = self._last_fired.get(key, 0)
            if now - last_fired < rule.cooldown_seconds:
                continue

            triggered, context = rule.evaluate(telemetry)
            if triggered:
                self._last_fired[key] = now
                result = DiagnosisResult(
                    diagnosis_code=rule.diagnosis_code,
                    severity=rule.severity,
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    confidence_source=rule.confidence_source,
                    context=context,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                results.append(result)
                _logger.warning(
                    "DiagnosisEngine: %s triggered (rule=%s v%s)",
                    rule.diagnosis_code.value, rule.rule_id, rule.rule_version
                )

        # Sort by severity
        severity_order = {Severity.EMERGENCY: 0, Severity.CRITICAL: 1, Severity.WARNING: 2, Severity.INFO: 3}
        results.sort(key=lambda r: severity_order.get(r.severity, 99))
        return results


@dataclass(frozen=True)
class DiagnosisResult:
    """Structured diagnosis output."""
    diagnosis_code: DiagnosisCode
    severity: Severity
    rule_id: str
    rule_version: str
    confidence_source: str
    context: dict[str, Any]
    timestamp: str


__all__ = [
    "DiagnosisCode",
    "Severity",
    "DiagnosisRule",
    "DiagnosisEngine",
    "DiagnosisResult",
]