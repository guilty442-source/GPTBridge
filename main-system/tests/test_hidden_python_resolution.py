from __future__ import annotations

import sys
from pathlib import Path


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

from tasks.tool_path_resolver import ToolPathResolver


def test_windows_tool_runtime_prefers_pythonw(tmp_path: Path, monkeypatch) -> None:
    tool_root = tmp_path / "shared-layer"
    scripts = tool_root / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (tool_root / "manifest.json").write_text('{"id":"shared-layer"}', "utf-8")
    (scripts / "python.exe").write_bytes(b"python")
    (scripts / "pythonw.exe").write_bytes(b"pythonw")
    monkeypatch.setattr("tasks.tool_path_resolver.os.name", "nt")
    resolver = ToolPathResolver(tmp_path)

    selected = resolver.resolve_python_executable({}, tool_root)

    assert selected.name == "pythonw.exe"
