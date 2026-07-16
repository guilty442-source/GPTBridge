"""Standalone local risk AI engine.

This module is intentionally deterministic: it does not call ChatGPT, Gemini,
Claude, or any browser session. Live mode only uses quote providers from the
investment manager core; offline mode evaluates portfolio and cost data only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import investment_manager_core as core


QuoteFunction = Callable[
    [core.Holding, dict[str, Any], list[str], datetime],
    tuple[core.Quote | None, list[core.QuoteAttempt]],
]

SEVERITY_LABELS = {
    "critical": "重大",
    "warning": "注意",
    "info": "提示",
}
SEVERITY_ORDER = {
    "critical": 0,
    "warning": 1,
    "info": 2,
}
ANALYSIS_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "基本面",
        "weight": 30,
        "criteria": "營收、EPS、毛利率、營業利益率、ROE、ROA、自由現金流、負債比率、現金流、股本結構",
    },
    {
        "name": "產業分析",
        "weight": 15,
        "criteria": "市場規模、產業成長率、競爭優勢、護城河、技術領先程度",
    },
    {
        "name": "總體經濟",
        "weight": 10,
        "criteria": "利率、通膨、GDP、匯率、政策、景氣循環",
    },
    {
        "name": "技術分析",
        "weight": 15,
        "criteria": "趨勢、均線、成交量、型態、支撐壓力、RSI、MACD、KD、布林通道",
    },
    {
        "name": "籌碼分析",
        "weight": 10,
        "criteria": "法人買賣、外資、投信、自營商、融資融券、大股東持股",
    },
    {
        "name": "估值分析",
        "weight": 10,
        "criteria": "本益比（P/E）、股價淨值比（P/B）、企業價值倍數（EV/EBITDA）、自由現金流折現（DCF）等",
    },
    {
        "name": "事件分析",
        "weight": 5,
        "criteria": "財報、法說會、新產品、併購、政策、重大新聞",
    },
    {
        "name": "風險分析",
        "weight": 5,
        "criteria": "法規、匯率、供應鏈、地緣政治、流動性等",
    },
)
ANALYSIS_WORKFLOW: tuple[str, ...] = (
    "公司與產業定位",
    "最新財報分析",
    "營收與獲利趨勢",
    "現金流與資本支出",
    "管理層執行能力",
    "競爭優勢（護城河）",
    "產業景氣循環",
    "同業比較",
    "估值是否合理",
    "技術面是否支持",
    "籌碼是否偏多",
    "風險與催化因素",
    "建立投資情境（樂觀／基準／悲觀）",
    "給出操作策略",
)
SCORE_MODEL: tuple[dict[str, Any], ...] = (
    {"name": "基本面", "max_score": 30},
    {"name": "成長性", "max_score": 15},
    {"name": "獲利能力", "max_score": 10},
    {"name": "財務安全", "max_score": 10},
    {"name": "技術面", "max_score": 10},
    {"name": "籌碼", "max_score": 10},
    {"name": "估值", "max_score": 10},
    {"name": "風險", "max_score": 5},
)
RESEARCH_COMPONENT_INPUTS: dict[str, tuple[str, str]] = {
    "基本面": ("fundamentals", "需要財報、現金流與營運品質資料"),
    "成長性": ("growth", "需要營收與 EPS 時序資料"),
    "獲利能力": ("profitability", "需要毛利率、營益率與資本報酬資料"),
    "財務安全": ("financial_safety", "需要資產負債表、償債與現金流資料"),
    "籌碼": ("positioning", "需要法人、融資融券或持股結構資料"),
    "估值": ("valuation", "需要估值倍數、同業比較或現金流估值資料"),
}
LOCAL_RISK_MODEL_VERSION = "heuristic-risk-proxy-v2"
ANALYTICS_CONTEXT_SECTIONS = (
    "risk",
    "events",
    "decision_journal",
    "ledger_summary",
    "policy",
)
ANALYTICS_CONTEXT_MAX_EVIDENCE = 80
ANALYTICS_CONTEXT_SENSITIVE_TOKENS = {
    "password",
    "passphrase",
    "secret",
    "token",
    "authorization",
    "credential",
    "private_key",
    "api_key",
}
RATING_STANDARDS: tuple[dict[str, str], ...] = (
    {"range": "90-100", "rating": "★★★★★", "label": "高度看好"},
    {"range": "80-89", "rating": "★★★★☆", "label": "偏多"},
    {"range": "70-79", "rating": "★★★☆☆", "label": "中性偏多"},
    {"range": "60-69", "rating": "★★☆☆☆", "label": "觀望"},
    {"range": "60 以下", "rating": "★☆☆☆☆", "label": "高風險"},
)
RISK_PROXY_STANDARDS: tuple[dict[str, str], ...] = (
    {"range": "90-100", "rating": "風險代理", "label": "本地風險訊號很低"},
    {"range": "80-89", "rating": "風險代理", "label": "本地風險訊號較低"},
    {"range": "70-79", "rating": "風險代理", "label": "本地風險訊號中等"},
    {"range": "60-69", "rating": "風險代理", "label": "本地風險訊號偏高"},
    {"range": "60 以下", "rating": "風險代理", "label": "本地風險訊號很高"},
)
RISK_CONTROL_PRINCIPLES: tuple[str, ...] = (
    "單一持股不宜過度集中。",
    "建立進出場計畫，而非一次性重押。",
    "分析會同時提出可能推翻投資假設的情境。",
    "區分短線（技術面）、中線（獲利成長）、長線（企業價值）的依據，不混用判斷標準。",
    "新資訊（財報、法說會、政策、重大事件）出現時，重新評估原本的投資假設。",
)
FRAMEWORK_APPLICABILITY = (
    "適用於台股、美股、ETF 與多數成熟市場；若分析高波動標的"
    "（如加密貨幣或小型股），提高事件風險與資金管理權重。"
)
CHIEF_BASELINE_WEIGHTS: tuple[dict[str, Any], ...] = (
    {
        "name": "企業基本面",
        "weight": 40,
        "philosophy": "長線基石；股價最終回歸盈餘，專注自由現金流、營業利益率與護城河，決定該買什麼。",
    },
    {
        "name": "資產配置與風控",
        "weight": 30,
        "philosophy": "生存防線；決定股債比例、現金水位與單一標的曝險上限。",
    },
    {
        "name": "總體經濟",
        "weight": 20,
        "philosophy": "環境風向；利率、通膨與資金成本決定估值天花板。",
    },
    {
        "name": "動能與趨勢",
        "weight": 10,
        "philosophy": "戰術微調；技術面與籌碼面只用於優化進出場時機。",
    },
)
DEFENSIVE_WEIGHTING = {
    "profile": "高估值防禦模式",
    "trigger": "高估值、高位階、通膨反撲、總經變數增加、單一持股集中或高波動標的。",
    "weights": (
        {
            "name": "企業基本面",
            "weight": 40,
            "adjustment": "維持 40%，但提高質檢標準，剔除只看未來夢想且尚未獲利的標的。",
        },
        {
            "name": "資產配置與風控",
            "weight": 45,
            "adjustment": "由 30% 提升至 45%，優先控管下行風險、現金水位、股債配置與單一標的曝險。",
        },
        {
            "name": "總體經濟",
            "weight": 10,
            "adjustment": "由 20% 下調至 10%，只確認大趨勢未崩盤，避免被月度數據噪音牽動。",
        },
        {
            "name": "動能與趨勢",
            "weight": 5,
            "adjustment": "由 10% 下調至 5%，只用於停損底線，不作為追高進場訊號。",
        },
    ),
}


@dataclass(frozen=True)
class RiskRuleConfig:
    warning_loss_percent: float = -5.0
    critical_loss_percent: float = -10.0
    intraday_drop_percent: float = -3.0
    large_gain_percent: float = 20.0
    concentration_warning_percent: float = 25.0
    concentration_critical_percent: float = 40.0
    stale_portfolio_hours: float = 72.0


QUOTE_DIVERGENCE_WARNING_PERCENT = 1.0
QUOTE_DIVERGENCE_CRITICAL_PERCENT = 3.0
QUOTE_OPEN_STALE_MINUTES = 15.0
QUOTE_CLOSED_STALE_HOURS = 48.0
QUOTE_VALIDATION_SAMPLE_SIZE = 3
MARKET_EXPECTED_CURRENCY = {
    "US": "USD",
    "TW": "TWD",
    "HK": "HKD",
}


@dataclass(frozen=True)
class LocalRiskCommand:
    instruction: str = ""
    live_quotes: bool | None = None
    symbols: tuple[str, ...] = ()
    config: RiskRuleConfig = RiskRuleConfig()
    notes: tuple[str, ...] = ()
    intent: str = "monitor"
    sections: tuple[str, ...] = ()


def analyze_state(
    state: dict[str, Any],
    *,
    live_quotes: bool = True,
    provider_order: Sequence[str] | None = None,
    now: datetime | None = None,
    quote_function: QuoteFunction | None = None,
    config: RiskRuleConfig = RiskRuleConfig(),
    instruction: str = "",
    previous_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_holdings = [
        item for item in state.get("holdings", []) if isinstance(item, dict)
    ]
    holdings = [holding_from_dict(item) for item in raw_holdings]
    research_inputs = {
        str(item.get("symbol") or "").strip().upper(): _research_inputs_from_holding(item)
        for item in raw_holdings
        if str(item.get("symbol") or "").strip()
    }
    analytics_context = (
        state.get("analytics_context")
        if isinstance(state.get("analytics_context"), dict)
        else {}
    )
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else None
    return analyze_holdings(
        holdings,
        portfolio=portfolio,
        live_quotes=live_quotes,
        provider_order=provider_order,
        now=now,
        quote_function=quote_function,
        config=config,
        instruction=instruction,
        previous_analysis=(
            previous_analysis
            if isinstance(previous_analysis, dict)
            else state.get("local_ai_analysis_cache")
            if isinstance(state.get("local_ai_analysis_cache"), dict)
            else None
        ),
        research_inputs=research_inputs,
        analytics_context=analytics_context,
    )


def analyze_portfolio_file(
    portfolio_file: Path,
    *,
    live_quotes: bool = True,
    provider_order: Sequence[str] | None = None,
    now: datetime | None = None,
    quote_function: QuoteFunction | None = None,
    config: RiskRuleConfig = RiskRuleConfig(),
    instruction: str = "",
) -> dict[str, Any]:
    snapshot = core.create_portfolio_file_snapshot(
        portfolio_file,
        Path(tempfile.gettempdir()) / "gptbridge-ai-investment-imports",
        keep=core.DEFAULT_IMPORT_SNAPSHOT_KEEP,
    )
    holdings = core.load_portfolio(snapshot)
    result = analyze_holdings(
        holdings,
        portfolio={
            "source_path": str(portfolio_file),
            "file_name": portfolio_file.name,
            "holding_count": len(holdings),
            "retained_snapshot_path": str(snapshot),
            "snapshot_retention": "append_only",
        },
        live_quotes=live_quotes,
        provider_order=provider_order,
        now=now,
        quote_function=quote_function,
        config=config,
        instruction=instruction,
    )
    result["retained_snapshot_path"] = str(snapshot)
    result["snapshot_retention"] = "append_only"
    return result


def analyze_holdings(
    holdings: Sequence[core.Holding],
    *,
    portfolio: dict[str, Any] | None = None,
    live_quotes: bool = True,
    provider_order: Sequence[str] | None = None,
    now: datetime | None = None,
    quote_function: QuoteFunction | None = None,
    config: RiskRuleConfig = RiskRuleConfig(),
    instruction: str = "",
    previous_analysis: dict[str, Any] | None = None,
    research_inputs: dict[str, dict[str, Any]] | None = None,
    analytics_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checked_at = now or core.local_device_now()
    command = parse_command(instruction, config)
    effective_live_quotes = live_quotes if command.live_quotes is None else command.live_quotes
    effective_holdings = filter_holdings_by_command(holdings, command)
    order = list(provider_order or core.DEFAULT_PROVIDER_ORDER)
    providers = core.provider_registry() if effective_live_quotes else {}
    framework = analysis_framework(effective_holdings, command)
    analytics_evidence = build_analytics_context_evidence(
        analytics_context or {},
        symbols=[holding.symbol for holding in effective_holdings],
    )
    cached_reports = {
        str(item.get("analysis_fingerprint") or ""): item
        for item in (previous_analysis or {}).get("holdings", [])
        if isinstance(item, dict) and str(item.get("analysis_fingerprint") or "")
    }

    def analyze_holding(holding: core.Holding) -> dict[str, Any]:
        supplemental = dict(
            (research_inputs or {}).get(holding.symbol.strip().upper()) or {}
        )
        fingerprint = holding_analysis_fingerprint(holding, supplemental)
        cached = cached_reports.get(fingerprint)
        if cached and cached_report_is_reusable(cached, checked_at, effective_live_quotes):
            report = dict(cached)
            report["analysis_mode"] = "reused"
            report["analysis_fingerprint"] = fingerprint
            return report
        if effective_live_quotes:
            quote, attempts, quote_candidates = quote_holding_with_candidates(
                holding,
                providers,
                order,
                checked_at,
                quote_function,
            )
        else:
            quote = None
            quote_candidates = []
            attempts = [
                core.QuoteAttempt(
                    provider="local-offline",
                    ok=False,
                    message="Live quote disabled for local offline mode.",
                )
            ]
        report = core.holding_to_report(holding, quote, attempts, checked_at)
        report["research_inputs"] = supplemental
        attach_quote_validation(report, holding, quote_candidates, checked_at)
        report["analysis_mode"] = "refreshed"
        report["analysis_fingerprint"] = fingerprint
        return report

    if effective_live_quotes and len(effective_holdings) > 4:
        worker_count = min(8, len(effective_holdings))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            reports = list(executor.map(analyze_holding, effective_holdings))
    else:
        reports = [analyze_holding(holding) for holding in effective_holdings]

    for report in reports:
        attach_data_source_confidence(report, effective_live_quotes)
    warnings = consolidate_risk_warnings(
        evaluate_risk_warnings(reports, portfolio, checked_at, command.config)
    )
    summary = build_summary(reports, warnings)
    summary["reused_holding_count"] = sum(
        1 for report in reports if report.get("analysis_mode") == "reused"
    )
    summary["refreshed_holding_count"] = sum(
        1 for report in reports if report.get("analysis_mode") != "reused"
    )
    summary["source_confidence"] = build_source_confidence_summary(reports)
    network_context = build_network_context(reports, summary, effective_live_quotes)
    local_ai_assessment = build_local_ai_assessment(
        reports,
        warnings,
        framework,
        command,
        effective_live_quotes,
        analytics_evidence,
    )
    command_result = build_command_result(
        reports,
        warnings,
        summary,
        framework,
        command,
        effective_live_quotes,
        local_ai_assessment,
    )
    product_status = build_product_status(
        reports,
        warnings,
        summary,
        portfolio,
        framework,
        command,
        effective_live_quotes,
        local_ai_assessment,
        checked_at,
    )
    content = render_content(
        reports,
        warnings,
        portfolio,
        checked_at,
        summary,
        live_quotes=effective_live_quotes,
        command=command,
        framework=framework,
        command_result=command_result,
        local_ai_assessment=local_ai_assessment,
        product_status=product_status,
    )
    execution_mode = build_execution_mode(effective_live_quotes)
    analytics_context_status = {
        "provided": bool(analytics_context),
        "accepted_sections": sorted(
            {
                str(item.get("section") or "")
                for item in analytics_evidence
                if str(item.get("section") or "")
            }
        ),
        "evidence_count": len(analytics_evidence),
        "evidence_ids": [
            str(item.get("id") or "")
            for item in analytics_evidence
            if str(item.get("id") or "")
        ],
        "bounded": True,
        "max_evidence": ANALYTICS_CONTEXT_MAX_EVIDENCE,
        "used_for_scoring": False,
        "used_for_explanation": bool(analytics_evidence),
    }
    return {
        "mode": "local-live" if effective_live_quotes else "local-offline",
        "data_mode": execution_mode["id"],
        "execution_mode": execution_mode,
        "analytics_context": analytics_context_status,
        "analytics_evidence": analytics_evidence,
        "model_version": LOCAL_RISK_MODEL_VERSION,
        "simulation_only": True,
        "human_approval_required": True,
        "generated_at": checked_at.isoformat(),
        "instruction": command.instruction,
        "command": command_to_dict(command),
        "analysis_framework": framework,
        "portfolio": portfolio,
        "summary": summary,
        "network_context": network_context,
        "local_ai_assessment": local_ai_assessment,
        "command_result": command_result,
        "product_status": product_status,
        "assistant_ready": product_status.get("state") != "empty",
        "holdings": reports,
        "risk_warnings": warnings,
        "content": content,
    }


COMMAND_INTENT_LABELS = {
    "monitor": "持倉監測",
    "risk": "風險預告",
    "allocation": "配置檢查",
    "score": "評分模型",
    "plan": "行動計畫",
    "summary": "摘要模式",
    "help": "命令說明",
}


def build_execution_mode(live_quotes: bool) -> dict[str, Any]:
    """Describe network boundaries without implying that inference is remote."""

    if live_quotes:
        return {
            "id": "local_with_public_data",
            "label": "本地分析＋公開市場資料",
            "rules_execution": "local",
            "language_model_execution": "optional_local_only",
            "public_market_data_enabled": True,
            "external_ai_enabled": False,
            "network_access": "public_market_data_only",
            "simulation_only": True,
            "human_approval_required": True,
        }
    return {
        "id": "offline",
        "label": "完全離線本地分析",
        "rules_execution": "local",
        "language_model_execution": "optional_local_only",
        "public_market_data_enabled": False,
        "external_ai_enabled": False,
        "network_access": "none",
        "simulation_only": True,
        "human_approval_required": True,
    }


def command_intent(lowered_instruction: str) -> str:
    if any(token in lowered_instruction for token in ("help", "說明", "怎麼用", "指令", "命令格式")):
        return "help"
    if any(token in lowered_instruction for token in ("評分", "分數", "權重", "星等", "rating", "score")):
        return "score"
    if any(
        token in lowered_instruction
        for token in ("行動", "策略", "操作", "再平衡", "計畫", "plan", "rebalance")
    ):
        return "plan"
    if any(token in lowered_instruction for token in ("配置", "集中度", "部位", "曝險", "倉位", "allocation")):
        return "allocation"
    if any(token in lowered_instruction for token in ("風險", "預警", "警示", "停損", "虧損", "risk", "alert")):
        return "risk"
    if any(token in lowered_instruction for token in ("摘要", "總覽", "概況", "summary")):
        return "summary"
    return "monitor"


def command_sections(lowered_instruction: str, intent: str) -> tuple[str, ...]:
    sections: list[str] = []
    section_keywords = (
        ("summary", ("摘要", "總覽", "概況", "summary")),
        ("warnings", ("風險", "預警", "警示", "停損", "虧損", "risk", "alert")),
        ("holdings", ("持股", "持倉", "標的", "部位", "holdings")),
        ("allocation", ("配置", "集中度", "曝險", "倉位", "allocation")),
        ("score", ("評分", "分數", "權重", "score", "rating")),
        ("plan", ("行動", "策略", "操作", "再平衡", "計畫", "plan", "rebalance")),
        ("triggers", ("觸發", "監測", "預告", "停損線", "停利線", "trigger", "watch")),
    )
    for section, keywords in section_keywords:
        if any(keyword in lowered_instruction for keyword in keywords):
            sections.append(section)
    defaults = {
        "monitor": ("summary", "warnings", "holdings"),
        "risk": ("warnings", "holdings"),
        "allocation": ("allocation", "plan", "warnings"),
        "score": ("score", "plan", "triggers", "allocation"),
        "plan": ("plan", "triggers", "warnings"),
        "summary": ("summary", "warnings"),
        "help": ("help",),
    }
    if not sections:
        return defaults[intent]
    for section in defaults[intent]:
        if section not in sections:
            sections.append(section)
    return tuple(dict.fromkeys(sections))


def parse_command(
    instruction: str,
    base_config: RiskRuleConfig = RiskRuleConfig(),
) -> LocalRiskCommand:
    text = instruction.strip()
    lowered = text.casefold()
    notes: list[str] = []
    live_quotes: bool | None = None
    config = base_config
    intent = command_intent(lowered)
    sections = command_sections(lowered, intent)
    intent_label = COMMAND_INTENT_LABELS.get(intent)
    if intent_label:
        notes.append(intent_label)

    if any(token in lowered for token in ("離線", "offline", "不抓報價", "不用報價")):
        live_quotes = False
        notes.append("離線模式")
    if any(
        token in lowered
        for token in (
            "即時",
            "連網",
            "網路",
            "網路報價",
            "可連網",
            "online",
            "network",
            "live",
            "抓報價",
            "更新報價",
        )
    ):
        live_quotes = True
        notes.append("連網即時報價")

    if any(token in lowered for token in ("嚴格", "保守", "敏感")):
        config = RiskRuleConfig(
            warning_loss_percent=-3.0,
            critical_loss_percent=-7.0,
            intraday_drop_percent=-2.0,
            large_gain_percent=15.0,
            concentration_warning_percent=20.0,
            concentration_critical_percent=35.0,
            stale_portfolio_hours=base_config.stale_portfolio_hours,
        )
        notes.append("嚴格門檻")
    elif any(token in lowered for token in ("寬鬆", "積極", "放寬")):
        config = RiskRuleConfig(
            warning_loss_percent=-8.0,
            critical_loss_percent=-15.0,
            intraday_drop_percent=-5.0,
            large_gain_percent=30.0,
            concentration_warning_percent=35.0,
            concentration_critical_percent=50.0,
            stale_portfolio_hours=base_config.stale_portfolio_hours,
        )
        notes.append("寬鬆門檻")

    loss_threshold = _percent_after_keywords(
        text,
        ("跌破成本", "成本跌幅", "虧損預警", "跌幅", "虧損", "損失"),
    )
    if loss_threshold is not None:
        warning = -abs(loss_threshold)
        critical = min(config.critical_loss_percent, warning * 2)
        config = RiskRuleConfig(
            warning_loss_percent=warning,
            critical_loss_percent=critical,
            intraday_drop_percent=config.intraday_drop_percent,
            large_gain_percent=config.large_gain_percent,
            concentration_warning_percent=config.concentration_warning_percent,
            concentration_critical_percent=config.concentration_critical_percent,
            stale_portfolio_hours=config.stale_portfolio_hours,
        )
        notes.append(f"跌破成本 {abs(warning):.2f}%")

    critical_threshold = _percent_after_keywords(text, ("重大虧損", "重大", "停損", "止損"))
    if critical_threshold is not None:
        config = RiskRuleConfig(
            warning_loss_percent=config.warning_loss_percent,
            critical_loss_percent=-abs(critical_threshold),
            intraday_drop_percent=config.intraday_drop_percent,
            large_gain_percent=config.large_gain_percent,
            concentration_warning_percent=config.concentration_warning_percent,
            concentration_critical_percent=config.concentration_critical_percent,
            stale_portfolio_hours=config.stale_portfolio_hours,
        )
        notes.append(f"重大虧損 {abs(critical_threshold):.2f}%")

    intraday_threshold = _percent_after_keywords(text, ("盤中跌幅", "盤中", "單日", "今日", "日跌"))
    if intraday_threshold is not None:
        config = RiskRuleConfig(
            warning_loss_percent=config.warning_loss_percent,
            critical_loss_percent=config.critical_loss_percent,
            intraday_drop_percent=-abs(intraday_threshold),
            large_gain_percent=config.large_gain_percent,
            concentration_warning_percent=config.concentration_warning_percent,
            concentration_critical_percent=config.concentration_critical_percent,
            stale_portfolio_hours=config.stale_portfolio_hours,
        )
        notes.append(f"盤中跌幅 {abs(intraday_threshold):.2f}%")

    gain_threshold = _percent_after_keywords(text, ("停利", "止盈", "獲利", "漲幅", "大漲"))
    if gain_threshold is not None:
        config = RiskRuleConfig(
            warning_loss_percent=config.warning_loss_percent,
            critical_loss_percent=config.critical_loss_percent,
            intraday_drop_percent=config.intraday_drop_percent,
            large_gain_percent=abs(gain_threshold),
            concentration_warning_percent=config.concentration_warning_percent,
            concentration_critical_percent=config.concentration_critical_percent,
            stale_portfolio_hours=config.stale_portfolio_hours,
        )
        notes.append(f"獲利提醒 {abs(gain_threshold):.2f}%")

    concentration_threshold = _percent_after_keywords(text, ("集中度", "部位"))
    if concentration_threshold is not None:
        warning = max(1.0, concentration_threshold)
        critical = max(warning + 10.0, config.concentration_critical_percent)
        config = RiskRuleConfig(
            warning_loss_percent=config.warning_loss_percent,
            critical_loss_percent=config.critical_loss_percent,
            intraday_drop_percent=config.intraday_drop_percent,
            large_gain_percent=config.large_gain_percent,
            concentration_warning_percent=warning,
            concentration_critical_percent=critical,
            stale_portfolio_hours=config.stale_portfolio_hours,
        )
        notes.append(f"集中度 {warning:.2f}%")

    stale_hours = _number_after_keywords(text, ("資料過期", "持倉過期", "匯入超過", "超過"))
    if stale_hours is not None:
        config = RiskRuleConfig(
            warning_loss_percent=config.warning_loss_percent,
            critical_loss_percent=config.critical_loss_percent,
            intraday_drop_percent=config.intraday_drop_percent,
            large_gain_percent=config.large_gain_percent,
            concentration_warning_percent=config.concentration_warning_percent,
            concentration_critical_percent=config.concentration_critical_percent,
            stale_portfolio_hours=max(1.0, stale_hours),
        )
        notes.append(f"資料過期 {max(1.0, stale_hours):.1f} 小時")

    return LocalRiskCommand(
        instruction=text,
        live_quotes=live_quotes,
        symbols=tuple(extract_symbols(text)),
        config=config,
        notes=tuple(dict.fromkeys(notes)),
        intent=intent,
        sections=sections,
    )


def command_to_dict(command: LocalRiskCommand) -> dict[str, Any]:
    return {
        "instruction": command.instruction,
        "intent": command.intent,
        "sections": list(command.sections),
        "live_quotes": command.live_quotes,
        "symbols": list(command.symbols),
        "notes": list(command.notes),
        "config": {
            "warning_loss_percent": command.config.warning_loss_percent,
            "critical_loss_percent": command.config.critical_loss_percent,
            "intraday_drop_percent": command.config.intraday_drop_percent,
            "large_gain_percent": command.config.large_gain_percent,
            "concentration_warning_percent": command.config.concentration_warning_percent,
            "concentration_critical_percent": command.config.concentration_critical_percent,
            "stale_portfolio_hours": command.config.stale_portfolio_hours,
        },
    }


def quote_holding_with_candidates(
    holding: core.Holding,
    providers: dict[str, Any],
    provider_order: list[str],
    now: datetime,
    quote_function: QuoteFunction | None,
) -> tuple[core.Quote | None, list[core.QuoteAttempt], list[Any]]:
    if quote_function is not None:
        result = quote_function(holding, providers, provider_order, now)
        if isinstance(result, tuple) and len(result) == 3:
            quote, attempts, candidates = result
            return quote, list(attempts or []), list(candidates or [])
        quote, attempts = result
        candidates = [quote] if quote is not None else []
        return quote, list(attempts or []), candidates
    if hasattr(core, "quote_holding_candidates"):
        quote, attempts, candidates = core.quote_holding_candidates(
            holding,
            providers,
            provider_order,
            now,
            max_successes=QUOTE_VALIDATION_SAMPLE_SIZE,
        )
        return quote, attempts, candidates
    quote, attempts = core.quote_holding(holding, providers, provider_order, now)
    return quote, attempts, [quote] if quote is not None else []


def attach_quote_validation(
    report: dict[str, Any],
    holding: core.Holding,
    quote_candidates: Sequence[Any],
    now: datetime,
) -> None:
    candidates = [quote_candidate_to_dict(item) for item in quote_candidates if item]
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else None
    if quote and not candidates:
        candidates = [dict(quote)]
    report["quote_candidates"] = candidates[:QUOTE_VALIDATION_SAMPLE_SIZE]
    validation = build_quote_validation(report, holding, candidates, now)
    report["quote_validation"] = validation
    report["trusted_quote"] = bool(validation.get("trusted"))
    if report.get("status") == "quoted" and not validation.get("trusted"):
        report["status"] = "quote_untrusted"
        report["market_value"] = None
        report["unrealized_pnl"] = None
        report["unrealized_pnl_percent"] = None


def holding_analysis_fingerprint(
    holding: core.Holding,
    research_inputs: dict[str, Any] | None = None,
) -> str:
    payload = {
        "symbol": holding.symbol.upper(),
        "name": holding.name,
        "market": holding.market.upper(),
        "asset_type": holding.asset_type.upper(),
        "quantity": holding.quantity,
        "average_cost": holding.average_cost,
        "currency": holding.currency.upper(),
        "source_row": holding.source_row,
        "research_inputs": research_inputs or {},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def cached_report_is_reusable(
    report: dict[str, Any],
    now: datetime,
    live_quotes: bool,
) -> bool:
    if not live_quotes:
        return True
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
    as_of = _parse_datetime(str(quote.get("as_of") or ""))
    if as_of is None:
        return False
    age_minutes = max(0.0, (now - as_of).total_seconds() / 60.0)
    market_status = (
        report.get("market_status")
        if isinstance(report.get("market_status"), dict)
        else {}
    )
    market_state = str(market_status.get("state") or "").casefold()
    if market_state == "snapshot_only" or str(report.get("market") or "").upper() == "FUND":
        return age_minutes <= 24 * 60
    if bool(market_status.get("is_open")):
        return age_minutes <= 2
    return age_minutes <= QUOTE_CLOSED_STALE_HOURS * 60


def attach_data_source_confidence(report: dict[str, Any], live_quotes: bool) -> None:
    validation = (
        report.get("quote_validation")
        if isinstance(report.get("quote_validation"), dict)
        else {}
    )
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
    provider = str(quote.get("provider") or "").strip()
    candidate_count = int(validation.get("candidate_count") or 0)
    issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
    blocking = sum(
        1
        for item in issues
        if isinstance(item, dict)
        and str(item.get("severity") or "") in {"critical", "warning"}
    )
    if report.get("status") == "quote_failed":
        score = 25 if not live_quotes else 15
        grade = "D"
    elif "Excel" in provider and str(report.get("market") or "").upper() == "FUND":
        score = 76
        grade = "B"
    elif validation.get("trusted") and candidate_count >= 2:
        score = 95
        grade = "A"
    elif validation.get("trusted"):
        score = 82
        grade = "B"
    else:
        score = max(30, 68 - blocking * 18)
        grade = "C" if score >= 55 else "D"
    report["data_confidence"] = {
        "score": score,
        "grade": grade,
        "label": "高" if score >= 85 else "中" if score >= 65 else "低",
        "provider": provider or ("離線持股資料" if not live_quotes else "未取得"),
        "candidate_count": candidate_count,
        "blocking_issue_count": blocking,
        "verified": bool(validation.get("trusted")),
    }


def build_source_confidence_summary(
    reports: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    scores = [
        _float_or_none((item.get("data_confidence") or {}).get("score"))
        for item in reports
        if isinstance(item.get("data_confidence"), dict)
    ]
    valid = [score for score in scores if score is not None]
    average = sum(valid) / len(valid) if valid else 0.0
    providers = sorted(
        {
            str((item.get("data_confidence") or {}).get("provider") or "")
            for item in reports
            if isinstance(item.get("data_confidence"), dict)
            and str((item.get("data_confidence") or {}).get("provider") or "")
        }
    )
    return {
        "score": core.round_number(average, 2),
        "grade": "A" if average >= 90 else "B" if average >= 75 else "C" if average >= 55 else "D",
        "label": "高" if average >= 85 else "中" if average >= 65 else "低",
        "provider_count": len(providers),
        "providers": providers[:12],
        "low_confidence_count": sum(1 for score in valid if score < 65),
        "evaluated_count": len(valid),
    }


def quote_candidate_to_dict(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, dict):
        return {
            "symbol": str(candidate.get("symbol") or ""),
            "requested_symbol": str(candidate.get("requested_symbol") or ""),
            "provider": str(candidate.get("provider") or ""),
            "price": core.round_number(_float_or_none(candidate.get("price"))),
            "currency": str(candidate.get("currency") or ""),
            "previous_close": core.round_number(_float_or_none(candidate.get("previous_close"))),
            "change": core.round_number(_float_or_none(candidate.get("change"))),
            "change_percent": core.round_number(_float_or_none(candidate.get("change_percent"))),
            "as_of": str(candidate.get("as_of") or ""),
            "market_state": str(candidate.get("market_state") or ""),
            "exchange": str(candidate.get("exchange") or ""),
            "raw_market": str(candidate.get("raw_market") or ""),
        }
    if hasattr(core, "quote_to_dict"):
        return core.quote_to_dict(candidate)
    return {
        "symbol": str(getattr(candidate, "symbol", "") or ""),
        "requested_symbol": str(getattr(candidate, "requested_symbol", "") or ""),
        "provider": str(getattr(candidate, "provider", "") or ""),
        "price": core.round_number(_float_or_none(getattr(candidate, "price", None))),
        "currency": str(getattr(candidate, "currency", "") or ""),
        "previous_close": core.round_number(
            _float_or_none(getattr(candidate, "previous_close", None))
        ),
        "change": core.round_number(_float_or_none(getattr(candidate, "change", None))),
        "change_percent": core.round_number(
            _float_or_none(getattr(candidate, "change_percent", None))
        ),
        "as_of": str(getattr(candidate, "as_of", "") or ""),
        "market_state": str(getattr(candidate, "market_state", "") or ""),
        "exchange": str(getattr(candidate, "exchange", "") or ""),
        "raw_market": str(getattr(candidate, "raw_market", "") or ""),
    }


def build_quote_validation(
    report: dict[str, Any],
    holding: core.Holding,
    candidates: Sequence[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else None
    if report.get("status") != "quoted" or not quote:
        return {
            "trusted": False,
            "state": "missing",
            "state_label": "未取得報價",
            "candidate_count": len(candidates),
            "cross_checked": False,
            "divergence_percent": None,
            "issues": issues,
        }

    prices = [
        _float_or_none(item.get("price"))
        for item in candidates
        if _float_or_none(item.get("price")) is not None
    ]
    prices = [price for price in prices if price is not None and price > 0]
    divergence_percent = quote_divergence_percent(prices)
    if divergence_percent is not None:
        if divergence_percent >= QUOTE_DIVERGENCE_CRITICAL_PERCENT:
            issues.append(
                quote_validation_issue(
                    "critical",
                    "quote_divergence_critical",
                    "報價來源差異過大",
                    f"多來源價格差距 {divergence_percent:.2f}%，此報價禁止作為決策依據。",
                    metric=divergence_percent,
                    action="請人工核對券商、交易所或官方行情後再使用。",
                )
            )
        elif divergence_percent >= QUOTE_DIVERGENCE_WARNING_PERCENT:
            issues.append(
                quote_validation_issue(
                    "warning",
                    "quote_divergence_warning",
                    "報價來源差異偏高",
                    f"多來源價格差距 {divergence_percent:.2f}%，本地AI會拒用此價格。",
                    metric=divergence_percent,
                    action="請等待報價同步或改用人工確認價格。",
                )
            )

    if len(candidates) < 2:
        issues.append(
            quote_validation_issue(
                "info",
                "quote_single_source",
                "報價僅有單一來源",
                "目前只有一個 provider 回傳價格，尚未完成交叉驗證。",
                action="可稍後重新執行，或確認第二報價來源是否可用。",
            )
        )

    issues.extend(quote_identity_issues(report, holding, quote))
    stale_issue = quote_stale_issue(report, quote, now)
    if stale_issue:
        issues.append(stale_issue)

    blocking_codes = {
        "quote_divergence_critical",
        "quote_divergence_warning",
        "quote_stale",
        "quote_symbol_mismatch",
        "quote_currency_mismatch",
        "quote_price_invalid",
    }
    trusted = not any(str(item.get("code") or "") in blocking_codes for item in issues)
    if trusted and len(candidates) >= 2:
        state = "verified"
        state_label = "交叉驗證通過"
    elif trusted:
        state = "caution"
        state_label = "單一來源可用"
    else:
        state = "untrusted"
        state_label = "報價未通過驗證"
    return {
        "trusted": trusted,
        "state": state,
        "state_label": state_label,
        "candidate_count": len(candidates),
        "cross_checked": len(candidates) >= 2,
        "divergence_percent": core.round_number(divergence_percent, 2),
        "issues": issues,
    }


def quote_identity_issues(
    report: dict[str, Any],
    holding: core.Holding,
    quote: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    price = _float_or_none(quote.get("price"))
    if price is None or price <= 0:
        issues.append(
            quote_validation_issue(
                "warning",
                "quote_price_invalid",
                "報價價格異常",
                f"取得價格為 {_format_number(price)}，不適合用於風險計算。",
                action="請人工確認報價來源。",
            )
        )
    expected_symbol = normalize_quote_symbol(holding.symbol)
    returned_symbols = {
        normalize_quote_symbol(str(quote.get("symbol") or "")),
        normalize_quote_symbol(str(quote.get("requested_symbol") or "")),
    }
    returned_symbols.discard("")
    if expected_symbol and returned_symbols and expected_symbol not in returned_symbols:
        issues.append(
            quote_validation_issue(
                "critical",
                "quote_symbol_mismatch",
                "報價代號疑似錯配",
                (
                    f"持倉代號 {holding.symbol} 與報價回傳 "
                    f"{quote.get('symbol') or quote.get('requested_symbol') or '-'} 不一致。"
                ),
                action="請檢查 Excel 的 symbol 與 market 欄位，避免抓錯標的。",
            )
        )

    expected_currency = (
        str(holding.currency or "").strip().upper()
        or MARKET_EXPECTED_CURRENCY.get(str(holding.market or "").strip().upper(), "")
    )
    quote_currency = str(quote.get("currency") or "").strip().upper()
    if expected_currency and quote_currency and expected_currency != quote_currency:
        issues.append(
            quote_validation_issue(
                "warning",
                "quote_currency_mismatch",
                "報價幣別與持倉不一致",
                f"持倉幣別 {expected_currency}，報價幣別 {quote_currency}。",
                action="請確認市場代碼與幣別欄位是否正確。",
            )
        )
    return issues


def quote_stale_issue(
    report: dict[str, Any],
    quote: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    as_of = _parse_datetime(str(quote.get("as_of") or ""))
    if as_of is None:
        return quote_validation_issue(
            "info",
            "quote_timestamp_missing",
            "報價時間缺失",
            "provider 未回傳可解析的報價時間，僅能視為未完全驗證。",
            action="若需盤中決策，請改以有時間戳的來源交叉確認。",
        )
    age_minutes = max(0.0, (now - as_of).total_seconds() / 60.0)
    market_status = report.get("market_status") if isinstance(report.get("market_status"), dict) else {}
    market_state = str(market_status.get("state") or "").casefold()
    is_open = bool(market_status.get("is_open")) or str(report.get("market") or "") == "CRYPTO"
    if is_open and age_minutes > QUOTE_OPEN_STALE_MINUTES:
        return quote_validation_issue(
            "warning",
            "quote_stale",
            "盤中報價過期",
            f"報價時間距今約 {age_minutes:.1f} 分鐘，已超過 {QUOTE_OPEN_STALE_MINUTES:.0f} 分鐘。",
            metric=age_minutes,
            action="本地AI已拒用此價格，請重新抓取報價。",
        )
    if not is_open and market_state != "snapshot_only":
        age_hours = age_minutes / 60.0
        if age_hours > QUOTE_CLOSED_STALE_HOURS:
            return quote_validation_issue(
                "warning",
                "quote_stale",
                "收盤報價過期",
                f"報價時間距今約 {age_hours:.1f} 小時，已超過 {QUOTE_CLOSED_STALE_HOURS:.0f} 小時。",
                metric=age_hours,
                action="請重新抓取報價或確認最新收盤價。",
            )
    return None


def quote_divergence_percent(prices: Sequence[float]) -> float | None:
    valid = sorted(price for price in prices if price > 0)
    if len(valid) < 2:
        return None
    low = valid[0]
    high = valid[-1]
    midpoint = (low + high) / 2.0
    if midpoint <= 0:
        return None
    return (high - low) / midpoint * 100.0


def quote_validation_issue(
    severity: str,
    code: str,
    title: str,
    detail: str,
    *,
    metric: float | None = None,
    action: str = "",
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "title": title,
        "detail": detail,
        "metric": core.round_number(metric, 4),
        "action": action,
    }


def normalize_quote_symbol(value: str) -> str:
    text = value.strip().upper()
    if not text:
        return ""
    text = re.sub(r"^(TSE|OTC)_", "", text)
    text = re.sub(r"\.(TW|TWO|HK|US)$", "", text)
    text = re.sub(r"\.(NASDAQ|NYSE|AMEX)$", "", text)
    text = text.replace("-USD", "").replace("/USD", "")
    text = re.sub(r"[^A-Z0-9]", "", text)
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def build_network_context(
    reports: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    live_quotes: bool,
) -> dict[str, Any]:
    providers: set[str] = set()
    successful_providers: set[str] = set()
    failed_providers: set[str] = set()
    attempt_count = 0
    cross_checked_count = 0
    single_source_count = 0
    untrusted_quote_count = 0
    validation_issue_count = 0
    divergence_count = 0
    stale_quote_count = 0
    symbol_mismatch_count = 0
    currency_mismatch_count = 0
    validation_issues: list[dict[str, Any]] = []
    quote_gaps: list[dict[str, Any]] = []
    for report in reports:
        attempts = report.get("attempts")
        report_attempts = attempts if isinstance(attempts, list) else []
        if not isinstance(attempts, list):
            attempts = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            provider = str(attempt.get("provider") or "").strip()
            if not provider:
                continue
            providers.add(provider)
            attempt_count += 1
            if bool(attempt.get("ok")):
                successful_providers.add(provider)
            else:
                failed_providers.add(provider)
        validation = (
            report.get("quote_validation")
            if isinstance(report.get("quote_validation"), dict)
            else {}
        )
        if validation.get("cross_checked"):
            cross_checked_count += 1
        elif report.get("status") == "quoted":
            single_source_count += 1
        if report.get("status") == "quote_untrusted" or validation.get("trusted") is False:
            if report.get("quote") is not None:
                untrusted_quote_count += 1
        issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code") or "")
            if code:
                validation_issue_count += 1
            if code.startswith("quote_divergence"):
                divergence_count += 1
            elif code == "quote_stale":
                stale_quote_count += 1
            elif code == "quote_symbol_mismatch":
                symbol_mismatch_count += 1
            elif code == "quote_currency_mismatch":
                currency_mismatch_count += 1
            validation_issues.append(
                {
                    "symbol": str(report.get("symbol") or ""),
                    "severity": str(issue.get("severity") or ""),
                    "code": code,
                    "title": str(issue.get("title") or ""),
                    "detail": str(issue.get("detail") or ""),
                }
            )
        status = str(report.get("status") or "")
        if status != "quoted":
            failed_attempts = [
                item
                for item in report_attempts
                if isinstance(item, dict) and not bool(item.get("ok"))
            ]
            failed_sources = [
                str(item.get("provider") or "")
                for item in failed_attempts
                if str(item.get("provider") or "").strip()
            ]
            reason = ""
            if status == "quote_untrusted":
                reason = "報價未通過交叉驗證"
            elif status == "quote_failed":
                reason = "所有報價來源失敗"
            else:
                reason = "未取得可信報價"
            last_message = ""
            for attempt in reversed(failed_attempts):
                last_message = str(attempt.get("message") or "").strip()
                if last_message:
                    break
            quote_gaps.append(
                {
                    "symbol": str(report.get("symbol") or ""),
                    "name": str(report.get("name") or ""),
                    "market": str(report.get("market") or ""),
                    "status": status,
                    "reason": reason,
                    "detail": last_message,
                    "failed_providers": sorted(dict.fromkeys(failed_sources))[:6],
                    "attempt_count": len(report_attempts),
                    "action": "請檢查代號、市場、幣別或改用手動價格後重新執行本地 AI。",
                }
            )

    holding_count = int(summary.get("holding_count") or len(reports))
    quoted_count = int(summary.get("quoted_count") or 0)
    failed_quote_count = int(summary.get("failed_quote_count") or 0)
    coverage_percent = (
        core.round_number((quoted_count / holding_count) * 100.0, 2)
        if holding_count > 0
        else 0.0
    )
    mode = "online-quotes" if live_quotes else "offline-local"
    if not live_quotes:
        health = "offline"
        health_label = "離線評估"
    elif holding_count <= 0:
        health = "setup"
        health_label = "等待持股"
    elif untrusted_quote_count > 0:
        health = "critical"
        health_label = "報價異常"
    elif quoted_count <= 0:
        health = "critical"
        health_label = "報價失敗"
    elif failed_quote_count > 0 or quoted_count < holding_count:
        health = "attention"
        health_label = "部分報價"
    elif single_source_count > 0:
        health = "attention"
        health_label = "單一來源"
    else:
        health = "ready"
        health_label = "交叉驗證通過"
    return {
        "enabled": bool(live_quotes),
        "mode": mode,
        "mode_label": "連網報價監測" if live_quotes else "本地離線監測",
        "health": health,
        "health_label": health_label,
        "policy": "本地AI推理不連接外部LLM；連網僅用公開報價來源更新股價資料。",
        "quote_provider_count": len(providers),
        "quote_providers": sorted(providers),
        "successful_providers": sorted(successful_providers),
        "failed_providers": sorted(failed_providers - successful_providers),
        "attempt_count": attempt_count,
        "holding_count": holding_count,
        "quoted_count": quoted_count,
        "verified_quote_count": quoted_count,
        "cross_checked_count": cross_checked_count,
        "single_source_count": single_source_count,
        "untrusted_quote_count": untrusted_quote_count,
        "failed_quote_count": failed_quote_count,
        "validation_issue_count": validation_issue_count,
        "divergence_count": divergence_count,
        "stale_quote_count": stale_quote_count,
        "symbol_mismatch_count": symbol_mismatch_count,
        "currency_mismatch_count": currency_mismatch_count,
        "validation_issues": validation_issues[:12],
        "quote_gaps": quote_gaps[:20],
        "quote_gap_count": len(quote_gaps),
        "coverage_percent": coverage_percent,
        "coverage_label": (
            f"{quoted_count} / {holding_count} 報價成功"
            if live_quotes
            else "離線資料評估"
        ),
    }


def build_command_result(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    framework: dict[str, Any],
    command: LocalRiskCommand,
    live_quotes: bool,
    local_ai_assessment: dict[str, Any],
) -> dict[str, Any]:
    reported_symbols = {str(report.get("symbol") or "").upper() for report in reports}
    missing_symbols = [
        symbol for symbol in command.symbols if symbol.upper() not in reported_symbols
    ]
    thresholds = {
        "warning_loss_percent": command.config.warning_loss_percent,
        "critical_loss_percent": command.config.critical_loss_percent,
        "intraday_drop_percent": command.config.intraday_drop_percent,
        "large_gain_percent": command.config.large_gain_percent,
        "concentration_warning_percent": command.config.concentration_warning_percent,
        "concentration_critical_percent": command.config.concentration_critical_percent,
        "stale_portfolio_hours": command.config.stale_portfolio_hours,
    }
    network_context = build_network_context(reports, summary, live_quotes)
    text = render_command_result_text(
        reports,
        warnings,
        summary,
        framework,
        command,
        live_quotes,
        missing_symbols,
        local_ai_assessment,
    )
    return {
        "intent": command.intent,
        "sections": list(command.sections),
        "symbols": list(command.symbols),
        "matched_symbols": sorted(symbol for symbol in reported_symbols if symbol),
        "missing_symbols": missing_symbols,
        "live_quotes": live_quotes,
        "thresholds": thresholds,
        "network_context": network_context,
        "notes": list(command.notes),
        "portfolio_score": local_ai_assessment.get("portfolio_score"),
        "score_type": local_ai_assessment.get("score_type"),
        "score_label": local_ai_assessment.get("score_label"),
        "score_coverage_percent": local_ai_assessment.get("score_coverage_percent"),
        "data_availability": local_ai_assessment.get("data_availability"),
        "portfolio_rating": local_ai_assessment.get("portfolio_rating"),
        "risk_level": local_ai_assessment.get("risk_level"),
        "assessments": list(local_ai_assessment.get("assessments", [])),
        "next_actions": list(local_ai_assessment.get("next_actions", [])),
        "confidence_summary": local_ai_assessment.get("confidence_summary"),
        "action_plan": list(local_ai_assessment.get("action_plan", [])),
        "watch_triggers": list(local_ai_assessment.get("watch_triggers", [])),
        "decision_brief": local_ai_assessment.get("decision_brief"),
        "text": text,
    }


def render_command_result_text(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    framework: dict[str, Any],
    command: LocalRiskCommand,
    live_quotes: bool,
    missing_symbols: Sequence[str],
    local_ai_assessment: dict[str, Any] | None = None,
) -> str:
    if command.intent == "help":
        return "\n".join(command_help_lines())

    lines = [
        f"命令意圖：{COMMAND_INTENT_LABELS.get(command.intent, command.intent)}",
        f"報價模式：{'連網即時報價' if live_quotes else '離線'}",
    ]
    if command.symbols:
        lines.append(f"聚焦標的：{', '.join(command.symbols)}")
    if missing_symbols:
        lines.append(f"找不到指定持倉：{', '.join(missing_symbols)}")
    if command.notes:
        lines.append(f"套用條件：{', '.join(command.notes)}")

    if "summary" in command.sections:
        network_context = build_network_context(reports, summary, live_quotes)
        lines.append(
            "摘要："
            f"{summary.get('holding_count', 0)} 檔，"
            f"{summary.get('warning_count', 0)} 項風險預告，"
            f"重大 {summary.get('critical_count', 0)} / 注意 {summary.get('attention_count', 0)}。"
        )
        lines.append(
            "網路資料："
            f"{'已啟用' if live_quotes else '未啟用'}，"
            f"{summary.get('quoted_count', 0)} / {summary.get('holding_count', 0)} 報價成功。"
        )
        lines.append(
            "報價驗證："
            f"{network_context.get('health_label', '-')}，"
            f"交叉驗證 {network_context.get('cross_checked_count', 0)} 檔，"
            f"異常 {network_context.get('untrusted_quote_count', 0)} 檔。"
        )

    if "warnings" in command.sections:
        lines.append("風險重點：")
        if warnings:
            for item in warnings[:5]:
                severity = SEVERITY_LABELS.get(str(item.get("severity")), "提示")
                lines.append(f"- [{severity}] {item.get('title')}")
        else:
            lines.append("- 目前沒有觸發本地風險規則。")

    if "allocation" in command.sections:
        lines.append("配置檢查：")
        positions = estimated_position_weights(reports)
        if positions:
            for item in positions[:5]:
                lines.append(
                    f"- {item['symbol']} 約 {item['weight_percent']:.2f}%"
                    f"（估算值 {_format_money(item['value'], str(item.get('currency') or ''))}）"
                )
        else:
            lines.append("- 持倉缺少可估算市值或成本資料，暫無法計算配置。")

    if "plan" in command.sections:
        lines.append("本地AI行動計畫：")
        action_plan = (
            local_ai_assessment.get("action_plan", [])
            if isinstance(local_ai_assessment, dict)
            else []
        )
        if action_plan:
            for item in action_plan[:6]:
                lines.append(
                    f"- [{item.get('due') or '-'}] {item.get('title') or '-'}："
                    f"{item.get('action') or '-'}"
                )
        else:
            lines.append("- 暫無需立即處理的行動，維持監測。")

    if "triggers" in command.sections:
        lines.append("監測觸發條件：")
        watch_triggers = (
            local_ai_assessment.get("watch_triggers", [])
            if isinstance(local_ai_assessment, dict)
            else []
        )
        if watch_triggers:
            for item in watch_triggers[:8]:
                lines.append(
                    f"- {item.get('symbol')} {item.get('trigger')}："
                    f"{item.get('threshold_text') or item.get('threshold') or '-'}；"
                    f"{item.get('action') or '-'}"
                )
        else:
            lines.append("- 暫無可建立的價位觸發條件，請先補齊成本或報價資料。")

    if "score" in command.sections:
        lines.append("評分/權重：")
        active = framework.get("active_weighting", {})
        lines.append(f"- 啟用權重：{active.get('profile') or '-'}")
        for item in framework.get("dynamic_score_model", []):
            lines.append(f"- {item.get('name')}：{item.get('max_score')} 分")
        if local_ai_assessment:
            rating = local_ai_assessment.get("portfolio_rating") or {}
            portfolio_score = _float_or_none(local_ai_assessment.get("portfolio_score"))
            score_text = "-" if portfolio_score is None else f"{portfolio_score:.0f}"
            lines.append(
                f"- 本地AI投組評分（{local_ai_assessment.get('score_label') or '啟發式風險代理'}）："
                f"{score_text} 分 "
                f"{rating.get('rating', '')}（{rating.get('label', '-')}）"
            )
            lines.append(
                f"- 評分資料覆蓋率："
                f"{local_ai_assessment.get('score_coverage_percent', 0)}%；"
                " unavailable 項目不以中性分補值。"
            )
            for item in local_ai_assessment.get("assessments", [])[:5]:
                item_score = _float_or_none(item.get("score"))
                item_score_text = "-" if item_score is None else f"{item_score:.0f}"
                lines.append(
                    f"- {item.get('symbol')}：{item_score_text} 分 "
                    f"{item.get('rating', '')}（{item.get('rating_label', '-')}），"
                    f"風險 {item.get('risk_level_label', '-')}"
                )
            lines.append("投資情境與操作策略：")
            for item in local_ai_assessment.get("assessments", [])[:3]:
                scenario = item.get("scenario") if isinstance(item.get("scenario"), dict) else {}
                lines.append(
                    f"- {item.get('symbol')} 基準：{scenario.get('base', '-')}"
                    f" 策略：{item.get('strategy', '-')}"
                )

    if "holdings" in command.sections:
        lines.append("持倉檢視：")
        if reports:
            for report in reports[:5]:
                lines.append(f"- {_holding_line(report)}")
        else:
            lines.append("- 沒有符合命令條件的持倉。")

    return "\n".join(lines)


def command_help_lines() -> list[str]:
    return [
        "可用本地輔助AI命令：",
        "- 預設：報價會自動連網抓公開股價資料；輸入離線可關閉。",
        "- 風險：連網 只看 AAPL 跌破成本 3% 停損 8%",
        "- 配置：連網 配置 集中度 25% 防禦",
        "- 評分：連網 評分 TSLA 高估值 通膨",
        "- 行動：連網 行動計畫 再平衡 停損線",
        "- 觸發：連網 監測觸發 AAPL 停利 20%",
        "- 報價：連網 即時 AAPL 盤中 2%",
        "- 離線：離線 摘要 持股",
        "- 門檻：停利 20% 資料過期 24小時",
    ]


def build_product_status(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    portfolio: dict[str, Any] | None,
    framework: dict[str, Any],
    command: LocalRiskCommand,
    live_quotes: bool,
    local_ai_assessment: dict[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    holding_count = int(summary.get("holding_count") or 0)
    warning_count = int(summary.get("warning_count") or 0)
    critical_count = int(summary.get("critical_count") or 0)
    portfolio_score = _float_or_none(local_ai_assessment.get("portfolio_score")) or 0.0
    score_coverage_percent = (
        _float_or_none(local_ai_assessment.get("score_coverage_percent")) or 0.0
    )
    risk_level = str(local_ai_assessment.get("risk_level") or "")
    risk_level_label = str(local_ai_assessment.get("risk_level_label") or "-")
    next_actions = [
        str(item)
        for item in local_ai_assessment.get("next_actions", [])
        if str(item or "").strip()
    ]
    action_plan = (
        local_ai_assessment.get("action_plan")
        if isinstance(local_ai_assessment.get("action_plan"), list)
        else []
    )
    watch_triggers = (
        local_ai_assessment.get("watch_triggers")
        if isinstance(local_ai_assessment.get("watch_triggers"), list)
        else []
    )
    confidence_summary = (
        local_ai_assessment.get("confidence_summary")
        if isinstance(local_ai_assessment.get("confidence_summary"), dict)
        else {}
    )
    network_context = build_network_context(reports, summary, live_quotes)

    if holding_count <= 0:
        state = "empty"
        state_label = "待匯入"
        recommendation = "先讀取 Excel 持股檔，本地AI會自動建立監測基準。"
    elif critical_count > 0 or risk_level == "critical" or portfolio_score < 60:
        state = "critical"
        state_label = "重大風險"
        recommendation = (
            next_actions[0]
            if next_actions
            else "先檢查停損線、單一持股上限與資料是否過期。"
        )
    elif warning_count > 0 or portfolio_score < 75 or score_coverage_percent < 50:
        state = "attention"
        state_label = "需要注意"
        recommendation = (
            next_actions[0]
            if next_actions
            else (
                "先補齊財報、籌碼與估值證據；目前僅能提供啟發式風險代理。"
                if score_coverage_percent < 50
                else "維持監測，新增資金先等待風險報酬比改善。"
            )
        )
    else:
        state = "ready"
        state_label = "運行正常"
        recommendation = "本地AI已自動監測持倉，維持紀律並在新資訊出現時重新評估。"

    return {
        "state": state,
        "state_label": state_label,
        "score": int(round(portfolio_score)) if holding_count else 0,
        "score_type": local_ai_assessment.get("score_type"),
        "score_label": local_ai_assessment.get("score_label"),
        "score_coverage_percent": core.round_number(score_coverage_percent, 2),
        "coverage_warning": local_ai_assessment.get("coverage_warning"),
        "data_availability": local_ai_assessment.get("data_availability"),
        "risk_level": risk_level or "unknown",
        "risk_level_label": risk_level_label,
        "decision_summary": str(local_ai_assessment.get("decision_brief") or ""),
        "confidence_label": str(confidence_summary.get("label") or "-"),
        "confidence_score": confidence_summary.get("score"),
        "action_count": len(action_plan),
        "trigger_count": len(watch_triggers),
        "network_enabled": network_context["enabled"],
        "network_mode": network_context["mode"],
        "network_mode_label": network_context["mode_label"],
        "quote_health": network_context["health"],
        "quote_health_label": network_context["health_label"],
        "network_policy": network_context["policy"],
        "network_context": network_context,
        "recommendation": recommendation,
        "next_actions": next_actions[:5],
        "command_suggestions": build_command_suggestions(
            reports,
            warnings,
            framework,
            command,
            live_quotes,
        ),
        "watch_status": "local-live" if live_quotes else "local-offline",
        "watch_status_label": network_context["mode_label"],
        "data_mode": build_execution_mode(live_quotes)["id"],
        "execution_mode": build_execution_mode(live_quotes),
        "offline_mode": not live_quotes,
        "has_portfolio": holding_count > 0,
        "portfolio_count": holding_count,
        "warning_count": warning_count,
        "notice_count": int(summary.get("issue_count") or warning_count),
        "critical_count": critical_count,
        "source_confidence": summary.get("source_confidence") or {},
        "reused_holding_count": int(summary.get("reused_holding_count") or 0),
        "refreshed_holding_count": int(summary.get("refreshed_holding_count") or 0),
        "risk_rule_profiles": ["stock", "etf", "fund", "bond"],
        "quoted_count": int(summary.get("quoted_count") or 0),
        "failed_quote_count": int(summary.get("failed_quote_count") or 0),
        "coverage_label": network_context["coverage_label"],
        "portfolio_file": str((portfolio or {}).get("file_name") or ""),
        "generated_at": generated_at.isoformat(),
    }


def build_command_suggestions(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    framework: dict[str, Any],
    command: LocalRiskCommand,
    live_quotes: bool,
) -> list[str]:
    suggestions: list[str] = []
    first_symbol = next(
        (
            str(report.get("symbol") or "").upper()
            for report in reports
            if str(report.get("symbol") or "").strip()
        ),
        "",
    )
    if first_symbol:
        suggestions.extend(
            [
                f"連網 評分 {first_symbol} 高估值 通膨",
                f"連網 只看 {first_symbol} 跌破成本 3% 停損 8%",
            ]
        )

    active = framework.get("active_weighting") if isinstance(framework, dict) else {}
    if isinstance(active, dict) and active.get("profile") == "高估值防禦模式":
        suggestions.append("連網 高估值 通膨 防禦")
    if not live_quotes:
        suggestions.append("連網 即時 盤中 2%")
    else:
        suggestions.append("離線 摘要 持股")
    if any(str(item.get("severity") or "") == "critical" for item in warnings):
        suggestions.append("連網 風險 預警 停損 8%")
    if command.intent != "summary":
        suggestions.append("離線 摘要 持股")
    suggestions.extend(["連網 配置 集中度 25%", "連網 評分 權重"])

    unique: list[str] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        normalized = suggestion.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
        if len(unique) >= 6:
            break
    return unique


def estimated_position_weights(reports: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    weighted: list[dict[str, Any]] = []
    for report in reports:
        value = _float_or_none(report.get("market_value"))
        if value is None:
            value = _float_or_none(report.get("cost_basis"))
        if value is None or value <= 0:
            continue
        weighted.append(
            {
                "symbol": str(report.get("symbol") or "-"),
                "currency": str(report.get("currency") or ""),
                "value": value,
            }
        )
    total = sum(float(item["value"]) for item in weighted)
    if total <= 0:
        return []
    for item in weighted:
        item["weight_percent"] = float(item["value"]) / total * 100
    return sorted(weighted, key=lambda item: float(item["weight_percent"]), reverse=True)


def build_local_ai_assessment(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    framework: dict[str, Any],
    command: LocalRiskCommand,
    live_quotes: bool,
    analytics_evidence: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    position_weights = {
        str(item.get("symbol") or "").upper(): _float_or_none(item.get("weight_percent"))
        for item in estimated_position_weights(reports)
    }
    warnings_by_symbol = _warnings_by_symbol(warnings)
    assessments = [
        score_holding_report(
            report,
            warnings_by_symbol.get(str(report.get("symbol") or "").upper(), []),
            position_weights.get(str(report.get("symbol") or "").upper()),
            framework,
            command,
            live_quotes,
        )
        for report in reports
    ]
    score_types = {
        str(item.get("score_type") or "")
        for item in assessments
        if str(item.get("score_type") or "")
    }
    portfolio_score_type = (
        "evidence_weighted_local_assessment"
        if "evidence_weighted_local_assessment" in score_types
        else "heuristic_risk_proxy"
    )
    score_label = (
        "證據加權本地評估"
        if portfolio_score_type == "evidence_weighted_local_assessment"
        else "啟發式風險代理分數"
    )
    portfolio_score = _weighted_portfolio_score(assessments)
    portfolio_heuristic_risk_proxy = _weighted_assessment_metric(
        assessments,
        "heuristic_risk_proxy_score",
    )
    portfolio_rating = (
        rating_for_score(portfolio_score)
        if portfolio_score_type == "evidence_weighted_local_assessment"
        else risk_proxy_rating_for_score(portfolio_score)
    )
    risk_level, risk_level_label = portfolio_risk_level(portfolio_score, warnings, assessments)
    top_risks = [str(item.get("title") or item.get("code") or "") for item in warnings[:6]]
    next_actions = build_next_actions(assessments, warnings, portfolio_score, live_quotes)
    confidence_summary = build_confidence_summary(assessments)
    action_plan = build_action_plan(
        assessments,
        warnings,
        portfolio_score,
        command,
        live_quotes,
    )
    watch_triggers = build_watch_triggers(reports, warnings, command)
    decision_brief = build_decision_brief(
        portfolio_score,
        portfolio_rating,
        risk_level_label,
        confidence_summary,
        action_plan,
        score_label,
    )
    assessment_coverages = [
        float(value)
        for value in (
            _float_or_none(item.get("score_coverage_percent"))
            for item in assessments
        )
        if value is not None
    ]
    score_coverage_percent = (
        sum(assessment_coverages) / len(assessment_coverages)
        if assessment_coverages
        else 0.0
    )
    return {
        "portfolio_score": core.round_number(portfolio_score, 2),
        "score_type": portfolio_score_type,
        "score_label": score_label,
        "heuristic_risk_proxy_score": (
            core.round_number(portfolio_heuristic_risk_proxy, 2)
        ),
        "score_coverage": core.round_number(score_coverage_percent / 100, 4),
        "score_coverage_percent": core.round_number(score_coverage_percent, 2),
        "coverage_warning": (
            "研究資料覆蓋不足；此分數只代表可驗證的本地風險規則，不代表基本面、籌碼或估值結論。"
            if portfolio_score_type == "heuristic_risk_proxy"
            else ""
        ),
        "portfolio_rating": portfolio_rating,
        "risk_level": risk_level,
        "risk_level_label": risk_level_label,
        "summary": (
            f"投組{score_label} {portfolio_score:.0f} 分，"
            f"{portfolio_rating['rating']}（{portfolio_rating['label']}），"
            f"風險層級：{risk_level_label}。"
        ),
        "assessments": assessments,
        "top_risks": top_risks,
        "next_actions": next_actions,
        "confidence_summary": confidence_summary,
        "action_plan": action_plan,
        "watch_triggers": watch_triggers,
        "data_availability": {
            "score_coverage_percent": core.round_number(score_coverage_percent, 2),
            "available_component_count": sum(
                int(item.get("scored_component_count") or 0)
                for item in assessments
            ),
            "unavailable_component_count": sum(
                int(item.get("unavailable_component_count") or 0)
                for item in assessments
            ),
            "evidence_ids": sorted(
                {
                    str(evidence.get("id"))
                    for item in assessments
                    for evidence in item.get("evidence", [])
                    if isinstance(evidence, dict) and evidence.get("id")
                }
            ),
            "analytics_context_evidence_ids": [
                str(item.get("id") or "")
                for item in analytics_evidence
                if isinstance(item, dict) and str(item.get("id") or "")
            ],
        },
        "decision_brief": decision_brief,
        "memory_update": build_memory_update(
            portfolio_score,
            portfolio_rating,
            risk_level_label,
            top_risks,
            next_actions,
            decision_brief,
            score_label,
        ),
    }


def score_holding_report(
    report: dict[str, Any],
    symbol_warnings: Sequence[dict[str, Any]],
    position_weight: float | None,
    framework: dict[str, Any],
    command: LocalRiskCommand,
    live_quotes: bool,
) -> dict[str, Any]:
    components = component_scores_for_report(
        report,
        symbol_warnings,
        framework,
        command,
        live_quotes,
    )
    scored_components = [
        item
        for item in components
        if item.get("included_in_score") is True
        and _float_or_none(item.get("score")) is not None
    ]
    heuristic_components = [
        item
        for item in scored_components
        if item.get("scoring_basis") == "heuristic_risk_proxy"
    ]
    scored_total = sum(float(item["score"]) for item in scored_components)
    scored_maximum = sum(float(item["max_score"]) for item in scored_components)
    model_maximum = sum(float(item["max_score"]) for item in components)
    covered_maximum = sum(
        float(item["max_score"])
        * clamp(_float_or_none(item.get("coverage")) or 0.0, 0.0, 1.0)
        for item in scored_components
    )
    score = scored_total / scored_maximum * 100 if scored_maximum > 0 else 0.0
    heuristic_total = sum(float(item["score"]) for item in heuristic_components)
    heuristic_maximum = sum(float(item["max_score"]) for item in heuristic_components)
    heuristic_risk_proxy_score = (
        heuristic_total / heuristic_maximum * 100
        if heuristic_maximum > 0
        else 0.0
    )
    score_coverage_percent = (
        covered_maximum / model_maximum * 100 if model_maximum > 0 else 0.0
    )
    has_research_score = any(
        item.get("scoring_basis") == "research_evidence"
        for item in scored_components
    )
    score_type = (
        "evidence_weighted_local_assessment"
        if has_research_score
        else "heuristic_risk_proxy"
    )
    rating = (
        rating_for_score(score)
        if has_research_score
        else risk_proxy_rating_for_score(score)
    )
    risk_level, risk_level_label = holding_risk_level(score, symbol_warnings)
    confidence = assessment_confidence(report, live_quotes)
    scenario = investment_scenario(report, command)
    strategy = operation_strategy(
        report,
        score,
        symbol_warnings,
        position_weight,
        command,
        confidence,
    )
    return {
        "symbol": str(report.get("symbol") or "-"),
        "name": str(report.get("name") or ""),
        "market": str(report.get("market") or ""),
        "score": core.round_number(score, 2),
        "score_type": score_type,
        "score_label": (
            "證據加權本地評估"
            if has_research_score
            else "啟發式風險代理分數"
        ),
        "heuristic_risk_proxy_score": (
            core.round_number(heuristic_risk_proxy_score, 2)
        ),
        "score_coverage": core.round_number(score_coverage_percent / 100, 4),
        "score_coverage_percent": core.round_number(score_coverage_percent, 2),
        "scored_component_count": len(scored_components),
        "unavailable_component_count": sum(
            1 for item in components if item.get("status") == "unavailable"
        ),
        "rating": rating["rating"],
        "rating_label": rating["label"],
        "rating_range": rating["range"],
        "risk_level": risk_level,
        "risk_level_label": risk_level_label,
        "position_weight_percent": core.round_number(position_weight, 2),
        "confidence": confidence,
        "score_components": components,
        "evidence": build_assessment_evidence_catalog(
            report,
            symbol_warnings,
            components,
        ),
        "risk_flags": [
            {
                "severity": item.get("severity"),
                "code": item.get("code"),
                "title": item.get("title"),
                "action": item.get("action"),
            }
            for item in symbol_warnings
        ],
        "scenario": scenario,
        "strategy": strategy,
        "reasons": assessment_reasons(report, symbol_warnings, position_weight, confidence),
    }


def component_scores_for_report(
    report: dict[str, Any],
    symbol_warnings: Sequence[dict[str, Any]],
    framework: dict[str, Any],
    command: LocalRiskCommand,
    live_quotes: bool,
) -> list[dict[str, Any]]:
    model = framework.get("dynamic_score_model") or SCORE_MODEL
    codes = {str(item.get("code") or "") for item in symbol_warnings}
    severities = [str(item.get("severity") or "") for item in symbol_warnings]
    critical_count = severities.count("critical")
    warning_count = severities.count("warning")
    info_count = severities.count("info")
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else None
    quote_is_trusted = report.get("status") == "quoted" and bool(report.get("trusted_quote", True))
    change_percent = (
        _float_or_none(quote.get("change_percent")) if quote and quote_is_trusted else None
    )
    high_volatility = report_is_high_volatility(report)
    quantity = _float_or_none(report.get("quantity"))
    quote_failed = report.get("status") != "quoted"
    symbol = _evidence_token(str(report.get("symbol") or "unknown"))
    research_inputs = (
        report.get("research_inputs")
        if isinstance(report.get("research_inputs"), dict)
        else {}
    )
    components: list[dict[str, Any]] = []

    for item in model:
        name = str(item.get("name") or "")
        max_score = _float_or_none(item.get("max_score")) or 0.0
        component_key = _component_key(name)

        if name in RESEARCH_COMPONENT_INPUTS:
            input_key, missing_reason = RESEARCH_COMPONENT_INPUTS[name]
            research_component = _research_component_score(
                symbol=symbol,
                component_key=component_key,
                max_score=max_score,
                input_key=input_key,
                source=research_inputs.get(input_key),
                missing_reason=missing_reason,
            )
            components.append({**research_component, "name": name})
            continue

        if name == "技術面":
            evidence_ids = (
                [f"quote:{symbol}:change_percent"]
                if change_percent is not None and quote_is_trusted
                else []
            )
            if not evidence_ids:
                components.append(
                    _unavailable_component(
                        name=name,
                        component_key=component_key,
                        max_score=max_score,
                        reason="缺少可信的價格變化、均線與量價時序；不以中性分補值。",
                        required_data=["trusted_price_change", "price_history", "volume_history"],
                    )
                )
                continue
            ratio = 0.68
            if change_percent <= command.config.intraday_drop_percent:
                ratio -= 0.18
            elif change_percent > 0:
                ratio += 0.06
            elif change_percent < 0:
                ratio -= 0.04
            score = max_score * clamp(ratio, 0.15, 0.98)
            components.append(
                {
                    "name": name,
                    "component_key": component_key,
                    "max_score": core.round_number(max_score, 2),
                    "score": core.round_number(score, 2),
                    "status": "partial",
                    "coverage": 0.25,
                    "coverage_percent": 25.0,
                    "included_in_score": True,
                    "scoring_basis": "heuristic_risk_proxy",
                    "evidence_ids": evidence_ids,
                    "required_data": ["price_history", "volume_history"],
                    "note": "僅使用可信的單期價格變化作風險代理，不代表完整技術面分析。",
                }
            )
            continue

        if name == "風險":
            ratio = 0.82
            ratio -= critical_count * 0.20
            ratio -= warning_count * 0.10
            ratio -= info_count * 0.03
            if high_volatility:
                ratio -= 0.10
            if quote_failed and live_quotes:
                ratio -= 0.08
            evidence_ids = []
            if quantity is not None:
                evidence_ids.append(f"holding:{symbol}:quantity")
            evidence_ids.extend(
                f"risk:{symbol}:{_evidence_token(str(code))}"
                for code in sorted(codes)
            )
            score = max_score * clamp(ratio, 0.15, 0.98)
            coverage = min(1.0, 0.50 + (0.15 if quantity is not None else 0.0))
            components.append(
                {
                    "name": name,
                    "component_key": component_key,
                    "max_score": core.round_number(max_score, 2),
                    "score": core.round_number(score, 2),
                    "status": "available",
                    "coverage": core.round_number(coverage, 2),
                    "coverage_percent": core.round_number(coverage * 100, 2),
                    "included_in_score": True,
                    "scoring_basis": "heuristic_risk_proxy",
                    "evidence_ids": evidence_ids,
                    "required_data": [],
                    "note": "依本地持倉、警示、集中度、報價覆蓋率與高波動屬性計算啟發式風險代理。",
                }
            )
            continue

        components.append(
            _unavailable_component(
                name=name,
                component_key=component_key,
                max_score=max_score,
                reason="此版本沒有可驗證的資料與評分規則，因此不補中性分。",
                required_data=[],
            )
        )

    return components


def _component_key(name: str) -> str:
    return {
        "基本面": "fundamentals",
        "成長性": "growth",
        "獲利能力": "profitability",
        "財務安全": "financial_safety",
        "技術面": "technical",
        "籌碼": "positioning",
        "估值": "valuation",
        "風險": "risk",
    }.get(name, _evidence_token(name))


def _unavailable_component(
    *,
    name: str,
    component_key: str,
    max_score: float,
    reason: str,
    required_data: Sequence[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "component_key": component_key,
        "max_score": core.round_number(max_score, 2),
        "score": None,
        "status": "unavailable",
        "coverage": 0.0,
        "coverage_percent": 0.0,
        "included_in_score": False,
        "scoring_basis": "unavailable",
        "evidence_ids": [],
        "required_data": list(required_data),
        "note": reason,
    }


def _research_component_score(
    *,
    symbol: str,
    component_key: str,
    max_score: float,
    input_key: str,
    source: Any,
    missing_reason: str,
) -> dict[str, Any]:
    if not isinstance(source, dict) or not source:
        return {
            **_unavailable_component(
                name="",
                component_key=component_key,
                max_score=max_score,
                reason=f"{missing_reason}；目前資料不可用，不以中性分補值。",
                required_data=[input_key],
            ),
        }

    evidence_fields = [
        str(key)
        for key, value in source.items()
        if key not in {"normalized_score", "coverage", "coverage_percent", "source", "as_of"}
        and isinstance(value, (str, int, float, bool))
        and value not in {None, ""}
    ]
    evidence_ids = [
        f"research:{symbol}:{component_key}:{_evidence_token(field)}"
        for field in sorted(evidence_fields)
    ]
    normalized_score = _float_or_none(source.get("normalized_score"))
    source_name = str(source.get("source") or "").strip()
    as_of = str(source.get("as_of") or "").strip()
    explicit_coverage = _float_or_none(source.get("coverage"))
    if explicit_coverage is None:
        percent = _float_or_none(source.get("coverage_percent"))
        explicit_coverage = percent / 100 if percent is not None else None
    coverage = clamp(
        explicit_coverage
        if explicit_coverage is not None
        else min(1.0, len(evidence_ids) / 3),
        0.0,
        1.0,
    )
    missing_contract_fields: list[str] = []
    if normalized_score is None:
        missing_contract_fields.append(f"{input_key}.normalized_score")
    if not source_name:
        missing_contract_fields.append(f"{input_key}.source")
    if not as_of:
        missing_contract_fields.append(f"{input_key}.as_of")
    if not evidence_ids:
        missing_contract_fields.append(f"{input_key}.evidence_field")
    if missing_contract_fields:
        return {
            "component_key": component_key,
            "max_score": core.round_number(max_score, 2),
            "score": None,
            "status": "partial",
            "coverage": core.round_number(coverage, 4),
            "coverage_percent": core.round_number(coverage * 100, 2),
            "included_in_score": False,
            "scoring_basis": "unscored_research_evidence",
            "evidence_ids": evidence_ids,
            "required_data": missing_contract_fields,
            "note": (
                "已收到部分研究資料，但 source、as_of、normalized_score "
                "與至少一個 scalar evidence field 必須齊備，因此不納入評分。"
            ),
        }
    normalized_score = clamp(normalized_score, 0.0, 100.0)
    return {
        "component_key": component_key,
        "max_score": core.round_number(max_score, 2),
        "score": core.round_number(max_score * normalized_score / 100, 2),
        "status": "available" if coverage >= 0.75 else "partial",
        "coverage": core.round_number(coverage, 4),
        "coverage_percent": core.round_number(coverage * 100, 2),
        "included_in_score": True,
        "scoring_basis": "research_evidence",
        "evidence_ids": evidence_ids,
        "required_data": [],
        "note": "使用明確提供且帶證據欄位的研究分數；仍須核對來源與資料日期。",
    }


def build_assessment_evidence_catalog(
    report: dict[str, Any],
    symbol_warnings: Sequence[dict[str, Any]],
    components: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    symbol = _evidence_token(str(report.get("symbol") or "unknown"))
    evidence: dict[str, dict[str, Any]] = {}

    def add(evidence_id: str, source: str, field: str, value: Any, as_of: str = "") -> None:
        evidence[evidence_id] = {
            "id": evidence_id,
            "source": source,
            "field": field,
            "value": value,
            "as_of": as_of,
        }

    quantity = _float_or_none(report.get("quantity"))
    if quantity is not None:
        add(f"holding:{symbol}:quantity", "local_portfolio", "quantity", quantity)
    average_cost = _float_or_none(report.get("average_cost"))
    if average_cost is not None:
        add(
            f"holding:{symbol}:average_cost",
            "local_portfolio",
            "average_cost",
            average_cost,
        )
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
    for field in ("price", "change_percent"):
        value = _float_or_none(quote.get(field))
        if value is not None:
            add(
                f"quote:{symbol}:{field}",
                str(quote.get("provider") or "public_market_data"),
                field,
                value,
                str(quote.get("as_of") or ""),
            )
    for warning in symbol_warnings:
        code = _evidence_token(str(warning.get("code") or "warning"))
        add(
            f"risk:{symbol}:{code}",
            "local_risk_rule",
            str(warning.get("code") or "warning"),
            warning.get("metric"),
        )
    research_inputs = (
        report.get("research_inputs")
        if isinstance(report.get("research_inputs"), dict)
        else {}
    )
    for component in components:
        component_key = str(component.get("component_key") or "")
        input_key = next(
            (
                candidate
                for _name, (candidate, _reason) in RESEARCH_COMPONENT_INPUTS.items()
                if candidate == component_key
            ),
            "",
        )
        source = research_inputs.get(input_key)
        if not isinstance(source, dict):
            continue
        for evidence_id in component.get("evidence_ids", []):
            field = str(evidence_id).rsplit(":", 1)[-1]
            original_field = next(
                (
                    key
                    for key in source
                    if _evidence_token(str(key)) == field
                ),
                field,
            )
            add(
                str(evidence_id),
                str(source.get("source") or "supplied_research"),
                str(original_field),
                source.get(original_field),
                str(source.get("as_of") or ""),
            )
    used_ids = {
        str(evidence_id)
        for component in components
        for evidence_id in component.get("evidence_ids", [])
    }
    return [evidence[key] for key in sorted(used_ids) if key in evidence]


def build_analytics_context_evidence(
    context: dict[str, Any],
    *,
    symbols: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Flatten explicitly allowed analytics context into bounded, cited facts."""

    target_symbols = {
        str(symbol or "").strip().upper()
        for symbol in symbols
        if str(symbol or "").strip()
    }
    collected: dict[str, dict[str, Any]] = {}

    def record_identity(value: dict[str, Any], fallback: str) -> str:
        for key in (
            "evidence_id",
            "event_id",
            "decision_id",
            "entry_id",
            "transaction_id",
            "rule_id",
            "id",
        ):
            if str(value.get(key) or "").strip():
                return _analytics_path_token(str(value[key]))
        return fallback

    def walk(
        section: str,
        value: Any,
        path: list[str],
        *,
        symbol_hint: str = "",
        source_hint: str = "",
        as_of_hint: str = "",
        depth: int = 0,
    ) -> None:
        if len(collected) >= ANALYTICS_CONTEXT_MAX_EVIDENCE or depth > 6:
            return
        if isinstance(value, dict):
            local_symbol = str(
                value.get("symbol")
                or value.get("ticker")
                or symbol_hint
                or ""
            ).strip().upper()
            if local_symbol and target_symbols and local_symbol not in target_symbols:
                return
            local_source = str(
                value.get("source")
                or value.get("provider")
                or source_hint
                or f"local_analytics.{section}"
            ).strip()
            local_as_of = str(
                value.get("as_of")
                or value.get("occurred_at")
                or value.get("observed_at")
                or value.get("updated_at")
                or value.get("created_at")
                or as_of_hint
                or ""
            ).strip()
            for key, child in value.items():
                key_text = str(key or "").strip()
                if not key_text or _analytics_key_is_sensitive(key_text):
                    continue
                walk(
                    section,
                    child,
                    [*path, _analytics_path_token(key_text)],
                    symbol_hint=local_symbol,
                    source_hint=local_source,
                    as_of_hint=local_as_of,
                    depth=depth + 1,
                )
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                identity = (
                    record_identity(child, str(index))
                    if isinstance(child, dict)
                    else str(index)
                )
                walk(
                    section,
                    child,
                    [*path, identity],
                    symbol_hint=symbol_hint,
                    source_hint=source_hint,
                    as_of_hint=as_of_hint,
                    depth=depth + 1,
                )
                if len(collected) >= ANALYTICS_CONTEXT_MAX_EVIDENCE:
                    break
            return
        if value is None or not isinstance(value, (str, int, float, bool)):
            return
        if isinstance(value, str) and (not value.strip() or len(value) > 2000):
            return
        field = ".".join(path) if path else section
        evidence_id = (
            f"analytics:{_analytics_path_token(section)}:"
            f"{':'.join(path) if path else 'value'}"
        )
        if evidence_id in collected:
            suffix = hashlib.sha256(field.encode("utf-8")).hexdigest()[:8]
            evidence_id = f"{evidence_id}:{suffix}"
        collected[evidence_id] = {
            "id": evidence_id,
            "section": section,
            "source": source_hint or f"local_analytics.{section}",
            "field": field,
            "value": value,
            "as_of": as_of_hint,
            "symbol": symbol_hint,
        }

    for section in ANALYTICS_CONTEXT_SECTIONS:
        value = context.get(section)
        if value is None:
            continue
        walk(section, value, [])
        if len(collected) >= ANALYTICS_CONTEXT_MAX_EVIDENCE:
            break
    return list(collected.values())


