from __future__ import annotations

import os
import sys
from pathlib import Path


sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from core.environment_doctor import (  # noqa: E402
    check_electron_runtime,
    check_platform_tools,
    check_requirements_file,
    collect_environment_report,
    normalize_requirement_name,
)


def test_normalize_requirement_name_handles_pins_and_comments() -> None:
    assert normalize_requirement_name("Pillow==12.0 # image support") == "pillow"
    assert normalize_requirement_name("python_dotenv>=1") == "python-dotenv"
    assert normalize_requirement_name("") is None


def test_requirements_check_reports_missing_required_packages(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi==0.1\n", encoding="utf-8")

    result = check_requirements_file(
        tmp_path,
        {
            "fastapi": "fastapi",
            "websockets": "websockets",
        },
    )

    assert result["ok"] is False
    assert result["missing"] == ["websockets"]


def test_electron_runtime_requires_binary_and_path_file(tmp_path: Path) -> None:
    electron_root = tmp_path / "node_modules" / "electron"
    dist_dir = electron_root / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "electron.exe").write_bytes(b"exe")
    (electron_root / "path.txt").write_text("electron.exe", encoding="utf-8")

    result = check_electron_runtime(tmp_path)

    assert result["ok"] is True
    assert result["exe_exists"] is True
    assert result["path_txt_exists"] is True


def test_platform_tool_scan_validates_runtime_entry_and_executable(tmp_path: Path) -> None:
    tool_dir = tmp_path / "platform_tools" / "demo"
    entry = tool_dir / "src" / "main.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("print('ok')\n", encoding="utf-8")
    (tool_dir / "manifest.json").write_text(
        """
{
  "id": "demo",
  "runtime": {"type": "python", "entry": "src/main.py"},
  "executable": {"path": "dist/demo.exe", "name": "demo"}
}
""".strip(),
        encoding="utf-8",
    )

    result = check_platform_tools(tmp_path)

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["tools"][0]["entry_exists"] is True


def test_environment_report_uses_injected_module_requirements(tmp_path: Path) -> None:
    for relative_path in (
        "package.json",
        "package-lock.json",
        "requirements.txt",
        "src-core/main.py",
        "src-core/ipc/server.py",
        "src-ui/renderer/.keep",
        "platform_tools/demo/src/main.py",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("json\n", encoding="utf-8")
    (tmp_path / "platform_tools" / "demo" / "manifest.json").write_text(
        '{"id":"demo","runtime":{"entry":"src/main.py"},"executable":{"path":"dist/demo.exe"}}',
        encoding="utf-8",
    )
    electron_root = tmp_path / "node_modules" / "electron"
    (electron_root / "dist").mkdir(parents=True)
    (electron_root / "dist" / "electron.exe").write_bytes(b"exe")
    (electron_root / "path.txt").write_text("electron.exe", encoding="utf-8")

    report = collect_environment_report(
        tmp_path,
        python_executable=sys.executable,
        required_python_modules={"json": "json"},
    )

    assert report["ok"] is True
    assert report["python"]["modules"] == {"json": True}
