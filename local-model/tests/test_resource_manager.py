from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "local-model" / "src" / "backend" / "services" / "local_ai" / "infrastructure" / "resource_manager.py"
SPEC = importlib.util.spec_from_file_location("resource_manager_contract", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
ResourceManager = MODULE.ResourceManager


class FakeOllama:
    def __init__(self, running: list[str] | None = None) -> None:
        self.running = list(running or [])
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, _timeout: float) -> dict[str, Any]:
        self.calls.append((method, url, payload))
        if method == "GET":
            return {"models": [{"name": name} for name in self.running]}
        assert payload is not None
        model = str(payload["model"])
        if payload.get("keep_alive") == 0:
            self.running = [name for name in self.running if name != model]
        elif model not in self.running:
            self.running.append(model)
        return {"done": True}


def test_prepare_model_releases_old_model_preloads_and_verifies() -> None:
    ollama = FakeOllama(["old-model"])
    manager = ResourceManager(transport=ollama)
    manager._nvidia_rows = lambda: []  # type: ignore[method-assign]

    result = manager.prepare_model("new-model")

    assert result["device"] == "cpu"
    assert result["released_models"] == ("old-model",)
    assert result["resident"] is True
    assert ollama.running == ["new-model"]


def test_prepare_model_keeps_release_after_request_model_long_enough_to_verify() -> None:
    ollama = FakeOllama()
    manager = ResourceManager(transport=ollama)
    manager._nvidia_rows = lambda: []  # type: ignore[method-assign]

    result = manager.prepare_model("transient-model", keep_alive=0)

    preload = next(
        payload
        for method, url, payload in ollama.calls
        if method == "POST" and url.endswith("/api/generate")
    )
    assert preload is not None
    assert preload["keep_alive"] == "30s"
    assert result["resident"] is True


def test_cpu_mode_forces_zero_gpu_layers() -> None:
    ollama = FakeOllama()
    manager = ResourceManager(transport=ollama)
    manager.switch_to_cpu()

    manager.preload_model("model-a")

    preload = next(payload for method, _url, payload in ollama.calls if method == "POST")
    assert preload is not None
    assert preload["options"]["num_gpu"] == 0


def test_gpu_mode_requires_detected_nvidia_gpu() -> None:
    manager = ResourceManager(transport=FakeOllama())
    manager._nvidia_rows = lambda: []  # type: ignore[method-assign]

    try:
        manager.switch_to_gpu()
    except RuntimeError as error:
        assert str(error) == "GPU_NOT_AVAILABLE"
    else:
        raise AssertionError("GPU mode accepted without a GPU")


def test_emergency_release_unloads_every_running_model() -> None:
    ollama = FakeOllama(["model-a", "model-b"])
    manager = ResourceManager(transport=ollama)

    result = manager.emergency_release()

    assert result["released_models"] == ("model-a", "model-b")
    assert result["errors"] == ()
    assert ollama.running == []


def test_residency_policy_is_owned_by_resource_manager() -> None:
    manager = ResourceManager(
        transport=FakeOllama(), resident_models={"commander"}
    )

    assert manager.keep_alive_for("commander", -1, release_after_request=True) == -1
    assert manager.keep_alive_for("worker", -1, release_after_request=True) == 0
    assert manager.keep_alive_for("worker", "5m") == "5m"
