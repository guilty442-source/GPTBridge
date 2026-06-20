"""Standalone local risk AI engine.

This module is intentionally deterministic: it does not call ChatGPT, Gemini,
Claude, or any browser session. Live mode only uses quote providers from the
investment manager core; offline mode evaluates portfolio and cost data only.
"""

from __future__ import annotations

import argparse
import json
import re
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
RATING_STANDARDS: tuple[dict[str, str], ...] = (
    {"range": "90-100", "rating": "★★★★★", "label": "高度看好"},
    {"range": "80-89", "rating": "★★★★☆", "label": "偏多"},
    {"range": "70-79", "rating": "★★★☆☆", "label": "中性偏多"},
    {"range": "60-69", "rating": "★★☆☆☆", "label": "觀望"},
    {"range": "60 以下", "rating": "★☆☆☆☆", "label": "高風險"},
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
) -> dict[str, Any]:
    holdings = [holding_from_dict(item) for item in state.get("holdings", [])]
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
    holdings = core.load_portfolio(portfolio_file)
    return analyze_holdings(
        holdings,
        portfolio={
            "source_path": str(portfolio_file),
            "file_name": portfolio_file.name,
            "holding_count": len(holdings),
        },
        live_quotes=live_quotes,
        provider_order=provider_order,
        now=now,
        quote_function=quote_function,
        config=config,
        instruction=instruction,
    )


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
) -> dict[str, Any]:
    checked_at = now or core.utc_now()
    command = parse_command(instruction, config)
    effective_live_quotes = live_quotes if command.live_quotes is None else command.live_quotes
    effective_holdings = filter_holdings_by_command(holdings, command)
    order = list(provider_order or core.DEFAULT_PROVIDER_ORDER)
    quote_runner = quote_function or core.quote_holding
    providers = core.provider_registry() if effective_live_quotes else {}
    reports: list[dict[str, Any]] = []
    framework = analysis_framework(effective_holdings, command)

    for holding in effective_holdings:
        if effective_live_quotes:
            quote, attempts = quote_runner(holding, providers, order, checked_at)
        else:
            quote = None
            attempts = [
                core.QuoteAttempt(
                    provider="local-offline",
                    ok=False,
                    message="Live quote disabled for local offline mode.",
                )
            ]
        reports.append(core.holding_to_report(holding, quote, attempts, checked_at))

    warnings = evaluate_risk_warnings(reports, portfolio, checked_at, command.config)
    summary = build_summary(reports, warnings)
    local_ai_assessment = build_local_ai_assessment(
        reports,
        warnings,
        framework,
        command,
        effective_live_quotes,
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
    return {
        "mode": "local-live" if effective_live_quotes else "local-offline",
        "generated_at": checked_at.isoformat(),
        "instruction": command.instruction,
        "command": command_to_dict(command),
        "analysis_framework": framework,
        "portfolio": portfolio,
        "summary": summary,
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
    "summary": "摘要模式",
    "help": "命令說明",
}


def command_intent(lowered_instruction: str) -> str:
    if any(token in lowered_instruction for token in ("help", "說明", "怎麼用", "指令", "命令格式")):
        return "help"
    if any(token in lowered_instruction for token in ("評分", "分數", "權重", "星等", "rating", "score")):
        return "score"
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
    )
    for section, keywords in section_keywords:
        if any(keyword in lowered_instruction for keyword in keywords):
            sections.append(section)
    defaults = {
        "monitor": ("summary", "warnings", "holdings"),
        "risk": ("warnings", "holdings"),
        "allocation": ("allocation", "warnings"),
        "score": ("score", "allocation"),
        "summary": ("summary", "warnings"),
        "help": ("help",),
    }
    if not sections:
        return defaults[intent]
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
    if any(token in lowered for token in ("即時", "live", "抓報價", "更新報價")):
        live_quotes = True
        notes.append("即時報價")

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
        "notes": list(command.notes),
        "portfolio_score": local_ai_assessment.get("portfolio_score"),
        "portfolio_rating": local_ai_assessment.get("portfolio_rating"),
        "risk_level": local_ai_assessment.get("risk_level"),
        "assessments": list(local_ai_assessment.get("assessments", [])),
        "next_actions": list(local_ai_assessment.get("next_actions", [])),
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
        f"報價模式：{'即時報價' if live_quotes else '離線'}",
    ]
    if command.symbols:
        lines.append(f"聚焦標的：{', '.join(command.symbols)}")
    if missing_symbols:
        lines.append(f"找不到指定持倉：{', '.join(missing_symbols)}")
    if command.notes:
        lines.append(f"套用條件：{', '.join(command.notes)}")

    if "summary" in command.sections:
        lines.append(
            "摘要："
            f"{summary.get('holding_count', 0)} 檔，"
            f"{summary.get('warning_count', 0)} 項風險預告，"
            f"重大 {summary.get('critical_count', 0)} / 注意 {summary.get('attention_count', 0)}。"
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
                f"- 本地AI投組評分：{score_text} 分 "
                f"{rating.get('rating', '')}（{rating.get('label', '-')}）"
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
        "- 風險：只看 AAPL 跌破成本 3% 停損 8%",
        "- 配置：配置 集中度 25% 防禦",
        "- 評分：評分 TSLA 高估值 通膨",
        "- 報價：即時 AAPL 盤中 2%",
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
    risk_level = str(local_ai_assessment.get("risk_level") or "")
    risk_level_label = str(local_ai_assessment.get("risk_level_label") or "-")
    next_actions = [
        str(item)
        for item in local_ai_assessment.get("next_actions", [])
        if str(item or "").strip()
    ]

    if holding_count <= 0:
        state = "empty"
        state_label = "待匯入"
        recommendation = "先上傳 Excel 持股檔，本地AI會自動建立監測基準。"
    elif critical_count > 0 or risk_level == "critical" or portfolio_score < 60:
        state = "critical"
        state_label = "重大風險"
        recommendation = (
            next_actions[0]
            if next_actions
            else "先檢查停損線、單一持股上限與資料是否過期。"
        )
    elif warning_count > 0 or portfolio_score < 75:
        state = "attention"
        state_label = "需要注意"
        recommendation = (
            next_actions[0]
            if next_actions
            else "維持監測，新增資金先等待風險報酬比改善。"
        )
    else:
        state = "ready"
        state_label = "運行正常"
        recommendation = "本地AI已自動監測持倉，維持紀律並在新資訊出現時重新評估。"

    return {
        "state": state,
        "state_label": state_label,
        "score": int(round(portfolio_score)) if holding_count else 0,
        "risk_level": risk_level or "unknown",
        "risk_level_label": risk_level_label,
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
        "watch_status_label": "即時報價監測" if live_quotes else "本地離線監測",
        "offline_mode": not live_quotes,
        "has_portfolio": holding_count > 0,
        "portfolio_count": holding_count,
        "warning_count": warning_count,
        "critical_count": critical_count,
        "quoted_count": int(summary.get("quoted_count") or 0),
        "failed_quote_count": int(summary.get("failed_quote_count") or 0),
        "coverage_label": (
            f"{int(summary.get('quoted_count') or 0)} / {holding_count} 報價成功"
            if live_quotes
            else "離線資料評估"
        ),
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
                f"評分 {first_symbol} 高估值 通膨",
                f"只看 {first_symbol} 跌破成本 3% 停損 8%",
            ]
        )

    active = framework.get("active_weighting") if isinstance(framework, dict) else {}
    if isinstance(active, dict) and active.get("profile") == "高估值防禦模式":
        suggestions.append("高估值 通膨 防禦")
    if not live_quotes:
        suggestions.append("即時 盤中 2%")
    if any(str(item.get("severity") or "") == "critical" for item in warnings):
        suggestions.append("風險 預警 停損 8%")
    if command.intent != "summary":
        suggestions.append("摘要 持股 離線")
    suggestions.extend(["配置 集中度 25%", "評分 權重"])

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
    portfolio_score = _weighted_portfolio_score(assessments)
    portfolio_rating = rating_for_score(portfolio_score)
    risk_level, risk_level_label = portfolio_risk_level(portfolio_score, warnings, assessments)
    top_risks = [str(item.get("title") or item.get("code") or "") for item in warnings[:6]]
    next_actions = build_next_actions(assessments, warnings, portfolio_score, live_quotes)
    return {
        "portfolio_score": core.round_number(portfolio_score, 2),
        "portfolio_rating": portfolio_rating,
        "risk_level": risk_level,
        "risk_level_label": risk_level_label,
        "summary": (
            f"投組本地AI評分 {portfolio_score:.0f} 分，"
            f"{portfolio_rating['rating']}（{portfolio_rating['label']}），"
            f"風險層級：{risk_level_label}。"
        ),
        "assessments": assessments,
        "top_risks": top_risks,
        "next_actions": next_actions,
        "memory_update": build_memory_update(
            portfolio_score,
            portfolio_rating,
            risk_level_label,
            top_risks,
            next_actions,
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
    score = sum(float(item["score"]) for item in components)
    rating = rating_for_score(score)
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
        "rating": rating["rating"],
        "rating_label": rating["label"],
        "rating_range": rating["range"],
        "risk_level": risk_level,
        "risk_level_label": risk_level_label,
        "position_weight_percent": core.round_number(position_weight, 2),
        "confidence": confidence,
        "score_components": components,
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
    pnl_percent = _float_or_none(report.get("unrealized_pnl_percent"))
    change_percent = _float_or_none(quote.get("change_percent")) if quote else None
    high_volatility = report_is_high_volatility(report)
    quantity = _float_or_none(report.get("quantity"))
    missing_cost = _float_or_none(report.get("average_cost")) is None
    quote_failed = report.get("status") != "quoted"
    components: list[dict[str, Any]] = []

    for item in model:
        name = str(item.get("name") or "")
        max_score = _float_or_none(item.get("max_score")) or 0.0
        ratio = 0.68
        note = "本地模式使用持倉、成本、報價與風險規則推估。"

        if name == "基本面":
            ratio = 0.72
            note = "尚未連接財報資料庫，先以資料完整度與風險規則作保守代理。"
            if high_volatility:
                ratio -= 0.06
            if missing_cost:
                ratio -= 0.05
        elif name == "成長性":
            ratio = 0.68
            note = "本地模式無營收與 EPS 時序資料，暫以價格相對成本趨勢作輔助。"
            if pnl_percent is not None and pnl_percent > 0:
                ratio += 0.06
            if pnl_percent is not None and pnl_percent <= command.config.warning_loss_percent:
                ratio -= 0.07
            if high_volatility:
                ratio -= 0.03
        elif name == "獲利能力":
            ratio = 0.70
            note = "以未實現損益作持倉層級獲利能力代理，後續可接入毛利率與營益率。"
            if pnl_percent is None:
                ratio -= 0.08
            elif pnl_percent >= command.config.large_gain_percent:
                ratio += 0.12
            elif pnl_percent > 0:
                ratio += 0.06
            elif pnl_percent <= command.config.critical_loss_percent:
                ratio -= 0.20
            elif pnl_percent <= command.config.warning_loss_percent:
                ratio -= 0.10
        elif name == "財務安全":
            ratio = 0.76
            note = "以成本資料完整度、報價可用性與部位集中度作安全邊際代理。"
            if quantity is None or quantity <= 0:
                ratio -= 0.18
            if missing_cost:
                ratio -= 0.10
            if "concentration_critical" in codes:
                ratio -= 0.16
            elif "concentration_warning" in codes:
                ratio -= 0.08
            if quote_failed:
                ratio -= 0.08 if live_quotes else 0.04
        elif name == "技術面":
            ratio = 0.68
            note = "以盤中漲跌與報價狀態作技術面代理，均線與量價仍需外部資料補強。"
            if quote is None:
                ratio -= 0.10
            elif change_percent is not None and change_percent <= command.config.intraday_drop_percent:
                ratio -= 0.18
            elif change_percent is not None and change_percent > 0:
                ratio += 0.06
            elif change_percent is not None and change_percent < 0:
                ratio -= 0.04
        elif name == "籌碼":
            ratio = 0.60
            note = "本地模式未接法人、融資融券與大股東資料，採保守中性分。"
            if quote_failed:
                ratio -= 0.03
        elif name == "估值":
            ratio = 0.66
            note = "尚未接 P/E、P/B、EV/EBITDA 與 DCF，先用成本偏離提醒估值重估。"
            if pnl_percent is None:
                ratio -= 0.06
            elif pnl_percent >= command.config.large_gain_percent:
                ratio -= 0.06
            elif pnl_percent <= command.config.critical_loss_percent:
                ratio -= 0.08
        elif name == "風險":
            ratio = 0.82
            note = "依本地警示、集中度、報價覆蓋率與高波動屬性扣分。"
            ratio -= critical_count * 0.20
            ratio -= warning_count * 0.10
            ratio -= info_count * 0.03
            if high_volatility:
                ratio -= 0.10

        score = max_score * clamp(ratio, 0.15, 0.98)
        components.append(
            {
                "name": name,
                "max_score": core.round_number(max_score, 2),
                "score": core.round_number(score, 2),
                "note": note,
            }
        )

    return components


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
    if "quote_failed" in codes or confidence.get("label") == "低":
        return "先補齊報價、成本與市場欄位，確認資料品質後再做進出場判斷。"
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
) -> dict[str, Any]:
    return {
        "kind": "investment-local-risk-ai",
        "title": "本地AI投組監測",
        "content": "\n".join(
            [
                f"本地AI投組評分：{portfolio_score:.0f} 分 "
                f"{portfolio_rating.get('rating', '')}（{portfolio_rating.get('label', '-')}）",
                f"風險層級：{risk_level_label}",
                "主要風險：" + ("；".join(item for item in top_risks if item) or "目前無重大風險"),
                "下一步：" + ("；".join(item for item in next_actions if item) or "維持監測"),
            ]
        ),
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
    if not assessments:
        return 0.0
    weighted_total = 0.0
    weight_sum = 0.0
    for item in assessments:
        score = _float_or_none(item.get("score"))
        weight = _float_or_none(item.get("position_weight_percent"))
        if score is None or weight is None or weight <= 0:
            continue
        weighted_total += score * weight
        weight_sum += weight
    if weight_sum > 0:
        return weighted_total / weight_sum
    valid_scores = [
        score
        for score in (_float_or_none(item.get("score")) for item in assessments)
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
        return list(holdings)
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
        quantity = _float_or_none(report.get("quantity"))
        average_cost = _float_or_none(report.get("average_cost"))
        pnl_percent = _float_or_none(report.get("unrealized_pnl_percent"))
        change_percent = _float_or_none(quote.get("change_percent")) if quote else None
        price = _float_or_none(quote.get("price")) if quote else None

        if report.get("status") != "quoted":
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
            if pnl_percent <= config.critical_loss_percent:
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
            elif pnl_percent <= config.warning_loss_percent:
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

        if change_percent is not None and change_percent <= config.intraday_drop_percent:
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

    return {
        "holding_count": len(reports),
        "quoted_count": quoted_count,
        "failed_quote_count": len(reports) - quoted_count,
        "warning_count": len(warnings),
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
            f"{product_status.get('watch_status_label', '-')}"
        ),
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
        f"生成時間：{now.astimezone(timezone.utc).isoformat()}",
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
        symbol = str(report.get("symbol") or "-")
        name = str(report.get("name") or "")
        label = f"{symbol} {name}".strip()
        if percent >= config.concentration_critical_percent:
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
        elif percent >= config.concentration_warning_percent:
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
    status = "報價成功" if report.get("status") == "quoted" else "報價失敗"
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
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
