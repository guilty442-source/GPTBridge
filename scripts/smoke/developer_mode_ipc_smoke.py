from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets


ROOT = Path(__file__).resolve().parents[2]
WS_URL = "ws://127.0.0.1:8765"
HTTP_BASE = "http://127.0.0.1:8765"


@dataclass
class SmokeResult:
    name: str
    passed: bool
    message: str
    payload: dict[str, Any]


async def wait_for_event(ws: Any, expected_event: str, timeout: float = 20.0) -> dict[str, Any]:
    start = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError(f"timeout waiting for event: {expected_event}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        event = data.get("event")
        if event != expected_event:
            continue
        return data


async def send_command_and_wait(
    ws: Any,
    command: str,
    payload: dict[str, Any],
    result_event: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    await ws.send(json.dumps({"command": command, "payload": payload}, ensure_ascii=False))
    return await wait_for_event(ws, result_event, timeout=timeout)


def _shutdown_server() -> None:
    try:
        urllib.request.urlopen(f"{HTTP_BASE}/shutdown", timeout=2).read()
    except Exception:
        pass


async def _wait_for_server(url: str, timeout: float = 40.0) -> None:
    start = time.monotonic()
    while True:
        try:
            ws = await websockets.connect(url, open_timeout=2)
            await ws.close()
            return
        except Exception:
            if time.monotonic() - start >= timeout:
                raise TimeoutError(f"server did not become ready within {timeout}s")
            await asyncio.sleep(0.5)


async def run_smoke(start_server: bool, full_cleanup: bool) -> tuple[int, list[SmokeResult]]:
    process: asyncio.subprocess.Process | None = None
    try:
        _shutdown_server()
        if start_server:
            process = await asyncio.create_subprocess_exec(
                str(ROOT / ".venv" / "Scripts" / "python.exe"),
                str(ROOT / "src-core" / "main.py"),
                "--serve",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(ROOT),
            )
            await _wait_for_server(WS_URL, timeout=60)

        results: list[SmokeResult] = []

        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            load_config_event = await send_command_and_wait(
                ws,
                "load_config",
                {},
                "load_config_result",
            )
            load_payload = load_config_event.get("payload", {})
            load_ok = bool(load_payload.get("ok"))
            results.append(
                SmokeResult(
                    name="load_config",
                    passed=load_ok,
                    message=load_payload.get("message", "load_config returned"),
                    payload=load_payload,
                )
            )

            config = load_payload.get("config", {}) if isinstance(load_payload.get("config"), dict) else {}
            save_event = await send_command_and_wait(
                ws,
                "save_config",
                {"config": config},
                "save_config_result",
            )
            save_payload = save_event.get("payload", {})
            results.append(
                SmokeResult(
                    name="save_config",
                    passed=bool(save_payload.get("ok")),
                    message=save_payload.get("message", "save_config returned"),
                    payload=save_payload,
                )
            )

            health_event = await send_command_and_wait(
                ws,
                "settings_health_refresh",
                {"source": "smoke"},
                "settings_health_refresh_result",
            )
            health_payload = health_event.get("payload", {})
            results.append(
                SmokeResult(
                    name="settings_health_refresh",
                    passed=bool(health_payload.get("ok")),
                    message=health_payload.get("message", "health refresh returned"),
                    payload=health_payload,
                )
            )

            focus_chatgpt_event = await send_command_and_wait(
                ws,
                "focus_chatgpt",
                {"target": "developer"},
                "focus_chatgpt_result",
                timeout=90.0,
            )
            focus_chatgpt_payload = focus_chatgpt_event.get("payload", {})
            results.append(
                SmokeResult(
                    name="focus_chatgpt",
                    passed=True,
                    message=focus_chatgpt_payload.get("message", "focus chatgpt returned"),
                    payload=focus_chatgpt_payload,
                )
            )

            focus_gemini_event = await send_command_and_wait(
                ws,
                "focus_gemini",
                {"target": "developer"},
                "focus_gemini_result",
                timeout=60.0,
            )
            focus_gemini_payload = focus_gemini_event.get("payload", {})
            results.append(
                SmokeResult(
                    name="focus_gemini",
                    passed=True,
                    message=focus_gemini_payload.get("message", "focus gemini returned"),
                    payload=focus_gemini_payload,
                )
            )

            cleanup_payload_input = {
                # Keep smoke deterministic and fast on Windows by avoiding
                # whole-project profile/cache traversal during CI/local checks.
                "scope": "sandbox",
                "dry_run": not full_cleanup,
            }
            cleanup_timeout = 45.0 if full_cleanup else 20.0
            cleanup_args = [
                "--cleanup-garbage",
                "--scope",
                str(cleanup_payload_input["scope"]),
                "--json",
            ]
            if cleanup_payload_input["dry_run"]:
                cleanup_args.append("--dry-run")
            cleanup_event = await send_command_and_wait(
                ws,
                "toolbox_run_tool",
                {
                    "tool_id": "project-cleaner",
                    "args": cleanup_args,
                    "source": "developer_smoke",
                },
                "toolbox_run_tool_result",
                timeout=cleanup_timeout,
            )
            cleanup_payload = cleanup_event.get("payload", {})
            try:
                cleanup_report = json.loads(str(cleanup_payload.get("stdout", "")).strip())
            except json.JSONDecodeError:
                cleanup_report = cleanup_payload
            results.append(
                SmokeResult(
                    name="project_cleaner_cleanup",
                    passed=bool(cleanup_payload.get("ok")) and bool(cleanup_report.get("ok")),
                    message=cleanup_report.get("message", "cleanup returned"),
                    payload=cleanup_report,
                )
            )

            sandbox_event = await send_command_and_wait(
                ws,
                "settings_maintain_sandbox",
                {},
                "settings_maintain_sandbox_result",
            )
            sandbox_payload = sandbox_event.get("payload", {})
            results.append(
                SmokeResult(
                    name="settings_maintain_sandbox",
                    passed=bool(sandbox_payload.get("ok")),
                    message=sandbox_payload.get("message", "sandbox maintenance returned"),
                    payload=sandbox_payload,
                )
            )

            backup_event = await send_command_and_wait(
                ws,
                "settings_backup_records",
                {},
                "settings_backup_records_result",
            )
            backup_payload = backup_event.get("payload", {})
            results.append(
                SmokeResult(
                    name="settings_backup_records",
                    passed=bool(backup_payload.get("ok")),
                    message=backup_payload.get("message", "backup records returned"),
                    payload=backup_payload,
                )
            )

            logs_event = await send_command_and_wait(
                ws,
                "settings_export_logs",
                {},
                "settings_export_logs_result",
            )
            logs_payload = logs_event.get("payload", {})
            results.append(
                SmokeResult(
                    name="settings_export_logs",
                    passed=bool(logs_payload.get("ok")),
                    message=logs_payload.get("message", "export logs returned"),
                    payload=logs_payload,
                )
            )

        failed = [item for item in results if not item.passed]
        return (1 if failed else 0), results

    finally:
        _shutdown_server()
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()


def print_report(results: list[SmokeResult]) -> None:
    print("\\n=== Developer Mode IPC Smoke Report ===")
    for item in results:
        state = "PASS" if item.passed else "FAIL"
        print(f"[{state}] {item.name}: {item.message}")

    print("\\n--- Payload Snapshot ---")
    for item in results:
        minimal = {
            "ok": item.payload.get("ok"),
            "message": item.payload.get("message"),
        }
        for key in ("dry_run", "cleaned_files", "cleaned_dirs", "archive"):
            if key in item.payload:
                minimal[key] = item.payload.get(key)
        print(f"{item.name}: {json.dumps(minimal, ensure_ascii=False)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Developer-mode card IPC smoke test (command -> IPC -> backend result)."
    )
    parser.add_argument(
        "--no-start-server",
        action="store_true",
        help="Use existing ws://127.0.0.1:8765 server instead of launching one.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run full cleanup (non-dry-run). Default uses dry-run cleanup.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    code, results = asyncio.run(
        run_smoke(start_server=not args.no_start_server, full_cleanup=args.full)
    )
    print_report(results)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
