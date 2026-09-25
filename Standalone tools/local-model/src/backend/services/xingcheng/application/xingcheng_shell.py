"""``xingcheng-shell/v1``：把所有核心包成一個「像模型」的統一對話介面。

設計採服務層 MoE（Mixture-of-Experts）：

- **gate（路由）**：確定性 intent router（``model_engines.classify_intents``
  + 運算式/文件/程式特徵偵測），對每個專家產生 0–1 權重。
- **experts（專家）**：
  - ``general`` — 共享專家，原生 Transformer 對話（chat 模板＋多輪歷史），
    永遠可作答，對應 MoE 的 shared expert；
  - ``math`` — 確定性離線計算/統計專家，可判定式問題直接接管；
  - ``reading`` — 來源標注式文件理解（需提供 documents）；
  - ``coding`` — AST 驗證的程式合成/分析；self_upgrade/repair 只產生
    提案（``source_write_performed=False``），殼層永不執行；
  - ``investment`` — 確定性投資組合分析（需提供 holdings）；
  - ``rag`` — 共享知識庫檢索回答（payload 旗標或知識庫提示詞觸發）；
  - ``web`` — 即時資訊：經受管 ``xingcheng_web_search`` 取證據後由
    shared expert grounded 生成並附引用；無證據時不生成即時資訊。
- **聚合**：top-k（上限 2）專家勝出直接作答；專家失敗或低信心時
  降回共享專家，回覆附 gate 權重與專家履歷供觀測。

模型層對應：general 專家即 ``XingChengConfig.use_moe`` 權重級 MoE
checkpoint（E=8/k=2/interval=2，由 dense v19b 經
``training/upcycle_moe.py`` 稀疏升級＋SFT 分化而來，weights v25）——
殼層 gate 決定「哪個能力中心作答」，模型層 router 決定「token 走
哪些專家」，兩級 MoE 同構。
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
    "repair": "coding",
    "self_upgrade": "coding",
    "analysis": "investment",
    "risk": "investment",
    "search": "investment",
}

#: 觸發 rag 專家的提示詞線索（知識庫/文件庫查詢語意）。
_RAG_CUES = ("知識庫", "文件庫", "資料庫裡", "內部文件", "共享文件")

#: 觸發 web 專家的即時資訊線索——模型本體沒有即時知識，命中此類
#: 問題時改走受管 ``xingcheng_web_search`` 通道取證據再回答。
_WEB_CUES = (
    "最新", "即時", "新聞", "今天天氣", "今日天氣", "現在天氣",
    "現在股價", "目前股價", "最新股價", "今天的新聞", "近日",
    "news", "weather", "latest", "current events", "recent news",
)

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
    EXPERTS = ("general", "math", "reading", "coding", "investment", "rag", "web")
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
            if not expert:
                continue
            # self_upgrade/repair 是明確的動作請求，意圖命中即接管
            # （coding 專家僅產生提案，不執行）。
            weights[expert] = max(
                weights[expert],
                0.75 if intent in {"self_upgrade", "repair"} else 0.6,
            )
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
        # 投資意圖僅在提供持股資料時接管；無資料一律降回共享專家誠實作答。
        if self._has_holdings(payload):
            weights["investment"] = max(weights["investment"], 0.85)
        if payload.get("rag") is True or payload.get("use_rag") is True or any(
            cue in prompt for cue in _RAG_CUES
        ):
            weights["rag"] = max(weights["rag"], 0.8)
        # 即時資訊：payload 旗標或提示詞即時線索 → web 專家取證據後
        # 仍由共享專家 grounded 生成（web 專家內部呼叫 general）。
        if (
            payload.get("web_search") is True
            or payload.get("use_web") is True
            or any(cue in prompt for cue in _WEB_CUES)
        ):
            weights["web"] = max(weights["web"], 0.8)
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

    @staticmethod
    def _has_holdings(payload: Mapping[str, Any]) -> bool:
        holdings = payload.get("holdings")
        return isinstance(holdings, list) and any(
            isinstance(item, Mapping) for item in holdings
        )

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

    def _expert_coding(
        self, prompt: str, payload: Mapping[str, Any], intent: str = "coding"
    ) -> dict[str, Any] | None:
        request = dict(payload)
        request.setdefault("prompt", prompt)
        try:
            result = self._service.coding_expert.process(request, intent)
        except Exception:
            return None
        if result.get("ok") is not True:
            return None
        source = str(result.get("source") or "").strip()
        if not source:
            return None
        language = str(result.get("language") or "").strip() or "text"
        text = f"```{language}\n{source}\n```"
        proposal = result.get("upgrade_proposal")
        if intent == "self_upgrade" and isinstance(proposal, dict):
            text = (
                "以下為自我升級提案（僅提案，未執行；需治理核准鏈）：\n\n"
                + text
            )
        return {
            "text": text,
            "detail": result,
        }

    def _expert_investment(
        self, prompt: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if not self._has_holdings(payload):
            return None
        try:
            from .investment_analysis import analyze_investments

            result = analyze_investments(dict(payload))
        except Exception:
            return None
        if result.get("ok") is not True:
            return None
        portfolio = result.get("portfolio") or {}
        total = portfolio.get("total_current_value_twd")
        count = portfolio.get("active_holding_count")
        lines = [f"投資組合分析（{count} 檔持倉，總值 NT${total}）："]
        models = result.get("model_results") or {}
        for key, label in (
            ("return-trend", "加權成本報酬率"),
            ("risk-volatility", "加權波動率"),
            ("income-distribution", "加權配息率"),
            ("scenario-stress", "壓力情境估計損失"),
        ):
            model = models.get(key) or {}
            metrics = model.get("metrics") or {}
            if key == "scenario-stress":
                loss = metrics.get("estimated_loss_twd")
                if loss is not None:
                    lines.append(f"- {label}：NT${loss}")
            else:
                for metric_key, value in metrics.items():
                    if value is not None and "percent" in metric_key:
                        lines.append(f"- {label}：{value:.2f}%")
                        break
        warnings = result.get("risk_warnings") or []
        if warnings:
            lines.append(f"風險提醒 {len(warnings)} 項：")
            lines.extend(
                f"- {item.get('message', '')}" for item in warnings[:5]
            )
        lines.append("（分析結果不是投資建議或交易指令。）")
        return {"text": "\n".join(lines), "detail": result}

    def _expert_rag(
        self, prompt: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        local_rag = getattr(self._service, "local_rag", None)
        if local_rag is None:
            return None
        request = dict(payload)
        request["question"] = prompt
        try:
            result = local_rag.query(request)
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("ok") is not True:
            return None
        answer = str(result.get("answer") or result.get("response") or "").strip()
        if not answer:
            return None
        citations = result.get("citations") or []
        if citations and result.get("evidence_sufficient") is True:
            answer = f"{answer}\n\n（引用 {len(citations)} 則知識庫來源）"
        return {"text": answer, "detail": result}

    def _expert_web(
        self,
        prompt: str,
        payload: Mapping[str, Any],
        history: list[dict[str, str]],
    ) -> dict[str, Any] | None:
        """即時資訊專家：受管 ``_run_web_search`` 取證據 → 注入
        general 專家 grounded 生成 → 附加來源引用。

        搜尋通道失敗時回傳 None（降回一般對話，模型以誠實邊界
        回應）；搜尋成功但無結果時回傳確定性告知；模型生成失敗
        時降為確定性結果列表——任何情況都不讓模型在無證據下
        編造即時資訊。
        """
        searcher = getattr(self._service, "_run_web_search", None)
        if searcher is None:
            return None
        query = str(payload.get("search_query") or prompt).strip()
        if not query:
            return None
        try:
            result = searcher(
                query, int(payload.get("max_results") or 5)
            )
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("ok") is not True:
            return None
        items = result.get("results") or []
        if not items:
            return {
                "text": (
                    "我搜尋了即時資料，但沒有找到相關結果。"
                    "如果你提供更多關鍵字或背景，我可以再試一次。"
                ),
                "detail": result,
            }
        # 證據塊：metadata only（title/domain/snippet），不含完整頁面。
        evidence_lines = [
            f"[{i + 1}] {item.get('title') or '(無標題)'}"
            f"（{item.get('domain') or '未知來源'}）"
            f"：{str(item.get('snippet') or '').strip()[:200]}"
            for i, item in enumerate(items[:5])
        ]
        evidence = "\n".join(evidence_lines)
        grounded_prompt = (
            f"{prompt}\n\n"
            "【即時搜尋結果】\n"
            f"{evidence}\n"
            "請只根據以上搜尋結果簡要回答；若結果不足，請說明資料不足。"
        )
        citations = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "domain": item.get("domain"),
                "rank": item.get("rank"),
            }
            for item in items[:5]
        ]
        domains = "、".join(
            dict.fromkeys(
                str(c["domain"]) for c in citations if c.get("domain")
            )
        )
        footer = f"\n\n（即時搜尋 · 來源：{domains or '網路'}）"
        general = self._expert_general(grounded_prompt, history)
        text = str(general.get("text") or "").strip()
        if general.get("ok") is not True or not text:
            # 生成失敗 → 確定性證據列表，引用照樣保留。
            text = "搜尋到以下即時結果：\n" + "\n".join(
                f"{i + 1}. {item.get('title') or '(無標題)'}"
                f"（{item.get('domain') or '未知來源'}）\n"
                f"   {str(item.get('snippet') or '').strip()[:150]}"
                for i, item in enumerate(items[:5])
            )
        return {
            "text": text + footer,
            "detail": result,
            "citations": citations,
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
                trigger = next(
                    (
                        intent
                        for intent in gate["intents"]
                        if _INTENT_TO_EXPERT.get(intent) == "coding"
                    ),
                    "coding",
                )
                specialist_out = self._expert_coding(
                    text_prompt, request_payload, trigger
                )
            elif name == "investment":
                specialist_out = self._expert_investment(
                    text_prompt, request_payload
                )
            elif name == "rag":
                specialist_out = self._expert_rag(text_prompt, request_payload)
            elif name == "web":
                specialist_out = self._expert_web(
                    text_prompt, request_payload, turns
                )
            if specialist_out is not None:
                experts_used.append(name)
                break

        if specialist_out is not None:
            response = {
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
            if specialist_out.get("citations"):
                response["citations"] = specialist_out["citations"]
            return response

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
