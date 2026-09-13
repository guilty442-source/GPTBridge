"""Environment doctor — facade.

This module provides the environment check and report functions.
Implementation details live in submodules:

  * :mod:`core.environment_doctor_constants` — constants, helpers.
  * :mod:`core.environment_doctor_tools` — independent tools, repair.

Windows background subprocess no-window flag: CREATE_NO_WINDOW.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .environment_doctor_constants import (
    _background_subprocess_kwargs,
    REQUIRED_PROJECT_PATHS,
    REQUIRED_WORKSPACE_PATHS,
    REQUIRED_PYTHON_MODULES,
    OPTIONAL_PYTHON_MODULE_GROUPS,
    default_python_executable,
    parse_requirement_names,
)
from .environment_doctor_tools import (
    check_external_tools,
    check_independent_tools,
    repair_electron_runtime,
)


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
    required_modules = dict(required_modules or REQUIRED_PYTHON_MODULES)
    declared_modules = dict(required_modules)
    for group in OPTIONAL_PYTHON_MODULE_GROUPS.values():
        declared_modules.update(group)
    requirements_path = project_root / "requirements.txt"
    names = parse_requirement_names(requirements_path)
    missing = [
        requirement
        for requirement in sorted(declared_modules)
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

    optional_probe = (
        "import importlib.util,json;"
        f"groups=json.loads({json.dumps(json.dumps({g: list(m.values()) for g, m in OPTIONAL_PYTHON_MODULE_GROUPS.items()}))});"
        "print(json.dumps({g: {n: importlib.util.find_spec(n) is not None for n in names} for g, names in groups.items()}, sort_keys=True))"
    )
    optional_results: dict[str, dict[str, bool]] = {}
    try:
        opt_completed = subprocess.run(
            [str(executable), "-c", optional_probe],
            cwd=str(project_root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            **_background_subprocess_kwargs(),
        )
        if opt_completed.returncode == 0:
            optional_results = {
                group: {
                    req: bool(modules.get(import_name))
                    for req, import_name in OPTIONAL_PYTHON_MODULE_GROUPS[group].items()
                }
                for group, modules in json.loads(
                    opt_completed.stdout.strip() or "{}"
                ).items()
            }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        optional_results = {}

    optional_missing = {
        group: sorted(
            req for req, present in modules.items() if not present
        )
        for group, modules in optional_results.items()
    }

    return {
        "ok": completed.returncode == 0 and not missing,
        "executable": str(executable),
        "exists": True,
        "missing": missing,
        "modules": module_results,
        "optional_modules": optional_results,
        "optional_missing": optional_missing,
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
            project_root / "package.json"
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
    external = check_external_tools()
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
    if not external["ok"]:
        failures.extend(f"missing external tool: {name}" for name in external["missing"])
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
    if external["missing"]:
        recommendations.append(
            "Install missing external tools: " + ", ".join(external["missing"])
        )
    for group, missing_reqs in (python.get("optional_missing") or {}).items():
        if missing_reqs:
            recommendations.append(
                f"Optional group '{group}' not installed: "
                + ", ".join(missing_reqs)
                + f" — pip install -e .[{group}] when needed."
            )
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
        "external_tools": external,
        "independent_tools": independent_tools,
        "failures": failures,
        "recommendations": recommendations,
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


__all__ = [
    "check_project_paths",
    "check_requirements_file",
    "check_python_modules",
    "check_electron_runtime",
    "check_node_environment",
    "check_external_tools",
    "check_independent_tools",
    "collect_environment_report",
    "format_environment_report",
    "repair_electron_runtime",
]
