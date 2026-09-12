from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from typing import Any


OLLAMA_MODEL = "ibm/granite4.2:30b-q4_K_M"


class InvestmentAiConnections:
    """AI Investment Manager's governed connection to Ollama only."""

    def __init__(self) -> None:
        self._client: Any | None = None

    def bind_channel(self, channel: Any) -> None:
        from governance_rule.permission_directory.registries.permissions.tool_routes import (
            authorize_ai_route,
            tool_actor,
        )
        from shared_layer import GovernedRequestClient

        self._client = GovernedRequestClient(
            channel,
            tool_actor("ai-assistant"),
            authorize_ai_route,
            transport="ai-channel",
        )

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    @staticmethod
    def _ollama_ready() -> bool:
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Best-effort JSON extraction from an Ollama CLI text response."""
        # Try fenced code block.
        code_match = re.search(
            r"```(?:json)?\s*\n(.*?)\n```",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        candidates = [code_match.group(1)] if code_match else []
        # Try standalone JSON object/array.
        for match in re.finditer(r"(\{.*\})|(\[.*\])", text, re.DOTALL):
            candidates.append(match.group(0))
        for candidate in candidates:
            cleaned = candidate.strip()
            if not cleaned:
                continue
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                continue
        return None

    def _ollama_request_sync(
        self,
        prompt: str,
        *,
        json_mode: bool = False,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        """Synchronous Ollama CLI generate call."""
        try:
            result = subprocess.run(
                ["ollama", "run", "--nowordwrap", OLLAMA_MODEL, prompt],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "error_code": "OLLAMA_UNREACHABLE",
                "message": f"Ollama subprocess failed: {exc}",
            }
        if result.returncode != 0:
            return {
                "ok": False,
                "error_code": "OLLAMA_RUN_FAILED",
                "message": result.stderr.strip() or "ollama run exited non-zero",
            }
        text = result.stdout.strip()
        if json_mode:
            parsed = self._extract_json(text)
            if parsed is not None:
                return {"ok": True, "parsed": parsed, "raw": text}
            return {"ok": True, "raw": text}
        return {"ok": True, "response": text}

    async def _ollama_request(
        self,
        prompt: str,
        *,
        json_mode: bool = False,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        """Asynchronous Ollama CLI generate call."""
        return await asyncio.to_thread(
            self._ollama_request_sync,
            prompt,
            json_mode=json_mode,
            timeout_seconds=timeout_seconds,
        )

    def status(self) -> dict[str, Any]:
        configured = self.is_configured
        ollama_ready = self._ollama_ready()
        return {
            "ok": True,
            "transport": "ollama-local-inference",
            "queue_when_offline": False,
            "database_shared": False,
            "investment_manager_network": "embedded-browser-view",
            "service_owner": "ollama",
            "highest_authority": "governance-rule",
            "channel_top_level_tool": "embedded-browser",
            "roles": {
                "investment_manager": "offline-portfolio-state-and-settings",
                "ollama": "local-generative-inference",
                "external_ai": "browser-authenticated-ai-collaboration",
            },
            "peers": {
                "embedded-browser": {"name": "內建瀏覽器", "configured": configured},
                "ai-collaboration": {
                    "name": "外部協作",
                    "configured": configured,
                    "permission": "ai-collaboration",
                },
            },
            "ollama_ready": ollama_ready,
        }

    def _not_ready(self) -> dict[str, Any]:
        return {
            "ok": False,
            "queued": False,
            "transport": "ollama",
            "error_code": "OLLAMA_NOT_READY",
            "message": "Ollama 本機推論服務尚未就緒",
        }

    async def consult(self, prompt: str, scope: str = "local") -> dict[str, Any]:
        if not prompt.strip():
            return {"ok": False, "message": "prompt is required"}
        if scope not in {"local", "star"}:
            return {
                "ok": False,
                "queued": False,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        if not self._ollama_ready():
            return self._not_ready()
        result = await self._ollama_request(prompt, timeout_seconds=200)
        return {
            "ok": result.get("ok") is True,
            "queued": False,
            "results": {"ollama": result},
        }

    def search_investments_sync(
        self,
        holdings: list[dict[str, Any]],
        *,
        allow_external_fallback: bool = False,
    ) -> dict[str, Any]:
        del allow_external_fallback
        if not self._ollama_ready():
            return self._not_ready()
        # Ollama is a local generative model without live web access, so market
        # search is a no-op. Holdings metadata is passed directly to analysis.
        return {
            "ok": True,
            "queued": False,
            "provider": "ollama",
            "searched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "requested_count": len(holdings),
            "updated_count": 0,
            "error_count": 0,
            "errors": [],
            "results": [],
            "message": "Ollama 不執行即時網路搜尋；僅使用本地持股資料。",
        }

    def analyze_investments_sync(
        self,
        holdings: list[dict[str, Any]],
        analysis_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._ollama_ready():
            return self._not_ready()
        prompt = (
            "You are an investment risk analyst. Given the following portfolio "
            "holdings and analysis parameters, produce a JSON object with exactly "
            "two keys: 'portfolio' and 'risk_warnings'. The 'portfolio' value "
            "must be an object with at least 'active_holding_count' (int) and "
            "'market_data_coverage_percent' (int, 0-100). The 'risk_warnings' "
            "value must be a list of objects, each with 'title' (str) and "
            "'description' (str). Be concise and do not include commentary.\n\n"
            f"HOLDINGS: {json.dumps(holdings, ensure_ascii=False)}\n"
            f"ANALYSIS PARAMETERS: {json.dumps(analysis_parameters or {}, ensure_ascii=False)}"
        )
        result = self._ollama_request_sync(prompt, json_mode=True, timeout_seconds=200)
        if result.get("ok") is not True:
            return result
        parsed = result.get("parsed") or result.get("raw", {})
        if not isinstance(parsed, dict):
            parsed = {}
        return {
            "ok": True,
            "queued": False,
            "portfolio": parsed.get("portfolio", {}),
            "risk_warnings": parsed.get("risk_warnings", []),
            "model": OLLAMA_MODEL,
            "provider": "ollama",
        }

    def manage_accounting_sync(
        self,
        reconciliation: dict[str, Any],
        ledger_summary: dict[str, Any],
        *,
        trigger: str,
    ) -> dict[str, Any]:
        if not self.is_configured:
            return self._not_ready()
        payload = {
            "reconciliation": reconciliation,
            "ledger_summary": ledger_summary,
            "trigger": trigger,
            "autonomous": True,
            "request_origin": "offline-ai-investment-manager",
        }
        return self._client.request_sync(
            "xingcheng",
            "xingcheng_manage_investment_accounting",
            payload,
            timeout_seconds=120,
        )

    @staticmethod
    def _discussion_snapshot(analysis: dict[str, Any]) -> dict[str, Any]:
        portfolio = analysis.get("portfolio")
        confidence = analysis.get("confidence")
        return {
            "portfolio": dict(portfolio) if isinstance(portfolio, dict) else {},
            "model_results": {
                str(key): dict(value)
                for key, value in list(
                    (
                        analysis.get("model_results")
                        if isinstance(analysis.get("model_results"), dict)
                        else {}
                    ).items()
                )[:8]
                if isinstance(value, dict)
            },
            "model_execution": dict(analysis.get("model_execution") or {}),
            "evidence": [
                dict(item)
                for item in analysis.get("evidence", [])[:30]
                if isinstance(item, dict)
            ],
            "risk_warnings": [
                dict(item)
                for item in analysis.get("risk_warnings", [])[:30]
                if isinstance(item, dict)
            ],
            "action_plan": [
                dict(item)
                for item in analysis.get("action_plan", [])[:30]
                if isinstance(item, dict)
            ],
            "watch_triggers": [
                dict(item)
                for item in analysis.get("watch_triggers", [])[:30]
                if isinstance(item, dict)
            ],
            "confidence": dict(confidence) if isinstance(confidence, dict) else {},
            "facts_locked": True,
            "database_shared": False,
        }

    def discuss_analysis_sync(self, analysis: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured:
            return self._not_ready()
        payload = {
            "analysis_snapshot": self._discussion_snapshot(analysis),
            "request_origin": "offline-ai-investment-manager",
        }
        return self._client.request_sync(
            "xingcheng",
            "xingcheng_discuss_investment_analysis",
            payload,
            timeout_seconds=200,
        )

    def list_star_memory_sync(
        self,
        *,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "embedded-browser",
            "embedded-browser_memory_list",
            {
                "include_inactive": include_inactive,
                "limit": max(1, min(500, int(limit))),
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=45,
        )

    def review_star_memory_sync(
        self,
        memory_id: str,
        *,
        action: str,
        reviewer: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "embedded-browser",
            "embedded-browser_memory_review",
            {
                "memory_id": memory_id,
                "action": action,
                "reviewer": reviewer,
                "reason": reason,
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=45,
        )