def _analytics_key_is_sensitive(value: str) -> bool:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    return any(
        token == normalized
        or normalized.endswith(f"_{token}")
        or normalized.startswith(f"{token}_")
        for token in ANALYTICS_CONTEXT_SENSITIVE_TOKENS
    )


def _analytics_path_token(value: str) -> str:
    original = str(value or "").strip()
    token = _evidence_token(original)
    if token != "unknown" or not original:
        return token
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    return f"unicode-{digest}"


def _evidence_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return token.casefold() or "unknown"


def rating_for_score(score: float | int | None) -> dict[str, Any]:
    value = _float_or_none(score) or 0.0
    if value >= 90:
        return {"range": "90-100", "rating": "★★★★★", "label": "高度看好"}
    if value >= 80:
        return {"range": "80-89", "rating": "★★★★☆", "label": "偏多"}
    if value >= 70:
        return {"range": "70-79", "rating": "★★★☆☆", "label": "中性偏多"}
    if value >= 60:
        return {"range": "60-69", "rating": "★★☆☆☆", "label": "觀望"}
    return {"range": "60 以下", "rating": "★☆☆☆☆", "label": "高風險"}


def risk_proxy_rating_for_score(score: float | int | None) -> dict[str, Any]:
    value = _float_or_none(score) or 0.0
    if value >= 90:
        return {"range": "90-100", "rating": "風險代理", "label": "本地風險訊號很低"}
    if value >= 80:
        return {"range": "80-89", "rating": "風險代理", "label": "本地風險訊號較低"}
    if value >= 70:
        return {"range": "70-79", "rating": "風險代理", "label": "本地風險訊號中等"}
    if value >= 60:
        return {"range": "60-69", "rating": "風險代理", "label": "本地風險訊號偏高"}
    return {"range": "60 以下", "rating": "風險代理", "label": "本地風險訊號很高"}


