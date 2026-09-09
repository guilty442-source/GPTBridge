from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from utils.archive import safe_extract_zip


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


REQUIRED_PROJECT_PATHS: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "src-core/main.py",
    "src-core/ipc/server.py",
    "src-ui/renderer",
)

REQUIRED_WORKSPACE_PATHS: tuple[str, ...] = (
    "governance_rule/governance_policy.py",
    "governance_rule/permission_directory/directory_authority.py",
)

REQUIRED_PYTHON_MODULES: dict[str, str] = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "pytest": "pytest",
    "pytest-asyncio": "pytest_asyncio",
    "python-dotenv": "dotenv",
    "imageio-ffmpeg": "imageio_ffmpeg",
    "pillow": "PIL",
    "pyinstaller": "PyInstaller",
    "websockets": "websockets",
}


def normalize_requirement_name(line: str) -> str | None:
    value = line.split("#", 1)[0].strip()
    if not value or value.startswith(("-r ", "--")):
        return None
    if " @ " in value:
        value = value.split(" @ ", 1)[0].strip()
    else:
        value = re.split(r"[<>=!~;\[\s]", value, maxsplit=1)[0].strip()
    return value.casefold().replace("_", "-") or None


def parse_requirement_names(requirements_path: Path) -> set[str]:
    try:
        lines = requirements_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {
        name
        for line in lines
        if (name := normalize_requirement_name(line)) is not None
    }


