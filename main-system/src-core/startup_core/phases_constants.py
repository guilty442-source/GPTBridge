"""Startup phase constants (A185 split).

Kept here to avoid circular imports between phases.py and its mixin.
"""
from __future__ import annotations

from typing import Any, Final

from startup_core.startup_config import (
    bootstrap_phases as _cfg_bootstrap_phases,
    dependency_manifest as _cfg_dependency_manifest,
    port as _cfg_port,
    probe_constant as _cfg_probe,
)

# ------------------------------------------------------------------
# Startup phase constants — loaded from config/startup_manifest.json
# (A191/A192: no longer hardcoded; editable without source changes)
# ------------------------------------------------------------------

OLLAMA_PROBE_TIMEOUT: Final[float] = _cfg_probe("ollama_probe_timeout")
POSTGRES_CONNECT_TIMEOUT: Final[float] = _cfg_probe("postgres_connect_timeout")
POSTGRES_PROBE_ATTEMPTS: Final[int] = _cfg_probe("postgres_probe_attempts")
POSTGRES_PROBE_DELAY: Final[float] = _cfg_probe("postgres_probe_delay")
QDRANT_PROBE_TIMEOUT: Final[float] = _cfg_probe("qdrant_probe_timeout")
STARTUP_GATE_DEADLINE_SECONDS: Final[float] = _cfg_probe("startup_gate_deadline_seconds")

BOOTSTRAP_PHASES: Final[tuple[str, ...]] = _cfg_bootstrap_phases()

# Current certified dependency contracts. Criticality is declared by the
# consuming contract, never inferred from a service name.
#
# Loaded from ``config/startup_manifest.json`` so new dependencies can be
# added without source-code changes.  The declarations are materialized
# into ``DependencyDeclaration`` objects at runtime inside
# ``_run_startup_phases``.
DEPENDENCY_MANIFEST: Final[tuple[dict[str, Any], ...]] = _cfg_dependency_manifest()

# Port constants — loaded from config (A191/A192)
OLLAMA_PORT: Final[int] = _cfg_port("ollama")
POSTGRESQL_PORT: Final[int] = _cfg_port("postgresql")
QDRANT_PORT: Final[int] = _cfg_port("qdrant")


__all__ = [
    "OLLAMA_PROBE_TIMEOUT",
    "POSTGRES_CONNECT_TIMEOUT",
    "POSTGRES_PROBE_ATTEMPTS",
    "POSTGRES_PROBE_DELAY",
    "QDRANT_PROBE_TIMEOUT",
    "STARTUP_GATE_DEADLINE_SECONDS",
    "BOOTSTRAP_PHASES",
    "DEPENDENCY_MANIFEST",
    "OLLAMA_PORT",
    "POSTGRESQL_PORT",
    "QDRANT_PORT",
]
