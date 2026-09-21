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
import hashlib
import importlib.metadata as md
import importlib.util
import json
import os
import platform
import site
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
    "arch": platform.machine(),
    "bits": 64 if sys.maxsize > 2**32 else 32,
    "isolated": bool(sys.flags.isolated),
    "env_pythonpath": os.environ.get("PYTHONPATH", ""),
    "user_site_enabled": bool(site.ENABLE_USER_SITE),
    "user_site": site.getusersitepackages(),
    "site_packages": list(site.getsitepackages()),
    "sys_path": list(sys.path),
    "pyvenv_cfg": None,
    "modules": {},
}
venv_cfg = os.path.join(sys.prefix, "pyvenv.cfg")
if os.path.isfile(venv_cfg):
    with open(venv_cfg, "rb") as handle:
        raw = handle.read()
    fields = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    out["pyvenv_cfg"] = {
        "path": venv_cfg,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "fields": fields,
    }
if config.get("include_distributions"):
    out["distributions"] = sorted(
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in md.distributions()
        if dist.metadata["Name"]
    )
out["attributes"] = {}
for name, reference in (config.get("attributes") or {}).items():
    try:
        module_name, _, attribute = reference.partition(":")
        module = __import__(module_name, fromlist=[attribute or "*"])
        out["attributes"][name] = getattr(module, attribute) if attribute else None
    except Exception as error:  # noqa: BLE001 — probe reports, never raises
        out["attributes"][name] = f"ERROR:{error.__class__.__name__}:{error}"
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
            if origin_kind == "SOURCE_TREE":
                errors.append(f"SHARED_RUNTIME_SOURCE_TREE_GAP:{name}:{origin}")
            elif shared_text is not None and not path_is_within(origin, shared_text):
                errors.append(f"SHARED_RUNTIME_ORIGIN_INVALID:{name}:{origin}")
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


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pe_machine(path: str | os.PathLike[str]) -> str | None:
    """Machine type from a PE header (``AMD64`` / ``x86`` / ``ARM64``)."""
    try:
        with open(os.fspath(path), "rb") as handle:
            if handle.read(2) != b"MZ":
                return None
            handle.seek(0x3C)
            offset = int.from_bytes(handle.read(4), "little")
            handle.seek(offset)
            if handle.read(4) != b"PE\x00\x00":
                return None
            machine = int.from_bytes(handle.read(2), "little")
    except (OSError, ValueError):
        return None
    return {0x8664: "AMD64", 0x14C: "x86", 0xAA64: "ARM64"}.get(machine, hex(machine))


