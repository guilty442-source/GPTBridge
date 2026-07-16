from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_visual_smoke():
    module_path = Path("scripts/smoke/ai_assistant_visual_smoke.py")
    spec = importlib.util.spec_from_file_location(
        "ai_assistant_visual_smoke",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_visual_smoke_fixture_is_synthetic_and_redacted() -> None:
    module = load_visual_smoke()
    fixture = module.synthetic_fixture()
    serialized = json.dumps(fixture, ensure_ascii=False)

    assert fixture["state_revision"] == "visual-fixture-v4"
    assert fixture["state"]["analytics"]["version"] == "4.0.0"
    assert fixture["state"]["v3"]["version"] == "4.0.0"
    assert len(fixture["state"]["holdings"]) == 3
    assert "synthetic_fixture" in serialized
    assert "範例投資組合.xlsx" in serialized
    assert "source_path" not in serialized
    assert "E:\\GPTBridge" not in serialized


def test_visual_smoke_is_available_as_an_npm_script() -> None:
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))
    assert (
        package["scripts"]["smoke:ai-assistant-ui"]
        == ".venv\\Scripts\\python.exe scripts/smoke/ai_assistant_visual_smoke.py"
    )
