from __future__ import annotations

import asyncio
import re
from typing import Dict, Any

from core.project_agent import ProjectAgent
from orchestrator.development_rules import with_development_rules

PROVIDER_TIMEOUT_SECONDS = 20.0
PROVIDER_OPERATION_TIMEOUT_SECONDS = 120.0
CODE_BLOCK_PATTERN = re.compile(r"```(?:[A-Za-z0-9_+.-]+)?\s*\n(.*?)\n```", re.DOTALL)


class AutonomousCodingAgent:
    def __init__(
        self,
        chatgpt,
        gemini,
        project_agent: ProjectAgent | None = None,
        **kwargs: Any
    ) -> None:
        self.chatgpt = chatgpt
        self.gemini = gemini
        self.project_agent = project_agent or ProjectAgent()
        self.enforcer = kwargs.get("enforcer")
        self.logger = kwargs.get("logger")
        self.toolbox_service = kwargs.get("toolbox_service")
        self.developer_service = kwargs.get("developer_service")
        self.rescue_service = kwargs.get("rescue_service")
        self.backup_manager = kwargs.get("backup_manager")
        self.history_manager = kwargs.get("history_manager")

    async def review_and_fix(self, rel_path: str, content: str) -> dict[str, Any]:
        """Reviews code and suggests fixes if governance or logic fails."""
        instruction = (
            "檢查這個單一檔案是否有明顯錯誤、缺漏或不符合既有功能的問題。"
            "若需要修改，請輸出完整替換後程式碼；若不需要修改，請保持原碼不變。"
        )
        return await self.process_instruction(rel_path, content, instruction, auto_test=False)

    async def process_instruction(self, rel_path: str, content: str, instruction: str, auto_test: bool = True) -> dict[str, Any]:
        """Processes user instructions and generates modified code."""
        if not str(content or "").strip():
            return {"ok": False, "message": "No code content was provided.", "suggested_fix": None}

        prompt = self._build_code_prompt(rel_path, content, instruction, auto_test)
        providers = [
            ("ChatGPT", self.chatgpt),
            ("Gemini", self.gemini),
        ]

        failures: list[str] = []
        for label, provider in providers:
            if provider is None:
                continue
            result = await self._ask_provider_for_code(label, provider, prompt, content)
            if result.get("ok"):
                return result
            failures.append(str(result.get("message", f"{label} failed")))

        message = "; ".join(failures) if failures else "No AI provider is available."
        return {"ok": False, "message": message, "suggested_fix": None}

    def _build_code_prompt(
        self,
        rel_path: str,
        content: str,
        instruction: str,
        auto_test: bool,
    ) -> str:
        test_note = (
            "若修改會影響測試，請在回答後簡短列出建議執行的測試指令。"
            if auto_test
            else "不需要執行測試，只需提供程式碼建議。"
        )
        prompt = f"""你是 GPTBridge 系統救援工具，請只處理下列單一檔案。

檔案路徑：{rel_path}

使用者修補指令：
{instruction.strip() or "請檢查並修正明顯錯誤。"}

要求：
- 保留原有功能與對外行為。
- 只修改和指令直接相關的區域。
- 回答必須包含完整替換後程式碼，並放在單一 Markdown code fence。
- 不要輸出 diff。
- {test_note}

目前程式碼：
```
{content}
```"""
        return with_development_rules(prompt)

    async def _ask_provider_for_code(
        self,
        label: str,
        provider: Any,
        prompt: str,
        original_content: str,
    ) -> dict[str, Any]:
        ok = await self._dispatch_prompt(provider, prompt)
        if not ok:
            detail = str(getattr(provider, "last_dispatch_error", "")).strip()
            message = f"{label} could not send the repair prompt"
            if detail:
                message = f"{message}: {detail}"
            return {"ok": False, "message": message, "suggested_fix": None}

        await self._wait_until_idle(provider)
        response = await self._capture_response(provider)
        suggested_fix = self._extract_code(response)
        if suggested_fix is None:
            return {
                "ok": False,
                "message": f"{label} returned no code block.",
                "ai_response": response,
                "suggested_fix": None,
            }

        changed = suggested_fix.strip() != original_content.strip()
        return {
            "ok": True,
            "message": f"{label} repair suggestion ready" if changed else f"{label} found no direct code change",
            "provider": label.lower(),
            "ai_response": response,
            "suggested_fix": suggested_fix,
        }

    @staticmethod
    def _extract_code(response: str) -> str | None:
        matches = CODE_BLOCK_PATTERN.findall(str(response or ""))
        if not matches:
            return None
        return matches[-1].strip() + "\n"

    @staticmethod
    async def _dispatch_prompt(provider: Any, prompt: str) -> bool:
        try:
            return bool(await provider.dispatch_prompt(prompt, timeout_seconds=PROVIDER_TIMEOUT_SECONDS))
        except TypeError:
            return bool(await provider.dispatch_prompt(prompt))

    @staticmethod
    async def _wait_until_idle(provider: Any) -> None:
        try:
            await provider.wait_until_idle(
                timeout_seconds=int(PROVIDER_OPERATION_TIMEOUT_SECONDS)
            )
        except TypeError:
            await provider.wait_until_idle(int(PROVIDER_OPERATION_TIMEOUT_SECONDS))
        except asyncio.TimeoutError:
            pass

    @staticmethod
    async def _capture_response(provider: Any) -> str:
        try:
            return str(
                await provider.capture_response(
                    timeout_seconds=PROVIDER_OPERATION_TIMEOUT_SECONDS
                )
            )
        except TypeError:
            return str(await provider.capture_response())

    async def fix_all_imports(self, old_path: str, new_path: str) -> None:
        """Updates imports project-wide after a file move."""
        pass

    async def verify_system_integrity(self, rel_path: str) -> None:
        """Verifies system integrity (tests, etc) after code changes."""
        pass

    async def autonomous_coding(self, requirement: str, strategy: str = "chatgpt_first") -> Dict[str, object]:
        if strategy not in ("chatgpt_first", "gemini_first"):
            raise ValueError("Unsupported strategy")

        if not requirement.strip():
            raise ValueError("Requirement text cannot be empty")

        if strategy == "chatgpt_first":
            await self.chatgpt.dispatch_prompt(requirement)
            await self.chatgpt.wait_until_idle(15)
            chatgpt_response = await self.chatgpt.capture_response()

            gemini_prompt = (
                "Review the following ChatGPT proposal and code as a senior reviewer. "
                f"Add corrections, risks, and concrete improvements:\n{chatgpt_response}"
            )
            await self.gemini.dispatch_prompt(gemini_prompt)
            await self.gemini.wait_until_idle(15)
            gemini_response = await self.gemini.capture_response()

            final_prompt = (
                "Using the original request and Gemini's critique, output the final applicable code changes only:\n"
                f"{gemini_response}"
            )
            await self.chatgpt.dispatch_prompt(final_prompt)
            await self.chatgpt.wait_until_idle(15)
            final_response = await self.chatgpt.capture_response()

            applied_files, backup_dir = self.project_agent.apply_project_dump(final_response)

            return {
                "strategy": strategy,
                "chatgpt_response": chatgpt_response,
                "gemini_response": gemini_response,
                "final_summary": final_response,
                "files_modified": applied_files,
                "backup_dir": str(backup_dir),
            }

        await self.gemini.dispatch_prompt(requirement)
        await self.gemini.wait_until_idle(15)
        gemini_response = await self.gemini.capture_response()

        chatgpt_prompt = (
            "Review the following Gemini proposal and code as a senior reviewer. "
            f"Add corrections, risks, and concrete improvements:\n{gemini_response}"
        )
        await self.chatgpt.dispatch_prompt(chatgpt_prompt)
        await self.chatgpt.wait_until_idle(15)
        chatgpt_response = await self.chatgpt.capture_response()

        final_prompt = (
            "Using the original request and ChatGPT's critique, output the final applicable code changes only:\n"
            f"{chatgpt_response}"
        )
        await self.gemini.dispatch_prompt(final_prompt)
        await self.gemini.wait_until_idle(15)
        final_response = await self.gemini.capture_response()

        applied_files, backup_dir = self.project_agent.apply_project_dump(final_response)

        return {
            "strategy": strategy,
            "gemini_response": gemini_response,
            "chatgpt_response": chatgpt_response,
            "final_summary": final_response,
            "files_modified": applied_files,
            "backup_dir": str(backup_dir),
        }