def default_python_executable(project_root: Path) -> Path:
    candidates = [
        project_root / ".venv" / "Scripts" / "python.exe",
        project_root / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def check_project_paths(project_root: Path) -> dict[str, Any]:
    checks = {
        rel_path: (project_root / rel_path).exists()
        for rel_path in REQUIRED_PROJECT_PATHS
    }
    workspace_root = project_root.parent
    checks.update(
        {
            rel_path: (workspace_root / rel_path).exists()
            for rel_path in REQUIRED_WORKSPACE_PATHS
        }
    )
    missing = [rel_path for rel_path, exists in checks.items() if not exists]
    return {
        "ok": not missing,
        "checks": checks,
        "missing": missing,
    }


def check_requirements_file(
    project_root: Path,
    required_modules: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    required_modules = required_modules or REQUIRED_PYTHON_MODULES
    requirements_path = project_root / "requirements.txt"
    names = parse_requirement_names(requirements_path)
    missing = [
        requirement
        for requirement in sorted(required_modules)
        if requirement not in names
    ]
    return {
        "ok": requirements_path.exists() and not missing,
        "exists": requirements_path.exists(),
        "path": str(requirements_path),
        "missing": missing,
        "declared_count": len(names),
    }


def check_python_modules(
    project_root: Path,
    python_executable: str | os.PathLike[str] | None = None,
    required_modules: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    required_modules = dict(required_modules or REQUIRED_PYTHON_MODULES)
    executable = Path(python_executable) if python_executable else default_python_executable(project_root)
    if not executable.exists():
        return {
            "ok": False,
            "executable": str(executable),
            "exists": False,
            "missing": sorted(required_modules),
            "modules": {},
            "error": "python executable not found",
        }

    probe = (
        "import importlib.util,json;"
        f"modules=json.loads({json.dumps(json.dumps(list(required_modules.values())))});"
        "print(json.dumps({name: importlib.util.find_spec(name) is not None for name in modules}, sort_keys=True))"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-c", probe],
            cwd=str(project_root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            **_background_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "executable": str(executable),
            "exists": True,
            "missing": sorted(required_modules),
            "modules": {},
            "error": str(exc),
        }

    module_results: dict[str, bool] = {}
    decode_error = ""
    if completed.returncode == 0:
        try:
            raw_results = json.loads(completed.stdout.strip() or "{}")
            module_results = {
                requirement: bool(raw_results.get(import_name))
                for requirement, import_name in required_modules.items()
            }
        except json.JSONDecodeError as exc:
            decode_error = str(exc)
            module_results = {
                requirement: False
                for requirement in required_modules
            }

    missing = [
        requirement
        for requirement in sorted(required_modules)
        if not module_results.get(requirement)
    ]
    if completed.returncode != 0 and not missing:
        missing = sorted(required_modules)

    return {
        "ok": completed.returncode == 0 and not missing,
        "executable": str(executable),
        "exists": True,
        "missing": missing,
        "modules": module_results,
        "error": completed.stderr.strip() or decode_error,
    }


def check_electron_runtime(project_root: Path) -> dict[str, Any]:
    electron_root = project_root / "node_modules" / "electron"
    dist_dir = electron_root / "dist"
    exe_path = dist_dir / "electron.exe"
    path_txt = electron_root / "path.txt"
    path_value = ""
    try:
        path_value = path_txt.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return {
        "ok": exe_path.exists() and path_txt.exists(),
        "electron_root": str(electron_root),
        "dist_dir": str(dist_dir),
        "exe_path": str(exe_path),
        "exe_exists": exe_path.exists(),
        "path_txt_exists": path_txt.exists(),
        "path_value": path_value,
    }


def check_node_environment(project_root: Path) -> dict[str, Any]:
    electron = check_electron_runtime(project_root)
    node_available = shutil.which("node") is not None
    npm_available = (
        shutil.which("npm.cmd") is not None
        or shutil.which("npm") is not None
    )
    checks = {
        "node_available": node_available,
        "npm_available": npm_available,
        "node_modules_exists": (project_root / "node_modules").exists(),
        "package_json_exists": (
            project_root / "main-system" / "package.json"
        ).exists(),
        "package_lock_exists": (project_root / "package-lock.json").exists(),
        "electron_runtime_ready": bool(electron["ok"]),
    }
    missing = [name for name, ok in checks.items() if not ok]
    return {
        "ok": not missing,
        "checks": checks,
        "missing": missing,
        "electron": electron,
    }


def _resolve_platform_tool_entry(
    project_root: Path,
    tool_dir: Path,
    manifest: Mapping[str, Any],
) -> Path:
    runtime = manifest.get("runtime")
    if isinstance(runtime, Mapping):
        runtime_entry = str(runtime.get("entry", "")).strip()
        if runtime_entry:
            return (tool_dir / runtime_entry).resolve()

    raw_entry = str(manifest.get("entry", "")).strip()
    if not raw_entry:
        return (tool_dir / "src" / "main.py").resolve()

    entry_path = project_root / raw_entry
    if entry_path.suffix == "":
        entry_path = entry_path.with_suffix(".py")
    return entry_path.resolve()


def check_independent_tools(project_root: Path) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    invalid_manifests: list[dict[str, str]] = []
    missing_entries: list[dict[str, str]] = []
    missing_executables: list[str] = []

    tool_directories = [
        path
        for path in sorted(project_root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "manifest.json").is_file()
    ]
    for host_dir in tuple(tool_directories):
        try:
            host_manifest = json.loads(
                (host_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        declarations = host_manifest.get("companion_tools")
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if not isinstance(declaration, Mapping):
                continue
            relative_path = str(declaration.get("path") or "").strip()
            candidate = (host_dir / relative_path).resolve()
            try:
                candidate.relative_to(host_dir.resolve())
            except ValueError:
                continue
            if (
                candidate.parent == host_dir.resolve()
                and (candidate / "manifest.json").is_file()
            ):
                tool_directories.append(candidate)

    for tool_dir in tool_directories:
        if not tool_dir.is_dir() or tool_dir.name.startswith("_"):
            continue
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            invalid_manifests.append({"tool": tool_dir.name, "error": str(exc)})
            continue
        if not isinstance(manifest, dict):
            invalid_manifests.append({"tool": tool_dir.name, "error": "manifest is not an object"})
            continue

        tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
        entry = _resolve_platform_tool_entry(project_root, tool_dir, manifest)
        executable = manifest.get("executable")
        has_executable = isinstance(executable, Mapping) and bool(
            str(executable.get("path") or executable.get("name") or "").strip()
        )
        distribution = manifest.get("distribution")
        lifecycle = manifest.get("lifecycle")
        explicitly_unpacked = isinstance(distribution, Mapping) and distribution.get("package") is False
        direct_load = isinstance(lifecycle, Mapping) and lifecycle.get("directLoad") is True
        requires_executable = not explicitly_unpacked and not direct_load
        if not entry.exists():
            missing_entries.append({"tool": tool_id, "entry": str(entry)})
        if requires_executable and not has_executable:
            missing_executables.append(tool_id)
        tools.append(
            {
                "id": tool_id,
                "entry": str(entry),
                "entry_exists": entry.exists(),
                "has_executable": has_executable,
                "requires_executable": requires_executable,
            }
        )

    ok = not invalid_manifests and not missing_entries and not missing_executables
    return {
        "ok": ok,
        "count": len(tools),
        "tools": tools,
        "invalid_manifests": invalid_manifests,
        "missing_entries": missing_entries,
        "missing_executables": missing_executables,
    }


def collect_environment_report(
    project_root: str | os.PathLike[str] | None = None,
    *,
    python_executable: str | os.PathLike[str] | None = None,
    required_python_modules: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(project_root or Path.cwd()).resolve()
    required_modules = required_python_modules or REQUIRED_PYTHON_MODULES
    paths = check_project_paths(root)
    requirements = check_requirements_file(root, required_modules)
    python = check_python_modules(root, python_executable, required_modules)
    node = check_node_environment(root)
    independent_tools = check_independent_tools(root.parent)

    failures: list[str] = []
    if not paths["ok"]:
        failures.extend(f"missing path: {path}" for path in paths["missing"])
    if not requirements["ok"]:
        if not requirements["exists"]:
            failures.append("requirements.txt not found")
        failures.extend(f"missing requirement: {name}" for name in requirements["missing"])
    if not python["ok"]:
        failures.extend(f"missing python module: {name}" for name in python["missing"])
        if python.get("error"):
            failures.append(f"python probe error: {python['error']}")
    if not node["ok"]:
        failures.extend(f"node environment issue: {name}" for name in node["missing"])
    if not independent_tools["ok"]:
        failures.extend(
            f"independent tool missing entry: {item['tool']}"
            for item in independent_tools["missing_entries"]
        )
        failures.extend(
            f"independent tool invalid manifest: {item['tool']}"
            for item in independent_tools["invalid_manifests"]
        )
        failures.extend(
            f"independent tool missing executable: {tool_id}"
            for tool_id in independent_tools["missing_executables"]
        )

    recommendations: list[str] = []
    if requirements["missing"]:
        recommendations.append("Add missing packages to requirements.txt and reinstall the virtual environment.")
    if python["missing"]:
        recommendations.append("Run .venv\\Scripts\\python.exe -m pip install -r requirements.txt.")
    if "node_modules_exists" in node["missing"]:
        recommendations.append("Run npm.cmd ci to restore Node dependencies.")
    if not node["electron"]["ok"]:
        recommendations.append("Run npm.cmd run doctor:fix to repair the local Electron runtime.")
    if independent_tools["missing_entries"]:
        recommendations.append("Fix independent tool runtime.entry paths before packaging.")

    ok = not failures
    return {
        "ok": ok,
        "status": "healthy" if ok else "degraded",
        "project_root": str(root),
        "paths": paths,
        "requirements": requirements,
        "python": python,
        "node": node,
        "independent_tools": independent_tools,
        "failures": failures,
        "recommendations": recommendations,
    }


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    safe_extract_zip(zip_path, target_dir)


def _electron_cache_roots() -> list[Path]:
    roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "electron" / "Cache")
    roots.append(Path.home() / ".cache" / "electron")
    return roots


def repair_electron_runtime(project_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    root = Path(project_root or Path.cwd()).resolve()
    before = check_electron_runtime(root)
    if before["ok"]:
        return {"ok": True, "changed": False, "method": "already_ready", "electron": before}

    electron_root = root / "node_modules" / "electron"
    package_path = electron_root / "package.json"
    installed_version_path = electron_root / "dist" / "version"
    try:
        package_version = str(json.loads(package_path.read_text(encoding="utf-8"))["version"]).lstrip("v")
        installed_version = installed_version_path.read_text(encoding="utf-8").strip().lstrip("v")
        if before["exe_exists"] and package_version == installed_version:
            (electron_root / "path.txt").write_text("electron.exe", encoding="utf-8", newline="\n")
            after_metadata_repair = check_electron_runtime(root)
            if after_metadata_repair["ok"]:
                return {
                    "ok": True,
                    "changed": True,
                    "method": "restore_electron_path_metadata",
                    "electron": after_metadata_repair,
                }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        pass

    install_script = root / "node_modules" / "electron" / "install.js"
    if install_script.exists() and shutil.which("node"):
        completed = subprocess.run(
            ["node", str(install_script)],
            cwd=str(root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            **_background_subprocess_kwargs(),
        )
        after_node_install = check_electron_runtime(root)
        if after_node_install["ok"]:
            return {
                "ok": True,
                "changed": True,
                "method": "electron_install_script",
                "output": completed.stdout,
                "electron": after_node_install,
            }

    cache_zips: list[Path] = []
    for cache_root in _electron_cache_roots():
        if cache_root.exists():
            cache_zips.extend(cache_root.rglob("electron-v*-win32-x64.zip"))
    cache_zips.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    dist_dir = electron_root / "dist"
    for zip_path in cache_zips:
        try:
            _safe_extract_zip(zip_path, dist_dir)
            (electron_root / "path.txt").write_text("electron.exe", encoding="utf-8", newline="\n")
        except (OSError, RuntimeError, zipfile.BadZipFile):
            continue
        after_cache = check_electron_runtime(root)
        if after_cache["ok"]:
            return {
                "ok": True,
                "changed": True,
                "method": "electron_cache_zip",
                "cache_zip": str(zip_path),
                "electron": after_cache,
            }

    return {
        "ok": False,
        "changed": False,
        "method": "unresolved",
        "electron": check_electron_runtime(root),
    }


def format_environment_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"GPTBridge environment: {report.get('status', 'unknown')}",
        f"Project root: {report.get('project_root', '')}",
        "",
        f"Python: {'OK' if report.get('python', {}).get('ok') else 'FAIL'}",
        f"Node/Electron: {'OK' if report.get('node', {}).get('ok') else 'FAIL'}",
        f"Independent tools: {'OK' if report.get('independent_tools', {}).get('ok') else 'FAIL'}",
    ]
    failures = list(report.get("failures") or [])
    recommendations = list(report.get("recommendations") or [])
    if failures:
        lines.append("")
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in failures)
    if recommendations:
        lines.append("")
        lines.append("Recommendations:")
        lines.extend(f"- {recommendation}" for recommendation in recommendations)
    return "\n".join(lines)
