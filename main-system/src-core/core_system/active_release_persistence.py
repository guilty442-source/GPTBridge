"""Active release pointer persistence — A181/E156 durable transactional ops.

Per A181 (certified-hot-update-persistence-and-reset-prevention) and E156
(hot-update-persistence), the active certified release pointer is durable,
transactional, restart-safe, and crash-safe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from core_system.active_release_types import ActiveReleasePointer

_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ACTIVE_POINTER_PATH: Final[Path] = (
    _DEFAULT_PROJECT_ROOT / "runtime" / "state"
    / "active-release-pointer.json"
)

# Public alias
ACTIVE_POINTER_PATH: Final[Path] = _ACTIVE_POINTER_PATH


def _ensure_state_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so the pointer is crash-safe."""
    _ensure_state_dir(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def publish_active_pointer(
    pointer: ActiveReleasePointer,
    *,
    pointer_path: Path = _ACTIVE_POINTER_PATH,
) -> None:
    """Atomically write the active release pointer (A181: PUBLISH)."""
    _atomic_write_json(pointer_path, pointer.as_dict())


def resolve_active_pointer(
    *,
    pointer_path: Path = _ACTIVE_POINTER_PATH,
) -> ActiveReleasePointer | None:
    """Read the active release pointer before startup/repair/reload/sync.

    Per A181: ``PERSISTENCE:durable+transactional+restart-safe+crash-safe+
    read-before-startup/repair/reload/reconnect/synchronization``.
    """
    if not pointer_path.is_file():
        return None
    try:
        data = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("pointer_format") != 1:
        return None
    required = (
        "release_id", "application_version", "artifact_root",
        "contract_root", "certificate_digest", "activation_generation",
        "activated_at",
    )
    if not all(key in data for key in required):
        return None
    return ActiveReleasePointer(**{key: data[key] for key in required})


__all__ = [
    "ACTIVE_POINTER_PATH",
    "publish_active_pointer",
    "resolve_active_pointer",
]
