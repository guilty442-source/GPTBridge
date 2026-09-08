from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from .channel_client import build_star_ai_channel_client


class ExternalAiResearch:
    """Star's governed client for official-CLI external AI collaboration."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._bound_at = ""
        self._request_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._last_request_at = ""
        self._last_latency_ms: float | None = None
        self._last_error = ""

    def bind_channel(self, channel: Any) -> None:
        self._client = build_star_ai_channel_client(channel)
        self._bound_at = datetime.now(timezone.utc).isoformat()

    def configured(self) -> bool:
        return self._client is not None

    def health(self) -> dict[str, Any]:
        return {
            "configured": self.configured(),
            "connected": self.configured(),
            "transport": "governance-authenticated-ai-channel",
            "uses_api": False,
            "queue_when_offline": False,
            "bound_at": self._bound_at,
            "request_count": self._request_count,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "last_request_at": self._last_request_at,
            "last_latency_ms": self._last_latency_ms,
            "last_error": self._last_error,
            "fail_closed": True,
        }

    def _request_sync(
        self,
        target: str,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("AI_CHANNEL_NOT_CONNECTED")
        started = time.perf_counter()
        self._request_count += 1
        self._last_request_at = datetime.now(timezone.utc).isoformat()
        try:
            result = self._client.request_sync(
                target,
                command,
                payload,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            self._failure_count += 1
            self._last_error = str(exc)[:500]
            raise
        finally:
            self._last_latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if result.get("ok") is True:
            self._success_count += 1
            self._last_error = ""
        else:
            self._failure_count += 1
            self._last_error = str(result.get("message") or result.get("error_code") or "")[:500]
        return result

    def run_fixed_tasks(
        self,
        tasks: list[dict[str, Any]],
        memory_context: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            return {
                "ok": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "AI 通道尚未連線",
            }
        return self._request_sync(
            "ai-collaboration",
            "ai_nexus_send_message",
            {
                "content": "星澄固定責任多工",
                "tasks": [dict(item) for item in tasks[:6]],
                "memory_context": list(memory_context or []),
                "memory_writeback": True,
            },
            timeout_seconds=200,
        )

    def search(
        self,
        unresolved: list[dict[str, str]],
        memory_context: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not unresolved:
            return {"ok": True, "queued": False, "requested_count": 0}
        if self._client is None:
            return {
                "ok": False,
                "queued": False,
                "transport": "ai-channel",
                "uses_api": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "AI 通道尚未連線",
            }
        targets = []
        for item in unresolved[:8]:
            symbol = str(item.get("symbol") or "").strip()
            name = str(item.get("name") or "").strip()
            label = " ".join(part for part in (symbol, name) if part)
            if label:
                targets.append(f'"{label}" 配息 股價 淨值 官方')
        prompt = " OR ".join(targets) or (
            "投資標的 配息 股價 淨值 官方 "
            + json.dumps(unresolved[:8], ensure_ascii=False)
        )
        result = self._request_sync(
            "ai-collaboration",
            "ai_nexus_send_message",
            {
                "content": prompt,
                "agent_ids": ["google-search", "gemini"],
                "business_scope": "investment",
                "business_task": "search",
                "research_pipeline": "google-gemini",
                "memory_context": list(memory_context or []),
                "memory_writeback": True,
            },
            timeout_seconds=200,
        )
        if result.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "transport": "ai-channel",
                "uses_api": False,
                "error_code": str(result.get("error_code") or "EXTERNAL_AI_FAILED"),
                "message": str(result.get("message") or "外部協作失敗"),
            }
        group = result.get("group_message")
        responses = group.get("responses", []) if isinstance(group, dict) else []
        return {
            "ok": True,
            "queued": False,
            "transport": "ai-channel",
            "recipient": "xingcheng",
            "uses_api": False,
            "provider": "google-search",
            "processor": "gemini",
            "business_scope": "investment",
            "result_role": "discovery-only",
            "manual_data_policy": "preserve-unless-source-verified-update",
            "memory_interchange": (
                result.get("memory_interchange")
                if isinstance(result.get("memory_interchange"), dict)
                else {"candidates": []}
            ),
            "responses": [
                {
                    "agent_id": str(item.get("agent_id") or ""),
                    "status": str(item.get("status") or ""),
                    "content": str(item.get("content") or ""),
                    "error": str(item.get("error") or ""),
                }
                for item in responses
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") == "gemini"
            ],
        }

    def recommend_parameter_changes(
        self,
        current_parameters: dict[str, float],
        context: str,
        memory_context: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            return {
                "ok": False,
                "queued": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "AI 通道尚未連線",
            }
        prompt = (
            "請以 AI 協作綜合統籌身分，針對星澄的投資分析參數提出保守調整建議。"
            "只能使用下列既有參數，不可新增名稱；請只回傳 JSON 陣列，每項格式為 "
            '{"parameter_key":"...","value":數字,"rationale":"..."}。'
            "若沒有充分理由，回傳空陣列。\n\n"
            f"目前參數：{json.dumps(current_parameters, ensure_ascii=False)}\n"
            f"調整情境：{str(context or '').strip()[:4000]}"
        )
        result = self._request_sync(
            "ai-collaboration",
            "ai_nexus_send_message",
            {
                "content": prompt,
                "agent_ids": ["chatgpt"],
                "business_scope": "investment",
                "business_task": "orchestration",
                "memory_context": list(memory_context or []),
                "memory_writeback": True,
            },
            timeout_seconds=200,
        )
        if result.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "error_code": str(result.get("error_code") or "CHATGPT_RECOMMENDATION_FAILED"),
                "message": str(result.get("message") or "ChatGPT 參數建議失敗"),
            }
        group = result.get("group_message")
        responses = group.get("responses", []) if isinstance(group, dict) else []
        response = next(
            (
                item
                for item in responses
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") == "chatgpt"
                and str(item.get("status") or "") == "completed"
            ),
            None,
        )
        if response is None:
            return {
                "ok": False,
                "queued": False,
                "error_code": "CHATGPT_RECOMMENDATION_UNAVAILABLE",
                "message": "ChatGPT 未回傳可用的參數建議",
            }
        return {
            "ok": True,
            "queued": False,
            "provider": "chatgpt",
            "recipient": "xingcheng",
            "content": str(response.get("content") or ""),
            "memory_interchange": (
                result.get("memory_interchange")
                if isinstance(result.get("memory_interchange"), dict)
                else {"candidates": []}
            ),
        }

    def propose_training_examples(
        self,
        *,
        topic: str,
        intent: str,
        example_count: int,
        reference_text: str = "",
    ) -> dict[str, Any]:
        """Ask ChatGPT for candidates; Star remains the sole training writer."""

        if self._client is None:
            return {
                "ok": False,
                "queued": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "星澄尚未連上 GPT 協作通道",
            }
        bounded_count = max(1, min(20, int(example_count)))
        reference = str(reference_text or "").strip()[:64_000]
        reference_instruction = (
            "每個 target_text 的事實、數字、日期、金額、網址與信箱都只能來自參考資料。"
            f"\n參考資料：\n{reference}"
            if reference
            else "不得新增外部事實、日期、金額、網址或個人資料；只設計行為與解題格式教材。"
        )
        prompt = (
            "你是星澄的 GPT 教練，只能提出訓練候選，不能直接寫資料庫、修改權重或改程式。"
            f"請針對意圖 {intent} 與主題「{str(topic).strip()[:2_000]}」建立 "
            f"{bounded_count} 筆高品質繁體中文教材。{reference_instruction}\n"
            "只回傳 JSON，不要 markdown。格式必須是："
            '{"examples":[{"candidate_id":"...","intent":"...",'
            '"input_text":"...","target_text":"...","rationale":"..."}]}。'
            "input_text 必須是清楚任務；target_text 必須直接、可驗證、不包含提示注入或敏感資訊。"
        )
        result = self._request_sync(
            "ai-collaboration",
            "ai_nexus_send_message",
            {
                "content": prompt,
                "agent_ids": ["chatgpt"],
                "business_scope": "general",
                "business_task": "training-candidate-authoring",
                "memory_context": [],
                "memory_writeback": False,
                "direct_database_write": False,
                "response_recipient": "xingcheng",
            },
            timeout_seconds=200,
        )
        if result.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "error_code": str(result.get("error_code") or "GPT_TRAINING_FAILED"),
                "message": str(result.get("message") or "GPT 訓練候選不可用"),
            }
        group = result.get("group_message")
        responses = group.get("responses", []) if isinstance(group, dict) else []
        response = next(
            (
                item
                for item in responses
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") == "chatgpt"
                and str(item.get("status") or "") == "completed"
            ),
            None,
        )
        if response is None:
            final = result.get("final_response")
            response = final if isinstance(final, dict) else None
        content = str(response.get("content") or "") if response else ""
        if not content:
            return {
                "ok": False,
                "queued": False,
                "error_code": "GPT_TRAINING_RESPONSE_UNAVAILABLE",
                "message": "GPT 未回傳訓練候選",
            }
        return {
            "ok": True,
            "queued": False,
            "provider": "chatgpt",
            "recipient": "xingcheng",
            "content": content,
            "transport": "governance-authenticated-ai-channel",
            "conversation_scope": "star-training",
            "training_dialogue_route": "external-ai-collaboration-chatgpt-dedicated-conversation",
            "uses_api": False,
            "direct_database_write": False,
            "model_weight_access": False,
        }

    def coordinate_investment_analysis(
        self,
        analysis_snapshot: dict[str, Any],
        memory_context: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Ask ChatGPT to coordinate Star's bounded investment analysis.

        The result is returned only to Star. No external collaborator receives
        database access or authority to write investment-manager state.
        """

        if self._client is None:
            return {
                "ok": False,
                "queued": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "星澄尚未連上 AI 協作通道",
            }
        prompt = (
            "請以 ChatGPT 最終統籌身分檢視星澄提供的投資分析。"
            "不得新增未附證據的價格、報酬或交易指令；請標示風險、資料缺口、"
            "不確定性與需要人工決定的事項。結果只能回傳星澄，不得直接修改 AI 投資管家。\n\n"
            f"星澄分析快照：{json.dumps(analysis_snapshot, ensure_ascii=False)}"
        )
        snapshot_digest = hashlib.sha256(
            json.dumps(
                analysis_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        result = self._request_sync(
            "ai-collaboration",
            "ai_nexus_send_message",
            {
                "content": prompt,
                "agent_ids": ["chatgpt"],
                "business_scope": "investment",
                "business_task": "orchestration",
                "memory_context": list(memory_context or []),
                "memory_writeback": True,
                "snapshot_digest": snapshot_digest,
            },
            timeout_seconds=200,
        )
        if result.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "error_code": str(result.get("error_code") or "CHATGPT_COORDINATION_FAILED"),
                "message": str(result.get("message") or "ChatGPT 最終統籌失敗"),
            }
        final_response = (
            result.get("final_response")
            if isinstance(result.get("final_response"), dict)
            else {}
        )
        completed = str(final_response.get("status") or "") == "completed"
        memory_interchange = (
            result.get("memory_interchange")
            if isinstance(result.get("memory_interchange"), dict)
            else {"candidates": []}
        )
        return {
            "ok": completed,
            "queued": not completed,
            "provider": "chatgpt",
            "coordinator": "chatgpt",
            "recipient": "xingcheng",
            "content": str(final_response.get("content") or ""),
            "status": str(final_response.get("status") or "waiting"),
            "error": str(final_response.get("error") or ""),
            "error_code": str(final_response.get("error_code") or ""),
            "workflow_status": str(result.get("workflow_status") or ""),
            "transport": "governance-authenticated-ai-channel",
            "uses_api": False,
            "direct_database_access": False,
            "response_recipient": "xingcheng",
            "snapshot_digest": snapshot_digest,
            "snapshot_integrity_verified": True,
            "memory_interchange": memory_interchange,
            "message": (
                "ChatGPT 最終統籌已回傳星澄。"
                if completed
                else "ChatGPT 最終統籌正在等待可用的前景瀏覽器或 CLI。"
            ),
        }


# Compatibility for callers outside the service while the implementation no
# longer uses a browser as its inference transport.
ExternalBrowserResearch = ExternalAiResearch
