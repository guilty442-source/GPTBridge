"""Python release import-origin validation (release isolation).

Purpose: a backend started from a release must not silently load official
Python modules from the development source tree.  This module probes the
release interpreter in a subprocess (isolated from this process) and checks
every resolved module origin against the release contract using normalized
Windows-safe path containment (case-folded, junction/symlink-resolved,
``commonpath`` — never plain string ``startswith``).

Contract classes (see ``shared_layer/database/release_manifest.py``):
RELEASE_CODE, RELEASE_DEPENDENCY, SHARED_RUNTIME, PERSISTENT_DATA, SECRET,
DEVELOPMENT_ONLY.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROBE_SCRIPT = r"""
import importlib.metadata as md
import importlib.util
import json
import os
import sys

config = json.loads(sys.stdin.read() or "{}")
for entry in reversed(list(config.get("extra_paths", []))):
    sys.path.insert(0, entry)
out = {
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "cwd": os.getcwd(),
    "version": list(sys.version_info[:3]),
    "modules": {},
}
for name in config.get("modules", []):
    entry = {"origin": None, "version": None, "native": False, "error": None}
    try:
        spec = importlib.util.find_spec(name)
        if spec is None:
            entry["error"] = "MODULE_NOT_FOUND"
        else:
            origin = getattr(spec, "origin", None)
            entry["origin"] = origin
            entry["native"] = bool(
                origin and str(origin).lower().endswith((".pyd", ".so", ".dll"))
            )
            try:
                entry["version"] = md.version(name)
            except Exception:
                entry["version"] = None
    except Exception as error:  # noqa: BLE001 — probe reports, never raises
        entry["error"] = f"{error.__class__.__name__}:{error}"
    out["modules"][name] = entry
print(json.dumps(out))
"""


class ReleaseDependencyError(RuntimeError):
    """Fail-closed denial while validating release dependencies."""

    failure_code = "RELEASE_DEPENDENCY_VALIDATION_FAILED"


def normalize_path(value: str | os.PathLike[str] | None) -> str:
    """Case-folded, resolved, normalized path for containment comparison.

    ``realpath`` resolves symbolic links and NTFS junctions; ``normcase``
    folds case on Windows; ``normpath`` removes ``.``/``..`` segments.
    """
    if not value:
        return ""
    text = os.fspath(value)
    if not text.strip():
        return ""
    absolute = os.path.abspath(text)
    return os.path.normcase(os.path.normpath(os.path.realpath(absolute)))


def path_is_within(
    child: str | os.PathLike[str] | None,
    root: str | os.PathLike[str] | None,
) -> bool:
    """True when ``child`` resolves at or below ``root``.

    Uses ``commonpath`` on normalized paths so different drives, case
    differences, junctions and symbolic links are handled correctly and a
    prefix string such as ``C:\\rel`` never matches ``C:\\release-other``.
    """
    normalized_child = normalize_path(child)
    normalized_root = normalize_path(root)
    if not normalized_child or not normalized_root:
        return False
    try:
        return os.path.commonpath([normalized_child, normalized_root]) == normalized_root
    except ValueError:
        return False


def probe_python_import_origins(
    python_executable: str | os.PathLike[str],
    modules: Sequence[str],
    *,
    extra_paths: Iterable[str | os.PathLike[str]] = (),
    cwd: str | os.PathLike[str] | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Run the release interpreter and report resolved module origins.

    The probe runs with ``-I`` so ``PYTHONPATH`` and the user site directory
    cannot leak into the result; ``extra_paths`` models the release's own
    ``sys.path`` layout explicitly.
    """
    config = {
        "extra_paths": [os.fspath(path) for path in extra_paths],
        "modules": list(modules),
    }
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [os.fspath(python_executable), "-I", "-X", "utf8", "-c", PROBE_SCRIPT],
            input=json.dumps(config),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=os.fspath(cwd) if cwd else None,
            env=environment,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseDependencyError(f"python probe failed: {error}") from error
    if result.returncode != 0:
        raise ReleaseDependencyError(
            "python probe rejected the release interpreter: "
            f"{result.stderr.strip()[:400] or 'unknown error'}"
        )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as error:
        raise ReleaseDependencyError("python probe returned unreadable output") from error
    if not isinstance(payload, dict):
        raise ReleaseDependencyError("python probe returned a non-object result")
    return payload