def holding_risk_level(
    score: float,
    symbol_warnings: Sequence[dict[str, Any]],
) -> tuple[str, str]:
    severities = {str(item.get("severity") or "") for item in symbol_warnings}
    if "critical" in severities or score < 60:
        return "high", "高"
    if "warning" in severities or score < 70:
        return "medium", "中"
    return "low", "低"


def portfolio_risk_level(
    score: float,
    warnings: Sequence[dict[str, Any]],
    assessments: Sequence[dict[str, Any]],
) -> tuple[str, str]:
    if not assessments:
        return "none", "無持倉"
    severities = {str(item.get("severity") or "") for item in warnings}
    if "critical" in severities or score < 60:
        return "high", "高"
    if "warning" in severities or score < 70:
        return "medium", "中"
    return "low", "低"


def assessment_confidence(report: dict[str, Any], live_quotes: bool) -> dict[str, Any]:
    score = 0.35
    if report.get("status") == "quoted":
        score += 0.25
    validation = (
        report.get("quote_validation")
        if isinstance(report.get("quote_validation"), dict)
        else {}
    )
    validation_state = str(validation.get("state") or "")
    if validation_state == "verified":
        score += 0.08
    elif validation_state == "caution":
        score -= 0.04
    elif validation_state == "untrusted":
        score -= 0.25
    if _float_or_none(report.get("average_cost")) is not None:
        score += 0.15
    quantity = _float_or_none(report.get("quantity"))
    if quantity is not None and quantity > 0:
        score += 0.10
    if str(report.get("market") or "").strip():
        score += 0.05
    if live_quotes:
        score += 0.05
    score = clamp(score, 0.0, 1.0)
    if score >= 0.80:
        label = "高"
    elif score >= 0.55:
        label = "中"
    else:
        label = "低"
    return {"score": core.round_number(score, 2), "label": label}


