from __future__ import annotations

from pathlib import Path
from typing import Any

from modes.mode_service import ModeService

from core_system.storage_paths import design_backup_root, ensure_backup_layout
from managers.child_tool_workspace import ChildToolWorkspace, REFERENCE_OPTIONS, sanitize_tool_name
from managers.core_governance import CoreGovernance
from managers.subsystem_backup import ScopedBackupStore


class DesignSubsystem(ModeService):
    """Design-mode subsystem for Windows 11 child-tool development only."""

    COMMANDS = {
        "design_backup",
        "design_new_project",
        "design_open_project",
        "design_new_child_file",
        "design_new_selected_file",
        "design_open_child_file",
        "design_open_selected_file",
        "design_save_child_file",
        "design_code_check",
        "design_diff_view",
        "design_generate_child_tool",
        "design_modify_child_tool",
        "design_delete_child_tool",
        "design_rename_child_tool",
        "design_release_summary",
        "design_test_child_tool",
        "design_package_child_tool",
        "design_optimize_plan",
        "design_repair_chain",
        "design_rollback_latest",
        "child_tool_code_check",
        "child_tool_repair",
        "child_tool_test",
        "child_tool_package",
        "generate_child_tool",
    }

    def __init__(self, app: Any, project_root: Path) -> None:
        self.app = app
        self.project_root = project_root.resolve()
        ensure_backup_layout(self.project_root)
        self.workspace = ChildToolWorkspace(self.project_root)
        self.core_governance = CoreGovernance(self.project_root)
        self.backups = ScopedBackupStore(
            self.workspace.workspace_root,
            design_backup_root(self.project_root),
            max_records=2,
        )

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def handle(self, command: str, payload: dict[str, Any], latest_ai_answer: str = "") -> tuple[str, dict[str, Any]]:
        self.workspace.refresh()
        self.backups.source_dir = self.workspace.workspace_root

        if command == "design_backup":
            return ("design_backup_result", self.backups.create("design"))

        if command == "design_new_project":
            return (
                "design_new_project_result",
                self.workspace.create_project_at(
                    str(payload.get("selectedRoot", "")),
                    self._tool_name(payload),
                    self._reference_type(payload),
                ),
            )

        if command == "design_open_project":
            return ("design_open_project_result", self.workspace.open_project_root(str(payload.get("selectedRoot", ""))))

        if command == "design_new_child_file":
            return ("design_new_child_file_result", self._new_child_file(payload))

        if command == "design_new_selected_file":
            return ("design_new_child_file_result", self.workspace.create_selected_file(str(payload.get("selectedPath", "")), self._reference_type(payload)))

        if command == "design_open_child_file":
            return ("design_open_child_file_result", self._open_child_file(payload))

        if command == "design_open_selected_file":
            return ("design_open_child_file_result", self.workspace.open_selected_file(str(payload.get("selectedPath", ""))))

        if command == "design_save_child_file":
            return ("design_save_child_file_result", self._save_child_file(payload))

        if command == "design_diff_view":
            return ("design_diff_view_result", self._diff_view(payload))

        if command == "design_rollback_latest":
            return ("design_rollback_latest_result", self.backups.restore_latest())

        if command in {"design_code_check", "child_tool_code_check"}:
            return ("design_code_check_result", self.workspace.check_project(self._tool_name(payload)))

        if command in {"design_test_child_tool", "child_tool_test"}:
            return ("design_test_child_tool_result", await self.workspace.test_project(self._tool_name(payload)))

        if command in {"design_generate_child_tool", "generate_child_tool"}:
            return ("design_generate_child_tool_result", await self._generate_child_tool(payload, latest_ai_answer))

        if command in {"design_package_child_tool", "child_tool_package"}:
            return ("design_package_child_tool_result", await self.workspace.package_project(self._tool_name(payload)))

        if command in {"design_modify_child_tool", "child_tool_repair"}:
            return ("design_modify_child_tool_result", self._modify_child_tool(payload, latest_ai_answer))

        if command == "design_delete_child_tool":
            if payload.get("confirmed") is not True:
                return ("design_delete_child_tool_result", {"ok": False, "message": "delete requires explicit confirmation"})
            return ("design_delete_child_tool_result", self.workspace.delete_project(self._tool_name(payload)))

        if command == "design_rename_child_tool":
            if payload.get("confirmed") is not True:
                return ("design_rename_child_tool_result", {"ok": False, "message": "rename requires explicit confirmation"})
            return ("design_rename_child_tool_result", self.workspace.rename_project(self._tool_name(payload), str(payload.get("newName", ""))))

        if command == "design_release_summary":
            return ("design_release_summary_result", self.workspace.release_summary(self._tool_name(payload)))

        if command == "design_optimize_plan":
            return ("design_optimization_plan_result", await self._optimization_plan(payload))

        if command == "design_repair_chain":
            return ("design_repair_chain_result", await self._repair_chain(payload, latest_ai_answer))

        raise ValueError(f"Unknown design subsystem command: {command}")

    def _new_child_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        tool_name = self._tool_name(payload)
        reference_type = self._reference_type(payload)
        return self.workspace.create_project(tool_name, reference_type)

    def _open_child_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.workspace.open_file(self._tool_name(payload), self._file_path(payload))

    def _save_child_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content", ""))
        return self.workspace.save_file(self._tool_name(payload), self._file_path(payload), content)

    async def _generate_child_tool(self, payload: dict[str, Any], latest_ai_answer: str) -> dict[str, Any]:
        tool_name = self._tool_name(payload)
        project = self.workspace.create_project(tool_name, self._reference_type(payload))
        ai_answer = self._ai_answer(payload, latest_ai_answer)
        if not ai_answer:
            project["message"] = "child tool project created from Windows 11 template"
            return project

        try:
            apply_result = self.workspace.apply_ai_answer(tool_name, ai_answer)
        except ValueError as exc:
            return {
                **project,
                "ok": True,
                "ai_answer_unapplied": True,
                "warning": str(exc),
                "message": "child tool project created; AI answer did not contain an applicable patch",
            }
        return {
            **apply_result,
            "project": project,
            "message": "child tool code created from AI answer" if apply_result.get("ok") else apply_result.get("message", "child tool creation failed"),
        }

    def _modify_child_tool(self, payload: dict[str, Any], latest_ai_answer: str) -> dict[str, Any]:
        ai_answer = self._ai_answer(payload, latest_ai_answer)
        if not ai_answer:
            return {
                "ok": False,
                "message": "no ai answer available; run AI consensus first or provide ai_answer",
            }

        tool_name = self._tool_name(payload)
        backup = self.backups.create("before-modify")
        try:
            result = self.workspace.apply_ai_answer(tool_name, ai_answer)
            result["backup_file"] = backup.get("backup_file")
            return result
        except Exception as exc:
            rollback = self.backups.restore_latest()
            return {
                "ok": False,
                "message": f"child tool save failed: {exc}",
                "backup_file": backup.get("backup_file"),
                "rollback": rollback,
            }

    async def _optimization_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        tool_name = self._tool_name(payload)
        context = self.workspace.context_summary(tool_name)
        if not context.get("ok"):
            return context

        prompt = (
            "You are GPTBridge GPT role. Review this Windows 11 child-tool context and propose safe improvements. "
            "Do not modify files. Return a concise plan and applicable code blocks only if the user approves later.\n\n"
            f"{context['summary']}"
        )
        try:
            response = await self._ask_chatgpt(prompt)
        except Exception as exc:
            return {
                "ok": False,
                "toolName": sanitize_tool_name(tool_name),
                "message": f"optimization plan failed: {exc}",
            }

        return {
            "ok": True,
            "toolName": sanitize_tool_name(tool_name),
            "chatgpt_response": response,
            "approval_required": True,
            "message": "optimization plan ready; user approval required before execution",
        }

    async def _repair_chain(self, payload: dict[str, Any], latest_ai_answer: str) -> dict[str, Any]:
        if payload.get("approved") is not True:
            return {
                "ok": False,
                "approval_required": True,
                "message": "repair chain requires explicit user approval",
            }

        modify_result = self._modify_child_tool(payload, latest_ai_answer)
        if not modify_result.get("ok"):
            return {
                "ok": False,
                "stage": "modify",
                "message": "repair chain stopped: ai answer save failed",
                "modify": modify_result,
            }

        test_result = await self.workspace.test_project(self._tool_name(payload))
        if not test_result.get("ok"):
            return {
                "ok": False,
                "stage": "test",
                "message": "repair chain stopped: test failed",
                "modify": modify_result,
                "test": test_result,
            }

        package_result = await self.workspace.package_project(self._tool_name(payload))
        return {
            "ok": bool(package_result.get("ok")),
            "stage": "package",
            "message": "repair chain completed" if package_result.get("ok") else "repair chain stopped: windows exe packaging failed",
            "modify": modify_result,
            "test": test_result,
            "package": package_result,
        }

    def _diff_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.workspace.context_summary(self._tool_name(payload), max_chars=20000)
        return {
            "ok": bool(context.get("ok")),
            "diff": context.get("summary", ""),
            "message": "child tool snapshot updated" if context.get("ok") else context.get("message", "child tool snapshot failed"),
        }

    async def _ask_chatgpt(self, prompt: str) -> str:
        page = await self.app.session.ensure_chatgpt_page("main")
        raw_provider = getattr(self.app.chatgpt, "_provider", self.app.chatgpt)
        raw_provider.page = page
        ok = await raw_provider.dispatch_prompt(prompt)
        if not ok:
            raise RuntimeError("ChatGPT dispatch failed")
        return await raw_provider.capture_response()

    @staticmethod
    def _tool_name(payload: dict[str, Any]) -> str:
        return sanitize_tool_name(str(payload.get("toolName", "ChildTool")).strip() or "ChildTool")

    @staticmethod
    def _file_path(payload: dict[str, Any]) -> str:
        return str(payload.get("filePath", "main.py")).strip() or "main.py"

    @staticmethod
    def _reference_type(payload: dict[str, Any]) -> str:
        value = str(payload.get("templateType") or payload.get("referenceType") or "python_desktop").strip()
        return value if value in REFERENCE_OPTIONS else "python_desktop"

    @staticmethod
    def _ai_answer(payload: dict[str, Any], latest_ai_answer: str) -> str:
        return str(
            payload.get("ai_answer")
            or payload.get("answer")
            or payload.get("text")
            or latest_ai_answer
            or ""
        ).strip()
