"""A191/A192 — Startup manifest configuration loader.

Reads ``main-system/config/startup_manifest.json`` and exposes typed
accessors for every constant that was previously hardcoded in
``startup_core/phases.py``, ``boot_core.py``, and
``core_system/governed_startup_types.py``.

The loader is importable before ``_ensure_runtime_paths()`` installs the
workspace paths because it resolves the config file relative to its own
``__file__`` location (``main-system/src-core/startup_core/startup_config.py``
→ ``main-system/config/startup_manifest.json``).

If the config file is missing or invalid, the loader falls back to the
certified defaults baked into this module so boot never blocks on a
corrupted config.  The fallback values mirror the original hardcoded
constants exactly.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

# ------------------------------------------------------------------
# Config file resolution
# ------------------------------------------------------------------

_CONFIG_RELATIVE: Final[tuple[str, ...]] = (
    "..", "..", "config", "startup_manifest.json",
)


def _config_path() -> Path:
    return Path(__file__).resolve().parents[0].joinpath(*_CONFIG_RELATIVE)


# ------------------------------------------------------------------
# Certified fallback defaults (mirror the original hardcoded values)
# ------------------------------------------------------------------

_FALLBACK: Final[dict[str, Any]] = {
    "bootstrap_phases": (
        "environment-check",
        "governance-audit",
    ),
    "dependency_manifest": (
        {
            "identity": "postgresql",
            "owner": "data-runtime-sovereign",
            "required_by": "main-system-authoritative-state",
            "criticality": "core-critical",
            "readiness_contract": "tcp-or-dsn-select-1",
            "deadline": "3s",
            "retry_budget": 2,
            "shutdown_order": 30,
        },
        {
            "identity": "qdrant",
            "owner": "rag-runtime-sovereign",
            "required_by": "rag-semantic-retrieval",
            "criticality": "capability-critical",
            "readiness_contract": "loopback-tcp-6333",
            "deadline": "3s",
            "retry_budget": 1,
            "shutdown_order": 20,
        },
        {
            "identity": "ollama",
            "owner": "model-runtime-sovereign",
            "required_by": "local-model-inference",
            "criticality": "capability-critical",
            "readiness_contract": "loopback-tcp-11434",
            "deadline": "3s",
            "retry_budget": 1,
            "shutdown_order": 10,
        },
    ),
    "probe_constants": {
        "ollama_probe_timeout": 0.5,
        "postgres_connect_timeout": 1.0,
        "postgres_probe_attempts": 3,
        "postgres_probe_delay": 0.5,
        "qdrant_probe_timeout": 0.5,
        "startup_gate_deadline_seconds": 8.0,
    },
    "ports": {
        "ollama": 11434,
        "postgresql": 5432,
        "qdrant": 6333,
        "health_probe": 8765,
    },
    "supervisor": {
        "max_restarts": 10,
        "backoff_schedule_seconds": (2, 5, 10, 20, 30, 45, 60),
        "healthy_uptime_reset_seconds": 60,
        "health_probe_timeout": 2.0,
        "health_probe_interval": 5.0,
        "startup_health_probe_interval": 0.5,
        "crash_repair_uptime_threshold": 30.0,
    },
    "governed_startup": {
        "startup_phases": (
            "phase-0-local-preflight",
            "phase-1-minimal-information-bootstrap",
            "phase-2-read-official-codex",
            "phase-3-load-permission-directory",
            "phase-4-switch-normal-information-mode",
            "phase-5-classify-dependency-dag",
            "phase-6-activate-core-sovereigns",
        ),
        "core_ready_conditions": (
            "official-codex-valid",
            "permission-sovereign-active",
            "normal-information-layer-active",
            "system-decision-active",
            "system-runtime-active",
            "maintenance-active",
            "all-core-critical-dependencies-ready",
        ),
        "bootstrap_capability_properties": (
            "pre-issued",
            "read-only",
            "startup-sovereign-bound",
            "official-codex-entry-only",
            "sealed-in-active-release",
            "verified-mechanically-without-live-permission-decision",
        ),
        "dependency_criticality_classes": (
            "core-critical",
            "capability-critical",
            "optional",
        ),
        "no_fixed_criticality_services": (
            "postgresql",
            "qdrant",
            "ollama",
        ),
    },
}


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Any]:
    """Load and cache the startup manifest JSON.

    Falls back to certified defaults if the file is missing or invalid.
    """
    path = _config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _deep_copy_fallback()
    return _normalize(raw)


def _deep_copy_fallback() -> dict[str, Any]:
    """Return a deep-enough copy of the fallback dict."""
    return {
        "bootstrap_phases": tuple(_FALLBACK["bootstrap_phases"]),
        "dependency_manifest": tuple(dict(d) for d in _FALLBACK["dependency_manifest"]),
        "probe_constants": dict(_FALLBACK["probe_constants"]),
        "ports": dict(_FALLBACK["ports"]),
        "supervisor": {**_FALLBACK["supervisor"],
                       "backoff_schedule_seconds": tuple(_FALLBACK["supervisor"]["backoff_schedule_seconds"])},
        "governed_startup": {
            k: tuple(v) if isinstance(v, tuple) else v
            for k, v in _FALLBACK["governed_startup"].items()
        },
    }


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize JSON lists into tuples where the consumers expect tuples."""
    return {
        "bootstrap_phases": tuple(raw.get("bootstrap_phases", _FALLBACK["bootstrap_phases"])),
        "dependency_manifest": tuple(
            dict(d) for d in raw.get("dependency_manifest", _FALLBACK["dependency_manifest"])
        ),
        "probe_constants": raw.get("probe_constants", dict(_FALLBACK["probe_constants"])),
        "ports": raw.get("ports", dict(_FALLBACK["ports"])),
        "supervisor": {
            **_FALLBACK["supervisor"],
            **raw.get("supervisor", {}),
            "backoff_schedule_seconds": tuple(
                raw.get("supervisor", {}).get(
                    "backoff_schedule_seconds",
                    _FALLBACK["supervisor"]["backoff_schedule_seconds"],
                )
            ),
        },
        "governed_startup": {
            k: tuple(v) if isinstance(v, list) else v
            for k, v in {
                **_FALLBACK["governed_startup"],
                **raw.get("governed_startup", {}),
            }.items()
        },
    }


# ------------------------------------------------------------------
# Typed accessors
# ------------------------------------------------------------------

def bootstrap_phases() -> tuple[str, ...]:
    return load_manifest()["bootstrap_phases"]


def dependency_manifest() -> tuple[dict[str, Any], ...]:
    return load_manifest()["dependency_manifest"]


def probe_constant(name: str) -> Any:
    return load_manifest()["probe_constants"][name]


def port(name: str) -> int:
    return load_manifest()["ports"][name]


def supervisor_constant(name: str) -> Any:
    return load_manifest()["supervisor"][name]


def governed_startup_constant(name: str) -> Any:
    return load_manifest()["governed_startup"][name]
