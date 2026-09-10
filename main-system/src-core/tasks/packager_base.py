from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


MAIN_SYSTEM_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = MAIN_SYSTEM_ROOT.parent
for _packager_path in (
    str(PROJECT_ROOT),
    str(PROJECT_ROOT / "main-system" / "src-core"),
    str(PROJECT_ROOT / "shared-layer" / "src"),
):
    if _packager_path not in sys.path:
        sys.path.insert(0, _packager_path)

from governance_rule.execution.integrity.package_integrity import (  # noqa: E402
    PACKAGE_FORMAT_VERSION,
    PACKAGE_METADATA_NAME,
    SOURCE_IGNORED_DIRECTORY_NAMES,
    collect_file_hashes,
    declared_source_exclusions,
    load_package_metadata,
    snapshot_digest,
    verify_packaged_app,
)


PLATFORM_TOOLS_DIR = PROJECT_ROOT
ELECTRON_DIST_DIR = MAIN_SYSTEM_ROOT / "node_modules" / "electron" / "dist"
PLATFORM_RENDERER_ROOT = MAIN_SYSTEM_ROOT / "dist-ui" / "independent-tools"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "platform-tool-app"
TOOL_RUNTIME_CONTRACT_PATH = MAIN_SYSTEM_ROOT / "config" / "tool-runtime-contract.json"
DEFAULT_BACKEND_PORT = 8765
STANDALONE_BACKEND_PORT_MIN = 20000
STANDALONE_BACKEND_PORT_COUNT = 20000
MAX_COMPLETED_RECOVERY_GENERATIONS = 1
TOOL_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def tool_display_version(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else ""


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def load_tool_runtime_contract() -> dict[str, int]:
    try:
        payload = json.loads(TOOL_RUNTIME_CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Tool runtime contract is unavailable") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Tool runtime contract must be an object")
    values: dict[str, int] = {}
    for key in (
        "contract_version",
        "protocol_version",
        "minimum_supported_contract_version",
    ):
        value = payload.get(key)
        if type(value) is not int or value < 1:
            raise RuntimeError(f"Tool runtime contract field is invalid: {key}")
        values[key] = value
    if values["minimum_supported_contract_version"] > values["contract_version"]:
        raise RuntimeError("Tool runtime contract compatibility range is invalid")
    return values


class PromotionRecoveryRequired(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        recovery_root: Path,
        rollback_errors: list[str],
    ) -> None:
        super().__init__(
            f"{message} Recovery data was preserved at: {recovery_root}"
        )
        self.recovery_root = recovery_root
        self.rollback_errors = tuple(rollback_errors)


class PackageOperationBusy(RuntimeError):
    pass


def run_upgrade_auto_repair(
    project_root: Path = PROJECT_ROOT,
    *,
    service_factory: type | None = None,
) -> dict[str, Any]:
    """Return an owner-tool request instead of importing tool business code."""

    return {
        "ok": False,
        "error_code": "TOOL_EXECUTION_REQUEST_REQUIRED",
        "message": "Global Cleaner must execute this request under its own identity",
        "request": {
            "command": "toolbox_request_tool_execution",
            "tool_id": "global-cleaner",
            "args": ["--repair-anomalies", "--scope", "global", "--json"],
            "project_boundary": str(project_root.resolve()),
        },
    }