def _version_tuple(value: object) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(value or "").split("."):
        digits = "".join(character for character in piece if character.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def _version_matches(actual: str | None, expected: Mapping[str, Any]) -> bool:
    actual_tuple = _version_tuple(actual)
    if not actual_tuple:
        return False
    minimum = expected.get("min")
    if minimum is not None and actual_tuple < tuple(int(v) for v in minimum):
        return False
    maximum_exclusive = expected.get("max_exclusive")
    if maximum_exclusive is not None and actual_tuple >= tuple(
        int(v) for v in maximum_exclusive
    ):
        return False
    return True


def _origin_class(
    origin: str | None,
    *,
    release_root: str,
    source_root: str | None,
    shared_root: str | None,
) -> str:
    if not origin:
        return "MISSING"
    if release_root and path_is_within(origin, release_root):
        return "RELEASE"
    if shared_root and path_is_within(origin, shared_root):
        return "SHARED"
    if source_root and path_is_within(origin, source_root):
        return "SOURCE_TREE"
    return "EXTERNAL"


def validate_module_origins(
    contract: Mapping[str, Any],
    probe: Mapping[str, Any],
    *,
    release_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None = None,
    shared_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Apply the dependency-class rules to a probe result."""
    errors: list[str] = []
    details: dict[str, dict[str, Any]] = {}
    release_text = os.fspath(release_root)
    source_text = os.fspath(source_root) if source_root else None
    shared_text = os.fspath(shared_root) if shared_root else None

    python_contract = contract.get("python") or {}
    version_range = python_contract.get("version_range")
    if version_range:
        if not _version_matches(".".join(str(v) for v in probe.get("version", [])), version_range):
            errors.append(
                "PYTHON_VERSION_MISMATCH:"
                f"runtime={'/'.join(str(v) for v in probe.get('version', []))}"
            )

    modules = contract.get("modules") or []
    probed = probe.get("modules") or {}
    for entry in modules:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("module") or "")
        dependency_class = str(entry.get("class") or "")
        required = bool(entry.get("required"))
        observed = probed.get(name) or {}
        origin = observed.get("origin")
        error = observed.get("error")
        origin_kind = _origin_class(
            origin,
            release_root=release_text,
            source_root=source_text,
            shared_root=shared_text,
        )
        details[name] = {
            "class": dependency_class,
            "origin": origin,
            "origin_kind": origin_kind,
            "version": observed.get("version"),
            "native": bool(observed.get("native")),
        }
        if error or not origin:
            if required:
                errors.append(f"REQUIRED_MODULE_MISSING:{name}")
            continue

        allowed_roots = [
            os.path.join(release_text, str(root)) if not os.path.isabs(str(root)) else str(root)
            for root in (entry.get("allowed_roots") or [])
        ]
        if allowed_roots and not any(path_is_within(origin, root) for root in allowed_roots):
            errors.append(f"ORIGIN_OUTSIDE_ALLOWED_ROOTS:{name}:{origin}")

        if dependency_class == "RELEASE_CODE":
            if origin_kind == "SOURCE_TREE":
                errors.append(f"SOURCE_TREE_LEAK:{name}:{origin}")
            elif origin_kind not in {"RELEASE", "SHARED"}:
                errors.append(f"RELEASE_CODE_ORIGIN_INVALID:{name}:{origin}")
        elif dependency_class == "RELEASE_DEPENDENCY":
            if origin_kind == "SOURCE_TREE":
                errors.append(f"SOURCE_TREE_LEAK:{name}:{origin}")
            expected_version = entry.get("version")
            if expected_version and str(observed.get("version") or "") != str(expected_version):
                errors.append(
                    f"DEPENDENCY_VERSION_MISMATCH:{name}:"
                    f"expected={expected_version}:actual={observed.get('version')}"
                )
            elif entry.get("version_range") and not _version_matches(
                observed.get("version"), entry["version_range"]
            ):
                errors.append(
                    f"DEPENDENCY_VERSION_MISMATCH:{name}:actual={observed.get('version')}"
                )
        elif dependency_class == "SHARED_RUNTIME":
            if shared_text is None:
                errors.append(f"SHARED_RUNTIME_UNDECLARED:{name}")
            elif origin_kind != "SHARED":
                errors.append(f"SHARED_RUNTIME_SOURCE_TREE_GAP:{name}:{origin}")
        elif dependency_class == "DEVELOPMENT_ONLY":
            errors.append(f"DEVELOPMENT_ONLY_LOADED:{name}:{origin}")
        elif dependency_class in {"PERSISTENT_DATA", "SECRET"}:
            errors.append(f"CLASS_NOT_IMPORTABLE:{name}:{dependency_class}")

        if observed.get("native") and origin_kind not in {"RELEASE", "SHARED"}:
            errors.append(f"NATIVE_EXTENSION_ORIGIN_INVALID:{name}:{origin}")

    return {"ok": not errors, "errors": errors, "modules": details}


def validate_release_python_origins(
    contract: Mapping[str, Any],
    *,
    python_executable: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None = None,
    shared_root: str | os.PathLike[str] | None = None,
    extra_paths: Iterable[str | os.PathLike[str]] = (),
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Probe the release interpreter and validate every module origin."""
    modules = [
        str(entry.get("module"))
        for entry in (contract.get("modules") or [])
        if isinstance(entry, Mapping) and entry.get("module")
    ]
    try:
        probe = probe_python_import_origins(
            python_executable, modules, extra_paths=extra_paths, cwd=cwd
        )
    except ReleaseDependencyError as error:
        return {
            "ok": False,
            "errors": [f"PROBE_FAILED:{error}"],
            "modules": {},
            "probe": {},
        }
    result = validate_module_origins(
        contract,
        probe,
        release_root=release_root,
        source_root=source_root,
        shared_root=shared_root,
    )
    result["probe"] = {
        "executable": probe.get("executable"),
        "prefix": probe.get("prefix"),
        "version": probe.get("version"),
    }
    return result


__all__ = [
    "ReleaseDependencyError",
    "normalize_path",
    "path_is_within",
    "probe_python_import_origins",
    "validate_module_origins",
    "validate_release_python_origins",
]
