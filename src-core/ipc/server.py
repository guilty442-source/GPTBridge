import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from main import GPTBridgeApp

import websockets # type: ignore

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


COMMAND_RESULT_EVENTS = {
    "app:add-governance-rule": "app:add-governance-rule_result",
    "app:agent-execute-tool": "app:agent-execute-tool_result",
    "app:agent-instruct": "app:agent-instruct_result",
    "app:agent-intervention": "app:agent-intervention_result",
    "app:delete-code": "app:delete-code_result",
    "app:delete-governance-rule": "app:delete-governance-rule_result",
    "app:get-governance-rules": "app:get-governance-rules_result",
    "app:set-governance-rules": "app:set-governance-rules_result",
    "app:update-governance-rule": "app:update-governance-rule_result",
    "app:run-unit-tests": "app:run-unit-tests_result",
    "app:move-code": "app:move-code_result",
    "app:save-code": "app:save-code_result",
    "app:update-config": "app:update-config_result",
    "audit_run": "audit_result",
    "change_provider_url": "change_provider_url_result",
    "child_tool_code_check": "design_code_check_result",
    "child_tool_package": "design_package_child_tool_result",
    "child_tool_repair": "design_modify_child_tool_result",
    "child_tool_test": "design_test_child_tool_result",
    "design_backup": "design_backup_result",
    "design_code_check": "design_code_check_result",
    "design_delete_child_tool": "design_delete_child_tool_result",
    "design_diff_view": "design_diff_view_result",
    "design_generate_child_tool": "design_generate_child_tool_result",
    "design_modify_child_tool": "design_modify_child_tool_result",
    "design_new_child_file": "design_new_child_file_result",
    "design_new_project": "design_new_project_result",
    "design_new_selected_file": "design_new_child_file_result",
    "design_open_child_file": "design_open_child_file_result",
    "design_open_project": "design_open_project_result",
    "design_open_selected_file": "design_open_child_file_result",
    "design_optimize_plan": "design_optimization_plan_result",
    "design_package_child_tool": "design_package_child_tool_result",
    "design_release_summary": "design_release_summary_result",
    "design_rename_child_tool": "design_rename_child_tool_result",
    "design_repair_chain": "design_repair_chain_result",
    "design_rollback_latest": "design_rollback_latest_result",
    "design_save_child_file": "design_save_child_file_result",
    "design_test_child_tool": "design_test_child_tool_result",
    "discussion_query": "discussion_result",
    "developer_auto_optimize": "developer_auto_optimize_result",
    "developer_apply_sandbox": "developer_apply_sandbox_result",
    "developer_deploy_summary": "developer_deploy_summary_result",
    "developer_phase1_integrity": "developer_phase1_integrity_result",
    "developer_phase2_static": "developer_phase2_static_result",
    "developer_phase3_startup": "developer_phase3_startup_result",
    "developer_phase4_health": "developer_phase4_health_result",
    "developer_phase5_ai_review": "developer_phase5_ai_review_result",
    "developer_phase6_build": "developer_phase6_build_result",
    "developer_prepare_sandbox": "developer_prepare_sandbox_result",
    "generate_child_tool": "design_generate_child_tool_result",
    "health_check": "health_check_result",
    "load_config": "load_config_result",
    "mother_backup": "mother_backup_result",
    "mother_check_self": "mother_check_self_result",
    "mother_provider_status": "mother_provider_status_result",
    "mother_startup_status": "mother_startup_status_result",
    "mother_storage_audit": "mother_storage_audit_result",
    "mother_url_session_check": "mother_url_session_check_result",
    "save_config": "save_config_result",
    "settings_backup_records": "settings_backup_records_result",
    "settings_delete_backup": "settings_delete_backup_result",
    "settings_export_error_logs": "settings_export_error_logs_result",
    "settings_export_logs": "settings_export_logs_result",
    "settings_health_refresh": "settings_health_refresh_result",
    "settings_mark_updates_applied": "settings_mark_updates_applied_result",
    "settings_maintain_sandbox": "settings_maintain_sandbox_result",
    "settings_open_system_browser": "settings_open_system_browser_result",
    "shared_load_config": "load_config_result",
    "shared_save_config": "save_config_result",
    "settings_factory_reset": "settings_factory_reset_result",
    "toolbox_add_tool": "toolbox_add_tool_result",
    "toolbox_list_tools": "toolbox_list_tools_result",
    "toolbox_open_tool_code": "toolbox_open_tool_code_result",
    "toolbox_cancel_tool_run": "toolbox_cancel_tool_run_result",
    "toolbox_run_tool": "toolbox_run_tool_result",
    "toolbox_save_tool_code": "toolbox_save_tool_code_result",
    "toolbox_start_tool": "toolbox_start_tool_result",
    "toolbox_stop_tool": "toolbox_stop_tool_result",
    "verify_mother_tool": "mother_check_self_result",
}


