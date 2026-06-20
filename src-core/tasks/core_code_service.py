from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from governance.operation import GovernanceOperation
from settings.config import load_config, save_config


class CoreCodeService:
    """Application service for code editing, test execution, and agent actions."""

    def __init__(self, app: Any, project_root: Path) -> None:
        self.app = app
        self.project_root = project_root.resolve()

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
            return path.resolve().relative_to(self.project_root).as_posix()
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

    def diagnose_code(self, target_path: str = "", content: str | None = None) -> dict[str, Any]:
        try:
            target = self._resolve_project_path(target_path) if target_path else self.project_root
        except PermissionError as exc:
            return self._error_result(str(exc), path=target_path)

        relative = self._project_relative_text(target)
        suffix = target.suffix.lower()
        is_file = target.is_file()
        content_text = content
        if content_text is None and is_file:
            try:
                content_text = target.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content_text = ""
        content_text = content_text or ""

        governance_rules_path = getattr(self.app, "governance_rules_path", None)
        governance_root = (
            governance_rules_path.parent.resolve()
            if isinstance(governance_rules_path, Path)
            else None
        )
        protected = bool(governance_root and self._is_within(target, governance_root))
        test_targets = ["type-check"] if suffix in {".ts", ".tsx"} else self._resolve_test_targets(relative)
        warnings: list[str] = []
        if not target.exists():
            warnings.append("target_missing")
        if protected:
            warnings.append("governance_protected")
        if is_file and not test_targets:
            warnings.append("no_test_target")
        if len(content_text) > 500_000:
            warnings.append("large_file")

        if protected:
            risk_level = "blocked"
        elif "target_missing" in warnings or "large_file" in warnings:
            risk_level = "warning"
        elif warnings:
            risk_level = "attention"
        else:
            risk_level = "ready"

        return self._success_result(
            path=relative,
            exists=target.exists(),
            is_file=is_file,
            extension=suffix,
            language=self._language_for_suffix(suffix),
            size_bytes=target.stat().st_size if target.exists() and target.is_file() else 0,
            character_count=len(content_text),
            line_count=len(content_text.splitlines()) if content_text else 0,
            modified_at=(
                target.stat().st_mtime if target.exists() and target.is_file() else None
            ),
            protected=protected,
            risk_level=risk_level,
            warnings=warnings,
            test_targets=test_targets,
            test_command=self._test_command_for_targets(test_targets, suffix),
            recommendations=self._diagnostic_recommendations(
                risk_level,
                warnings,
                test_targets,
                suffix,
            ),
        )

    @staticmethod
    def _language_for_suffix(suffix: str) -> str:
        return {
            ".css": "css",
            ".html": "html",
            ".json": "json",
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
        }.get(suffix, "javascript")

    def _test_command_for_targets(self, targets: list[str], suffix: str) -> str:
        if not targets:
            return ""
        if targets == ["type-check"] or suffix in {".ts", ".tsx"}:
            npm = "npm.cmd" if os.name == "nt" else "npm"
            return " ".join([npm, "run", "type-check"])
        cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", *targets]
        return " ".join(cmd)

    @staticmethod
    def _diagnostic_recommendations(
        risk_level: str,
        warnings: list[str],
        test_targets: list[str],
        suffix: str,
    ) -> list[str]:
        recommendations: list[str] = []
        if risk_level == "blocked":
            recommendations.append("此路徑受治理保護，請改由治理介面處理。")
        if "target_missing" in warnings:
            recommendations.append("目標檔案不存在，請重新開啟應用程式程式碼。")
        if "large_file" in warnings:
            recommendations.append("檔案偏大，建議先拆分模組再交給救援工具修補。")
        if not test_targets:
            recommendations.append("尚未找到可對應測試，修補後請補上測試目標。")
        elif suffix in {".ts", ".tsx"}:
            recommendations.append("此檔案會先執行 TypeScript 型別檢查。")
        else:
            recommendations.append("此檔案會執行對應 pytest 測試目標。")
        return recommendations

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

    async def _post_operation_sync(self, operation_type: str, context: dict[str, Any]) -> None:
        agent = getattr(self.app, "autonomous_agent", None)
        if not agent:
            return

        logger = getattr(self.app, "core_logger", None)
        if logger is not None:
            logger.info("core", f"Starting post-{operation_type} synchronization.", context)

        if operation_type == "move" and hasattr(agent, "fix_all_imports"):
            await agent.fix_all_imports(
                old_path=context.get("from"),
                new_path=context.get("to"),
            )
            return

        if operation_type == "save" and hasattr(agent, "verify_system_integrity"):
            await agent.verify_system_integrity(rel_path=context.get("path"))

    async def delete_code_from_disk(self, rel_path: str) -> dict[str, Any]:
        try:
            target_path = self._resolve_project_path(rel_path)
        except PermissionError as exc:
            return self._error_result(str(exc))
        governance_root = self.app.governance_rules_path.parent.resolve()

        if self._is_within(target_path, governance_root):
            return self._error_result("Access Denied: Governance rules are protected from deletion.")

        enforcer = getattr(self.app, "enforcer", None)
        if enforcer:
            audit = enforcer.validate_operation(
                GovernanceOperation(
                    actor="agent_coder",
                    action="delete_file",
                    target=str(target_path.relative_to(self.project_root)),
                    reason="Refactoring/Cleanup",
                ).to_dict()
            )
            if not audit.get("allowed", True):
                return self._error_result(f"Governance Block: {audit.get('reason')}")

        backup_manager = getattr(self.app, "backup_manager", None)
        if backup_manager:
            backup_manager.create_snapshot(operation_reason=f"Agent Delete: {rel_path}")

        try:
            if target_path.is_file():
                target_path.unlink()
            elif target_path.is_dir():
                shutil.rmtree(target_path)
            else:
                return self._error_result("Target does not exist.", path=rel_path)
            logger = getattr(self.app, "core_logger", None)
            if logger is not None:
                logger.info("core", f"Agent deleted: {rel_path}")
            return self._success_result(path=rel_path)
        except Exception as exc:
            return self._error_result(str(exc))

    async def move_code_on_disk(self, src_rel: str, dst_rel: str) -> dict[str, Any]:
        try:
            src_path = self._resolve_project_path(src_rel)
            dst_path = self._resolve_project_path(dst_rel)
        except PermissionError as exc:
            return self._error_result(str(exc))
        governance_root = self.app.governance_rules_path.parent.resolve()

        if self._is_within(src_path, governance_root) or self._is_within(dst_path, governance_root):
            return self._error_result("Access Denied: Governance resources cannot be moved.")

        enforcer = getattr(self.app, "enforcer", None)
        if enforcer:
            audit = enforcer.validate_operation(
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

        backup_manager = getattr(self.app, "backup_manager", None)
        if backup_manager:
            backup_manager.create_snapshot(operation_reason=f"Agent Move: {src_rel} -> {dst_rel}")

        try:
            if not src_path.exists():
                return self._error_result("Source path does not exist.", from_path=src_rel)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            await self._post_operation_sync("move", {"from": src_rel, "to": dst_rel})

            logger = getattr(self.app, "core_logger", None)
            if logger is not None:
                logger.info("core", f"Agent moved {src_rel} to {dst_rel}")
            return self._success_result(**{"from": src_rel, "to": dst_rel})
        except Exception as exc:
            return self._error_result(str(exc))

    async def request_agent_intervention(self, rel_path: str, content: str) -> dict[str, Any]:
        agent = getattr(self.app, "autonomous_agent", None)
        if not agent:
            return self._error_result("Agent Coder is not available.")

        logger = getattr(self.app, "core_logger", None)
        if logger is not None:
            logger.info("agent", f"Agent intervention triggered for {rel_path}")

        intervention_result = await agent.review_and_fix(rel_path, content)
        if isinstance(intervention_result, dict):
            ok = self._is_result_ok(intervention_result)
            intervention_result.setdefault("ok", ok)
            intervention_result.setdefault("status", "success" if ok else "error")
        return intervention_result

    async def run_unit_tests(self, target_path: str = "") -> dict[str, Any]:
        logger = getattr(self.app, "core_logger", None)
        if logger is not None:
            logger.info("core", f"Running unit tests for {target_path or 'project'}")
        started = time.perf_counter()
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
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                    }
                )
                return result

            targets = self._resolve_test_targets(target_path)
            if not targets:
                return self._success_result(
                    output="No pytest tests were found for this project.",
                    targets=[],
                    command="",
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )

            cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", *targets]
            result = await self._run_process(cmd, timeout_seconds=120)
            result.update(
                {
                    "command": " ".join(cmd),
                    "targets": targets,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            )
            return result
        except Exception as exc:
            return self._error_result(
                f"Test execution failed: {exc}",
                duration_ms=round((time.perf_counter() - started) * 1000),
            )

    async def instruct_agent_on_code(
        self,
        rel_path: str,
        content: str,
        instruction: str,
        auto_test: bool = True,
    ) -> dict[str, Any]:
        agent = getattr(self.app, "autonomous_agent", None)
        if not agent:
            return self._error_result("Agent Coder is not available.")

        logger = getattr(self.app, "core_logger", None)
        if logger is not None:
            logger.info("agent", f"Instruction for {rel_path}: {instruction}")

        if hasattr(agent, "process_instruction"):
            result = await agent.process_instruction(
                rel_path,
                content,
                instruction,
                auto_test=auto_test,
            )
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

    async def execute_agent_tool_operation(
        self,
        service_name: str,
        command: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        enforcer = getattr(self.app, "enforcer", None)
        if not enforcer:
            return {"ok": False, "message": "Governance system is offline."}

        if "governance" in command.lower() or "rules" in command.lower():
            return {"ok": False, "message": "Governance Block: Agent Coder cannot manage rules."}

        audit = enforcer.validate_operation(
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

        service = getattr(self.app, f"{service_name}_service", None) or getattr(self.app, service_name, None)
        if not service:
            return {"ok": False, "message": f"Service {service_name} is unavailable."}

        logger = getattr(self.app, "core_logger", None)
        if logger is not None:
            logger.info("agent", f"Agent Coder executing {command} on {service_name}")

        if hasattr(service, "handle"):
            handled = await service.handle(command, payload)
            if isinstance(handled, tuple) and len(handled) == 2:
                event_name, handled_payload = handled
                if isinstance(handled_payload, dict):
                    handled_payload.setdefault("event", event_name)
                    return handled_payload
                return {"ok": True, "event": event_name, "result": handled_payload}
            return handled
        if hasattr(service, command):
            method = getattr(service, command)
            return await method(payload) if asyncio.iscoroutinefunction(method) else method(payload)

        return {"ok": False, "message": f"Method {command} not found on {service_name}."}

    async def update_config_value(self, key: str, value: Any) -> dict[str, Any]:
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
            self.app.auto_cycle = cycle_val
            backup_manager = getattr(self.app, "backup_manager", None)
            if backup_manager:
                await backup_manager.stop_auto_backup()
                await backup_manager.start_auto_backup(interval_seconds=cycle_val)
            if hasattr(self.app, "_log"):
                self.app._log(
                    {
                        "type": "info",
                        "message": f"Auto cycle updated to {cycle_val}s and persisted.",
                    }
                )
        return {"ok": True, "key": key}

    async def save_code_to_disk(
        self,
        rel_path: str,
        content: str,
        force_agent_review: bool = True,
    ) -> dict[str, Any]:
        try:
            target_path = self._resolve_project_path(rel_path)
        except PermissionError as exc:
            return self._error_result(str(exc))
        governance_root = self.app.governance_rules_path.parent.resolve()

        if self._is_within(target_path, governance_root):
            return self._error_result("Access Denied: Governance rules can only be modified via User interface.")

        agent = getattr(self.app, "autonomous_agent", None)
        if force_agent_review and agent and hasattr(agent, "review_and_fix"):
            review = await agent.review_and_fix(rel_path, content)
            if not self._is_result_ok(review):
                return self._error_result(f"Agent Intervention Block: {review.get('message')}")
            if review.get("suggested_fix"):
                content = review["suggested_fix"]

        enforcer = getattr(self.app, "enforcer", None)
        if enforcer:
            audit_result = enforcer.validate_operation(
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
                logger = getattr(self.app, "core_logger", None)
                if logger is not None:
                    logger.warning("governance", error_msg, {"path": rel_path, "audit_result": audit_result})
                return self._error_result(
                    error_msg,
                    rule_id=audit_result.get("rule_id"),
                    details=audit_result.get("details", {}),
                )

        backup_manager = getattr(self.app, "backup_manager", None)
        if backup_manager:
            backup_manager.create_snapshot(operation_reason=f"Manual Save Code: {rel_path}")

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            await self._post_operation_sync("save", {"path": rel_path})

            logger = getattr(self.app, "core_logger", None)
            if logger is not None:
                logger.info("core", f"User successfully saved code to {rel_path}")
            return self._success_result(path=rel_path)
        except Exception as exc:
            logger = getattr(self.app, "core_logger", None)
            if logger is not None:
                logger.error("core", f"Save code failed for {rel_path}: {exc}")
            return self._error_result(str(exc))
