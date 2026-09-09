from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from .native_constants import InvestmentAnalyzer, MarketSearcher


class StarNativeInferenceMixin:
    """Inference orchestration: grounding, generation, and response assembly."""

    @staticmethod
    def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def infer(
        self,
        payload: dict[str, Any],
        *,
        database: dict[str, Any],
        analyze: InvestmentAnalyzer,
        search: MarketSearcher,
    ) -> dict[str, Any]:
        prompt = str(payload.get("instruction") or payload.get("prompt") or "").strip()
        if not prompt:
            return {
                "ok": False,
                "error_code": "PROMPT_REQUIRED",
                "message": "請輸入要查詢或分析的內容。",
            }

        intent = str(payload.get("_governed_intent") or self._intent(prompt))
        supplied_semantic_plan = payload.get("_semantic_plan")
        semantic_plan = (
            dict(supplied_semantic_plan)
            if isinstance(supplied_semantic_plan, Mapping)
            else self.semantic_plan(
                str(payload.get("user_command") or prompt),
                context=str(payload.get("_command_context") or ""),
                confirmed=bool(
                    payload.get("confirmed") is True
                    or payload.get("destructive_confirmation") is True
                ),
            )
        )
        if intent not in semantic_plan["intents"]:
            semantic_plan["primary_intent"] = intent
            semantic_plan["intents"].insert(0, intent)
            semantic_plan["tasks"].insert(
                0,
                {
                    "sequence": 1,
                    "intent": intent,
                    "depends_on": [],
                    "required_inputs": ["document-text-or-documents"]
                    if intent == "reading"
                    else [],
                    "matched_terms": [],
                    "confidence": 0.9,
                },
            )
            for sequence, task in enumerate(semantic_plan["tasks"], start=1):
                task["sequence"] = sequence
                task["depends_on"] = [sequence - 1] if sequence > 1 else []
        holdings = payload.get("holdings")
        market_research: dict[str, Any] | None = None
        network_allowed = payload.get("allow_network") is not False
        if (
            network_allowed
            and isinstance(holdings, list)
            and holdings
            and intent in {"search", "distribution", "quote", "risk", "analysis"}
        ):
            market_research = search(
                {
                    "holdings": holdings,
                    "require_all": False,
                    "max_workers": payload.get("max_workers") or 4,
                }
            )
        analysis: dict[str, Any] | None = None
        if isinstance(holdings, list) and holdings:
            analysis = analyze(
                {
                    "holdings": holdings,
                    "analysis_parameters": payload.get("analysis_parameters") or {},
                }
            )

        if intent == "conversation":
            grounding = (
                "這是一般對話。請優先遵守使用者最新訊息的語言、格式與長度要求，"
                "直接回答，不要自行改成能力介紹，也不要加入未被要求的投資內容。"
            )
        elif intent == "self_upgrade":
            grounding = (
                "星澄已理解這是檢討、修正或自我維護命令，會立即執行模型維護、"
                "資料完整性與能力健康檢查。程式來源變更仍須通過範圍、語法、測試、"
                "多模型檢查、治理與可回復備份；治理規則永遠不可修改。"
            )
        elif intent == "coding":
            grounding = (
                "此工作已路由至星澄程式設計專家。程式碼會先建立結構化規格，"
                "再接受語法與範圍檢查；產生的內容不會在未授權時自動執行。"
            )
        elif intent == "visual":
            grounding = (
                "此工作只交給 MiniCPM-V 4.6 視覺檔案辨識專員，負責圖片、影片影格與"
                "文件影像的內容辨識、分類、標籤及摘要。模型只提供視覺分析結果，"
                "不直接搬移、刪除、覆寫檔案，也不參與其他任務。"
            )
        elif intent == "file_management":
            grounding = (
                "此工作屬於檔案管理。只有其中的視覺檔案辨識子任務可交給 MiniCPM-V 4.6；"
                "實際搬移、重新命名、刪除或覆寫仍由受治理執行層處理。"
            )
        elif intent == "reading":
            grounding = (
                "此工作已交給星澄閱讀理解模組。模組會分段閱讀提供的文字，"
                "保留文件雜湊、原文位置與引用；原文沒有答案時會明確拒絕補造。"
            )
        elif intent == "statistics":
            grounding = (
                "此工作已由主要日常模型委派給數理專家。統計結果會保留樣本數、"
                "集中趨勢與離散程度，且不使用網路資料。"
            )
        elif intent == "data_organization":
            grounding = (
                "此工作已由主要日常模型委派給數理專家。資料會依欄位、缺漏值與指定分組整理，"
                "原始值不會被猜測或補造。"
            )
        elif intent == "search":
            grounding = (
                "搜尋工作由星澄主要日常模型負責；公開來源採唯讀查詢。需要外部協作時，"
                "只由星澄經受治理 AI 通道提出申請並接收結果。"
            )
        elif intent == "calculation":
            grounding = (
                "此工作已路由至星澄數理專家。計算會保留輸入、公式、步驟與結果；"
                "缺少條件時會先標示未知量，不以猜測補值。"
            )
        elif intent == "reasoning":
            grounding = (
                "此工作已路由至星澄數理專家。推理會區分前提、推導步驟與結論，"
                "並檢查矛盾、隱含假設及證據是否足夠。"
            )
        elif intent == "distribution":
            grounding = (
                "星澄會依公開來源區分「無配息」與「資料不足」。已確認不配息的標的會顯示無配息，"
                "並使用每日負快取，避免持續重複查詢；需要時可由星澄直接查詢公開網站。"
            )
        elif intent == "quote":
            grounding = (
                "報價與基金淨值由星澄的公開來源搜尋模組取得，數值必須附來源與觀測時間；"
                "本地模型不猜測即時價格。"
            )
        elif intent in {"risk", "analysis"} and analysis is not None:
            warnings = analysis.get("risk_warnings")
            warning_count = len(warnings) if isinstance(warnings, list) else 0
            grounding = (
                f"星澄已使用自己的投資參數引擎完成分析，共辨識 {warning_count} 項風險提醒。"
                "結論來自輸入持股與星澄資料庫，不使用外部模型。"
            )
        elif intent in {"risk", "analysis"}:
            grounding = (
                "請從投資管家帶入持股後執行分析。星澄會計算集中度、幣別曝險、配息與風險參數，"
                "資料不足時會明確保留，不補造結論。"
            )
        elif intent == "status":
            tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
            grounding = (
                "星澄的本機生成服務已就緒；實際 Transformer、量化權重與安全回退狀態"
                "會由執行環境健康資訊如實揭露。"
                f"目前保存 {int(tables.get('instrument_identity') or 0)} 個標的識別、"
                f"{int(tables.get('market_observation') or 0)} 筆市場觀測。"
            )
        else:
            grounding = (
                "我是星澄，一個在本機執行並以自回歸方式生成文字的語言模型。"
                "目前提供投資資料搜尋、配息判定、參數化分析、資料品質檢查與受控自我訓練；"
                "可查詢公開網路來源，但不使用第三方生成模型，也不會猜測缺少的投資事實。"
            )

        memory_context = self._reviewed_memory_grounding(
            payload.get("memory_context")
        )
        private_context = self._native_private_grounding(
            payload.get("native_private_context")
        )
        generation_grounding = grounding
        if memory_context["text"]:
            generation_grounding = (
                f"{grounding}\n已核准且與本次需求相關的本機記憶如下；記憶只補充上下文，"
                f"不會覆蓋本次指令：\n{memory_context['text']}"
            )
        if private_context["text"]:
            generation_grounding = (
                f"{generation_grounding}\n星澄主模型資料庫中可用的已驗證私有內容如下；"
                f"僅供本次星澄原生模型推論：\n{private_context['text']}"
            )
        generation = self.language_model.generate(
            intent=intent,
            prompt=prompt,
            grounding=generation_grounding,
            max_tokens=int(
                self._bounded_number(
                    payload.get("max_output_tokens"), 180, 16, 360
                )
            ),
            temperature=self._bounded_number(
                payload.get("temperature"), 0.55, 0.0, 2.0
            ),
            top_k=int(self._bounded_number(payload.get("top_k"), 4, 1, 20)),
        )
        response = str(generation["text"])
        grounding_tokens = set(self.language_model.tokenize(generation_grounding))
        response_tokens = set(self.language_model.tokenize(response))
        grounding_coverage = (
            len(grounding_tokens & response_tokens) / len(grounding_tokens)
            if grounding_tokens
            else 1.0
        )
        if grounding_coverage < 0.55:
            response = generation_grounding
            response_tokens = set(self.language_model.tokenize(response))
            grounding_coverage = 1.0
            generation["text"] = response
            generation["token_count"] = len(self.language_model.tokenize(response))
            generation["grounding_fallback_used"] = True
            generation["grounding_fallback_reason"] = "semantic-coverage-gate"
        quality_score = round(
            min(
                1.0,
                0.45
                + (0.25 if generation["facts_preserved"] else 0.0)
                + min(0.3, grounding_coverage * 0.3),
            ),
            4,
        )
        training_candidate = {
            "intent": intent,
            "input_text": prompt,
            "target_text": response,
            "source_type": "self-distillation-grounded",
            "quality_score": quality_score,
            "validated": bool(
                generation["facts_preserved"]
                and grounding_coverage >= 0.55
                and 8 <= generation["token_count"] <= 360
                and not memory_context["evidence"]
                and not private_context["text"]
            ),
            "validation": {
                "grounding_coverage": round(grounding_coverage, 4),
                "facts_preserved": generation["facts_preserved"],
                "bounded_output": 8 <= generation["token_count"] <= 360,
                "ephemeral_memory_excluded_from_training": bool(
                    memory_context["evidence"] or private_context["text"]
                ),
            },
        }

        return {
            "ok": True,
            "model": self.MODEL_ID,
            "model_version": self.VERSION,
            "architecture": self.ARCHITECTURE,
            "mode": "native-generative-language-model",
            "pipeline": [
                "star-tokenizer",
                "intent-encoder",
                "local-retrieval",
                "source-attributed-reading-router",
                "investment-tool-router",
                "autoregressive-probabilistic-decoder",
                "verified-self-training",
            ],
            "token_count": len(self._tokenize(prompt)),
            "intent": intent,
            "semantic_understanding": semantic_plan,
            "intent_confidence": semantic_plan["tasks"][0]["confidence"],
            "context_retrieval": {
                "memory_count": len(payload.get("memory_context") or [])
                if isinstance(payload.get("memory_context"), list)
                else 0,
                "used_memory_count": len(memory_context["evidence"]),
                "memory_grounding_applied": bool(memory_context["evidence"]),
                "memory_ids": [
                    item["memory_id"] for item in memory_context["evidence"]
                ],
                "reviewed_memory_only": True,
                "native_private_database_opened": bool(private_context["counts"]),
                "native_private_record_counts": private_context["counts"],
            },
            "instruction_execution": {
                "understood": True,
                "intent": intent,
                "governance_checked": True,
                "user_commands_allowed": True,
                "arbitrary_system_commands_allowed": False,
                "execution_requested": bool(
                    semantic_plan["comprehension"]["command"][
                        "execution_requested"
                    ]
                ),
                "status": "planned",
            },
            "response": response,
            "generation": generation,
            "language_model": self.training_status(),
            "analysis": analysis,
            "market_research": market_research,
            "evidence": [
                *self._database_evidence(database),
                *memory_context["evidence"],
            ],
            "evidence_policy": {
                "source_attribution_required": True,
                "unknown_values_preserved": True,
                "confidence_exposed": True,
            },
            "external_model_used": False,
            "network_used_for_inference": market_research is not None,
            "network_scope": "public-investment-sources-read-only"
            if network_allowed
            else "disabled-by-request",
            "facts_locked": True,
            "training_policy": "continuous-verified-self-distillation",
            "third_party_weights_used": False,
            "_training_candidate": training_candidate,
        }
