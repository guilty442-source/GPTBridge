import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from governance.operation import GovernanceOperation

# Add src-core to sys.path so absolute imports work when this is not run as a module
sys.path.insert(0, str(Path(__file__).resolve().parent))

from governance.enforcer import GovernanceEnforcer
from governance.rule_catalog import (
    DEFAULT_ACTIVE_GOVERNANCE_RULES,
    GOVERNANCE_RULE_CATALOG,
)
from managers.backup_manager import BackupManager
from managers.browser_session import BrowserSessionManager
from core.paths import ensure_backup_layout
from managers.optimization_history import OptimizationHistoryManager
from core.project_agent import ProjectAgent
from orchestrator.state_machine import MultiAgentOrchestrator
from orchestrator.autonomous_coder import AutonomousCodingAgent
from ipc.server import run_server
from ipc.handlers import CommandRouter
from core_logger import CoreLogger
from tasks.queue import TaskQueue
from tasks.toolbox_service import ToolboxService
from tasks.developer_service import DeveloperService
from tasks.rescue_service import RescueService
from utils.child_tool_workspace import ChildToolWorkspace
from settings.config import load_config, save_config
from modes.mode_manager import ModeManager


class LazyProviderProxy:
    """Ensure the browser is ready before provider calls run."""

    def __init__(self, provider: Any, app: "GPTBridgeApp") -> None:
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_app", app)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._provider, name)
        if callable(attr) and asyncio.iscoroutinefunction(attr):

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                await self._app.ensure_browser_ready()
                return await attr(*args, **kwargs)

            return wrapper
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_provider", "_app"}:
            object.__setattr__(self, name, value)
            return
        setattr(self._provider, name, value)


