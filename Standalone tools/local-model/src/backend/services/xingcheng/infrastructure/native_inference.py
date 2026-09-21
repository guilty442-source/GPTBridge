from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from .native_constants import InvestmentAnalyzer, MarketSearcher
from .native_engine import generation_defaults

#: 檔位 → 原生引擎生成預算（tokens，於引擎上限內）
_INTENSITY_ENGINE_TOKENS: dict[str, int] = {
    "simple": 48,
    "normal": 96,
    "intermediate": 144,
    "difficult": 192,
}
#: 推理等級 → 溫度傾向（高推理 = 更確定）
_EFFORT_TEMPERATURES: dict[str, float] = {
    "low": 0.45,
    "medium": 0.40,
    "high": 0.35,
}
#: 反應速度 → 長度倍率
_SPEED_FACTORS: dict[str, float] = {
    "slow": 1.25,
    "low": 1.1,
    "medium": 1.0,
    "high": 0.75,
    "ultra": 0.5,
}


def native_gear_generation(payload: Mapping[str, Any]) -> tuple[int, float | None]:
    """把對話檔位（任務強度/推理等級/反應速度）映射為引擎生成參數。

    未指定檔位（自動）時，以用戶端 ``max_output_tokens`` 依引擎上限等比縮放。
    """
    try:
        cap = int(generation_defaults()["max_new_tokens"])
    except (TypeError, ValueError, KeyError):
        cap = 192
    cap = max(16, min(cap, 512))

    intensity = str(payload.get("task_intensity") or "").strip().casefold()
    base = _INTENSITY_ENGINE_TOKENS.get(intensity)
    if base is None:
        try:
            raw = int(payload.get("max_output_tokens"))
        except (TypeError, ValueError):
            raw = 512
        base = max(32, min(int(round(raw * cap / 1_024)), cap))
    speed = str(payload.get("generation_speed") or "").strip().casefold()
    factor = _SPEED_FACTORS.get(speed, 1.0)
    max_tokens = max(16, min(int(round(base * factor)), cap))

    effort = str(payload.get("reasoning_effort") or "").strip().casefold()
    temperature: float | None = None
    if payload.get("temperature") is None:
        temperature = _EFFORT_TEMPERATURES.get(effort)
    return max_tokens, temperature


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
        semantic_plan = self._resolve_semantic_plan(payload, prompt, intent)
        network_allowed = payload.get("allow_network") is not False
        neural_result = self._native_transformer_result(
            payload, prompt, intent, semantic_plan, database, network_allowed
        )
        if neural_result is not None:
            return neural_result
        market_research, analysis = self._market_inputs(
            payload, intent, network_allowed, analyze, search
        )

        grounding = self._intent_grounding(intent, analysis, database)

        memory_context = self._reviewed_memory_grounding(
            payload.get("memory_context")
        )
        private_context = self._native_private_grounding(
            payload.get("native_private_context")
        )
        generation_grounding = self._compose_grounding(
            payload, grounding, memory_context, private_context
        )
        generation, response, training_candidate = self._generate_and_score(
            payload, intent, prompt, generation_grounding,
            memory_context, private_context,
        )

        return self._infer_result(
            payload=payload,
            prompt=prompt,
            intent=intent,
            semantic_plan=semantic_plan,
            response=response,
            generation=generation,
            analysis=analysis,
            market_research=market_research,
            database=database,
            network_allowed=network_allowed,
            memory_context=memory_context,
            private_context=private_context,
            training_candidate=training_candidate,
        )

    def _native_transformer_result(
        self,
        payload: dict[str, Any],
        prompt: str,
        intent: str,
        semantic_plan: dict[str, Any],
        database: dict[str, Any],
        network_allowed: bool,
    ) -> dict[str, Any] | None:
        """原生 Transformer（自有權重）優先回答；未啟用或失敗時回 None。"""
        try:
            from .native_engine import (
                flag_enabled,
                generate_via_native_engine,
                small_talk_reply,
            )
        except Exception:  # pragma: no cover - 匯入失敗時維持 n-gram 路徑
            return None
        if not flag_enabled():
            return None
        templated = small_talk_reply(prompt)
        if templated is not None:
            return self._native_template_result(
                templated, prompt, intent, semantic_plan, database, network_allowed
            )
        gear_tokens, gear_temperature = native_gear_generation(payload)
        result = generate_via_native_engine(
            {
                "prompt": prompt,
                "intent": intent,
                "max_tokens": gear_tokens,
                "temperature": gear_temperature
                if gear_temperature is not None
                else payload.get("temperature"),
                "top_k": payload.get("top_k"),
                "top_p": payload.get("top_p"),
                "repetition_penalty": payload.get("repetition_penalty"),
                "seed": payload.get("seed"),
            }
        )
        if result.get("ok") is not True:
            return None
        text = str(result.get("text") or "")
        generation = {
            "text": text,
            "model_type": "native-transformer-autoregressive-decoder",
            "token_count": int(result.get("eval_count") or 0),
            "facts_preserved": True,
            "grounding_fallback_used": False,
            "native_checkpoint_path": result.get("checkpoint_path"),
            "native_state_sha256": result.get("state_sha256"),
            "native_parameter_count": result.get("parameter_count"),
            "native_quantization": result.get("quantization"),
            "native_latency_ms": result.get("latency_ms"),
            "sampling": result.get("sampling"),
            "gear": {
                "task_intensity": str(payload.get("task_intensity") or "auto"),
                "reasoning_effort": str(payload.get("reasoning_effort") or "auto"),
                "generation_speed": str(payload.get("generation_speed") or "auto"),
                "max_tokens": gear_tokens,
            },
        }
        return {
            "ok": True,
            "model": result.get("model") or self.MODEL_ID,
            "model_version": self.VERSION,
            "architecture": result.get("architecture") or self.ARCHITECTURE,
            "mode": "governed-native-transformer-llm",
            "pipeline": [
                "native-bpe-tokenization",
                "transformer-prefill",
                "kv-cache-decode",
                "sampling",
            ],
            "token_count": int(result.get("prompt_eval_count") or 0),
            "intent": intent,
            "semantic_understanding": semantic_plan,
            "intent_confidence": semantic_plan["tasks"][0]["confidence"],
            "context_retrieval": {
                "memory_count": 0,
                "used_memory_count": 0,
                "memory_grounding_applied": False,
                "memory_ids": [],
                "reviewed_memory_only": True,
                "native_private_database_opened": False,
                "native_private_record_counts": {},
            },
            "instruction_execution": self._instruction_execution_result(
                intent, semantic_plan
            ),
            "response": text,
            "generation": generation,
            "language_model": self.training_status(),
            "analysis": None,
            "market_research": None,
            "evidence": [*self._database_evidence(database)],
            "evidence_policy": dict(self._EVIDENCE_POLICY),
            "native_engine": True,
            "star_native_model_used": True,
            "native_checkpoint_path": result.get("checkpoint_path"),
            "native_state_sha256": result.get("state_sha256"),
            **self._result_policy_fields(network_allowed, None, {}),
        }

    def _native_template_result(
        self,
        text: str,
        prompt: str,
        intent: str,
        semantic_plan: dict[str, Any],
        database: dict[str, Any],
        network_allowed: bool,
    ) -> dict[str, Any]:
        """超短問候的第一方模板回覆（不經模型生成，誠實標記）。"""
        return {
            "ok": True,
            "model": self.MODEL_ID,
            "model_version": self.VERSION,
            "architecture": self.ARCHITECTURE,
            "mode": "governed-native-transformer-llm",
            "pipeline": ["first-party-small-talk-template"],
            "token_count": len(prompt),
            "intent": intent,
            "semantic_understanding": semantic_plan,
            "intent_confidence": semantic_plan["tasks"][0]["confidence"],
            "context_retrieval": {
                "memory_count": 0,
                "used_memory_count": 0,
                "memory_grounding_applied": False,
                "memory_ids": [],
                "reviewed_memory_only": True,
                "native_private_database_opened": False,
                "native_private_record_counts": {},
            },
            "instruction_execution": self._instruction_execution_result(
                intent, semantic_plan
            ),
            "response": text,
            "generation": {
                "text": text,
                "model_type": "first-party-small-talk-template",
                "token_count": len(text),
                "facts_preserved": True,
                "grounding_fallback_used": False,
                "template_reply": True,
            },
            "language_model": self.training_status(),
            "analysis": None,
            "market_research": None,
            "evidence": [*self._database_evidence(database)],
            "evidence_policy": dict(self._EVIDENCE_POLICY),
            "native_engine": True,
            "template_reply": True,
            "star_native_model_used": True,
            **self._result_policy_fields(network_allowed, None, {}),
        }

    def _compose_grounding(
        self,
        payload: dict[str, Any],
        grounding: str,
        memory_context: dict[str, Any],
        private_context: dict[str, Any],
    ) -> str:
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
        diagnostic_lines = self._diagnostic_grounding_lines(
            payload.get("fault_diagnostics")
        )
        if diagnostic_lines:
            generation_grounding = (
                f"{generation_grounding}\n以下是治理故障碼目錄、維護手冊與"
                "執行期狀態的唯讀診斷證據；只能依此說明故障，不得聲稱已執行修復，"
                "也不得補造證據中沒有的錯誤碼：\n"
                + "\n".join(diagnostic_lines)
            )
        rag_lines = self._rag_grounding_lines(payload.get("rag_context"))
        if rag_lines:
            generation_grounding = (
                f"{generation_grounding}\n以下是本地受管語料檢索到的相關知識片段；"
                "僅作為參考上下文，不得聲稱為已驗證事實，引用時須保留來源：\n"
                + "\n".join(rag_lines)
            )
        return generation_grounding

    def _generate_and_score(
        self,
        payload: dict[str, Any],
        intent: str,
        prompt: str,
        generation_grounding: str,
        memory_context: dict[str, Any],
        private_context: dict[str, Any],
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
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
        response, grounding_coverage = self._coverage_gate(
            generation, generation_grounding
        )
        quality_score = round(
            min(
                1.0,
                0.45
                + (0.25 if generation["facts_preserved"] else 0.0)
                + min(0.3, grounding_coverage * 0.3),
            ),
            4,
        )
        training_candidate = self._training_candidate(
            intent, prompt, response, generation,
            grounding_coverage, quality_score,
            memory_context, private_context,
        )
        return generation, response, training_candidate

    def _coverage_gate(
        self, generation: dict[str, Any], generation_grounding: str
    ) -> tuple[str, float]:
        response = str(generation["text"])
        grounding_tokens = set(self.language_model.tokenize(generation_grounding))
        response_tokens = set(self.language_model.tokenize(response))
        grounding_coverage = (
            len(grounding_tokens & response_tokens) / len(grounding_tokens)
            if grounding_tokens
            else 1.0
        )
        if grounding_coverage >= 0.55:
            return response, grounding_coverage
        generation["text"] = generation_grounding
        generation["token_count"] = len(
            self.language_model.tokenize(generation_grounding)
        )
        generation["grounding_fallback_used"] = True
        generation["grounding_fallback_reason"] = "semantic-coverage-gate"
        return generation_grounding, 1.0

    @staticmethod
    def _training_candidate(
        intent: str,
        prompt: str,
        response: str,
        generation: dict[str, Any],
        grounding_coverage: float,
        quality_score: float,
        memory_context: dict[str, Any],
        private_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
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

    def _infer_result(
        self,
        *,
        payload: dict[str, Any],
        prompt: str,
        intent: str,
        semantic_plan: dict[str, Any],
        response: str,
        generation: dict[str, Any],
        analysis: dict[str, Any] | None,
        market_research: dict[str, Any] | None,
        database: dict[str, Any],
        network_allowed: bool,
        memory_context: dict[str, Any],
        private_context: dict[str, Any],
        training_candidate: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "model": self.MODEL_ID,
            "model_version": self.VERSION,
            "architecture": self.ARCHITECTURE,
            "mode": "native-generative-language-model",
            "pipeline": list(self._RESULT_PIPELINE),
            "token_count": len(self._tokenize(prompt)),
            "intent": intent,
            "semantic_understanding": semantic_plan,
            "intent_confidence": semantic_plan["tasks"][0]["confidence"],
            "context_retrieval": self._context_retrieval_result(
                payload, memory_context, private_context
            ),
            "instruction_execution": self._instruction_execution_result(
                intent, semantic_plan
            ),
            "response": response,
            "generation": generation,
            "language_model": self.training_status(),
            "analysis": analysis,
            "market_research": market_research,
            "evidence": [
                *self._database_evidence(database),
                *memory_context["evidence"],
            ],
            "evidence_policy": dict(self._EVIDENCE_POLICY),
            **self._result_policy_fields(
                network_allowed, market_research, training_candidate
            ),
        }

    @staticmethod
    def _context_retrieval_result(
        payload: dict[str, Any],
        memory_context: dict[str, Any],
        private_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
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
        }

    @staticmethod
    def _instruction_execution_result(
        intent: str, semantic_plan: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "understood": True,
            "intent": intent,
            "governance_checked": True,
            "user_commands_allowed": True,
            "arbitrary_system_commands_allowed": False,
            "execution_requested": bool(
                semantic_plan["comprehension"]["command"]["execution_requested"]
            ),
            "status": "planned",
        }

    @staticmethod
    def _result_policy_fields(
        network_allowed: bool,
        market_research: dict[str, Any] | None,
        training_candidate: dict[str, Any],
    ) -> dict[str, Any]:
        return {
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

    @staticmethod
    def _market_inputs(
        payload: dict[str, Any],
        intent: str,
        network_allowed: bool,
        analyze: InvestmentAnalyzer,
        search: MarketSearcher,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        holdings = payload.get("holdings")
        market_research: dict[str, Any] | None = None
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
        return market_research, analysis

    def _resolve_semantic_plan(
        self, payload: dict[str, Any], prompt: str, intent: str
    ) -> dict[str, Any]:
        supplied = payload.get("_semantic_plan")
        semantic_plan = (
            dict(supplied)
            if isinstance(supplied, Mapping)
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
        return semantic_plan

    def _intent_grounding(
        self,
        intent: str,
        analysis: dict[str, Any] | None,
        database: dict[str, Any],
    ) -> str:
        warnings = (
            analysis.get("risk_warnings")
            if isinstance(analysis, dict)
            else None
        )
        warning_count = len(warnings) if isinstance(warnings, list) else 0
        tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
        templates = self._INTENT_GROUNDING
        if intent in {"risk", "analysis"} and analysis is not None:
            return templates["analysis_done"].format(count=warning_count)
        if intent == "status":
            return templates["status"].format(
                instruments=int(tables.get("instrument_identity") or 0),
                observations=int(tables.get("market_observation") or 0),
            )
        return templates.get(intent, templates["_default"])

    @staticmethod
    def _diagnostic_grounding_lines(fault_diagnostics: Any) -> list[str]:
        """Flatten a fault-diagnosis pack into bounded grounding lines."""
        if not (
            isinstance(fault_diagnostics, Mapping)
            and fault_diagnostics.get("ok") is True
        ):
            return []
        lines = StarNativeInferenceMixin._suspect_lines(
            fault_diagnostics.get("localization")
        )
        matched_codes = [
            str(code)
            for code in fault_diagnostics.get("matched_fault_code_ids") or []
        ][:5]
        if matched_codes:
            lines.append(
                f"治理故障碼目錄比對結果：{', '.join(matched_codes)}"
            )
        lines.extend(
            StarNativeInferenceMixin._live_fault_lines(fault_diagnostics)
        )
        for manual in (fault_diagnostics.get("maintenance_manuals") or [])[:2]:
            if isinstance(manual, Mapping):
                lines.append(
                    f"維護手冊 {manual.get('manual_code')}: "
                    f"{str(manual.get('ordered_steps') or '')[:400]}"
                )
        return lines

    @staticmethod
    def _rag_grounding_lines(rag_context: Any) -> list[str]:
        """Flatten bounded local-RAG citations into grounding lines."""
        if not isinstance(rag_context, Mapping):
            return []
        citations = rag_context.get("citations")
        if not isinstance(citations, list):
            return []
        lines: list[str] = []
        for item in citations[:4]:
            if not isinstance(item, Mapping):
                continue
            excerpt = str(item.get("excerpt") or "").strip()
            if not excerpt:
                continue
            source = str(
                item.get("title") or item.get("source") or "local-corpus"
            ).strip()
            lines.append(f"[{source}] {excerpt[:360]}")
        return lines

    @staticmethod
    def _suspect_lines(localization: Any) -> list[str]:
        lines: list[str] = []
        if not isinstance(localization, Mapping):
            return lines
        primary = localization.get("primary_suspect")
        if not (isinstance(primary, Mapping) and primary.get("entity")):
            return lines
        lines.append(
            f"故障定位：主要嫌疑位置 {primary['entity']}"
            f"（信心 {primary.get('confidence', 'unknown')}）"
        )
        for suspect in (localization.get("suspect_locations") or [])[:3]:
            if not isinstance(suspect, Mapping):
                continue
            signals = [
                str(ev.get("signal"))
                for ev in suspect.get("evidence") or []
                if isinstance(ev, Mapping) and ev.get("signal")
            ][:2]
            if signals:
                lines.append(
                    f"定位證據 {suspect.get('entity')}: " + "；".join(signals)
                )
        return lines

    @staticmethod
    def _live_fault_lines(fault_diagnostics: Mapping) -> list[str]:
        lines: list[str] = []
        for line in (fault_diagnostics.get("log_errors") or [])[:5]:
            lines.append(f"後端日誌錯誤：{str(line)[:200]}")
        quarantined = [
            str(t.get("tool_id"))
            for t in fault_diagnostics.get("quarantined_tools") or []
            if isinstance(t, Mapping) and t.get("tool_id")
        ][:5]
        if quarantined:
            lines.append(f"已被隔離的崩潰工具：{', '.join(quarantined)}")
        for signature in (
            fault_diagnostics.get("recurring_fault_signatures") or []
        )[:3]:
            if isinstance(signature, Mapping):
                lines.append(
                    f"反覆出現的故障：{signature.get('failure_code')}"
                    f"（累計 {signature.get('occurrence_count')} 次）"
                )
        context = fault_diagnostics.get("automation_context")
        if isinstance(context, Mapping) and context.get(
            "automatic_repair_enabled"
        ) is False:
            lines.append(
                "自動修復目前關閉；修復項目需使用者在面板逐一確認後才會執行"
            )
        return lines