def result_event_for_command(command: str) -> str:
    return COMMAND_RESULT_EVENTS.get(command) or f"{command}_result"


def should_simulate_progress(command: str) -> bool:
    return command == "discussion_query" or command.startswith("developer_") and (
        "ai" in command or "optimize" in command
    )

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


async def simulated_progress_loop(app: "GPTBridgeApp", ui: UIShell, task_record: Any) -> None:
    while task_record is not None and task_record.status == "running":
        await asyncio.sleep(2)
        if task_record.status != "running":
            break
        if task_record.percent >= 92:
            continue
        next_percent = min(92, task_record.percent + 3)
        phase = task_record.phase if task_record.phase and task_record.phase != task_record.stage else "ai_waiting"
        await app.task_queue.update_progress(
            task_record,
            ui.send_event,
            phase=phase,
            percent=next_percent,
            message=task_record.message or "background progress",
        )

async def process_command_task(app: "GPTBridgeApp", ui: UIShell, command: str, payload: Dict[str, Any]):
    task_record = None
    progress_task = None
    orchestrator = None
    previous_progress_reporter = None
    try:
        if getattr(app, "task_queue", None):
            task_record = await app.task_queue.begin(command, payload, ui.send_event)
            if task_record is not None and task_record.status == "blocked":
                await ui.send_event(
                    "task_blocked_result",
                    {
                        "ok": False,
                        "command": command,
                        "message": task_record.message,
                    },
                )
                return

        if task_record is not None and should_simulate_progress(command) and getattr(app, "task_queue", None):
            progress_task = asyncio.create_task(simulated_progress_loop(app, ui, task_record))
            orchestrator = getattr(app, "orchestrator", None)
            if orchestrator is not None and hasattr(orchestrator, "set_progress_reporter"):
                previous_progress_reporter = getattr(orchestrator, "progress_reporter", None)

                async def report_ai_progress(phase: str, percent: int, message: str = "") -> None:
                    await app.task_queue.update_progress(
                        task_record,
                        ui.send_event,
                        stage="ai_analysis",
                        phase=phase,
                        percent=percent,
                        message=message,
                    )

                orchestrator.set_progress_reporter(report_ai_progress)
                if hasattr(orchestrator, "set_log_reporter"):
                    orchestrator.set_log_reporter(ui.send_log)

        app.command_router._log_reporter = ui.send_log
        if command == "toolbox_run_tool" and getattr(app.command_router, "toolbox_service", None):
            event_name = "toolbox_run_tool_result"
            payload_out = await app.command_router.toolbox_service.run_tool(
                payload,
                event_callback=ui.send_event,
            )
        else:
            event_name, payload_out = await app.command_router.handle(command, payload)
        if isinstance(payload_out, dict) and payload.get("request_id"):
            payload_out.setdefault("request_id", str(payload.get("request_id")))
        if getattr(app, "core_logger", None):
            category = getattr(task_record, "category", "core") if task_record else "core"
            app.core_logger.write(category, f"{command} result", payload_out)
        await ui.send_event(event_name, payload_out)

        if event_name == "audit_result":
            await ui.send_log(f"[Audit] {payload_out.get('summary', 'Audit completed.')}")
            for item in payload_out.get("items", []):
                severity = item.get("severity", "INFO")
                category = item.get("category", "audit")
                message = item.get("message", "")
                await ui.send_log(f"[Audit][{severity}][{category}] {message}")

        if event_name == "discussion_result":
            ok = payload_out.get("ok", False)
            mode = payload_out.get("mode", "unknown")
            await ui.send_log(f"[Discussion][{'OK' if ok else 'WARNING'}] mode={mode}")
        if getattr(app, "task_queue", None):
            await app.task_queue.finish(task_record, command_result_ok(payload_out), ui.send_event, payload_out)

    except asyncio.CancelledError:
        if command == "audit_run":
            await ui.send_event("audit_stop_result", {"ok": True, "message": "self-check stopped"})
        if getattr(app, "task_queue", None):
            await app.task_queue.cancel(task_record, ui.send_event)
        raise
    except Exception as exc:
        print(f"[IPC] Error processing command '{command}':\n{traceback.format_exc()}")
        if getattr(app, "core_logger", None):
            app.core_logger.write("error", f"{command} failed", {"error": str(exc), "traceback": traceback.format_exc()})
        result_event = result_event_for_command(command)
        
        # 將完整的 Traceback 傳給前端，方便在介面上的 LogPanel 直接看到錯誤行數
        error_detail = f"Exception: {exc}\n{traceback.format_exc()}"
        error_payload = {"ok": False, "command": command, "message": str(exc), "error": error_detail}
        if payload.get("tool_id"):
            error_payload["tool_id"] = str(payload.get("tool_id"))
        if payload.get("request_id"):
            error_payload["request_id"] = str(payload.get("request_id"))
        await ui.send_event(result_event, error_payload)
        await ui.send_error(f"Error processing command '{command}': {exc}")
        if command == "discussion_query":
            await ui.send_log(f"[Discussion][FAILED] {exc}")
        if getattr(app, "task_queue", None):
            await app.task_queue.finish(task_record, False, ui.send_event, {"ok": False, "error": str(exc)})
    finally:
        if orchestrator is not None and hasattr(orchestrator, "set_progress_reporter"):
            orchestrator.set_progress_reporter(previous_progress_reporter)
            if hasattr(orchestrator, "set_log_reporter"):
                orchestrator.set_log_reporter(None)
        if progress_task is not None and not progress_task.done():
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task


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

    # Send initial mode services status to the UI so frontend can show available subsystems
    try:
        if getattr(app_instance, "command_router", None):
            try:
                event_name, payload = await app_instance.command_router.handle("app:get-mode-services-status", {})
                await ui.send_event(event_name, payload)
            except Exception:
                # best-effort only
                pass
    except Exception:
        pass

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                command = data.get("command")
                payload = data.get("payload") or {}

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

                if command == "audit_stop":
                    task = getattr(app_instance, "_active_audit_task", None)
                    await ui.send_event("COMMAND_RECEIVED", {"command": command, "status": "processing"})
                    if task is not None and not task.done():
                        task.cancel()
                    else:
                        await ui.send_event("audit_stop_result", {"ok": False, "message": "no active self-check task"})
                    continue

                task = asyncio.create_task(process_command_task(app_instance, ui, command, payload))
                connection_tasks.add(task)
                app_instance._command_tasks.add(task)
                app_instance._command_task_meta[task] = {"command": command}

                def clear_command_task(done_task):
                    connection_tasks.discard(done_task)
                    app_instance._command_tasks.discard(done_task)
                    app_instance._command_task_meta.pop(done_task, None)
                    if getattr(app_instance, "_active_audit_task", None) is done_task:
                        app_instance._active_audit_task = None
                    if not done_task.cancelled():
                        with contextlib.suppress(Exception):
                            done_task.exception()

                task.add_done_callback(clear_command_task)
                if command == "audit_run":
                    app_instance._active_audit_task = task

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