def investment_scenario(
    report: dict[str, Any],
    command: LocalRiskCommand,
) -> dict[str, str]:
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
    currency = str(quote.get("currency") or report.get("currency") or "")
    symbol = str(report.get("symbol") or "-")
    price = _float_or_none(quote.get("price"))
    average_cost = _float_or_none(report.get("average_cost"))
    pnl_percent = _float_or_none(report.get("unrealized_pnl_percent"))
    if price is not None:
        optimistic = (
            f"{symbol} 守住現價 {_format_money(price, currency)} 附近並改善動能，"
            "再用財報、法說會與產業催化確認加碼條件。"
        )
    else:
        optimistic = (
            f"{symbol} 補齊報價與最新財報後，若風險警示解除，可恢復正常監測。"
        )

    if pnl_percent is not None:
        base = (
            f"以目前成本損益 {pnl_percent:.2f}% 為基準，"
            "維持分批與停損紀律，不把短線訊號混用為長線價值判斷。"
        )
    else:
        base = "資料尚不足，以持倉成本、集中度與報價可用性作基準情境。"

    if average_cost is not None:
        stop_line = average_cost * (1 + command.config.critical_loss_percent / 100)
        pessimistic = (
            f"若跌破成本風險線 {_format_money(stop_line, currency)}，"
            "或新資訊推翻投資假設，先降曝險再重新評估。"
        )
    else:
        pessimistic = "若報價持續失敗、部位過度集中或資料缺漏未補，先暫停新增部位。"
    return {
        "optimistic": optimistic,
        "base": base,
        "pessimistic": pessimistic,
    }


