from __future__ import annotations

import importlib.util
from pathlib import Path


def test_platform_tool_wrapper_prevents_duplicate_windows() -> None:
    source = Path("scripts/templates/platform-tool-app/main.cjs").read_text(
        encoding="utf-8"
    )

    assert "app.requestSingleInstanceLock()" in source
    assert "app.on('second-instance'" in source
    assert "mainWindow.focus()" in source


def test_platform_tool_packager_copies_runtime_source(tmp_path: Path) -> None:
    module_path = Path("scripts/package_platform_tools.py")
    spec = importlib.util.spec_from_file_location("package_platform_tools", module_path)
    assert spec and spec.loader
    package_platform_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package_platform_tools)

    tool_dir = tmp_path / "demo-tool"
    entry = tool_dir / "src" / "main.py"
    backend_file = tool_dir / "src" / "backend" / "service.py"
    pycache_file = tool_dir / "src" / "backend" / "__pycache__" / "service.pyc"
    backend_file.parent.mkdir(parents=True)
    pycache_file.parent.mkdir(parents=True)
    entry.write_text("print('demo')\n", encoding="utf-8")
    backend_file.write_text("class Service:\n    pass\n", encoding="utf-8")
    pycache_file.write_bytes(b"cache")

    app_dir = tmp_path / "dist" / "resources" / "app"
    app_dir.mkdir(parents=True)
    copied = package_platform_tools.copy_runtime_source(tool_dir, entry, app_dir)

    assert copied == app_dir / "src"
    assert (app_dir / "src" / "main.py").exists()
    assert (app_dir / "src" / "backend" / "service.py").exists()
    assert not (app_dir / "src" / "backend" / "__pycache__").exists()


def test_platform_tool_packager_prefers_hardlinks_for_runtime_files(tmp_path: Path) -> None:
    module_path = Path("scripts/package_platform_tools.py")
    spec = importlib.util.spec_from_file_location("package_platform_tools", module_path)
    assert spec and spec.loader
    package_platform_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package_platform_tools)

    source = tmp_path / "electron" / "resources" / "app.asar"
    target = tmp_path / "tool" / "resources" / "app.asar"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"shared")

    package_platform_tools.copy_runtime_item(source, target)

    assert target.read_bytes() == b"shared"
    try:
        assert source.samefile(target)
    except OSError:
        assert target.exists()
