"""Active certified release — A181/E156 and A182/E157 facade.

This module re-exports the active release types, persistence, and
verification functions from the split submodules.  It preserves backward
compatibility for existing imports.

Submodules:
  * ``active_release_types`` — data structures (ActiveReleasePointer,
    ActivationLedgerEntry, VersionMismatch, constants).
  * ``active_release_persistence`` — durable transactional operations
    (publish, resolve, record, read).
  * ``active_release_verify`` — verification, status, and mismatch
    classification (verify_active_release, frontend_backend_release_match,
    active_release_status, classify_version_mismatch,
    version_mismatch_signal).
"""

from __future__ import annotations

from core_system.active_release_persistence import (
    ACTIVE_POINTER_PATH,
    publish_active_pointer,
    resolve_active_pointer,
)
from core_system.active_release_ledger import (
    ACTIVATION_LEDGER_PATH,
    read_activation_ledger,
    record_activation,
)
from core_system.active_release_types import (
    MISMATCH_CLASSIFICATIONS,
    VERSION_NAMESPACES,
    ActivationLedgerEntry,
    ActiveReleasePointer,
    VersionMismatch,
)
from core_system.active_release_verify import (
    frontend_backend_release_match,
    verify_active_release,
)
from core_system.active_release_mismatch import (
    classify_version_mismatch,
    version_mismatch_signal,
)
from core_system.active_release_status import active_release_status

__all__ = [
    "active_release_verify",
    "active_release_types",
    "active_release_persistence",
]
