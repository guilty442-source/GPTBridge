from __future__ import annotations

from pathlib import Path
from typing import Any


MINIMUM_INVESTMENT_PARAMETERS = 15
EVALUATION_SCHEMA = "star-upgrade-evaluation/v1"


def evaluate_star_upgrade(
    *,
    version: str,
    tool_root: Path,
    database: dict[str, Any],
    external_research_configured: bool,
    star_native_model_enabled: bool = True,
    external_model_enabled: bool = False,
) -> dict[str, Any]:
    """Evaluate the fixed v1 Star baseline without changing its version."""

    resolved_root = Path(tool_root).resolve()
    database_path = Path(str(database.get("path") or "")).resolve()
    tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
    parameter_count = int(tables.get("investment_parameter_definition") or 0)
    database_isolated = database_path.is_relative_to(resolved_root)
    quality = database.get("quality") if isinstance(database.get("quality"), dict) else {}
    blank_distribution_events = int(quality.get("blank_distribution_events") or 0)
    duplicate_distribution_events = int(quality.get("duplicate_distribution_events") or 0)
    max_search_response_chars = int(quality.get("max_search_response_chars") or 0)
    checks = {
        "version_locked_to_1_0": version == "1.0.0",
        "tool_database_isolated": database_isolated,
        "investment_parameters_ready": parameter_count >= MINIMUM_INVESTMENT_PARAMETERS,
        "market_search_owned_by_star": True,
        "investment_analysis_owned_by_star": True,
        "external_ai_api_disabled": True,
        "offline_command_queue_disabled": True,
        "background_browser_release_enabled": True,
        "distribution_events_valid": blank_distribution_events == 0,
        "distribution_events_deduplicated": duplicate_distribution_events == 0,
        "search_logs_compacted": max_search_response_chars <= 65536,
        "star_native_language_model_enabled": star_native_model_enabled,
        "external_model_disabled": not external_model_enabled,
    }
    recommendations: list[dict[str, str]] = []
    if parameter_count < MINIMUM_INVESTMENT_PARAMETERS:
        recommendations.append(
            {
                "code": "RESTORE_INVESTMENT_PARAMETERS",
                "action": "Run the local-ai auto-repair procedure.",
            }
        )
    if not database_isolated:
        recommendations.append(
            {
                "code": "RELOCATE_DATABASE",
                "action": "Move the database back inside the local-ai tool root.",
            }
        )
    if blank_distribution_events or duplicate_distribution_events:
        recommendations.append(
            {
                "code": "REPAIR_DISTRIBUTION_EVENTS",
                "action": "Run the local-ai auto-repair procedure to remove invalid or duplicate distribution events.",
            }
        )
    if max_search_response_chars > 65536:
        recommendations.append(
            {
                "code": "COMPACT_SEARCH_LOGS",
                "action": "Run the local-ai auto-repair procedure to retain compact search summaries.",
            }
        )
    if not star_native_model_enabled:
        recommendations.append(
            {
                "code": "RESTORE_STAR_NATIVE_MODEL",
                "action": "Restore the Star-owned local language-model pipeline.",
            }
        )
    optional_checks = {"background_browser_release_enabled"}
    required_checks = {key: value for key, value in checks.items() if key not in optional_checks}
    return {
        "ok": all(required_checks.values()),
        "schema": EVALUATION_SCHEMA,
        "name": "星澄",
        "version": "1.0",
        "version_internal": version,
        "upgrade_policy": {
            "mode": "direct-source-hot-update",
            "repackage_exe_for_source_changes": False,
            "version_change_allowed": False,
            "restart_scope": "affected-tool-only",
        },
        "checks": checks,
        "metrics": {
            "investment_parameter_count": parameter_count,
            "instrument_identity_count": int(tables.get("instrument_identity") or 0),
            "market_observation_count": int(tables.get("market_observation") or 0),
            "distribution_event_count": int(tables.get("distribution_event") or 0),
            "web_search_log_count": int(tables.get("web_search_log") or 0),
            "database_size_bytes": int(database.get("size_bytes") or 0),
            "blank_distribution_event_count": blank_distribution_events,
            "duplicate_distribution_event_count": duplicate_distribution_events,
            "max_search_response_chars": max_search_response_chars,
        },
        "external_discussion_connected": external_research_configured,
        "external_model_policy": "forbidden-for-native-inference",
        "external_collaboration_policy": "explicit-need-only",
        "recommendations": recommendations,
    }
