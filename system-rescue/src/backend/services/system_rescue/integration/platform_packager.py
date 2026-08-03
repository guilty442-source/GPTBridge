from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request


SYSTEM_RESCUE_ROOT = Path(__file__).resolve().parents[5]
PROJECT_ROOT = SYSTEM_RESCUE_ROOT.parent
MAIN_SYSTEM_ROOT = PROJECT_ROOT / "main-system"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from governance_rule.execution.integrity.package_integrity import (  # noqa: E402
    PACKAGE_FORMAT_VERSION,
    PACKAGE_METADATA_NAME,
    SOURCE_IGNORED_DIRECTORY_NAMES,
    collect_file_hashes,
    declared_source_exclusions,
    load_package_metadata,
    snapshot_digest,
    verify_packaged_app,
)


PLATFORM_TOOLS_DIR = PROJECT_ROOT
ELECTRON_DIST_DIR = MAIN_SYSTEM_ROOT / "node_modules" / "electron" / "dist"
PLATFORM_RENDERER_ROOT = MAIN_SYSTEM_ROOT / "dist-ui" / "independent-tools"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "platform-tool-app"
TOOL_RUNTIME_CONTRACT_PATH = MAIN_SYSTEM_ROOT / "config" / "tool-runtime-contract.json"
DEFAULT_BACKEND_PORT = 8765
STANDALONE_BACKEND_PORT_MIN = 20000
STANDALONE_BACKEND_PORT_COUNT = 20000
MAX_COMPLETED_RECOVERY_GENERATIONS = 1
REQUIRED_TOOL_VERSION = "1.0.0"
REQUIRED_TOOL_DISPLAY_VERSION = "1.0"


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def load_tool_runtime_contract() -> dict[str, int]:
    try:
        payload = json.loads(TOOL_RUNTIME_CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Tool runtime contract is unavailable") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Tool runtime contract must be an object")
    values: dict[str, int] = {}
    for key in (
        "contract_version",
        "protocol_version",
        "minimum_supported_contract_version",
    ):
        value = payload.get(key)
        if type(value) is not int or value < 1:
            raise RuntimeError(f"Tool runtime contract field is invalid: {key}")
        values[key] = value
    if values["minimum_supported_contract_version"] > values["contract_version"]:
        raise RuntimeError("Tool runtime contract compatibility range is invalid")
    return values


class PromotionRecoveryRequired(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        recovery_root: Path,
        rollback_errors: list[str],
    ) -> None:
        super().__init__(
            f"{message} Recovery data was preserved at: {recovery_root}"
        )
        self.recovery_root = recovery_root
        self.rollback_errors = tuple(rollback_errors)


class PackageOperationBusy(RuntimeError):
    pass


def run_upgrade_auto_repair(
    project_root: Path = PROJECT_ROOT,
    *,
    service_factory: type | None = None,
) -> dict[str, Any]:
    """Return an owner-tool request instead of importing tool business code."""

    return {
        "ok": False,
        "error_code": "TOOL_EXECUTION_REQUEST_REQUIRED",
        "message": "Global Cleaner must execute this request under its own identity",
        "request": {
            "command": "toolbox_request_tool_execution",
            "tool_id": "global-cleaner",
            "args": ["--repair-anomalies", "--scope", "global", "--json"],
            "project_boundary": str(project_root.resolve()),
        },
    }


def _is_link_or_reparse(path: Path) -> bool:
    path_stat = path.lstat()
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(path_stat.st_mode) or bool(attributes & 0x400)


def _sha256_regular_file(path: Path) -> str:
    path_stat = path.lstat()
    if _is_link_or_reparse(path) or not stat_module.S_ISREG(path_stat.st_mode):
        raise RuntimeError(
            f"Package recovery requires a regular non-reparse file: {path}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    final_stat = path.lstat()
    if (
        _is_link_or_reparse(path)
        or not stat_module.S_ISREG(final_stat.st_mode)
        or (
            int(final_stat.st_dev),
            int(final_stat.st_ino),
            int(final_stat.st_size),
            int(final_stat.st_mtime_ns),
        )
        != (
            int(path_stat.st_dev),
            int(path_stat.st_ino),
            int(path_stat.st_size),
            int(path_stat.st_mtime_ns),
        )
    ):
        raise RuntimeError(f"Package recovery file changed while hashing: {path}")
    return digest.hexdigest()


def _inventory_package_tree(root: Path) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    root_stat = root.lstat()
    if _is_link_or_reparse(root) or not stat_module.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(
            f"Package tree must be a regular non-reparse directory: {root}"
        )
    if root.resolve(strict=True) != root:
        raise RuntimeError(f"Package tree escaped its lexical root: {root}")
    entries: list[dict[str, Any]] = []

    def walk_error(error: OSError) -> None:
        raise RuntimeError(
            f"Package recovery inventory is incomplete: {error}"
        ) from error

    for current_root, dirs, files in os.walk(
        root,
        followlinks=False,
        onerror=walk_error,
    ):
        current = Path(current_root)
        if (
            _is_link_or_reparse(current)
            or not current.is_dir()
            or current.resolve(strict=True) != current
        ):
            raise RuntimeError(f"Unsafe package recovery directory: {current}")
        for name in sorted(dirs):
            child = current / name
            child_stat = child.lstat()
            if _is_link_or_reparse(child) or not stat_module.S_ISDIR(
                child_stat.st_mode
            ):
                raise RuntimeError(
                    f"Unsafe package recovery directory entry: {child}"
                )
            entries.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "type": "directory",
                }
            )
        for name in sorted(files):
            child = current / name
            child_stat = child.lstat()
            if _is_link_or_reparse(child) or not stat_module.S_ISREG(
                child_stat.st_mode
            ):
                raise RuntimeError(
                    f"Unsafe package recovery file entry: {child}"
                )
            entries.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "type": "file",
                    "size": int(child_stat.st_size),
                    "sha256": _sha256_regular_file(child),
                }
            )
    entries.sort(key=lambda entry: (str(entry["path"]), str(entry["type"])))
    digest = hashlib.sha256(
        json.dumps(
            entries,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {"entries": entries, "tree_digest": digest}


def _persist_package_document(
    path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"Package journal already exists: {path}")
    document = {
        **payload,
        "document_digest": hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    partial = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
    )
    try:
        with partial.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(partial, path, follow_symlinks=False)
        except FileExistsError as error:
            raise RuntimeError(
                f"Package journal appeared during publication: {path}"
            ) from error
        with path.open("rb+") as stream:
            os.fsync(stream.fileno())
        partial.unlink()
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    persisted = json.loads(path.read_text(encoding="utf-8"))
    if persisted != document:
        raise RuntimeError(f"Package journal verification failed: {path}")
    return document


def _process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, process_id)
        if not process:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(
                process,
                ctypes.byref(exit_code),
            ):
                return True
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(process_id, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


@contextmanager
def package_operation_lock(
    tool_id: str,
    tool_dir: Path,
) -> Iterator[None]:
    build_root = tool_dir / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(build_root) or build_root.resolve(strict=True) != build_root:
        raise RuntimeError(f"Package build root is unsafe: {build_root}")
    safe_tool_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", tool_id) or "tool"
    lock_path = build_root / f".package-{safe_tool_id}.lock"
    token = uuid.uuid4().hex
    for attempt in range(2):
        try:
            lock_path.mkdir(mode=0o700)
            owner_path = lock_path / "owner.json"
            _persist_package_document(
                owner_path,
                {
                    "format_version": 1,
                    "tool_id": tool_id,
                    "pid": os.getpid(),
                    "token": token,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            )
            break
        except FileExistsError as error:
            if attempt:
                raise PackageOperationBusy(
                    f"Could not acquire package lock for {tool_id}"
                ) from error
            lock_stat = lock_path.lstat()
            if (
                _is_link_or_reparse(lock_path)
                or not stat_module.S_ISDIR(lock_stat.st_mode)
                or lock_path.resolve(strict=True) != lock_path
            ):
                raise RuntimeError(f"Package lock is unsafe: {lock_path}")
            owner_path = lock_path / "owner.json"
            age_seconds = max(0.0, time.time() - lock_stat.st_mtime)
            owner: dict[str, Any] = {}
            if owner_path.exists() and not owner_path.is_symlink():
                try:
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    owner = {}
            owner_valid = (
                owner.get("tool_id") == tool_id
                and isinstance(owner.get("pid"), int)
                and re.fullmatch(r"[0-9a-f]{32}", str(owner.get("token") or ""))
                is not None
            )
            if owner_valid and _process_is_alive(int(owner["pid"])):
                raise PackageOperationBusy(
                    f"Another package operation is active for {tool_id}"
                )
            if not owner_valid and age_seconds < 30:
                raise PackageOperationBusy(
                    f"Another package operation is initializing for {tool_id}"
                )
            stale_path = build_root / (
                f"package-lock-recovery-{safe_tool_id}-{uuid.uuid4().hex}"
            )
            os.replace(lock_path, stale_path)
    else:
        raise PackageOperationBusy(
            f"Could not acquire package lock for {tool_id}"
        )
    try:
        yield
    finally:
        try:
            owner_path = lock_path / "owner.json"
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            if (
                owner.get("tool_id") != tool_id
                or owner.get("pid") != os.getpid()
                or owner.get("token") != token
            ):
                raise RuntimeError("Package lock ownership changed")
            owner_path.unlink()
            lock_path.rmdir()
        except Exception:
            pass


SENSITIVE_RUNTIME_DIRECTORY_NAMES = frozenset(
    {
        "browser-profile",
        "browser-profiles",
        "edge-profile",
        "runtime",
        "log",
        "logs",
        "import",
        "imports",
        "export",
        "exports",
    }
)
SENSITIVE_RUNTIME_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".ivault",
    ".jsonl",
    ".log",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
PYTHON_RUNTIME_EXCLUDED_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "_pytest",
        "_pyinstaller_hooks_contrib",
        "ensurepip",
        "idlelib",
        "networkx",
        "pip",
        "PyInstaller",
        "pygments",
        "pytest",
        "scipy",
        "sympy",
        "tests",
        "tkinter",
        "tokenizers",
        "torch",
        "torchaudio",
        "torchvision",
        "transformers",
    }
)
PYTHON_RUNTIME_EXCLUDED_PREFIXES = (
    "networkx-",
    "pip-",
    "pyinstaller-",
    "pyinstaller_hooks_contrib-",
    "pygments-",
    "pytest-",
    "pytest_asyncio-",
    "scipy-",
    "sympy-",
    "tokenizers-",
    "torch-",
    "torchaudio-",
    "torchvision-",
    "transformers-",
)

REQUIRED_RUNTIME_IMPORTS = ("playwright.async_api", "websockets")
TOOL_REQUIRED_RUNTIME_IMPORTS: dict[str, tuple[str, ...]] = {
    "file-sorter": ("PIL", "imageio_ffmpeg"),
    "vaultly": ("imageio_ffmpeg",),
}


def standalone_backend_port(tool_id: str) -> int:
    normalized = str(tool_id or "").strip().lower()
    if not normalized:
        raise ValueError("tool_id is required to assign a standalone backend port")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:4], "big") % STANDALONE_BACKEND_PORT_COUNT
    return STANDALONE_BACKEND_PORT_MIN + offset


