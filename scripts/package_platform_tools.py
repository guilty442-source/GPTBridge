from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_TOOLS_DIR = PROJECT_ROOT / "platform_tools"


def load_manifest(tool_dir: Path) -> dict[str, Any] | None:
    manifest_path = tool_dir / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def resolve_entry(tool_dir: Path, manifest: dict[str, Any]) -> Path:
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        runtime_entry = str(runtime.get("entry", "")).strip()
        if runtime_entry:
            return (tool_dir / runtime_entry).resolve()

    raw_entry = str(manifest.get("entry", "")).strip()
    if not raw_entry:
        return (tool_dir / "src" / "main.py").resolve()

    entry_path = PROJECT_ROOT / raw_entry
    if entry_path.suffix == "":
        entry_path = entry_path.with_suffix(".py")
    return entry_path.resolve()


def resolve_executable_name(tool_id: str, manifest: dict[str, Any]) -> str:
    executable = manifest.get("executable")
    if isinstance(executable, dict):
        raw_name = str(executable.get("name", "")).strip()
        if raw_name:
            return Path(raw_name).stem
        raw_path = str(executable.get("path", "")).strip()
        if raw_path:
            return Path(raw_path).stem
    return tool_id


def iter_tools(selected_ids: set[str] | None) -> list[tuple[str, Path, dict[str, Any]]]:
    tools: list[tuple[str, Path, dict[str, Any]]] = []
    if not PLATFORM_TOOLS_DIR.exists():
        return tools

    for tool_dir in sorted(PLATFORM_TOOLS_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not tool_dir.is_dir() or tool_dir.name.startswith("_"):
            continue
        manifest = load_manifest(tool_dir)
        if not manifest:
            continue
        tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
        if selected_ids is not None and tool_id not in selected_ids:
            continue
        tools.append((tool_id, tool_dir, manifest))
    return tools


def package_tool(tool_id: str, tool_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    entry = resolve_entry(tool_dir, manifest)
    if not entry.exists() or entry.suffix.lower() != ".py":
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Python entry not found: {entry}",
        }

    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    build_dir = tool_dir / "build"
    dist_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--clean",
        "--name",
        executable_name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
        str(entry),
    ]

    executable = manifest.get("executable")
    console = True
    if isinstance(executable, dict):
        console = executable.get("console", True) is not False
    if not console:
        command.insert(5, "--noconsole")

    completed = subprocess.run(
        command,
        cwd=str(tool_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    exe_path = dist_dir / f"{executable_name}.exe"
    return {
        "ok": completed.returncode == 0 and exe_path.exists(),
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "exit_code": completed.returncode,
        "output": completed.stdout,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Package platform tools as standalone EXEs.")
    parser.add_argument("tool_ids", nargs="*", help="Optional tool ids to package.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    selected_ids = set(args.tool_ids) if args.tool_ids else None
    results = [
        package_tool(tool_id, tool_dir, manifest)
        for tool_id, tool_dir, manifest in iter_tools(selected_ids)
    ]
    ok = all(result.get("ok") for result in results)

    if args.as_json:
        print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False))
    else:
        for result in results:
            status = "OK" if result.get("ok") else "FAIL"
            print(f"[{status}] {result.get('tool_id')}: {result.get('exe_path') or result.get('message')}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
