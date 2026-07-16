from __future__ import annotations

import ctypes
import difflib
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Sequence

from . import investment_manager_core


FUND_QUOTE_TYPES = {"MUTUALFUND", "ETF"}
LOCAL_EXPLANATION_SCHEMA_VERSION = "local-explanation.v2"
LOCAL_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "compact": {
        "label": "輕量保守",
        "minimum_memory_gb": 0,
        "max_model_bytes": 2_500_000_000,
        "num_ctx": 2048,
        "num_predict": 240,
        "timeout": 45,
    },
    "balanced": {
        "label": "平衡",
        "minimum_memory_gb": 12,
        "max_model_bytes": 6_000_000_000,
        "num_ctx": 4096,
        "num_predict": 360,
        "timeout": 75,
    },
    "quality": {
        "label": "高品質",
        "minimum_memory_gb": 24,
        "max_model_bytes": 12_000_000_000,
        "num_ctx": 6144,
        "num_predict": 480,
        "timeout": 110,
    },
}


def _normalized_name(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[\s\-_./()（）·•]+", "", text)
    for token in ("基金", "股份", "有限公司", "class", "acc", "inc", "dis"):
        text = text.replace(token, "")
    return text


def _fund_match_score(name: str, candidate: dict[str, Any]) -> float:
    expected = _normalized_name(name)
    candidate_names = [
        _normalized_name(candidate.get("longname")),
        _normalized_name(candidate.get("shortname")),
        _normalized_name(candidate.get("name")),
    ]
    ratios = [
        difflib.SequenceMatcher(None, expected, value).ratio()
        for value in candidate_names
        if expected and value
    ]
    score = max(ratios, default=0.0)
    quote_type = str(candidate.get("quoteType") or "").upper()
    if quote_type == "MUTUALFUND":
        score += 0.08
    elif quote_type == "ETF":
        score -= 0.04
    return max(0.0, min(1.0, score))


class FundIdentityResolver:
    """Resolve placeholder fund rows without replacing the user's local identity."""

    def __init__(
        self,
        request_json: Callable[[str, int], Any] | None = None,
    ) -> None:
        self.request_json = request_json or (
            lambda url, timeout: investment_manager_core.request_json(
                url,
                timeout=timeout,
            )
        )

    def search(self, holding: dict[str, Any], timeout: int = 8) -> list[dict[str, Any]]:
        name = str(holding.get("name") or holding.get("symbol") or "").strip()
        if not name:
            return []
        url = "https://query2.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode(
            {
                "q": name,
                "quotesCount": 8,
                "newsCount": 0,
                "enableFuzzyQuery": "true",
            }
        )
        payload = self.request_json(url, timeout)
        quotes = payload.get("quotes") if isinstance(payload, dict) else []
        candidates: list[dict[str, Any]] = []
        for source in quotes if isinstance(quotes, list) else []:
            if not isinstance(source, dict):
                continue
            quote_type = str(source.get("quoteType") or "").upper()
            symbol = str(source.get("symbol") or "").strip().upper()
            if not symbol or quote_type not in FUND_QUOTE_TYPES:
                continue
            score = _fund_match_score(name, source)
            candidates.append(
                {
                    "symbol": symbol,
                    "name": str(
                        source.get("longname")
                        or source.get("shortname")
                        or symbol
                    ),
                    "exchange": str(
                        source.get("exchDisp")
                        or source.get("exchange")
                        or ""
                    ),
                    "quote_type": quote_type,
                    "currency": str(source.get("currency") or "").upper(),
                    "confidence_score": round(score * 100, 2),
                    "confidence_label": (
                        "高" if score >= 0.88 else "中" if score >= 0.68 else "低"
                    ),
                    "source": "Yahoo Finance Search",
                    "source_url": url,
                }
            )
        return sorted(
            candidates,
            key=lambda item: float(item.get("confidence_score") or 0),
            reverse=True,
        )[:5]

    def resolve_holdings(
        self,
        holdings: Sequence[dict[str, Any]],
        *,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        output = [dict(item) for item in holdings]
        attempted = matched = auto_confirmed = failed = 0
        for item in output:
            is_fund = (
                str(item.get("market") or "").upper() == "FUND"
                or str(item.get("asset_type") or "").upper() == "FUND"
            )
            if not is_fund or attempted >= max(1, min(200, int(limit))):
                continue
            if str(item.get("fund_identity_status") or "") == "confirmed":
                continue
            attempted += 1
            try:
                candidates = self.search(item)
            except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError):
                candidates = []
                failed += 1
            item["fund_identity_candidates"] = candidates
            item["fund_identity_checked_at"] = datetime.now().astimezone().isoformat()
            if not candidates:
                item["fund_identity_status"] = "unresolved"
                continue
            best = candidates[0]
            matched += 1
            item["fund_candidate_symbol"] = best["symbol"]
            item["fund_identity_confidence"] = best["confidence_score"]
            item["fund_identity_source"] = best["source"]
            item["fund_identity_source_url"] = best["source_url"]
            if float(best["confidence_score"]) >= 94:
                item["fund_quote_symbol"] = best["symbol"]
                item["fund_identity_status"] = "confirmed"
                item["fund_identity_confirmation"] = "automatic_high_confidence"
                auto_confirmed += 1
            else:
                item["fund_identity_status"] = "suggested"
        return output, {
            "attempted_count": attempted,
            "matched_count": matched,
            "auto_confirmed_count": auto_confirmed,
            "unresolved_count": max(0, attempted - matched),
            "request_failed_count": failed,
            "status": "ready" if attempted else "no_funds",
            "generated_at": datetime.now().astimezone().isoformat(),
        }


