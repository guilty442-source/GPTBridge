"""Environment doctor — facade.

This module provides the environment check and report functions.
Implementation details live in submodules:

  * :mod:`core.environment_doctor_constants` — constants, helpers.
  * :mod:`core.environment_doctor_tools` — independent tools.

Windows background subprocess no-window flag: CREATE_NO_WINDOW.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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
    if not required_modules and not OPTIONAL_PYTHON_MODULE_GROUPS:
        # Python runtime retired (B167/B38): nothing to probe — report
        # success without spawning or requiring a python executable.
        return {
            "ok": True,
            "executable": str(executable),
            "exists": executable.exists(),
            "missing": [],
            "modules": {},
            "optional_modules": {},
            "optional_missing": {},
            "error": "",
            "retired": True,
        }
    if not executable.exists():
        return {
            "ok": False,
            "executable": str(executable),
            "exists": False,
            "missing": sorted(required_modules),
            "modules": {},
            "error": "python executable not found",
        }

    if _is_current_interpreter(executable):
        # Same-interpreter fast path: find_spec in-process (~µs) instead of
        # spawning a second interpreter (~150ms) to ask the identical question.
        return _check_modules_in_process(executable, project_root, required_modules)

    probe = (
        "import importlib.util,json;"
        f"modules=json.loads({json.dumps(json.dumps(list(required_modules.values())))});"
        "print(json.dumps({name: importlib.util.find_spec(name) is not None for name in modules}, sort_keys=True))"
    )
    try:
        completed = _run_module_probe(executable, project_root, probe)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "executable": str(executable),
            "exists": True,
            "missing": sorted(required_modules),
            "modules": {},
            "error": str(exc),
        }

    module_results, decode_error, missing = _required_module_results(
        completed, required_modules
    )
    optional_results, optional_missing = _probe_optional_modules(
        executable, project_root
    )

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


def _is_current_interpreter(executable: Path) -> bool:
    """True when the probe target is this process's own interpreter."""
    try:
        return Path(executable).resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _check_modules_in_process(
    executable: Path,
    project_root: Path,
    required_modules: Mapping[str, str],
) -> dict[str, Any]:
    """In-process variant of the module probes (same-interpreter only).

    Mirrors ``python -c`` semantics by resolving against ``project_root``
    (the subprocess probe runs with ``cwd=project_root``).
    """
    root_str = os.fspath(project_root)
    inserted = root_str not in sys.path
    if inserted:
        sys.path.insert(0, root_str)
    try:
        module_results = {
            requirement: importlib.util.find_spec(import_name) is not None
            for requirement, import_name in required_modules.items()
        }
        optional_results: dict[str, dict[str, bool]] = {
            group: {
                name: importlib.util.find_spec(import_name) is not None
                for name, import_name in modules.items()
            }
            for group, modules in OPTIONAL_PYTHON_MODULE_GROUPS.items()
        }
    finally:
        if inserted:
            try:
                sys.path.remove(root_str)
            except ValueError:
                pass
    missing = sorted(
        requirement for requirement, present in module_results.items() if not present
    )
    optional_missing = {
        group: sorted(req for req, present in modules.items() if not present)
        for group, modules in optional_results.items()
    }
    return {
        "ok": not missing,
        "executable": str(executable),
        "exists": True,
        "missing": missing,
        "modules": module_results,
        "optional_modules": optional_results,
        "optional_missing": optional_missing,
        "error": "",
    }


def _run_module_probe(executable: Path, project_root: Path, probe: str):
    return subprocess.run(
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


def _required_module_results(
    completed, required_modules: Mapping[str, str]
) -> tuple[dict[str, bool], str, list[str]]:
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
    return module_results, decode_error, missing


def _probe_optional_modules(
    executable: Path, project_root: Path
) -> tuple[dict[str, dict[str, bool]], dict[str, list[str]]]:
    optional_probe = (
        "import importlib.util,json;"
        f"groups=json.loads({json.dumps(json.dumps({g: list(m.values()) for g, m in OPTIONAL_PYTHON_MODULE_GROUPS.items()}))});"
        "print(json.dumps({g: {n: importlib.util.find_spec(n) is not None for n in names} for g, names in groups.items()}, sort_keys=True))"
    )
    optional_results: dict[str, dict[str, bool]] = {}
    try:
        opt_completed = _run_module_probe(executable, project_root, optional_probe)
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
    return optional_results, optional_missing


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
    external = check_external_tools()
    independent_tools = check_independent_tools(root.parent)

    failures = _collect_failures(
        paths, requirements, python, external, independent_tools
    )
    recommendations = _collect_recommendations(
        requirements, python, external, independent_tools
    )

    ok = not failures
    return {
        "ok": ok,
        "status": "healthy" if ok else "degraded",
        "project_root": str(root),
        "paths": paths,
        "requirements": requirements,
        "python": python,
        "external_tools": external,
        "independent_tools": independent_tools,
        "failures": failures,
        "recommendations": recommendations,
    }


def _collect_failures(
    paths: dict[str, Any],
    requirements: dict[str, Any],
    python: dict[str, Any],
    external: dict[str, Any],
    independent_tools: dict[str, Any],
) -> list[str]:
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
    return failures


def _collect_recommendations(
    requirements: dict[str, Any],
    python: dict[str, Any],
    external: dict[str, Any],
    independent_tools: dict[str, Any],
) -> list[str]:
    recommendations: list[str] = []
    if requirements["missing"]:
        recommendations.append("Reconcile requirements declarations with the governed toolchain manifest.")
    if python["missing"]:
        recommendations.append("Python runtime is retired (B167/B38); route the capability to its governed native replacement.")
    if external["missing"]:
        recommendations.append(
            "Install missing external tools: " + ", ".join(external["missing"])
        )
    for group, missing_reqs in (python.get("optional_missing") or {}).items():
        if missing_reqs:
            recommendations.append(
                f"Optional group '{group}' declared but absent — "
                "acquire through the governed Winget channel."
            )
    if independent_tools["missing_entries"]:
        recommendations.append("Fix independent tool runtime.entry paths before packaging.")
    return recommendations


def format_environment_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"GPTBridge environment: {report.get('status', 'unknown')}",
        f"Project root: {report.get('project_root', '')}",
        "",
        f"Python: {'OK' if report.get('python', {}).get('ok') else 'FAIL'}",
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
    "check_external_tools",
    "check_independent_tools",
    "collect_environment_report",
    "format_environment_report",
]
