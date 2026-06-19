import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from orchestrator.autonomous_coder import AutonomousCodingAgent
from orchestrator.development_rules import with_development_rules
from main import GPTBridgeApp


class FakeProvider:
    def __init__(self, response: str, dispatch_ok: bool = True) -> None:
        self.response = response
        self.dispatch_ok = dispatch_ok
        self.last_dispatch_error = "" if dispatch_ok else "not ready"
        self.prompts: list[str] = []

    async def dispatch_prompt(self, prompt: str, timeout_seconds: float = 20) -> bool:
        self.prompts.append(prompt)
        return self.dispatch_ok

    async def wait_until_idle(self, timeout_seconds: int = 120) -> bool:
        return True

    async def capture_response(self, timeout_seconds: float = 120) -> str:
        return self.response


class FakeLogger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        return None

    def warning(self, *_args: object, **_kwargs: object) -> None:
        return None

    def error(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeAutonomousAgent:
    async def process_instruction(
        self,
        rel_path: str,
        content: str,
        instruction: str,
        auto_test: bool = True,
    ) -> dict[str, object]:
        _ = rel_path, content, instruction, auto_test
        return {
            "ok": True,
            "message": "repair ready",
            "suggested_fix": "print('fixed')\n",
        }


def create_app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> GPTBridgeApp:
    monkeypatch.setenv("GPTBRIDGE_PROJECT_ROOT", str(tmp_path))
    app = GPTBridgeApp()
    app.core_logger = FakeLogger()
    return app


def test_development_rules_are_idempotent() -> None:
    prompt = "請修正這個錯誤。"
    with_rules = with_development_rules(prompt)

    assert "AI 開發工作規範" in with_rules
    assert with_development_rules(with_rules) == with_rules


@pytest.mark.asyncio
async def test_agent_coder_sends_rules_and_extracts_code() -> None:
    provider = FakeProvider("說明\n```python\nprint('fixed')\n```")
    agent = AutonomousCodingAgent(provider, None)

    result = await agent.process_instruction(
        "platform_tools/example/src/main.py",
        "print('old')\n",
        "把輸出文字修正。",
    )

    assert result["ok"] is True
    assert result["suggested_fix"] == "print('fixed')\n"
    assert provider.prompts
    assert "AI 開發工作規範" in provider.prompts[0]
    assert "platform_tools/example/src/main.py" in provider.prompts[0]


@pytest.mark.asyncio
async def test_agent_coder_falls_back_to_gemini() -> None:
    chatgpt = FakeProvider("", dispatch_ok=False)
    gemini = FakeProvider("```python\nprint('gemini fixed')\n```")
    agent = AutonomousCodingAgent(chatgpt, gemini)

    result = await agent.process_instruction(
        "platform_tools/example/src/main.py",
        "print('old')\n",
        "修正輸出。",
    )

    assert result["ok"] is True
    assert result["provider"] == "gemini"
    assert result["suggested_fix"] == "print('gemini fixed')\n"


@pytest.mark.asyncio
async def test_run_unit_tests_maps_source_file_to_matching_test(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path, monkeypatch)
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "def test_sample():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    result = await app.run_unit_tests("src/sample.py")

    assert result["ok"] is True
    assert result["targets"] == ["tests/test_sample.py"]
    assert "test_sample.py" in result["command"]


@pytest.mark.asyncio
async def test_run_unit_tests_falls_back_to_project_tests(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path, monkeypatch)
    source = tmp_path / "src" / "feature.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n",
        encoding="utf-8",
    )

    result = await app.run_unit_tests("src/feature.py")

    assert result["ok"] is True
    assert result["targets"] == ["tests"]


@pytest.mark.asyncio
async def test_agent_instruction_attaches_auto_test_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(tmp_path, monkeypatch)
    app.autonomous_agent = FakeAutonomousAgent()
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text("print('old')\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "def test_sample():\n    assert True\n",
        encoding="utf-8",
    )

    result = await app.instruct_agent_on_code(
        "src/sample.py",
        "print('old')\n",
        "fix it",
        auto_test=True,
    )

    assert result["ok"] is True
    assert result["test_ok"] is True
    assert result["test_result"]["targets"] == ["tests/test_sample.py"]
