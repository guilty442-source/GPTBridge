"""Active release status — A181/E156 observability.

Per A181 (certified-hot-update-persistence-and-reset-prevention), the
active release status is published for observability and UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from core_system.active_release_persistence import (
    ACTIVE_POINTER_PATH,
    resolve_active_pointer,
)
from core_system.active_release_ledger import (
    ACTIVATION_LEDGER_PATH,
    read_activation_ledger,
)


def active_release_status(
    *,
    pointer_path: Path = ACTIVE_POINTER_PATH,
    ledger_path: Path = ACTIVATION_LEDGER_PATH,
) -> dict[str, Any]:
    """Return the active release status for observability and UI (A181).

    Per A181: ``USER-NOTICE:active-version+operation+preserved-or-rollback-
    status`` and ``OBSERVABILITY:uptime+restart-count+...+release-id``.
    """
    pointer = resolve_active_pointer(pointer_path=pointer_path)
    ledger = read_activation_ledger(ledger_path=ledger_path, limit=10)
    last_entry = ledger[-1] if ledger else None
    return {
        "has_active_release": pointer is not None,
        "release_id": pointer.release_id if pointer else "",
        "application_version": pointer.application_version if pointer else "",
        "activation_generation": pointer.activation_generation if pointer else "",
        "certificate_digest": pointer.certificate_digest if pointer else "",
        "activated_at": pointer.activated_at if pointer else "",
        "last_ledger_operation": last_entry.operation if last_entry else "",
        "last_ledger_recorded_at": last_entry.recorded_at if last_entry else "",
        "ledger_entries": len(ledger),
        "authority": "permission-sovereign",
        "basis": "A181/E156",
    }


__all__ = [
    "active_release_status",
]
