from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from xingcheng.infrastructure.model_parameter_policy import ModelParameterPolicy


def test_base_parameters_are_applied_when_dynamic_layer_is_empty() -> None:
    policy = ModelParameterPolicy()

    resolved = policy.resolve(
        model="qwen3-coder:30b-a3b-q4_K_M",
        task_intensity="difficult",
    )

    assert resolved["context_limit"] == 65_536
    assert resolved["default_output_tokens"] == 4_096
    assert resolved["max_output_tokens"] == 8_192
    assert resolved["reasoning_effort"] == "high"
    assert resolved["selected_mode"] == "maximum_quality"
    assert resolved["generation"] == {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "repeat_penalty": 1.05,
    }


def test_dynamic_layer_overrides_only_selected_fields(tmp_path: Path) -> None:
    base = {
        "schema_version": 1,
        "task_profiles": {
            "normal": {
                "context_source": "daily",
                "default_output_tokens": 512,
                "default_reasoning_effort": "low",
            }
        },
        "defaults": {
            "daily_context": 8192,
            "difficult_context": 32768,
            "max_output_tokens": 4096,
            "thinking": "dynamic",
            "keep_alive": 0,
            "generation": {},
        },
        "models": {
            "model:test": {
                "daily_context": 16384,
                "max_output_tokens": 4096,
                "generation": {"temperature": 0.7, "top_p": 0.8},
            }
        },
    }
    dynamic = {
        "schema_version": 1,
        "enabled": True,
        "models": {
            "model:test": {
                "daily_context": 12288,
                "generation": {"temperature": 0.2},
            }
        },
    }
    base_path = tmp_path / "base.json"
    dynamic_path = tmp_path / "dynamic.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    dynamic_path.write_text(json.dumps(dynamic), encoding="utf-8")
    policy = ModelParameterPolicy(
        base_path=base_path,
        dynamic_path=dynamic_path,
    )

    resolved = policy.resolve(model="model:test", task_intensity="normal")

    assert resolved["context_limit"] == 12_288
    assert resolved["generation"] == {"temperature": 0.2, "top_p": 0.8}
    assert resolved["max_output_tokens"] == 4_096


def test_dynamic_layer_reloads_after_file_change(tmp_path: Path) -> None:
    base = {
        "schema_version": 1,
        "task_profiles": {
            "normal": {
                "context_source": "daily",
                "default_output_tokens": 512,
                "default_reasoning_effort": "low",
            }
        },
        "defaults": {
            "daily_context": 8192,
            "max_output_tokens": 4096,
            "thinking": "dynamic",
            "generation": {},
        },
        "models": {},
    }
    base_path = tmp_path / "base.json"
    dynamic_path = tmp_path / "dynamic.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    dynamic_path.write_text(
        json.dumps({"schema_version": 1, "enabled": True, "defaults": {}}),
        encoding="utf-8",
    )
    policy = ModelParameterPolicy(base_path=base_path, dynamic_path=dynamic_path)
    assert policy.resolve(model="unknown")["context_limit"] == 8_192

    dynamic_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "defaults": {"daily_context": 16384},
            }
        ),
        encoding="utf-8",
    )

    assert policy.resolve(model="unknown")["context_limit"] == 16_384


def test_task_random_mode_is_weighted_reproducible_and_quality_safe() -> None:
    policy = ModelParameterPolicy()

    first = policy.resolve(
        model="qwen3.8:27b-q4_K_M",
        task_intensity="simple",
        request_key="整理下載資料夾",
    )
    second = policy.resolve(
        model="qwen3.8:27b-q4_K_M",
        task_intensity="simple",
        request_key="整理下載資料夾",
    )
    difficult = policy.resolve(
        model="qwen3.8:27b-q4_K_M",
        task_intensity="difficult",
        request_key="跨儲存庫追查高風險錯誤",
    )

    assert first["selected_mode"] in {"maximum_speed", "maximum_efficiency"}
    assert second["selected_mode"] == first["selected_mode"]
    assert difficult["selected_mode"] == "maximum_quality"
    assert difficult["reasoning_effort"] == "high"

    normal = policy.resolve(
        model="qwen3.8:27b-q4_K_M",
        task_intensity="normal",
        request_key="一般總指揮任務",
    )
    intermediate = policy.resolve(
        model="qwen3.8:27b-q4_K_M",
        task_intensity="intermediate",
        request_key="中級總指揮任務",
    )
    assert (normal["context_limit"], normal["default_output_tokens"]) == (
        8_192,
        1_536,
    )
    assert normal["reasoning_effort"] == "low"
    assert normal["keep_alive"] == -1
    assert (intermediate["context_limit"], intermediate["default_output_tokens"]) == (
        16_384,
        2_048,
    )
    assert difficult["context_limit"] == 32_768
    assert difficult["default_output_tokens"] == 4_096

    base = policy.resolve(
        model="qwen3.8:27b-q4_K_M",
        task_intensity="difficult",
        request_key="跨儲存庫追查高風險錯誤",
        immutable_base=True,
    )
    assert base["selected_mode"] == "base"
    assert base["default_output_tokens"] == 1_024
    assert base["dynamic_overrides_enabled"] is False
