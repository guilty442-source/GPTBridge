from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_TOOLS_DIR = PROJECT_ROOT / "platform_tools"
ELECTRON_DIST_DIR = PROJECT_ROOT / "node_modules" / "electron" / "dist"
PLATFORM_RENDERER_ROOT = PROJECT_ROOT / "dist-ui" / "platform-tools"
TEMPLATE_DIR = PROJECT_ROOT / "scripts" / "templates" / "platform-tool-app"


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


def npx_command() -> str:
    return "npx.cmd" if os.name == "nt" else "npx"


def renderer_output_dir(tool_id: str) -> Path:
    return PLATFORM_RENDERER_ROOT / tool_id / "renderer"


def build_platform_renderer(tool_id: str) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["GPTBRIDGE_PLATFORM_TOOL_ID"] = tool_id
    command = [
        npx_command(),
        "vite",
        "build",
        "-c",
        "vite.platform-tools.config.ts",
    ]
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output_dir = renderer_output_dir(tool_id)
    index_path = output_dir / "index.html"
    if completed.returncode == 0 and not index_path.exists():
        html_files = list(output_dir.rglob("*.html"))
        if len(html_files) == 1:
            html_source = html_files[0]
            html = html_source.read_text(encoding="utf-8")
            relative_prefix = "../" * len(html_source.relative_to(output_dir).parents[:-1])
            if relative_prefix:
                html = html.replace(f'{relative_prefix}assets/', './assets/')
            index_path.write_text(html, encoding="utf-8", newline="\n")
            html_source.unlink()
            for parent in reversed(html_source.relative_to(output_dir).parents[:-1]):
                candidate = output_dir / parent
                if candidate.exists() and not any(candidate.iterdir()):
                    candidate.rmdir()
    return {
        "ok": completed.returncode == 0 and index_path.exists(),
        "tool_id": tool_id,
        "renderer_path": str(output_dir),
        "exit_code": completed.returncode,
        "output": completed.stdout,
    }


def copy_app_templates(app_dir: Path) -> None:
    for filename in ("main.cjs", "preload.cjs"):
        source = TEMPLATE_DIR / filename
        if not source.exists():
            raise FileNotFoundError(f"Wrapper template not found: {source}")
        shutil.copy2(source, app_dir / filename)


def copy_runtime_source(tool_dir: Path, entry: Path, app_dir: Path) -> Path | None:
    try:
        relative_entry = entry.resolve().relative_to(tool_dir.resolve())
    except ValueError:
        return None
    if not relative_entry.parts:
        return None

    source_root = tool_dir / relative_entry.parts[0]
    if not source_root.is_dir():
        return None

    target_root = app_dir / relative_entry.parts[0]
    if target_root.exists():
        target_resolved = target_root.resolve()
        app_resolved = app_dir.resolve()
        if app_resolved != target_resolved and app_resolved not in target_resolved.parents:
            raise RuntimeError(f"Refusing to replace path outside app package: {target_root}")
        shutil.rmtree(target_root)
    shutil.copytree(
        source_root,
        target_root,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "runtime",
        ),
    )
    return target_root


def copy_file_preserving_locked_target(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            if source.samefile(target):
                return
            target.unlink()
        except PermissionError:
            return
        except OSError:
            pass
    try:
        os.link(source, target)
        return
    except OSError:
        pass

    try:
        shutil.copy2(source, target)
    except PermissionError:
        if not target.exists():
            raise


def copy_runtime_item(source: Path, target: Path) -> None:
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_runtime_item(child, target / child.name)
        return
    copy_file_preserving_locked_target(source, target)


def copy_electron_runtime(dist_dir: Path, exe_path: Path) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)

    for source in ELECTRON_DIST_DIR.iterdir():
        target = dist_dir / source.name
        if source.name == "electron.exe":
            continue
        copy_runtime_item(source, target)

    copy_file_preserving_locked_target(ELECTRON_DIST_DIR / "electron.exe", exe_path)
    leftover_electron = dist_dir / "electron.exe"
    if leftover_electron.exists():
        try:
            leftover_electron.unlink()
        except PermissionError:
            pass


def package_tool(tool_id: str, tool_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    entry = resolve_entry(tool_dir, manifest)
    if not entry.exists() or entry.suffix.lower() != ".py":
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Python runtime entry not found: {entry}",
        }

    electron_exe = ELECTRON_DIST_DIR / "electron.exe"
    if not electron_exe.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Electron runtime not found: {electron_exe}",
        }

    renderer_result = build_platform_renderer(tool_id)
    if not renderer_result.get("ok"):
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "message": "Platform renderer build failed",
            "renderer_output": renderer_result.get("output", ""),
        }

    renderer_dir = Path(str(renderer_result["renderer_path"]))
    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    exe_path = dist_dir / f"{executable_name}.exe"

    try:
        copy_electron_runtime(dist_dir, exe_path)

        app_dir = dist_dir / "resources" / "app"
        if app_dir.exists():
            shutil.rmtree(app_dir)
        app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(renderer_dir, app_dir / "renderer")
        runtime_path = copy_runtime_source(tool_dir, entry, app_dir)

        app_manifest = dict(manifest)
        app_manifest["id"] = tool_id
        (app_dir / "manifest.json").write_text(
            json.dumps(app_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (app_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": f"gptbridge-tool-{tool_id}",
                    "version": str(manifest.get("version", "1.0.0")),
                    "main": "main.cjs",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        copy_app_templates(app_dir)
    except Exception as exc:
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "exe_path": str(exe_path),
            "message": str(exc),
        }

    return {
        "ok": exe_path.exists(),
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "renderer_path": str(app_dir / "renderer"),
        "runtime_path": str(runtime_path) if runtime_path else "",
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Package platform tools as standalone EXEs.")
    parser.add_argument("tool_ids", nargs="*", help="Optional tool ids to package.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--build-renderers-only",
        action="store_true",
        help="Only build per-tool renderer bundles without assembling Electron EXEs.",
    )
    args = parser.parse_args()

    selected_ids = set(args.tool_ids) if args.tool_ids else None
    tools = iter_tools(selected_ids)
    if args.build_renderers_only:
        results = [build_platform_renderer(tool_id) for tool_id, _, _ in tools]
    else:
        results = [
            package_tool(tool_id, tool_dir, manifest)
            for tool_id, tool_dir, manifest in tools
        ]
    ok = all(result.get("ok") for result in results)

    if args.as_json:
        print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False))
    else:
        for result in results:
            status = "OK" if result.get("ok") else "FAIL"
            target = result.get("exe_path") or result.get("renderer_path") or result.get("message")
            print(f"[{status}] {result.get('tool_id')}: {target}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
