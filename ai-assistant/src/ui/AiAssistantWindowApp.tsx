import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocalBackendSocket } from "./backendSocket";
import {
  formatInvestmentClock,
  formatInvestmentNumber,
  useInvestmentWatchFeature,
  type InvestmentHolding,
} from "./investmentWatchFeature";
import { useBrowserController } from "./browserController";
import { useExcelMapper } from "./excelMapper";
import { Topbar } from "./Topbar";
import { StatusStrip } from "./StatusStrip";
import { PortfolioWorkspace } from "./PortfolioWorkspace";
import { BrowserWorkspace } from "./BrowserWorkspace";
import { SystemWorkspace } from "./SystemWorkspace";
import { StarAccountingPanel } from "./StarAccountingPanel";
import "./ai-assistant.css";
import {
  EMPTY_HOLDING_DRAFT,
  WORKSPACE_VIEWS,
  socketStatusLabel,
  waitForIpcEvent,
  type HoldingDraft,
  type WorkspaceView,
} from "./aiAssistantDefinitions";

export function AiAssistantWindowApp() {
  const {
    sendCommand,
    status: socketStatus,
    waitUntilConnected,
  } = useLocalBackendSocket();
  const [message, setMessage] = useState("AI投資管家已就緒");
  const [busyAction, setBusyAction] = useState("");
  const [workspaceView, setWorkspaceView] =
    useState<WorkspaceView>("portfolio");
  const [holdingEditorOpen, setHoldingEditorOpen] = useState(false);
  const [holdingDraft, setHoldingDraft] =
    useState<HoldingDraft>(EMPTY_HOLDING_DRAFT);
  const autoMissingInfoKeyRef = useRef("");

  const request = useCallback(
    async (
      command: string,
      payload: Record<string, unknown> = {},
      timeoutMs = 30000,
    ) => {
      if (command !== "investment_watch_get_state") {
        await waitUntilConnected(15_000);
      }
      const requestId = `${command}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
      const waitPromise = waitForIpcEvent<Record<string, unknown>>(
        `${command}_result`,
        timeoutMs,
        requestId,
      );
      const sent = sendCommand(command, { ...payload, request_id: requestId });
      if (!sent.ok && !sent.queued) {
        void waitPromise.catch(() => undefined);
        throw new Error(sent.message || "送出投資管家指令失敗");
      }
      const response = await waitPromise;
      return response;
    },
    [sendCommand, waitUntilConnected],
  );

  const browser = useBrowserController(setMessage);

  const investmentWatch = useInvestmentWatchFeature({
    request,
    setBusyAction,
    setMessage,
  });

  const excel = useExcelMapper({
    runCommand: investmentWatch.runInvestmentV2Command,
    excelImportProfile: investmentWatch.investmentState.excel_import_profile,
    portfolioSourcePath:
      investmentWatch.investmentState.portfolio?.source_path || "",
    setMessage,
  });

  const { loadInvestmentState } = investmentWatch;
  const portfolio = investmentWatch.investmentState.portfolio;
  const visibleHoldings = investmentWatch.investmentHoldings;
  const portfolioUpdatedAt =
    formatInvestmentClock(portfolio?.imported_at) || "未更新";
  const socketLabel = socketStatusLabel(socketStatus);
  const openMarkets =
    investmentWatch.investmentState.market_sessions?.open_markets || [];
  const liveUpdateLabel =
    socketStatus === "Connected"
      ? openMarkets.length > 0
        ? `即時 · ${openMarkets.join("/")} 開盤`
        : "即時 · 目前休市"
      : socketLabel;
  const workbookScan = investmentWatch.investmentState.workbook_scan;
  const workbookQuality = investmentWatch.investmentState.workbook_scan_quality;
  const selectedWorkbookSheet = workbookScan?.selected_sheet || null;
  const workbookScanLabel = selectedWorkbookSheet?.sheet_name
    ? selectedWorkbookSheet.header_row_number
      ? `${selectedWorkbookSheet.sheet_name} · 第 ${selectedWorkbookSheet.header_row_number} 列`
      : `${selectedWorkbookSheet.sheet_name} · 資料第 ${selectedWorkbookSheet.data_start_row_number || 1} 列`
    : workbookScan?.sheet_count
      ? `已掃描 ${workbookScan.sheet_count} 張工作表`
      : "尚未掃描";
  const portfolioVersions =
    investmentWatch.investmentState.portfolio_versions || [];
  const latestPortfolioVersion = portfolioVersions[0];
  const localAiStatus = investmentWatch.localAiProductStatus;
  const diagnostics = investmentWatch.investmentDiagnostics;
  const localAiStatusLabel =
    localAiStatus?.state_label ||
    (investmentWatch.investmentHoldings.length > 0
      ? "等待AI投資管家分析"
      : "等待持股資料");
  const localAiScore =
    typeof localAiStatus?.score === "number"
      ? String(localAiStatus.score)
      : "-";

  const localAiNetworkContext =
    investmentWatch.investmentState.xingcheng_network_context ||
    localAiStatus?.network_context ||
    null;
  const holdingQuoteStatus = useMemo(() => {
    const eligible = visibleHoldings.filter(
      (holding) =>
        Number(holding.quantity || 0) > 0 &&
        ["TW", "US", "HK"].includes(String(holding.market || "").toUpperCase()),
    );
    const quoted = eligible.filter(
      (holding) => Number(holding.web_current_price || 0) > 0,
    );
    const providers = new Set(
      quoted
        .map((holding) => String(holding.market_data_source || "").trim())
        .filter(Boolean),
    );
    return {
      eligibleCount: eligible.length,
      quotedCount: quoted.length,
      providerCount: providers.size,
      coveragePercent:
        eligible.length > 0 ? (quoted.length / eligible.length) * 100 : 0,
    };
  }, [visibleHoldings]);
  const holdingQuoteCoverage =
    holdingQuoteStatus.eligibleCount > 0
      ? `${holdingQuoteStatus.quotedCount} / ${holdingQuoteStatus.eligibleCount} 筆（${formatInvestmentNumber(holdingQuoteStatus.coveragePercent, 1)}%）`
      : "尚無可報價持股";
  const holdingQuoteAvailable = holdingQuoteStatus.quotedCount > 0;
  const localAiNetworkLabel =
    localAiNetworkContext?.mode_label ||
    localAiStatus?.network_mode_label ||
    diagnostics?.xingcheng?.network_mode_label ||
    localAiStatus?.watch_status_label ||
    (holdingQuoteAvailable
      ? openMarkets.length > 0
        ? "盤中自動報價"
        : "收盤報價可用"
      : openMarkets.length > 0
        ? "開盤同步中"
        : "目前休市");
  const localAiNetworkCoverage =
    localAiNetworkContext?.coverage_label ||
    localAiStatus?.coverage_label ||
    diagnostics?.xingcheng?.coverage_label ||
    holdingQuoteCoverage;
  const localAiQuoteHealthLabel =
    localAiNetworkContext?.health_label ||
    localAiStatus?.quote_health_label ||
    diagnostics?.xingcheng?.quote_health_label ||
    (holdingQuoteAvailable
      ? "報價可用"
      : openMarkets.length > 0
        ? "同步中"
        : "等待開盤");
  const localAiCrossCheckedCount =
    localAiNetworkContext?.cross_checked_count ??
    diagnostics?.xingcheng?.cross_checked_count ??
    0;
  const localAiUntrustedQuoteCount =
    localAiNetworkContext?.untrusted_quote_count ??
    diagnostics?.xingcheng?.untrusted_quote_count ??
    0;
  const localAiVerificationLabel = `驗證 ${localAiCrossCheckedCount} · 異常 ${localAiUntrustedQuoteCount}`;
  const holdingValueLabel = useMemo(() => {
    const count = investmentWatch.investmentHoldings.length;
    return `${count} 筆持股`;
  }, [investmentWatch.investmentHoldings.length]);
  const estimatedWeeklyDividendTwd = useMemo(
    () =>
      investmentWatch.investmentHoldings.reduce(
        (total, holding) =>
          total + Number(holding.estimated_weekly_dividend_twd || 0),
        0,
      ),
    [investmentWatch.investmentHoldings],
  );
  const diagnosticState = diagnostics?.state || "setup";
  const diagnosticStateLabel =
    diagnostics?.state_label ||
    (investmentWatch.investmentHoldings.length > 0
      ? "診斷同步中"
      : "等待持股資料");
  const diagnosticMessage =
    diagnostics?.message ||
    (investmentWatch.investmentHoldings.length > 0
      ? "已載入持股資料，正在同步本地診斷。"
      : "請先讀取持股檔，診斷會在載入後自動更新。");
  const portfolioAgeHours = diagnostics?.portfolio?.age_hours;
  const portfolioAgeLabel =
    typeof portfolioAgeHours === "number"
      ? `${formatInvestmentNumber(portfolioAgeHours, 1)} 小時`
      : "-";
  const workbookDiagnosticLabel =
    diagnostics?.workbook?.state_label ||
    workbookQuality?.state_label ||
    "尚未掃描";
  const latestRunLabel = diagnostics?.runs?.latest?.created_at
    ? formatInvestmentClock(diagnostics.runs.latest.created_at)
    : "尚未執行";

  useEffect(() => {
    if (socketStatus !== "Connected" || busyAction) return undefined;
    const refreshState = () => {
      void loadInvestmentState(true);
    };
    const refreshMarketQuotes = () => {
      void investmentWatch.syncOpenMarkets();
    };
    const refreshNow = () => {
      refreshState();
      refreshMarketQuotes();
    };
    const handleVisibility = () => {
      if (document.visibilityState === "visible") refreshNow();
    };

    void loadInvestmentState(false, true);
    refreshMarketQuotes();
    const stateTimer = window.setInterval(refreshState, 5000);
    const marketTimer = window.setInterval(refreshMarketQuotes, 60000);
    window.addEventListener("focus", refreshNow);
    window.addEventListener("online", refreshNow);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      window.clearInterval(stateTimer);
      window.clearInterval(marketTimer);
      window.removeEventListener("focus", refreshNow);
      window.removeEventListener("online", refreshNow);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [
    busyAction,
    investmentWatch.syncOpenMarkets,
    loadInvestmentState,
    socketStatus,
  ]);

  useEffect(() => {
    if (
      socketStatus !== "Connected" ||
      busyAction ||
      visibleHoldings.length === 0
    )
      return;
    const activeTradable = visibleHoldings.filter(
      (holding) =>
        Number(holding.quantity || 0) > 0 &&
        ["TW", "US", "HK"].includes(String(holding.market || "").toUpperCase()),
    );
    const syncInfo = investmentWatch.investmentState.dividend_sync;
    if (
      String(syncInfo?.portfolio_imported_at || "") ===
        String(portfolio?.imported_at || "") &&
      Number(syncInfo?.portfolio_manual_revision || 0) ===
        Number(portfolio?.manual_revision || 0)
    ) {
      return;
    }
    const hasMissingInfo = activeTradable.some(
      (holding) =>
        !holding.market_data_updated_at ||
        !holding.dividend_frequency ||
        !holding.currency ||
        !holding.asset_type ||
        holding.asset_type === "AUTO" ||
        !holding.name ||
        holding.name.toUpperCase() ===
          String(holding.symbol || "").toUpperCase(),
    );
    if (!hasMissingInfo) return;
    const key = [
      portfolio?.imported_at || "",
      portfolio?.manual_revision || 0,
      activeTradable.length,
    ].join(":");
    if (!key || autoMissingInfoKeyRef.current === key) return;
    autoMissingInfoKeyRef.current = key;
    void investmentWatch.syncMissingInfo();
  }, [
    investmentWatch.syncMissingInfo,
    investmentWatch.investmentState.dividend_sync,
    portfolio?.imported_at,
    portfolio?.manual_revision,
    busyAction,
    socketStatus,
    visibleHoldings,
  ]);

  useEffect(() => {
    if (workspaceView === "browser" && !browser.browserSession) {
      void browser.openBrowser();
    }
  }, [browser.browserSession, browser.openBrowser, workspaceView]);

  const openNewHolding = () => {
    setHoldingDraft(EMPTY_HOLDING_DRAFT);
    setHoldingEditorOpen(true);
  };

  const openHoldingEditor = (holding: InvestmentHolding) => {
    setHoldingDraft({
      holding_id: holding.holding_id || "",
      symbol: holding.symbol || "",
      name: holding.name || "",
      market: holding.market || "TW",
      asset_type: holding.asset_type || "STOCK",
      quantity: String(holding.quantity ?? 1),
      average_cost: String(holding.average_cost ?? 0),
      currency: holding.currency || "TWD",
      principal_amount: String(holding.principal_amount ?? ""),
      principal_currency:
        holding.principal_currency || holding.currency || "TWD",
      principal_twd: String(holding.principal_twd ?? ""),
      dividend_amount_twd: String(holding.dividend_amount_twd ?? ""),
      dividend_per_unit: String(holding.dividend_per_unit ?? ""),
      monthly_dividend_twd: String(holding.monthly_dividend_twd ?? ""),
      annual_dividend_yield_percent: String(
        holding.annual_dividend_yield_percent ?? "",
      ),
      dividend_frequency: holding.dividend_frequency || "unknown",
      payback_rate_percent: String(holding.payback_rate_percent ?? ""),
      current_value_twd: String(holding.current_value_twd ?? ""),
      fund_code: holding.fund_code || "",
      fund_isin: holding.fund_isin || "",
      fund_share_class: holding.fund_share_class || "",
      fund_quote_symbol: holding.fund_quote_symbol || "",
    });
    setHoldingEditorOpen(true);
  };

  const saveHolding = async (event: React.FormEvent) => {
    event.preventDefault();
    const result = await investmentWatch.runInvestmentV2Command(
      "investment_watch_upsert_holding",
      {
        ...holdingDraft,
        quantity: Number(holdingDraft.quantity),
        average_cost: Number(holdingDraft.average_cost),
      },
      holdingDraft.holding_id ? "持股修改" : "持股新增",
    );
    if (result) {
      setHoldingEditorOpen(false);
      setHoldingDraft(EMPTY_HOLDING_DRAFT);
    }
  };

  const deleteHolding = async (holding: InvestmentHolding) => {
    if (!holding.holding_id) {
      setMessage("請先重新匯入或編輯此持股，以建立本機識別碼。");
      return;
    }
    if (
      !window.confirm(
        `確定刪除 ${holding.symbol || "這筆持股"}？原始 Excel 不會被修改。`,
      )
    )
      return;
    const result = await investmentWatch.runInvestmentV2Command(
      "investment_watch_delete_holding",
      { holding_id: holding.holding_id, confirmed: true },
      "持股刪除",
    );
    if (result && holdingDraft.holding_id === holding.holding_id) {
      setHoldingEditorOpen(false);
      setHoldingDraft(EMPTY_HOLDING_DRAFT);
    }
  };

  const restoreLatestPortfolioVersion = async () => {
    if (!latestPortfolioVersion?.version_id) return;
    if (
      !window.confirm(
        `確定還原 ${latestPortfolioVersion.holding_count || 0} 筆持股版本？目前版本會先自動保留。`,
      )
    )
      return;
    await investmentWatch.runInvestmentV2Command(
      "investment_watch_restore_portfolio_version",
      { version_id: latestPortfolioVersion.version_id, confirmed: true },
      "持股版本還原",
      120000,
    );
  };

  return (
    <main className="nexus-app" data-update-mode="direct-overlay">
      <Topbar
        liveUpdateLabel={liveUpdateLabel}
        holdingValueLabel={holdingValueLabel}
        estimatedWeeklyDividendTwd={estimatedWeeklyDividendTwd}
        busyAction={busyAction}
        readPortfolioFile={investmentWatch.readPortfolioFile}
        loadInvestmentState={loadInvestmentState}
      />

      <nav className="nexus-workspace-nav" aria-label="AI 投資管家工作區">
        {WORKSPACE_VIEWS.map((view) => (
          <button
            key={view.key}
            type="button"
            className={workspaceView === view.key ? "is-active" : ""}
            aria-current={workspaceView === view.key ? "page" : undefined}
            onClick={() => setWorkspaceView(view.key)}
          >
            {view.label}
          </button>
        ))}
      </nav>

      <StatusStrip
        holdingValueLabel={holdingValueLabel}
        portfolioFileName={portfolio?.file_name || ""}
        localAiStatusLabel={localAiStatusLabel}
        localAiScore={localAiScore}
        localAiQuoteHealthLabel={localAiQuoteHealthLabel}
        localAiNetworkCoverage={localAiNetworkCoverage}
        portfolioUpdatedAt={portfolioUpdatedAt}
        message={message}
      />

      {workspaceView === "system" ? (
        <SystemWorkspace
          diagnosticState={diagnosticState}
          diagnosticStateLabel={diagnosticStateLabel}
          diagnosticMessage={diagnosticMessage}
          portfolioAgeLabel={portfolioAgeLabel}
          workbookDiagnosticLabel={workbookDiagnosticLabel}
          localAiNetworkCoverage={localAiNetworkCoverage}
          localAiVerificationLabel={localAiVerificationLabel}
          latestRunLabel={latestRunLabel}
          diagnostics={diagnostics}
          socketLabel={socketLabel}
          hasLatestPortfolioVersion={Boolean(latestPortfolioVersion)}
          busyAction={busyAction}
          openExcelMapper={excel.openExcelMapper}
          restoreLatestPortfolioVersion={restoreLatestPortfolioVersion}
          exportInvestmentReport={investmentWatch.exportInvestmentReport}
          clearInvestmentData={investmentWatch.clearInvestmentData}
        />
      ) : null}

      {workspaceView === "accounting" ? (
        <StarAccountingPanel
          analytics={investmentWatch.investmentState.analytics || null}
          busyAction={busyAction}
          holdingCount={investmentWatch.investmentHoldings.length}
          runCommand={investmentWatch.runInvestmentV2Command}
        />
      ) : null}

      {workspaceView === "portfolio" || workspaceView === "browser" ? (
        <section
          className={`nexus-workbench nexus-workbench--local nexus-workbench--${workspaceView}`}
        >
          {workspaceView === "portfolio" ? (
            <PortfolioWorkspace
              holdingCount={investmentWatch.investmentHoldings.length}
              busyAction={busyAction}
              holdings={visibleHoldings}
              workbookScanLabel={workbookScanLabel}
              selectedWorkbookSheet={selectedWorkbookSheet}
              workbookQuality={workbookQuality}
              excel={excel}
              holdingEditorOpen={holdingEditorOpen}
              setHoldingEditorOpen={setHoldingEditorOpen}
              holdingDraft={holdingDraft}
              setHoldingDraft={setHoldingDraft}
              saveHolding={saveHolding}
              deleteHolding={deleteHolding}
              openNewHolding={openNewHolding}
              openHoldingEditor={openHoldingEditor}
              runCommand={investmentWatch.runInvestmentV2Command}
              message={message}
            />
          ) : null}

          {workspaceView === "browser" ? (
            <BrowserWorkspace browser={browser} busyAction={busyAction} />
          ) : null}
        </section>
      ) : null}
    </main>
  );
}
