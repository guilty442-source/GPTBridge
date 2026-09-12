from __future__ import annotations

import json
from pathlib import Path


def _investment_app_version() -> str:
    try:
        from core_system.versioning import component_version

        return component_version("ai-assistant")
    except Exception:
        pass
    manifest_path = Path(__file__).resolve().parents[5] / "manifest.json"
    try:
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if version:
            return version
    except Exception:
        pass
    return "1.0.0"


# Application releases and persisted-data schemas intentionally evolve
# independently.  Keeping them separate prevents a UI/feature release from
# being mistaken for a destructive data migration.
INVESTMENT_APP_VERSION = _investment_app_version()
INVESTMENT_STATE_SCHEMA_VERSION = 2
INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION = 1
INVESTMENT_ANALYTICS_SCHEMA_VERSION = 5


def upgrade_compatibility_contract() -> dict[str, object]:
    return {
        "application_version": INVESTMENT_APP_VERSION,
        "state_schema": {
            "current": INVESTMENT_STATE_SCHEMA_VERSION,
            "minimum_supported": INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION,
        },
        "analytics_schema": {
            "current": INVESTMENT_ANALYTICS_SCHEMA_VERSION,
        },
        "migration_policy": "snapshot_then_atomic_migrate",
        "future_schema_policy": "fail_closed_without_overwrite",
    }
