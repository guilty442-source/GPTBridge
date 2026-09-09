from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

try:
    from .privacy import decode_json_document, encode_json_document
    from ..domain.contract import (
        INVESTMENT_APP_VERSION,
        INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION,
        INVESTMENT_STATE_SCHEMA_VERSION,
    )
except ImportError:
    _privacy_path = Path(__file__).with_name("privacy.py")
    _privacy_spec = importlib.util.spec_from_file_location(
        "investment_watch_privacy",
        _privacy_path,
    )
    if _privacy_spec is None or _privacy_spec.loader is None:
        raise
    _privacy_module = importlib.util.module_from_spec(_privacy_spec)
    sys.modules[_privacy_spec.name] = _privacy_module
    _privacy_spec.loader.exec_module(_privacy_module)
    decode_json_document = _privacy_module.decode_json_document
    encode_json_document = _privacy_module.encode_json_document
    _contract_path = Path(__file__).resolve().parent.parent / "domain" / "contract.py"
    _contract_spec = importlib.util.spec_from_file_location(
        "investment_watch_contract",
        _contract_path,
    )
    if _contract_spec is None or _contract_spec.loader is None:
        raise
    _contract_module = importlib.util.module_from_spec(_contract_spec)
    sys.modules[_contract_spec.name] = _contract_module
    _contract_spec.loader.exec_module(_contract_module)
    INVESTMENT_APP_VERSION = _contract_module.INVESTMENT_APP_VERSION
    INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION = (
        _contract_module.INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION
    )
    INVESTMENT_STATE_SCHEMA_VERSION = (
        _contract_module.INVESTMENT_STATE_SCHEMA_VERSION
    )


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


DATA_ROOT_ENV = "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT"
PROFILE_ENV = "GPTBRIDGE_AI_ASSISTANT_PROFILE"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class InvestmentStateRecoveryRequired(RuntimeError):
    """Raised when persisted state exists but cannot be safely decoded."""


class InvestmentStateUpgradeRequired(RuntimeError):
    """Raised when state was written by a newer, unsupported application."""


def _pid_is_alive(pid: int) -> bool:
    """Compatibility diagnostic; ownership decisions use OS file locks."""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if process:
            try:
                exit_code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                ):
                    return exit_code.value == 259  # STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _try_lock_descriptor(descriptor: int) -> bool:
    """Acquire an OS-owned exclusive lock without relying on PID metadata."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _normalized_profile(value: Any) -> str:
    profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "default").strip())
    return profile.strip(".-")[:64] or "default"


def _same_path_identity(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeError(f"Cannot safely inspect investment path {path}") from error
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return path.is_symlink() or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _lexical_absolute(path: Path, *, label: str) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        raise RuntimeError(f"{label} must be an absolute path: {requested}")
    normalized = Path(os.path.abspath(str(requested)))
    if not _same_path_identity(requested, normalized):
        raise RuntimeError(f"{label} must not contain traversal: {requested}")
    return normalized


def _validated_storage_path(
    path: Path,
    *,
    label: str,
    boundary: Path | None = None,
    require_exists: bool = False,
    expected_kind: str | None = None,
) -> Path:
    """Validate a lexical path without resolving links outside its boundary."""

    requested = _lexical_absolute(path, label=label)
    if boundary is not None:
        validated_boundary = _lexical_absolute(boundary, label=f"{label} boundary")
        try:
            requested.relative_to(validated_boundary)
        except ValueError as error:
            raise RuntimeError(
                f"{label} escaped its storage boundary: {requested}"
            ) from error

    chain = [*reversed(requested.parents), requested]
    for candidate in chain:
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"Cannot safely inspect {label}: {candidate}") from error
        if _is_link_or_reparse(candidate):
            raise RuntimeError(
                f"{label} cannot use a symlink, junction, or reparse point: "
                f"{candidate}"
            )
        try:
            canonical = candidate.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(f"Cannot safely resolve {label}: {candidate}") from error
        if not _same_path_identity(candidate, canonical):
            raise RuntimeError(
                f"{label} has a non-canonical ancestor: {candidate}"
            )

    exists = requested.exists() or requested.is_symlink()
    if require_exists and not exists:
        raise RuntimeError(f"{label} does not exist: {requested}")
    if exists:
        requested_stat = requested.lstat()
        if expected_kind == "directory" and not stat.S_ISDIR(
            requested_stat.st_mode
        ):
            raise RuntimeError(f"{label} is not a directory: {requested}")
        if expected_kind == "file" and not stat.S_ISREG(requested_stat.st_mode):
            raise RuntimeError(f"{label} is not a regular file: {requested}")
    return requested


def _iter_migration_files(
    root: Path,
    *,
    label: str = "Legacy investment state",
) -> Iterator[Path]:
    validated_root = _validated_storage_path(
        root,
        label=f"{label} root",
        require_exists=True,
        expected_kind="directory",
    )

    def migration_error(error: OSError) -> None:
        raise RuntimeError(
            f"Cannot safely enumerate {label} root: {validated_root}"
        ) from error

    for current_root, directory_names, file_names in os.walk(
        validated_root,
        topdown=True,
        onerror=migration_error,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current = _validated_storage_path(
            Path(current_root),
            label=f"{label} directory",
            boundary=validated_root,
            require_exists=True,
            expected_kind="directory",
        )
        for name in directory_names:
            _validated_storage_path(
                current / name,
                label=f"{label} directory",
                boundary=validated_root,
                require_exists=True,
                expected_kind="directory",
            )
        for name in file_names:
            yield _validated_storage_path(
                current / name,
                label=f"{label} file",
                boundary=validated_root,
                require_exists=True,
                expected_kind="file",
            )


def _runtime_root(tool_root: Path) -> Path:
    configured_root = str(os.environ.get(DATA_ROOT_ENV) or "").strip()
    profile = _normalized_profile(os.environ.get(PROFILE_ENV))
    if configured_root:
        base = _validated_storage_path(
            Path(configured_root),
            label="Configured AI investment data root",
            expected_kind="directory",
        )
        return _validated_storage_path(
            base / profile,
            label="AI investment profile root",
            boundary=base,
            expected_kind="directory",
        )
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data and tool_root.name.lower() == "ai-assistant":
        base = _validated_storage_path(
            Path(local_app_data),
            label="Local application data root",
            expected_kind="directory",
        )
        return _validated_storage_path(
            base / "GPTBridge" / "ai-assistant" / profile,
            label="AI investment profile root",
            boundary=base,
            expected_kind="directory",
        )
    # Preserve embedders and isolated test roots that intentionally own their runtime.
    return _validated_storage_path(
        tool_root / "runtime",
        label="AI investment runtime root",
        boundary=tool_root,
        expected_kind="directory",
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    _validated_storage_path(
        path.parent,
        label="Investment data directory",
        expected_kind="directory",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _validated_storage_path(
        path.parent,
        label="Investment data directory",
        require_exists=True,
        expected_kind="directory",
    )
    _validated_storage_path(
        path,
        label="Investment data file",
        boundary=path.parent,
        expected_kind="file",
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_verified(source: Path, destination: Path) -> bool:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if destination.exists():
        return hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    _atomic_write_bytes(destination, payload)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise OSError(f"runtime migration verification failed: {destination}")
    return True
