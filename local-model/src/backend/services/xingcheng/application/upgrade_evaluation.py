from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MINIMUM_INVESTMENT_PARAMETERS = 15
MINIMUM_INVESTMENT_MODELS = 8
MINIMUM_MATHEMATICAL_CAPABILITIES = 16
EVALUATION_SCHEMA = "star-upgrade-evaluation/v1"


def _tables(database: dict[str, Any]) -> dict[str, Any]:
    value = database.get("tables")
    return value if isinstance(value, dict) else {}


def _quality(database: dict[str, Any]) -> dict[str, Any]:
    value = database.get("quality")
    return value if isinstance(value, dict) else {}


def evaluate_star_upgrade(
    *,
    version: str,
    tool_root: Path,
    database: dict[str, Any],
    external_research_configured: bool,
    star_native_model_enabled: bool = True,
    local_transformer_enabled: bool = False,
    remote_model_enabled: bool = False,
    databases: dict[str, dict[str, Any]] | None = None,
    registered_analysis_models: list[str] | tuple[str, ...] | None = None,
    executable_analysis_models: list[str] | tuple[str, ...] | None = None,
    mathematical_capability_count: int = 0,
    external_research_health: dict[str, Any] | None = None,
    memory_status: dict[str, Any] | None = None,
    runtime_metrics: dict[str, Any] | None = None,
    market_source_count: int = 0,
    official_market_source_count: int = 0,
    coding_capability_enabled: bool = True,
    self_maintenance_enabled: bool = True,
    capability_evaluation: dict[str, Any] | None = None,
    transformer_training_database: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate Star v1 from live storage, routing and runtime evidence."""

    resolved_root = Path(tool_root).resolve()
    all_databases = dict(databases or {"investment-specialist": database})
    if not all_databases:
        all_databases = {"investment-specialist": database}
    isolated_paths: list[str] = []
    databases_isolated = True
    sqlite_integrity_ok = True
    latest_market_observation_at = ""
    total_search_successes = 0
    total_search_failures = 0
    total_inference_audits = 0
    total_parameter_adjustments = 0
    for item in all_databases.values():
        try:
            path = Path(str(item.get("path") or "")).resolve()
            databases_isolated = databases_isolated and path.is_relative_to(resolved_root)
            isolated_paths.append(str(path))
        except (OSError, ValueError):
            databases_isolated = False
        sqlite_integrity_ok = sqlite_integrity_ok and (
            str(_quality(item).get("sqlite_integrity") or "ok").casefold() == "ok"
        )
        item_quality = _quality(item)
        observed_at = str(item_quality.get("latest_market_observation_at") or "")
        latest_market_observation_at = max(latest_market_observation_at, observed_at)
        total_search_successes += int(item_quality.get("search_success_count") or 0)
        total_search_failures += int(item_quality.get("search_failure_count") or 0)
        total_inference_audits += int(_tables(item).get("inference_log") or 0)
        total_parameter_adjustments += int(
            _tables(item).get("investment_parameter_adjustment") or 0
        )
    databases_isolated = databases_isolated and len(set(isolated_paths)) == len(isolated_paths)

    tables = _tables(database)
    quality = _quality(database)
    parameter_count = int(tables.get("investment_parameter_definition") or 0)
    model_count = int(tables.get("investment_model_definition") or 0)
    blank_distribution_events = int(quality.get("blank_distribution_events") or 0)
    duplicate_distribution_events = int(quality.get("duplicate_distribution_events") or 0)
    max_search_response_chars = int(quality.get("max_search_response_chars") or 0)
    registered = {str(item) for item in (registered_analysis_models or []) if item}
    executable = {str(item) for item in (executable_analysis_models or []) if item}
    model_coverage = registered <= executable if registered else model_count >= MINIMUM_INVESTMENT_MODELS
    external_health = dict(external_research_health or {})
    memory = dict(memory_status or {})
    metrics = dict(runtime_metrics or {})
    capability_provided = capability_evaluation is not None
    capability = dict(capability_evaluation or {})
    training_database_provided = transformer_training_database is not None
    training_database = dict(transformer_training_database or {})
    search_requests = int(metrics.get("search_request_count") or 0)
    search_cache_hits = int(metrics.get("search_cache_hit_count") or 0)
    inference_requests = int(metrics.get("inference_request_count") or 0)
    analysis_requests = int(metrics.get("analysis_request_count") or 0)
    market_data_age_hours: float | None = None
    if latest_market_observation_at:
        try:
            observed = datetime.fromisoformat(
                latest_market_observation_at.replace("Z", "+00:00")
            )
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            market_data_age_hours = max(
                0.0,
                (datetime.now(timezone.utc) - observed).total_seconds() / 3600,
            )
        except ValueError:
            market_data_age_hours = None

    checks = {
        "version_locked_to_1_0": version == "1.0.0",
        "tool_databases_isolated": databases_isolated,
        "sqlite_integrity_verified": sqlite_integrity_ok,
        "investment_parameters_ready": parameter_count >= MINIMUM_INVESTMENT_PARAMETERS,
        "registered_investment_models_executable": model_coverage,
        "mathematical_capabilities_ready": mathematical_capability_count
        >= MINIMUM_MATHEMATICAL_CAPABILITIES,
        "market_sources_runtime_registered": market_source_count > 0,
        "official_market_sources_available": official_market_source_count > 0,
        "source_success_telemetry_available": all(
            "search_success_count" in _quality(item)
            and "search_failure_count" in _quality(item)
            for item in all_databases.values()
        ),
        "market_freshness_observable": all(
            "latest_market_observation_at" in _quality(item)
            for item in all_databases.values()
        ),
        "inference_audit_trail_available": all(
            "inference_log" in _tables(item) for item in all_databases.values()
        ),
        "runtime_route_and_latency_telemetry_available": {
            "model_route_fallback_count",
            "error_count",
            "inference_last_latency_ms",
            "analysis_last_latency_ms",
            "search_last_latency_ms",
        }
        <= set(metrics),
        "external_collaboration_fail_closed": (
            external_health.get("uses_api", False) is False
            and external_health.get("queue_when_offline", False) is False
            and external_health.get("transport", "governance-authenticated-ai-channel")
            == "governance-authenticated-ai-channel"
        ),
        "memory_external_review_enforced": memory.get("review_required_for_external", True)
        is True,
        "memory_expiry_enforced": memory.get("expiry_enforced", True) is True,
        "external_ai_api_disabled": not bool(external_health.get("uses_api", False)),
        "offline_command_queue_disabled": not bool(external_health.get("queue_when_offline", False)),
        "distribution_events_valid": blank_distribution_events == 0,
        "distribution_events_deduplicated": duplicate_distribution_events == 0,
        "search_logs_compacted": max_search_response_chars <= 65_536,
        "star_native_language_model_enabled": star_native_model_enabled,
        "local_transformer_or_native_model_enabled": bool(
            local_transformer_enabled or star_native_model_enabled
        ),
        "remote_model_disabled": not remote_model_enabled,
        "external_model_disabled": not remote_model_enabled,
        "coding_capability_enabled": coding_capability_enabled,
        "bounded_self_maintenance_enabled": self_maintenance_enabled,
        "held_out_capability_evaluation_passed": (
            capability.get("ok") is True if capability_provided else True
        ),
        "transformer_training_database_healthy": (
            training_database.get("ok") is True
            if training_database_provided
            else True
        ),
        "transformer_base_weights_immutable": (
            training_database.get("base_weights_immutable") is True
            and training_database.get("automatic_weight_replacement") is False
            if training_database_provided
            else True
        ),
    }
    recommendations: list[dict[str, str]] = []
    if parameter_count < MINIMUM_INVESTMENT_PARAMETERS:
        recommendations.append(
            {
                "code": "RESTORE_INVESTMENT_PARAMETERS",
                "severity": "required",
                "action": "Request the centralized System Rescue repair procedure for xingcheng through governance.",
            }
        )
    if not model_coverage:
        recommendations.append(
            {
                "code": "RESTORE_ANALYSIS_MODEL_EXECUTION",
                "severity": "required",
                "action": "Restore executable coverage for every registered Star investment model.",
            }
        )
    if not databases_isolated or not sqlite_integrity_ok:
        recommendations.append(
            {
                "code": "REPAIR_ISOLATED_DATABASES",
                "severity": "required",
                "action": "Request governed System Rescue diagnosis for the affected Star model database.",
            }
        )
    if blank_distribution_events or duplicate_distribution_events:
        recommendations.append(
            {
                "code": "REPAIR_DISTRIBUTION_EVENTS",
                "severity": "required",
                "action": "Request centralized System Rescue diagnosis; business records are not rewritten automatically.",
            }
        )
    if max_search_response_chars > 65_536:
        recommendations.append(
            {
                "code": "COMPACT_SEARCH_LOGS",
                "severity": "required",
                "action": "Run the governed xingcheng data compaction procedure.",
            }
        )
    if not external_research_configured:
        recommendations.append(
            {
                "code": "CONNECT_OPTIONAL_AI_COLLABORATION",
                "severity": "optional",
                "action": "Start Star through the governed shared-layer channel when external final coordination is needed.",
            }
        )

    required_checks = dict(checks)
    return {
        "ok": all(required_checks.values()),
        "schema": EVALUATION_SCHEMA,
        "name": "星澄",
        "version": "1.0",
        "version_internal": version,
        "upgrade_policy": {
            "mode": "self-authored-governance-versioned-release",
            "repackage_exe_for_source_changes": False,
            "version_change_allowed": False,
            "restart_scope": "affected-tool-only",
            "runtime_source_write": False,
            "publish_authority": "governance-versioned-release-only",
        },
        "checks": checks,
        "metrics": {
            "investment_parameter_count": parameter_count,
            "registered_investment_model_count": model_count,
            "executable_investment_model_count": len(executable),
            "mathematical_capability_count": mathematical_capability_count,
            "market_source_count": market_source_count,
            "official_market_source_count": official_market_source_count,
            "database_count": len(all_databases),
            "database_size_bytes": sum(int(item.get("size_bytes") or 0) for item in all_databases.values()),
            "blank_distribution_event_count": blank_distribution_events,
            "duplicate_distribution_event_count": duplicate_distribution_events,
            "max_search_response_chars": max_search_response_chars,
            "runtime": metrics,
            "runtime_derived": {
                "search_cache_hit_rate_percent": round(
                    search_cache_hits / search_requests * 100, 4
                )
                if search_requests
                else None,
                "average_inference_latency_ms": round(
                    float(metrics.get("inference_latency_ms_total") or 0)
                    / inference_requests,
                    3,
                )
                if inference_requests
                else None,
                "average_analysis_latency_ms": round(
                    float(metrics.get("analysis_latency_ms_total") or 0)
                    / analysis_requests,
                    3,
                )
                if analysis_requests
                else None,
            },
            "source_success_count": total_search_successes,
            "source_failure_count": total_search_failures,
            "source_success_rate_percent": round(
                total_search_successes
                / (total_search_successes + total_search_failures)
                * 100,
                4,
            )
            if total_search_successes + total_search_failures
            else None,
            "latest_market_observation_at": latest_market_observation_at,
            "market_data_age_hours": round(market_data_age_hours, 3)
            if market_data_age_hours is not None
            else None,
            "inference_audit_record_count": total_inference_audits,
            "parameter_adjustment_count": total_parameter_adjustments,
        },
        "external_discussion_connected": external_research_configured,
        "capability_evaluation": capability,
        "external_research_health": external_health,
        "memory_health": memory,
        "transformer_training_database": training_database,
        "model_runtime_policy": "local-transformer-loopback-with-native-fallback",
        "remote_model_policy": "forbidden-for-star-inference",
        "external_collaboration_policy": "explicit-need-only",
        "recommendations": recommendations,
    }
