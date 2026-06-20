from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import investment_manager_core
from . import local_risk_ai
from .investment_browser_session import AGENTS, InvestmentBrowserSession
from .investment_repository import InvestmentWatchRepository


TOOL_ROOT = Path(__file__).resolve().parents[4]

# Compatibility exports for older dynamic loaders that treated the investment
# watch service module as the former investment manager entry.
Holding = investment_manager_core.Holding
InvestmentManagerError = investment_manager_core.InvestmentManagerError
QuoteProviderError = investment_manager_core.QuoteProviderError
load_portfolio = investment_manager_core.load_portfolio
load_json_portfolio = investment_manager_core.load_json_portfolio
load_csv_portfolio = investment_manager_core.load_csv_portfolio
load_xlsx_portfolio = investment_manager_core.load_xlsx_portfolio
scan_xlsx_workbook = investment_manager_core.scan_xlsx_workbook
create_snapshot = investment_manager_core.create_snapshot
run_manager = investment_manager_core.run_manager
provider_registry = investment_manager_core.provider_registry
quote_holding = investment_manager_core.quote_holding
market_status = investment_manager_core.market_status
utc_now = investment_manager_core.utc_now
main = investment_manager_core.main


def _resolve_tool_root(project_root: Path) -> Path:
    candidate = project_root / "platform_tools" / "ai-assistant"
    if candidate.exists():
        return candidate.resolve()
    return project_root.resolve()


