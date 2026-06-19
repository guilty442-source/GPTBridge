from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(
    os.environ.get("GPTBRIDGE_PROJECT_ROOT")
    or Path(__file__).resolve().parent.parent.parent
).resolve()
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"

OFFICIAL_NAMES = (
    "GPTBridge",
    "\u4ecb\u9762\u7cfb\u7d71",
    "\u6838\u5fc3\u7cfb\u7d71",
    "\u8a2d\u8a08\u6a21\u5f0f",
    "\u6551\u63f4\u6a21\u5f0f",
    "\u958b\u767c\u8005\u6a21\u5f0f",
    "\u8a2d\u5b9a",
    "\u5171\u7528\u5c64",
)

DEFAULT_DISPLAY_NAMES = {
    "product": "GPTBridge",
    "interface_system": "\u4ecb\u9762\u7cfb\u7d71",
    "core_system": "\u6838\u5fc3\u7cfb\u7d71",
    "design_mode": "\u8a2d\u8a08\u6a21\u5f0f",
    "rescue_mode": "\u6551\u63f4\u6a21\u5f0f",
    "developer_mode": "\u958b\u767c\u8005\u6a21\u5f0f",
    "settings": "\u8a2d\u5b9a",
    "shared_layer": "\u5171\u7528\u5c64",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "chatgpt_main_url": "https://chatgpt.com/",
    "gemini_main_url": "https://gemini.google.com/",
    "claude_main_url": "https://claude.ai/",
    "perplexity_main_url": "https://www.perplexity.ai/",
    "deepseek_main_url": "https://chat.deepseek.com/",
    "design_project_output_dir": "platform_tools",
    "core_lock_enabled": True,
    "target_platform": "Windows 11",
    "ai_cost_mode": "balanced",
    "context_limits": {
        "chatgpt": 10,
        "gemini": 10,
        "rescue_a": 20,
        "rescue_b": 10,
    },
    "timeouts": {
        "provider_init_seconds": 60,
        "provider_response_seconds": 120,
        "browser_startup_seconds": 60,
        "build_seconds": 300,
        "sandbox_seconds": 180,
        "health_seconds": 180,
    },
    "browser_behavior": {
        "background": True,
        "headless": False,
    },
    "backup_policy": {
        "design_mode_max_records": 2,
        "mother_tool_max_records": 2,
    },
    "display_names": DEFAULT_DISPLAY_NAMES,
    "auto_cycle": 60,
    "max_backup_count": 3,
    "profile": "main",
    "auto_start": False,
}

AI_COST_PROFILES: dict[str, dict[str, Any]] = {
    "resource_saver": {
        "level": 1,
        "preferred_provider": "chatgpt",
        "fallback_provider": "gemini",
        "allow_dual_parallel": False,
        "allow_dual_collaboration": False,
        "max_cross_review_rounds": 0,
        "description": "Prefer single GPT tasks and use Gemini only as fallback.",
    },
    "balanced": {
        "level": 3,
        "preferred_provider": "chatgpt",
        "fallback_provider": "gemini",
        "allow_dual_parallel": True,
        "allow_dual_collaboration": True,
        "max_cross_review_rounds": 1,
        "description": "Use single AI for small tasks and dual AI when the workflow requires it.",
    },
    "full_power": {
        "level": 4,
        "preferred_provider": "dual",
        "fallback_provider": "single_available_provider",
        "allow_dual_parallel": True,
        "allow_dual_collaboration": True,
        "max_cross_review_rounds": 1,
        "description": "Prefer dual AI review and one-round collaboration for higher-risk work.",
    },
}