def build_smart_mapping_repair(preview: dict[str, Any]) -> dict[str, Any]:
    consolidated = (
        preview.get("consolidated_layout")
        if isinstance(preview.get("consolidated_layout"), dict)
        else {}
    )
    horizontal = (
        preview.get("horizontal_layout")
        if isinstance(preview.get("horizontal_layout"), dict)
        else {}
    )
    if consolidated.get("detected"):
        count = int(consolidated.get("holding_count") or 0)
        return {
            "recommended": True,
            "layout": "consolidated_report",
            "sheet_name": str(consolidated.get("sheet_name") or ""),
            "confidence_score": 99 if count > 20 else 92,
            "confidence_label": "高",
            "reason": f"同一工作表同時辨識到基金與證券區塊，共 {count} 筆。",
            "changes": ["使用雙區塊報酬表解析器", "保留基金、台股與美股各自欄位"],
        }
    if horizontal.get("detected"):
        count = int(horizontal.get("holding_count") or 0)
        return {
            "recommended": True,
            "layout": "horizontal_matrix",
            "sheet_name": "",
            "confidence_score": 96 if count > 10 else 86,
            "confidence_label": "高" if count > 10 else "中",
            "reason": f"辨識到 {horizontal.get('sheet_count', 0)} 張橫向持股表，共 {count} 筆。",
            "changes": ["依工作表資產類別分組", "自動推定名稱、價格與數量列"],
        }

    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for sheet in preview.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        for candidate in sheet.get("header_candidates", []):
            if isinstance(candidate, dict):
                candidates.append((sheet, candidate))
    candidates.sort(key=lambda pair: int(pair[1].get("score") or 0), reverse=True)
    if not candidates:
        return {
            "recommended": False,
            "layout": "row_mapping",
            "confidence_score": 0,
            "confidence_label": "低",
            "reason": "未找到可驗證的表頭候選，請人工指定欄位。",
            "changes": [],
        }
    sheet, best = candidates[0]
    runner_up = int(candidates[1][1].get("score") or 0) if len(candidates) > 1 else 0
    score = int(best.get("score") or 0)
    gap = max(0, score - runner_up)
    valid_rows = int(best.get("valid_data_row_count") or 0)
    confidence = min(98, 55 + min(25, valid_rows) + min(18, gap // 20))
    canonical = best.get("canonical_columns") or []
    mapping = {
        str(field): index
        for index, field in enumerate(canonical)
        if str(field or "").strip()
    }
    return {
        "recommended": bool({"symbol", "quantity"}.issubset(mapping)),
        "layout": "row_mapping",
        "sheet_name": str(sheet.get("sheet_name") or ""),
        "header_row_number": best.get("header_row_number"),
        "data_start_row_number": best.get("data_start_row_number"),
        "column_mapping": mapping,
        "confidence_score": confidence,
        "confidence_label": "高" if confidence >= 85 else "中" if confidence >= 65 else "低",
        "reason": f"最佳表頭包含 {valid_rows} 筆有效持股，與次佳候選分差 {gap}。",
        "changes": ["重新選擇最可信表頭", "依欄位內容補上缺少的欄名"],
    }


class LocalExplanationEngine:
    """Facts-first explanation with a schema-bound, optional local Ollama layer."""

    def __init__(self, endpoint: str | None = None, model: str | None = None) -> None:
        del endpoint, model
        # Direct model access belongs exclusively to the independent local-ai
        # tool (星澄). Investment Manager keeps its deterministic fallback and
        # uses authenticated peer IPC for model inference.
        self.endpoint = ""
        self.enabled = False
        self.model = ""

    def explain(self, analysis: dict[str, Any], calibration: dict[str, Any]) -> dict[str, Any]:
        deterministic = self._deterministic_explanation(analysis, calibration)
        facts = self._fact_bundle(analysis, calibration)
        execution_mode = (
            analysis.get("execution_mode")
            if isinstance(analysis.get("execution_mode"), dict)
            else {}
        )
        deterministic_structured = self._deterministic_structured(
            deterministic,
            facts,
        )
        profile = self._model_profile()
        fallback = {
            "mode": "deterministic",
            "mode_label": "規則引擎說明",
            "model": "",
            "text": deterministic,
            "structured": deterministic_structured,
            "schema_version": LOCAL_EXPLANATION_SCHEMA_VERSION,
            "facts_locked": True,
            "evidence_ids": deterministic_structured["evidence_ids"],
            "counterfactuals": deterministic_structured["counterfactuals"],
            "model_profile": profile,
            "data_mode": str(
                analysis.get("data_mode")
                or execution_mode.get("id")
                or "offline"
            ),
            "simulation_only": True,
            "human_approval_required": True,
            "model_output_rejected": False,
            "model_abstained": False,
        }
        if not self.enabled:
            return {**fallback, "model_unavailable_reason": "disabled"}
        if not self._endpoint_is_local():
            return {**fallback, "model_unavailable_reason": "non_local_endpoint_blocked"}
        if not self._local_server_available():
            return {**fallback, "model_unavailable_reason": "local_server_unavailable"}

        installed_models = self._installed_models()
        selection = self._select_installed_model(installed_models, profile)
        model = str(selection.get("model") or "")
        fallback["model_selection"] = selection
        if not model:
            return {
                **fallback,
                "model_unavailable_reason": str(
                    selection.get("reason") or "no_installed_text_model"
                ),
            }

        schema = self._response_schema()
        prompt = (
            "你是完全在本機執行的投資風險解釋器。只能根據 FACTS 中的事實"
            "與 evidence id 解釋，不能新增數字、價格、報酬預測或買賣指令。"
            "每項結論與反證條件都必須引用 FACTS 內存在的 evidence id。"
            "資料不足時將 abstain 設為 true 並簡短說明；不得猜測。"
            "輸出必須完全符合 JSON_SCHEMA，不要輸出 Markdown。\n"
            f"JSON_SCHEMA={json.dumps(schema, ensure_ascii=False, sort_keys=True)}\n"
            f"FACTS={json.dumps(facts, ensure_ascii=False, sort_keys=True)}"
        )
        options = {
            "num_ctx": int(profile["num_ctx"]),
            "num_predict": int(profile["num_predict"]),
            "temperature": 0.1,
            "seed": 17,
        }
        try:
            payload = self._post_json(
                f"{self.endpoint}/api/generate",
                {
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": "5m",
                    "format": schema,
                    "options": options,
                },
                timeout=int(profile["timeout"]),
            )
            response_text = (
                str(payload.get("response") or "").strip()
                if isinstance(payload, dict)
                else ""
            )
        except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            response_text = ""

        candidate = self._parse_model_json(response_text)
        accepted, rejection_reason = self._validate_model_payload(
            candidate,
            facts,
        )
        if not accepted:
            return {
                **fallback,
                "model_output_rejected": bool(response_text),
                "model_rejection_reason": rejection_reason,
                "model_selection": selection,
            }
        if bool(candidate.get("abstain")):
            return {
                **fallback,
                "mode_label": "規則引擎說明（本機模型拒答）",
                "model": model,
                "model_abstained": True,
                "abstention_reason": str(candidate.get("abstention_reason") or ""),
                "model_response": candidate,
                "model_selection": selection,
            }

        counterfactuals = list(candidate.get("counterfactuals") or [])
        text = self._render_model_explanation(candidate)
        return {
            **fallback,
            "mode": "hybrid",
            "mode_label": "規則引擎 + 本機結構化解釋",
            "model": model,
            "text": text,
            "structured": candidate,
            "evidence_ids": list(candidate.get("evidence_ids") or []),
            "counterfactuals": counterfactuals,
            "facts_locked": True,
            "model_selection": selection,
        }

    @staticmethod
    def _deterministic_explanation(
        analysis: dict[str, Any],
        calibration: dict[str, Any],
    ) -> str:
        command = analysis.get("command_result") if isinstance(analysis.get("command_result"), dict) else {}
        summary = analysis.get("summary") if isinstance(analysis.get("summary"), dict) else {}
        confidence = command.get("confidence_summary") if isinstance(command.get("confidence_summary"), dict) else {}
        return (
            f"風險摘要：{command.get('decision_brief') or '維持監測'} "
            f"可行動警示 {summary.get('warning_count', 0)} 項，重大 {summary.get('critical_count', 0)} 項。 "
            f"資料信心 {confidence.get('label') or '-'}；校準樣本 {calibration.get('evaluated_count', 0)} 筆。 "
            "所有結果僅供輔助，請人工確認資料與部位上限。"
        )

    @staticmethod
    def _model_text_is_safe(text: str, deterministic: str) -> bool:
        if not text or len(text) > 600:
            return False
        generated_numbers = set(re.findall(r"[+-]?\d+(?:\.\d+)?", text))
        fact_numbers = set(re.findall(r"[+-]?\d+(?:\.\d+)?", deterministic))
        if not generated_numbers.issubset(fact_numbers):
            return False
        required_terms = ("風險", "資料信心", "人工", "確認")
        if any(term not in text for term in required_terms):
            return False
        unsupported_claims = (
            "保證",
            "一定",
            "預期",
            "經驗表明",
            "市場表現",
            "將會上漲",
            "將會下跌",
            "建議買進",
            "建議賣出",
            "立即加碼",
            "立即減碼",
        )
        return not any(
            claim in text and claim not in deterministic
            for claim in unsupported_claims
        )

    @staticmethod
    def _normalized_copy(value: str) -> str:
        return re.sub(
            r"[\s`*_#，。；：,.!?！？]+",
            "",
            str(value or ""),
        ).strip()

    @staticmethod
    def _fact_bundle(
        analysis: dict[str, Any],
        calibration: dict[str, Any],
    ) -> dict[str, Any]:
        summary = (
            analysis.get("summary")
            if isinstance(analysis.get("summary"), dict)
            else {}
        )
        product = (
            analysis.get("product_status")
            if isinstance(analysis.get("product_status"), dict)
            else {}
        )
        command = (
            analysis.get("command_result")
            if isinstance(analysis.get("command_result"), dict)
            else {}
        )
        assessment = (
            analysis.get("local_ai_assessment")
            if isinstance(analysis.get("local_ai_assessment"), dict)
            else {}
        )
        confidence = (
            command.get("confidence_summary")
            if isinstance(command.get("confidence_summary"), dict)
            else {}
        )
        execution_mode = (
            analysis.get("execution_mode")
            if isinstance(analysis.get("execution_mode"), dict)
            else {}
        )
        evidence: list[dict[str, Any]] = [
            {
                "id": "portfolio:state",
                "source": "local_rules",
                "field": "state_label",
                "value": str(product.get("state_label") or "維持監測"),
            },
            {
                "id": "portfolio:warning_count",
                "source": "local_rules",
                "field": "warning_count",
                "value": int(summary.get("warning_count") or 0),
            },
            {
                "id": "portfolio:critical_count",
                "source": "local_rules",
                "field": "critical_count",
                "value": int(summary.get("critical_count") or 0),
            },
            {
                "id": "assessment:data_confidence",
                "source": "local_rules",
                "field": "confidence_label",
                "value": str(confidence.get("label") or "-"),
            },
            {
                "id": "assessment:score_coverage",
                "source": "local_rules",
                "field": "score_coverage_percent",
                "value": assessment.get("score_coverage_percent"),
            },
            {
                "id": "calibration:evaluated_count",
                "source": "local_calibration",
                "field": "evaluated_count",
                "value": int(calibration.get("evaluated_count") or 0),
            },
        ]
        seen_ids = {str(item["id"]) for item in evidence}
        analytics_evidence = (
            analysis.get("analytics_evidence")
            if isinstance(analysis.get("analytics_evidence"), list)
            else []
        )
        for candidate in analytics_evidence:
            if not isinstance(candidate, dict):
                continue
            evidence_id = str(candidate.get("id") or "")
            if not evidence_id or evidence_id in seen_ids:
                continue
            seen_ids.add(evidence_id)
            evidence.append(
                {
                    "id": evidence_id,
                    "source": str(candidate.get("source") or "local_analytics"),
                    "field": str(candidate.get("field") or ""),
                    "value": candidate.get("value"),
                    "as_of": str(candidate.get("as_of") or ""),
                    "symbol": str(candidate.get("symbol") or ""),
                    "section": str(candidate.get("section") or ""),
                }
            )
            if len(evidence) >= 80:
                break
        assessments = (
            assessment.get("assessments")
            if isinstance(assessment.get("assessments"), list)
            else []
        )
        for item in assessments:
            if len(evidence) >= 80:
                break
            if not isinstance(item, dict):
                continue
            for candidate in item.get("evidence", []):
                if not isinstance(candidate, dict):
                    continue
                evidence_id = str(candidate.get("id") or "")
                if not evidence_id or evidence_id in seen_ids:
                    continue
                seen_ids.add(evidence_id)
                evidence.append(
                    {
                        "id": evidence_id,
                        "source": str(candidate.get("source") or "local"),
                        "field": str(candidate.get("field") or ""),
                        "value": candidate.get("value"),
                        "as_of": str(candidate.get("as_of") or ""),
                    }
                )
                if len(evidence) >= 80:
                    break
            if len(evidence) >= 80:
                break
        return {
            "portfolio_state": str(product.get("state_label") or "維持監測"),
            "warning_count": int(summary.get("warning_count") or 0),
            "critical_count": int(summary.get("critical_count") or 0),
            "data_confidence": str(confidence.get("label") or "-"),
            "calibration_sample_count": int(calibration.get("evaluated_count") or 0),
            "score_type": str(
                assessment.get("score_type") or "heuristic_risk_proxy"
            ),
            "score_coverage_percent": assessment.get("score_coverage_percent"),
            "decision_brief": str(command.get("decision_brief") or "維持監測"),
            "data_mode": str(
                analysis.get("data_mode")
                or execution_mode.get("id")
                or "offline"
            ),
            "analytics_context": (
                analysis.get("analytics_context")
                if isinstance(analysis.get("analytics_context"), dict)
                else {}
            ),
            "evidence": evidence,
            "constraints": {
                "simulation_only": True,
                "human_approval_required": True,
                "return_forecast_allowed": False,
                "autonomous_order_allowed": False,
            },
        }

    @staticmethod
    def _deterministic_structured(
        deterministic: str,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        evidence_ids = [
            "portfolio:state",
            "portfolio:warning_count",
            "portfolio:critical_count",
            "assessment:data_confidence",
            "calibration:evaluated_count",
        ]
        if facts.get("score_coverage_percent") is not None:
            evidence_ids.append("assessment:score_coverage")
        analytics_ids = [
            str(item.get("id") or "")
            for item in facts.get("evidence", [])
            if isinstance(item, dict)
            and str(item.get("id") or "").startswith("analytics:")
        ]
        counterfactual_ids = (
            analytics_ids[:3]
            if analytics_ids
            else ["portfolio:warning_count", "assessment:data_confidence"]
        )
        return {
            "summary": deterministic,
            "evidence_ids": evidence_ids,
            "counterfactuals": [
                {
                    "condition": (
                        "若分析脈絡中的風險、事件、決策日誌、帳本摘要或政策改變"
                        if analytics_ids
                        else "若持倉、公開報價、風險警示或資料覆蓋改變"
                    ),
                    "effect": "重新執行本地規則評估並由人工確認",
                    "evidence_ids": counterfactual_ids,
                }
            ],
            "abstain": False,
            "abstention_reason": "",
        }

    @staticmethod
    def _response_schema() -> dict[str, Any]:
        evidence_id_array = {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
            "maxItems": 20,
            "uniqueItems": True,
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary",
                "evidence_ids",
                "counterfactuals",
                "abstain",
                "abstention_reason",
            ],
            "properties": {
                "summary": {"type": "string", "maxLength": 600},
                "evidence_ids": evidence_id_array,
                "counterfactuals": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["condition", "effect", "evidence_ids"],
                        "properties": {
                            "condition": {"type": "string", "maxLength": 240},
                            "effect": {"type": "string", "maxLength": 240},
                            "evidence_ids": evidence_id_array,
                        },
                    },
                },
                "abstain": {"type": "boolean"},
                "abstention_reason": {"type": "string", "maxLength": 240},
            },
        }

    @staticmethod
    def _parse_model_json(text: str) -> dict[str, Any] | None:
        candidate = str(text or "").strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
            candidate = re.sub(r"\s*```$", "", candidate)
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def _validate_model_payload(
        cls,
        candidate: dict[str, Any] | None,
        facts: dict[str, Any],
    ) -> tuple[bool, str]:
        if not isinstance(candidate, dict):
            return False, "invalid_json"
        required_keys = {
            "summary",
            "evidence_ids",
            "counterfactuals",
            "abstain",
            "abstention_reason",
        }
        if set(candidate) != required_keys:
            return False, "schema_keys_mismatch"
        if not isinstance(candidate.get("abstain"), bool):
            return False, "invalid_abstain"
        if not isinstance(candidate.get("summary"), str):
            return False, "invalid_summary"
        if not isinstance(candidate.get("abstention_reason"), str):
            return False, "invalid_abstention_reason"
        if len(candidate["summary"]) > 600 or len(candidate["abstention_reason"]) > 240:
            return False, "text_too_long"
        if not isinstance(candidate.get("evidence_ids"), list):
            return False, "invalid_evidence_ids"
        if not isinstance(candidate.get("counterfactuals"), list):
            return False, "invalid_counterfactuals"
        if len(candidate["counterfactuals"]) > 3:
            return False, "too_many_counterfactuals"

        allowed_evidence_ids = {
            str(item.get("id") or "")
            for item in facts.get("evidence", [])
            if isinstance(item, dict) and item.get("id")
        }
        cited_ids = candidate["evidence_ids"]
        if any(not isinstance(item, str) for item in cited_ids):
            return False, "invalid_evidence_id_type"
        if len(cited_ids) != len(set(cited_ids)):
            return False, "duplicate_evidence_id"
        if not set(cited_ids).issubset(allowed_evidence_ids):
            return False, "unknown_evidence_id"

        combined_text = [candidate["summary"], candidate["abstention_reason"]]
        for counterfactual in candidate["counterfactuals"]:
            if not isinstance(counterfactual, dict) or set(counterfactual) != {
                "condition",
                "effect",
                "evidence_ids",
            }:
                return False, "invalid_counterfactual_schema"
            condition = counterfactual.get("condition")
            effect = counterfactual.get("effect")
            counter_ids = counterfactual.get("evidence_ids")
            if not isinstance(condition, str) or not isinstance(effect, str):
                return False, "invalid_counterfactual_text"
            if len(condition) > 240 or len(effect) > 240:
                return False, "counterfactual_too_long"
            if not isinstance(counter_ids, list) or not counter_ids:
                return False, "counterfactual_missing_evidence"
            if any(not isinstance(item, str) for item in counter_ids):
                return False, "invalid_counterfactual_evidence"
            if not set(counter_ids).issubset(allowed_evidence_ids):
                return False, "unknown_counterfactual_evidence"
            combined_text.extend([condition, effect])

        if not cls._text_uses_only_cited_numbers(
            candidate["summary"],
            cited_ids,
            facts,
        ):
            return False, "number_not_supported_by_evidence"
        if not cls._text_uses_only_cited_numbers(
            candidate["abstention_reason"],
            cited_ids,
            facts,
        ):
            return False, "number_not_supported_by_evidence"
        for counterfactual in candidate["counterfactuals"]:
            if not cls._text_uses_only_cited_numbers(
                str(counterfactual.get("condition") or "")
                + " "
                + str(counterfactual.get("effect") or ""),
                counterfactual.get("evidence_ids") or [],
                facts,
            ):
                return False, "number_not_supported_by_evidence"
        if any(cls._contains_unsupported_claim(text) for text in combined_text):
            return False, "unsupported_claim"

        if candidate["abstain"]:
            if not candidate["abstention_reason"].strip():
                return False, "missing_abstention_reason"
            return True, ""
        if not cited_ids:
            return False, "missing_evidence"
        if not cls._model_summary_is_safe(candidate["summary"]):
            return False, "unsafe_summary"
        return True, ""

    @classmethod
    def _model_summary_is_safe(cls, text: str) -> bool:
        if not text or len(text) > 600:
            return False
        required_terms = ("風險", "資料信心", "人工", "確認")
        return (
            all(term in text for term in required_terms)
            and not cls._contains_unsupported_claim(text)
        )

    @staticmethod
    def _text_uses_only_cited_numbers(
        text: str,
        evidence_ids: Sequence[str],
        facts: dict[str, Any],
    ) -> bool:
        generated_numbers = set(
            re.findall(r"[+-]?\d+(?:\.\d+)?", str(text or ""))
        )
        if not generated_numbers:
            return True
        cited = {
            str(evidence_id)
            for evidence_id in evidence_ids
            if isinstance(evidence_id, str)
        }
        cited_evidence = [
            item
            for item in facts.get("evidence", [])
            if isinstance(item, dict) and str(item.get("id") or "") in cited
        ]
        evidence_numbers = set(
            re.findall(
                r"[+-]?\d+(?:\.\d+)?",
                json.dumps(cited_evidence, ensure_ascii=False, sort_keys=True),
            )
        )
        return generated_numbers.issubset(evidence_numbers)

    @staticmethod
    def _contains_unsupported_claim(text: str) -> bool:
        return any(
            claim in str(text or "")
            for claim in (
                "保證",
                "一定上漲",
                "一定下跌",
                "將會上漲",
                "將會下跌",
                "建議買進",
                "建議賣出",
                "立即加碼",
                "立即減碼",
                "自動下單",
            )
        )

    @staticmethod
    def _render_model_explanation(candidate: dict[str, Any]) -> str:
        lines = [str(candidate.get("summary") or "").strip()]
        counterfactuals = candidate.get("counterfactuals")
        if isinstance(counterfactuals, list) and counterfactuals:
            lines.append("反證條件：")
            for item in counterfactuals:
                if not isinstance(item, dict):
                    continue
                evidence = "、".join(str(value) for value in item.get("evidence_ids", []))
                lines.append(
                    f"- {item.get('condition')}；{item.get('effect')}。"
                    f"〔證據：{evidence}〕"
                )
        lines.append("所有結果僅供模擬與輔助，任何動作都需要人工核准。")
        return "\n".join(line for line in lines if line)

    def _endpoint_is_local(self) -> bool:
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = str(parsed.hostname or "").strip().casefold()
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _model_profile(self) -> dict[str, Any]:
        hardware = self._detect_hardware()
        configured = str(
            os.environ.get("GPTBRIDGE_LOCAL_LLM_PROFILE") or "auto"
        ).strip().casefold()
        if configured not in {*LOCAL_MODEL_PROFILES, "auto"}:
            configured = "auto"
        memory_gb = hardware.get("memory_gb")
        cpu_count = int(hardware.get("cpu_count") or 1)
        if configured == "auto":
            if isinstance(memory_gb, (int, float)) and memory_gb >= 24 and cpu_count >= 8:
                selected = "quality"
            elif isinstance(memory_gb, (int, float)) and memory_gb >= 12 and cpu_count >= 6:
                selected = "balanced"
            else:
                selected = "compact"
        else:
            selected = configured
        profile = dict(LOCAL_MODEL_PROFILES[selected])
        profile.update(
            {
                "id": selected,
                "configured": configured,
                "cpu_count": cpu_count,
                "memory_gb": memory_gb,
                "no_download": True,
                "selection_policy": "installed_models_only",
            }
        )
        profile["num_ctx"] = self._bounded_env_int(
            "GPTBRIDGE_LOCAL_LLM_NUM_CTX",
            int(profile["num_ctx"]),
            512,
            32768,
        )
        profile["num_predict"] = self._bounded_env_int(
            "GPTBRIDGE_LOCAL_LLM_NUM_PREDICT",
            int(profile["num_predict"]),
            64,
            1024,
        )
        return profile

    @staticmethod
    def _bounded_env_int(name: str, default: int, lower: int, upper: int) -> int:
        try:
            value = int(str(os.environ.get(name) or default))
        except (TypeError, ValueError):
            value = default
        return max(lower, min(upper, value))

    @staticmethod
    def _detect_hardware() -> dict[str, Any]:
        memory_gb: float | None = None
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            try:
                status = MemoryStatus()
                status.length = ctypes.sizeof(MemoryStatus)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    memory_gb = round(status.total_physical / (1024**3), 2)
            except (AttributeError, OSError, TypeError):
                memory_gb = None
        else:
            try:
                pages = int(os.sysconf("SC_PHYS_PAGES"))
                page_size = int(os.sysconf("SC_PAGE_SIZE"))
                memory_gb = round(pages * page_size / (1024**3), 2)
            except (AttributeError, OSError, TypeError, ValueError):
                memory_gb = None
        return {
            "cpu_count": max(1, int(os.cpu_count() or 1)),
            "memory_gb": memory_gb,
        }

    def _installed_models(self) -> list[dict[str, Any]]:
        if not self._endpoint_is_local():
            return []
        try:
            request = urllib.request.Request(f"{self.endpoint}/api/tags")
            with urllib.request.urlopen(request, timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            return []
        models = payload.get("models") if isinstance(payload, dict) else []
        return [dict(item) for item in models if isinstance(item, dict)]

    def _select_installed_model(
        self,
        models: Sequence[dict[str, Any]],
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        candidates = [
            {
                "name": str(item.get("name") or item.get("model") or "").strip(),
                "size": int(item.get("size") or 0),
            }
            for item in models
            if str(item.get("name") or item.get("model") or "").strip()
            and not any(
                token in str(item.get("name") or item.get("model") or "").casefold()
                for token in ("embed", "embedding", "rerank")
            )
        ]
        requested = self.model
        if requested:
            exact = next(
                (
                    item
                    for item in candidates
                    if item["name"].casefold() == requested.casefold()
                ),
                None,
            )
            if exact:
                return {
                    "model": exact["name"],
                    "source": "configured_installed",
                    "requested_model": requested,
                    "requested_model_available": True,
                    "download_attempted": False,
                }
        if not candidates:
            return {
                "model": "",
                "source": "none",
                "requested_model": requested,
                "requested_model_available": False if requested else None,
                "download_attempted": False,
                "reason": "no_installed_text_model",
            }
        maximum = int(profile.get("max_model_bytes") or 0)
        fitting = [
            item
            for item in candidates
            if item["size"] <= 0 or maximum <= 0 or item["size"] <= maximum
        ]
        if fitting:
            known_size = [item for item in fitting if item["size"] > 0]
            if known_size:
                chosen = (
                    min(known_size, key=lambda item: item["size"])
                    if profile.get("id") == "compact"
                    else max(known_size, key=lambda item: item["size"])
                )
            else:
                chosen = sorted(fitting, key=lambda item: item["name"].casefold())[0]
            source = "profile_match"
        else:
            chosen = min(
                candidates,
                key=lambda item: item["size"] if item["size"] > 0 else 2**63,
            )
            source = "conservative_smallest_fallback"
        return {
            "model": chosen["name"],
            "source": source,
            "requested_model": requested,
            "requested_model_available": False if requested else None,
            "download_attempted": False,
        }

    def _local_server_available(self) -> bool:
        if not self.enabled or not self._endpoint_is_local():
            return False
        parsed = urllib.parse.urlparse(self.endpoint)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            return False

    def _first_model(self) -> str:
        selection = self._select_installed_model(
            self._installed_models(),
            self._model_profile(),
        )
        return str(selection.get("model") or "")

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], timeout: int) -> Any:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