class InvestmentWatchService:
    VERSION = "0.1.0"
    COMMANDS = {
        "investment_watch_get_state",
        "investment_watch_import_portfolio",
        "investment_watch_clear_state",
        "investment_watch_open_agent",
        "investment_watch_run_primary_agent",
        "investment_watch_run_all_primary_agents",
        "investment_watch_run_local_risk_ai",
        "investment_watch_gemini_quote_search",
        "investment_watch_gpt_extract_filter",
        "investment_watch_run_pipeline",
    }

    def __init__(self, project_root: Path, browser_session: Any | None = None) -> None:
        self.project_root = project_root.resolve()
        self.tool_root = _resolve_tool_root(project_root)
        self.repository = InvestmentWatchRepository(self.tool_root)
        self.browser = InvestmentBrowserSession(self.project_root, browser_session)

    @property
    def workspace(self) -> Any:
        tool_root = self.tool_root

        class Workspace:
            workspace_root = tool_root

        return Workspace()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        await self.browser.shutdown()

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        handlers = {
            "investment_watch_get_state": self._get_state,
            "investment_watch_import_portfolio": self._import_portfolio,
            "investment_watch_clear_state": self._clear_state,
            "investment_watch_open_agent": self._open_agent,
            "investment_watch_run_primary_agent": self._run_primary_agent,
            "investment_watch_run_all_primary_agents": self._run_all_primary_agents,
            "investment_watch_run_local_risk_ai": self._run_local_risk_ai,
            "investment_watch_gemini_quote_search": self._gemini_quote_search,
            "investment_watch_gpt_extract_filter": self._gpt_extract_filter,
            "investment_watch_run_pipeline": self._run_pipeline,
        }
        try:
            result = await handlers[command](payload)
        except Exception as exc:
            result = {"ok": False, "message": str(exc)}
        return f"{command}_result", result

    async def _get_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "version": self.VERSION,
            "agents": self._agents_state(),
            "state": self.repository.load_state(),
            "tool_root": str(self.tool_root),
            "state_path": str(self.repository.state_path),
            "browser_profile_path": str(self.browser.shared_profile_dir),
            "safety": {
                "captcha": "若 Edge 顯示 CAPTCHA 或 Cloudflare，請人工完成驗證後重試。",
                "session": "登入狀態保存在此工具專屬 Edge Profile。",
                "fragility": "網頁改版時會回報 selector/擷取失敗，資料仍保存在本地記憶。",
            },
        }

    async def _import_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(payload.get("path", "")).strip()
        if not raw_path:
            return {"ok": False, "message": "請選擇 Excel 持股檔。"}
        source = Path(raw_path).expanduser().resolve()
        workbook_scan = None
        if source.suffix.casefold() in investment_manager_core.XLSX_EXTENSIONS:
            workbook_scan = investment_manager_core.scan_xlsx_workbook(source)
        holdings = investment_manager_core.load_portfolio(source)
        holding_dicts = [self._holding_to_dict(holding) for holding in holdings]
        state = self.repository.save_portfolio(
            source,
            holding_dicts,
            workbook_scan=workbook_scan,
        )
        local_risk_result = self._run_local_risk_ai_for_state(
            state,
            {"trigger": "portfolio_import"},
        )
        if isinstance(local_risk_result.get("state"), dict):
            state = local_risk_result["state"]
        selected_sheet = workbook_scan.get("selected_sheet") if workbook_scan else None
        sheet_note = ""
        if isinstance(selected_sheet, dict):
            sheet_note = (
                f"（{selected_sheet.get('sheet_name')}，"
                f"第 {selected_sheet.get('header_row_number')} 列欄位）"
            )
        return {
            "ok": True,
            "message": f"已匯入 {len(holding_dicts)} 筆持股{sheet_note}。",
            "state": state,
            "workbook_scan": workbook_scan,
            "product_status": local_risk_result.get("product_status"),
            "local_risk_ai": local_risk_result,
        }

    async def _clear_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return {
            "ok": True,
            "message": "舊資料已刪除。",
            "state": self.repository.clear_state(),
        }

    async def _open_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip().lower()
        if agent_id not in AGENTS:
            return {"ok": False, "message": f"不支援的 AI：{agent_id}"}
        result = await self.browser.open_agent(agent_id)
        return {**result, "agents": self._agents_state()}

    async def _run_primary_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip().lower()
        if agent_id not in AGENTS:
            return {"ok": False, "message": f"不支援的 AI：{agent_id}"}
        state = self.repository.load_state()
        prompt = self._primary_agent_prompt(agent_id, state)
        result = await self.browser.send_prompt(agent_id, prompt)
        run = self.repository.add_ai_run(
            role="primary_analysis",
            provider=agent_id,
            prompt=prompt,
            status=str(result.get("status") or "failed"),
            content=str(result.get("content") or ""),
            error=str(result.get("error") or ""),
        )
        agent_name = AGENTS[agent_id]["name"]
        return {
            "ok": result.get("status") == "completed",
            "message": self._status_message(f"{agent_name} 主要分析", result),
            "run": run,
            "state": self.repository.load_state(),
        }

    async def _run_all_primary_agents(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        runs: list[dict[str, Any]] = []
        completed = 0
        waiting = 0
        failed = 0
        for agent_id in AGENTS:
            result = await self._run_primary_agent({"agent_id": agent_id})
            run = result.get("run")
            if isinstance(run, dict):
                runs.append(run)
            if result.get("ok"):
                completed += 1
            elif run and run.get("status") == "waiting_verification":
                waiting += 1
            else:
                failed += 1
        return {
            "ok": completed > 0 and failed == 0 and waiting == 0,
            "message": f"全部主要 AI 已執行：完成 {completed}、待驗證 {waiting}、失敗 {failed}。",
            "runs": runs,
            "state": self.repository.load_state(),
        }

    async def _run_local_risk_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        return self._run_local_risk_ai_for_state(state, payload)

    def _run_local_risk_ai_for_state(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not state.get("holdings"):
            return {
                "ok": False,
                "message": "請先匯入 Excel 持股檔。",
                "state": state,
            }
        instruction = str(payload.get("instruction") or payload.get("command") or "").strip()
        live_quotes = bool(payload.get("live_quotes", not bool(instruction)))
        prompt = "本地輔助AI：自動監測股價、持倉成本與集中度風險（本地模式，非投資建議）"
        if instruction:
            prompt = f"{prompt}\n使用者命令：{instruction}"
        try:
            result = local_risk_ai.analyze_state(
                state,
                live_quotes=live_quotes,
                instruction=instruction,
            )
            run = self.repository.add_ai_run(
                role="local_risk_monitor",
                provider="local-risk-ai",
                prompt=prompt,
                status="completed",
                content=str(result.get("content") or ""),
                error="",
            )
            summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
            product_status = (
                result.get("product_status")
                if isinstance(result.get("product_status"), dict)
                else None
            )
            state_after_run = self.repository.save_local_ai_result(
                product_status,
                summary,
                result.get("risk_warnings") if isinstance(result.get("risk_warnings"), list) else [],
                result.get("command_result")
                if isinstance(result.get("command_result"), dict)
                else None,
            )
            status_label = ""
            if isinstance(product_status, dict):
                status_label = str(product_status.get("state_label") or "").strip()
            message = f"本地輔助AI完成：{summary.get('warning_count', 0)} 項風險預告。"
            if status_label:
                message = f"{message} 狀態：{status_label}。"
            return {
                "ok": True,
                "message": message,
                "run": run,
                "summary": summary,
                "product_status": product_status,
                "risk_warnings": result.get("risk_warnings", []),
                "local_risk_ai": result,
                "state": state_after_run,
            }
        except Exception as exc:
            run = self.repository.add_ai_run(
                role="local_risk_monitor",
                provider="local-risk-ai",
                prompt=prompt,
                status="failed",
                content="",
                error=str(exc),
            )
            return {
                "ok": False,
                "message": str(exc),
                "run": run,
                "state": self.repository.load_state(),
            }

    async def _gemini_quote_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        state = self.repository.load_state()
        prompt = self._gemini_prompt(state)
        result = await self.browser.send_prompt("gemini", prompt)
        run = self.repository.add_ai_run(
            role="quote_search",
            provider="gemini",
            prompt=prompt,
            status=str(result.get("status") or "failed"),
            content=str(result.get("content") or ""),
            error=str(result.get("error") or ""),
        )
        return {
            "ok": result.get("status") == "completed",
            "message": self._status_message("Gemini 報價與搜尋", result),
            "run": run,
            "state": self.repository.load_state(),
        }

    async def _gpt_extract_filter(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_text = str(payload.get("raw_text") or "").strip()
        if not raw_text:
            raw_text = self.repository.latest_ai_content("quote_search")
        if not raw_text:
            return {"ok": False, "message": "尚無 Gemini 搜尋結果可供 GPT 萃取。"}
        state = self.repository.load_state()
        prompt = self._gpt_prompt(state, raw_text)
        result = await self.browser.send_prompt("chatgpt", prompt)
        run = self.repository.add_ai_run(
            role="feature_extract",
            provider="chatgpt",
            prompt=prompt,
            status=str(result.get("status") or "failed"),
            content=str(result.get("content") or ""),
            error=str(result.get("error") or ""),
        )
        return {
            "ok": result.get("status") == "completed",
            "message": self._status_message("GPT 特徵萃取", result),
            "run": run,
            "state": self.repository.load_state(),
        }

    async def _run_pipeline(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run_all_primary_agents(payload)

    def _agents_state(self) -> list[dict[str, Any]]:
        return [
            {
                "agent_id": agent_id,
                **agent,
                "primary": True,
                "profile_path": str(self.browser.shared_profile_dir),
            }
            for agent_id, agent in AGENTS.items()
        ]

    @staticmethod
    def _holding_to_dict(holding: Any) -> dict[str, Any]:
        return {
            "symbol": holding.symbol,
            "name": holding.name,
            "market": holding.market,
            "asset_type": holding.asset_type,
            "quantity": holding.quantity,
            "average_cost": holding.average_cost,
            "currency": holding.currency,
            "source_row": holding.source_row,
        }

    @staticmethod
    def _status_message(label: str, result: dict[str, Any]) -> str:
        status = str(result.get("status") or "")
        if status == "completed":
            return f"{label}完成。"
        if status == "waiting_verification":
            return str(result.get("error") or "等待人工驗證。")
        return str(result.get("error") or f"{label}失敗。")

    @staticmethod
    def _compact_json(value: Any, limit: int = 12000) -> str:
        text = json.dumps(value, ensure_ascii=False, indent=2)
        return text if len(text) <= limit else text[:limit] + "\n...truncated..."

    def _gemini_prompt(self, state: dict[str, Any]) -> str:
        holdings = state.get("holdings", [])
        return "\n".join(
            [
                "你是投資看盤資料搜尋員，只負責報價與搜尋，不做投資建議。",
                "請根據我的持股清單，搜尋每個標的的最新價格、漲跌幅、重要新聞、最新財報重點或公告。",
                "請輸出可被下一階段 GPT 萃取的原始資料，務必附上來源名稱與時間；若無法確認，標記為待確認。",
                "格式：",
                "1. Market snapshot",
                "2. Holding raw facts",
                "3. News / filings / earnings raw facts",
                "4. Unknowns / needs verification",
                "",
                "共用記憶：",
                str(state.get("shared_memory") or ""),
                "",
                "持股原始資料：",
                self._compact_json(holdings),
            ]
        )

    def _primary_agent_prompt(self, agent_id: str, state: dict[str, Any]) -> str:
        agent = AGENTS[agent_id]
        latest_runs = [
            {
                "role": run.get("role"),
                "provider": run.get("provider"),
                "status": run.get("status"),
                "content": str(run.get("content") or "")[:3000],
                "error": run.get("error"),
            }
            for run in state.get("ai_runs", [])[:8]
        ]
        if agent_id == "gemini":
            return self._gemini_prompt(state)
        if agent_id == "chatgpt":
            raw_text = "\n\n".join(
                str(run.get("content") or "")
                for run in state.get("ai_runs", [])
                if run.get("content") and run.get("provider") != "chatgpt"
            )
            return self._gpt_prompt(state, raw_text or "目前尚無其他 AI 回覆，請直接依持股資料做特徵萃取與過濾。")
        return "\n".join(
            [
                "你是投資看盤工作台的主要 AI，不是輔助 AI。",
                f"AI 名稱：{agent['name']}",
                f"主要職責：{agent['role']}",
                "",
                "請依你的主要職責分析下列持股，輸出可被其他主要 AI 交叉比對的資料。",
                "不要提供個人化買賣指令；請標示資料來源、時間、信心程度與缺漏。",
                "請用繁體中文，並使用以下格式：",
                "1. Key facts",
                "2. Portfolio impact",
                "3. Evidence and sources",
                "4. Risk flags",
                "5. Missing data",
                "6. Machine JSON",
                "",
                "共用記憶：",
                str(state.get("shared_memory") or ""),
                "",
                "我的持股狀況：",
                str(state.get("portfolio_memory") or ""),
                "",
                "最近其他主要 AI 結果：",
                self._compact_json(latest_runs),
            ]
        )

    def _gpt_prompt(self, state: dict[str, Any], raw_text: str) -> str:
        return "\n".join(
            [
                "你是投資看盤的特徵萃取與過濾引擎。",
                "你的任務不是報價、不是搜尋、不是投資建議；只負責把 Gemini 的原始資料去雜訊，整理為標準條列數據。",
                "請剔除空話、行銷語、重複內容與未能對應持股的資訊。",
                "輸出固定格式：",
                "- Portfolio features: 每檔 symbol/name/market/price/change/news_count/source_count/status",
                "- Financial features: revenue/eps/margin/guidance/debt/cash/YoY/QoQ，無資料填 null",
                "- News features: event/date/impact_direction/confidence/source",
                "- Risk flags: 條列具體風險與觸發原因",
                "- Missing data: 條列仍需人工確認的欄位",
                "- Machine JSON: 最後提供一段 JSON 陣列，key 使用英文 snake_case",
                "",
                "我的持股記憶：",
                str(state.get("portfolio_memory") or ""),
                "",
                "共用記憶：",
                str(state.get("shared_memory") or ""),
                "",
                "Gemini 原始資料：",
                raw_text[:20000],
            ]
        )