async def run_server(app_instance, profile: str = "main", headless: bool = False, auto_kill_backend_port: bool = False):
    try:
        if headless:
            print("[IPC] headless request ignored; provider browser is forced to Edge headful mode.")
        async def bound_handler(ws):
            await handler(ws, app_instance)

        shutdown_event = asyncio.Event()

        def process_request_with_shutdown(_connection, request):
            if request.path == "/health":
                startup_status = (
                    app_instance.get_startup_status()
                    if hasattr(app_instance, "get_startup_status")
                    else {}
                )
                mode_manager = getattr(app_instance, "mode_manager", None)
                active_mode = getattr(mode_manager, "active_mode", None)
                ready = getattr(app_instance, "command_router", None) is not None and active_mode in {"safe", "full"}
                body = json.dumps(
                    {
                        "ok": ready,
                        "mode": active_mode,
                        **startup_status,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                if ready:
                    return http_response(200, "OK", body, "application/json")
                return http_response(503, "STARTING", body, "application/json")
            if request.path == "/shutdown":
                app_instance._manual_shutdown = True
                shutdown_event.set()
                return http_response(200, "OK", b"OK")
            return None

        # Start the IPC Server first so health checks pass immediately, preventing UI timeouts
        try:
            async with websockets.serve(bound_handler, "127.0.0.1", 8765, process_request=process_request_with_shutdown):
                print("IPC Server running at ws://127.0.0.1:8765")
                if hasattr(app_instance, "_mark_startup_phase"):
                    app_instance._mark_startup_phase("server_listener_ready")

                try:
                    if hasattr(app_instance, "_mark_startup_phase"):
                        app_instance._mark_startup_phase("safe_mode_initializing")
                    await app_instance.initialize(mode="safe", profile=profile, headless=False)
                except Exception as exc:
                    try:
                        app_instance._log({"type": "error", "message": f"safe mode initialization failed: {exc}"})
                    except Exception:
                        print(f"Safe init failed: {exc}")

                async def _init_background():
                    try:
                        if hasattr(app_instance, "_mark_startup_phase"):
                            app_instance._mark_startup_phase("full_mode_initializing")
                        app_instance._log({"type": "info", "message": "background initialization started"})
                        await app_instance.initialize(mode="full", profile=profile, headless=False)
                        app_instance._log({"type": "info", "message": "background initialization completed"})
                    except Exception as exc:
                        try:
                            app_instance._log({"type": "error", "message": f"background initialization failed: {exc}"})
                        except Exception:
                            print(f"Background init failed: {exc}")

                asyncio.create_task(_init_background())
                await shutdown_event.wait()
        except OSError as exc:
            if exc.errno in {98, 10048}:
                print("[IPC] Failed to bind backend server to 127.0.0.1:8765: address already in use.")
                pid, owner = _get_port_owner(8765)
                if owner:
                    print(f"[IPC] Port owner: {owner}")
                if auto_kill_backend_port and pid is not None:
                    if _is_gptbridge_process(pid, Path(__file__).resolve().parents[2]):
                        print(f"[IPC] Detected existing GPTBridge backend process PID {pid}; attempting safe termination.")
                        if _kill_process(pid):
                            print("[IPC] Previous GPTBridge backend terminated. Retrying server bind...")
                            await asyncio.sleep(1)
                            return await run_server(app_instance, profile, headless, auto_kill_backend_port=False)
                        print("[IPC] Failed to terminate the existing GPTBridge backend process.")
                    else:
                        print("[IPC] Existing process does not appear to be a GPTBridge backend; auto-kill aborted.")
                print("[IPC] Please stop the existing GPTBridge backend or run `npm run kill-backend-port` before starting.")
                return
            raise
    except KeyboardInterrupt:
        print("Stopping IPC server...")
    finally:
        await app_instance.shutdown()