CONFIG_FIELDS: tuple[dict[str, str], ...] = (
    {"key": "chatgpt_main_url", "label": "AI ChatGPT URL", "kind": "url", "provider": "chatgpt", "target": "main"},
    {"key": "gemini_main_url", "label": "AI Gemini URL", "kind": "url", "provider": "gemini", "target": "main"},
    {"key": "claude_main_url", "label": "AI Claude URL", "kind": "url", "provider": "claude", "target": "main"},
    {"key": "perplexity_main_url", "label": "AI Perplexity URL", "kind": "url", "provider": "perplexity", "target": "main"},
    {"key": "deepseek_main_url", "label": "AI DeepSeek URL", "kind": "url", "provider": "deepseek", "target": "main"},
    {"key": "design_project_output_dir", "label": "Design project output directory", "kind": "path"},
    {"key": "core_lock_enabled", "label": "Core Lock", "kind": "boolean"},
    {"key": "target_platform", "label": "Target platform", "kind": "text"},
    {"key": "ai_cost_mode", "label": "AI cost mode", "kind": "select"},
    {"key": "context_limits", "label": "Context limits", "kind": "object"},
    {"key": "timeouts", "label": "Timeouts", "kind": "object"},
    {"key": "browser_behavior", "label": "Browser behavior", "kind": "object"},
    {"key": "backup_policy", "label": "Backup policy", "kind": "object"},
    {"key": "display_names", "label": "Display names", "kind": "names"},
)

def sanitize_config(data: dict[str, Any]) -> dict[str, Any]:
    merged = DEFAULT_CONFIG.copy()
    merged.update(data if isinstance(data, dict) else {})
    if not merged.get("chatgpt_main_url") and isinstance(data, dict):
        merged["chatgpt_main_url"] = (
            data.get("chatgpt_developer_url")
            or data.get("chatgpt_audit_url")
            or DEFAULT_CONFIG["chatgpt_main_url"]
        )
    if not merged.get("gemini_main_url") and isinstance(data, dict):
        merged["gemini_main_url"] = (
            data.get("gemini_developer_url")
            or data.get("gemini_audit_url")
            or DEFAULT_CONFIG["gemini_main_url"]
        )
    merged.pop("chatgpt_developer_url", None)
    merged.pop("gemini_developer_url", None)
    merged.pop("chatgpt_audit_url", None)
    merged.pop("gemini_audit_url", None)
    merged.pop("expected_chatgpt_account", None)
    merged.pop("expected_gemini_account", None)
    merged["display_names"] = sanitize_display_names(merged.get("display_names"))
    merged["ai_cost_mode"] = sanitize_ai_cost_mode(merged.get("ai_cost_mode"))
    merged["context_limits"] = sanitize_context_limits(merged.get("context_limits"))
    merged["timeouts"] = sanitize_nested_number_map(merged.get("timeouts"), "timeouts")
    merged["browser_behavior"] = sanitize_nested_number_map(merged.get("browser_behavior"), "browser_behavior")
    merged["backup_policy"] = sanitize_nested_number_map(merged.get("backup_policy"), "backup_policy")
    merged["target_platform"] = "Windows 11"
    return merged

def sanitize_display_names(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return DEFAULT_DISPLAY_NAMES.copy()
    output = DEFAULT_DISPLAY_NAMES.copy()
    for key in output:
        candidate = str(value.get(key, output[key]))
        if candidate in OFFICIAL_NAMES:
            output[key] = candidate
    return output

def sanitize_ai_cost_mode(value: Any) -> str:
    candidate = str(value or "balanced").strip()
    return candidate if candidate in {"resource_saver", "balanced", "full_power"} else "balanced"

def sanitize_context_limits(value: Any) -> dict[str, int]:
    defaults = DEFAULT_CONFIG["context_limits"]
    output = dict(defaults)
    if isinstance(value, dict):
        for key in output:
            try:
                output[key] = max(1, min(50, int(value.get(key, output[key]))))
            except (TypeError, ValueError):
                output[key] = defaults[key]
    return output

def sanitize_nested_number_map(value: Any, default_key: str) -> dict[str, Any]:
    defaults = DEFAULT_CONFIG[default_key]
    output = dict(defaults)
    if isinstance(value, dict):
        for key, default_value in defaults.items():
            candidate = value.get(key, default_value)
            if isinstance(default_value, bool):
                output[key] = bool(candidate)
            else:
                try:
                    output[key] = max(1, int(candidate))
                except (TypeError, ValueError):
                    output[key] = default_value
    return output

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return sanitize_config(data if isinstance(data, dict) else {})

def save_config(data: dict[str, Any]) -> None:
    merged = sanitize_config(data if isinstance(data, dict) else {})
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )

def update_config_url(key: str, url: str) -> None:
    if not url:
        return
    config = load_config()
    config[key] = url.strip()
    save_config(config)
