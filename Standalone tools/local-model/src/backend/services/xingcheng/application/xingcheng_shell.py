"""``xingcheng-shell/v1``：把所有核心包成一個「像模型」的統一對話介面。

設計採服務層 MoE（Mixture-of-Experts）：

- **gate（路由）**：確定性 intent router（``model_engines.classify_intents``
  + 運算式/文件/程式特徵偵測），對每個專家產生 0–1 權重。
- **experts（專家）**：
  - ``general`` — 共享專家，原生 Transformer 對話（chat 模板＋多輪歷史），
    永遠可作答，對應 MoE 的 shared expert；
  - ``math`` — 確定性離線計算/統計專家，可判定式問題直接接管；
  - ``reading`` — 來源標注式文件理解（需提供 documents）；
  - ``coding`` — AST 驗證的程式合成/分析。
- **聚合**：top-k（預設 1）專家勝出直接作答；專家失敗或低信心時
  降回共享專家，回覆附 gate 權重與專家履歷供觀測。

模型層對應：``XingChengConfig.use_moe``/``*_moe`` presets 提供權重級
MoE；本殼先在服務層給出相同拓撲（router→experts→shared expert），
未來換用 MoE checkpoint 時 general expert 直接升級、介面不變。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..infrastructure import native_engine

SHELL_FORMAT = "xingcheng-shell/v1"

# gate 權重達此門檻才讓專家接管；低於門檻一律走共享專家。
_EXPERT_TAKEOVER = 0.7

_HISTORY_LIMIT = 8
_HISTORY_CHARS = 1_000
_PROMPT_CHARS = 8_000

_INTENT_TO_EXPERT = {
    "calculation": "math",
    "statistics": "math",
    "data_organization": "math",
    "reading": "reading",
    "coding": "coding",
}

_EXPRESSION_PATTERN = re.compile(
    r"[0-9][0-9.()\s+*/%^×÷-]*[0-9)]\s*(?:=|等於|是多少|多少)?"
)


def _sanitize_history(history: Any) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    turns: list[dict[str, str]] = []
    for item in history[-_HISTORY_LIMIT:]:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().casefold()
        if role not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "").strip()[:_HISTORY_CHARS]
        if content:
            turns.append({"role": role, "content": content})
    return turns


class XingchengShell:
    """服務層 MoE 殼：gate→top-k experts→聚合；general 為共享專家。"""

    FORMAT = SHELL_FORMAT
    EXPERTS = ("general", "math", "reading", "coding")
    TOP_K = 2  # gate 取樣上限；共享專家不計入

    def __init__(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------------
    # gate（router）：intent 分類＋特徵偵測 → 專家權重
    # ------------------------------------------------------------------
    def _gate(self, prompt: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        intents: list[str] = []
        try:
            intents = list(
                self._service.model_engines.classify_intents(prompt)
            )[:3]
        except Exception:
            intents = []
        weights = {name: 0.0 for name in self.EXPERTS}
        weights["general"] = 1.0  # shared expert 永遠可用
        for intent in intents:
            expert = _INTENT_TO_EXPERT.get(intent)
            if expert:
                weights[expert] = max(weights[expert], 0.6)
        # 特徵加成：可解析算式 → math；有文件 → reading；
        # 明確程式產物要求 → coding。
        if _EXPRESSION_PATTERN.search(prompt) and any(
            cue in prompt for cue in ("=", "等於", "多少", "計算", "加", "減", "乘", "除")
        ):
            weights["math"] = max(weights["math"], 0.8)
        if self._has_documents(payload):
            weights["reading"] = max(weights["reading"], 0.85)
        if any(
            cue in prompt
            for cue in ("```", "函式", "function", "def ", "class ", "寫一個程式", "寫程式")
        ):
            weights["coding"] = max(weights["coding"], 0.75)
        ranked = sorted(
            ((w, name) for name, w in weights.items() if name != "general"),
            key=lambda item: (-item[0], item[1]),
        )
        chosen = [
            name for weight, name in ranked if weight >= _EXPERT_TAKEOVER
        ][: self.TOP_K]
        return {
            "intents": intents,
            "weights": weights,
            "chosen_experts": chosen,
        }

    @staticmethod
    def _has_documents(payload: Mapping[str, Any]) -> bool:
        for key in ("documents", "document_text", "text", "content", "source_text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, list) and value:
                return True
        return False

    # ------------------------------------------------------------------
    # experts
    # ------------------------------------------------------------------
    def _expert_math(self, prompt: str) -> dict[str, Any] | None:
        try:
            result = self._service.mathematical_expert.process(
                {"prompt": prompt}, "calculation"
            )
        except Exception:
            return None
        calculation = result.get("calculation") or {}
        value = calculation.get("value")
        if value is None:
            return None
        expression = str(calculation.get("expression") or "").strip()
        rendered = (
            f"{expression} = {value:g}"
            if isinstance(value, float) and value == int(value)
            else f"{expression} = {value}"
        )
        return {"text": rendered, "detail": result}

    def _expert_reading(self, prompt: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            result = self._service.reading_expert.process(dict(payload))
        except Exception:
            return None
        if result.get("ok") is not True:
            return None
        answer = str(result.get("answer") or result.get("summary") or "").strip()
        if not answer:
            return None
        return {"text": answer, "detail": result}

    def _expert_coding(self, prompt: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        request = dict(payload)
        request.setdefault("prompt", prompt)
        try:
            result = self._service.coding_expert.process(request, "coding")
        except Exception:
            return None
        if result.get("ok") is not True:
            return None
        source = str(result.get("source") or "").strip()
        if not source:
            return None
        language = str(result.get("language") or "").strip() or "text"
        return {
            "text": f"```{language}\n{source}\n```",
            "detail": result,
        }

    def _expert_general(
        self, prompt: str, history: list[dict[str, str]]
    ) -> dict[str, Any]:
        result = native_engine.generate_via_native_engine(
            {
                "prompt": prompt,
                "dialogue_interactive": True,
                "history": history,
                "intent": "conversation",
            }
        )
        return {
            "text": str(result.get("text") or ""),
            "detail": result,
            "ok": result.get("ok") is True,
        }

    # ------------------------------------------------------------------
    # 聚合：專家優先，失敗/低信心降回共享專家
    # ------------------------------------------------------------------
    def chat(
        self,
        prompt: str,
        history: Any = None,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        text_prompt = str(prompt or "").strip()[:_PROMPT_CHARS]
        if not text_prompt:
            return {
                "ok": False,
                "error_code": "EMPTY_PROMPT",
                "format": SHELL_FORMAT,
            }
        request_payload = dict(payload or {})
        request_payload.setdefault("prompt", text_prompt)
        turns = _sanitize_history(history)
        gate = self._gate(text_prompt, request_payload)

        experts_used: list[str] = []
        specialist_out: dict[str, Any] | None = None
        for name in gate["chosen_experts"]:
            if name == "math":
                specialist_out = self._expert_math(text_prompt)
            elif name == "reading":
                specialist_out = self._expert_reading(text_prompt, request_payload)
            elif name == "coding":
                specialist_out = self._expert_coding(text_prompt, request_payload)
            if specialist_out is not None:
                experts_used.append(name)
                break

        if specialist_out is not None:
            return {
                "ok": True,
                "format": SHELL_FORMAT,
                "text": specialist_out["text"],
                "expert": experts_used[0],
                "moe": {
                    "gate_intents": gate["intents"],
                    "expert_weights": gate["weights"],
                    "experts_used": experts_used,
                    "shared_expert": "general",
                    "top_k": self.TOP_K,
                },
                "expert_detail": specialist_out.get("detail"),
            }

        general = self._expert_general(text_prompt, turns)
        experts_used.append("general")
        return {
            "ok": bool(general.get("ok")),
            "format": SHELL_FORMAT,
            "text": general["text"],
            "expert": "general",
            "moe": {
                "gate_intents": gate["intents"],
                "expert_weights": gate["weights"],
                "experts_used": experts_used,
                "shared_expert": "general",
                "top_k": self.TOP_K,
            },
            "generation": general.get("detail"),
        }


class XingchengShellMixin:
    """把殼掛上 LocalAiService：``xingcheng_chat`` 統一入口。"""

    def _shell(self) -> XingchengShell:
        shell = self.__dict__.get("_xingcheng_shell")
        if shell is None:
            shell = XingchengShell(self)
            self.__dict__["_xingcheng_shell"] = shell
        return shell

    async def _handle_chat(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        import asyncio

        prompt = str(
            payload.get("prompt")
            or payload.get("message")
            or payload.get("question")
            or ""
        )
        result = await asyncio.to_thread(
            self._shell().chat,
            prompt,
            payload.get("history"),
            payload=payload,
        )
        return "xingcheng_chat_result", result
