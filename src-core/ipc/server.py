import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from main import GPTBridgeApp

import websockets # type: ignore


class _ExpectedProbeNoiseFilter(logging.Filter):
    """Hide expected health/TCP probe disconnects without hiding real errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != "opening handshake failed":
            return True
        exception = record.exc_info[1] if record.exc_info else None
        return not isinstance(
            exception,
            (
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.InvalidMessage,
            ),
        )

# Safe fallback for older websockets versions to prevent ImportError crashes
try:
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    def http_response(status_code: int, reason: str, body: bytes, content_type: str = "text/plain") -> Any:
        return Response(
            status_code,
            reason,
            Headers(
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(body))),
                ]
            ),
            body,
        )
except ImportError:
    import http
    def http_response(status_code: int, reason: str, body: bytes, content_type: str = "text/plain") -> Any:
        status = http.HTTPStatus(status_code)
        return (status, [("Content-Type", content_type), ("Content-Length", str(len(body)))], body)

from core.ui_shell import UIShell


SHUTDOWN_TOKEN_ENV = "GPTBRIDGE_SHUTDOWN_TOKEN"
IPC_SESSION_TOKEN_ENV = "GPTBRIDGE_IPC_SESSION_TOKEN"
IPC_STATE_ROOT_ENV = "GPTBRIDGE_IPC_STATE_ROOT"
IPC_PORT_ENV = "GPTBRIDGE_IPC_PORT"
STANDALONE_TOOL_ID_ENV = "GPTBRIDGE_STANDALONE_TOOL_ID"
DEFAULT_IPC_PORT = 8765
TRUSTED_WEBSOCKET_ORIGINS = (
    None,
    "file://",
    "null",
    "http://127.0.0.1:5180",
    "http://localhost:5180",
    "http://127.0.0.1:5183",
    "http://localhost:5183",
)
MAX_CONNECTION_COMMAND_TASKS = 32
_IPC_SESSION_TOKEN: str | None = None
_IPC_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_IPC_TOKEN_LOCK_NAME = ".session-token.lock"
_IPC_TOKEN_LOCK_WAIT_SECONDS = 10.0
_IPC_TOKEN_STALE_LOCK_SECONDS = 5.0
_WINDOWS_FILE_REPLACE_RETRY_SECONDS = 2.0
_WINDOWS_FILE_REPLACE_RETRY_INTERVAL_SECONDS = 0.025


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


def _standalone_tool_id() -> str:
    configured = str(os.environ.get(STANDALONE_TOOL_ID_ENV) or "").strip()
    if not configured:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", configured):
        raise RuntimeError(
            f"{STANDALONE_TOOL_ID_ENV} contains an invalid tool identifier"
        )
    return configured


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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return


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
    return reason != "hot-reload"


def _get_port_owner(port: int) -> tuple[int | None, str | None]:
    if socket is None or sys.platform != "win32":
        return None, None
    try:
        output = subprocess.check_output(["netstat", "-ano"], text=True, encoding="utf-8", errors="ignore")
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                pid = parts[-1]
                proc = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                ).strip()
                try:
                    return int(pid), f"PID {pid} ({proc})"
                except ValueError:
                    return None, f"PID {pid} ({proc})"
    except Exception:
        return None, None
    return None, None


def _query_process_commandline(pid: int) -> tuple[str | None, str | None]:
    if sys.platform != "win32":
        return None, None
    try:
        output = subprocess.check_output(
            [
                "wmic",
                "process",
                "where",
                f"ProcessId={pid}",
                "get",
                "CommandLine,ExecutablePath",
                "/FORMAT:LIST",
            ],
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        cmdline = None
        exe_path = None
        for line in output.splitlines():
            if line.startswith("CommandLine="):
                cmdline = line.partition("=")[2].strip()
            elif line.startswith("ExecutablePath="):
                exe_path = line.partition("=")[2].strip()
        return cmdline, exe_path
    except Exception:
        return None, None


def _is_gptbridge_process(pid: int, project_root: Path) -> bool:
    if sys.platform != "win32":
        return False
    cmdline, exe_path = _query_process_commandline(pid)
    if cmdline:
        normalized = cmdline.lower()
        project_path_lower = str(project_root).lower()
        if project_path_lower in normalized:
            return True
        if "run.py" in normalized and "--serve" in normalized:
            return True
        if "src-core\\main.py" in normalized or "src-core/main.py" in normalized:
            return True
        if "gptbridge" in normalized and project_path_lower in normalized:
            return True
    if exe_path:
        normalized_exe = exe_path.lower()
        if str(project_root).lower() in normalized_exe:
            return True
    return False


def _kill_process(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
        )
        return completed.returncode == 0
    except Exception:
        return False


def result_event_for_command(command: str) -> str:
    return f"{command}_result"

def command_result_ok(payload_out: Any) -> bool:
    if not isinstance(payload_out, dict):
        return True
    ok_value = payload_out.get("ok")
    if isinstance(ok_value, bool):
        return ok_value
    status = str(payload_out.get("status", "")).strip().lower()
    if status in {"success", "ok", "completed"}:
        return True
    if status in {"error", "failed", "failure", "blocked", "cancelled"}:
        return False
    return True


_INVESTMENT_WATCH_LOG_OMITTED_FIELDS = frozenset(
    {"state", "diagnostics", "excel_mapping_preview"}
)
_INVESTMENT_WATCH_LOG_PRIMARY_FIELDS = (
    "ok",
    "request_id",
    "not_modified",
    "state_revision",
    "status",
    "message",
    "error",
    "error_id",
    "error_logged",
)
_INVESTMENT_WATCH_LOG_SUMMARY_FIELDS = (
    "version",
    "import_mode",
    "source_file_released",
    "source_file_modified",
    "local_only",
    "scheduled",
    "created",
    "updated",
    "deleted",
    "restored",
    "holding_id",
    "transaction_id",
    "event_id",
    "rule_id",
    "decision_id",
    "import_id",
    "backup_id",
    "file_name",
    "sheet_name",
    "mode",
    "provider",
)


def _scalar_log_summary(value: Any, *, max_items: int = 20) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    summary: dict[str, Any] = {}
    for key, item in value.items():
        if len(summary) >= max_items:
            break
        if isinstance(item, (str, int, float, bool)) or item is None:
            summary[str(key)] = item
    return summary


def _add_collection_count(
    counts: dict[str, int],
    name: str,
    value: Any,
) -> None:
    if isinstance(value, (list, tuple, set)):
        counts[name] = len(value)


def _investment_watch_result_log_payload(
    command: str,
    payload_out: Any,
) -> dict[str, Any]:
    """Build a bounded diagnostic summary without changing the IPC response."""
    if not isinstance(payload_out, dict):
        return {
            "command": command,
            "counts": {},
            "summary": {"result_type": type(payload_out).__name__},
        }

    log_payload: dict[str, Any] = {"command": command}
    for key in _INVESTMENT_WATCH_LOG_PRIMARY_FIELDS:
        if key in payload_out:
            log_payload[key] = payload_out[key]

    counts: dict[str, int] = {"top_level_fields": len(payload_out)}
    summary = _scalar_log_summary(payload_out.get("summary"))
    for key in _INVESTMENT_WATCH_LOG_SUMMARY_FIELDS:
        value = payload_out.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None and key in payload_out:
            summary.setdefault(key, value)

    state = payload_out.get("state")
    if isinstance(state, dict):
        counts["state_fields"] = len(state)
        _add_collection_count(counts, "holdings", state.get("holdings"))
        _add_collection_count(
            counts,
            "portfolio_versions",
            state.get("portfolio_versions"),
        )
        analytics = state.get("analytics")
        if isinstance(analytics, dict):
            for key in (
                "transactions",
                "events",
                "decisions",
                "alerts",
                "alert_rules",
                "equity_curve",
            ):
                _add_collection_count(
                    counts,
                    f"analytics.{key}",
                    analytics.get(key),
                )
        portfolio = state.get("portfolio")
        if isinstance(portfolio, dict):
            portfolio_summary = _scalar_log_summary(
                {
                    key: portfolio.get(key)
                    for key in (
                        "file_name",
                        "holding_count",
                        "imported_at",
                        "source_type",
                        "sheet_name",
                    )
                    if key in portfolio
                }
            )
            if portfolio_summary:
                summary["portfolio"] = portfolio_summary
        if "updated_at" in state:
            summary["state_updated_at"] = state.get("updated_at")

    diagnostics = payload_out.get("diagnostics")
    if isinstance(diagnostics, dict):
        counts["diagnostic_fields"] = len(diagnostics)
        for key, value in diagnostics.items():
            _add_collection_count(counts, f"diagnostics.{key}", value)
        diagnostic_summary = _scalar_log_summary(
            {
                key: diagnostics.get(key)
                for key in ("state", "state_label", "status", "severity", "message")
                if key in diagnostics
            }
        )
        if diagnostic_summary:
            summary["diagnostic_status"] = diagnostic_summary

    excel_preview = payload_out.get("excel_mapping_preview")
    if isinstance(excel_preview, dict):
        sheets = excel_preview.get("sheets")
        _add_collection_count(counts, "excel_sheets", sheets)
        _add_collection_count(
            counts,
            "excel_mapping_fields",
            excel_preview.get("mapping_fields"),
        )
        _add_collection_count(
            counts,
            "excel_required_fields",
            excel_preview.get("required_fields"),
        )
        if isinstance(sheets, list):
            counts["excel_preview_rows"] = sum(
                len(sheet.get("rows"))
                for sheet in sheets
                if isinstance(sheet, dict) and isinstance(sheet.get("rows"), list)
            )
        excel_summary = _scalar_log_summary(
            {
                key: excel_preview.get(key)
                for key in ("sheet_count", "selected_sheet_name", "file_name")
                if key in excel_preview
            }
        )
        if excel_summary:
            summary["excel_mapping"] = excel_summary

    for key, value in payload_out.items():
        if key in _INVESTMENT_WATCH_LOG_OMITTED_FIELDS:
            continue
        _add_collection_count(counts, key, value)
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                _add_collection_count(
                    counts,
                    f"{key}.{nested_key}",
                    nested_value,
                )

    log_payload["counts"] = counts
    log_payload["summary"] = summary
    return log_payload


def _write_core_log_safely(
    app: "GPTBridgeApp",
    category: str,
    message: str,
    payload: Any,
) -> None:
    logger = getattr(app, "core_logger", None)
    if logger is None:
        return
    try:
        logger.write(category, message, payload)
    except Exception as exc:
        # Logging is auxiliary. A failed sink must never replace an already
        # completed command result with a synthetic command failure.
        print(f"[IPC] Core log write failed for '{message}': {exc}")


def _toolbox_result_log_payload(payload: Any) -> dict[str, Any]:
    """Keep tool output out of the main-program log boundary."""
    if not isinstance(payload, dict):
        return {"ok": False, "payload_type": type(payload).__name__}

    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    error = payload.get("error")
    error_code = payload.get("error_code")
    if not error_code and isinstance(error, dict):
        error_code = error.get("code")
    return {
        "ok": bool(payload.get("ok")),
        "tool_id": str(payload.get("tool_id") or ""),
        "request_id": str(payload.get("request_id") or ""),
        "status": str(payload.get("status") or ""),
        "exit_code": payload.get("exit_code"),
        "cancelled": bool(payload.get("cancelled")),
        "timed_out": bool(payload.get("timed_out")),
        "error_code": str(error_code or ""),
        "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
        "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
        "stdout_sha256": hashlib.sha256(
            stdout.encode("utf-8", errors="replace")
        ).hexdigest() if stdout else "",
        "stderr_sha256": hashlib.sha256(
            stderr.encode("utf-8", errors="replace")
        ).hexdigest() if stderr else "",
        "stdout_truncated": bool(payload.get("stdout_truncated")),
        "stderr_truncated": bool(payload.get("stderr_truncated")),
    }


async def process_command_task(
    app: "GPTBridgeApp",
    ui: UIShell,
    command: str,
    payload: Dict[str, Any],
) -> None:
    task_record = None
    try:
        if getattr(app, "task_queue", None):
            task_record = await app.task_queue.begin(command, payload, ui.send_event)
            if task_record is not None and task_record.status == "blocked":
                await ui.send_event(
                    "task_blocked_result",
                    {"ok": False, "command": command, "message": task_record.message},
                )
                return

        if (
            command == "toolbox_run_tool"
            and getattr(app.command_router, "scope", "main") == "standalone"
            and getattr(
            app.command_router, "toolbox_service", None
            )
        ):
            event_name = "toolbox_run_tool_result"
            payload_out = await app.command_router.toolbox_service.run_tool(
                payload, event_callback=ui.send_event
            )
        else:
            event_name, payload_out = await app.command_router.handle(command, payload)

        if isinstance(payload_out, dict) and payload.get("request_id"):
            payload_out.setdefault("request_id", str(payload.get("request_id")))
        bounded_log = {
            "command": command,
            "ok": payload_out.get("ok") if isinstance(payload_out, dict) else True,
            "tool_id": str(payload.get("tool_id") or ""),
            "request_id": str(payload.get("request_id") or ""),
            "error_code": str(
                payload_out.get("error_code") or ""
                if isinstance(payload_out, dict)
                else ""
            ),
        }
        _write_core_log_safely(app, "core", f"{command} result", bounded_log)
        await ui.send_event(event_name, payload_out)
        if getattr(app, "task_queue", None):
            await app.task_queue.finish(
                task_record, command_result_ok(payload_out), ui.send_event, bounded_log
            )
    except asyncio.CancelledError:
        if getattr(app, "task_queue", None):
            await app.task_queue.cancel(task_record, ui.send_event)
        raise
    except Exception as exc:
        error_id = uuid.uuid4().hex
        print(f"[IPC] command failed ({error_id}): {type(exc).__name__}: {exc}")
        _write_core_log_safely(
            app,
            "error",
            f"{command} failed",
            {"error_id": error_id, "error_type": type(exc).__name__},
        )
        payload_out = {
            "ok": False,
            "command": command,
            "message": "Command failed; consult the local error log.",
            "error_id": error_id,
        }
        if payload.get("tool_id"):
            payload_out["tool_id"] = str(payload.get("tool_id"))
        if payload.get("request_id"):
            payload_out["request_id"] = str(payload.get("request_id"))
        await ui.send_event(result_event_for_command(command), payload_out)
        if getattr(app, "task_queue", None):
            await app.task_queue.finish(task_record, False, ui.send_event, payload_out)
async def handler(websocket, app_instance):
    ui = UIShell(websocket)
    connection_tasks: set[asyncio.Task] = set()

    # Gracefully wait for the backend to finish its heavy initialization
    while app_instance.command_router is None:
        await asyncio.sleep(0.5)

    if getattr(app_instance, "task_queue", None):
        pending = app_instance.task_queue.pending_recovery()
        if pending:
            await ui.send_event("task_recovery_required", {"ok": True, "tasks": pending})

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if not isinstance(data, dict):
                    raise ValueError("IPC message must be a JSON object")
                command = data.get("command")
                payload = data.get("payload") or {}
                if not isinstance(command, str) or not command.strip():
                    raise ValueError("IPC command must be a non-empty string")
                command = command.strip()
                if not isinstance(payload, dict):
                    raise ValueError("IPC payload must be a JSON object")

                if command == "task_recovery_decision":
                    resume = bool(payload.get("resume"))
                    result = app_instance.task_queue.resolve_recovery(resume) if getattr(app_instance, "task_queue", None) else {
                        "ok": True,
                        "resume": resume,
                        "task_count": 0,
                    }
                    await ui.send_event("task_recovery_decision_result", result)
                    continue

                if command in ("design_ai_stop", "developer_ai_stop"):
                    stopped = 0
                    for active_task, meta in list(getattr(app_instance, "_command_task_meta", {}).items()):
                        active_command = str(meta.get("command", ""))
                        if active_command.startswith("design_") or active_command.startswith("developer_") or active_command == "discussion_query":
                            if not active_task.done():
                                active_task.cancel()
                                stopped += 1
                    event_prefix = command.split("_")[0]
                    await ui.send_event(f"{command}_result", {"ok": True, "stopped": stopped, "message": f"{event_prefix} workflow stop requested"})
                    continue

                if len(connection_tasks) >= MAX_CONNECTION_COMMAND_TASKS:
                    await ui.send_error("Too many commands are already running")
                    continue

                task = asyncio.create_task(process_command_task(app_instance, ui, command, payload))
                connection_tasks.add(task)
                app_instance._command_tasks.add(task)
                app_instance._command_task_meta[task] = {"command": command}

                def clear_command_task(done_task):
                    connection_tasks.discard(done_task)
                    app_instance._command_tasks.discard(done_task)
                    app_instance._command_task_meta.pop(done_task, None)
                    if not done_task.cancelled():
                        with contextlib.suppress(Exception):
                            done_task.exception()

                task.add_done_callback(clear_command_task)
                await ui.send_event("COMMAND_RECEIVED", {"command": command, "status": "processing"})

            except Exception as exc:
                await ui.send_error(str(exc))
    except websockets.exceptions.ConnectionClosed:
        return
    finally:
        for task in list(connection_tasks):
            if not task.done():
                task.cancel()
        if connection_tasks:
            await asyncio.gather(*connection_tasks, return_exceptions=True)



async def run_server(app_instance, auto_kill_backend_port: bool = False):
    ipc_port = _ipc_port()
    standalone_tool_id = _standalone_tool_id()
    try:
        async def bound_handler(ws):
            await handler(ws, app_instance)

        shutdown_event = asyncio.Event()

        def process_request_with_shutdown(_connection, request):
            request_path = urlsplit(str(request.path)).path
            if request_path == "/health":
                startup_status = (
                    app_instance.get_startup_status()
                    if hasattr(app_instance, "get_startup_status")
                    else {}
                )
                ready = getattr(app_instance, "command_router", None) is not None
                runtime_state = "ready" if ready else "starting"
                capabilities = getattr(
                    app_instance,
                    "standalone_capabilities",
                    {},
                )
                if standalone_tool_id:
                    ready = bool(
                        ready
                        and isinstance(capabilities, dict)
                        and capabilities.get("tool_id") == standalone_tool_id
                        and str(capabilities.get("tool_version") or "").strip()
                    )
                services_info: dict[str, dict[str, str]] = {}
                try:
                    command_router = getattr(app_instance, "command_router", None)
                    capability_services = (
                        getattr(command_router, "capability_services", {}) or {}
                    )
                    for name, svc in capability_services.items():
                        info: dict[str, str] = {
                            "class": svc.__class__.__name__,
                            "module": svc.__class__.__module__,
                        }
                        version = getattr(svc, "VERSION", None)
                        if version:
                            info["version"] = str(version)
                        services_info[str(name)] = info
                except Exception:
                    services_info = {}
                body = json.dumps(
                    {
                        "ok": ready,
                        "version": str(getattr(app_instance, "version", "0.0.0")),
                        "workspace_instance_id": _workspace_instance_id(),
                        "runtime_state": runtime_state,
                        "runtime_scope": "standalone" if standalone_tool_id else "main",
                        "services": services_info,
                        "capabilities": capabilities,
                        **startup_status,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                if ready:
                    return http_response(200, "OK", body, "application/json")
                return http_response(503, "STARTING", body, "application/json")
            if request_path == "/shutdown":
                if not _shutdown_request_authorized(request):
                    return http_response(403, "FORBIDDEN", b"Forbidden")
                app_instance._manual_shutdown = _shutdown_request_is_manual(request)
                shutdown_event.set()
                return http_response(200, "OK", b"OK")
            if not _websocket_request_authorized(request):
                return http_response(403, "FORBIDDEN", b"Forbidden")
            return None

        # Start the IPC Server first so health checks pass immediately, preventing UI timeouts
        try:
            websocket_logger = logging.getLogger("gptbridge.websockets.server")
            if not any(
                isinstance(item, _ExpectedProbeNoiseFilter)
                for item in websocket_logger.filters
            ):
                websocket_logger.addFilter(_ExpectedProbeNoiseFilter())
            async with websockets.serve(
                bound_handler,
                "127.0.0.1",
                ipc_port,
                origins=TRUSTED_WEBSOCKET_ORIGINS,
                process_request=process_request_with_shutdown,
                logger=websocket_logger,
            ):
                print(f"IPC Server running at ws://127.0.0.1:{ipc_port}")
                if hasattr(app_instance, "_mark_startup_phase"):
                    app_instance._mark_startup_phase("server_listener_ready")

                try:
                    if hasattr(app_instance, "_mark_startup_phase"):
                        app_instance._mark_startup_phase("runtime_initializing")
                    if standalone_tool_id:
                        initialize_standalone = getattr(
                            app_instance,
                            "initialize_standalone_tool",
                            None,
                        )
                        if initialize_standalone is None:
                            raise RuntimeError(
                                "Standalone tool initialization is unavailable."
                            )
                        await initialize_standalone()
                    else:
                        await app_instance.initialize()
                except Exception as exc:
                    if hasattr(app_instance, "_mark_startup_phase"):
                        app_instance._mark_startup_phase("runtime_failed")
                    try:
                        app_instance._log({"type": "error", "message": f"runtime initialization failed: {exc}"})
                    except Exception:
                        print(f"Runtime initialization failed: {exc}")
                    raise

                if standalone_tool_id:
                    if hasattr(app_instance, "_mark_startup_phase"):
                        app_instance._mark_startup_phase("standalone_tool_ready")
                    try:
                        app_instance._log(
                            {
                                "type": "info",
                                "message": "standalone tool backend ready",
                                "tool_id": standalone_tool_id,
                            }
                        )
                    except Exception:
                        pass
                await shutdown_event.wait()
        except OSError as exc:
            if exc.errno in {98, 10048}:
                print(
                    f"[IPC] Failed to bind backend server to "
                    f"127.0.0.1:{ipc_port}: address already in use."
                )
                pid, owner = _get_port_owner(ipc_port)
                if owner:
                    print(f"[IPC] Port owner: {owner}")
                if auto_kill_backend_port and pid is not None:
                    if _is_gptbridge_process(pid, Path(__file__).resolve().parents[2]):
                        print(f"[IPC] Detected existing GPTBridge backend process PID {pid}; attempting safe termination.")
                        if _kill_process(pid):
                            print("[IPC] Previous GPTBridge backend terminated. Retrying server bind...")
                            await asyncio.sleep(1)
                            return await run_server(app_instance, auto_kill_backend_port=False)
                        print("[IPC] Failed to terminate the existing GPTBridge backend process.")
                    else:
                        print("[IPC] Existing process does not appear to be a GPTBridge backend; auto-kill aborted.")
                print(
                    f"[IPC] Please stop the existing process on port {ipc_port} "
                    "before starting."
                )
                return
            raise
    except KeyboardInterrupt:
        print("Stopping IPC server...")
    finally:
        await app_instance.shutdown()