def probe_runtime_environment(
    python_executable: str | os.PathLike[str],
    *,
    modules: Sequence[str] = (),
    attributes: Mapping[str, str] | None = None,
    extra_paths: Iterable[str | os.PathLike[str]] = (),
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    isolated: bool = False,
    include_distributions: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Probe the release interpreter's environment (pollution observable).

    ``isolated=False`` (default) reproduces the backend's real startup so
    ``PYTHONPATH`` / user-site pollution is visible; ``isolated=True`` adds
    ``-I`` and a sanitized environment for comparison runs.
    """
    config = {
        "extra_paths": [os.fspath(path) for path in extra_paths],
        "modules": list(modules),
        "attributes": dict(attributes or {}),
        "include_distributions": bool(include_distributions),
    }
    environment = dict(env) if env is not None else dict(os.environ)
    arguments = [os.fspath(python_executable)]
    if isolated:
        environment.pop("PYTHONPATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        arguments.append("-I")
    arguments += ["-X", "utf8", "-c", PROBE_SCRIPT]
    try:
        result = subprocess.run(
            arguments,
            input=json.dumps(config),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=os.fspath(cwd) if cwd else None,
            env=environment,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseDependencyError(f"environment probe failed: {error}") from error
    if result.returncode != 0:
        raise ReleaseDependencyError(
            "environment probe rejected the interpreter: "
            f"{result.stderr.strip()[:400] or 'unknown error'}"
        )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as error:
        raise ReleaseDependencyError("environment probe returned unreadable output") from error
    if not isinstance(payload, dict):
        raise ReleaseDependencyError("environment probe returned a non-object result")
    return payload


def validate_runtime_environment(
    contract: Mapping[str, Any],
    probe: Mapping[str, Any],
    *,
    release_root: str | os.PathLike[str],
    allowed_dependency_roots: Iterable[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """Validate interpreter identity, venv markers and pollution policy."""
    errors: list[str] = []
    environment_contract = contract.get("runtime_environment") or {}
    python_contract = environment_contract.get("python") or {}
    version = ".".join(str(part) for part in probe.get("version", []))
    if python_contract.get("version") and version != str(python_contract["version"]):
        errors.append(f"PYTHON_VERSION_MISMATCH:{version}")
    elif python_contract.get("version_range") and not _version_matches(
        version, python_contract["version_range"]
    ):
        errors.append(f"PYTHON_VERSION_MISMATCH:{version}")
    if python_contract.get("arch") and str(probe.get("arch", "")).upper() != str(
        python_contract["arch"]
    ).upper():
        errors.append(f"PYTHON_ARCH_MISMATCH:{probe.get('arch')}")
    if python_contract.get("bits") and int(probe.get("bits", 0)) != int(
        python_contract["bits"]
    ):
        errors.append(f"PYTHON_BITS_MISMATCH:{probe.get('bits')}")

    venv_contract = environment_contract.get("venv") or {}
    config = probe.get("pyvenv_cfg")
    if venv_contract.get("required") and not config:
        errors.append("VENV_MARKER_MISSING")
    if config:
        fields = config.get("fields") or {}
        include_system = str(
            fields.get("include-system-site-packages", "")
        ).strip().lower() == "true"
        if venv_contract.get("include_system_site_packages") is False and include_system:
            errors.append("SYSTEM_SITE_PACKAGES_ENABLED")
        expected_hash = venv_contract.get("pyvenv_cfg_sha256")
        if expected_hash and str(config.get("sha256")) != str(expected_hash):
            errors.append("VENV_CONFIG_HASH_MISMATCH")
        if venv_contract.get("require_built_at_final_location"):
            command = str(fields.get("command", "")).strip()
            original = command.split()[-1] if command else ""
            if original and normalize_path(original) != normalize_path(probe.get("prefix")):
                errors.append(f"VENV_NOT_BUILT_AT_FINAL_LOCATION:{original}")

    if venv_contract.get("user_site") == "forbidden":
        if probe.get("user_site_enabled"):
            errors.append("USER_SITE_ENABLED")
        user_site = str(probe.get("user_site") or "")
        if user_site and any(
            normalize_path(entry) == normalize_path(user_site)
            for entry in probe.get("sys_path", [])
        ):
            errors.append(f"USER_SITE_POLLUTION:{user_site}")
    if venv_contract.get("pythonpath") == "forbidden" and str(
        probe.get("env_pythonpath") or ""
    ).strip():
        errors.append(f"PYTHONPATH_POLLUTION:{probe.get('env_pythonpath')}")

    roots = [release_root, *allowed_dependency_roots]
    for site_path in probe.get("site_packages") or []:
        if not any(path_is_within(site_path, root) for root in roots):
            errors.append(f"SITE_PACKAGES_OUTSIDE_RELEASE:{site_path}")

    return {
        "ok": not errors,
        "errors": errors,
        "python": {
            "version": version,
            "arch": probe.get("arch"),
            "bits": probe.get("bits"),
            "prefix": probe.get("prefix"),
        },
    }


def validate_dependency_lock(
    contract: Mapping[str, Any],
    probe: Mapping[str, Any],
) -> dict[str, Any]:
    """Dependency lock identity must match the installed distribution set."""
    import hashlib

    lock_contract = contract.get("dependency_lock") or {}
    expected = lock_contract.get("identity")
    if not expected:
        return {"ok": True, "errors": [], "identity": None}
    distributions = probe.get("distributions")
    if distributions is None:
        return {"ok": False, "errors": ["DISTRIBUTIONS_NOT_PROBED"], "identity": None}
    identity = hashlib.sha256(
        "\n".join(str(item) for item in distributions).encode("utf-8")
    ).hexdigest()
    errors: list[str] = []
    if identity != str(expected):
        errors.append(f"DEPENDENCY_LOCK_MISMATCH:{identity}")
    if lock_contract.get("entries") and len(distributions) != int(lock_contract["entries"]):
        errors.append(
            f"DEPENDENCY_LOCK_ENTRY_COUNT_MISMATCH:{len(distributions)}"
        )
    return {"ok": not errors, "errors": errors, "identity": identity}


def native_extension_errors(
    contract: Mapping[str, Any],
    *,
    release_root: str | os.PathLike[str],
) -> list[str]:
    """Required native extensions / DLLs: presence, architecture, ABI tag."""
    errors: list[str] = []
    root = Path(os.fspath(release_root))
    for entry in contract.get("native_extensions") or []:
        if not isinstance(entry, Mapping):
            errors.append("NATIVE_EXTENSION_ENTRY_INVALID")
            continue
        relative = str(entry.get("file") or "")
        path = root / relative
        if not relative or not path.is_file():
            errors.append(f"NATIVE_EXTENSION_MISSING:{relative}")
            continue
        expected_machine = entry.get("machine")
        machine = pe_machine(path)
        if expected_machine and machine and machine.upper() != str(expected_machine).upper():
            errors.append(f"NATIVE_ARCH_MISMATCH:{relative}:{machine}")
        expected_abi = str(entry.get("abi") or "").lower()
        if expected_abi and f".{expected_abi}-" not in path.name.lower():
            errors.append(f"NATIVE_ABI_MISMATCH:{relative}:{expected_abi}")
    for relative in contract.get("required_dlls") or []:
        if not (root / str(relative)).is_file():
            errors.append(f"NATIVE_DLL_MISSING:{relative}")
    return errors


def validate_forbidden_release_content(
    contract: Mapping[str, Any],
    release_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Secrets and official-authority data must never be packaged."""
    patterns = [
        str(pattern).replace("\\", "/").lower().lstrip("*")
        for pattern in (contract.get("forbidden_content") or [])
        if str(pattern).strip()
    ]
    errors: list[str] = []
    root = Path(os.fspath(release_root))
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix().lower()
            for suffix in patterns:
                if suffix and relative.endswith(suffix):
                    errors.append(f"FORBIDDEN_CONTENT_PACKAGED:{suffix}:{relative}")
                    break
    return {"ok": not errors, "errors": errors}


def validate_shared_layer_classification(
    contract: Mapping[str, Any],
    *,
    code_roots: Iterable[str | os.PathLike[str]] = (),
) -> list[str]:
    """Shared-layer responsibilities must be classified with evidence."""
    allowed = {
        "RELEASE_DEPENDENCY",
        "SHARED_SERVICE",
        "PERSISTENT_STATE",
        "RUNTIME_CONTRACT",
        "DEVELOPMENT_ONLY",
    }
    entries = contract.get("shared_layer_classification")
    if not isinstance(entries, list) or not entries:
        return ["SHARED_LAYER_CLASSIFICATION_MISSING"]
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            errors.append("SHARED_LAYER_ENTRY_INVALID")
            continue
        path = str(entry.get("path") or "")
        dependency_class = str(entry.get("class") or "")
        evidence = str(entry.get("evidence") or "")
        if dependency_class not in allowed:
            errors.append(f"SHARED_LAYER_CLASS_UNKNOWN:{path}:{dependency_class}")
        if not path or not evidence:
            errors.append(f"SHARED_LAYER_EVIDENCE_MISSING:{path}")
        if dependency_class in {"SHARED_SERVICE", "PERSISTENT_STATE"} and any(
            path_is_within(path, root) for root in code_roots
        ):
            errors.append(f"SHARED_LAYER_SERVICE_AS_CODE:{path}")
    return errors


def validate_governance_references(
    contract: Mapping[str, Any],
    *,
    codex_path: str | os.PathLike[str],
) -> list[str]:
    """Codex identity/version/hash and contract identities must match.

    Reads the official codex read-only; it never seals, mutates or replaces
    it, and it is not a substitute for the governed codex validation
    (``python -m governance_rule.execution.audit``).
    """
    import hashlib
    import sqlite3

    references = contract.get("governance_references") or {}
    errors: list[str] = []
    path = Path(os.fspath(codex_path))
    if not path.is_file():
        return ["CODEX_FILE_MISSING"]
    expected_hash = references.get("codex_sha256")
    if expected_hash and _sha256_file(path) != str(expected_hash):
        errors.append("CODEX_HASH_MISMATCH")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        from governance_rule.execution.codex_update_validation import (
            foreign_key_violations,
            validate_database_integrity,
        )

        baseline = tuple(foreign_key_violations(connection))
        for finding in validate_database_integrity(
            connection, baseline_violations=baseline
        ):
            errors.append(f"CODEX_INTEGRITY_FAILED:{finding}")
        metadata = dict(connection.execute("select key, value from metadata"))
        version = metadata.get("codex_version")
        if references.get("codex_version") and version != references["codex_version"]:
            errors.append(f"CODEX_VERSION_MISMATCH:{version}")
        for table, key, code in (
            (
                "identity_authentication_contract",
                "governance_runtime_contract_version",
                "GOVERNANCE_CONTRACT_INCOMPATIBLE",
            ),
            (
                "sql_session_binding_contract",
                "permission_contract_version",
                "PERMISSION_CONTRACT_INCOMPATIBLE",
            ),
        ):
            row = connection.execute(
                f"select version_identity from {table} limit 1"
            ).fetchone()
            actual = row[0] if row else None
            if references.get(key) and actual != references[key]:
                errors.append(f"{code}:{key}:{actual}")
        sovereigns = sorted(
            str(row[0]) for row in connection.execute("select sovereign_id from sovereigns")
        )
        identity = hashlib.sha256("\n".join(sovereigns).encode("utf-8")).hexdigest()
        if references.get("sovereign_registry_identity") and identity != references[
            "sovereign_registry_identity"
        ]:
            errors.append("SOVEREIGN_REGISTRY_MISMATCH")
    except sqlite3.Error as error:
        errors.append(f"CODEX_READ_FAILED:{error}")
    finally:
        connection.close()
    return errors


def validate_shared_layer_contract(
    contract: Mapping[str, Any],
    probe: Mapping[str, Any],
) -> list[str]:
    """Shared-layer contract version must match the declared compatible set."""
    section = contract.get("shared_layer_contract") or {}
    version_key = str(section.get("version_key") or "")
    if not version_key:
        return []
    observed = (probe.get("attributes") or {}).get("shared_layer_contract_version")
    if observed is None or isinstance(observed, str) and observed.startswith("ERROR:"):
        return [f"SHARED_LAYER_CONTRACT_UNREADABLE:{observed or 'missing'}"]
    expected = section.get("version")
    compatible = [str(item) for item in (section.get("compatible_versions") or [])]
    if expected is not None and str(observed) != str(expected):
        return [f"SHARED_LAYER_CONTRACT_INCOMPATIBLE:{observed}"]
    if compatible and str(observed) not in compatible:
        return [f"SHARED_LAYER_CONTRACT_INCOMPATIBLE:{observed}"]
    return []


def validate_service_topology(contract: Mapping[str, Any]) -> list[str]:
    """A release connects to the existing shared service; never duplicates it."""
    topology = contract.get("service_topology") or {}
    errors: list[str] = []
    shared = topology.get("shared_layer_service") or {}
    if shared:
        if str(shared.get("mode") or "") not in {
            "connect-to-existing",
            "external-existing",
        }:
            errors.append(f"SHARED_LAYER_SERVICE_MODE_INVALID:{shared.get('mode')}")
        if shared.get("duplicate_instance_allowed") is True:
            errors.append("SHARED_SERVICE_DUPLICATION_FORBIDDEN")
    governance = topology.get("governance_authority") or {}
    if governance:
        if str(governance.get("mode") or "") != "reference-existing":
            errors.append(
                f"GOVERNANCE_AUTHORITY_MODE_INVALID:{governance.get('mode')}"
            )
        if governance.get("duplicate_authority_allowed") is True:
            errors.append("GOVERNANCE_AUTHORITY_DUPLICATION_FORBIDDEN")
    return errors


def validate_governance_dependencies(
    contract: Mapping[str, Any],
    *,
    release_root: str | os.PathLike[str],
    probe: Mapping[str, Any],
) -> list[str]:
    """Required governance code / codex / permission paths must be present."""
    errors: list[str] = []
    root = Path(os.fspath(release_root))
    probed = probe.get("modules") or {}
    for entry in contract.get("governance_dependencies") or []:
        if not isinstance(entry, Mapping) or not entry.get("required"):
            continue
        dependency = str(entry.get("dependency") or "")
        kind = str(entry.get("kind") or "")
        if not dependency:
            errors.append("GOVERNANCE_DEPENDENCY_INVALID")
            continue
        if kind == "module":
            observed = probed.get(dependency) or {}
            if not observed.get("origin"):
                errors.append(f"GOVERNANCE_DEPENDENCY_MISSING:{dependency}")
        else:
            path = Path(dependency)
            if not path.is_absolute():
                path = root / dependency
            if not path.exists():
                errors.append(f"GOVERNANCE_DEPENDENCY_MISSING:{dependency}")
    return errors


def validate_official_state_separation(
    contract: Mapping[str, Any],
    *,
    release_root: str | os.PathLike[str],
    official_root: str | os.PathLike[str],
) -> list[str]:
    """Official authority state must live outside the release payload."""
    errors: list[str] = []
    root = Path(os.fspath(official_root))
    for raw in contract.get("official_state_paths") or []:
        path = Path(str(raw))
        if not path.is_absolute():
            path = root / str(raw)
        if path_is_within(path, release_root):
            errors.append(f"OFFICIAL_STATE_INSIDE_RELEASE:{raw}")
    return errors


def validate_ipc_contract(
    contract: Mapping[str, Any],
    *,
    official_contract_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Release IPC contract must stay compatible with the official contract."""
    section = contract.get("ipc_contract") or {}
    if not section:
        return []
    try:
        if official_contract_path is not None:
            payload = json.loads(
                Path(os.fspath(official_contract_path)).read_text(encoding="utf-8")
            )
            official = {
                "contract_version": int(payload["contract_version"]),
                "minimum_supported_contract_version": int(
                    payload["minimum_supported_contract_version"]
                ),
            }
        else:
            import sys as _sys

            for candidate in (
                Path(__file__).resolve().parents[3] / "main-system" / "src-core" / "tasks",
                Path(__file__).resolve().parents[3] / "main-system" / "src-core",
            ):
                if str(candidate) not in _sys.path:
                    _sys.path.insert(0, str(candidate))
            from packager_base import load_tool_runtime_contract

            official = load_tool_runtime_contract()
    except (OSError, ValueError, KeyError, ImportError) as error:
        return [f"IPC_CONTRACT_UNREADABLE:{error}"]
    release_version = int(section.get("contract_version", 0) or 0)
    release_minimum = int(section.get("minimum_supported_contract_version", 0) or 0)
    if release_minimum > official["contract_version"]:
        return [f"IPC_CONTRACT_INCOMPATIBLE:release-too-new:{release_minimum}"]
    if release_version < official["minimum_supported_contract_version"]:
        return [f"IPC_CONTRACT_INCOMPATIBLE:release-too-old:{release_version}"]
    return []


def validate_release_bundle(
    contract: Mapping[str, Any],
    *,
    python_executable: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str] | None = None,
    shared_root: str | os.PathLike[str] | None = None,
    extra_paths: Iterable[str | os.PathLike[str]] = (),
    cwd: str | os.PathLike[str] | None = None,
    codex_path: str | os.PathLike[str] | None = None,
    allowed_dependency_roots: Iterable[str | os.PathLike[str]] = (),
    service_code_roots: Iterable[str | os.PathLike[str]] = (),
    check_forbidden_content: bool = False,
    official_contract_path: str | os.PathLike[str] | None = None,
    official_root: str | os.PathLike[str] | None = None,
    check_official_state_separation: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Full release-bundle validation: environment, lock, origins, natives.

    Governance references are validated when ``codex_path`` is supplied;
    shared-layer classification is always checked.  Forbidden-content
    scanning applies to real release payloads (``check_forbidden_content``)
    so a development workspace root is never misjudged as a package.
    """
    errors: list[str] = []
    module_names = [
        str(entry.get("module"))
        for entry in (contract.get("modules") or [])
        if isinstance(entry, Mapping) and entry.get("module")
    ]
    module_names.extend(
        str(entry.get("dependency"))
        for entry in (contract.get("governance_dependencies") or [])
        if isinstance(entry, Mapping)
        and entry.get("kind") == "module"
        and entry.get("dependency")
    )
    attributes: dict[str, str] = {}
    shared_layer_key = str(
        (contract.get("shared_layer_contract") or {}).get("version_key") or ""
    )
    if shared_layer_key:
        attributes["shared_layer_contract_version"] = shared_layer_key
    try:
        environment = probe_runtime_environment(
            python_executable,
            modules=module_names,
            attributes=attributes,
            extra_paths=extra_paths,
            cwd=cwd,
            env=env,
            include_distributions=bool((contract.get("dependency_lock") or {}).get("identity")),
        )
    except ReleaseDependencyError as error:
        return {
            "ok": False,
            "errors": [f"PROBE_FAILED:{error}"],
            "environment": {},
            "origins": {},
            "lock": {},
        }
    environment_result = validate_runtime_environment(
        contract,
        environment,
        release_root=release_root,
        allowed_dependency_roots=allowed_dependency_roots,
    )
    errors.extend(environment_result["errors"])
    origins_result = validate_module_origins(
        contract,
        environment,
        release_root=release_root,
        source_root=source_root,
        shared_root=shared_root,
    )
    errors.extend(origins_result["errors"])
    lock_result = validate_dependency_lock(contract, environment)
    errors.extend(lock_result["errors"])
    errors.extend(
        native_extension_errors(contract, release_root=release_root)
    )
    if check_forbidden_content:
        errors.extend(
            validate_forbidden_release_content(contract, release_root)["errors"]
        )
    errors.extend(
        validate_shared_layer_classification(
            contract, code_roots=service_code_roots
        )
    )
    errors.extend(validate_shared_layer_contract(contract, environment))
    errors.extend(validate_service_topology(contract))
    errors.extend(
        validate_governance_dependencies(
            contract, release_root=release_root, probe=environment
        )
    )
    if check_official_state_separation:
        errors.extend(
            validate_official_state_separation(
                contract,
                release_root=release_root,
                official_root=official_root or release_root,
            )
        )
    errors.extend(
        validate_ipc_contract(
            contract, official_contract_path=official_contract_path
        )
    )
    if codex_path is not None:
        errors.extend(
            validate_governance_references(contract, codex_path=codex_path)
        )
    return {
        "ok": not errors,
        "errors": list(dict.fromkeys(errors)),
        "environment": environment_result,
        "origins": origins_result,
        "lock": lock_result,
        "python": environment_result.get("python"),
    }


__all__ = [
    "ReleaseDependencyError",
    "normalize_path",
    "path_is_within",
    "probe_python_import_origins",
    "probe_runtime_environment",
    "validate_module_origins",
    "validate_release_python_origins",
    "validate_runtime_environment",
    "validate_dependency_lock",
    "native_extension_errors",
    "validate_forbidden_release_content",
    "validate_shared_layer_classification",
    "validate_shared_layer_contract",
    "validate_service_topology",
    "validate_governance_dependencies",
    "validate_official_state_separation",
    "validate_ipc_contract",
    "validate_governance_references",
    "validate_release_bundle",
    "pe_machine",
]
