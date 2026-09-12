"""Command-result helpers, bounded logging, and per-command task processing.

Extracted from ``ipc.server`` to keep each module focused and under 500 lines.
All names here are re-exported by ``ipc.server`` for backward compatibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from main import GPTBridgeApp

from core.ui_shell import UIShell
from shared_layer.runtime_gateway import InformationChannelGateway


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

        gateway = getattr(app, "_information_channel_gateway", None)
        if not isinstance(gateway, InformationChannelGateway):
            gateway = InformationChannelGateway(
                app.command_router.handle,
                audit=lambda record: _write_core_log_safely(
                    app, "information-channel", "command routed", record
                ),
                project_root=getattr(app, "project_root", None),
            )
            app._information_channel_gateway = gateway
        event_name, payload_out = await gateway.dispatch(
            sender="authenticated-ui",
            destination="main-system",
            command=command,
            payload=payload,
        )

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
