from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


class ModelParameterPolicy:
    """Load immutable base parameters plus a mutable dynamic override layer."""

    VALID_INTENSITIES = frozenset({"simple", "normal", "intermediate", "difficult"})
    VALID_EFFORTS = frozenset({"none", "low", "medium", "high"})
    _META_KEYS = frozenset({"schema_version", "enabled", "base_config", "description"})

    def __init__(
        self,
        *,
        base_path: Path | None = None,
        dynamic_path: Path | None = None,
    ) -> None:
        config_root = Path(__file__).resolve().parents[5] / "config"
        self.base_path = base_path or config_root / "ollama-model-parameters.json"
        self.dynamic_path = (
            dynamic_path
            or config_root / "ollama-model-parameters.dynamic.json"
        )
        self._lock = threading.Lock()
        self._signature: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._base: dict[str, Any] = {}
        self._merged: dict[str, Any] = {}
        self._dynamic_enabled = False
        self.reload_if_changed(force=True)

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size

    @staticmethod
    def _load_object(path: Path) -> dict[str, Any]:
        decoded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError(f"MODEL_PARAMETER_CONFIG_NOT_OBJECT:{path.name}")
        if int(decoded.get("schema_version") or 0) != 1:
            raise ValueError(f"MODEL_PARAMETER_CONFIG_SCHEMA_INVALID:{path.name}")
        return decoded

    @classmethod
    def _deep_merge(
        cls, base: Mapping[str, Any], override: Mapping[str, Any]
    ) -> dict[str, Any]:
        merged = deepcopy(dict(base))
        for key, value in override.items():
            if key in cls._META_KEYS:
                continue
            existing = merged.get(key)
            if isinstance(existing, Mapping) and isinstance(value, Mapping):
                merged[key] = cls._deep_merge(existing, value)
            else:
                merged[key] = deepcopy(value)
        return merged

    def reload_if_changed(self, *, force: bool = False) -> bool:
        signature = (
            self._file_signature(self.base_path),
            self._file_signature(self.dynamic_path),
        )
        with self._lock:
            if not force and signature == self._signature:
                return False
            base = self._load_object(self.base_path)
            dynamic = self._load_object(self.dynamic_path)
            enabled = dynamic.get("enabled") is not False
            merged = self._deep_merge(base, dynamic) if enabled else deepcopy(base)
            self._base = deepcopy(base)
            self._merged = merged
            self._dynamic_enabled = enabled
            self._signature = signature
        return True

    def snapshot(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            return deepcopy(self._merged)

    def base_snapshot(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            return deepcopy(self._base)

    def global_ollama(self) -> dict[str, Any]:
        config = self.snapshot().get("global_ollama")
        return dict(config) if isinstance(config, Mapping) else {}

    def temporary_parameter_advisor(self) -> dict[str, Any]:
        config = self.snapshot().get("temporary_parameter_advisor")
        return dict(config) if isinstance(config, Mapping) else {}

    def resolve(
        self,
        *,
        model: str,
        task_intensity: Any = None,
        reasoning_effort: Any = None,
        request_key: Any = None,
        immutable_base: bool = False,
    ) -> dict[str, Any]:
        config = self.base_snapshot() if immutable_base else self.snapshot()
        intensity = str(task_intensity or "normal").strip().casefold()
        if intensity not in self.VALID_INTENSITIES:
            intensity = "normal"

        modes = config.get("modes")
        modes = modes if isinstance(modes, Mapping) else {}
        selected_mode = str(config.get("active_mode") or "base").strip()
        selected_definition = modes.get(selected_mode)
        if isinstance(selected_definition, Mapping):
            strategy = str(selected_definition.get("selection_strategy") or "")
            if strategy == "deterministic-task-weighted":
                candidates_by_task = selected_definition.get("candidates_by_task")
                candidates_by_task = (
                    candidates_by_task
                    if isinstance(candidates_by_task, Mapping)
                    else {}
                )
                candidates = candidates_by_task.get(intensity)
                candidates = (
                    [str(item) for item in candidates if str(item) in modes]
                    if isinstance(candidates, list)
                    else []
                )
                if candidates:
                    digest = hashlib.sha256(
                        f"{intensity}\0{str(request_key or '')}".encode("utf-8")
                    ).digest()
                    selected_mode = candidates[
                        int.from_bytes(digest[:8], "big") % len(candidates)
                    ]
                    selected_definition = modes.get(selected_mode)
        if isinstance(selected_definition, Mapping):
            config = self._deep_merge(config, selected_definition)

        defaults = config.get("defaults")
        defaults = dict(defaults) if isinstance(defaults, Mapping) else {}
        models = config.get("models")
        models = models if isinstance(models, Mapping) else {}
        model_override = models.get(str(model or ""))
        profile = self._deep_merge(
            defaults,
            model_override if isinstance(model_override, Mapping) else {},
        )

        task_profiles = config.get("task_profiles")
        task_profiles = task_profiles if isinstance(task_profiles, Mapping) else {}
        task = task_profiles.get(intensity)
        task = dict(task) if isinstance(task, Mapping) else {}
        model_task_profiles = profile.get("task_profiles")
        model_task_profiles = (
            model_task_profiles
            if isinstance(model_task_profiles, Mapping)
            else {}
        )
        model_task = model_task_profiles.get(intensity)
        if isinstance(model_task, Mapping):
            # Model-specific intensity settings are applied last so a resident
            # coordinator can stay lean for daily work and scale up only when
            # the classified task actually requires it.
            task = self._deep_merge(task, model_task)

        effort = str(reasoning_effort or "").strip().casefold()
        if effort not in self.VALID_EFFORTS:
            effort = str(task.get("default_reasoning_effort") or "medium").casefold()
        if effort not in self.VALID_EFFORTS:
            effort = "medium"

        context_source = str(task.get("context_source") or "daily")
        configured_context = task.get("context_limit")
        if configured_context is None:
            configured_context = profile.get(f"{context_source}_context")
        if configured_context is None:
            configured_context = profile.get("daily_context", 8192)
        context_limit = max(2048, int(configured_context))

        model_output_limit = max(32, int(profile.get("max_output_tokens") or 4096))
        default_output = max(
            32, int(task.get("default_output_tokens") or min(512, model_output_limit))
        )
        default_output = min(default_output, model_output_limit)

        generation = profile.get("generation")
        generation = dict(generation) if isinstance(generation, Mapping) else {}
        return {
            "model": str(model or ""),
            "display_name": str(profile.get("display_name") or model or ""),
            "role": str(profile.get("role") or ""),
            "task_intensity": intensity,
            "reasoning_effort": effort,
            "context_limit": context_limit,
            "default_output_tokens": default_output,
            "max_output_tokens": model_output_limit,
            "thinking": str(profile.get("thinking") or "dynamic").casefold(),
            "keep_alive": profile.get("keep_alive", 0),
            "generation": generation,
            "selected_mode": selected_mode,
            "dynamic_overrides_enabled": (
                self._dynamic_enabled and not immutable_base
            ),
        }

    @staticmethod
    def think_value(*, style: str, effort: str, model: str) -> bool | str:
        if effort == "none" or style == "off":
            return False
        if model.startswith("gpt-oss:"):
            return effort
        if style == "on":
            return True
        if style == "high":
            return effort == "high"
        if style == "medium-high":
            return effort in {"medium", "high"}
        if style == "low-medium":
            return effort in {"low", "medium", "high"}
        return effort in {"medium", "high"}


__all__ = ["ModelParameterPolicy"]
