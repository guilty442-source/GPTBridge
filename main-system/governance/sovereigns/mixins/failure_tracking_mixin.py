"""Sovereign Failure Tracking Mixin — child failure counting shared by all sovereign parents."""

from __future__ import annotations


class FailureTrackingBase:
    """Mixin providing child failure tracking shared by all sovereign parents."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-child consecutive-failure counts, fed by the governed
        # executor and by children reporting through ``report_to_parent``.
        self._child_failure_counts: dict[str, int] = {}

    def record_child_failure(self, child_id: str) -> int:
        """Record a consecutive child failure; returns the new count."""
        count = self._child_failure_counts.get(child_id, 0) + 1
        self._child_failure_counts[child_id] = count
        return count

    def record_child_success(self, child_id: str) -> None:
        """Clear the consecutive-failure counter after a child recovers."""
        self._child_failure_counts.pop(child_id, None)

    def child_failure_count(self, child_id: str) -> int:
        return self._child_failure_counts.get(child_id, 0)


__all__ = ["FailureTrackingBase"]