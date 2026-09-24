import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocalBackendSocket } from "./backendSocket";
import "./ai-assistant.css";

type DomainId =
  | "tw-stock"
  | "us-stock"
  | "fund"
  | "ai-analysis"
  | "auto-trading"
  | "asset-management";

type DomainDef = {
  id: DomainId;
  label: string;
  commands: { key: string; command: string; label: string }[];
};

const DOMAINS: DomainDef[] = [
  {
    id: "tw-stock",
    label: "台灣股票",
    commands: [
      { key: "portfolio", command: "investment_tw_portfolio", label: "持倉" },
      { key: "analysis", command: "investment_tw_analysis", label: "星澄分析" },
      {
        key: "recommendations",
        command: "investment_tw_recommendations",
        label: "買賣建議",
      },
    ],
  },
  {
    id: "us-stock",
    label: "美國股票",
    commands: [
      { key: "portfolio", command: "investment_us_portfolio", label: "持倉" },
      { key: "analysis", command: "investment_us_analysis", label: "星澄分析" },
      {
        key: "recommendations",
        command: "investment_us_recommendations",
        label: "買賣建議",
      },
    ],
  },
  {
    id: "fund",
    label: "共同基金",
    commands: [
      { key: "portfolio", command: "investment_fund_portfolio", label: "持倉" },
      {
        key: "recommendations",
        command: "investment_fund_recommendations",
        label: "申贖建議",
      },
      {
        key: "platforms",
        command: "investment_fund_platforms",
        label: "平台",
      },
    ],
  },
  {
    id: "ai-analysis",
    label: "AI 投資分析",
    commands: [
      { key: "trend", command: "investment_ai_trend", label: "市場趨勢" },
      {
        key: "portfolio",
        command: "investment_ai_portfolio_analysis",
        label: "投組分析",
      },
      {
        key: "strategy",
        command: "investment_ai_strategy_research",
        label: "策略研究",
      },
      {
        key: "allocation",
        command: "investment_ai_allocation",
        label: "資產配置",
      },
      { key: "risk", command: "investment_ai_risk", label: "市場風險" },
      { key: "report", command: "investment_ai_report", label: "投資報告" },
    ],
  },
  {
    id: "auto-trading",
    label: "AI 自動操盤",
    commands: [
      { key: "signals", command: "investment_trade_signals", label: "訊號簿" },
      { key: "orders", command: "investment_trade_orders", label: "訂單" },
      { key: "mode", command: "investment_trade_mode", label: "交易模式" },
      {
        key: "authorizations",
        command: "investment_trade_authorizations",
        label: "授權",
      },
      { key: "audit", command: "investment_trade_audit", label: "交易審計" },
    ],
  },
  {
    id: "asset-management",
    label: "全資產管理",
    commands: [
      {
        key: "summary",
        command: "investment_assets_summary",
        label: "資產總覽",
      },
      {
        key: "holdings",
        command: "investment_assets_holdings",
        label: "持倉明細",
      },
      {
        key: "transactions",
        command: "investment_assets_transactions",
        label: "交易紀錄",
      },
    ],
  },
];

type CommandResult = {
  event: string;
  payload: unknown;
  at: number;
};

export function AiAssistantWindowApp() {
  const { sendCommand, status } = useLocalBackendSocket();
  const [activeDomain, setActiveDomain] = useState<DomainId>("tw-stock");
  const [result, setResult] = useState<CommandResult | null>(null);
  const [busy, setBusy] = useState<string>("");
  const [lastError, setLastError] = useState<string>("");

  const domain = useMemo(
    () => DOMAINS.find((d) => d.id === activeDomain) ?? DOMAINS[0],
    [activeDomain]
  );

  // Results arrive as `ipc_event` frames: {event: "<command>_result", payload}.
  useEffect(() => {
    const onEvent = (e: Event) => {
      const detail = (e as CustomEvent).detail as {
        event?: string;
        payload?: unknown;
      };
      if (!detail?.event || !String(detail.event).endsWith("_result")) return;
      setResult({
        event: String(detail.event),
        payload: detail.payload,
        at: Date.now(),
      });
      setBusy("");
    };
    window.addEventListener("ipc_event", onEvent);
    return () => window.removeEventListener("ipc_event", onEvent);
  }, []);

  const runCommand = useCallback(
    (command: string, key: string) => {
      setBusy(key);
      setLastError("");
      const ack = sendCommand(command, {});
      if (!ack.ok) {
        setBusy("");
        setLastError(ack.message || "指令未送出");
      }
    },
    [sendCommand]
  );

  useEffect(() => {
    setResult(null);
    setLastError("");
  }, [activeDomain]);

  return (
    <div className="nexus-app">
      <header className="nexus-console-header">
        <h1>星澄 AI 投資管理與自動操盤系統</h1>
        <span className="nexus-console-status">後端：{status}</span>
      </header>

      <nav className="nexus-console-nav">
        {DOMAINS.map((d) => (
          <button
            key={d.id}
            className={
              d.id === activeDomain
                ? "nexus-console-nav-item active"
                : "nexus-console-nav-item"
            }
            onClick={() => setActiveDomain(d.id)}
          >
            {d.label}
          </button>
        ))}
      </nav>

      <main className="nexus-console-main">
        <h2>{domain.label}</h2>
        <div className="nexus-console-actions">
          {domain.commands.map((c) => (
            <button
              key={c.key}
              disabled={busy === c.key}
              onClick={() => runCommand(c.command, c.key)}
            >
              {c.label}
            </button>
          ))}
        </div>
        {lastError && <p className="nexus-console-error">{lastError}</p>}
        {result && (
          <pre className="nexus-console-output">
            {JSON.stringify(result.payload, null, 2)}
          </pre>
        )}
        <p className="nexus-console-note">
          星澄提供分析與候選訊號；正式交易決策由獨立策略引擎與風控引擎處理。
          預設 ANALYSIS 模式，LIVE 需明確人工授權與已驗證券商 API。
        </p>
      </main>
    </div>
  );
}