class GPTBridgeApp:
    # Full catalog of supported governance rules for UI selection menus.
    AVAILABLE_GOVERNANCE_RULES = list(GOVERNANCE_RULE_CATALOG)

    def __init__(self) -> None:
        self.session: BrowserSessionManager | None = None
        self.chatgpt: Any | None = None
        self.gemini: Any | None = None
        self.LazyProviderProxy = LazyProviderProxy
        self._raw_chatgpt: Any | None = None
        self._raw_gemini: Any | None = None

        self.backup_manager: BackupManager | None = None
        self.history_manager: OptimizationHistoryManager | None = None
        self.connection_monitor_task: asyncio.Task[Any] | None = None
        self.orchestrator: MultiAgentOrchestrator | None = None
        self.toolbox_service: ToolboxService | None = None
        self.developer_service: DeveloperService | None = None
        self.rescue_service: RescueService | None = None
        self.design_service: Any | None = None
        self.project_agent: ProjectAgent | None = None
        self.autonomous_agent: AutonomousCodingAgent | None = None
        self.command_router: CommandRouter | None = None
        self.core_logger: CoreLogger | None = None
        self.enforcer: GovernanceEnforcer | None = None
        self.task_queue: TaskQueue | None = None
        self.mode_manager: ModeManager | None = None

        self.auto_cycle = 60
        self.max_backup_count = 3
        project_root_override = os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        self.project_root = (
            Path(project_root_override).resolve()
            if project_root_override
            else Path(__file__).resolve().parent.parent
        )
        self._active_audit_task: asyncio.Task[Any] | None = None
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._command_task_meta: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self._session_hooks_bound = False

        self.governance_rules_path = (
            self.project_root
            / "runtime"
            / "governance"
            / "rules.json"
        )
        self.governance_rules = self._load_governance_rules()
        self._manual_shutdown = False
        self.startup_phase = "created"
        self.startup_phase_active_since = time.monotonic()
        self.startup_phase_history: list[dict[str, Any]] = []
        self._startup_persistence_synced = False

    def _mark_startup_phase(self, phase: str) -> None:
        now = time.monotonic()
        previous = getattr(self, "startup_phase", None)
        duration_ms = None
        if previous is not None and previous != phase:
            duration_ms = int((now - self.startup_phase_active_since) * 1000)
            self.startup_phase_history.append(
                {
                    "phase": previous,
                    "duration_ms": duration_ms,
                    "finished_at": time.time(),
                }
            )
        self.startup_phase = phase
        self.startup_phase_active_since = now
        try:
            self._log(
                {
                    "type": "startup_phase",
                    "phase": phase,
                    "duration_since_last_ms": duration_ms,
                    "timestamp": time.time(),
                }
            )
        except Exception:
            pass

    def get_startup_status(self) -> dict[str, Any]:
        now = time.monotonic()
        active_duration_ms = int((now - self.startup_phase_active_since) * 1000)
        return {
            "phase": getattr(self, "startup_phase", "unknown"),
            "phase_duration_ms": active_duration_ms,
            "phase_history": list(self.startup_phase_history),
        }

    @staticmethod
    def _is_within(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False

    def _resolve_project_path(self, rel_path: str) -> Path:
        project_root = self.project_root.resolve()
        target_path = (project_root / rel_path).resolve()
        if not self._is_within(target_path, project_root):
            raise PermissionError("Access Denied: Path outside project root.")
        return target_path

    def _project_relative_text(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def _platform_tool_test_targets(self, relative_path: str) -> list[str]:
        parts = Path(relative_path).parts
        if len(parts) < 2 or parts[0] != "platform_tools":
            return []

        manifest_path = self.project_root / "platform_tools" / parts[1] / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        test_targets = manifest.get("test_targets", [])
        if not isinstance(test_targets, list):
            return []

        resolved_targets: list[str] = []
        for item in test_targets:
            test_path = str(item).strip()
            if not test_path:
                continue
            target = self._resolve_project_path(test_path)
            if target.exists():
                resolved_targets.append(self._project_relative_text(target))
        return resolved_targets

    def _resolve_test_targets(self, target_path: str = "") -> list[str]:
        tests_dir = self.project_root / "tests"
        if not target_path:
            return ["tests"] if tests_dir.exists() else []

        target = self._resolve_project_path(target_path)
        relative = self._project_relative_text(target)
        platform_tool_tests = self._platform_tool_test_targets(relative)
        if platform_tool_tests:
            return platform_tool_tests

        if target.exists() and target.is_dir():
            return [self._project_relative_text(target)]

        if self._is_within(target, tests_dir):
            return [self._project_relative_text(target)]

        mapped_tests = {
            "src-core/orchestrator/autonomous_coder.py": "tests/test_agent_coder.py",
            "src-core/tasks/toolbox_service.py": "tests/test_toolbox_service.py",
        }
        for prefix, test_path in mapped_tests.items():
            if relative == prefix.rstrip("/") or relative.startswith(prefix):
                if (self.project_root / test_path).exists():
                    return [test_path]

        candidates: list[Path] = []
        if target.suffix == ".py":
            candidates.append(tests_dir / f"test_{target.stem}.py")
            if target.parent.name:
                candidates.append(tests_dir / f"test_{target.parent.name}.py")

        for candidate in candidates:
            if candidate.exists():
                return [self._project_relative_text(candidate)]

        return ["tests"] if tests_dir.exists() else []

    async def _run_process(
        self,
        cmd: list[str],
        *,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.project_root),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return {
                "ok": False,
                "exit_code": None,
                "output": f"Command timed out after {timeout_seconds} seconds: {' '.join(cmd)}",
            }

        output = (
            stdout.decode(errors="ignore") + "\n" + stderr.decode(errors="ignore")
        ).strip()
        return {
            "ok": process.returncode == 0,
            "exit_code": process.returncode,
            "output": output,
        }

    @staticmethod
    def _is_result_ok(result: Any) -> bool:
        if not isinstance(result, dict):
            return True
        ok_value = result.get("ok")
        if isinstance(ok_value, bool):
            return ok_value
        status = str(result.get("status", "")).strip().lower()
        if status in {"success", "ok", "completed"}:
            return True
        if status in {"error", "failed", "failure", "blocked"}:
            return False
        return True

    @staticmethod
    def _error_result(message: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "status": "error", "message": message}
        payload.update(extra)
        return payload

    @staticmethod
    def _success_result(**extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": True, "status": "success"}
        payload.update(extra)
        return payload

    def _load_governance_rules(self) -> list[str]:
        default_rules = self._normalize_global_governance_rules(
            DEFAULT_ACTIVE_GOVERNANCE_RULES
        )
        if self.governance_rules_path.exists():
            try:
                with self.governance_rules_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, list):
                    rules = self._normalize_global_governance_rules(payload)
                    if rules != payload:
                        self.governance_rules = rules
                        self._save_governance_rules()
                    return rules
            except Exception:
                pass
        try:
            self.governance_rules_path.parent.mkdir(parents=True, exist_ok=True)
            with self.governance_rules_path.open("w", encoding="utf-8") as handle:
                json.dump(default_rules, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return default_rules

    def _normalize_global_governance_rules(self, rules: Any) -> list[str]:
        catalog = [str(item).strip() for item in self.AVAILABLE_GOVERNANCE_RULES]
        incoming = rules if isinstance(rules, list) else []
        normalized = [str(item).strip() for item in incoming if str(item).strip()]
        return list(dict.fromkeys([*catalog, *normalized]))

    def _save_governance_rules(self) -> None:
        self.governance_rules = self._normalize_global_governance_rules(
            self.governance_rules
        )
        self.governance_rules_path.parent.mkdir(parents=True, exist_ok=True)
        with self.governance_rules_path.open("w", encoding="utf-8") as handle:
            json.dump(self.governance_rules, handle, ensure_ascii=False, indent=2)

    def _log(self, data: dict[str, Any]) -> None:
        print(json.dumps(data, ensure_ascii=False), flush=True)

    async def _manage_startup_entry(self, enable: bool) -> None:
        """Manages Windows startup shortcut to ensure persistence across reboots."""
        if sys.platform != "win32":
            return

        appdata = os.environ.get("APPDATA")
        if not appdata:
            return
            
        startup_folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if not startup_folder.exists():
            return
            
        shortcut_path = startup_folder / "GPTBridge.lnk"
        
        if enable:
            if not shortcut_path.exists():
                script_path = self.project_root / "run.py"
                target = sys.executable
                cfg = load_config()
                profile_name = cfg.get("profile", "main")
                args = f'"{script_path}" serve --profile {profile_name}'
                
                ps_cmd = (
                    f'$WshShell = New-Object -ComObject WScript.Shell; '
                    f'$Shortcut = $WshShell.CreateShortcut("{shortcut_path}"); '
                    f'$Shortcut.TargetPath = "{target}"; '
                    f'$Shortcut.Arguments = "{args.replace(chr(34), "`" + chr(34))}"; '
                    f'$Shortcut.WorkingDirectory = "{self.project_root}"; '
                    f'$Shortcut.Save()'
                )
                process = await asyncio.create_subprocess_exec(
                    "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await process.communicate()
        else:
            if shortcut_path.exists():
                with contextlib.suppress(Exception):
                    shortcut_path.unlink()

    async def _post_operation_sync(self, operation_type: str, context: dict[str, Any]) -> None:
        """
        Triggered after file modifications or moves to ensure systemic stability.
        Delegates refactoring tasks to Agent Coder.
        """
        if not self.autonomous_agent:
            return

        self.core_logger.info("core", f"Starting post-{operation_type} synchronization.", context)
        
        if operation_type == "move":
            # Automate import updates across the project
            if hasattr(self.autonomous_agent, "fix_all_imports"):
                await self.autonomous_agent.fix_all_imports(
                    old_path=context.get("from"),
                    new_path=context.get("to")
                )
        elif operation_type == "save":
            # Perform background integrity check for the modified file
            if hasattr(self.autonomous_agent, "verify_system_integrity"):
                await self.autonomous_agent.verify_system_integrity(rel_path=context.get("path"))

    async def delete_code_from_disk(self, rel_path: str) -> dict[str, Any]:
        """Global deletion permission: Audits and removes a file or directory."""
        try:
            target_path = self._resolve_project_path(rel_path)
        except PermissionError as exc:
            return self._error_result(str(exc))
        governance_root = self.governance_rules_path.parent.resolve()

        # Governance Protection: Exclude Agent from deleting rules
        if self._is_within(target_path, governance_root):
            return self._error_result("Access Denied: Governance rules are protected from deletion.")

        if self.enforcer:
            audit = self.enforcer.validate_operation(
                GovernanceOperation(
                    actor="agent_coder",
                    action="delete_file",
                    target=str(target_path.relative_to(self.project_root)),
                    reason="Refactoring/Cleanup",
                ).to_dict()
            )
            if not audit.get("allowed", True):
                return self._error_result(f"Governance Block: {audit.get('reason')}")

        if self.backup_manager:
            self.backup_manager.create_snapshot(operation_reason=f"Agent Delete: {rel_path}")

        try:
            if target_path.is_file():
                target_path.unlink()
            elif target_path.is_dir():
                shutil.rmtree(target_path)
            else:
                return self._error_result("Target does not exist.", path=rel_path)
            self.core_logger.info("core", f"Agent deleted: {rel_path}")
            return self._success_result(path=rel_path)
        except Exception as e:
            return self._error_result(str(e))

    async def move_code_on_disk(self, src_rel: str, dst_rel: str) -> dict[str, Any]:
        """Classification/Integration permission: Moves or renames project resources."""
        try:
            src_path = self._resolve_project_path(src_rel)
            dst_path = self._resolve_project_path(dst_rel)
        except PermissionError as exc:
            return self._error_result(str(exc))
        governance_root = self.governance_rules_path.parent.resolve()

        # Governance Protection: Exclude Agent from re-classifying governance rules
        if self._is_within(src_path, governance_root) or self._is_within(dst_path, governance_root):
            return self._error_result("Access Denied: Governance resources cannot be moved.")

        if self.enforcer:
            audit = self.enforcer.validate_operation(
                GovernanceOperation(
                    actor="agent_coder",
                    action="move_file",
                    target=src_rel,
                    destination=dst_rel,
                    reason="Resource Migration",
                ).to_dict()
            )
            if not audit.get("allowed", True):
                return self._error_result(f"Governance Block (Move): {audit.get('reason')}")

        if self.backup_manager:
            self.backup_manager.create_snapshot(operation_reason=f"Agent Move: {src_rel} -> {dst_rel}")

        try:
            if not src_path.exists():
                return self._error_result("Source path does not exist.", from_path=src_rel)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            
            # Trigger automatic update of systemic references
            await self._post_operation_sync("move", {"from": src_rel, "to": dst_rel})
            
            self.core_logger.info("core", f"Agent moved {src_rel} to {dst_rel}")
            return self._success_result(**{"from": src_rel, "to": dst_rel})
        except Exception as e:
            return self._error_result(str(e))

    async def request_agent_intervention(self, rel_path: str, content: str) -> dict[str, Any]:
        """
        Forced intervention: Agent Coder reviews the provided code against governance 
        standards and offers a corrected version or a blocking audit.
        """
        if not self.autonomous_agent:
            return self._error_result("Agent Coder is not available.")

        self.core_logger.info("agent", f"Agent intervention triggered for {rel_path}")
        # Delegate to the autonomous agent for re-layering and standard enforcement
        intervention_result = await self.autonomous_agent.review_and_fix(rel_path, content)
        if isinstance(intervention_result, dict):
            ok = self._is_result_ok(intervention_result)
            intervention_result.setdefault("ok", ok)
            intervention_result.setdefault("status", "success" if ok else "error")
        return intervention_result

    async def run_unit_tests(self, target_path: str = "") -> dict[str, Any]:
        """Executes pytest to verify code integrity. Available for Agent Coder and UI."""
        if self.core_logger:
            self.core_logger.info("core", f"Running unit tests for {target_path or 'project'}")
        try:
            resolved = self._resolve_project_path(target_path) if target_path else None
            suffix = resolved.suffix.lower() if resolved is not None else ""
            if suffix in {".ts", ".tsx"}:
                npm = "npm.cmd" if os.name == "nt" else "npm"
                result = await self._run_process(
                    [npm, "run", "type-check"],
                    timeout_seconds=120,
                )
                result.update(
                    {
                        "command": " ".join([npm, "run", "type-check"]),
                        "targets": ["type-check"],
                    }
                )
                return result

            targets = self._resolve_test_targets(target_path)
            if not targets:
                return self._success_result(
                    output="No pytest tests were found for this project.",
                    targets=[],
                    command="",
                )

            cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", *targets]
            result = await self._run_process(cmd, timeout_seconds=120)
            result.update({"command": " ".join(cmd), "targets": targets})
            return result
        except Exception as e:
            return self._error_result(f"Test execution failed: {e}")

    async def instruct_agent_on_code(self, rel_path: str, content: str, instruction: str, auto_test: bool = True) -> dict[str, Any]:
        """
        Sends a user instruction to the Agent Coder for a specific code file.
        """
        if not self.autonomous_agent:
            return self._error_result("Agent Coder is not available.")

        self.core_logger.info("agent", f"Instruction for {rel_path}: {instruction}")
        
        # Delegate to the autonomous agent to process the instruction and return a suggested fix/result
        if hasattr(self.autonomous_agent, "process_instruction"):
            result = await self.autonomous_agent.process_instruction(rel_path, content, instruction, auto_test=auto_test)
            if isinstance(result, dict):
                ok = self._is_result_ok(result)
                result.setdefault("ok", ok)
                result.setdefault("status", "success" if ok else "error")
                if auto_test and result.get("suggested_fix"):
                    test_result = await self.run_unit_tests(rel_path)
                    result["test_result"] = test_result
                    result["test_output"] = str(test_result.get("output", ""))
                    result["test_ok"] = bool(test_result.get("ok"))
                    if not test_result.get("ok"):
                        result["status"] = "warning"
                        result["message"] = (
                            f"{result.get('message', 'Agent repair suggestion ready')}; "
                            "auto test failed"
                        )
            return result
        
        return self._error_result("Agent Coder does not support 'process_instruction' yet.")

    async def execute_agent_tool_operation(self, service_name: str, command: str, payload: dict) -> dict:
        """Allows Agent Coder to control various tools with governance oversight."""
        if not self.enforcer:
            return {"ok": False, "message": "Governance system is offline."}

        # Exclude Permission: Agent Coder is forbidden from managing governance rules
        if "governance" in command.lower() or "rules" in command.lower():
            return {"ok": False, "message": "Governance Block: Agent Coder cannot manage rules."}
            
        # Governance Audit for Tool Control
        audit = self.enforcer.validate_operation(
            GovernanceOperation(
                actor="agent_coder",
                action="modify_workflow",
                target=f"{service_name}:{command}",
                reason="Agent Tool Execution",
                metadata={"service": service_name, "command": command, "payload": payload},
            ).to_dict()
        )
        if not audit.get("allowed", True):
            return {"ok": False, "message": f"Governance Block: {audit.get('reason')}"}
            
        service = getattr(self, f"{service_name}_service", None) or getattr(self, service_name, None)
        if not service:
            return {"ok": False, "message": f"Service {service_name} is unavailable."}

        self.core_logger.info("agent", f"Agent Coder executing {command} on {service_name}")
        
        # Dispatch to the appropriate service handler
        if hasattr(service, "handle"):
            return await service.handle(command, payload)
        elif hasattr(service, command):
            method = getattr(service, command)
            return await method(payload) if asyncio.iscoroutinefunction(method) else method(payload)
        
        return {"ok": False, "message": f"Method {command} not found on {service_name}."}

    async def update_config_value(self, key: str, value: Any) -> None:
        """Update config and persist. Restarts backup task if auto_cycle changes."""
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)
        if key == "auto_cycle":
            try:
                cycle_val = int(value)
                if cycle_val <= 0:
                    cycle_val = 60
            except (ValueError, TypeError):
                cycle_val = 60
            self.auto_cycle = cycle_val
            if self.backup_manager:
                await self.backup_manager.stop_auto_backup()
                await self.backup_manager.start_auto_backup(interval_seconds=cycle_val)
            self._log({"type": "info", "message": f"Auto cycle updated to {cycle_val}s and persisted."})

    async def save_code_to_disk(self, rel_path: str, content: str, force_agent_review: bool = True) -> dict[str, Any]:
        """
        Implementation of 'Save Code': Audits, snapshots, and writes to disk.
        Optionally forces Agent Coder intervention for architecturally critical files.
        """
        try:
            target_path = self._resolve_project_path(rel_path)
        except PermissionError as exc:
            return self._error_result(str(exc))
        governance_root = self.governance_rules_path.parent.resolve()

        # Governance Protection: Exclude Agent from modifying rule definitions manually
        if self._is_within(target_path, governance_root):
            return self._error_result("Access Denied: Governance rules can only be modified via User interface.")

        # 1.5. Agent Coder forced intervention check
        if force_agent_review and self.autonomous_agent and hasattr(self.autonomous_agent, "review_and_fix"):
            review = await self.autonomous_agent.review_and_fix(rel_path, content)
            if not self._is_result_ok(review):
                return self._error_result(f"Agent Intervention Block: {review.get('message')}")
            # If agent suggests a fix, use it instead
            if review.get("suggested_fix"):
                content = review["suggested_fix"]

        # 2. Governance Enforcer Audit
        if self.enforcer:
            audit_result = self.enforcer.validate_operation(
                GovernanceOperation(
                    actor="user",
                    action="modify_file",
                    target=rel_path,
                    reason="Manual Save Code",
                    content=content,
                ).to_dict()
            )
            if not audit_result.get("allowed", True):
                error_msg = f"Governance Block: {audit_result.get('reason', 'Violation of coding standards')}"
                self.core_logger.warning("governance", error_msg, {"path": rel_path, "audit_result": audit_result})
                return self._error_result(
                    error_msg,
                    rule_id=audit_result.get("rule_id"),
                    details=audit_result.get("details", {}),
                )

        # 3. Pre-write snapshot for recovery
        if self.backup_manager:
            self.backup_manager.create_snapshot(operation_reason=f"Manual Save Code: {rel_path}")

        # 4. File I/O
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            
            # Trigger post-save integrity verification
            await self._post_operation_sync("save", {"path": rel_path})
            
            self.core_logger.info("core", f"User successfully saved code to {rel_path}")
            return self._success_result(path=rel_path)
        except Exception as e:
            self.core_logger.error("core", f"Save code failed for {rel_path}: {e}")
            return self._error_result(str(e))

    async def _ensure_playwright_browsers_installed(self) -> None:
        """Ensure required browser channels are available for Playwright."""
        if self._edge_executable_exists():
            self._log(
                {
                    "type": "info",
                    "message": "Microsoft Edge is available; skipping browser install during startup.",
                }
            )
            return

        if os.environ.get("GPTBRIDGE_INSTALL_BROWSERS_ON_STARTUP") != "1":
            self._log(
                {
                    "type": "warn",
                    "message": "Microsoft Edge was not found; browser install is deferred to keep startup fast.",
                }
            )
            return

        self._log(
            {
                "type": "info",
                "message": "Checking Playwright browser installations...",
            }
        )

        channels = ["msedge"]
        for channel in channels:
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright",
                    "install",
                    channel,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    self._log(
                        {
                            "type": "warn",
                            "message": f"Playwright channel '{channel}' install check timed out; continuing startup.",
                        }
                    )
                    continue
                if process.returncode == 0:
                    self._log(
                        {
                            "type": "info",
                            "message": f"Browser channel '{channel}' is ready.",
                        }
                    )
                else:
                    detail = stderr.decode(errors="ignore").strip()
                    self._log(
                        {
                            "type": "warn",
                            "message": f"Playwright channel '{channel}' note: {detail}",
                        }
                    )
            except Exception as exc:
                self._log(
                    {
                        "type": "error",
                        "message": f"Error checking browser channel {channel}: {exc}",
                    }
                )

    @staticmethod
    def _edge_executable_exists() -> bool:
        if shutil.which("msedge") or shutil.which("microsoft-edge"):
            return True

        if sys.platform != "win32":
            return False

        candidates = []
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")

        return any(path.exists() for path in candidates)

    async def initialize(
        self,
        mode: str = "full",
        profile: str = "main",
        headless: bool = False,
    ) -> None:
        # Windows 11 baseline: force Edge headful mode.
        _ = headless

        # Unify configuration management via ConfigManager for global persistence
        cfg = load_config()
        try:
            self.max_backup_count = int(cfg.get("max_backup_count", 3))
        except (ValueError, TypeError):
            self.max_backup_count = 3
        try:
            self.auto_cycle = int(cfg.get("auto_cycle", 60))
        except (ValueError, TypeError):
            self.auto_cycle = 60

        # Persistence: mark as active once per process; safe and full mode both call initialize.
        if cfg.get("auto_start") is not True:
            cfg["auto_start"] = True
            save_config(cfg)
        if not self._startup_persistence_synced:
            await self._manage_startup_entry(enable=True)
            self._startup_persistence_synced = True

        self._log({"type": "info", "message": f"Loaded auto_cycle: {self.auto_cycle}s"})

        project_root = self.project_root

        if self.history_manager is None:
            self.history_manager = OptimizationHistoryManager()
        if self.project_agent is None:
            self.project_agent = ProjectAgent(max_backup_count=self.max_backup_count)
        else:
            self.project_agent.max_backup_count = self.max_backup_count

        ensure_backup_layout(project_root)

        if self.core_logger is None:
            self.core_logger = CoreLogger(project_root)
        if self.enforcer is None:
            self.enforcer = GovernanceEnforcer(project_root, self.core_logger)
        if self.task_queue is None:
            self.task_queue = TaskQueue(project_root, self.core_logger)
        if self.toolbox_service is None:
            self.toolbox_service = ToolboxService(project_root, self.enforcer)
        if self.developer_service is None:
            self.developer_service = DeveloperService(project_root)
        if self.rescue_service is None:
            self.rescue_service = RescueService(self, project_root)
        if self.mode_manager is None:
            self.mode_manager = ModeManager(self)

        try:
            if mode == "full":
                await self.mode_manager.initialize_full_mode(profile, headless)
                self._mark_startup_phase("full_mode_ready")
                self._log({"type": "status", "status": "ready"})
                return

            if mode == "safe":
                await self.mode_manager.initialize_safe_mode()
                return

            raise ValueError(f"unknown mode: {mode}")

        except Exception as global_err:
            self._log(
                {
                    "type": "status",
                    "status": "error",
                    "message": str(global_err),
                }
            )
            if mode == "full":
                if self.core_logger:
                    self.core_logger.write(
                        "error",
                        "full mode startup failed; entering safe mode",
                        {"error": str(global_err)},
                    )
                await self.initialize(mode="safe", profile=profile, headless=False)
                return
            raise

    async def ensure_browser_ready(self) -> None:
        if self.session is None:
            raise RuntimeError("browser is unavailable in Emergency Safe Mode")

        if not self.session.is_initialized:
            await self.session.ensure_initialized()

    async def shutdown(self) -> None:
        # Persistence: If manually stopped, disable auto-start
        if getattr(self, "_manual_shutdown", False):
            cfg = load_config()
            cfg["auto_start"] = False
            save_config(cfg)
            await self._manage_startup_entry(enable=False)

        if self._active_audit_task and not self._active_audit_task.done():
            self._active_audit_task.cancel()

        pending_tasks = [task for task in self._command_tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        self._command_tasks.clear()
        self._command_task_meta.clear()
        self._active_audit_task = None

        if self.connection_monitor_task and not self.connection_monitor_task.done():
            self.connection_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.connection_monitor_task
        self.connection_monitor_task = None

        if self.backup_manager is not None:
            await self.backup_manager.stop_auto_backup()
            self.backup_manager = None

        if self.mode_manager is not None:
            for service in self.mode_manager.all_mode_services():
                if hasattr(service, "shutdown"):
                    await service.shutdown()

        if self.session is not None:
            await self.session.shutdown()
            self.session = None


async def main() -> None:
    app_instance = GPTBridgeApp()
    parser = argparse.ArgumentParser(description="GPTBridge Mother Tool Entry")
    parser.add_argument(
        "--profile",
        default="main",
        help="Browser profile name.",
    )
    parser.add_argument(
        "--generate-child-tool",
        dest="generate_child_tool",
        metavar="NAME",
        help="Generate a child tool from the mother tool with the given name.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the IPC server for the mother tool.",
    )
    parser.add_argument(
        "--auto-kill-backend-port",
        action="store_true",
        help="Automatically terminate a previous GPTBridge backend holding port 8765 before starting.",
    )

    args = parser.parse_args()

    try:
        if args.generate_child_tool:
            workspace = ChildToolWorkspace(Path(__file__).resolve().parent.parent)
            result = workspace.create_project(args.generate_child_tool, "python_desktop")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            await run_server(
                app_instance,
                profile=args.profile,
                auto_kill_backend_port=args.auto_kill_backend_port,
            )
    finally:
        await app_instance.shutdown()


if __name__ == "__main__":
    try:
        from startup import run_cli

        run_cli()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
