"""Dynamic-First Configuration — A386/A389 Implementation.

A386: DEFAULT:DYNAMIC-FIRST. Every value that may vary by machine, environment, worktree,
module, generation, release, capacity or deployment is referenced by a typed canonical
code registry or governed environment binding.

A389: ONLY-HARDCODE-ALLOWANCE: the only literal runtime/deployment value permitted to be
hardcoded is a timestamp value created for an immutable event, revision or evidence record.

This module provides:
1. Typed canonical code registries for all configurable values
2. Governed environment bindings with validation
3. Timestamp-only hardcode allowance enforcement
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Callable

_logger = logging.getLogger("gptbridge.dynamic_config")


class ConfigCategory(Enum):
    """Categories of dynamic configuration values."""
    MACHINE = "machine"           # Varies by machine/host
    ENVIRONMENT = "environment"   # Varies by environment (dev/staging/prod)
    WORKTREE = "worktree"         # Varies by git worktree
    MODULE = "module"             # Varies by module
    GENERATION = "generation"     # Varies by runtime generation
    RELEASE = "release"           # Varies by release
    CAPACITY = "capacity"         # Varies by capacity constraints
    DEPLOYMENT = "deployment"     # Varies by deployment target


@dataclass(frozen=True)
class ConfigKey:
    """A386: Typed canonical code for a dynamic configuration value."""
    code: str
    category: ConfigCategory
    value_type: type
    description: str
    default: Any = None
    validator: Optional[Callable[[Any], bool]] = None
    source_registry: str = ""  # e.g., "canonical-code-registry", "governed-env-binding"
    required: bool = True


@dataclass
class ConfigValue:
    """A dynamic configuration value with metadata."""
    key: ConfigKey
    value: Any
    source: str  # "registry", "env-binding", "timestamp-hardcode"
    resolved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_details: dict[str, Any] = field(default_factory=dict)


class DynamicConfigRegistry:
    """A386: Typed canonical code registry for all dynamic configuration values.

    Every value that may vary is referenced by a typed canonical code
    from this registry or governed environment binding.
    """

    def __init__(self) -> None:
        self._keys: dict[str, ConfigKey] = {}
        self._values: dict[str, ConfigValue] = {}
        self._register_builtin_keys()

    def _register_builtin_keys(self) -> None:
        """Register built-in configuration keys from canonical registries."""
        self._register_machine_keys()
        self._register_environment_keys()
        self._register_worktree_keys()
        self._register_module_keys()
        self._register_generation_keys()
        self._register_release_keys()
        self._register_deployment_keys()

    def _register_machine_keys(self) -> None:
        """Machine-specific configuration keys."""
        self.register(ConfigKey(
            code="machine.id",
            category=ConfigCategory.MACHINE,
            value_type=str,
            description="Unique machine identifier",
            source_registry="canonical-machine-registry",
        ))
        self.register(ConfigKey(
            code="machine.capacity.cpu_cores",
            category=ConfigCategory.CAPACITY,
            value_type=int,
            description="Number of CPU cores available",
            source_registry="canonical-capacity-registry",
        ))
        self.register(ConfigKey(
            code="machine.capacity.memory_gb",
            category=ConfigCategory.CAPACITY,
            value_type=int,
            description="Available memory in GB",
            source_registry="canonical-capacity-registry",
        ))

    def _register_environment_keys(self) -> None:
        """Environment-specific configuration keys."""
        self.register(ConfigKey(
            code="environment.name",
            category=ConfigCategory.ENVIRONMENT,
            value_type=str,
            description="Environment name (development/staging/production)",
            default="development",
            source_registry="governed-env-binding",
        ))
        self.register(ConfigKey(
            code="environment.postgresql.dsn",
            category=ConfigCategory.ENVIRONMENT,
            value_type=str,
            description="PostgreSQL connection string",
            source_registry="governed-env-binding",
        ))
        self.register(ConfigKey(
            code="environment.qdrant.url",
            category=ConfigCategory.ENVIRONMENT,
            value_type=str,
            description="Qdrant connection URL",
            source_registry="governed-env-binding",
        ))

    def _register_worktree_keys(self) -> None:
        """Worktree-specific configuration keys."""
        self.register(ConfigKey(
            code="worktree.root",
            category=ConfigCategory.WORKTREE,
            value_type=str,
            description="Git worktree root path",
            source_registry="governed-env-binding",
        ))

    def _register_module_keys(self) -> None:
        """Module-specific configuration keys."""
        self.register(ConfigKey(
            code="module.interval.hot_reload",
            category=ConfigCategory.MODULE,
            value_type=float,
            description="Hot reload poll interval (seconds)",
            default=10.0,
            validator=lambda v: isinstance(v, (int, float)) and v > 0,
            source_registry="canonical-module-registry",
        ))
        self.register(ConfigKey(
            code="module.interval.connection_watchdog",
            category=ConfigCategory.MODULE,
            value_type=float,
            description="Connection watchdog probe interval (seconds)",
            default=15.0,
            validator=lambda v: isinstance(v, (int, float)) and v > 0,
            source_registry="canonical-module-registry",
        ))

    def _register_generation_keys(self) -> None:
        """Generation-specific configuration keys."""
        self.register(ConfigKey(
            code="generation.hot_update.interval",
            category=ConfigCategory.GENERATION,
            value_type=float,
            description="Hot update idle loop interval (seconds)",
            default=30.0,
            validator=lambda v: isinstance(v, (int, float)) and v > 0,
            source_registry="canonical-generation-registry",
        ))

    def _register_release_keys(self) -> None:
        """Release-specific configuration keys."""
        self.register(ConfigKey(
            code="release.hot_reload.poll_interval",
            category=ConfigCategory.RELEASE,
            value_type=float,
            description="Hot reload poll interval for release",
            default=10.0,
            validator=lambda v: isinstance(v, (int, float)) and v > 0,
            source_registry="canonical-release-registry",
        ))

    def _register_deployment_keys(self) -> None:
        """Deployment-specific configuration keys."""
        self.register(ConfigKey(
            code="deployment.postgresql.pool.max_connections",
            category=ConfigCategory.DEPLOYMENT,
            value_type=int,
            description="PostgreSQL connection pool max connections",
            default=20,
            validator=lambda v: isinstance(v, int) and v > 0,
            source_registry="canonical-deployment-registry",
        ))
        self.register(ConfigKey(
            code="deployment.qdrant.collection",
            category=ConfigCategory.DEPLOYMENT,
            value_type=str,
            description="Qdrant collection name",
            default="gptbridge_rag",
            source_registry="governed-env-binding",
        ))

    def register(self, key: ConfigKey) -> None:
        """Register a new configuration key."""
        if key.code in self._keys:
            raise ValueError(f"Config key already registered: {key.code}")
        self._keys[key.code] = key

    def get_key(self, code: str) -> Optional[ConfigKey]:
        """Get a configuration key by code."""
        return self._keys.get(code)

    def list_keys(self, category: Optional[ConfigCategory] = None) -> list[ConfigKey]:
        """List all registered keys, optionally filtered by category."""
        keys = list(self._keys.values())
        if category:
            keys = [k for k in keys if k.category == category]
        return keys

    def resolve_value(self, code: str, context: dict[str, Any] = None) -> ConfigValue:
        """A386: Resolve a value from registry or governed environment binding."""
        key = self._keys.get(code)
        if not key:
            raise KeyError(f"Unknown config code: {code}")

        context = context or {}

        # Try governed environment binding first
        env_value = self._resolve_from_env_binding(key, context)
        if env_value is not None:
            return ConfigValue(
                key=key,
                value=env_value,
                source="env-binding",
                source_details={"env_var": f"GPTBRIDGE_{code.upper().replace('.', '_')}"},
            )

        # Try canonical code registry
        registry_value = self._resolve_from_registry(key, context)
        if registry_value is not None:
            return ConfigValue(
                key=key,
                value=registry_value,
                source="registry",
                source_details={"registry": key.source_registry},
            )

        # Use default if available
        if key.default is not None:
            return ConfigValue(
                key=key,
                value=key.default,
                source="default",
                source_details={"default": True},
            )

        # Required but not found
        raise ValueError(f"Required config value not found: {code}")

    def _resolve_from_env_binding(self, key: ConfigKey, context: dict[str, Any]) -> Any:
        """Resolve value from governed environment binding."""
        env_var = f"GPTBRIDGE_{key.code.upper().replace('.', '_')}"
        if env_var in os.environ:
            raw = os.environ[env_var]
            return self._convert_value(raw, key.value_type)

        # Check context for override
        if key.code in context:
            return self._convert_value(context[key.code], key.value_type)

        return None

    def _resolve_from_registry(self, key: ConfigKey, context: dict[str, Any]) -> Any:
        """Resolve value from canonical code registry."""
        # In a full implementation, this would query a registry file/database
        # For now, check context for registry values
        registry_key = f"registry.{key.code}"
        if registry_key in context:
            return self._convert_value(context[registry_key], key.value_type)
        return None

    def _convert_value(self, raw: Any, target_type: type) -> Any:
        """Convert raw value to target type."""
        if isinstance(raw, target_type):
            return raw
        if target_type == bool:
            if isinstance(raw, str):
                return raw.lower() in ("true", "1", "yes", "on")
            return bool(raw)
        if target_type == int:
            return int(float(raw))
        if target_type == float:
            return float(raw)
        if target_type == str:
            return str(raw)
        if target_type == list:
            if isinstance(raw, str):
                return json.loads(raw)
            return list(raw)
        if target_type == dict:
            if isinstance(raw, str):
                return json.loads(raw)
            return dict(raw)
        return raw

    def set_value(self, code: str, value: Any, source: str = "manual") -> None:
        """Set a resolved value."""
        key = self._keys.get(code)
        if not key:
            raise KeyError(f"Unknown config code: {code}")

        # Validate if validator provided
        if key.validator and not key.validator(value):
            raise ValueError(f"Value {value} failed validation for {code}")

        self._values[code] = ConfigValue(
            key=key,
            value=value,
            source=source,
        )

    def get_value(self, code: str) -> Optional[Any]:
        """Get a previously resolved value."""
        cv = self._values.get(code)
        return cv.value if cv else None

    def validate_all(self, context: dict[str, Any] = None) -> list[str]:
        """Validate all registered keys can be resolved."""
        errors = []
        for key in self._keys.values():
            if key.required:
                try:
                    self.resolve_value(key.code, context)
                except Exception as exc:
                    errors.append(f"{key.code}: {exc}")
        return errors


class TimestampHardcodeValidator:
    """A389: Enforce ONLY-HARDCODE-ALLOWANCE for timestamps only."""

    # Patterns that are allowed to be hardcoded
    ALLOWED_TIMESTAMP_PATTERNS = [
        r'datetime\.now\(timezone\.utc\)\.isoformat\(\)',
        r'datetime\.utcnow\(\)\.isoformat\(\)',
        r'time\.time\(\)',
        r'time\.time_ns\(\)',
        r'datetime\.now\(\)\.timestamp\(\)',
    ]

    # Patterns that are NOT allowed to be hardcoded
    FORBIDDEN_HARDCODE_PATTERNS = [
        (r'["\']\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', 'ISO timestamp literal'),
        (r'["\']\d{10,}', 'Unix timestamp literal'),
        (r'\b\d{1,2}/\d{1,2}/\d{4}\b', 'Date literal'),
        (r'port\s*=\s*\d+', 'Port number'),
        (r'host\s*=\s*["\'][^"\']+["\']', 'Host literal'),
        (r'url\s*=\s*["\'][^"\']+["\']', 'URL literal'),
        (r'timeout\s*=\s*\d+', 'Timeout value'),
        (r'interval\s*=\s*\d+', 'Interval value'),
        (r'max_connections\s*=\s*\d+', 'Pool size'),
        (r'chunk_size\s*=\s*\d+', 'Chunk size'),
        (r'batch_size\s*=\s*\d+', 'Batch size'),
    ]

    def __init__(self) -> None:
        self._timestamp_pattern = re.compile(
            r'\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?\b'
        )

    def scan_file(self, file_path: Path) -> list[dict[str, Any]]:
        """Scan a Python file for forbidden hardcoded values."""
        violations = []
        try:
            content = file_path.read_text(encoding="utf-8")
            lines = content.split('\n')

            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue

                # Check for forbidden patterns
                for pattern, description in self.FORBIDDEN_HARDCODE_PATTERNS:
                    if re.search(pattern, stripped, re.IGNORECASE):
                        # Check if it's in an allowed context (timestamp creation)
                        if not self._is_allowed_context(stripped):
                            violations.append({
                                "file": str(file_path),
                                "line": i,
                                "code": stripped[:100],
                                "pattern": pattern,
                                "description": description,
                            })

        except Exception as exc:
            _logger.warning("TimestampHardcodeValidator: failed to scan %s: %s", file_path, exc)

        return violations

    def _is_allowed_context(self, line: str) -> bool:
        """Check if the line is in an allowed timestamp creation context."""
        for pattern in self.ALLOWED_TIMESTAMP_PATTERNS:
            if re.search(pattern, line):
                return True
        return False

    def scan_directory(self, directory: Path, pattern: str = "**/*.py") -> dict[str, list]:
        """Scan all Python files in a directory."""
        results = {}
        for file_path in directory.glob(pattern):
            if "__pycache__" in str(file_path) or ".venv" in str(file_path):
                continue
            violations = self.scan_file(file_path)
            if violations:
                results[str(file_path)] = violations
        return results


def create_dynamic_config_registry() -> DynamicConfigRegistry:
    """Factory to create the dynamic configuration registry."""
    return DynamicConfigRegistry()


def create_timestamp_validator() -> TimestampHardcodeValidator:
    """Factory to create the timestamp hardcode validator."""
    return TimestampHardcodeValidator()


__all__ = [
    "ConfigCategory",
    "ConfigKey",
    "ConfigValue",
    "DynamicConfigRegistry",
    "TimestampHardcodeValidator",
    "create_dynamic_config_registry",
    "create_timestamp_validator",
]