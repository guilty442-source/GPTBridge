from __future__ import annotations

from typing import Any


SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    "profile": {"type": str, "default": "main", "required": False},
    "chatgpt_main_url": {"type": str, "default": "https://chatgpt.com/", "required": False},
    "gemini_main_url": {"type": str, "default": "https://gemini.google.com/", "required": False},
    "claude_main_url": {"type": str, "default": "https://claude.ai/", "required": False},
    "deepseek_main_url": {"type": str, "default": "https://chat.deepseek.com/", "required": False},
    "auto_start_backend": {"type": bool, "default": True, "required": False},
    "safe_mode": {"type": bool, "default": False, "required": False},
}


def default_settings() -> dict[str, Any]:
    return {key: rule["default"] for key, rule in SETTINGS_SCHEMA.items()}


def validate_settings(settings: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(settings, dict):
        return False, ["settings must be a dictionary"]

    for key, rule in SETTINGS_SCHEMA.items():
        if rule.get("required") and key not in settings:
            errors.append(f"{key} is required")
            continue
        if key not in settings:
            continue
        expected_type = rule.get("type")
        if expected_type is not None and not isinstance(settings[key], expected_type):
            errors.append(f"{key} must be {getattr(expected_type, '__name__', expected_type)}")

    return not errors, errors


def normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = default_settings()
    for key, value in settings.items():
        if key in SETTINGS_SCHEMA:
            normalized[key] = value
    return normalized
