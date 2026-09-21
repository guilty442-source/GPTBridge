from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

for path in (
    ROOT / "local-model" / "src" / "backend" / "services",
    ROOT / "shared-layer" / "src",
    ROOT,
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _isolate_native_engine_settings(monkeypatch, tmp_path_factory):
    """測試一律不讀機器上的執行期 native-engine 設定（可決定性）。"""
    try:
        from xingcheng.infrastructure import native_engine as module
    except Exception:  # pragma: no cover - 匯入失敗時不影響其他測試
        return
    root = tmp_path_factory.mktemp("native-engine-isolation")
    if hasattr(module, "settings_path"):
        monkeypatch.setattr(module, "settings_path", lambda: root / "absent.json")
    if hasattr(module, "_engine_cache"):
        monkeypatch.setattr(module, "_engine_cache", {})
    if hasattr(module, "NATIVE_EXECUTION_LEDGER"):
        monkeypatch.setattr(
            module, "NATIVE_EXECUTION_LEDGER", str(root / "ledger.jsonl")
        )
    for name in ("NATIVE_ENGINE_ENV", "NATIVE_CHECKPOINT_ENV"):
        env_name = getattr(module, name, "")
        if env_name:
            monkeypatch.delenv(env_name, raising=False)