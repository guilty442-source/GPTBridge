"""G48: contract-axis coverage audit — fail-closed.

Every mandated contract axis must have a ``*-contract.json`` under
``main-system/config`` with a valid ``contract_version`` (int >= 1) and
``minimum_supported_contract_version <= contract_version``. A missing or
malformed axis is an audit error — version-axis compatibility is verified,
never assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_AXES: tuple[str, ...] = (
    "ai-connection",
    "backend-lifecycle",
    "data-architecture",
    "ipc",
    "sql-schema",
    "tool-runtime",
)


def check_contract_axes(root: Path, errors: list[str]) -> None:
    config_dir = root / "main-system" / "config"
    for axis in REQUIRED_AXES:
        path = config_dir / f"{axis}-contract.json"
        if not path.is_file():
            errors.append(f"contract-axis-missing:{axis}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"contract-axis-unreadable:{axis}:{exc.__class__.__name__}")
            continue
        version = data.get("contract_version")
        if version is None:
            schema = data.get("schema")
            if isinstance(schema, str) and "/v" in schema:
                try:
                    version = int(schema.rsplit("/v", 1)[1])
                except (ValueError, IndexError):
                    version = None
        if not isinstance(version, int) or version < 1:
            errors.append(f"contract-axis-version-invalid:{axis}")
            continue
        minimum = data.get("minimum_supported_contract_version")
        if minimum is not None and (not isinstance(minimum, int) or minimum < 0):
            errors.append(f"contract-axis-minimum-invalid:{axis}")
        elif isinstance(minimum, int) and minimum > version:
            errors.append(f"contract-axis-minimum-exceeds-version:{axis}")


__all__ = ["check_contract_axes", "REQUIRED_AXES"]
