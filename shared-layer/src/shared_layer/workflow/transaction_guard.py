"""PG transaction guard for cross-engine work.

PostgreSQL transactions must stay short.  These patterns are forbidden:

  * waiting on Qdrant inside an open PG transaction,
  * running model embedding inside an open PG transaction,
  * large file I/O inside an open PG transaction,
  * waiting on human input inside an open PG transaction.

Cross-engine work belongs to the Saga executor outside the transaction.
"""

from __future__ import annotations

from typing import Final

FORBIDDEN_IN_TRANSACTION: Final[tuple[str, ...]] = (
    "qdrant-io",
    "model-embedding",
    "large-file-io",
    "human-wait",
)


class TransactionBoundaryError(RuntimeError):
    """Raised when cross-engine work is attempted inside a PG transaction."""


def assert_short_transaction(pg_transaction_open: bool, activity: str = "") -> None:
    """Fail closed when an open PG transaction precedes cross-engine work."""
    if pg_transaction_open:
        raise TransactionBoundaryError(
            "PG_TRANSACTION_MUST_STAY_SHORT:" + (activity or "cross-engine-step")
        )


def assert_activity_allowed(activity: str, *, pg_transaction_open: bool) -> None:
    if pg_transaction_open and activity in FORBIDDEN_IN_TRANSACTION:
        raise TransactionBoundaryError(f"FORBIDDEN_IN_TRANSACTION:{activity}")


__all__ = [
    "FORBIDDEN_IN_TRANSACTION",
    "TransactionBoundaryError",
    "assert_activity_allowed",
    "assert_short_transaction",
]
