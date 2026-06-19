from __future__ import annotations

import asyncio
import traceback
from typing import Any, Awaitable, Callable, Dict, Tuple

from orchestrator.development_rules import with_development_rules
from settings.config import load_config

PROVIDER_TIMEOUT_SECONDS = 20.0
PROVIDER_OPERATION_TIMEOUT_SECONDS = 120.0
MAX_CONTEXT_CHARS = 12000
CAPTURE_RETRY_LIMIT = 3
RESEND_RETRY_LIMIT = 1

PROVIDER_ERROR_MARKERS = (
    "captcha",
    "human verification",
    "verify you are human",
    "login expired",
    "sign in",
    "rate limit",
    "too many requests",
    "429",
    "policy",
    "unsupported request",
    "internal error",
    "provider error",
    "verification",
)

PLACEHOLDER_MARKERS = (
    "generating",
    "loading",
    "thinking",
    "please wait",
)


class ProviderManualIntervention(RuntimeError):
    pass


class ProviderCaptureFailure(RuntimeError):
    pass


class MultiAgentOrchestrator:
    def __init__(self, chatgpt, gemini, logger: Any | None = None) -> None:
        self.chatgpt = chatgpt
        self.gemini = gemini
        self.logger = logger
        self.progress_reporter: Callable[[str, int, str], Awaitable[None]] | None = None
        self.log_reporter: Callable[[str], Awaitable[None]] | None = None

    def set_progress_reporter(self, reporter: Callable[[str, int, str], Awaitable[None]] | None) -> None:
        self.progress_reporter = reporter

    def set_log_reporter(self, reporter: Callable[[str], Awaitable[None]] | None) -> None:
        self.log_reporter = reporter

    def _bind_provider_pages(self) -> None:
        if self.chatgpt.page is None:
            self.chatgpt.page = self.chatgpt.session_manager.chatgpt_page
        if self.gemini.page is None:
            self.gemini.page = self.gemini.session_manager.gemini_page

    def _log(self, message: str, data: dict[str, Any] | None = None) -> None:
        if self.logger is not None:
            self.logger.write("ai", message, data or {})

    async def _progress(self, phase: str, percent: int, message: str = "") -> None:
        if self.progress_reporter is None:
            return
        try:
            await self.progress_reporter(phase, percent, message)
        except Exception:
            pass

    @staticmethod
    def _provider_name(provider: Any, label: str) -> str:
        return str(getattr(provider, "provider_name", label.lower()))

    @staticmethod
    def _looks_like_provider_error(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in PROVIDER_ERROR_MARKERS)

    @staticmethod
    def _looks_like_placeholder(text: str) -> bool:
        stripped = text.strip().lower()
        if not stripped:
            return True
        if len(stripped) < 8:
            return True
        return any(marker == stripped or stripped.endswith(marker) for marker in PLACEHOLDER_MARKERS)

    async def _capture_with_retries(self, provider: Any, label: str) -> str:
        for attempt in range(1, CAPTURE_RETRY_LIMIT + 1):
            provider_key = self._provider_name(provider, label)
            await self._progress(f"{provider_key}_capture", 42 + attempt * 3, f"{label} capture attempt {attempt}")
            response = await provider.capture_response(timeout_seconds=PROVIDER_OPERATION_TIMEOUT_SECONDS)
            if self._looks_like_provider_error(response):
                self._log("provider manual intervention required", {"provider": label, "attempt": attempt})
                raise ProviderManualIntervention(f"{label} returned provider error UI")
            if not self._looks_like_placeholder(response):
                if attempt > 1:
                    self._log("provider capture recovered", {"provider": label, "attempt": attempt})
                return response
            self._log("provider capture retry", {"provider": label, "attempt": attempt})
            await self._progress(f"{provider_key}_retry", 48 + attempt * 4, f"{label} capture retry {attempt}")
            await provider.wait_until_idle(timeout_seconds=3, log_reporter=self.log_reporter)
        raise ProviderCaptureFailure(f"{label} capture_response failed after retries")

    async def _restart_provider(self, provider: Any, label: str) -> None:
        session_manager = getattr(provider, "session_manager", None)
        provider_name = self._provider_name(provider, label)
        if session_manager is None or not hasattr(session_manager, "restart_provider"):
            raise ProviderCaptureFailure(f"{label} browser restart is unavailable")
        self._log("provider browser restart", {"provider": provider_name})
        await session_manager.restart_provider(provider_name, preserve_session=True)
        target = getattr(session_manager, "page_targets", {}).get(provider_name) or "main"
        if provider_name == "chatgpt":
            provider.page = await session_manager.ensure_chatgpt_page(target)
        elif provider_name == "gemini":
            provider.page = await session_manager.ensure_gemini_page(target)

    async def _run_provider_inner(self, provider, label: str, prompt: str) -> str:
        provider_key = self._provider_name(provider, label)
        await self._progress(f"{provider_key}_dispatch", 24, f"{label} prompt dispatch")
        ok = await provider.dispatch_prompt(prompt, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)
        if not ok:
            detail = str(getattr(provider, "last_dispatch_error", "") or "").strip()
            message = f"{label} dispatch_prompt failed"
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeError(message)

        await self._progress(f"{provider_key}_waiting", 32, f"{label} waiting")
        idle_ok = await provider.wait_until_idle(
            timeout_seconds=int(PROVIDER_OPERATION_TIMEOUT_SECONDS),
            log_reporter=self.log_reporter,
        )
        if not idle_ok:
            print(f"[Orchestrator] warning: {label} wait_until_idle timed out")

        try:
            return await self._capture_with_retries(provider, label)
        except ProviderManualIntervention:
            raise
        except ProviderCaptureFailure:
            pass

        for attempt in range(1, RESEND_RETRY_LIMIT + 1):
            self._log("provider resend prompt", {"provider": label, "attempt": attempt})
            await self._progress(f"{provider_key}_resend", 62, f"{label} resend prompt")
            resent = await provider.dispatch_prompt(prompt, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)
            if not resent:
                continue
            await provider.wait_until_idle(timeout_seconds=int(PROVIDER_OPERATION_TIMEOUT_SECONDS), log_reporter=self.log_reporter)
            try:
                return await self._capture_with_retries(provider, label)
            except ProviderManualIntervention:
                raise
            except ProviderCaptureFailure:
                continue

        await self._restart_provider(provider, label)
        await self._progress(f"{provider_key}_restart", 68, f"{label} browser restart")
        try:
            return await self._capture_with_retries(provider, label)
        except ProviderCaptureFailure:
            self._log("provider resend after restart", {"provider": label})
            resent = await provider.dispatch_prompt(prompt, timeout_seconds=PROVIDER_TIMEOUT_SECONDS)
            if resent:
                await provider.wait_until_idle(timeout_seconds=int(PROVIDER_OPERATION_TIMEOUT_SECONDS), log_reporter=self.log_reporter)
                return await self._capture_with_retries(provider, label)
            raise

    async def _run_provider(self, provider, label: str, prompt: str) -> Tuple[str, bool]:
        if self.log_reporter:
            try:
                await self.log_reporter(f"[Developer] {label} dispatch started")
            except Exception:
                pass
        try:
            await self._progress(f"{self._provider_name(provider, label)}_running", 20, f"{label} running")
            text = await asyncio.wait_for(
                self._run_provider_inner(provider, label, prompt),
                timeout=PROVIDER_OPERATION_TIMEOUT_SECONDS,
            )
            if self.log_reporter:
                try:
                    await self.log_reporter(f"[Developer] {label} completed")
                except Exception:
                    pass
            return text, True
        except asyncio.TimeoutError:
            msg = f"[{label}] operation timed out ({int(PROVIDER_OPERATION_TIMEOUT_SECONDS)}s)"
            print(f"[Orchestrator] {msg}")
            self._log("provider timeout", {"provider": label, "message": msg})
            if self.log_reporter:
                try:
                    await self.log_reporter(f"[Developer] {label} failed: timeout")
                except Exception:
                    pass
            return msg, False
        except ProviderManualIntervention as exc:
            msg = f"[{label}] manual intervention required: {exc}"
            print(f"[Orchestrator] {msg}")
            self._log("provider manual intervention", {"provider": label, "message": msg})
            if self.log_reporter:
                try:
                    await self.log_reporter(f"[Developer] {label} failed: manual intervention")
                except Exception:
                    pass
            return msg, False
        except Exception as exc:
            msg = f"[{label}] failed: {exc}"
            print(f"[Orchestrator] {msg}")
            self._log("provider failure", {"provider": label, "message": msg})
            if self.log_reporter:
                try:
                    await self.log_reporter(f"[Developer] {label} failed: {exc}")
                except Exception:
                    pass
            return msg, False

    def _build_result(
        self,
        mode: str,
        prompt: str,
        chatgpt_response: str,
        gemini_response: str,
        chatgpt_ok: bool,
        gemini_ok: bool,
    ) -> Dict[str, object]:
        parts = [f"User request:\n{prompt}"]
        if chatgpt_response:
            parts.append(f"ChatGPT:\n{chatgpt_response}")
        if gemini_response:
            parts.append(f"Gemini:\n{gemini_response}")

        return {
            "mode": mode,
            "ok": chatgpt_ok or gemini_ok,
            "chatgpt_ok": chatgpt_ok,
            "gemini_ok": gemini_ok,
            "chatgpt_response": chatgpt_response,
            "gemini_response": gemini_response,
            "final_summary": "\n\n".join(parts),
        }

    @staticmethod
    def _trim_context(instruction: str, payload: str) -> str:
        instruction_len = len(instruction)
        max_payload = MAX_CONTEXT_CHARS - instruction_len - 10
        if max_payload < 1000:
            max_payload = 1000
            
        payload = payload.strip()
        if len(payload) > max_payload:
            payload = "...\n" + payload[-max_payload:]
        return f"{instruction}\n\n{payload}"

    @staticmethod
    def _cost_mode() -> str:
        value = str(load_config().get("ai_cost_mode", "balanced")).strip()
        return value if value in {"resource_saver", "balanced", "full_power"} else "balanced"

    def _apply_cost_mode(self, mode: str) -> str:
        cost_mode = self._cost_mode()
        if cost_mode == "resource_saver":
            self._log("ai cost mode downgraded", {"from": mode, "to": "gpt_only", "cost_mode": cost_mode})
            return "gpt_only"
        if cost_mode == "full_power" and mode in {"chatgpt_first", "gemini_first"}:
            self._log("ai cost mode upgraded", {"from": mode, "to": "ask_both", "cost_mode": cost_mode})
            return "ask_both"
        return mode

    async def _mutual_review(self, prompt: str) -> Dict[str, object]:
        await self._progress("dual_ai_initial", 18, "dual AI initial review")
        initial_instruction = (
            "Target platform is Windows 11. Provide your independent answer. "
            "Gemini focuses on architecture, risks, bugs, and optimization advice. "
        )
        initial_prompt = self._trim_context(initial_instruction, prompt)
        (gpt_initial, gpt_initial_ok), (gemini_initial, gemini_initial_ok) = await asyncio.gather(
            self._run_provider(self.chatgpt, "ChatGPT", initial_prompt),
            self._run_provider(self.gemini, "Gemini", initial_prompt),
        )

        if not gpt_initial_ok and not gemini_initial_ok:
            return self._build_result("mutual_review", prompt, gpt_initial, gemini_initial, False, False)

        if not gpt_initial_ok:
            return self._build_result("mutual_review_gemini_fallback", prompt, gpt_initial, gemini_initial, False, True)

        if not gemini_initial_ok:
            return self._build_result("mutual_review_gpt_fallback", prompt, gpt_initial, gemini_initial, True, False)

        await self._progress("dual_ai_review", 52, "dual AI cross review")
        gemini_review_instruction = (
            "Review this GPT answer once. Return corrections, architecture risks, bugs, and optimization advice. "
            "Do not request another review round."
        )
        gemini_review_prompt = self._trim_context(gemini_review_instruction, gpt_initial)
        gpt_review_instruction = (
            "Review this Gemini answer once. Return concrete implementation fixes and final execution notes. "
            "Do not request another review round."
        )
        gpt_review_prompt = self._trim_context(gpt_review_instruction, gemini_initial)

        (gpt_review, gpt_review_ok), (gemini_review, gemini_review_ok) = await asyncio.gather(
            self._run_provider(self.chatgpt, "ChatGPT", gpt_review_prompt),
            self._run_provider(self.gemini, "Gemini", gemini_review_prompt),
        )

        await self._progress("chatgpt_final", 76, "ChatGPT final integration")
        final_instruction = (
            "Produce the final GPT-integrated answer for execution. Use only one review round. "
            "Do not continue the loop. Target Windows 11 stability."
        )
        final_payload = (
            f"Original request:\n{prompt}\n\n"
            f"GPT initial:\n{gpt_initial}\n\n"
            f"Gemini initial:\n{gemini_initial}\n\n"
            f"Gemini review:\n{gemini_review}\n\n"
            f"GPT review:\n{gpt_review}"
        )
        final_prompt = self._trim_context(final_instruction, final_payload)
        gpt_final, gpt_final_ok = await self._run_provider(self.chatgpt, "ChatGPT", final_prompt)

        chatgpt_response = "\n\n".join(
            part
            for part in [
                f"Initial:\n{gpt_initial}",
                f"Review:\n{gpt_review}" if gpt_review else "",
                f"Final:\n{gpt_final}" if gpt_final else "",
            ]
            if part
        )
        gemini_response = "\n\n".join(
            part
            for part in [
                f"Initial:\n{gemini_initial}",
                f"Review:\n{gemini_review}" if gemini_review else "",
            ]
            if part
        )
        return self._build_result(
            "mutual_review",
            prompt,
            chatgpt_response,
            gemini_response,
            gpt_initial_ok and gpt_final_ok,
            gemini_initial_ok,
        )

    async def discussion_query(self, prompt: str, mode: str = "chatgpt_first") -> Dict[str, object]:
        if mode not in ("gpt_only", "gemini_only", "chatgpt_first", "gemini_first", "ask_both", "mutual_review"):
            raise ValueError("Unsupported mode")
        prompt = with_development_rules(prompt)
        requested_mode = mode
        mode = self._apply_cost_mode(mode)
        await self._progress("ai_prepare", 10, "AI prepare")

        self._bind_provider_pages()
        needs_chatgpt = mode in {"gpt_only", "chatgpt_first", "gemini_first", "ask_both", "mutual_review"}
        needs_gemini = mode in {"gemini_only", "chatgpt_first", "gemini_first", "ask_both", "mutual_review"}

        if needs_chatgpt and self.chatgpt.page is None:
            try:
                self.chatgpt.page = await self.chatgpt.session_manager.ensure_chatgpt_page()
            except Exception as e:
                self._log("failed to open chatgpt page", {"error": str(e)})

        if needs_gemini and self.gemini.page is None:
            try:
                self.gemini.page = await self.gemini.session_manager.ensure_gemini_page()
            except Exception as e:
                self._log("failed to open gemini page", {"error": str(e)})

        if (needs_chatgpt and self.chatgpt.page is None) or (needs_gemini and self.gemini.page is None):
            result = self._build_result(
                mode,
                prompt,
                "[system] ChatGPT/Gemini pages are not ready",
                "[system] ChatGPT/Gemini pages are not ready",
                False,
                False,
            )
            result["requested_mode"] = requested_mode
            result["cost_mode"] = self._cost_mode()
            return result

        chatgpt_response = ""
        gemini_response = ""
        chatgpt_ok = False
        gemini_ok = False

        try:
            if mode == "mutual_review":
                result = await self._mutual_review(prompt)
                result["requested_mode"] = requested_mode
                result["cost_mode"] = self._cost_mode()
                return result
            if mode == "gpt_only":
                await self._progress("chatgpt_stage", 24, "ChatGPT stage")
                chatgpt_response, chatgpt_ok = await self._run_provider(self.chatgpt, "ChatGPT", prompt)
            elif mode == "gemini_only":
                await self._progress("gemini_stage", 24, "Gemini stage")
                gemini_response, gemini_ok = await self._run_provider(self.gemini, "Gemini", prompt)
            elif mode == "ask_both":
                await self._progress("dual_ai_parallel", 24, "dual AI parallel")
                (chatgpt_response, chatgpt_ok), (gemini_response, gemini_ok) = await asyncio.gather(
                    self._run_provider(self.chatgpt, "ChatGPT", prompt),
                    self._run_provider(self.gemini, "Gemini", prompt),
                )
            elif mode == "chatgpt_first":
                await self._progress("chatgpt_stage", 24, "ChatGPT stage")
                chatgpt_response, chatgpt_ok = await self._run_provider(self.chatgpt, "ChatGPT", prompt)

                await self._progress("gemini_stage", 56, "Gemini review stage")
                gemini_prompt = (
                    "Review the following ChatGPT answer as a senior reviewer. "
                    "Add corrections, risks, and concrete improvement advice.\n"
                    f"{chatgpt_response}"
                )
                gemini_response, gemini_ok = await self._run_provider(
                    self.gemini, "Gemini", gemini_prompt
                )
            else:
                await self._progress("gemini_stage", 24, "Gemini stage")
                gemini_response, gemini_ok = await self._run_provider(self.gemini, "Gemini", prompt)

                await self._progress("chatgpt_stage", 56, "ChatGPT review stage")
                chatgpt_prompt = (
                    "Review the following Gemini answer as a senior reviewer. "
                    "Add corrections, risks, and concrete improvement advice.\n"
                    f"{gemini_response}"
                )
                chatgpt_response, chatgpt_ok = await self._run_provider(
                    self.chatgpt, "ChatGPT", chatgpt_prompt
                )
        except Exception:
            print(f"[Orchestrator] discussion_query unexpected error:\n{traceback.format_exc()}")

        result = self._build_result(
            mode, prompt, chatgpt_response, gemini_response, chatgpt_ok, gemini_ok
        )
        result["requested_mode"] = requested_mode
        result["cost_mode"] = self._cost_mode()
        return result
