"""Maintenance Verifier.

Post-execution verification for all maintenance actions.
SQL command success != maintenance success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .models import MaintenanceRiskClass, MaintenanceReasonCode
from .registry import MaintenanceAction


@dataclass(frozen=True)
class VerificationResult:
    """Result of a maintenance verification."""

    verified: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=datetime.utcnow)


class Verifier:
    """Verifies maintenance action outcomes."""

    def __init__(self) -> None:
        self._custom_verifiers: dict[str, Callable] = {}

    def register_verifier(
        self,
        action_id: str,
        verifier: Callable[[MaintenanceAction, dict[str, Any], dict[str, Any], dict[str, Any]], tuple[bool, str]],
    ) -> None:
        """Register a custom verifier for an action."""
        self._custom_verifiers[action_id] = verifier

    def verify(
        self,
        action: MaintenanceAction,
        before_state: dict[str, Any],
        after_state: dict[str, Any],
        context: dict[str, Any],
    ) -> VerificationResult:
        """Verify an action's outcome.

        First tries the action's built-in verification_contract,
        then falls back to custom verifiers.
        """
        # Try built-in verification contract
        try:
            verified, reason = action.verification_contract(before_state, after_state, context)
            if verified:
                return VerificationResult(True, reason, {"source": "builtin"})
            # If built-in fails, try custom
        except Exception:
            pass

        # Try custom verifier
        custom = self._custom_verifiers.get(action.action_id)
        if custom:
            try:
                verified, reason = custom(action, before_state, after_state, context)
                return VerificationResult(verified, reason, {"source": "custom"})
            except Exception as exc:
                return VerificationResult(False, f"Custom verifier error: {exc}")

        # Default: require explicit success marker
        if after_state.get("maintenance_verified", False):
            return VerificationResult(True, "Explicit verification marker present")
        return VerificationResult(False, "No verification performed")


def verify_action(
    action: MaintenanceAction,
    before_state: dict[str, Any],
    after_state: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Convenience function to verify an action."""
    verifier = Verifier()
    result = verifier.verify(action, before_state, after_state, context)
    return result.verified, result.reason


# --- Engine-specific verification functions ---

def verify_pg_analyze(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify PostgreSQL ANALYZE execution."""
    # Check explicit completion marker
    if not after.get("analyze_completed", False):
        return False, "ANALYZE did not report completion"

    # Check statistics refreshed
    if not after.get("stats_refreshed", False):
        return False, "Statistics not refreshed"

    # Check no new critical locks
    if after.get("new_critical_locks", 0) > 0:
        return False, f"New critical locks detected: {after['new_critical_locks']}"

    # Check database still healthy
    if not after.get("pg_healthy", True):
        return False, "PostgreSQL unhealthy after ANALYZE"

    return True, "ANALYZE verified"


def verify_sqlite_checkpoint(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify SQLite checkpoint execution."""
    if not after.get("checkpoint_completed", False):
        return False, "Checkpoint did not complete"

    wal_before = before.get("wal_size_mb", 0)
    wal_after = after.get("wal_size_mb", 0)

    # WAL should have decreased (at least some progress)
    if wal_after >= wal_before * 0.9:
        return False, f"WAL size did not decrease: {wal_before:.1f}MB -> {wal_after:.1f}MB"

    # Check integrity not worsened
    if after.get("integrity_worsened", False):
        return False, "Integrity check worsened after checkpoint"

    # Database should still be accessible
    if not after.get("db_accessible", True):
        return False, "Database not accessible after checkpoint"

    return True, "Checkpoint verified"


def verify_reconcile_throttle(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify reconcile throttle adjustment."""
    new_rate = after.get("reconcile_rate", 0)
    new_batch = after.get("reconcile_batch", 0)

    min_rate = context.get("reconcile_min_rate", 10)
    max_rate = context.get("reconcile_max_rate", 1000)
    min_batch = context.get("reconcile_min_batch", 10)
    max_batch = context.get("reconcile_max_batch", 500)

    if not (min_rate <= new_rate <= max_rate):
        return False, f"Rate {new_rate} outside bounds [{min_rate}, {max_rate}]"

    if not (min_batch <= new_batch <= max_batch):
        return False, f"Batch {new_batch} outside bounds [{min_batch}, {max_batch}]"

    # Check policy state was updated
    if not after.get("policy_updated", False):
        return False, "Reconcile policy not updated"

    # Check transport latency didn't worsen significantly
    latency_before = before.get("transport_latency_ms", 0)
    latency_after = after.get("transport_latency_ms", 0)
    if latency_after > latency_before * 1.5:
        return False, f"Transport latency worsened: {latency_before}ms -> {latency_after}ms"

    return True, "Reconcile throttle verified"


def verify_backup_verify(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify backup verification."""
    if not after.get("checksum_verified", False):
        return False, "Backup checksum verification failed"

    if not after.get("schema_valid", False):
        return False, "Backup schema validation failed"

    if not after.get("integrity_valid", False):
        return False, "Backup integrity check failed"

    if not after.get("release_valid", False):
        return False, "Backup release validation failed"

    # Restore test (if performed)
    if after.get("restore_test_attempted", False):
        if not after.get("restore_test_passed", False):
            return False, "Restore test failed"

    return True, "Backup verified"


def verify_projection_rebuild(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify projection rebuild."""
    if not after.get("generation_correct", False):
        return False, "Projection generation mismatch"

    if not after.get("source_revision_matches", False):
        return False, "Source revision mismatch"

    if not after.get("projection_readable", False):
        return False, "Projection not readable"

    if not after.get("row_count_contract_ok", False):
        return False, "Row count contract failed"

    if not after.get("hash_contract_ok", False):
        return False, "Hash contract failed"

    return True, "Projection rebuild verified"


def verify_health_observe(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify health observation (always passes if data collected)."""
    if after.get("metrics_collected", False):
        return True, "Health metrics collected"
    return False, "No metrics collected"


# Register default verifiers
def _register_default_verifiers(verifier: Verifier) -> None:
    verifier.register_verifier("pg_analyze_table_v1", verify_pg_analyze)
    verifier.register_verifier("sqlite_checkpoint_v1", verify_sqlite_checkpoint)
    verifier.register_verifier("reconcile_throttle_v1", verify_reconcile_throttle)
    verifier.register_verifier("backup_verify_v1", verify_backup_verify)
    verifier.register_verifier("projection_rebuild_v1", verify_projection_rebuild)
    verifier.register_verifier("pg_health_observe_v1", verify_health_observe)
    verifier.register_verifier("sqlite_health_observe_v1", verify_health_observe)
    verifier.register_verifier("reconcile_health_observe_v1", verify_health_observe)


# Global verifier with defaults
_default_verifier: Verifier | None = None


def get_verifier() -> Verifier:
    """Get global verifier with default verifiers registered."""
    global _default_verifier
    if _default_verifier is None:
        _default_verifier = Verifier()
        _register_default_verifiers(_default_verifier)
    return _default_verifier


__all__ = [
    "VerificationResult",
    "Verifier",
    "verify_action",
    "get_verifier",
    "verify_pg_analyze",
    "verify_sqlite_checkpoint",
    "verify_reconcile_throttle",
    "verify_backup_verify",
    "verify_projection_rebuild",
    "verify_health_observe",
]