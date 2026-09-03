import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocalBackendSocket } from "./backendSocket";
import { useRef } from "react";
import {
  formatInvestmentClock,
  formatInvestmentNumber,
  investmentCodeLabel,
  investmentRunLabel,
  investmentStatusLabel,
  openInvestmentFile,
  type ExcelHorizontalSheetPreview,
  type ExcelMappingPreview,
  type ExcelMappingSheetPreview,
  type InvestmentHolding,
  useInvestmentWatchFeature,
} from "./investmentWatchFeature";
import { ExcelMappingEditor } from "./ExcelMappingEditor";
import { HoldingEditor } from "./HoldingEditor";
import { StarAccountingPanel } from "./StarAccountingPanel";
import "./ai-assistant.css";
import {
  CONSOLIDATED_COLUMN_FIELDS,
  CONSOLIDATED_ROW_FIELDS,
  EMPTY_CONSOLIDATED_DRAFT,
  EMPTY_HOLDING_DRAFT,
  EXCEL_COLUMN_OPTIONS,
  EXCEL_MAPPING_FIELDS,
  STAR_COMMAND_PRESETS,
  WORKSPACE_VIEWS,
  excelColumnLetter,
  socketStatusLabel,
  waitForIpcEvent,
  type ExcelColumnMapping,
  type ExcelConsolidatedDraft,
  type ExcelHorizontalSheetDraft,
  type ExcelLayoutMode,
  type HoldingDraft,
  type InvestmentShellState,
  type WorkspaceView,
} from "./aiAssistantDefinitions";

