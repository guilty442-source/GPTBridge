"""Externalized Model Router Configuration.

Loads model routing rules from external configuration files (JSON/YAML)
instead of hardcoding in Python. Supports hot-reloading and environment
overrides.

Configuration priority (highest to lowest):
1. Environment variables (MODEL_ROUTE_*)
2. User config file (~/.config/local-model/routes.json)
3. Project config file (./config/routes.json)
4. Built-in defaults (this module)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final
from typing import Any

from . import ModelRoute

# Type aliases
RouteDict = dict[str, Any]
ConfigDict = dict[str, Any]

# Built-in defaults (same as original DEFAULT_ROUTES)
DEFAULT_ROUTES_DATA: Final[tuple[dict[str, Any], ...]] = (
    {"task_type": "understanding", "model_id": "qwen3.8:27b-q4_K_M", "role": "command-understanding", "priority": 10},
    {"task_type": "execution", "model_id": "qwen3.6:35b-a3b-coding", "role": "main-engineer", "priority": 9},
    {"task_type": "inspection", "model_id": "qwen3.8:27b-q4_K_M", "role": "inspector", "priority": 8},
    {"task_type": "integration", "model_id": "qwen3.8:27b-q4_K_M", "role": "integrator", "priority": 8},
    {"task_type": "data", "model_id": "ibm/granite4.2:30b-q4_K_M", "role": "data-worker", "priority": 7},
    {"task_type": "visual", "model_id": "openbmb/minicpm-v4.6:q8_0", "role": "visual-specialist", "priority": 6},
    {"task_type": "reasoning", "model_id": "deepseek-r1:14b", "role": "reasoning", "priority": 7},
    {"task_type": "training", "model_id": "gpt-oss:20b", "role": "training-owner", "priority": 5},
    {"task_type": "general", "model_id": "qwen3.5:9b-q4_K_M", "role": "resident-generalist", "priority": 1},
)

# Config file search paths (in order of precedence)
CONFIG_PATHS: Final[list[Path]] = [
    Path(os.environ.get("MODEL_ROUTE_CONFIG", "")) if os.environ.get("MODEL_ROUTE_CONFIG") else None,
    Path.home() / ".config" / "local-model" / "routes.json",
    Path.cwd() / "config" / "routes.json",
    Path(__file__).parent.parent.parent / "config" / "routes.json",
]


@dataclass(frozen=True)
class RouterConfig:
    """Externalized router configuration."""
    routes: tuple[ModelRoute, ...]
    loaded_from: str = ""
    last_modified: float = 0.0


class RouterConfigLoader:
    """Loads and manages external router configuration."""

    def __init__(self) -> None:
        self._config: RouterConfig | None = None
        self._watchers: list[callable] = []

    def load(self, force: bool = False) -> RouterConfig:
        """Load configuration from file, with fallback to defaults."""
        if self._config is not None and not force:
            # Check if file was modified
            if self._config.loaded_from:
                path = Path(self._config.loaded_from)
                if path.exists() and path.stat().st_mtime > self._config.last_modified:
                    force = True

        if not force and self._config is not None:
            return self._config

        # Try each config path in order
        for path in CONFIG_PATHS:
            if path and path.exists():
                try:
                    config = self._load_from_file(path)
                    self._config = RouterConfig(
                        routes=config,
                        loaded_from=str(path),
                        last_modified=path.stat().st_mtime,
                    )
                    self._notify_watchers(self._config)
                    return self._config
                except Exception:
                    continue

        # Fallback to built-in defaults
        default_routes = tuple(ModelRoute(**r) for r in DEFAULT_ROUTES_DATA)
        self._config = RouterConfig(
            routes=default_routes,
            loaded_from="built-in",
            last_modified=0.0,
        )
        return self._config

    def _load_from_file(self, path: Path) -> tuple[ModelRoute, ...]:
        """Load routes from JSON or YAML file."""
        content = path.read_text(encoding="utf-8")

        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
                data = yaml.safe_load(content)
            except ImportError:
                raise ValueError("PyYAML required for YAML config files")
        else:
            data = json.loads(content)

        # Support both formats:
        # { "routes": [ {task_type, model_id, role, priority}, ... ] }
        # [ {task_type, model_id, role, priority}, ... ]
        if isinstance(data, dict) and "routes" in data:
            routes_data = data["routes"]
        elif isinstance(data, list):
            routes_data = data
        else:
            raise ValueError(f"Invalid config format in {path}")

        routes = []
        for r in routes_data:
            if not isinstance(r, dict):
                continue
            routes.append(ModelRoute(
                task_type=r.get("task_type", ""),
                model_id=r.get("model_id", ""),
                role=r.get("role", ""),
                priority=r.get("priority", 0),
            ))

        return tuple(routes)

    def add_watcher(self, callback: callable) -> None:
        """Add a callback to be notified on config reload."""
        self._watchers.append(callback)

    def _notify_watchers(self, config: RouterConfig) -> None:
        for callback in self._watchers:
            try:
                callback(config)
            except Exception:
                pass


# Global loader instance
_loader = RouterConfigLoader()


def get_routes() -> tuple[ModelRoute, ...]:
    """Get current model routes (loads config on first call)."""
    return _loader.load().routes


def route_for(task_type: str) -> ModelRoute | None:
    """Return the model route for a given task type."""
    task_lower = task_type.lower()
    for route in get_routes():
        if route.task_type == task_lower:
            return route
    # Fallback to resident generalist
    for route in get_routes():
        if route.task_type == "general":
            return route
    return None


def all_routes() -> tuple[ModelRoute, ...]:
    """Return all declared model routes."""
    return get_routes()


def reload_config() -> tuple[ModelRoute, ...]:
    """Force reload configuration from file."""
    return _loader.load(force=True).routes


def add_config_watcher(callback: callable) -> None:
    """Add a callback to be notified when config is reloaded."""
    _loader.add_watcher(callback)


# Environment variable overrides
def _apply_env_overrides(routes: tuple[ModelRoute, ...]) -> tuple[ModelRoute, ...]:
    """Apply environment variable overrides to routes."""
    # Support MODEL_ROUTE_<TASK_TYPE>_MODEL_ID, MODEL_ROUTE_<TASK_TYPE>_ROLE, etc.
    env_routes = []
    for route in routes:
        task_upper = route.task_type.upper()
        model_env = os.environ.get(f"MODEL_ROUTE_{task_upper}_MODEL_ID")
        role_env = os.environ.get(f"MODEL_ROUTE_{task_upper}_ROLE")
        priority_env = os.environ.get(f"MODEL_ROUTE_{task_upper}_PRIORITY")

        new_route = ModelRoute(
            task_type=route.task_type,
            model_id=model_env if model_env else route.model_id,
            role=role_env if role_env else route.role,
            priority=int(priority_env) if priority_env else route.priority,
        )
        env_routes.append(new_route)

    return tuple(env_routes)


# Export public API
__all__ = [
    "ModelRoute",
    "DEFAULT_ROUTES_DATA",
    "RouterConfig",
    "RouterConfigLoader",
    "get_routes",
    "route_for",
    "all_routes",
    "reload_config",
    "add_config_watcher",
]

# Keep backward compatibility
DEFAULT_ROUTES = tuple(ModelRoute(**r) for r in DEFAULT_ROUTES_DATA)
__all__.extend(["DEFAULT_ROUTES"])
