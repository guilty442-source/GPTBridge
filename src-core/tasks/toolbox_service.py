from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict
from managers.process_utils import terminate_process_tree
from .toolbox_repository import ToolboxRepository

ToolEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]

class ToolboxService:
    """Service for managing platform tools."""
    
    def __init__(self, project_root: Path, enforcer: Any = None):
        self.project_root = project_root
        self.tools_dir = self.project_root / "platform_tools"
        self.enforcer = enforcer
        self.repository = ToolboxRepository(project_root)
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_process_ids: set[int] = set()

    @staticmethod
    def _governance_reason(check: Dict[str, Any]) -> str:
        reason = str(check.get("reason", "")).strip()
        if reason:
            return reason
        error_report = check.get("error_report")
        if isinstance(error_report, dict):
            root_cause = str(error_report.get("root_cause", "")).strip()
            if root_cause:
                return root_cause
        return "unknown governance rule"

    def _governance_blocked(self, check: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "message": f"GOVERNANCE BLOCKED: {self._governance_reason(check)}"}

    @staticmethod
    def _build_entry_content(tool_name: str) -> str:
        return (
            f"\"\"\"{tool_name} tool entry.\"\"\"\n\n"
            "def main() -> None:\n"
            f"    print(\"{tool_name} is ready on Windows 11\")\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )

    def _resolve_entry_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        runtime = manifest.get("runtime")
        if isinstance(runtime, dict):
            runtime_entry = str(runtime.get("entry", "")).strip()
            if runtime_entry:
                entry_path = tool_dir / Path(runtime_entry)
                if entry_path.suffix == "":
                    entry_path = entry_path.with_suffix(".py")
                return entry_path

        entry = str(manifest.get("entry", "")).strip()
        if entry:
            entry_path = self.project_root / Path(entry)
            if entry_path.suffix == "":
                entry_path = entry_path.with_suffix(".py")
            return entry_path
        return tool_dir / "src" / "main.py"

    def _resolve_working_directory(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        runtime = manifest.get("runtime")
        raw_cwd = "."
        if isinstance(runtime, dict):
            raw_cwd = str(runtime.get("workingDirectory", ".")).strip() or "."
        cwd = (tool_dir / raw_cwd).resolve()
        try:
            cwd.relative_to(tool_dir.resolve())
        except ValueError:
            return tool_dir.resolve()
        return cwd

    def _resolve_executable_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        executable = manifest.get("executable")
        raw_path = ""
        if isinstance(executable, dict):
            raw_path = str(executable.get("path", "")).strip()
        if not raw_path:
            tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
            raw_path = f"dist/{tool_id}.exe"
        exe_path = (tool_dir / raw_path).resolve()
        try:
            exe_path.relative_to(tool_dir.resolve())
        except ValueError:
            return tool_dir / "dist" / f"{tool_dir.name}.exe"
        return exe_path

    def _resolve_python_executable(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        runtime = manifest.get("runtime")
        candidates: list[Path] = []
        if isinstance(runtime, dict):
            raw_python = str(runtime.get("python", "")).strip()
            if raw_python:
                python_path = Path(raw_python)
                candidates.append((tool_dir / python_path).resolve() if not python_path.is_absolute() else python_path)
        candidates.extend(
            [
                tool_dir / ".venv" / "Scripts" / "python.exe",
                tool_dir / ".venv" / "bin" / "python",
                Path(sys.executable),
            ]
        )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return Path(sys.executable)

    def _tool_environment(self, tool_id: str, tool_dir: Path) -> dict[str, str]:
        child_env = os.environ.copy()
        child_env.pop("ELECTRON_RUN_AS_NODE", None)
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["GPTBRIDGE_PROJECT_ROOT"] = str(self.project_root)
        child_env["GPTBRIDGE_TOOL_ID"] = tool_id
        child_env["GPTBRIDGE_TOOL_DIR"] = str(tool_dir)
        return child_env

    @staticmethod
    def _project_size_bytes(tool_dir: Path) -> int:
        total = 0
        if not tool_dir.exists() or not tool_dir.is_dir():
            return total

        for root, dirs, files in os.walk(tool_dir):
            for filename in files:
                path = Path(root) / filename
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _manifest_to_record(self, tool_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
        manifest_path = tool_dir / "manifest.json"
        entry_file = self._resolve_entry_file(manifest, tool_dir)
        executable_file = self._resolve_executable_file(manifest, tool_dir)
        record = dict(manifest)
        record["folder_path"] = str(tool_dir)
        record["manifest_path"] = str(manifest_path)
        record["code_path"] = str(entry_file)
        record["standalone"] = True
        record["executable_path"] = str(executable_file)
        record["executable_exists"] = executable_file.exists()
        record["project_size_bytes"] = self._project_size_bytes(tool_dir)
        return record

    def _load_manifest_records(self) -> list[Dict[str, Any]]:
        records: list[Dict[str, Any]] = []
        if not self.tools_dir.exists():
            return records

        for tool_dir in sorted(self.tools_dir.iterdir(), key=lambda item: item.name.lower()):
            manifest_path = tool_dir / "manifest.json"
            if not tool_dir.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            records.append(self._manifest_to_record(tool_dir, manifest))
        return records

    def _sync_database_from_manifests(self) -> list[Dict[str, Any]]:
        records = self._load_manifest_records()
        self.repository.replace_tools(records)
        return records

    async def add_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = payload.get("tool_id", "").strip()
        tool_name = payload.get("tool_name", "").strip()
        
        if not tool_id or not tool_name:
            return {"ok": False, "message": "Missing tool_id or tool_name"}
            
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
            
        tool_dir = self.tools_dir / tool_id
        manifest = {
            "id": tool_id,
            "name": tool_name,
            "version": "1.0.0",
            "status": "stopped",
            "enabled": True,
            "entry": f"platform_tools/{tool_id}/src/main",
            "runtime": {
                "type": "python",
                "entry": "src/main.py",
                "workingDirectory": ".",
            },
            "executable": {
                "path": f"dist/{tool_id}.exe",
                "name": tool_id,
                "console": True,
            },
            "description": ""
        }
        manifest_path = tool_dir / "manifest.json"
        manifest_content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        readme_path = tool_dir / "README.md"
        readme_content = f"# {tool_name}\n\nProject ID: {tool_id}\n"
        entry_path = tool_dir / "src" / "main.py"
        entry_content = self._build_entry_content(tool_name)

        if self.enforcer:
            check = self.enforcer.can_create_folder(tool_dir, "toolbox", f"Adding new tool: {tool_id}")
            if not check.get("allowed", False):
                return self._governance_blocked(check)
            manifest_check = self.enforcer.can_create_file(
                manifest_path,
                "toolbox",
                f"Create tool manifest for {tool_id}",
                manifest_content,
            )
            if not manifest_check.get("allowed", False):
                return self._governance_blocked(manifest_check)
            readme_check = self.enforcer.can_create_file(
                readme_path,
                "toolbox",
                f"Create tool readme for {tool_id}",
                readme_content,
            )
            if not readme_check.get("allowed", False):
                return self._governance_blocked(readme_check)
            entry_check = self.enforcer.can_create_file(
                entry_path,
                "toolbox",
                f"Create tool source entry for {tool_id}",
                entry_content,
            )
            if not entry_check.get("allowed", False):
                return self._governance_blocked(entry_check)

        if tool_dir.exists():
            return {"ok": False, "message": f"Tool {tool_id} already exists"}
            
        try:
            tool_dir.mkdir(parents=True, exist_ok=True)
            for sub in ["src", "config", "assets", "logs", "build"]:
                (tool_dir / sub).mkdir(exist_ok=True)

            manifest_path.write_text(manifest_content, encoding="utf-8", newline="\n")
            readme_path.write_text(readme_content, encoding="utf-8", newline="\n")
            entry_path.write_text(entry_content, encoding="utf-8", newline="\n")
            self.repository.upsert_tool(self._manifest_to_record(tool_dir, manifest))
                
            return {"ok": True, "message": "Tool created successfully", "tool_id": tool_id}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def list_tools(self) -> Dict[str, Any]:
        manifest_records = self._sync_database_from_manifests()
        tools = self.repository.list_tools()
        records_by_id = {
            str(record.get("id", "")).strip(): record
            for record in manifest_records
            if str(record.get("id", "")).strip()
        }
        for tool in tools:
            tool_id = str(tool.get("id", "")).strip()
            manifest_record = records_by_id.get(tool_id, {})
            for field in (
                "folder_path",
                "manifest_path",
                "code_path",
                "standalone",
                "executable_path",
                "executable_exists",
            ):
                if field in manifest_record:
                    tool[field] = manifest_record[field]
            manifest_path = Path(str(tool.get("manifest_path", "")))
            if manifest_path.name:
                folder_path = manifest_path.parent
                tool["folder_path"] = str(folder_path)
            elif tool_id:
                folder_path = self.tools_dir / tool_id
                tool["folder_path"] = str(folder_path)
            else:
                folder_path = None
            if folder_path is not None:
                tool["project_size_bytes"] = self._project_size_bytes(folder_path)
        return {"ok": True, "tools": tools, "database_path": str(self.repository.db_path)}

    async def open_tool_code(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        tool_name = str(payload.get("tool_name", "")).strip()

        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}

        # Special handling for the main system
        if tool_id == "main-system":
            main_py_path = self.project_root / "src-core" / "main.py"
            if not main_py_path.exists():
                return {"ok": False, "message": "Main system entry file (src-core/main.py) not found."}
                
            return {
                "ok": True,
                "tool_id": tool_id,
                "tool_name": "主系統 (GPTBridge)",
                "file_path": str(main_py_path),
                "content": main_py_path.read_text(encoding="utf-8", errors="ignore"),
                "database_path": str(self.repository.db_path),
                "opened": False,
                "message": "Main system loaded",
            }

        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}

        tool_dir = self.tools_dir / tool_id
        if not tool_dir.exists():
            create_result = await self.add_tool(
                {
                    "tool_id": tool_id,
                    "tool_name": tool_name or tool_id,
                }
            )
            if not create_result.get("ok"):
                return create_result

        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            return {"ok": False, "message": "Tool manifest not found"}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "message": f"Invalid tool manifest: {exc}"}

        resolved_name = str(manifest.get("name") or tool_name or tool_id)
        source_dir = tool_dir / "src"
        source_dir.mkdir(parents=True, exist_ok=True)
        entry_file = self._resolve_entry_file(manifest, tool_dir)
        entry_file.parent.mkdir(parents=True, exist_ok=True)
        entry_content = self._build_entry_content(resolved_name)

        if not entry_file.exists():
            if self.enforcer:
                check = self.enforcer.can_create_file(
                    entry_file,
                    "toolbox",
                    f"Create tool source entry for {tool_id}",
                    entry_content,
                )
                if not check.get("allowed", False):
                    return self._governance_blocked(check)

            entry_file.write_text(
                entry_content,
                encoding="utf-8",
                newline="\n",
            )

        opened = False
        open_message = "tool code file path ready"
        no_external = bool(payload.get("no_external", False))
        
        if not no_external:
            try:
                if os.name == "nt":
                    os.startfile(str(entry_file))
                    opened = True
                else:
                    opener = shutil.which("xdg-open")
                    if opener:
                        subprocess.Popen(
                            [opener, str(entry_file)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        opened = True
                if opened:
                    open_message = "tool code file opened"
            except Exception as exc:
                open_message = f"tool code open fallback: {exc}"

        return {
            "ok": True,
            "tool_id": tool_id,
            "tool_name": resolved_name,
            "file_path": str(entry_file),
            "content": entry_file.read_text(encoding="utf-8", errors="ignore"),
            "database_path": str(self.repository.db_path),
            "opened": opened,
            "message": open_message,
        }

    async def save_tool_code(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if tool_id != "main-system" and not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}

        content = str(payload.get("content", ""))
        tool_name = str(payload.get("tool_name", "")).strip()

        open_result = await self.open_tool_code(
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "no_external": True,
            }
        )
        if not open_result.get("ok"):
            return open_result

        entry_file = Path(str(open_result.get("file_path", "")))
        if not entry_file.exists():
            return {"ok": False, "message": "Tool code file not found"}

        if tool_id != "main-system":
            try:
                entry_file.relative_to(self.tools_dir)
            except ValueError:
                return {"ok": False, "message": "Invalid tool code path"}

        if self.enforcer:
            check = self.enforcer.can_modify_file(
                entry_file,
                "toolbox" if tool_id != "main-system" else "core",
                f"Save source for {tool_id}",
                content,
            )
            if not check.get("allowed", False):
                return self._governance_blocked(check)

        entry_file.write_text(content, encoding="utf-8", newline="\n")
        
        if tool_id != "main-system":
            manifest_path = self.tools_dir / tool_id / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {
                    "id": tool_id,
                    "name": str(open_result.get("tool_name", tool_name or tool_id)),
                    "version": "1.0.0",
                    "status": "stopped",
                    "enabled": True,
                    "entry": str(entry_file.with_suffix("").relative_to(self.project_root)).replace("\\", "/"),
                    "description": "",
                }
            self.repository.upsert_tool(self._manifest_to_record(manifest_path.parent, manifest))
            
        return {
            "ok": True,
            "tool_id": tool_id,
            "tool_name": str(open_result.get("tool_name", tool_name or tool_id)),
            "file_path": str(entry_file),
            "bytes": len(content.encode("utf-8")),
            "message": "tool code saved",
        }

    async def update_status(self, tool_id: str, status: str) -> Dict[str, Any]:
        manifest_path = self.tools_dir / tool_id / "manifest.json"
        updated_database = self.repository.update_status(tool_id, status)
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                manifest["status"] = status
                next_manifest_content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

                if self.enforcer:
                    check = self.enforcer.can_modify_file(
                        manifest_path,
                        "toolbox",
                        f"Updating status to {status}",
                        next_manifest_content,
                    )
                    if not check.get("allowed", False):
                        return self._governance_blocked(check)

                manifest_path.write_text(next_manifest_content, encoding="utf-8", newline="\n")
                self.repository.upsert_tool(self._manifest_to_record(manifest_path.parent, manifest))
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "message": str(e)}
        if updated_database:
            return {"ok": True}
        return {"ok": False, "message": "Tool not found"}

    async def start_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}

        running = self._running_processes.get(tool_id)
        if running is not None and running.returncode is None:
            return {"ok": True, "tool_id": tool_id, "message": "Tool executable is already running"}

        tool_dir = (self.tools_dir / tool_id).resolve()
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            return {"ok": False, "tool_id": tool_id, "message": "Tool manifest not found"}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "tool_id": tool_id, "message": f"Invalid tool manifest: {exc}"}

        if manifest.get("enabled", True) is False:
            return {"ok": False, "tool_id": tool_id, "message": "Tool is disabled"}

        executable_file = self._resolve_executable_file(manifest, tool_dir)
        if not executable_file.exists():
            await self.update_status(tool_id, "stopped")
            return {
                "ok": False,
                "tool_id": tool_id,
                "message": f"Standalone EXE not found. Run npm run package:tool -- {tool_id}",
                "executable_path": str(executable_file),
            }

        raw_args = payload.get("args", [])
        if not isinstance(raw_args, list):
            return {"ok": False, "tool_id": tool_id, "message": "args must be a list"}
        args = [str(item) for item in raw_args[:20]]

        try:
            process = await asyncio.create_subprocess_exec(
                str(executable_file),
                *args,
                cwd=str(tool_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._tool_environment(tool_id, tool_dir),
            )
        except Exception as exc:
            await self.update_status(tool_id, "error")
            return {"ok": False, "tool_id": tool_id, "message": str(exc)}

        self._running_processes[tool_id] = process
        asyncio.create_task(self._watch_started_tool(tool_id, process))
        status_result = await self.update_status(tool_id, "running")
        if not status_result.get("ok"):
            return status_result
        return {
            "ok": True,
            "tool_id": tool_id,
            "pid": process.pid,
            "executable_path": str(executable_file),
            "message": "Standalone tool executable started",
        }

    async def _watch_started_tool(self, tool_id: str, process: asyncio.subprocess.Process) -> None:
        try:
            await process.wait()
        finally:
            current = self._running_processes.get(tool_id)
            if current is process:
                self._running_processes.pop(tool_id, None)
                await self.update_status(tool_id, "stopped")

    async def stop_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}

        process = self._running_processes.get(tool_id)
        if process is not None and process.returncode is None:
            await terminate_process_tree(process)
            self._running_processes.pop(tool_id, None)

        status_result = await self.update_status(tool_id, "stopped")
        if not status_result.get("ok"):
            return status_result
        return {"ok": True, "tool_id": tool_id, "message": "Standalone tool executable stopped"}

    async def run_tool(
        self,
        payload: Dict[str, Any],
        event_callback: ToolEventCallback | None = None,
    ) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}

        tool_dir = (self.tools_dir / tool_id).resolve()
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            return {"ok": False, "message": "Tool manifest not found"}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "message": f"Invalid tool manifest: {exc}"}

        if manifest.get("enabled", True) is False:
            return {"ok": False, "message": "Tool is disabled"}

        entry_file = self._resolve_entry_file(manifest, tool_dir).resolve()
        try:
            entry_file.relative_to(tool_dir)
        except ValueError:
            return {"ok": False, "message": "Tool entry must stay inside its tool directory"}

        if not entry_file.exists():
            return {"ok": False, "message": "Tool entry file not found"}

        raw_args = payload.get("args", [])
        if not isinstance(raw_args, list):
            return {"ok": False, "message": "args must be a list"}
        args = [str(item) for item in raw_args[:20]]

        try:
            timeout_seconds = int(manifest.get("timeout_seconds", 120))
        except (TypeError, ValueError):
            timeout_seconds = 120
        timeout_seconds = max(1, min(timeout_seconds, 3600))

        def _decode_bytes(b: bytes) -> tuple[str, str]:
            if b is None:
                return "", "utf-8"
            # Try utf-8 first, then cp950 (Traditional Chinese on Windows), then fallback
            try:
                return b.decode("utf-8"), "utf-8"
            except Exception:
                try:
                    return b.decode("cp950"), "cp950"
                except Exception:
                    return b.decode("utf-8", errors="replace"), "utf-8-replace"

        child_env = self._tool_environment(tool_id, tool_dir)
        python_executable = self._resolve_python_executable(manifest, tool_dir)
        working_directory = self._resolve_working_directory(manifest, tool_dir)

        if event_callback is not None:
            try:
                (
                    completed_returncode,
                    stdout,
                    stderr,
                    stdout_encoding,
                    stderr_encoding,
                    cancelled,
                ) = (
                    await self._run_tool_streaming(
                        tool_id=tool_id,
                        entry_file=entry_file,
                        python_executable=python_executable,
                        working_directory=working_directory,
                        args=args,
                        child_env=child_env,
                        timeout_seconds=timeout_seconds,
                        decode_bytes=_decode_bytes,
                        event_callback=event_callback,
                    )
                )
            except asyncio.TimeoutError:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "message": f"Tool timed out after {timeout_seconds} seconds",
                    "stdout": "",
                    "stderr": "",
                }
            except Exception as exc:
                return {"ok": False, "tool_id": tool_id, "message": str(exc)}

            return {
                "ok": completed_returncode == 0 and not cancelled,
                "tool_id": tool_id,
                "exit_code": completed_returncode,
                "cancelled": cancelled,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_encoding": stdout_encoding,
                "stderr_encoding": stderr_encoding,
                "message": (
                    "Tool cancelled by user"
                    if cancelled
                    else "Tool completed"
                    if completed_returncode == 0
                    else "Tool failed"
                ),
            }

        try:
            # Capture raw bytes and decode with best-effort to avoid mojibake on Windows
            completed = await asyncio.to_thread(
                subprocess.run,
                [str(python_executable), str(entry_file), *args],
                cwd=str(working_directory),
                capture_output=True,
                text=False,
                timeout=timeout_seconds,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "tool_id": tool_id,
                "message": f"Tool timed out after {timeout_seconds} seconds",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            }
        except Exception as exc:
            return {"ok": False, "tool_id": tool_id, "message": str(exc)}

        stdout, stdout_encoding = _decode_bytes(completed.stdout) if hasattr(completed, "stdout") else ("", "utf-8")
        stderr, stderr_encoding = _decode_bytes(completed.stderr) if hasattr(completed, "stderr") else ("", "utf-8")

        return {
            "ok": completed.returncode == 0,
            "tool_id": tool_id,
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_encoding": stdout_encoding,
            "stderr_encoding": stderr_encoding,
            "message": "Tool completed" if completed.returncode == 0 else "Tool failed",
        }

    async def cancel_tool_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        process = self._running_processes.get(tool_id)
        if process is None or process.returncode is not None:
            return {"ok": False, "tool_id": tool_id, "message": "No running tool process"}
        if process.pid:
            self._cancelled_process_ids.add(process.pid)
        await terminate_process_tree(process)
        return {"ok": True, "tool_id": tool_id, "message": "Tool stop requested"}

    async def _run_tool_streaming(
        self,
        *,
        tool_id: str,
        entry_file: Path,
        python_executable: Path,
        working_directory: Path,
        args: list[str],
        child_env: dict[str, str],
        timeout_seconds: int,
        decode_bytes: Callable[[bytes], tuple[str, str]],
        event_callback: ToolEventCallback,
    ) -> tuple[int, str, str, str, str, bool]:
        progress_prefixes = (
            "DUPLICATE_CLEANER_PROGRESS_JSON=",
            "INVESTMENT_MANAGER_PROGRESS_JSON=",
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_encoding = "utf-8"
        stderr_encoding = "utf-8"

        process = await asyncio.create_subprocess_exec(
            str(python_executable),
            str(entry_file),
            *args,
            cwd=str(working_directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        if process.pid:
            self._running_processes[tool_id] = process

        async def read_stdout() -> None:
            nonlocal stdout_encoding
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded, encoding = decode_bytes(line)
                line_text = decoded.rstrip("\r\n")
                progress_prefix = next(
                    (
                        prefix
                        for prefix in progress_prefixes
                        if line_text.startswith(prefix)
                    ),
                    "",
                )
                if progress_prefix:
                    raw_progress = line_text[len(progress_prefix) :]
                    try:
                        progress = json.loads(raw_progress)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(progress, dict):
                        await event_callback(
                            "toolbox_run_tool_progress",
                            {
                                "ok": True,
                                "tool_id": tool_id,
                                **progress,
                            },
                        )
                    continue
                stdout_encoding = encoding
                stdout_chunks.append(decoded)

        async def read_stderr() -> None:
            nonlocal stderr_encoding
            assert process.stderr is not None
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                decoded, encoding = decode_bytes(line)
                stderr_encoding = encoding
                stderr_chunks.append(decoded)

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())

        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            if process.pid:
                self._cancelled_process_ids.discard(process.pid)
            current_process = self._running_processes.get(tool_id)
            if current_process is process:
                self._running_processes.pop(tool_id, None)
            raise

        await asyncio.gather(stdout_task, stderr_task)
        cancelled = bool(process.pid and process.pid in self._cancelled_process_ids)
        if process.pid:
            self._cancelled_process_ids.discard(process.pid)
        current_process = self._running_processes.get(tool_id)
        if current_process is process:
            self._running_processes.pop(tool_id, None)
        return (
            returncode,
            "".join(stdout_chunks),
            "".join(stderr_chunks),
            stdout_encoding,
            stderr_encoding,
            cancelled,
        )

    async def delete_tool(self, tool_id: str) -> Dict[str, Any]:
        tool_dir = self.tools_dir / tool_id
        if tool_dir.exists():
            shutil.rmtree(tool_dir)
            self.repository.delete_tool(tool_id)
            return {"ok": True, "message": f"Tool {tool_id} deleted successfully"}
        return {"ok": False, "message": "Tool not found"}