type StarMemoryRecord = {
  memory_id: string;
  title: string;
  content: string;
  source_type: string;
  source_id: string;
  confidence: number;
  expires_at: string;
  review_status: "pending-review" | "approved" | "rejected" | "revoked";
  owner_model_id: string;
};

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
  const [excelMapperOpen, setExcelMapperOpen] = useState(false);
  const [excelMappingPreview, setExcelMappingPreview] =
    useState<ExcelMappingPreview | null>(null);
  const [excelSheetName, setExcelSheetName] = useState("");
  const [excelHeaderRow, setExcelHeaderRow] = useState(1);
  const [excelDataStartRow, setExcelDataStartRow] = useState(2);
  const [excelColumnMapping, setExcelColumnMapping] =
    useState<ExcelColumnMapping>({});
  const [excelLayoutMode, setExcelLayoutMode] =
    useState<ExcelLayoutMode>("row_mapping");
  const [excelHorizontalSheets, setExcelHorizontalSheets] = useState<
    ExcelHorizontalSheetDraft[]
  >([]);
  const [excelConsolidatedDraft, setExcelConsolidatedDraft] =
    useState<ExcelConsolidatedDraft>(EMPTY_CONSOLIDATED_DRAFT);
  const autoMissingInfoKeyRef = useRef("");
  const [starMemories, setStarMemories] = useState<StarMemoryRecord[]>([]);
  const [starMemoryLoaded, setStarMemoryLoaded] = useState(false);

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

  const loadStarMemories = useCallback(async () => {
    try {
      const result = await request("investment_star_memory_list", {
        include_inactive: true,
        limit: 100,
      });
      const records = Array.isArray(result.records)
        ? (result.records.filter(
            (item): item is StarMemoryRecord =>
              Boolean(item) &&
              typeof item === "object" &&
              typeof (item as StarMemoryRecord).memory_id === "string",
          ) as StarMemoryRecord[])
        : [];
      setStarMemories(records);
      setStarMemoryLoaded(true);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "讀取星澄記憶失敗");
    }
  }, [request]);

  const reviewStarMemory = useCallback(
    async (memoryId: string, action: "approve" | "reject" | "revoke") => {
      setBusyAction(`star-memory:${memoryId}`);
      try {
        const result = await request("investment_star_memory_review", {
          memory_id: memoryId,
          action,
          reviewer: "investment-manager-owner",
          reason: "由 AI 投資管家星澄工作區審核",
        });
        if (result.ok !== true) {
          throw new Error(String(result.message || "星澄記憶審核失敗"));
        }
        setMessage(
          action === "approve"
            ? "星澄記憶已核准"
            : action === "reject"
              ? "星澄記憶已拒絕"
              : "星澄記憶已撤銷",
        );
        await loadStarMemories();
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "星澄記憶審核失敗");
      } finally {
        setBusyAction("");
      }
    },
    [loadStarMemories, request],
  );

  useEffect(() => {
    if (workspaceView === "star" && !starMemoryLoaded) {
      void loadStarMemories();
    }
  }, [loadStarMemories, starMemoryLoaded, workspaceView]);

  const investmentWatch = useInvestmentWatchFeature({
    request,
    setBusyAction,
    setMessage,
  });
  const { loadInvestmentState } = investmentWatch;
  const portfolio = investmentWatch.investmentState.portfolio;
  const visibleHoldings = investmentWatch.investmentHoldings;
  const latestInvestmentRuns = investmentWatch.investmentRuns.slice(0, 8);
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
  const excelImportProfile =
    investmentWatch.investmentState.excel_import_profile;
  const portfolioVersions =
    investmentWatch.investmentState.portfolio_versions || [];
  const latestPortfolioVersion = portfolioVersions[0];
  const selectedExcelSheet = useMemo<ExcelMappingSheetPreview | null>(
    () =>
      excelMappingPreview?.sheets.find(
        (sheet) => sheet.sheet_name === excelSheetName,
      ) ||
      excelMappingPreview?.sheets[0] ||
      null,
    [excelMappingPreview, excelSheetName],
  );
  const excelHeaderValues = useMemo(() => {
    const values = selectedExcelSheet?.rows[excelHeaderRow - 1] || [];
    const width = Math.max(
      selectedExcelSheet?.column_count || 0,
      values.length,
    );
    return Array.from({ length: width }, (_, index) => values[index] || "");
  }, [selectedExcelSheet, excelHeaderRow]);
  const mappedExcelPreviewRows = useMemo(() => {
    if (!selectedExcelSheet) return [];
    return selectedExcelSheet.rows
      .slice(Math.max(0, excelDataStartRow - 1))
      .filter((row) =>
        EXCEL_MAPPING_FIELDS.some(({ key }) => {
          const rawIndex = excelColumnMapping[key];
          if (rawIndex === undefined || rawIndex === "") return false;
          return String(row[Number(rawIndex)] || "").trim().length > 0;
        }),
      )
      .slice(0, 6);
  }, [selectedExcelSheet, excelDataStartRow, excelColumnMapping]);
  const selectedHorizontalSheets = useMemo(
    () => excelHorizontalSheets.filter((sheet) => sheet.enabled),
    [excelHorizontalSheets],
  );
  const horizontalHoldingCount = useMemo(
    () =>
      selectedHorizontalSheets.reduce(
        (total, sheet) => total + sheet.holding_count,
        0,
      ),
    [selectedHorizontalSheets],
  );
  const horizontalSampleHoldings = useMemo(
    () =>
      selectedHorizontalSheets
        .flatMap((sheet) =>
          (sheet.sample_holdings || []).map((holding) => ({
            ...holding,
            source_sheet: sheet.sheet_name,
          })),
        )
        .slice(0, 10),
    [selectedHorizontalSheets],
  );
  const localAiStatus = investmentWatch.localAiProductStatus;
  const diagnostics = investmentWatch.investmentDiagnostics;
  const localAiStatusLabel =
    localAiStatus?.state_label ||
    (investmentWatch.investmentHoldings.length > 0
      ? "等待星澄分析"
      : "等待持股資料");
  const localAiScore =
    typeof localAiStatus?.score === "number"
      ? String(localAiStatus.score)
      : "-";
  const localAiActions = (localAiStatus?.next_actions || [])
    .filter(Boolean)
    .slice(0, 4);
  const localAiCommands = (localAiStatus?.command_suggestions || [])
    .filter(Boolean)
    .slice(0, 6);
  const localAiWarnings = investmentWatch.localAiRiskWarnings.slice(0, 5);
  const localAiCommandResult = investmentWatch.localAiCommandResult;
  const localAiCommandPreview = (localAiCommandResult?.text || "").trim();
  const localAiActionPlan = (
    localAiCommandResult?.action_plan ||
    investmentWatch.investmentState.xingcheng_action_plan ||
    []
  ).slice(0, 5);
  const localAiWatchTriggers = (
    localAiCommandResult?.watch_triggers ||
    investmentWatch.investmentState.xingcheng_watch_triggers ||
    []
  ).slice(0, 6);
  const localAiConfidence =
    localAiCommandResult?.confidence_summary ||
    investmentWatch.investmentState.xingcheng_confidence ||
    null;
  const localAiDecisionBrief =
    localAiCommandResult?.decision_brief ||
    investmentWatch.investmentState.xingcheng_decision_brief ||
    localAiStatus?.decision_summary ||
    "";
  const localAiNetworkContext =
    localAiCommandResult?.network_context ||
    investmentWatch.investmentState.xingcheng_network_context ||
    localAiStatus?.network_context ||
    null;
  const localAiExplanation =
    investmentWatch.investmentState.xingcheng_explanation;
  const externalAiDiscussion =
    investmentWatch.investmentState.xingcheng_external_discussion;
  const coordinatorLabel = externalAiDiscussion?.ok
    ? "ChatGPT 已統籌"
    : externalAiDiscussion?.queued
      ? "ChatGPT 等待中"
      : "尚未統籌";
  const localAiSourceConfidence = localAiStatus?.source_confidence;
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
  const localAiQuoteHealth =
    localAiNetworkContext?.health ||
    localAiStatus?.quote_health ||
    diagnostics?.xingcheng?.quote_health ||
    (holdingQuoteAvailable
      ? "ready"
      : openMarkets.length > 0
        ? "attention"
        : "offline");
  const localAiQuoteHealthLabel =
    localAiNetworkContext?.health_label ||
    localAiStatus?.quote_health_label ||
    diagnostics?.xingcheng?.quote_health_label ||
    (holdingQuoteAvailable
      ? "報價可用"
      : openMarkets.length > 0
        ? "同步中"
        : "等待開盤");
  const localAiProviderCount =
    localAiNetworkContext?.quote_provider_count ??
    diagnostics?.xingcheng?.quote_provider_count ??
    holdingQuoteStatus.providerCount;
  const localAiQuoteGaps = (
    localAiNetworkContext?.quote_gaps ||
    diagnostics?.xingcheng?.quote_gaps ||
    []
  ).slice(0, 8);
  const localAiQuoteGapCount =
    localAiNetworkContext?.quote_gap_count ??
    diagnostics?.xingcheng?.quote_gap_count ??
    localAiQuoteGaps.length;
  const localAiCrossCheckedCount =
    localAiNetworkContext?.cross_checked_count ??
    diagnostics?.xingcheng?.cross_checked_count ??
    0;
  const localAiUntrustedQuoteCount =
    localAiNetworkContext?.untrusted_quote_count ??
    diagnostics?.xingcheng?.untrusted_quote_count ??
    0;
  const localAiVerificationLabel = `驗證 ${localAiCrossCheckedCount} · 異常 ${localAiUntrustedQuoteCount}`;
  const localAiRecommendation =
    localAiStatus?.recommendation ||
    (investmentWatch.investmentHoldings.length > 0
      ? "星澄會依照持倉成本、報價、集中度與動態權重產生風險預告。"
      : "讀取 Excel 持股檔後，星澄會自動執行監測。");
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
  const applyLocalCommandPreset = useCallback(
    (command: string) => {
      investmentWatch.setLocalRiskCommand(command);
    },
    [investmentWatch.setLocalRiskCommand],
  );
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

  const openNewHolding = () => {
    setHoldingDraft(EMPTY_HOLDING_DRAFT);
    setExcelMapperOpen(false);
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
    setExcelMapperOpen(false);
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

  const resolveFundIdentities = async () => {
    await investmentWatch.runInvestmentV2Command(
      "investment_watch_resolve_fund_identities",
      { limit: 80 },
      "共同基金辨識",
      240000,
      true,
    );
  };

  const initializeConsolidatedReport = (preview: ExcelMappingPreview) => {
    const detected = preview.consolidated_layout;
    if (!detected?.detected) {
      setExcelConsolidatedDraft(EMPTY_CONSOLIDATED_DRAFT);
      return;
    }
    const saved =
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.layout === "consolidated_report"
        ? (excelImportProfile as Record<string, unknown>)
        : null;
    const detectedValues = detected as Record<string, unknown>;
    const next = { ...EMPTY_CONSOLIDATED_DRAFT };
    for (const key of Object.keys(next) as Array<
      keyof ExcelConsolidatedDraft
    >) {
      const value = saved?.[key] ?? detectedValues[key] ?? next[key];
      if (key === "include_zero_quantity") {
        next[key] = Boolean(value) as never;
      } else {
        next[key] = String(value ?? "") as never;
      }
    }
    setExcelConsolidatedDraft(next);
  };

  const initializeHorizontalSheets = (preview: ExcelMappingPreview) => {
    const detected = preview.horizontal_layout?.sheets || [];
    const savedSheets =
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.layout === "horizontal_matrix" &&
      Array.isArray(excelImportProfile.sheets)
        ? excelImportProfile.sheets
        : [];
    setExcelHorizontalSheets(
      detected.map((sheet) => {
        const saved = savedSheets.find(
          (item) => String(item.sheet_name || "") === sheet.sheet_name,
        );
        const value = (key: string, fallback: unknown) =>
          String(saved?.[key] ?? fallback ?? "");
        return {
          enabled:
            savedSheets.length > 0
              ? Boolean(saved)
              : Boolean(sheet.selected_by_default),
          sheet_name: sheet.sheet_name,
          category_label: value("category_label", sheet.category_label),
          market: value("market", sheet.market || "TW"),
          asset_type: value("asset_type", sheet.asset_type || "AUTO"),
          currency: value("currency", sheet.currency || "TWD"),
          header_row_number: value(
            "header_row_number",
            sheet.header_row_number || 1,
          ),
          holding_row_number: value(
            "holding_row_number",
            sheet.holding_row_number || 1,
          ),
          price_row_number: value(
            "price_row_number",
            sheet.price_row_number ?? "",
          ),
          quantity_row_number: value(
            "quantity_row_number",
            sheet.quantity_row_number || 1,
          ),
          first_asset_column_index: value(
            "first_asset_column_index",
            sheet.first_asset_column_index ?? 1,
          ),
          group_width: value("group_width", sheet.group_width || 4),
          holding_count: sheet.holding_count || 0,
          sample_holdings: sheet.sample_holdings || [],
        };
      }),
    );
  };

  const updateHorizontalSheet = (
    sheetName: string,
    changes: Partial<ExcelHorizontalSheetDraft>,
  ) => {
    setExcelHorizontalSheets((current) =>
      current.map((sheet) =>
        sheet.sheet_name === sheetName ? { ...sheet, ...changes } : sheet,
      ),
    );
  };

  const mappingForExcelSheet = (
    preview: ExcelMappingPreview,
    sheet: ExcelMappingSheetPreview,
    preferSaved = true,
  ): ExcelColumnMapping => {
    const savedMatches =
      preferSaved &&
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.sheet_name === sheet.sheet_name;
    const sourceMapping = savedMatches
      ? excelImportProfile?.column_mapping
      : sheet.suggested_mapping;
    return Object.fromEntries(
      Object.entries(sourceMapping || {}).map(([field, index]) => [
        field,
        String(index),
      ]),
    );
  };

  const selectExcelSheet = (
    preview: ExcelMappingPreview,
    sheet: ExcelMappingSheetPreview,
    preferSaved = true,
  ) => {
    const savedMatches =
      preferSaved &&
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.sheet_name === sheet.sheet_name;
    setExcelSheetName(sheet.sheet_name);
    setExcelHeaderRow(
      savedMatches
        ? excelImportProfile?.header_row_number || 1
        : sheet.suggested_header_row_number || 1,
    );
    setExcelDataStartRow(
      savedMatches
        ? excelImportProfile?.data_start_row_number || 2
        : sheet.suggested_data_start_row_number ||
            (sheet.suggested_header_row_number || 1) + 1,
    );
    setExcelColumnMapping(mappingForExcelSheet(preview, sheet, preferSaved));
  };

  const applySmartExcelRepair = () => {
    if (!excelMappingPreview?.smart_repair?.recommended) return;
    const repair = excelMappingPreview.smart_repair;
    if (repair.layout === "consolidated_report") {
      setExcelLayoutMode("consolidated_report");
      initializeConsolidatedReport(excelMappingPreview);
      return;
    }
    if (repair.layout === "horizontal_matrix") {
      setExcelLayoutMode("horizontal_matrix");
      initializeHorizontalSheets(excelMappingPreview);
      return;
    }
    const sheet =
      excelMappingPreview.sheets.find(
        (item) => item.sheet_name === repair.sheet_name,
      ) || excelMappingPreview.sheets[0];
    if (!sheet) return;
    setExcelLayoutMode("row_mapping");
    setExcelSheetName(sheet.sheet_name);
    setExcelHeaderRow(repair.header_row_number || 1);
    setExcelDataStartRow(
      repair.data_start_row_number || (repair.header_row_number || 1) + 1,
    );
    setExcelColumnMapping(
      Object.fromEntries(
        Object.entries(repair.column_mapping || {}).map(([field, index]) => [
          field,
          String(index),
        ]),
      ),
    );
  };

  const openExcelMapper = async (chooseFile = false) => {
    let sourcePath = chooseFile ? "" : portfolio?.source_path || "";
    if (!sourcePath.toLowerCase().endsWith(".xlsx")) {
      sourcePath = await openInvestmentFile();
    }
    if (!sourcePath) {
      setMessage("未選擇 Excel 檔案。");
      return;
    }
    if (!sourcePath.toLowerCase().endsWith(".xlsx")) {
      setMessage("欄位設定目前支援 .xlsx，請先將舊版 Excel 另存為 .xlsx。");
      return;
    }
    const result = await investmentWatch.runInvestmentV2Command(
      "investment_watch_preview_excel_mapping",
      { path: sourcePath },
      "Excel 欄位預覽",
      120000,
    );
    const preview = result?.excel_mapping_preview as
      ExcelMappingPreview | undefined;
    if (!preview?.sheets.length) return;
    setExcelMappingPreview(preview);
    initializeConsolidatedReport(preview);
    initializeHorizontalSheets(preview);
    setExcelLayoutMode(
      excelImportProfile?.source_path === preview.source_path &&
        excelImportProfile?.layout === "consolidated_report"
        ? "consolidated_report"
        : excelImportProfile?.source_path === preview.source_path &&
            excelImportProfile?.layout === "horizontal_matrix"
          ? "horizontal_matrix"
          : preview.consolidated_layout?.recommended
            ? "consolidated_report"
            : preview.horizontal_layout?.recommended
              ? "horizontal_matrix"
              : "row_mapping",
    );
    const savedSheet =
      excelImportProfile?.source_path === preview.source_path
        ? preview.sheets.find(
            (sheet) => sheet.sheet_name === excelImportProfile.sheet_name,
          )
        : undefined;
    const initialSheet =
      savedSheet ||
      preview.sheets.find(
        (sheet) => sheet.sheet_name === preview.selected_sheet_name,
      ) ||
      preview.sheets[0];
    selectExcelSheet(preview, initialSheet, true);
    setHoldingEditorOpen(false);
    setExcelMapperOpen(true);
  };

  const importExcelWithMapping = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!excelMappingPreview || !selectedExcelSheet) return;
    if (excelLayoutMode === "consolidated_report") {
      const numericFields: Array<keyof ExcelConsolidatedDraft> = [
        "fund_start_row_number",
        "security_start_row_number",
        "data_end_row_number",
        "fund_name_column_index",
        "tw_symbol_column_index",
        "tw_name_column_index",
        "us_symbol_column_index",
        "us_name_column_index",
        "quantity_column_index",
        "cost_amount_column_index",
        "price_column_index",
        "current_value_column_index",
        "dividend_amount_column_index",
        "dividend_per_unit_column_index",
        "monthly_dividend_column_index",
        "annual_dividend_yield_column_index",
        "payback_rate_column_index",
      ];
      const config: Record<string, unknown> = { ...excelConsolidatedDraft };
      for (const field of numericFields) {
        config[field] = Number(excelConsolidatedDraft[field]);
      }
      const result = await investmentWatch.runInvestmentV2Command(
        "investment_watch_import_excel_mapping",
        {
          path: excelMappingPreview.source_path,
          layout: "consolidated_report",
          config,
        },
        "報酬工作表匯入",
        240000,
      );
      if (result) {
        setExcelMapperOpen(false);
        setExcelMappingPreview(null);
      }
      return;
    }
    if (excelLayoutMode === "horizontal_matrix") {
      if (selectedHorizontalSheets.length === 0) {
        setMessage("請至少選擇一張 ETF、台股、美股或共同基金工作表。");
        return;
      }
      const invalidSheet = selectedHorizontalSheets.find(
        (sheet) =>
          !sheet.header_row_number ||
          !sheet.holding_row_number ||
          !sheet.quantity_row_number ||
          !sheet.group_width,
      );
      if (invalidSheet) {
        setMessage(`請完成 ${invalidSheet.sheet_name} 的列與欄位設定。`);
        return;
      }
      const result = await investmentWatch.runInvestmentV2Command(
        "investment_watch_import_excel_mapping",
        {
          path: excelMappingPreview.source_path,
          layout: "horizontal_matrix",
          sheets: selectedHorizontalSheets.map((sheet) => ({
            enabled: true,
            sheet_name: sheet.sheet_name,
            category_label: sheet.category_label,
            market: sheet.market,
            asset_type: sheet.asset_type,
            currency: sheet.currency,
            header_row_number: Number(sheet.header_row_number),
            holding_row_number: Number(sheet.holding_row_number),
            price_row_number:
              sheet.price_row_number === ""
                ? null
                : Number(sheet.price_row_number),
            quantity_row_number: Number(sheet.quantity_row_number),
            first_asset_column_index: Number(sheet.first_asset_column_index),
            group_width: Number(sheet.group_width),
          })),
        },
        "Excel 橫向持股匯入",
        240000,
      );
      if (result) {
        setExcelMapperOpen(false);
        setExcelMappingPreview(null);
      }
      return;
    }
    const missing = EXCEL_MAPPING_FIELDS.filter(
      (field) =>
        field.required &&
        (excelColumnMapping[field.key] === undefined ||
          excelColumnMapping[field.key] === ""),
    );
    if (missing.length > 0) {
      setMessage(
        `請設定必要欄位：${missing.map((field) => field.label).join("、")}`,
      );
      return;
    }
    const columnMapping = Object.fromEntries(
      Object.entries(excelColumnMapping)
        .filter(([, index]) => index !== "")
        .map(([field, index]) => [field, Number(index)]),
    );
    const result = await investmentWatch.runInvestmentV2Command(
      "investment_watch_import_excel_mapping",
      {
        path: excelMappingPreview.source_path,
        sheet_name: selectedExcelSheet.sheet_name,
        header_row_number: excelHeaderRow,
        data_start_row_number: excelDataStartRow,
        column_mapping: columnMapping,
      },
      "Excel 欄位匯入",
      240000,
    );
    if (result) {
      setExcelMapperOpen(false);
      setExcelMappingPreview(null);
    }
  };

  return (
    <main className="nexus-app" data-update-mode="direct-overlay">
      <header className="nexus-topbar">
        <div className="nexus-title-block">
          <p className="nexus-eyebrow">投資管理</p>
          <h1>AI投資管家</h1>
          <div className="nexus-top-meta">
            <span>{liveUpdateLabel}</span>
            <span>獨立投資對話 · 模型全自動（Gemma＋GPT 主力）</span>
            <span>{holdingValueLabel}</span>
            <span>
              週配息 NT$ {formatInvestmentNumber(estimatedWeeklyDividendTwd, 2)}
            </span>
          </div>
        </div>
        <div className="nexus-toolbar" aria-label="主要操作">
          <button
            type="button"
            className="nexus-primary"
            onClick={() => void investmentWatch.readPortfolioFile()}
            disabled={Boolean(busyAction)}
          >
            {busyAction === "investment:read-file" ? "讀取中..." : "讀取檔案"}
          </button>
          <button
            type="button"
            onClick={() => void loadInvestmentState()}
            disabled={Boolean(busyAction)}
          >
            重新整理
          </button>
        </div>
      </header>

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

      <section className="nexus-status-strip" aria-label="投資管家狀態">
        <div>
          <span>投資組合</span>
          <strong>
            {holdingValueLabel} · {portfolio?.file_name || "尚未匯入"}
          </strong>
        </div>
        <div>
          <span>星澄</span>
          <strong>
            {localAiStatusLabel} · {localAiScore}
          </strong>
        </div>
        <div>
          <span>市場資料</span>
          <strong>
            {localAiQuoteHealthLabel} · {localAiNetworkCoverage}
          </strong>
        </div>
        <div className="nexus-status-message">
          <span>{portfolioUpdatedAt}</span>
          <strong>{message}</strong>
        </div>
      </section>

      {workspaceView === "system" ? (
        <section
          className={`nexus-diagnostics-panel nexus-diagnostics-panel--${diagnosticState}`}
          aria-label="本地診斷"
        >
          <div className="nexus-diagnostics-summary">
            <span>{diagnosticStateLabel}</span>
            <strong>{diagnosticMessage}</strong>
          </div>
          <div className="nexus-diagnostic-grid">
            <div>
              <span>資料年齡</span>
              <strong>{portfolioAgeLabel}</strong>
            </div>
            <div>
              <span>Excel 掃描</span>
              <strong>{workbookDiagnosticLabel}</strong>
            </div>
            <div>
              <span>星澄風險</span>
              <strong>
                {diagnostics?.xingcheng?.warning_count ?? 0} /{" "}
                {diagnostics?.xingcheng?.critical_count ?? 0}
              </strong>
            </div>
            <div>
              <span>網路報價</span>
              <strong>{localAiNetworkCoverage}</strong>
            </div>
            <div>
              <span>交叉驗證</span>
              <strong>{localAiVerificationLabel}</strong>
            </div>
            <div>
              <span>最近執行</span>
              <strong>{latestRunLabel}</strong>
            </div>
            <div>
              <span>本地錯誤記錄</span>
              <strong>{diagnostics?.error_logging?.count ?? 0} 筆</strong>
            </div>
          </div>
        </section>
      ) : null}

      {workspaceView === "accounting" ? (
        <StarAccountingPanel
          analytics={investmentWatch.investmentState.analytics || null}
          busyAction={busyAction}
          holdingCount={investmentWatch.investmentHoldings.length}
          runCommand={investmentWatch.runInvestmentV2Command}
        />
      ) : null}

      {workspaceView === "portfolio" || workspaceView === "star" ? (
        <section
          className={`nexus-workbench nexus-workbench--local nexus-workbench--${workspaceView}`}
        >
          {workspaceView === "portfolio" ? (
            <section className="nexus-column nexus-column--main">
              <section className="nexus-surface nexus-holdings-surface">
                <div className="nexus-section-head">
                  <span>持股資料</span>
                  <div className="nexus-holding-head-actions">
                    <strong>{investmentWatch.investmentHoldings.length}</strong>
                    <button
                      type="button"
                      className="nexus-toggle-button"
                      onClick={() => void openExcelMapper(false)}
                      disabled={Boolean(busyAction)}
                    >
                      欄位設定
                    </button>
                    <button
                      type="button"
                      className="nexus-toggle-button"
                      onClick={() =>
                        void investmentWatch.runInvestmentV2Command(
                          "investment_watch_sync_dividends",
                          {},
                          "星澄配息搜尋",
                          240000,
                        )
                      }
                      disabled={Boolean(busyAction)}
                    >
                      {busyAction ===
                      "investment:investment_watch_sync_dividends"
                        ? "同步中..."
                        : "星澄搜尋配息"}
                    </button>
                    <button
                      type="button"
                      className="nexus-toggle-button"
                      title="依基金名稱搜尋基金代號、級別與可用報價代號"
                      onClick={() => void resolveFundIdentities()}
                      disabled={Boolean(busyAction)}
                    >
                      {busyAction ===
                      "investment:investment_watch_resolve_fund_identities"
                        ? "辨識中..."
                        : "辨識基金"}
                    </button>
                    <button
                      type="button"
                      className="nexus-toggle-button"
                      onClick={openNewHolding}
                      disabled={Boolean(busyAction)}
                    >
                      新增持股
                    </button>
                  </div>
                </div>
                <p className="nexus-scan-note">
                  {workbookScanLabel}
                  {selectedWorkbookSheet?.header_mode
                    ? ` · ${selectedWorkbookSheet.header_mode}`
                    : ""}
                </p>
                {workbookQuality ? (
                  <div className="nexus-scan-quality">
                    <span>
                      掃描品質
                      <strong>{workbookQuality.state_label || "-"}</strong>
                    </span>
                    <span>
                      分數
                      <strong>{workbookQuality.score ?? "-"}</strong>
                    </span>
                    <span>
                      有效資料
                      <strong>
                        {workbookQuality.valid_data_row_count ?? "-"}
                      </strong>
                    </span>
                    <span>
                      表頭深度
                      <strong>{workbookQuality.header_depth || 1} 層</strong>
                    </span>
                    <p>{workbookQuality.recommendation}</p>
                  </div>
                ) : null}
                <ExcelMappingEditor
                  open={excelMapperOpen}
                  excelMappingPreview={excelMappingPreview}
                  selectedExcelSheet={selectedExcelSheet}
                  excelLayoutMode={excelLayoutMode}
                  setExcelLayoutMode={setExcelLayoutMode}
                  excelConsolidatedDraft={excelConsolidatedDraft}
                  setExcelConsolidatedDraft={setExcelConsolidatedDraft}
                  excelHorizontalSheets={excelHorizontalSheets}
                  selectedHorizontalSheets={selectedHorizontalSheets}
                  horizontalHoldingCount={horizontalHoldingCount}
                  horizontalSampleHoldings={horizontalSampleHoldings}
                  updateHorizontalSheet={updateHorizontalSheet}
                  excelHeaderRow={excelHeaderRow}
                  setExcelHeaderRow={setExcelHeaderRow}
                  excelDataStartRow={excelDataStartRow}
                  setExcelDataStartRow={setExcelDataStartRow}
                  excelColumnMapping={excelColumnMapping}
                  setExcelColumnMapping={setExcelColumnMapping}
                  excelHeaderValues={excelHeaderValues}
                  mappedExcelPreviewRows={mappedExcelPreviewRows}
                  busyAction={busyAction}
                  message={message}
                  importExcelWithMapping={importExcelWithMapping}
                  applySmartExcelRepair={applySmartExcelRepair}
                  selectExcelSheet={selectExcelSheet}
                  openExcelMapper={openExcelMapper}
                  setExcelMapperOpen={setExcelMapperOpen}
                />
                <HoldingEditor
                  open={holdingEditorOpen}
                  holdingDraft={holdingDraft}
                  setHoldingDraft={setHoldingDraft}
                  busyAction={busyAction}
                  saveHolding={saveHolding}
                  setHoldingEditorOpen={setHoldingEditorOpen}
                />
                <div className="nexus-holding-table">
                  <div className="nexus-holding-head">
                    <span>標的</span>
                    <span>數量</span>
                    <span>成本</span>
                    <span>市值 TWD</span>
                    <span>配息</span>
                    <span>週配息 TWD</span>
                    <span>操作</span>
                  </div>
                  {visibleHoldings.length === 0 ? (
                    <p className="nexus-empty-line">尚無持股資料</p>
                  ) : (
                    visibleHoldings.map((holding, index) => (
                      <div
                        key={`${holding.symbol || "holding"}:${index}`}
                        className="nexus-holding-row"
                      >
                        <div className="nexus-holding-identity">
                          <strong>{holding.symbol || "-"}</strong>
                          <span>{holding.name || "-"}</span>
                          {holding.fund_identity_status ? (
                            <small>
                              {holding.fund_identity_status === "confirmed"
                                ? `已辨識 ${holding.fund_quote_symbol || ""}`
                                : holding.fund_identity_status === "suggested"
                                  ? `待確認 ${holding.fund_candidate_symbol || ""}`
                                  : "基金代號待補"}
                            </small>
                          ) : null}
                          <small>
                            {investmentCodeLabel(holding.market)} ·{" "}
                            {holding.currency || "-"}
                          </small>
                        </div>
                        <span>
                          {formatInvestmentNumber(holding.quantity, 4)}
                        </span>
                        <span>
                          {holding.currency || "-"}{" "}
                          {formatInvestmentNumber(holding.average_cost, 4)}
                        </span>
                        <span>
                          NT${" "}
                          {formatInvestmentNumber(
                            holding.web_current_value_twd ??
                              holding.current_value_twd,
                            2,
                          )}
                        </span>
                        <span>
                          {holding.dividend_frequency_label || "待同步"}
                          {holding.dividend_frequency_source === "manual"
                            ? " · 手動"
                            : ""}
                        </span>
                        <span>
                          NT${" "}
                          {formatInvestmentNumber(
                            holding.estimated_weekly_dividend_twd,
                            2,
                          )}
                        </span>
                        <div className="nexus-holding-row-actions">
                          <button
                            type="button"
                            onClick={() => openHoldingEditor(holding)}
                            disabled={Boolean(busyAction)}
                          >
                            編輯
                          </button>
                          <button
                            type="button"
                            className="nexus-danger"
                            onClick={() => void deleteHolding(holding)}
                            disabled={Boolean(busyAction)}
                          >
                            刪除
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </section>
            </section>
          ) : null}

          {workspaceView === "star" ? (
            <aside className="nexus-column nexus-column--right">
              <section className="nexus-surface nexus-xingcheng-status">
                <div className="nexus-section-head">
                  <span>星澄</span>
                  <strong>
                    {localAiStatus?.watch_status_label || "等待監測"}
                  </strong>
                </div>
                <div
                  className={`nexus-ai-score nexus-ai-score--${localAiStatus?.state || "empty"}`}
                >
                  <strong>{localAiScore}</strong>
                  <span>{localAiStatusLabel}</span>
                </div>
                <p>{localAiRecommendation}</p>
                <div className="nexus-coordinator-status">
                  <div>
                    <span>最終統籌</span>
                    <strong>{coordinatorLabel}</strong>
                  </div>
                  <p>
                    {externalAiDiscussion?.content ||
                      externalAiDiscussion?.message ||
                      "由星澄完成分析後，透過 AI 通道交由 ChatGPT 最終統籌。"}
                  </p>
                </div>
                {localAiDecisionBrief ? (
                  <div className="nexus-xingcheng-decision">
                    <span>星澄決策摘要</span>
                    <strong>
                      信心{" "}
                      {localAiConfidence?.label ||
                        localAiStatus?.confidence_label ||
                        "-"}
                      {" · "}
                      行動{" "}
                      {localAiStatus?.action_count ?? localAiActionPlan.length}
                      {" · "}
                      觸發{" "}
                      {localAiStatus?.trigger_count ??
                        localAiWatchTriggers.length}
                    </strong>
                    <p>{localAiDecisionBrief}</p>
                  </div>
                ) : null}
                <div className="nexus-xingcheng-meta">
                  <span>{localAiStatus?.coverage_label || "等待持股資料"}</span>
                  <span
                    className={`nexus-network-pill nexus-network-pill--${localAiQuoteHealth}`}
                  >
                    {localAiQuoteHealthLabel}
                  </span>
                  <span>{localAiNetworkLabel}</span>
                  <span>{localAiVerificationLabel}</span>
                  <span>來源 {localAiProviderCount}</span>
                  <span>
                    信心 {localAiSourceConfidence?.grade || "-"} ·{" "}
                    {formatInvestmentNumber(localAiSourceConfidence?.score, 0)}
                  </span>
                  <span>
                    刷新 {localAiStatus?.refreshed_holding_count || 0} · 快取{" "}
                    {localAiStatus?.reused_holding_count || 0}
                  </span>
                  <span>
                    {localAiExplanation?.mode_label ||
                      localAiStatus?.explanation_mode_label ||
                      "規則引擎說明"}
                  </span>
                  <span>風險 {localAiStatus?.warning_count ?? 0}</span>
                  <span>重大 {localAiStatus?.critical_count ?? 0}</span>
                </div>
                {localAiExplanation?.text ? (
                  <div className="nexus-xingcheng-explanation">
                    <strong>{localAiExplanation.mode_label}</strong>
                    <p>{localAiExplanation.text}</p>
                  </div>
                ) : null}
                {localAiQuoteGapCount > 0 ? (
                  <div className="nexus-quote-gaps">
                    <span>報價缺口 {localAiQuoteGapCount}</span>
                    {localAiQuoteGaps.length === 0 ? (
                      <p>有報價缺口，但目前沒有取得明細；請重新執行星澄。</p>
                    ) : (
                      localAiQuoteGaps.map((gap, index) => (
                        <article key={`${gap.symbol || "quote-gap"}:${index}`}>
                          <strong>
                            {gap.symbol || "-"}
                            {gap.name ? ` · ${gap.name}` : ""}
                          </strong>
                          <span>
                            {gap.market || "-"} ·{" "}
                            {gap.reason || gap.status || "未取得可信報價"}
                          </span>
                          <p>
                            {gap.detail ||
                              gap.action ||
                              "請檢查代號、市場或重新執行星澄。"}
                          </p>
                        </article>
                      ))
                    )}
                  </div>
                ) : null}
                {localAiActions.length > 0 ? (
                  <ul className="nexus-xingcheng-actions">
                    {localAiActions.map((action, index) => (
                      <li key={`${action}:${index}`}>{action}</li>
                    ))}
                  </ul>
                ) : null}
                <div className="nexus-risk-board">
                  <span>持倉風險預告</span>
                  {localAiWarnings.length === 0 ? (
                    <p>尚無風險預告</p>
                  ) : (
                    localAiWarnings.map((warning, index) => (
                      <article
                        key={`${warning.code || "risk"}:${warning.symbol || "portfolio"}:${index}`}
                        className={`nexus-risk-row nexus-risk-row--${warning.severity || "info"}`}
                      >
                        <strong>
                          {warning.title || warning.code || "風險提示"}
                        </strong>
                        <span>{warning.detail || warning.action || "-"}</span>
                      </article>
                    ))
                  )}
                </div>
                <div className="nexus-command-suggestions">
                  <span>星澄命令建議</span>
                  <div>
                    {localAiCommands.length === 0 ? (
                      <code>匯入持股後可下達本地分析命令</code>
                    ) : (
                      localAiCommands.map((command, index) => (
                        <button
                          key={`${command}:${index}`}
                          type="button"
                          className="nexus-command-chip"
                          onClick={() => applyLocalCommandPreset(command)}
                          disabled={Boolean(busyAction)}
                        >
                          {command}
                        </button>
                      ))
                    )}
                  </div>
                </div>
              </section>

              <section className="nexus-surface">
                <div className="nexus-section-head">
                  <span>星澄命令</span>
                  <strong>{investmentWatch.investmentRuns.length}</strong>
                </div>
                <form
                  className="nexus-xingcheng-command"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void investmentWatch.sendLocalRiskCommand();
                  }}
                >
                  <label htmlFor="local-risk-command">輸入命令給星澄</label>
                  <div
                    className="nexus-command-presets"
                    aria-label="星澄常用命令"
                  >
                    {STAR_COMMAND_PRESETS.map((preset) => (
                      <button
                        key={preset.label}
                        type="button"
                        onClick={() => applyLocalCommandPreset(preset.command)}
                        disabled={
                          Boolean(busyAction) ||
                          investmentWatch.investmentHoldings.length === 0
                        }
                      >
                        {preset.label}
                      </button>
                    ))}
                  </div>
                  <input
                    id="local-risk-command"
                    value={investmentWatch.localRiskCommand}
                    onChange={(event) =>
                      investmentWatch.setLocalRiskCommand(event.target.value)
                    }
                    placeholder={
                      investmentWatch.investmentHoldings.length === 0
                        ? "請先讀取 Excel 檔"
                        : "預設自動連網報價，例如：只看 AAPL 跌破成本 3% 集中度 30%"
                    }
                    disabled={
                      Boolean(busyAction) ||
                      investmentWatch.investmentHoldings.length === 0
                    }
                  />
                  <button
                    type="submit"
                    className="nexus-primary nexus-command-submit"
                    disabled={
                      Boolean(busyAction) ||
                      investmentWatch.investmentHoldings.length === 0
                    }
                  >
                    {busyAction === "investment:local-risk-command"
                      ? "星澄執行中..."
                      : "送出星澄命令"}
                  </button>
                </form>
                {localAiCommandPreview ? (
                  <div className="nexus-command-result">
                    <span>星澄回覆</span>
                    <pre>{localAiCommandPreview}</pre>
                  </div>
                ) : null}
                {localAiActionPlan.length > 0 ? (
                  <div className="nexus-action-plan">
                    <span>星澄行動計畫</span>
                    {localAiActionPlan.map((item, index) => (
                      <article
                        key={`${item.symbol || "portfolio"}:${item.title || "action"}:${index}`}
                        className={`nexus-plan-row nexus-plan-row--${item.priority || "monitor"}`}
                      >
                        <strong>{item.title || item.symbol || "行動"}</strong>
                        <span>
                          {item.due || "-"} · {item.risk_level_label || "-"}
                        </span>
                        <p>{item.action || "-"}</p>
                      </article>
                    ))}
                  </div>
                ) : null}
                {localAiWatchTriggers.length > 0 ? (
                  <div className="nexus-watch-triggers">
                    <span>監測觸發條件</span>
                    {localAiWatchTriggers.map((item, index) => (
                      <article
                        key={`${item.symbol || "trigger"}:${item.trigger || "condition"}:${index}`}
                        className={`nexus-trigger-row nexus-trigger-row--${item.severity || "info"}`}
                      >
                        <strong>
                          {item.symbol || "-"} · {item.trigger || "-"}
                        </strong>
                        <span>
                          {item.threshold_text || item.threshold || "-"}
                        </span>
                        <p>{item.action || "-"}</p>
                      </article>
                    ))}
                  </div>
                ) : null}
                <div className="nexus-run-list">
                  {latestInvestmentRuns.length === 0 ? (
                    <p className="nexus-empty-line">尚無分析紀錄</p>
                  ) : (
                    latestInvestmentRuns.map((run) => {
                      const preview = (run.content || run.error || "").trim();
                      return (
                        <article key={run.run_id} className="nexus-run-row">
                          <strong>{investmentRunLabel(run)}</strong>
                          <span>
                            {investmentStatusLabel(run.status)} ·{" "}
                            {formatInvestmentClock(run.created_at)}
                          </span>
                          {preview ? (
                            <pre className="nexus-run-preview">{preview}</pre>
                          ) : null}
                        </article>
                      );
                    })
                  )}
                </div>
              </section>

              <section className="nexus-surface nexus-star-memory">
                <div className="nexus-section-head">
                  <span>星澄可審核記憶</span>
                  <strong>
                    待審{" "}
                    {
                      starMemories.filter(
                        (item) => item.review_status === "pending-review",
                      ).length
                    }
                  </strong>
                </div>
                <p>外部 AI 只可提出候選記憶；核准前不會進入星澄推理上下文。</p>
                <button
                  type="button"
                  onClick={() => void loadStarMemories()}
                  disabled={Boolean(busyAction)}
                >
                  重新整理記憶
                </button>
                <div className="nexus-star-memory-list">
                  {starMemories.length === 0 ? (
                    <p className="nexus-empty-line">目前沒有可審核記憶</p>
                  ) : (
                    starMemories.map((item) => (
                      <article key={`${item.owner_model_id}:${item.memory_id}`}>
                        <div>
                          <strong>{item.title || "未命名記憶"}</strong>
                          <span>
                            {item.review_status} · 信心{" "}
                            {formatInvestmentNumber(
                              Number(item.confidence) * 100,
                              0,
                            )}
                            %
                          </span>
                        </div>
                        <p>{item.content}</p>
                        <small>
                          來源 {item.source_type || "-"} /{" "}
                          {item.source_id || "-"}
                        </small>
                        <div className="nexus-star-memory-actions">
                          {item.review_status === "pending-review" ? (
                            <>
                              <button
                                type="button"
                                className="nexus-primary"
                                onClick={() =>
                                  void reviewStarMemory(
                                    item.memory_id,
                                    "approve",
                                  )
                                }
                                disabled={Boolean(busyAction)}
                              >
                                核准
                              </button>
                              <button
                                type="button"
                                className="nexus-danger"
                                onClick={() =>
                                  void reviewStarMemory(
                                    item.memory_id,
                                    "reject",
                                  )
                                }
                                disabled={Boolean(busyAction)}
                              >
                                拒絕
                              </button>
                            </>
                          ) : item.review_status === "approved" ? (
                            <button
                              type="button"
                              className="nexus-danger"
                              onClick={() =>
                                void reviewStarMemory(item.memory_id, "revoke")
                              }
                              disabled={Boolean(busyAction)}
                            >
                              撤銷
                            </button>
                          ) : null}
                        </div>
                      </article>
                    ))
                  )}
                </div>
              </section>
            </aside>
          ) : null}
        </section>
      ) : null}

      {workspaceView === "system" ? (
        <section className="nexus-system-overview" aria-label="系統與治理">
          <article className="nexus-surface">
            <div className="nexus-section-head">
              <span>服務架構</span>
              <strong>{socketLabel}</strong>
            </div>
            <p>AI 投資管家只管理本機持股與設定；分析及帳務決策由星澄提供。</p>
            <div className="nexus-system-flow">
              <span>AI 投資管家</span>
              <strong>→ AI 通道 →</strong>
              <span>星澄</span>
            </div>
          </article>
          <article className="nexus-surface">
            <div className="nexus-section-head">
              <span>自動資料</span>
              <strong>{localAiQuoteHealthLabel}</strong>
            </div>
            <p>配息、股價與淨值由星澄搜尋；未知值不覆寫手動資料。</p>
            <span>{localAiVerificationLabel}</span>
          </article>
          <article className="nexus-surface">
            <div className="nexus-section-head">
              <span>手機工具</span>
              <strong>已分離</strong>
            </div>
            <p>investment-mobile 經星澄連線；桌面管家不開啟 LAN 服務。</p>
          </article>
          <article className="nexus-surface">
            <div className="nexus-section-head">
              <span>治理</span>
              <strong>最高權限</strong>
            </div>
            <p>星澄負責分析與自主帳務；外部 AI 不能直接寫入投資管家。</p>
          </article>
          <article className="nexus-surface nexus-system-tools">
            <div className="nexus-section-head">
              <span>維護工具</span>
              <strong>必要時使用</strong>
            </div>
            <p>低頻率維護操作集中在這裡，不占用日常持股與星澄工作區。</p>
            <div>
              <button
                type="button"
                onClick={() => void openExcelMapper(false)}
                disabled={Boolean(busyAction)}
              >
                Excel 欄位設定
              </button>
              <button
                type="button"
                onClick={() => void restoreLatestPortfolioVersion()}
                disabled={Boolean(busyAction) || !latestPortfolioVersion}
              >
                還原最近持股
              </button>
              <button
                type="button"
                onClick={() => void investmentWatch.exportInvestmentReport()}
                disabled={Boolean(busyAction)}
              >
                匯出去識別診斷
              </button>
              <button
                type="button"
                className="nexus-danger"
                onClick={() => {
                  if (
                    window.confirm(
                      "確定刪除投資管家的本機舊資料？此操作不會修改原始 Excel。",
                    )
                  ) {
                    void investmentWatch.clearInvestmentData();
                  }
                }}
                disabled={Boolean(busyAction)}
              >
                刪除舊資料
              </button>
            </div>
          </article>
        </section>
      ) : null}
    </main>
  );
}