def operation_strategy(
    report: dict[str, Any],
    score: float,
    symbol_warnings: Sequence[dict[str, Any]],
    position_weight: float | None,
    command: LocalRiskCommand,
    confidence: dict[str, Any],
) -> str:
    codes = {str(item.get("code") or "") for item in symbol_warnings}
    severities = {str(item.get("severity") or "") for item in symbol_warnings}
    if "critical" in severities or score < 60:
        return "暫停加碼，優先檢查停損線、持倉成本與部位上限，必要時分批降曝險。"
    if (
        position_weight is not None
        and position_weight >= command.config.concentration_warning_percent
    ):
        return "持股資料放大檢視，先控管單一部位比重，再等待基本面或估值重新確認。"
    if quote_quality_codes(codes) or confidence.get("label") == "低":
        return "先核對報價來源、時間戳、代號與幣別，確認價格可信後再做進出場判斷。"
    if score >= 80:
        return "可續抱並用回撤或財報確認作為加碼條件，避免在極端貪婪時追高。"
    if score >= 70:
        return "中性偏多，維持觀察與分批紀律，新增資金需等待風險報酬比改善。"
    return "觀望為主，建立明確進出場計畫，等風險警示下降後再提高部位。"


def assessment_reasons(
    report: dict[str, Any],
    symbol_warnings: Sequence[dict[str, Any]],
    position_weight: float | None,
    confidence: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if position_weight is not None:
        reasons.append(f"估算部位占比 {position_weight:.2f}%。")
    pnl_percent = _float_or_none(report.get("unrealized_pnl_percent"))
    if pnl_percent is not None:
        reasons.append(f"相對成本損益 {pnl_percent:.2f}%。")
    if symbol_warnings:
        reasons.append(f"觸發 {len(symbol_warnings)} 項本地風險規則。")
    reasons.append(f"資料信心：{confidence.get('label')}。")
    return reasons


def build_confidence_summary(assessments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    scores: list[float] = []
    low_symbols: list[str] = []
    for item in assessments:
        confidence = item.get("confidence") if isinstance(item.get("confidence"), dict) else {}
        score = _float_or_none(confidence.get("score"))
        if score is None:
            continue
        scores.append(score)
        if score < 0.55 or confidence.get("label") == "低":
            low_symbols.append(str(item.get("symbol") or "-"))
    average = sum(scores) / len(scores) if scores else 0.0
    if average >= 0.80:
        label = "高"
    elif average >= 0.55:
        label = "中"
    else:
        label = "低"
    return {
        "score": core.round_number(average, 2),
        "label": label,
        "sample_count": len(scores),
        "low_confidence_symbols": low_symbols[:8],
    }


def build_action_plan(
    assessments: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    portfolio_score: float,
    command: LocalRiskCommand,
    live_quotes: bool,
) -> list[dict[str, Any]]:
    del command
    warning_symbols = {
        str(item.get("symbol") or "").upper()
        for item in warnings
        if str(item.get("symbol") or "").strip()
    }
    ranked = sorted(
        assessments,
        key=lambda item: (
            _risk_rank(str(item.get("risk_level") or "")),
            _float_or_none(item.get("score")) or 999.0,
            -(_float_or_none(item.get("position_weight_percent")) or 0.0),
        ),
    )
    plan: list[dict[str, Any]] = []
    for item in ranked:
        symbol = str(item.get("symbol") or "-")
        score = _float_or_none(item.get("score")) or 0.0
        risk_level = str(item.get("risk_level") or "")
        confidence = item.get("confidence") if isinstance(item.get("confidence"), dict) else {}
        position_weight = _float_or_none(item.get("position_weight_percent"))
        if risk_level == "high" or score < 60:
            priority = "critical"
            due = "立即"
            title = f"{symbol} 先降風險"
        elif risk_level == "medium" or score < 72 or symbol.upper() in warning_symbols:
            priority = "attention"
            due = "本週"
            title = f"{symbol} 建立觀察線"
        else:
            priority = "monitor"
            due = "下次更新"
            title = f"{symbol} 維持監測"
        if priority == "monitor" and len(plan) >= 4:
            continue
        plan.append(
            {
                "priority": priority,
                "due": due,
                "symbol": symbol,
                "title": title,
                "action": str(item.get("strategy") or "維持紀律並等待新資訊。"),
                "score": core.round_number(score, 2),
                "risk_level": risk_level,
                "risk_level_label": item.get("risk_level_label"),
                "confidence_label": confidence.get("label"),
                "position_weight_percent": core.round_number(position_weight, 2),
                "rationale": list(item.get("reasons", []))[:3],
            }
        )
        if len(plan) >= 6:
            break

    if portfolio_score < 70:
        plan.insert(
            0,
            {
                "priority": "critical",
                "due": "立即",
                "symbol": "PORTFOLIO",
                "title": "暫停新增單筆投入",
                "action": "投組分數低於 70，先降低錯誤決策風險，再等待風險預告下降。",
                "score": core.round_number(portfolio_score, 2),
                "risk_level": "high",
                "risk_level_label": "高",
                "confidence_label": "",
                "position_weight_percent": None,
                "rationale": ["投組評分低於中性偏多門檻。"],
            },
        )
    if not live_quotes:
        plan.append(
            {
                "priority": "attention",
                "due": "需要盤中監測時",
                "symbol": "DATA",
                "title": "切換即時報價",
                "action": "目前為離線模式；若要監測盤中跌幅，請用命令加入「即時」。",
                "score": None,
                "risk_level": "medium",
                "risk_level_label": "中",
                "confidence_label": "",
                "position_weight_percent": None,
                "rationale": ["離線模式不抓即時報價。"],
            }
        )
    return _rank_action_plan(plan)[:8]


def build_watch_triggers(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    command: LocalRiskCommand,
) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    warning_codes_by_symbol: dict[str, set[str]] = {}
    for item in warnings:
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            warning_codes_by_symbol.setdefault(symbol, set()).add(str(item.get("code") or ""))
    for report in reports:
        symbol = str(report.get("symbol") or "-").upper()
        quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
        currency = str(quote.get("currency") or report.get("currency") or "")
        price = _float_or_none(quote.get("price"))
        average_cost = _float_or_none(report.get("average_cost"))
        if average_cost is not None:
            warning_line = average_cost * (1 + command.config.warning_loss_percent / 100)
            critical_line = average_cost * (1 + command.config.critical_loss_percent / 100)
            take_profit_line = average_cost * (1 + command.config.large_gain_percent / 100)
            triggers.extend(
                [
                    _watch_trigger(
                        symbol,
                        "成本預警",
                        "warning",
                        warning_line,
                        price,
                        currency,
                        f"跌破成本 {abs(command.config.warning_loss_percent):.2f}% 時重新檢查假設。",
                    ),
                    _watch_trigger(
                        symbol,
                        "停損底線",
                        "critical",
                        critical_line,
                        price,
                        currency,
                        f"跌破成本 {abs(command.config.critical_loss_percent):.2f}% 時先降曝險。",
                    ),
                    _watch_trigger(
                        symbol,
                        "停利檢查",
                        "info",
                        take_profit_line,
                        price,
                        currency,
                        f"達到成本上方 {command.config.large_gain_percent:.2f}% 時檢查是否分批鎖利。",
                    ),
                ]
            )
        symbol_codes = warning_codes_by_symbol.get(symbol, set())
        if "quote_failed" in symbol_codes or quote_quality_codes(symbol_codes):
            triggers.append(
                {
                    "symbol": symbol,
                    "trigger": "報價核驗",
                    "severity": "warning",
                    "threshold": None,
                    "threshold_text": "通過報價驗證",
                    "current_price": None,
                    "currency": currency,
                    "action": "確認來源差異、報價時間、代號與幣別後重新執行本地 AI。",
                }
            )
    return sorted(
        triggers,
        key=lambda item: (
            SEVERITY_ORDER.get(str(item.get("severity") or ""), 99),
            str(item.get("symbol") or ""),
            str(item.get("trigger") or ""),
        ),
    )[:12]


def build_decision_brief(
    portfolio_score: float,
    portfolio_rating: dict[str, Any],
    risk_level_label: str,
    confidence_summary: dict[str, Any],
    action_plan: Sequence[dict[str, Any]],
    score_label: str = "啟發式風險代理分數",
) -> str:
    first_action = next(
        (str(item.get("title") or item.get("action") or "") for item in action_plan if item),
        "維持監測",
    )
    return (
        f"本地AI判讀：{score_label} {portfolio_score:.0f} 分 "
        f"{portfolio_rating.get('rating', '')}（{portfolio_rating.get('label', '-')}），"
        f"風險 {risk_level_label}，資料信心 {confidence_summary.get('label', '-')}。"
        f"優先事項：{first_action}。"
    )


def build_next_actions(
    assessments: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    portfolio_score: float,
    live_quotes: bool,
) -> list[str]:
    if not assessments:
        return ["先匯入或掃描持股資料，再讓本地AI建立監測基準。"]
    codes = {str(item.get("code") or "") for item in warnings}
    severities = {str(item.get("severity") or "") for item in warnings}
    actions: list[str] = []
    if "critical" in severities:
        actions.append("先處理重大風險持倉，確認停損線、部位上限與是否需要分批降曝險。")
    if "quote_failed" in codes or "quote_coverage_low" in codes:
        actions.append("修正股票代號、市場欄位或網路報價設定，避免低估價格風險。")
    if quote_quality_codes(codes):
        actions.append("先人工核對報價來源差異、時間戳、代號與幣別；未通過前不要用價格做進出場決策。")
    if "concentration_critical" in codes or "concentration_warning" in codes:
        actions.append("檢查單一持股集中度，新增資金先避開已過度集中的標的。")
    if portfolio_score < 70:
        actions.append("暫停新增單筆投入，等風險預告下降或投資假設重新成立。")
    if not live_quotes:
        actions.append("目前為離線模式；需要盤中風險時可用命令指定即時報價。")
    actions.append("新財報、法說會、政策或重大事件出現時，重新評估原本投資假設。")
    return actions[:5]


def build_memory_update(
    portfolio_score: float,
    portfolio_rating: dict[str, Any],
    risk_level_label: str,
    top_risks: Sequence[str],
    next_actions: Sequence[str],
    decision_brief: str = "",
    score_label: str = "啟發式風險代理分數",
) -> dict[str, Any]:
    return {
        "kind": "investment-local-risk-ai",
        "title": "本地AI投組監測",
        "content": "\n".join(
            [
                decision_brief,
                f"本地AI投組評分（{score_label}）：{portfolio_score:.0f} 分 "
                f"{portfolio_rating.get('rating', '')}（{portfolio_rating.get('label', '-')}）",
                f"風險層級：{risk_level_label}",
                "主要風險：" + ("；".join(item for item in top_risks if item) or "目前無重大風險"),
                "下一步：" + ("；".join(item for item in next_actions if item) or "維持監測"),
            ]
        ),
    }


def quote_quality_codes(codes: set[str]) -> bool:
    return any(
        code
        in {
            "quote_divergence_critical",
            "quote_divergence_warning",
            "quote_stale",
            "quote_symbol_mismatch",
            "quote_currency_mismatch",
            "quote_price_invalid",
        }
        for code in codes
    )


def _risk_rank(value: str) -> int:
    return {"high": 0, "medium": 1, "low": 2, "none": 3}.get(value, 9)


def _action_priority_rank(value: str) -> int:
    return {"critical": 0, "attention": 1, "monitor": 2, "info": 3}.get(value, 9)


def _rank_action_plan(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        items,
        key=lambda item: (
            _action_priority_rank(str(item.get("priority") or "")),
            _float_or_none(item.get("score")) or 999.0,
            str(item.get("symbol") or ""),
        ),
    )
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in ranked:
        key = (str(item.get("symbol") or ""), str(item.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(dict(item))
    return output


def _watch_trigger(
    symbol: str,
    trigger: str,
    severity: str,
    threshold: float,
    current_price: float | None,
    currency: str,
    action: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trigger": trigger,
        "severity": severity,
        "threshold": core.round_number(threshold, 4),
        "threshold_text": _format_money(threshold, currency),
        "current_price": core.round_number(current_price, 4),
        "currency": currency,
        "action": action,
    }


def _warnings_by_symbol(
    warnings: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in warnings:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(dict(item))
    return grouped


def _weighted_portfolio_score(assessments: Sequence[dict[str, Any]]) -> float:
    return _weighted_assessment_metric(assessments, "score")


def _weighted_assessment_metric(
    assessments: Sequence[dict[str, Any]],
    field: str,
) -> float:
    if not assessments:
        return 0.0
    weighted_total = 0.0
    weight_sum = 0.0
    for item in assessments:
        score = _float_or_none(item.get(field))
        weight = _float_or_none(item.get("position_weight_percent"))
        if score is None or weight is None or weight <= 0:
            continue
        weighted_total += score * weight
        weight_sum += weight
    if weight_sum > 0:
        return weighted_total / weight_sum
    valid_scores = [
        score
        for score in (_float_or_none(item.get(field)) for item in assessments)
        if score is not None
    ]
    if not valid_scores:
        return 0.0
    return sum(valid_scores) / len(valid_scores)


def report_is_high_volatility(report: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(report.get("symbol") or ""),
            str(report.get("name") or ""),
            str(report.get("market") or ""),
            str(report.get("asset_type") or ""),
        ]
    ).casefold()
    return any(token in text for token in ("crypto", "加密", "小型", "small"))


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def analysis_framework(
    holdings: Sequence[core.Holding],
    command: LocalRiskCommand | None = None,
) -> dict[str, Any]:
    dynamic_model, notes = build_dynamic_score_model(holdings)
    active_weighting = build_active_weighting(holdings, command)
    return {
        "chief_baseline_weighting": [dict(item) for item in CHIEF_BASELINE_WEIGHTS],
        "active_weighting": active_weighting,
        "defensive_weighting": {
            "profile": DEFENSIVE_WEIGHTING["profile"],
            "trigger": DEFENSIVE_WEIGHTING["trigger"],
            "weights": [dict(item) for item in DEFENSIVE_WEIGHTING["weights"]],
        },
        "dimensions": [dict(item) for item in ANALYSIS_DIMENSIONS],
        "workflow": list(ANALYSIS_WORKFLOW),
        "base_score_model": [dict(item) for item in SCORE_MODEL],
        "dynamic_score_model": dynamic_model,
        "dynamic_weight_notes": notes,
        "rating_standards": [dict(item) for item in RATING_STANDARDS],
        "risk_proxy_rating_standards": [
            dict(item) for item in RISK_PROXY_STANDARDS
        ],
        "score_semantics": {
            "default_type": "heuristic_risk_proxy",
            "label": "啟發式風險代理分數",
            "higher_is_lower_detected_rule_risk": True,
            "not_a_return_forecast": True,
            "unavailable_components_receive_no_neutral_score": True,
        },
        "research_data_policy": {
            "required_component_inputs": {
                name: {
                    "input_key": input_key,
                    "missing_reason": missing_reason,
                }
                for name, (input_key, missing_reason) in RESEARCH_COMPONENT_INPUTS.items()
            },
            "evidence_ids_required": True,
        },
        "risk_control_principles": list(RISK_CONTROL_PRINCIPLES),
        "applicability": FRAMEWORK_APPLICABILITY,
    }


def build_active_weighting(
    holdings: Sequence[core.Holding],
    command: LocalRiskCommand | None,
) -> dict[str, Any]:
    defensive_reasons = defensive_weight_reasons(holdings, command)
    if defensive_reasons:
        return {
            "profile": DEFENSIVE_WEIGHTING["profile"],
            "weights": [dict(item) for item in DEFENSIVE_WEIGHTING["weights"]],
            "reasons": defensive_reasons,
        }
    return {
        "profile": "常態市場基準模式",
        "weights": [dict(item) for item in CHIEF_BASELINE_WEIGHTS],
        "reasons": ["未偵測到需要提高防禦權重的條件。"],
    }


def defensive_weight_reasons(
    holdings: Sequence[core.Holding],
    command: LocalRiskCommand | None,
) -> list[str]:
    reasons: list[str] = []
    instruction = (command.instruction if command else "").casefold()
    if any(
        token in instruction
        for token in ("防禦", "高估值", "高位階", "通膨", "估值修正", "風控優先")
    ):
        reasons.append("命令觸發高估值/總經防禦模式。")
    if any(_is_high_volatility_holding(holding) for holding in holdings):
        reasons.append("持倉包含高波動標的。")
    concentration = _estimated_concentration(holdings)
    if concentration is not None and concentration >= 40:
        reasons.append(f"單一持股估算集中度 {concentration:.2f}% 偏高。")
    return reasons


def build_dynamic_score_model(
    holdings: Sequence[core.Holding],
) -> tuple[list[dict[str, Any]], list[str]]:
    scores = {str(item["name"]): int(item["max_score"]) for item in SCORE_MODEL}
    notes: list[str] = []

    if any(_is_high_volatility_holding(holding) for holding in holdings):
        scores["風險"] += 5
        scores["技術面"] += 5
        scores["基本面"] -= 5
        scores["估值"] -= 5
        notes.append("高波動標的：提高風險與技術面權重，降低基本面與估值權重。")

    concentration = _estimated_concentration(holdings)
    if concentration is not None and concentration >= 40:
        scores["風險"] += 5
        scores["基本面"] -= 5
        notes.append(f"單一持股估算集中度 {concentration:.2f}%：提高風險權重。")

    _normalize_score_total(scores)
    return [
        {"name": name, "max_score": scores[name]}
        for name in [str(item["name"]) for item in SCORE_MODEL]
    ], notes


def render_framework_lines(framework: dict[str, Any] | None) -> list[str]:
    if not framework:
        return []
    lines = ["", "分析流程："]
    lines.extend(["", "核心分析權重基準："])
    for item in framework.get("chief_baseline_weighting", []):
        lines.append(
            f"- {item.get('name')} {item.get('weight')}%：{item.get('philosophy')}"
        )

    active = framework.get("active_weighting", {})
    lines.extend(["", f"目前啟用權重：{active.get('profile') or '-'}"])
    for item in active.get("weights", []):
        detail = item.get("adjustment") or item.get("philosophy") or ""
        lines.append(f"- {item.get('name')} {item.get('weight')}%：{detail}")
    for reason in active.get("reasons", []):
        lines.append(f"- 啟用原因：{reason}")

    for index, step in enumerate(framework.get("workflow", []), start=1):
        lines.append(f"{index}. {step}")

    lines.extend(["", "分析面向權重："])
    for item in framework.get("dimensions", []):
        lines.append(f"- {item.get('name')} {item.get('weight')}%：{item.get('criteria')}")

    lines.extend(["", "動態評分模型（100 分制）："])
    for item in framework.get("dynamic_score_model", []):
        lines.append(f"- {item.get('name')}：{item.get('max_score')} 分")
    notes = framework.get("dynamic_weight_notes", [])
    if notes:
        lines.append("動態權重調整：")
        lines.extend([f"- {note}" for note in notes])
    else:
        lines.append("動態權重調整：使用預設權重")

    lines.extend(["", "評等標準："])
    for item in framework.get("rating_standards", []):
        lines.append(
            f"- {item.get('range')}：{item.get('rating')}（{item.get('label')}）"
        )

    lines.extend(["", "風險控管原則："])
    for item in framework.get("risk_control_principles", []):
        lines.append(f"- {item}")
    lines.append(str(framework.get("applicability") or ""))
    return lines


def _is_high_volatility_holding(holding: core.Holding) -> bool:
    text = " ".join(
        [holding.symbol, holding.name, holding.market, holding.asset_type]
    ).casefold()
    return any(token in text for token in ("crypto", "加密", "小型", "small"))


def _estimated_concentration(holdings: Sequence[core.Holding]) -> float | None:
    values: list[float] = []
    for holding in holdings:
        if holding.average_cost is None or holding.quantity <= 0:
            continue
        value = holding.quantity * holding.average_cost
        if value > 0:
            values.append(value)
    total = sum(values)
    if total <= 0:
        return None
    return max(values) / total * 100


def _normalize_score_total(scores: dict[str, int]) -> None:
    for key, value in list(scores.items()):
        scores[key] = max(0, int(value))
    total = sum(scores.values())
    if total == 100:
        return
    scores["基本面"] = max(0, scores.get("基本面", 0) + (100 - total))


def filter_holdings_by_command(
    holdings: Sequence[core.Holding],
    command: LocalRiskCommand,
) -> list[core.Holding]:
    if not command.symbols:
        return [holding for holding in holdings if holding.quantity > 0]
    wanted = {symbol.upper() for symbol in command.symbols}
    return [holding for holding in holdings if holding.symbol.upper() in wanted]


def extract_symbols(instruction: str) -> list[str]:
    if not instruction.strip():
        return []
    stop_words = {
        "AI",
        "LIVE",
        "OFFLINE",
        "HELP",
        "SUMMARY",
        "RISK",
        "ALERT",
        "SCORE",
        "RATING",
        "MONITOR",
        "ALLOCATION",
        "USD",
        "TWD",
        "HKD",
        "US",
        "TW",
        "HK",
        "NYSE",
        "NASDAQ",
        "ETF",
    }
    symbols: list[str] = []
    for match in re.findall(r"\b[A-Za-z][A-Za-z0-9.-]{0,9}\b|\b\d{4,6}\b", instruction):
        symbol = match.strip().upper()
        if symbol in stop_words or re.fullmatch(r"\d{1,3}", symbol):
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _percent_after_keywords(text: str, keywords: Sequence[str]) -> float | None:
    for keyword in keywords:
        pattern = rf"{re.escape(keyword)}[^\d\-+%％]{{0,12}}([+-]?\d+(?:\.\d+)?)\s*[%％]?"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _float_or_none(match.group(1))
    return None


def _number_after_keywords(text: str, keywords: Sequence[str]) -> float | None:
    for keyword in keywords:
        pattern = rf"{re.escape(keyword)}[^\d\-+]{{0,12}}([+-]?\d+(?:\.\d+)?)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _float_or_none(match.group(1))
    return None


def asset_rule_profile(
    report: dict[str, Any],
    config: RiskRuleConfig,
) -> dict[str, Any]:
    asset_type = str(report.get("asset_type") or "").strip().upper()
    market = str(report.get("market") or "").strip().upper()
    if asset_type == "FUND" or market == "FUND":
        return {
            "key": "fund",
            "label": "共同基金",
            "warning_loss_percent": min(config.warning_loss_percent, -12.0),
            "critical_loss_percent": min(config.critical_loss_percent, -25.0),
            "intraday_enabled": False,
            "concentration_warning_percent": max(config.concentration_warning_percent, 35.0),
            "concentration_critical_percent": max(config.concentration_critical_percent, 55.0),
        }
    if asset_type == "ETF":
        return {
            "key": "etf",
            "label": "ETF",
            "warning_loss_percent": min(config.warning_loss_percent, -15.0),
            "critical_loss_percent": min(config.critical_loss_percent, -30.0),
            "intraday_enabled": True,
            "concentration_warning_percent": max(config.concentration_warning_percent, 35.0),
            "concentration_critical_percent": max(config.concentration_critical_percent, 55.0),
        }
    if asset_type == "BOND":
        return {
            "key": "bond",
            "label": "債券",
            "warning_loss_percent": min(config.warning_loss_percent, -8.0),
            "critical_loss_percent": min(config.critical_loss_percent, -15.0),
            "intraday_enabled": False,
            "concentration_warning_percent": max(config.concentration_warning_percent, 40.0),
            "concentration_critical_percent": max(config.concentration_critical_percent, 60.0),
        }
    return {
        "key": "stock",
        "label": "股票",
        "warning_loss_percent": config.warning_loss_percent,
        "critical_loss_percent": config.critical_loss_percent,
        "intraday_enabled": True,
        "concentration_warning_percent": config.concentration_warning_percent,
        "concentration_critical_percent": config.concentration_critical_percent,
    }


def consolidate_risk_warnings(
    warnings: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped_codes = {
        "quote_single_source",
        "quote_timestamp_missing",
        "missing_average_cost",
    }
    output: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for source in warnings:
        item = dict(source)
        severity = str(item.get("severity") or "info")
        code = str(item.get("code") or "")
        symbol = str(item.get("symbol") or "")
        if code in grouped_codes:
            grouped.setdefault((severity, code), []).append(item)
            continue
        key = (severity, code, symbol)
        if key in seen:
            continue
        seen.add(key)
        item["occurrence_count"] = 1
        output.append(item)
    for (_severity, code), items in grouped.items():
        first = dict(items[0])
        symbols = [str(item.get("symbol") or "") for item in items if item.get("symbol")]
        first["occurrence_count"] = len(items)
        first["symbols"] = symbols[:20]
        first["symbol"] = symbols[0] if len(symbols) == 1 else ""
        if len(items) > 1:
            first["title"] = f"{len(items)} 檔：{first.get('title') or code}"
            first["detail"] = (
                f"同類提醒已合併，涉及 {len(items)} 檔持倉。"
                f"代表標的：{'、'.join(symbols[:6]) or '-'}。"
            )
        output.append(first)
    return sorted(
        output,
        key=lambda item: (
            SEVERITY_ORDER.get(str(item.get("severity")), 99),
            -int(item.get("occurrence_count") or 1),
            str(item.get("symbol") or ""),
            str(item.get("code") or ""),
        ),
    )


def evaluate_risk_warnings(
    reports: Sequence[dict[str, Any]],
    portfolio: dict[str, Any] | None,
    now: datetime,
    config: RiskRuleConfig,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []

    for report in reports:
        symbol = str(report.get("symbol") or "-")
        name = str(report.get("name") or "")
        label = f"{symbol} {name}".strip()
        quote = report.get("quote") if isinstance(report.get("quote"), dict) else None
        quote_is_trusted = report.get("status") == "quoted" and bool(report.get("trusted_quote", True))
        quantity = _float_or_none(report.get("quantity"))
        average_cost = _float_or_none(report.get("average_cost"))
        pnl_percent = _float_or_none(report.get("unrealized_pnl_percent"))
        change_percent = (
            _float_or_none(quote.get("change_percent")) if quote and quote_is_trusted else None
        )
        price = _float_or_none(quote.get("price")) if quote else None
        rule_profile = asset_rule_profile(report, config)

        if report.get("status") == "quote_failed":
            warnings.append(
                _warning(
                    "warning",
                    "quote_failed",
                    f"{label} 報價未取得",
                    "目前無法取得即時或延遲報價，這檔持倉沒有納入價格風險計算。",
                    symbol=symbol,
                    action="請確認代號、市場欄位與網路連線，再重新執行本地輔助AI。",
                )
            )
        warnings.extend(_quote_validation_warnings(report))

        if quantity is None or quantity <= 0:
            warnings.append(
                _warning(
                    "warning",
                    "quantity_invalid",
                    f"{label} 持倉數量異常",
                    f"目前數量為 {_format_number(quantity)}，可能造成市值與集中度失真。",
                    symbol=symbol,
                    action="請回到 Excel 檢查 quantity 欄位。",
                )
            )

        if average_cost is None:
            warnings.append(
                _warning(
                    "info",
                    "missing_average_cost",
                    f"{label} 缺少平均成本",
                    "缺少成本後無法計算未實現損益與跌破成本預警。",
                    symbol=symbol,
                    action="請補上 average_cost 欄位以啟用完整風險規則。",
                )
            )

        if price is not None and price <= 0:
            warnings.append(
                _warning(
                    "warning",
                    "price_invalid",
                    f"{label} 報價異常",
                    f"取得價格為 {_format_number(price)}，不適合用於風險計算。",
                    symbol=symbol,
                    action="請人工確認報價來源。",
                )
            )

        if pnl_percent is not None:
            if pnl_percent <= float(rule_profile["critical_loss_percent"]):
                warnings.append(
                    _warning(
                        "critical",
                        "cost_drawdown_critical",
                        f"{label} 跌破成本 {abs(pnl_percent):.2f}%",
                        _cost_detail(report),
                        symbol=symbol,
                        metric=pnl_percent,
                        action="請優先檢查停損線、部位大小與是否需要人工處理。",
                    )
                )
                warnings[-1]["rule_profile"] = rule_profile["key"]
            elif pnl_percent <= float(rule_profile["warning_loss_percent"]):
                warnings.append(
                    _warning(
                        "warning",
                        "cost_drawdown_warning",
                        f"{label} 跌破成本 {abs(pnl_percent):.2f}%",
                        _cost_detail(report),
                        symbol=symbol,
                        metric=pnl_percent,
                        action="請檢查持倉理由、風險承受度與是否需要更新觀察清單。",
                    )
                )
                warnings[-1]["rule_profile"] = rule_profile["key"]
            elif pnl_percent >= config.large_gain_percent:
                warnings.append(
                    _warning(
                        "info",
                        "large_unrealized_gain",
                        f"{label} 未實現獲利 {pnl_percent:.2f}%",
                        _cost_detail(report),
                        symbol=symbol,
                        metric=pnl_percent,
                        action="請留意回撤風險與是否需要調整追蹤條件。",
                    )
                )

        if (
            rule_profile["intraday_enabled"]
            and change_percent is not None
            and change_percent <= config.intraday_drop_percent
        ):
            warnings.append(
                _warning(
                    "warning",
                    "intraday_drop",
                    f"{label} 盤中跌幅 {abs(change_percent):.2f}%",
                    f"報價來源顯示單日變動為 {change_percent:.2f}%。",
                    symbol=symbol,
                    metric=change_percent,
                    action="請確認是否有財報、公告或市場事件造成波動。",
                )
            )

    warnings.extend(_concentration_warnings(reports, config))
    stale_warning = _portfolio_stale_warning(portfolio, now, config)
    if stale_warning:
        warnings.append(stale_warning)
    warnings.extend(_quote_coverage_warnings(reports))

    return sorted(
        warnings,
        key=lambda item: (
            SEVERITY_ORDER.get(str(item.get("severity")), 99),
            str(item.get("symbol") or ""),
            str(item.get("code") or ""),
        ),
    )


def build_summary(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    quoted_count = 0
    for report in reports:
        if report.get("status") == "quoted":
            quoted_count += 1

    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    for item in warnings:
        severity = str(item.get("severity") or "")
        if severity in severity_counts:
            severity_counts[severity] += 1

    actionable_count = severity_counts["critical"] + severity_counts["warning"]
    return {
        "holding_count": len(reports),
        "quoted_count": quoted_count,
        "failed_quote_count": len(reports) - quoted_count,
        "warning_count": actionable_count,
        "issue_count": len(warnings),
        "critical_count": severity_counts["critical"],
        "attention_count": severity_counts["warning"],
        "info_count": severity_counts["info"],
    }


def render_assessment_lines(assessment: dict[str, Any] | None) -> list[str]:
    if not assessment:
        return []
    rating = assessment.get("portfolio_rating")
    rating_text = ""
    if isinstance(rating, dict):
        rating_text = f"{rating.get('rating', '')}（{rating.get('label', '-')}）"
    portfolio_score = _float_or_none(assessment.get("portfolio_score"))
    score_text = "-" if portfolio_score is None else f"{portfolio_score:.0f}"
    lines = [
        "",
        "本地AI評分與策略：",
        (
            f"- 投組評分：{score_text} 分 {rating_text}，"
            f"風險層級：{assessment.get('risk_level_label', '-')}"
        ),
    ]
    next_actions = [str(item) for item in assessment.get("next_actions", []) if item]
    if next_actions:
        lines.append("- 下一步：" + "；".join(next_actions[:3]))
    for item in assessment.get("assessments", [])[:8]:
        item_score = _float_or_none(item.get("score"))
        item_score_text = "-" if item_score is None else f"{item_score:.0f}"
        weight = _float_or_none(item.get("position_weight_percent"))
        weight_text = "-" if weight is None else f"{weight:.2f}%"
        scenario = item.get("scenario") if isinstance(item.get("scenario"), dict) else {}
        lines.append(
            f"- {item.get('symbol')}：{item_score_text} 分 "
            f"{item.get('rating', '')}（{item.get('rating_label', '-')}），"
            f"風險 {item.get('risk_level_label', '-')}，部位 {weight_text}。"
        )
        lines.append(f"  情境：樂觀 {scenario.get('optimistic', '-')} / 基準 {scenario.get('base', '-')}")
        lines.append(f"  悲觀：{scenario.get('pessimistic', '-')}")
        lines.append(f"  操作策略：{item.get('strategy', '-')}")
    return lines


def render_product_status_lines(product_status: dict[str, Any] | None) -> list[str]:
    if not product_status:
        return []
    lines = [
        "",
        "本地AI產品狀態：",
        (
            f"- 狀態：{product_status.get('state_label', '-')}，"
            f"分數 {product_status.get('score', 0)}，"
            f"{product_status.get('watch_status_label', '-')}，"
            f"信心 {product_status.get('confidence_label', '-')}"
        ),
        (
            f"- 網路資料：{product_status.get('network_mode_label', product_status.get('watch_status_label', '-'))}，"
            f"{product_status.get('coverage_label', '-')}，"
            f"{product_status.get('quote_health_label', '-')}"
        ),
        f"- 決策：{product_status.get('decision_summary') or '等待下一次本地AI判讀。'}",
        f"- 建議：{product_status.get('recommendation') or '維持監測。'}",
    ]
    commands = [
        str(item)
        for item in product_status.get("command_suggestions", [])
        if str(item or "").strip()
    ]
    if commands:
        lines.append("- 可輸入命令：" + "；".join(commands[:4]))
    return lines


def render_content(
    reports: Sequence[dict[str, Any]],
    warnings: Sequence[dict[str, Any]],
    portfolio: dict[str, Any] | None,
    now: datetime,
    summary: dict[str, Any],
    *,
    live_quotes: bool,
    command: LocalRiskCommand | None = None,
    framework: dict[str, Any] | None = None,
    command_result: dict[str, Any] | None = None,
    local_ai_assessment: dict[str, Any] | None = None,
    product_status: dict[str, Any] | None = None,
) -> str:
    mode_label = "本地即時報價模式" if live_quotes else "本地離線模式"
    file_name = str((portfolio or {}).get("file_name") or "未命名持倉")
    lines = [
        "本地輔助AI監測摘要（非投資建議）",
        f"模式：{mode_label}",
        f"檔案：{file_name}",
        f"生成時間：{now.astimezone().isoformat()}",
        (
            "總覽："
            f"{summary['holding_count']} 檔持倉，"
            f"{summary['quoted_count']} 檔報價成功，"
            f"{summary['warning_count']} 項風險預告"
            f"（重大 {summary['critical_count']} / 注意 {summary['attention_count']} / 提示 {summary['info_count']}）"
        ),
        "",
        "風險預告：",
    ]

    if command and command.instruction:
        lines.extend(
            [
                f"本地命令：{command.instruction}",
                f"命令套用：{', '.join(command.notes) if command.notes else '一般門檻'}",
                "",
            ]
        )
    if command_result and command_result.get("text"):
        lines.extend(["命令結果：", str(command_result.get("text") or ""), ""])
    lines.extend(render_product_status_lines(product_status))
    lines.extend(render_assessment_lines(local_ai_assessment))
    lines.extend(render_framework_lines(framework))
    if command and command.symbols and not reports:
        lines.extend(
            [
                f"- 找不到指定持倉：{', '.join(command.symbols)}",
                "",
                "持倉監測：",
                "- 沒有符合命令條件的持倉。",
            ]
        )
        return "\n".join(lines)

    if warnings:
        for item in warnings[:10]:
            severity = SEVERITY_LABELS.get(str(item.get("severity")), "提示")
            detail = str(item.get("detail") or "")
            action = str(item.get("action") or "")
            tail = f" {action}" if action else ""
            lines.append(f"- [{severity}] {item.get('title')}: {detail}{tail}")
    else:
        lines.append("- 目前沒有觸發本地風險規則。")

    lines.extend(["", "持倉監測："])
    for report in reports[:12]:
        lines.append(f"- {_holding_line(report)}")
    if len(reports) > 12:
        lines.append(f"- 其餘 {len(reports) - 12} 檔已納入計算，請查看 JSON 結果。")

    return "\n".join(lines)


def holding_from_dict(raw: dict[str, Any]) -> core.Holding:
    return core.Holding(
        symbol=str(raw.get("symbol") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        market=str(raw.get("market") or "").strip().upper(),
        asset_type=str(raw.get("asset_type") or "").strip(),
        quantity=_float_or_none(raw.get("quantity")) or 0.0,
        average_cost=_float_or_none(raw.get("average_cost")),
        currency=str(raw.get("currency") or "").strip().upper(),
        source_row=_int_or_none(raw.get("source_row")),
    )


def _research_inputs_from_holding(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy only the explicit, versioned research inputs understood by this engine."""

    output: dict[str, Any] = {}
    container = (
        raw.get("research_inputs")
        if isinstance(raw.get("research_inputs"), dict)
        else {}
    )
    for input_key, _reason in RESEARCH_COMPONENT_INPUTS.values():
        value = container.get(input_key)
        if value is None:
            value = raw.get(input_key)
        if isinstance(value, dict):
            output[input_key] = dict(value)
    return output


def _warning(
    severity: str,
    code: str,
    title: str,
    detail: str,
    *,
    symbol: str = "",
    action: str = "",
    metric: float | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "symbol": symbol,
        "title": title,
        "detail": detail,
        "action": action,
        "metric": core.round_number(metric),
    }


def _concentration_warnings(
    reports: Sequence[dict[str, Any]],
    config: RiskRuleConfig,
) -> list[dict[str, Any]]:
    weighted: list[tuple[dict[str, Any], float]] = []
    for report in reports:
        value = _float_or_none(report.get("market_value"))
        if value is None:
            value = _float_or_none(report.get("cost_basis"))
        if value is not None and value > 0:
            weighted.append((report, value))

    total = sum(value for _, value in weighted)
    if total <= 0:
        return []

    warnings: list[dict[str, Any]] = []
    for report, value in weighted:
        percent = value / total * 100
        rule_profile = asset_rule_profile(report, config)
        symbol = str(report.get("symbol") or "-")
        name = str(report.get("name") or "")
        label = f"{symbol} {name}".strip()
        if percent >= float(rule_profile["concentration_critical_percent"]):
            warnings.append(
                _warning(
                    "critical",
                    "concentration_critical",
                    f"{label} 部位集中 {percent:.2f}%",
                    f"此持倉估算占可計算總市值 {percent:.2f}%。",
                    symbol=symbol,
                    metric=percent,
                    action="請優先確認單一持倉風險是否符合你的資金規則。",
                )
            )
            warnings[-1]["rule_profile"] = rule_profile["key"]
        elif percent >= float(rule_profile["concentration_warning_percent"]):
            warnings.append(
                _warning(
                    "warning",
                    "concentration_warning",
                    f"{label} 部位集中 {percent:.2f}%",
                    f"此持倉估算占可計算總市值 {percent:.2f}%。",
                    symbol=symbol,
                    metric=percent,
                    action="請追蹤集中度是否持續升高。",
                )
            )
            warnings[-1]["rule_profile"] = rule_profile["key"]
    return warnings


def _portfolio_stale_warning(
    portfolio: dict[str, Any] | None,
    now: datetime,
    config: RiskRuleConfig,
) -> dict[str, Any] | None:
    if not portfolio:
        return None
    imported_at = _parse_datetime(str(portfolio.get("imported_at") or ""))
    if imported_at is None:
        return None
    age_hours = (now - imported_at).total_seconds() / 3600
    if age_hours <= config.stale_portfolio_hours:
        return None
    return _warning(
        "info",
        "portfolio_stale",
        "持倉檔案可能過舊",
        f"距離上次匯入約 {age_hours:.1f} 小時，持倉數量或成本可能已改變。",
        action="請重新匯入最新 Excel 後再執行監測。",
        metric=age_hours,
    )


def _quote_coverage_warnings(reports: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(reports) <= 1:
        return []
    failed = sum(1 for report in reports if report.get("status") != "quoted")
    if failed == 0 or failed / len(reports) < 0.5:
        return []
    return [
        _warning(
            "warning",
            "quote_coverage_low",
            "報價覆蓋率偏低",
            f"{failed}/{len(reports)} 檔持倉未取得報價，總體風險可能低估。",
            action="請檢查市場代碼、股票代號格式與網路連線。",
            metric=failed / len(reports) * 100,
        )
    ]


def _quote_validation_warnings(report: dict[str, Any]) -> list[dict[str, Any]]:
    validation = (
        report.get("quote_validation")
        if isinstance(report.get("quote_validation"), dict)
        else {}
    )
    issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
    symbol = str(report.get("symbol") or "")
    output: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "")
        if code in {"quote_single_source", "quote_timestamp_missing"}:
            severity = "info"
        else:
            severity = str(issue.get("severity") or "warning")
        output.append(
            _warning(
                severity,
                code,
                str(issue.get("title") or "報價驗證提醒"),
                str(issue.get("detail") or ""),
                symbol=symbol,
                metric=_float_or_none(issue.get("metric")),
                action=str(issue.get("action") or ""),
            )
        )
    return output


def _cost_detail(report: dict[str, Any]) -> str:
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
    currency = str(quote.get("currency") or report.get("currency") or "")
    return (
        f"現價 {_format_money(quote.get('price'), currency)}，"
        f"平均成本 {_format_money(report.get('average_cost'), currency)}，"
        f"未實現損益 {_format_money(report.get('unrealized_pnl'), currency)}。"
    )


def _holding_line(report: dict[str, Any]) -> str:
    symbol = str(report.get("symbol") or "-")
    market = str(report.get("market") or "-")
    quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
    currency = str(quote.get("currency") or report.get("currency") or "")
    if report.get("status") == "quoted":
        status = "報價已驗證" if report.get("trusted_quote") else "報價成功"
    elif report.get("status") == "quote_untrusted":
        status = "報價未驗證"
    else:
        status = "報價失敗"
    price = _format_money(quote.get("price"), currency)
    pnl_percent = _float_or_none(report.get("unrealized_pnl_percent"))
    pnl_text = "-" if pnl_percent is None else f"{pnl_percent:.2f}%"
    value_text = _format_money(report.get("market_value"), currency)
    return f"{symbol} / {market} / {status} / 現價 {price} / 市值 {value_text} / 損益 {pnl_text}"


def _format_money(value: Any, currency: str = "") -> str:
    number = _float_or_none(value)
    if number is None:
        return "-"
    suffix = f" {currency}" if currency else ""
    return f"{number:,.4f}".rstrip("0").rstrip(".") + suffix


def _format_number(value: Any) -> str:
    number = _float_or_none(value)
    if number is None:
        return "-"
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        local_timezone = datetime.now().astimezone().tzinfo or timezone.utc
        return parsed.replace(tzinfo=local_timezone)
    return parsed.astimezone()


def _provider_order(value: str) -> list[str] | None:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the AI Investment Manager local risk AI."
    )
    parser.add_argument("portfolio", nargs="?", help="CSV, JSON, or XLSX portfolio file.")
    parser.add_argument("--portfolio", dest="portfolio_option", help="Portfolio file path.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run without live quote providers.",
    )
    parser.add_argument(
        "--provider-order",
        default="",
        help="Comma-separated quote provider order for live mode.",
    )
    parser.add_argument("--instruction", default="", help="Local risk AI command text.")
    parser.add_argument("--output", help="Optional JSON output file.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args(argv)

    raw_portfolio = args.portfolio_option or args.portfolio
    if not raw_portfolio:
        parser.error("portfolio file is required")

    result = analyze_portfolio_file(
        Path(raw_portfolio).expanduser().resolve(),
        live_quotes=not bool(args.offline),
        provider_order=_provider_order(str(args.provider_order or "")),
        instruction=str(args.instruction or ""),
    )

    if args.output:
        Path(args.output).expanduser().resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["content"])
    return 0
