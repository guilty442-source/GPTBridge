"""IPC session-token management, authorization, and related constants.

Extracted from ``ipc.server`` to keep each module focused and under 500 lines.
All names here are re-exported by ``ipc.server`` for backward compatibility.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import socket  # noqa: F401  (kept for parity with original import block)
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


# ------------------------------------------------------------------
# Environment / configuration constants
# ------------------------------------------------------------------

SHUTDOWN_TOKEN_ENV = "GPTBRIDGE_SHUTDOWN_TOKEN"
IPC_SESSION_TOKEN_ENV = "GPTBRIDGE_IPC_SESSION_TOKEN"
IPC_STATE_ROOT_ENV = "GPTBRIDGE_IPC_STATE_ROOT"
IPC_PORT_ENV = "GPTBRIDGE_IPC_PORT"
DEFAULT_IPC_PORT = 8765

# ------------------------------------------------------------------
# Internal constants
# ------------------------------------------------------------------

_IPC_SESSION_TOKEN: str | None = None
_IPC_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_IPC_TOKEN_LOCK_NAME = ".session-token.lock"
_IPC_TOKEN_LOCK_WAIT_SECONDS = 10.0
_IPC_TOKEN_STALE_LOCK_SECONDS = 5.0
_WINDOWS_FILE_REPLACE_RETRY_SECONDS = 2.0
_WINDOWS_FILE_REPLACE_RETRY_INTERVAL_SECONDS = 0.025


# ------------------------------------------------------------------
# Shared subprocess helper
# ------------------------------------------------------------------

def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


# ------------------------------------------------------------------
# IPC port / state-root helpers
# ------------------------------------------------------------------

def _ipc_port() -> int:
    configured = str(os.environ.get(IPC_PORT_ENV) or "").strip()
    if not configured:
        return DEFAULT_IPC_PORT
    try:
        port = int(configured)
    except ValueError as exc:
        raise RuntimeError(f"{IPC_PORT_ENV} must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise RuntimeError(f"{IPC_PORT_ENV} must be between 1024 and 65535")
    return port


def _ipc_state_root() -> Path:
    configured = str(os.environ.get(IPC_STATE_ROOT_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        base = (
            Path(local_app_data).expanduser()
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
        base = (
            Path(xdg_state_home).expanduser()
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
    return (base / "GPTBridge" / "ipc").resolve()


def _ipc_session_token_file() -> Path:
    return _ipc_state_root() / "session-token"


def _workspace_instance_id(project_root: str | Path | None = None) -> str:
    root = Path(
        project_root
        or os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        or Path(__file__).resolve().parents[2]
    ).expanduser().absolute()
    normalized = os.path.normcase(str(root)).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _valid_ipc_session_token(value: str) -> bool:
    return _IPC_TOKEN_PATTERN.fullmatch(value.strip().lower()) is not None


# ------------------------------------------------------------------
# File-system hardening helpers
# ------------------------------------------------------------------

def _harden_private_path(target_path: Path, *, directory: bool = False) -> None:
    try:
        target_path.chmod(0o700 if directory else 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    username = str(os.environ.get("USERNAME") or "").strip()
    if not username:
        return
    try:
        subprocess.run(
            [
                "icacls.exe",
                str(target_path),
                "/inheritance:r",
                "/grant:r",
                f"{username}:{'(OI)(CI)(F)' if directory else '(R,W)'}",
                "/grant:r",
                f"*S-1-5-18:{'(OI)(CI)(F)' if directory else '(F)'}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            **_background_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return


# ------------------------------------------------------------------
# Token read / write / repair
# ------------------------------------------------------------------

def _read_ipc_token(token_path: Path) -> str:
    try:
        token = token_path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    return token if _valid_ipc_session_token(token) else ""


def _replace_path_atomically(source_path: Path, target_path: Path) -> None:
    deadline = time.monotonic() + _WINDOWS_FILE_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source_path, target_path)
            return
        except PermissionError:
            # Windows can briefly deny a rename while another process is
            # reading the shared token. Keep the atomic replace semantics and
            # allow those short-lived readers to finish.
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(_WINDOWS_FILE_REPLACE_RETRY_INTERVAL_SECONDS)


def _break_stale_ipc_token_lock(lock_path: Path) -> None:
    try:
        age_seconds = time.time() - lock_path.lstat().st_mtime
    except OSError:
        return
    if age_seconds < _IPC_TOKEN_STALE_LOCK_SECONDS:
        return

    stale_path = lock_path.with_name(
        f"{lock_path.name}.stale-{os.getpid()}-{secrets.token_hex(6)}"
    )
    try:
        lock_path.rename(stale_path)
    except OSError:
        return
    try:
        stale_path.rmdir()
    except OSError:
        # Never recursively delete unknown contents. Renaming the stale lock is
        # enough to free the canonical lock name for recovery.
        pass


def _write_ipc_token_atomically(token_path: Path, token: str) -> None:
    temporary_path = token_path.with_name(
        f".{token_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            descriptor = None
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_path_atomically(temporary_path, token_path)
        if os.name != "nt":
            try:
                directory_fd = os.open(token_path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except OSError:
            pass


def _repair_or_create_ipc_token(token_path: Path) -> str:
    state_root = token_path.parent
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _harden_private_path(state_root, directory=True)
    lock_path = state_root / _IPC_TOKEN_LOCK_NAME
    deadline = time.monotonic() + _IPC_TOKEN_LOCK_WAIT_SECONDS
    owns_lock = False
    owner_nonce = ""

    while not owns_lock:
        raced_token = _read_ipc_token(token_path)
        if raced_token:
            return raced_token
        try:
            lock_path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            try:
                owner_nonce = secrets.token_hex(16)
                owner_path = lock_path / "owner"
                descriptor = os.open(
                    owner_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(owner_nonce + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                _harden_private_path(owner_path)
                owns_lock = True
                break
            except OSError:
                try:
                    (lock_path / "owner").unlink()
                    lock_path.rmdir()
                except OSError:
                    pass
                raise

        _break_stale_ipc_token_lock(lock_path)
        if time.monotonic() >= deadline:
            final_token = _read_ipc_token(token_path)
            if final_token:
                return final_token
            raise TimeoutError(f"Timed out acquiring IPC token lock: {lock_path}")
        time.sleep(0.025)

    def still_owns_lock() -> bool:
        if not owns_lock or not owner_nonce:
            return False
        try:
            return (lock_path / "owner").read_text(
                encoding="ascii"
            ).strip() == owner_nonce
        except OSError:
            return False

    try:
        raced_token = _read_ipc_token(token_path)
        if raced_token:
            return raced_token
        if not still_owns_lock():
            raise OSError("Lost IPC token repair lock")
        try:
            os.utime(lock_path, None)
        except OSError as exc:
            raise OSError("Cannot refresh IPC token repair lock") from exc

        try:
            token_path.lstat()
        except OSError:
            pass
        else:
            quarantine_path = token_path.with_name(
                f"{token_path.name}.invalid-{time.time_ns()}-"
                f"{os.getpid()}-{secrets.token_hex(6)}"
            )
            _replace_path_atomically(token_path, quarantine_path)

        generated = secrets.token_hex(32)
        _write_ipc_token_atomically(token_path, generated)
        _harden_private_path(token_path)
        persisted = _read_ipc_token(token_path)
        if not persisted:
            raise OSError("IPC session token write verification failed")
        return persisted
    finally:
        if still_owns_lock():
            try:
                (lock_path / "owner").unlink()
                lock_path.rmdir()
            except OSError:
                pass


def _get_or_create_ipc_session_token() -> str:
    global _IPC_SESSION_TOKEN
    if _IPC_SESSION_TOKEN:
        return _IPC_SESSION_TOKEN

    configured = str(os.environ.get(IPC_SESSION_TOKEN_ENV) or "").strip().lower()
    if _valid_ipc_session_token(configured):
        _IPC_SESSION_TOKEN = configured
        return configured

    token_path = _ipc_session_token_file()
    existing = _read_ipc_token(token_path)
    if existing:
        _harden_private_path(token_path.parent, directory=True)
        _harden_private_path(token_path)
        _IPC_SESSION_TOKEN = existing
        return existing

    try:
        generated = _repair_or_create_ipc_token(token_path)
    except OSError:
        return ""

    if not _valid_ipc_session_token(generated):
        return ""
    _IPC_SESSION_TOKEN = generated
    return generated


# ------------------------------------------------------------------
# Request authorization
# ------------------------------------------------------------------

def _websocket_request_authorized(request: Any) -> bool:
    expected = _get_or_create_ipc_session_token()
    if not expected:
        return False
    try:
        query = parse_qs(urlsplit(str(request.path)).query)
        provided = str((query.get("token") or [""])[0]).strip()
        provided_instance = str((query.get("instance") or [""])[0]).strip()
    except Exception:
        return False
    return (
        bool(provided)
        and hmac.compare_digest(provided, expected)
        and bool(provided_instance)
        and hmac.compare_digest(provided_instance, _workspace_instance_id())
    )


def _shutdown_request_authorized(request: Any) -> bool:
    expected = os.environ.get(SHUTDOWN_TOKEN_ENV, "").strip()
    if not expected:
        return False
    try:
        provided = str(request.headers.get("X-GPTBridge-Shutdown-Token", "")).strip()
    except Exception:
        return False
    return bool(provided) and hmac.compare_digest(provided, expected)


def _shutdown_request_is_manual(request: Any) -> bool:
    try:
        reason = str(
            request.headers.get("X-GPTBridge-Shutdown-Reason", "")
        ).strip().casefold()
    except Exception:
        reason = ""
    return reason != "hot-update"