def load_manifest(tool_dir: Path) -> dict[str, Any] | None:
    manifest_path = tool_dir / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def validate_tool_version_baseline(
    tool_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version = str(manifest.get("version") or "").strip()
    display_version = str(manifest.get("display_version") or "").strip()
    errors: list[str] = []
    if version != REQUIRED_TOOL_VERSION:
        errors.append(
            f"tool version must be {REQUIRED_TOOL_VERSION}; found {version or 'missing'}"
        )
    if display_version and display_version != REQUIRED_TOOL_DISPLAY_VERSION:
        errors.append(
            "tool display version must be "
            f"{REQUIRED_TOOL_DISPLAY_VERSION}; found {display_version}"
        )
    return {
        "ok": not errors,
        "tool_id": tool_id,
        "error_code": "TOOL_VERSION_MISMATCH" if errors else "",
        "message": "; ".join(errors),
    }


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


def iter_tools(
    selected_ids: set[str] | None,
    *,
    include_special_unpacked: bool = False,
) -> list[tuple[str, Path, dict[str, Any]]]:
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
        distribution = manifest.get("distribution")
        launch = manifest.get("launch")
        explicitly_selected_hybrid = bool(
            selected_ids is not None
            and tool_id in selected_ids
            and isinstance(launch, dict)
            and str(launch.get("primary") or "").strip().casefold() == "executable"
        )
        if (
            isinstance(distribution, dict)
            and distribution.get("mode") == "special-unpackaged"
            and distribution.get("package") is False
            and not include_special_unpacked
            and not explicitly_selected_hybrid
        ):
            continue
        if selected_ids is not None and tool_id not in selected_ids:
            continue
        tools.append((tool_id, tool_dir, manifest))
    return tools


def iter_companion_renderer_tools(
    selected_ids: set[str] | None,
) -> list[tuple[str, Path, dict[str, Any]]]:
    tools: list[tuple[str, Path, dict[str, Any]]] = []
    if not PLATFORM_TOOLS_DIR.exists():
        return tools
    for host_dir in sorted(
        PLATFORM_TOOLS_DIR.iterdir(), key=lambda item: item.name.lower()
    ):
        if not host_dir.is_dir():
            continue
        host_manifest = load_manifest(host_dir)
        declarations = (
            host_manifest.get("companion_tools")
            if isinstance(host_manifest, dict)
            else None
        )
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if not isinstance(declaration, dict):
                continue
            tool_id = str(declaration.get("id") or "").strip()
            relative = Path(str(declaration.get("path") or "").strip())
            if (
                not tool_id
                or selected_ids is not None
                and tool_id not in selected_ids
                or relative.is_absolute()
                or len(relative.parts) != 1
            ):
                continue
            tool_dir = (host_dir / relative).resolve()
            try:
                tool_dir.relative_to(host_dir.resolve())
            except ValueError:
                continue
            manifest = load_manifest(tool_dir)
            if (
                not isinstance(manifest, dict)
                or manifest.get("id") != tool_id
                or manifest.get("host_tool_id")
                != str(host_manifest.get("id") or host_dir.name)
                or manifest.get("main_system_independent_tool") is not True
                or manifest.get("has_custom_ui") is not True
            ):
                continue
            tools.append((tool_id, tool_dir, manifest))
    return tools


def npx_command() -> str:
    return "npx.cmd" if os.name == "nt" else "npx"


def renderer_output_dir(tool_id: str) -> Path:
    return PLATFORM_RENDERER_ROOT / tool_id / "renderer"


def build_platform_renderer(
    tool_id: str,
    tool_dir: Path | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["GPTBRIDGE_PLATFORM_TOOL_ID"] = tool_id
    if tool_dir is not None:
        env["GPTBRIDGE_PLATFORM_TOOL_ROOT"] = str(tool_dir.resolve())
    command = [
        npx_command(),
        "vite",
        "build",
        "-c",
        "vite.platform-tools.config.ts",
    ]
    completed = subprocess.run(
        command,
        cwd=str(MAIN_SYSTEM_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **_background_subprocess_kwargs(),
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


def package_copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in SENSITIVE_RUNTIME_DIRECTORY_NAMES:
            ignored.add(name)
            continue
        if lowered in {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        }:
            ignored.add(name)
            continue
        if lowered.endswith((".pyc", ".pyo")):
            ignored.add(name)
            continue
        if any(lowered.endswith(suffix) for suffix in SENSITIVE_RUNTIME_SUFFIXES):
            ignored.add(name)
    return ignored


def python_runtime_copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in {item.lower() for item in PYTHON_RUNTIME_EXCLUDED_NAMES}:
            ignored.add(name)
            continue
        if any(
            lowered.startswith(prefix.lower())
            for prefix in PYTHON_RUNTIME_EXCLUDED_PREFIXES
        ):
            ignored.add(name)
            continue
        if lowered.endswith((".pyc", ".pyo", ".pth")):
            ignored.add(name)
    return ignored


def copy_runtime_source(
    tool_dir: Path,
    entry: Path,
    app_dir: Path,
    *,
    excluded_root_names: frozenset[str] = frozenset(),
) -> Path | None:
    try:
        relative_entry = entry.resolve().relative_to(tool_dir.resolve())
    except ValueError:
        return None
    if not relative_entry.parts:
        return None

    source_root = tool_dir / relative_entry.parts[0]
    if not source_root.is_dir():
        return None
    canonical_source_root = source_root.resolve()

    target_root = app_dir / relative_entry.parts[0]
    if target_root.exists():
        target_resolved = target_root.resolve()
        app_resolved = app_dir.resolve()
        if app_resolved != target_resolved and app_resolved not in target_resolved.parents:
            raise RuntimeError(f"Refusing to replace path outside app package: {target_root}")
        shutil.rmtree(target_root)

    def runtime_copy_ignore(directory: str, names: list[str]) -> set[str]:
        ignored = package_copy_ignore(directory, names)
        if Path(directory).resolve() == canonical_source_root:
            ignored.update(name for name in names if name in excluded_root_names)
        return ignored

    shutil.copytree(
        source_root,
        target_root,
        ignore=runtime_copy_ignore,
    )
    return target_root


def copy_backend_source_bundle(
    tool_id: str,
    tool_dir: Path,
    entry: Path,
    app_dir: Path,
) -> Path:
    manifest = load_manifest(tool_dir) or {}
    request_channel = manifest.get("request_channel")
    governed_channel = (
        isinstance(request_channel, dict)
        and request_channel.get("model") == "governance-authenticated-shared-layer"
    )
    if governed_channel:
        packaged_tool_dir = app_dir / "independent_tool" / tool_id
    else:
        core_target = app_dir / "src-core"
        if core_target.exists():
            shutil.rmtree(core_target)
        shutil.copytree(SRC_CORE_DIR, core_target, ignore=package_copy_ignore)
        packaged_tool_dir = app_dir / "platform_tools" / tool_id
    packaged_tool_dir.mkdir(parents=True, exist_ok=True)
    relative_entry = entry.resolve().relative_to(tool_dir.resolve())
    runtime_root_name = relative_entry.parts[0]
    excluded_paths = package_source_excluded_paths(tool_dir, entry)
    package_policy = manifest.get("package")
    generated_files = (
        package_policy.get("generated_files", {})
        if isinstance(package_policy, dict)
        else {}
    )
    if not isinstance(generated_files, dict):
        raise RuntimeError("package.generated_files must be an object")
    source_project_root = tool_dir.resolve().parent
    tool_relative_exclusions: list[Path] = []
    for excluded_path in excluded_paths:
        excluded_source = source_project_root / excluded_path
        relative = excluded_source.relative_to(tool_dir.resolve())
        if not relative.parts or relative.parts[0] != runtime_root_name:
            raise RuntimeError(
                f"Package exclusion is outside the runtime root: {excluded_path}"
            )
        if excluded_source.exists() or excluded_source.is_symlink():
            excluded_stat = excluded_source.lstat()
            attributes = int(
                getattr(excluded_stat, "st_file_attributes", 0) or 0
            )
            if (
                stat_module.S_ISLNK(excluded_stat.st_mode)
                or bool(attributes & 0x400)
                or not stat_module.S_ISREG(excluded_stat.st_mode)
            ):
                raise RuntimeError(
                    "Excluded package source must be a regular, non-reparse "
                    f"file: {excluded_source}"
                )
        tool_relative_exclusions.append(relative)
    runtime_path = copy_runtime_source(
        tool_dir,
        entry,
        packaged_tool_dir,
    )
    if runtime_path is None:
        raise RuntimeError(
            f"Tool runtime entry must be inside its tool directory: {entry}"
        )
    excluded_set = {path.as_posix() for path in tool_relative_exclusions}
    for relative in tool_relative_exclusions:
        packaged_path = packaged_tool_dir / relative
        if packaged_path.exists() or packaged_path.is_symlink():
            if packaged_path.is_dir() and not packaged_path.is_symlink():
                raise RuntimeError(
                    f"Excluded package source became a directory: {packaged_path}"
                )
            packaged_path.unlink()
    for raw_relative, default_value in generated_files.items():
        relative = Path(str(raw_relative))
        canonical = relative.as_posix()
        if canonical not in excluded_set or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(
                "Generated package files must also be declared in source_excludes"
            )
        generated_path = packaged_tool_dir / relative
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_text(
            json.dumps(default_value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    shutil.copy2(tool_dir / "manifest.json", packaged_tool_dir / "manifest.json")
    return runtime_path


def copy_portable_python_runtime(
    app_dir: Path,
    *,
    base_prefix: Path | None = None,
    environment_prefix: Path | None = None,
) -> Path:
    base = (base_prefix or Path(sys.base_prefix)).resolve()
    environment = (environment_prefix or Path(sys.prefix)).resolve()
    target = app_dir / "python"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    root_runtime_files = [
        candidate
        for candidate in base.iterdir()
        if candidate.is_file()
        and (
            candidate.suffix.lower() in {".dll", ".exe"}
            or candidate.name.lower() in {"license.txt", "news.txt"}
        )
    ]
    if not any(candidate.name.lower() == "python.exe" for candidate in root_runtime_files):
        raise RuntimeError(f"Portable Python executable not found under: {base}")
    for source in root_runtime_files:
        shutil.copy2(source, target / source.name)

    for directory_name in ("DLLs", "Lib"):
        source = base / directory_name
        if not source.is_dir():
            raise RuntimeError(f"Portable Python directory not found: {source}")
        shutil.copytree(
            source,
            target / directory_name,
            ignore=python_runtime_copy_ignore,
        )

    source_site_packages = environment / "Lib" / "site-packages"
    target_site_packages = target / "Lib" / "site-packages"
    if source_site_packages.is_dir() and source_site_packages != (
        base / "Lib" / "site-packages"
    ):
        if target_site_packages.exists():
            shutil.rmtree(target_site_packages)
        shutil.copytree(
            source_site_packages,
            target_site_packages,
            ignore=python_runtime_copy_ignore,
        )
    if not (target / "python.exe").exists():
        raise RuntimeError("Portable Python runtime copy verification failed")
    return target


def validate_staged_python_runtime(
    app_dir: Path,
    tool_id: str,
    backend_entry_relative: str,
) -> None:
    """Fail before promotion when an isolated runtime lost a required feature."""

    runtime = app_dir / "python" / "python.exe"
    backend_entry = app_dir / Path(backend_entry_relative)
    imports = tuple(
        dict.fromkeys(
            (
                *REQUIRED_RUNTIME_IMPORTS,
                *TOOL_REQUIRED_RUNTIME_IMPORTS.get(tool_id, ()),
            )
        )
    )
    import_probe = (
        "import importlib,json,sys;"
        "[importlib.import_module(name) for name in json.loads(sys.argv[1])]"
    )
    probes = (
        (
            [
                str(runtime),
                "-B",
                "-s",
                "-E",
                "-X",
                "utf8",
                "-c",
                import_probe,
                json.dumps(imports),
            ],
            "required dependency import",
        ),
        (
            [
                str(runtime),
                "-B",
                "-s",
                "-E",
                "-X",
                "utf8",
                "-c",
                (
                    "import pathlib,sys;"
                    "source=pathlib.Path(sys.argv[1]).read_text(encoding='utf-8');"
                    "compile(source,sys.argv[1],'exec')"
                ),
                str(backend_entry),
            ],
            "standalone backend startup import",
        ),
    )
    for command, label in probes:
        completed = subprocess.run(
            command,
            cwd=app_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
            **_background_subprocess_kwargs(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"Staged Python {label} failed for {tool_id}: "
                f"{detail or f'exit code {completed.returncode}'}"
            )


def copy_file_preserving_locked_target(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if source.samefile(target):
            return
        target.unlink()
    try:
        os.link(source, target)
        return
    except OSError:
        pass

    shutil.copy2(source, target)


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
        leftover_electron.unlink()


def package_source_roots(tool_dir: Path, entry: Path) -> list[str]:
    relative_entry = entry.resolve().relative_to(tool_dir.resolve())
    runtime_root = tool_dir / relative_entry.parts[0]
    candidates = [
        tool_dir / "manifest.json",
        tool_dir / "README.md",
        runtime_root,
        MAIN_SYSTEM_ROOT / "config" / "tool-runtime-contract.json",
    ]
    request_channel = (load_manifest(tool_dir) or {}).get("request_channel")
    if (
        isinstance(request_channel, dict)
        and request_channel.get("model") == "governance-authenticated-shared-layer"
    ):
        candidates.append(PROJECT_ROOT / "shared-layer" / "src" / "shared_layer")
    roots: list[str] = []
    for candidate in candidates:
        if candidate.exists():
            roots.append(candidate.resolve().relative_to(PROJECT_ROOT).as_posix())
    return roots


def package_source_excluded_paths(tool_dir: Path, entry: Path) -> list[str]:
    del entry
    tool_id = tool_id_from_directory(tool_dir)
    tool_root = tool_dir.resolve()
    source_project_root = tool_root.parent
    excluded_paths = declared_source_exclusions(source_project_root, tool_id)
    if excluded_paths is None:
        raise RuntimeError(
            f"Invalid package.source_excludes declaration for {tool_id}"
        )
    for relative_path in excluded_paths:
        candidate = (source_project_root / relative_path).resolve()
        try:
            candidate.relative_to(tool_root)
        except ValueError as error:
            raise RuntimeError(
                f"Package exclusion escaped tool root: {relative_path}"
            ) from error
    return sorted(excluded_paths)


def tool_id_from_directory(tool_dir: Path) -> str:
    manifest = load_manifest(tool_dir)
    if manifest:
        return str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
    return tool_dir.name


def running_executable_process_ids(executable_file: Path) -> list[int]:
    if os.name != "nt" or not executable_file.exists():
        return []
    env = os.environ.copy()
    env["GPTBRIDGE_EXECUTABLE_PATH"] = str(executable_file.resolve())
    command = (
        "$target = [System.IO.Path]::GetFullPath($env:GPTBRIDGE_EXECUTABLE_PATH);"
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -and "
        "([System.IO.Path]::GetFullPath($_.ExecutablePath)).Equals("
        "$target, [System.StringComparison]::OrdinalIgnoreCase) } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **_background_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return []
    process_ids: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            process_ids.append(int(line.strip()))
        except ValueError:
            continue
    return process_ids


def stop_running_executable_for_upgrade(
    executable_file: Path,
    process_ids: list[int],
    *,
    timeout_seconds: float = 15.0,
) -> bool:
    """Stop only verified processes for one packaged tool before promotion."""

    if os.name != "nt" or not process_ids:
        return not running_executable_process_ids(executable_file)
    expected = set(process_ids)
    current = set(running_executable_process_ids(executable_file))
    targets = sorted(expected & current)
    if not targets:
        return True

    for process_id in targets:
        # Re-resolve the executable immediately before termination so a
        # recycled PID can never broaden an upgrade beyond this tool.
        if process_id not in running_executable_process_ids(executable_file):
            continue
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            **_background_subprocess_kwargs(),
        )

    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if not running_executable_process_ids(executable_file):
            return True
        time.sleep(0.2)
    return not running_executable_process_ids(executable_file)


def restart_packaged_executable(executable_file: Path) -> bool:
    """Restart an upgraded tool without inheriting Codex/Electron test mode."""

    if not executable_file.is_file():
        return False
    environment = os.environ.copy()
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    environment.pop("GPTBRIDGE_START_HIDDEN", None)
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    try:
        subprocess.Popen(
            [str(executable_file.resolve())],
            cwd=str(executable_file.parent.resolve()),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError:
        return False
    return True


def windows_process_owns_listening_port(process_id: int, port: int) -> bool:
    if os.name != "nt" or process_id <= 0 or not (1024 <= port <= 65535):
        return False
    env = os.environ.copy()
    env["GPTBRIDGE_EXPECTED_PROCESS_ID"] = str(process_id)
    env["GPTBRIDGE_EXPECTED_LISTEN_PORT"] = str(port)
    command = (
        "$expectedPid = [int]$env:GPTBRIDGE_EXPECTED_PROCESS_ID;"
        "$expectedPort = [int]$env:GPTBRIDGE_EXPECTED_LISTEN_PORT;"
        "$match = Get-NetTCPConnection -State Listen "
        "-LocalPort $expectedPort -ErrorAction SilentlyContinue | "
        "Where-Object { $_.OwningProcess -eq $expectedPid } | "
        "Select-Object -First 1;"
        "if ($null -ne $match) { 'owned' }"
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        **_background_subprocess_kwargs(),
    )
    return completed.returncode == 0 and completed.stdout.strip() == "owned"


def legacy_standalone_backend_owner_path(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        state_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
        state_base = (
            Path(xdg_state_home)
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
    return state_base / "GPTBridge" / "ipc" / f"standalone-{safe_tool_id}-backend.json"


def standalone_project_root(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        state_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
        state_base = (
            Path(xdg_state_home)
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
    return Path(
        os.path.abspath(state_base / "GPTBridge" / "standalone" / safe_tool_id)
    )


def standalone_backend_owner_path(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    return (
        standalone_project_root(tool_id)
        / "runtime"
        / "ipc"
        / f"standalone-{safe_tool_id}-backend.json"
    )


def workspace_instance_id(project_root: Path) -> str:
    normalized = os.path.normcase(
        os.path.abspath(project_root)
    ).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _force_stop_verified_unresponsive_backend(
    *,
    tool_id: str,
    owner: dict[str, Any],
    owner_path: Path,
    backend_port: int,
    live_app_dir: Path,
) -> bool:
    """Terminate only a hung process proven to belong to this exact package."""

    if os.name != "nt":
        return False
    try:
        process_id = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False

    metadata = load_package_metadata(live_app_dir)
    payload_files = metadata.get("payload_files")
    package_digest = str(metadata.get("payload_digest") or "").strip().lower()
    owner_digest = str(owner.get("package_digest") or "").strip().lower()
    expected_runtime_hash = (
        str(payload_files.get("python/python.exe") or "").strip().lower()
        if isinstance(payload_files, dict)
        else ""
    )
    runtime = live_app_dir / "python" / "python.exe"
    if (
        re.fullmatch(r"[0-9a-f]{64}", package_digest) is None
        or owner_digest != package_digest
        or re.fullmatch(r"[0-9a-f]{64}", expected_runtime_hash) is None
        or not runtime.is_file()
        or _is_link_or_reparse(runtime)
        or _sha256_regular_file(runtime) != expected_runtime_hash
        or process_id not in running_executable_process_ids(runtime)
        or not windows_process_owns_listening_port(process_id, backend_port)
    ):
        return False

    expected_root = standalone_project_root(tool_id)
    for controlled_path in (
        expected_root.parent.parent,
        expected_root.parent,
        expected_root,
    ):
        if not controlled_path.exists():
            continue
        if _is_link_or_reparse(controlled_path):
            return False
        if controlled_path.resolve(strict=True) != controlled_path:
            return False

    audit_root = expected_root / "runtime" / "backend-handoff-recovery"
    audit_root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(audit_root):
        return False
    operation_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-{uuid.uuid4().hex}"
    )
    requested_path = audit_root / f"{operation_id}.requested.json"
    audit_payload = {
        "format_version": 1,
        "status": "force-stop-requested",
        "reason": "verified-packaged-backend-health-timeout",
        "tool_id": tool_id,
        "process_id": process_id,
        "backend_port": backend_port,
        "owner_path": str(owner_path),
        "project_root": str(expected_root),
        "runtime_executable": str(runtime),
        "runtime_sha256": expected_runtime_hash,
        "package_digest": package_digest,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    _persist_package_document(requested_path, audit_payload)

    completed = subprocess.run(
        ["taskkill", "/PID", str(process_id), "/T", "/F"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        **_background_subprocess_kwargs(),
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (
            not _process_is_alive(process_id)
            and not windows_process_owns_listening_port(
                process_id,
                backend_port,
            )
        ):
            _persist_package_document(
                audit_root / f"{operation_id}.completed.json",
                {
                    **audit_payload,
                    "status": "force-stop-completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "taskkill_exit_code": int(completed.returncode),
                    "request_journal": str(requested_path),
                },
            )
            return True
        time.sleep(0.2)
    raise RuntimeError(
        "Verified packaged backend remained active after bounded force-stop; "
        f"recovery journal: {requested_path}"
    )


def stop_verified_packaged_backend(tool_id: str, live_app_dir: Path) -> bool:
    def valid_port(value: object) -> int | None:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return None
        return port if 1024 <= port <= 65535 else None

    expected_root = standalone_project_root(tool_id)
    expected_instance = workspace_instance_id(expected_root)
    expected_root_text = os.path.normcase(
        os.path.normpath(str(expected_root))
    )

    descriptor_port: int | None = None
    for descriptor_path in (
        live_app_dir / "manifest.json",
        live_app_dir / PACKAGE_METADATA_NAME,
    ):
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(descriptor, dict):
            continue
        if descriptor_path.name == "manifest.json":
            standalone = descriptor.get("standalone")
            port_value = (
                standalone.get("backend_port")
                if isinstance(standalone, dict)
                else None
            )
        else:
            port_value = descriptor.get("backend_port")
        descriptor_port = valid_port(port_value)
        if descriptor_port is not None:
            break

    owner_candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[int, str]] = set()
    owner_paths = (
        standalone_backend_owner_path(tool_id),
        legacy_standalone_backend_owner_path(tool_id),
    )
    for owner_index, owner_path in enumerate(owner_paths):
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(owner, dict):
            continue
        if str(owner.get("tool_id") or "") != tool_id:
            continue
        owner_root_text = str(owner.get("project_root") or "").strip()
        if (
            not owner_root_text
            or not os.path.isabs(owner_root_text)
            or os.path.normcase(os.path.normpath(owner_root_text))
            != expected_root_text
            or str(owner.get("workspace_instance_id") or "")
            != expected_instance
        ):
            continue
        shutdown_token = str(owner.get("shutdown_token") or "")
        if re.fullmatch(r"[0-9a-fA-F]{64}", shutdown_token) is None:
            continue
        backend_port = valid_port(owner.get("backend_port"))
        if backend_port is None:
            backend_port = (
                DEFAULT_BACKEND_PORT
                if owner_index == 1
                else descriptor_port or standalone_backend_port(tool_id)
            )
        candidate_identity = (backend_port, shutdown_token)
        if candidate_identity in seen_candidates:
            continue
        seen_candidates.add(candidate_identity)
        owner_candidates.append(
            {
                "backend_port": backend_port,
                "shutdown_token": shutdown_token,
                "owner": owner,
                "owner_path": owner_path,
            }
        )

    for candidate in owner_candidates:
        backend_port = int(candidate["backend_port"])
        shutdown_token = str(candidate["shutdown_token"])
        health_unavailable = False
        try:
            with urllib_request.urlopen(
                f"http://127.0.0.1:{backend_port}/health",
                timeout=1.5,
            ) as response:
                health = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            urllib_error.URLError,
        ):
            health_unavailable = True
            health = None
        if health_unavailable:
            if _force_stop_verified_unresponsive_backend(
                tool_id=tool_id,
                owner=dict(candidate["owner"]),
                owner_path=Path(candidate["owner_path"]),
                backend_port=backend_port,
                live_app_dir=live_app_dir,
            ):
                return True
            continue
        if (
            not isinstance(health, dict)
            or str(health.get("workspace_instance_id") or "")
            != expected_instance
        ):
            continue

        shutdown_request = urllib_request.Request(
            f"http://127.0.0.1:{backend_port}/shutdown",
            headers={"X-GPTBridge-Shutdown-Token": shutdown_token},
        )
        try:
            with urllib_request.urlopen(shutdown_request, timeout=3) as response:
                if int(getattr(response, "status", 0)) != 200:
                    continue
        except (OSError, urllib_error.URLError):
            continue

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                urllib_request.urlopen(
                    f"http://127.0.0.1:{backend_port}/health",
                    timeout=0.3,
                ).close()
            except (OSError, urllib_error.URLError):
                return True
            time.sleep(0.2)
        raise RuntimeError(
            f"Packaged backend on port {backend_port} did not stop "
            "before the update timeout"
        )
    return False


def _inventory_file_map(inventory: dict[str, Any]) -> dict[str, tuple[int, str]]:
    return {
        str(entry["path"]): (
            int(entry["size"]),
            str(entry["sha256"]),
        )
        for entry in inventory["entries"]
        if entry.get("type") == "file"
    }


def _synchronize_distribution_files_in_place(
    source_root: Path,
    live_root: Path,
    *,
    retired_root: Path,
) -> dict[str, Any]:
    """Replace files without renaming the watched live distribution root."""

    source_inventory = _inventory_package_tree(source_root)
    live_inventory = _inventory_package_tree(live_root)
    source_files = _inventory_file_map(source_inventory)
    live_files = _inventory_file_map(live_inventory)
    retired_root.mkdir(parents=True, exist_ok=False)

    for relative_path in sorted(set(live_files) - set(source_files)):
        live_path = live_root / Path(*relative_path.split("/"))
        retired_path = retired_root / "removed-from-live" / Path(
            *relative_path.split("/")
        )
        retired_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(live_path, retired_path)

    source_directories = sorted(
        (
            str(entry["path"])
            for entry in source_inventory["entries"]
            if entry.get("type") == "directory"
        ),
        key=lambda value: (value.count("/"), value),
    )
    for relative_path in source_directories:
        target = live_root / Path(*relative_path.split("/"))
        if target.exists() and not target.is_dir():
            conflict = retired_root / "type-conflicts" / Path(
                *relative_path.split("/")
            )
            conflict.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, conflict)
        target.mkdir(parents=True, exist_ok=True)

    for relative_path, expected in sorted(source_files.items()):
        source = source_root / Path(*relative_path.split("/"))
        target = live_root / Path(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_dir():
            conflict = (
                retired_root
                / "type-conflicts"
                / Path(*relative_path.split("/"))
            )
            conflict.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, conflict)
        if target.is_file() and not _is_link_or_reparse(target):
            current = (int(target.stat().st_size), _sha256_regular_file(target))
            if current == expected:
                continue

        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
        )
        try:
            shutil.copy2(source, temporary, follow_symlinks=False)
            copied = (
                int(temporary.stat().st_size),
                _sha256_regular_file(temporary),
            )
            if copied != expected:
                raise RuntimeError(
                    f"In-place package copy verification failed: {relative_path}"
                )
            if target.exists() or target.is_symlink():
                retired = retired_root / "replaced-live" / Path(
                    *relative_path.split("/")
                )
                retired.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, retired)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    installed_inventory = _inventory_package_tree(live_root)
    installed_files = _inventory_file_map(installed_inventory)
    if installed_files != source_files:
        raise RuntimeError(
            "In-place installed distribution files do not match staged package"
        )
    if _inventory_package_tree(source_root) != source_inventory:
        raise RuntimeError("Staged distribution changed during in-place promotion")
    return installed_inventory


def _promote_staged_distribution_in_place(
    *,
    staged_dist: Path,
    dist_dir: Path,
    package_root: Path,
    previous_dist: Path,
    previous_inventory: dict[str, Any],
    previous_metadata: dict[str, Any],
    unknown_live_app_paths: list[str],
    unknown_live_runtime_paths: list[str],
    root_rename_error: PermissionError,
) -> Path:
    """Windows fallback for directories held by a non-destructive watcher."""

    if previous_dist.exists() or previous_dist.is_symlink():
        raise RuntimeError(
            f"Previous distribution recovery already exists: {previous_dist}"
        )
    shutil.copytree(dist_dir, previous_dist, copy_function=shutil.copy2)
    retained_inventory = _inventory_package_tree(previous_dist)
    current_inventory = _inventory_package_tree(dist_dir)
    if (
        retained_inventory != previous_inventory
        or current_inventory != previous_inventory
    ):
        raise RuntimeError(
            "Previous distribution changed during verified fallback copy"
        )

    retired_root = package_root / "retired-live-paths"
    failed_live = package_root / "failed-live-dist"
    try:
        installed_inventory = _synchronize_distribution_files_in_place(
            staged_dist,
            dist_dir,
            retired_root=retired_root,
        )
    except BaseException as install_error:
        rollback_errors: list[str] = []
        try:
            shutil.copytree(dist_dir, failed_live, copy_function=shutil.copy2)
            _inventory_package_tree(failed_live)
        except BaseException as capture_error:
            rollback_errors.append(
                f"capture failed in-place distribution: {capture_error}"
            )
        try:
            _synchronize_distribution_files_in_place(
                previous_dist,
                dist_dir,
                retired_root=package_root
                / f"rollback-retired-{uuid.uuid4().hex}",
            )
            if _inventory_file_map(_inventory_package_tree(dist_dir)) != (
                _inventory_file_map(previous_inventory)
            ):
                raise RuntimeError("in-place rollback verification mismatch")
        except BaseException as rollback_error:
            rollback_errors.append(
                f"restore previous distribution in place: {rollback_error}"
            )
        _persist_package_document(
            package_root / "promotion-aborted.json",
            {
                "format_version": 1,
                "status": (
                    "rollback-incomplete"
                    if rollback_errors
                    else "aborted-and-rolled-back"
                ),
                "strategy": "verified-in-place",
                "aborted_at_utc": datetime.now(timezone.utc).isoformat(),
                "root_rename_error": str(root_rename_error),
                "error": str(install_error),
                "rollback_errors": rollback_errors,
                "live_dist": str(dist_dir),
                "previous_dist": str(previous_dist),
                "failed_live_dist": (
                    str(failed_live) if failed_live.exists() else ""
                ),
            },
        )
        if rollback_errors:
            raise PromotionRecoveryRequired(
                "In-place distribution update failed and rollback is incomplete: "
                + "; ".join(rollback_errors),
                recovery_root=package_root,
                rollback_errors=rollback_errors,
            ) from install_error
        raise

    recovery_manifest = _persist_package_document(
        package_root / "promotion-recovery-manifest.json",
        {
            "format_version": 1,
            "status": "retained",
            "strategy": "verified-in-place",
            "retained_at_utc": datetime.now(timezone.utc).isoformat(),
            "root_rename_error": str(root_rename_error),
            "live_dist": str(dist_dir),
            "recovery_root": str(package_root),
            "previous_dist": str(previous_dist),
            "retired_live_paths": str(retired_root),
            "staged_dist": str(staged_dist),
            "previous_tree_digest": retained_inventory["tree_digest"],
            "previous_entries": retained_inventory["entries"],
            "previous_package_digest": str(
                previous_metadata.get("payload_digest") or ""
            ),
            "unknown_live_app_paths": unknown_live_app_paths,
            "unknown_live_runtime_paths": unknown_live_runtime_paths,
        },
    )
    _persist_package_document(
        package_root / "promotion-complete.json",
        {
            "format_version": 1,
            "status": "complete-with-recovery",
            "strategy": "verified-in-place",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "live_tree_digest": installed_inventory["tree_digest"],
            "previous_tree_digest": retained_inventory["tree_digest"],
            "recovery_manifest_digest": recovery_manifest["document_digest"],
        },
    )
    return package_root


def promote_staged_distribution(
    staged_dist: Path,
    dist_dir: Path,
) -> Path | None:
    staged_dist = Path(os.path.abspath(staged_dist))
    dist_dir = Path(os.path.abspath(dist_dir))
    package_root = staged_dist.parent
    if (
        not staged_dist.exists()
        or _is_link_or_reparse(staged_dist)
        or staged_dist.resolve(strict=True) != staged_dist
    ):
        raise RuntimeError(f"Staged distribution is unsafe: {staged_dist}")
    staged_inventory = _inventory_package_tree(staged_dist)
    previous_dist = package_root / "previous-dist"
    failed_dist = package_root / "failed-new-dist"
    journal_path = package_root / "promotion-journal.json"

    had_live_dist = dist_dir.exists() or dist_dir.is_symlink()
    previous_inventory: dict[str, Any] | None = None
    previous_metadata: dict[str, Any] = {}
    unknown_live_app_paths: list[str] = []
    unknown_live_runtime_paths: list[str] = []
    if had_live_dist:
        if (
            _is_link_or_reparse(dist_dir)
            or not dist_dir.is_dir()
            or dist_dir.resolve(strict=True) != dist_dir
        ):
            raise RuntimeError(f"Live distribution is unsafe: {dist_dir}")
        previous_inventory = _inventory_package_tree(dist_dir)
        live_app = dist_dir / "resources" / "app"
        metadata_path = live_app / PACKAGE_METADATA_NAME
        if metadata_path.is_file() and not _is_link_or_reparse(metadata_path):
            try:
                previous_metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                previous_metadata = {}
        owned_payload_paths = {
            f"resources/app/{relative_path}"
            for relative_path in (
                previous_metadata.get("payload_files")
                if isinstance(previous_metadata.get("payload_files"), dict)
                else {}
            )
        }
        previous_file_paths = {
            str(entry["path"])
            for entry in previous_inventory["entries"]
            if entry.get("type") == "file"
        }
        unknown_live_app_paths = sorted(
            relative_path
            for relative_path in previous_file_paths
            if relative_path.startswith("resources/app/")
            and relative_path not in owned_payload_paths
            and relative_path
            != f"resources/app/{PACKAGE_METADATA_NAME}"
        )
        staged_file_paths = {
            str(entry["path"])
            for entry in staged_inventory["entries"]
            if entry.get("type") == "file"
        }
        unknown_live_runtime_paths = sorted(
            relative_path
            for relative_path in previous_file_paths
            if not relative_path.startswith("resources/app/")
            and relative_path not in staged_file_paths
        )

    _persist_package_document(
        journal_path,
        {
            "format_version": 1,
            "status": "prepared",
            "operation_id": package_root.name,
            "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
            "staged_dist": str(staged_dist),
            "live_dist": str(dist_dir),
            "previous_dist": str(previous_dist),
            "staged_tree_digest": staged_inventory["tree_digest"],
            "previous_tree_digest": (
                previous_inventory["tree_digest"]
                if previous_inventory is not None
                else ""
            ),
            "unknown_live_app_paths": unknown_live_app_paths,
            "unknown_live_runtime_paths": unknown_live_runtime_paths,
        },
    )

    moved_previous = False
    installed_new = False
    try:
        dist_dir.parent.mkdir(parents=True, exist_ok=True)
        if had_live_dist:
            if previous_dist.exists() or previous_dist.is_symlink():
                raise RuntimeError(
                    f"Previous distribution recovery already exists: {previous_dist}"
                )
            try:
                os.replace(dist_dir, previous_dist)
            except PermissionError as root_rename_error:
                if previous_inventory is None:
                    raise
                return _promote_staged_distribution_in_place(
                    staged_dist=staged_dist,
                    dist_dir=dist_dir,
                    package_root=package_root,
                    previous_dist=previous_dist,
                    previous_inventory=previous_inventory,
                    previous_metadata=previous_metadata,
                    unknown_live_app_paths=unknown_live_app_paths,
                    unknown_live_runtime_paths=unknown_live_runtime_paths,
                    root_rename_error=root_rename_error,
                )
            moved_previous = True
        os.replace(staged_dist, dist_dir)
        installed_new = True

        installed_inventory = _inventory_package_tree(dist_dir)
        if installed_inventory != staged_inventory:
            raise RuntimeError(
                "Installed distribution does not match the staged package"
            )
        if moved_previous and previous_inventory is not None:
            retained_inventory = _inventory_package_tree(previous_dist)
            if retained_inventory != previous_inventory:
                raise RuntimeError(
                    "Previous distribution recovery verification failed"
                )
            recovery_manifest = _persist_package_document(
                package_root / "promotion-recovery-manifest.json",
                {
                    "format_version": 1,
                    "status": "retained",
                    "retained_at_utc": datetime.now(timezone.utc).isoformat(),
                    "live_dist": str(dist_dir),
                    "recovery_root": str(package_root),
                    "previous_dist": str(previous_dist),
                    "previous_tree_digest": retained_inventory["tree_digest"],
                    "previous_entries": retained_inventory["entries"],
                    "previous_package_digest": str(
                        previous_metadata.get("payload_digest") or ""
                    ),
                    "unknown_live_app_paths": unknown_live_app_paths,
                    "unknown_live_runtime_paths": unknown_live_runtime_paths,
                },
            )
            _persist_package_document(
                package_root / "promotion-complete.json",
                {
                    "format_version": 1,
                    "status": "complete-with-recovery",
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "live_tree_digest": installed_inventory["tree_digest"],
                    "previous_tree_digest": retained_inventory["tree_digest"],
                    "recovery_manifest_digest": recovery_manifest[
                        "document_digest"
                    ],
                },
            )
            return package_root

        _persist_package_document(
            package_root / "promotion-complete.json",
            {
                "format_version": 1,
                "status": "complete-new-install",
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "live_tree_digest": installed_inventory["tree_digest"],
            },
        )
        return None
    except BaseException as error:
        rollback_errors: list[str] = []
        if installed_new and dist_dir.exists():
            try:
                if failed_dist.exists() or failed_dist.is_symlink():
                    failed_dist = package_root / (
                        f"failed-new-dist-{uuid.uuid4().hex}"
                    )
                os.replace(dist_dir, failed_dist)
            except BaseException as rollback_error:
                rollback_errors.append(
                    f"preserve failed new distribution: {rollback_error}"
                )
        if moved_previous:
            try:
                if previous_dist.exists() and not dist_dir.exists():
                    os.replace(previous_dist, dist_dir)
                elif not dist_dir.exists():
                    raise FileNotFoundError(
                        f"Previous distribution is missing: {previous_dist}"
                    )
            except BaseException as rollback_error:
                rollback_errors.append(
                    f"restore previous distribution: {rollback_error}"
                )
        try:
            _persist_package_document(
                package_root / "promotion-aborted.json",
                {
                    "format_version": 1,
                    "status": (
                        "rollback-incomplete"
                        if rollback_errors
                        else "aborted-and-rolled-back"
                    ),
                    "aborted_at_utc": datetime.now(timezone.utc).isoformat(),
                    "error": str(error),
                    "rollback_errors": rollback_errors,
                    "live_dist": str(dist_dir),
                    "previous_dist": str(previous_dist),
                    "failed_new_dist": (
                        str(failed_dist) if failed_dist.exists() else ""
                    ),
                },
            )
        except BaseException as journal_error:
            rollback_errors.append(f"persist abort journal: {journal_error}")
        if rollback_errors:
            raise PromotionRecoveryRequired(
                "Distribution update failed and recovery is incomplete: "
                + "; ".join(rollback_errors),
                recovery_root=package_root,
                rollback_errors=rollback_errors,
            ) from error
        raise


def verify_tool_package(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version_check = validate_tool_version_baseline(tool_id, manifest)
    if not version_check["ok"]:
        return version_check
    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    exe_path = dist_dir / f"{executable_name}.exe"
    app_dir = dist_dir / "resources" / "app"
    if not exe_path.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_MISSING",
            "message": f"Standalone executable not found: {exe_path}",
        }
    result = verify_packaged_app(app_dir, project_root=PROJECT_ROOT)
    if not result.get("ok"):
        return {
            **result,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
        }

    metadata = load_package_metadata(app_dir)
    try:
        app_manifest = json.loads(
            (app_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        return {
            **result,
            "ok": False,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
            "error_code": "PACKAGE_SEMANTICS_INVALID",
            "message": f"Standalone app manifest is invalid: {error}",
        }
    standalone = (
        app_manifest.get("standalone")
        if isinstance(app_manifest, dict)
        else None
    )
    expected_port = standalone_backend_port(tool_id)
    expected_version = str(manifest.get("version", "1.0.0"))
    request_channel = manifest.get("request_channel")
    governed_channel = (
        isinstance(request_channel, dict)
        and request_channel.get("model") == "governance-authenticated-shared-layer"
    )
    channel_runtime_entry = (
        str(request_channel.get("runtime_entry") or "").strip()
        if isinstance(request_channel, dict)
        else ""
    )
    expected_backend_entry = (
        f"independent_tool/{tool_id}/{channel_runtime_entry}"
        if governed_channel
        else "src-core/main.py"
    )
    runtime_contract = load_tool_runtime_contract()
    expected_protocol_version = runtime_contract["protocol_version"]
    expected_contract_version = runtime_contract["contract_version"]
    expected_python_runtime = "python/python.exe"
    semantic_errors: list[str] = []
    expected_source_roots = package_source_roots(
        tool_dir,
        resolve_entry(tool_dir, manifest),
    )
    packaged_source_roots = metadata.get("source_roots")
    normalized_packaged_roots = (
        sorted(str(item) for item in packaged_source_roots)
        if isinstance(packaged_source_roots, list)
        else []
    )
    if expected_source_roots and (
        not normalized_packaged_roots
        or not set(expected_source_roots).issubset(normalized_packaged_roots)
    ):
        semantic_errors.append(
            "package source roots do not match the current packaging contract"
        )
    if not isinstance(app_manifest, dict) or not isinstance(standalone, dict):
        semantic_errors.append("app manifest has no standalone configuration")
    else:
        if str(app_manifest.get("id") or "") != tool_id:
            semantic_errors.append("app manifest tool_id does not match")
        if str(app_manifest.get("version") or "") != expected_version:
            semantic_errors.append("app manifest version does not match")
        if (
            type(standalone.get("backend_port")) is not int
            or standalone.get("backend_port") != expected_port
        ):
            semantic_errors.append(
                "app manifest backend_port is not deterministic"
            )
        if standalone.get("isolated_backend") is not True:
            semantic_errors.append(
                "app manifest isolated_backend must be true"
            )
        if standalone.get("backend_entry") != expected_backend_entry:
            semantic_errors.append("app manifest backend_entry does not match")
        if standalone.get("governed_channel") != (
            "shared-layer" if governed_channel else ""
        ):
            semantic_errors.append("app manifest governed channel does not match")
        if (
            type(standalone.get("protocol_version")) is not int
            or standalone.get("protocol_version")
            != expected_protocol_version
        ):
            semantic_errors.append(
                "app manifest protocol_version does not match"
            )
        if standalone.get("python_runtime") != expected_python_runtime:
            semantic_errors.append(
                "app manifest python_runtime does not match"
            )
        if (
            int(standalone.get("runtime_contract_version") or 1)
            != expected_contract_version
        ):
            semantic_errors.append(
                "app manifest runtime contract version does not match"
            )
        if (
            str(standalone.get("backend_service_version") or "")
            != expected_version
        ):
            semantic_errors.append(
                "app manifest backend service version does not match"
            )

    if str(metadata.get("tool_id") or "") != tool_id:
        semantic_errors.append("package metadata tool_id does not match")
    if str(manifest.get("id") or tool_id) != tool_id:
        semantic_errors.append("source manifest tool_id does not match")
    if str(metadata.get("tool_version") or "") != expected_version:
        semantic_errors.append("package metadata tool version does not match")
    if (
        str(metadata.get("backend_service_version") or "")
        != expected_version
    ):
        semantic_errors.append(
            "package metadata backend service version does not match"
        )
    if (
        type(metadata.get("backend_port")) is not int
        or metadata.get("backend_port") != expected_port
    ):
        semantic_errors.append(
            "package metadata backend_port is not deterministic"
        )
    if metadata.get("isolated_backend") is not True:
        semantic_errors.append("package metadata isolated_backend must be true")
    if metadata.get("backend_entry") != expected_backend_entry:
        semantic_errors.append("package metadata backend_entry does not match")
    if metadata.get("governed_channel") != (
        "shared-layer" if governed_channel else ""
    ):
        semantic_errors.append("package metadata governed channel does not match")
    if (
        type(metadata.get("protocol_version")) is not int
        or metadata.get("protocol_version") != expected_protocol_version
    ):
        semantic_errors.append(
            "package metadata protocol_version does not match"
        )
    if metadata.get("python_runtime") != expected_python_runtime:
        semantic_errors.append("package metadata python_runtime does not match")
    if (
        int(metadata.get("runtime_contract_version") or 1)
        != expected_contract_version
    ):
        semantic_errors.append(
            "package metadata runtime contract version does not match"
        )

    if isinstance(standalone, dict):
        paired_fields = (
            "backend_port",
            "isolated_backend",
            "backend_entry",
            "governed_channel",
            "protocol_version",
            "runtime_contract_version",
            "python_runtime",
            "backend_service_version",
        )
        if any(
            metadata.get(field) != standalone.get(field)
            for field in paired_fields
        ):
            semantic_errors.append(
                "package metadata and app manifest standalone settings differ"
            )
    if not (app_dir / expected_backend_entry).is_file():
        semantic_errors.append("packaged backend entry is missing")
    if not (app_dir / expected_python_runtime).is_file():
        semantic_errors.append("packaged Python runtime is missing")

    if semantic_errors:
        return {
            **result,
            "ok": False,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
            "error_code": "PACKAGE_SEMANTICS_INVALID",
            "message": "; ".join(dict.fromkeys(semantic_errors)),
        }
    return {
        **result,
        "tool_id": tool_id,
        "exe_path": str(exe_path),
    }


def package_tool(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    try:
        with package_operation_lock(tool_id, tool_dir):
            return _package_tool_locked(tool_id, tool_dir, manifest)
    except PackageOperationBusy as error:
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_OPERATION_ACTIVE",
            "message": str(error),
        }
    except Exception as error:
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_LOCK_FAILED",
            "message": str(error),
        }


def verify_upgrade_result(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Verify a promoted package and retry one safe rebuild if necessary."""

    if not result.get("ok"):
        return result
    verification = verify_tool_package(tool_id, tool_dir, manifest)
    if verification.get("ok"):
        return {
            **result,
            "upgrade_auto_retry": False,
            "post_verification": verification,
        }

    retry = package_tool(tool_id, tool_dir, manifest)
    retry["upgrade_auto_retry"] = True
    retry["initial_post_verification"] = verification
    if not retry.get("ok"):
        return retry

    final_verification = verify_tool_package(tool_id, tool_dir, manifest)
    retry["post_verification"] = final_verification
    if final_verification.get("ok"):
        return retry
    return {
        **retry,
        "ok": False,
        "error_code": "UPGRADE_POST_VERIFY_FAILED",
        "message": str(
            final_verification.get("message")
            or "Package verification failed after automatic retry"
        ),
    }


def prune_completed_recovery_roots(
    tool_dir: Path,
    *,
    keep: int = MAX_COMPLETED_RECOVERY_GENERATIONS,
) -> list[str]:
    """Bound successful rollback generations without touching failed recovery."""

    build_root = (tool_dir / "build").resolve()
    if keep < 0 or not build_root.is_dir():
        return []
    candidates: list[Path] = []
    for candidate in build_root.glob("package-*"):
        try:
            if (
                not candidate.is_dir()
                or _is_link_or_reparse(candidate)
                or candidate.resolve(strict=True).parent != build_root
                or not (candidate / "promotion-complete.json").is_file()
                or not (candidate / "promotion-recovery-manifest.json").is_file()
            ):
                continue
            aborted_path = candidate / "promotion-aborted.json"
            if aborted_path.is_file():
                aborted = json.loads(aborted_path.read_text(encoding="utf-8"))
                if aborted.get("status") == "rollback-incomplete":
                    continue
            candidates.append(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    removed: list[str] = []
    for candidate in candidates[keep:]:
        try:
            shutil.rmtree(candidate)
        except OSError:
            # Promotion has already completed and the live package was
            # verified. A transient antivirus/Explorer lock on an older,
            # completed recovery generation must not turn that success into
            # a package failure. The retained recovery can be pruned later.
            continue
        removed.append(str(candidate))
    return removed


def _package_tool_locked(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version_check = validate_tool_version_baseline(tool_id, manifest)
    if not version_check["ok"]:
        return version_check
    entry = resolve_entry(tool_dir, manifest)
    backend_port = standalone_backend_port(tool_id)
    runtime_contract = load_tool_runtime_contract()
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
    running_process_ids = running_executable_process_ids(exe_path)
    restart_after_upgrade = bool(running_process_ids)
    desktop_stopped_for_upgrade = False
    desktop_restarted = False

    package_root = tool_dir / "build" / f"package-{uuid.uuid4().hex}"
    staged_dist = package_root / "dist"
    staged_exe = staged_dist / f"{executable_name}.exe"
    preserve_package_root = False
    promotion_recovery_root: Path | None = None

    try:
        source_roots = package_source_roots(tool_dir, entry)
        source_excluded_paths = package_source_excluded_paths(tool_dir, entry)
        source_excluded_path_set = frozenset(source_excluded_paths)
        source_files = collect_file_hashes(
            PROJECT_ROOT,
            source_roots,
            ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
            excluded_relative_paths=source_excluded_path_set,
        )
        source_digest = snapshot_digest(source_files)
        copy_electron_runtime(staged_dist, staged_exe)

        app_dir = staged_dist / "resources" / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(renderer_dir, app_dir / "renderer")
        runtime_path = copy_backend_source_bundle(
            tool_id,
            tool_dir,
            entry,
            app_dir,
        )
        python_runtime_path = copy_portable_python_runtime(app_dir)

        request_channel = manifest.get("request_channel")
        governed_channel = (
            isinstance(request_channel, dict)
            and request_channel.get("model")
            == "governance-authenticated-shared-layer"
        )
        channel_runtime_entry = (
            str(request_channel.get("runtime_entry") or "").strip()
            if isinstance(request_channel, dict)
            else ""
        )
        backend_entry_relative = (
            f"independent_tool/{tool_id}/{channel_runtime_entry}"
            if governed_channel
            else "src-core/main.py"
        )
        if governed_channel and (
            not channel_runtime_entry
            or Path(channel_runtime_entry).is_absolute()
            or ".." in Path(channel_runtime_entry).parts
        ):
            raise RuntimeError("Invalid governed request channel runtime entry")

        app_manifest = dict(manifest)
        app_manifest["id"] = tool_id
        app_manifest["version"] = str(manifest.get("version", "1.0.0"))
        app_manifest["standalone"] = {
            "backend_entry": backend_entry_relative,
            "backend_service_version": str(manifest.get("version", "1.0.0")),
            "backend_port": backend_port,
            "isolated_backend": True,
            "protocol_version": runtime_contract["protocol_version"],
            "runtime_contract_version": runtime_contract["contract_version"],
            "python_runtime": "python/python.exe",
            "governed_channel": "shared-layer" if governed_channel else "",
        }
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
        validate_staged_python_runtime(
            app_dir,
            tool_id,
            backend_entry_relative,
        )

        current_source_files = collect_file_hashes(
            PROJECT_ROOT,
            source_roots,
            ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
            excluded_relative_paths=source_excluded_path_set,
        )
        if current_source_files != source_files:
            raise RuntimeError(
                "Package inputs changed during the build; retry after edits finish"
            )

        payload_files = collect_file_hashes(
            app_dir,
            ["."],
            excluded_relative_paths=frozenset({PACKAGE_METADATA_NAME}),
        )
        package_metadata = {
            "format_version": PACKAGE_FORMAT_VERSION,
            "tool_id": tool_id,
            "tool_version": str(manifest.get("version", "1.0.0")),
            "backend_service_version": str(manifest.get("version", "1.0.0")),
            "backend_port": backend_port,
            "isolated_backend": True,
            "protocol_version": runtime_contract["protocol_version"],
            "runtime_contract_version": runtime_contract["contract_version"],
            "backend_entry": backend_entry_relative,
            "python_runtime": "python/python.exe",
            "governed_channel": "shared-layer" if governed_channel else "",
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_roots": source_roots,
            "source_excluded_paths": source_excluded_paths,
            "source_files": source_files,
            "source_digest": source_digest,
            "payload_files": payload_files,
            "payload_digest": snapshot_digest(payload_files),
        }
        (app_dir / PACKAGE_METADATA_NAME).write_text(
            json.dumps(package_metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        staged_verification = verify_packaged_app(
            app_dir,
            project_root=PROJECT_ROOT,
        )
        if not staged_verification.get("ok"):
            raise RuntimeError(
                f"Staged package verification failed: "
                f"{staged_verification.get('message', 'unknown error')}"
            )

        latest_process_ids = running_executable_process_ids(exe_path)
        if latest_process_ids:
            running_process_ids = latest_process_ids
            restart_after_upgrade = True
        stop_verified_packaged_backend(
            tool_id,
            dist_dir / "resources" / "app",
        )
        if restart_after_upgrade:
            if not stop_running_executable_for_upgrade(
                exe_path,
                running_process_ids,
            ):
                raise RuntimeError(
                    "The verified standalone tool did not stop for its "
                    "production upgrade"
                )
            desktop_stopped_for_upgrade = True
        promotion_recovery_root = promote_staged_distribution(
            staged_dist,
            dist_dir,
        )
        if promotion_recovery_root is not None:
            preserve_package_root = True
            live_verification = verify_packaged_app(
                dist_dir / "resources" / "app",
                project_root=PROJECT_ROOT,
            )
            if not live_verification.get("ok"):
                raise RuntimeError(
                    "Promoted package verification failed before old package "
                    f"cleanup: {live_verification.get('message', 'unknown error')}"
                )
            shutil.rmtree(promotion_recovery_root)
            promotion_recovery_root = None
            preserve_package_root = False
        if restart_after_upgrade:
            desktop_restarted = restart_packaged_executable(exe_path)
            if not desktop_restarted:
                raise RuntimeError(
                    "The package was promoted but the upgraded tool could "
                    "not be restarted automatically"
                )
    except PromotionRecoveryRequired as exc:
        preserve_package_root = True
        if (
            desktop_stopped_for_upgrade
            and not running_executable_process_ids(exe_path)
        ):
            desktop_restarted = restart_packaged_executable(exe_path)
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "exe_path": str(exe_path),
            "error_code": "PROMOTION_ROLLBACK_INCOMPLETE",
            "message": str(exc),
            "recovery_path": str(exc.recovery_root),
            "rollback_errors": list(exc.rollback_errors),
            "desktop_restarted": desktop_restarted,
        }
    except Exception as exc:
        retained_recovery = (
            (package_root / "promotion-aborted.json").exists()
            or (package_root / "promotion-recovery-manifest.json").exists()
            or (package_root / "previous-dist").exists()
            or any(package_root.glob("failed-new-dist*"))
        )
        if retained_recovery:
            preserve_package_root = True
        if (
            desktop_stopped_for_upgrade
            and not running_executable_process_ids(exe_path)
        ):
            desktop_restarted = restart_packaged_executable(exe_path)
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "exe_path": str(exe_path),
            "error_code": (
                "PROMOTION_FAILED_RECOVERY_RETAINED"
                if retained_recovery
                else "PACKAGE_FAILED"
            ),
            "message": str(exc),
            "diagnostic": traceback.format_exc(limit=12),
            "recovery_path": (
                str(package_root) if retained_recovery else ""
            ),
            "desktop_restarted": desktop_restarted,
        }
    finally:
        contains_recovery = (
            (package_root / "previous-dist").exists()
            or (package_root / "previous-runtime").exists()
            or (package_root / "previous-app").exists()
            or any(package_root.glob("failed-new-dist*"))
            or (package_root / "promotion-recovery-manifest.json").exists()
            or (package_root / "promotion-aborted.json").exists()
        )
        if (
            package_root.exists()
            and not preserve_package_root
            and not contains_recovery
        ):
            shutil.rmtree(package_root, ignore_errors=True)

    pruned_recovery_paths = prune_completed_recovery_roots(tool_dir)
    return {
        "ok": exe_path.exists(),
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "renderer_path": str(dist_dir / "resources" / "app" / "renderer"),
        "runtime_path": (
            str(dist_dir / "resources" / "app" / runtime_path.relative_to(app_dir))
            if runtime_path
            else ""
        ),
        "python_runtime_path": str(
            dist_dir
            / "resources"
            / "app"
            / python_runtime_path.relative_to(app_dir)
        ),
        "package_digest": staged_verification.get("package_digest", ""),
        "backend_port": backend_port,
        "runtime_contract_version": runtime_contract["contract_version"],
        "production_upgrade": (
            "verified-stop-promote-restart"
            if restart_after_upgrade
            else "verified-promote"
        ),
        "desktop_restarted": desktop_restarted,
        "pruned_recovery_paths": pruned_recovery_paths,
        "recovery_path": (
            str(promotion_recovery_root)
            if promotion_recovery_root is not None
            else ""
        ),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Package platform tools as standalone EXEs.")
    parser.add_argument("tool_ids", nargs="*", help="Optional tool ids to package.")
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_tools",
        help="Explicitly operate on every platform tool.",
    )
    parser.add_argument(
        "--changed",
        action="store_true",
        help="Package only missing, stale, or invalid platform tool EXEs.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--build-renderers-only",
        action="store_true",
        help="Only build per-tool renderer bundles without assembling Electron EXEs.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify packaged EXEs and confirm their source snapshots are current.",
    )
    args = parser.parse_args()

    scope_count = int(args.all_tools) + int(args.changed) + int(bool(args.tool_ids))
    if scope_count != 1:
        parser.error("Specify tool ids, --changed, or --all (exactly one scope)")
    if args.changed and (args.verify or args.build_renderers_only):
        parser.error("--changed is only available for package assembly")
    selected_ids = None if (args.all_tools or args.changed) else set(args.tool_ids)
    tools = iter_tools(
        selected_ids,
        include_special_unpacked=args.build_renderers_only,
    )
    if args.build_renderers_only:
        tools = [
            item
            for item in tools
            if item[0] not in {"governance_rule", "shared-layer", "local-ai"}
            and item[2].get("has_custom_ui") is not False
        ]
        known_ids = {tool_id for tool_id, _, _ in tools}
        tools.extend(
            item
            for item in iter_companion_renderer_tools(selected_ids)
            if item[0] not in known_ids
        )
    auto_repair: dict[str, Any] | None = None
    post_repair: dict[str, Any] | None = None
    if args.changed:
        tools = [
            item
            for item in tools
            if not verify_tool_package(*item).get("ok")
        ]
    if args.verify:
        results = [
            verify_tool_package(tool_id, tool_dir, manifest)
            for tool_id, tool_dir, manifest in tools
        ]
    elif args.build_renderers_only:
        results = [
            build_platform_renderer(tool_id, tool_dir)
            for tool_id, tool_dir, _ in tools
        ]
    else:
        results = [
            package_tool(tool_id, tool_dir, manifest)
            for tool_id, tool_dir, manifest in tools
        ]
        results = [
            verify_upgrade_result(tool_id, tool_dir, manifest, result)
            for (tool_id, tool_dir, manifest), result in zip(
                tools,
                results,
                strict=True,
            )
        ]
    ok = (
        all(result.get("ok") for result in results)
        and (auto_repair is None or bool(auto_repair.get("ok")))
        and (post_repair is None or bool(post_repair.get("ok")))
    )

    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "auto_repair": auto_repair,
                    "post_repair": post_repair,
                    "results": results,
                },
                ensure_ascii=False,
            )
        )
    else:
        for result in results:
            status = "OK" if result.get("ok") else "FAIL"
            target = result.get("exe_path") or result.get("renderer_path") or result.get("message")
            print(f"[{status}] {result.get('tool_id')}: {target}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
