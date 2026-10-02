// ai-assistant governed executor — the investment-manager business
// layer (manifest: capabilities.investment-manager with
// mobile_business_layer_owner=ai-assistant,
// separate_mobile_business_layer=false, authority=
// "portfolio-state-and-tool-settings-only").
//
// Data plane: the investment product's business journals live under
// Standalone tools/investment-mobile/runtime (the mobile deployment
// root; PostgreSQL gptbridge_trading is the formal write authority and
// its governed outbox path is still pending). Read commands project the
// real journal contents read-only — accounts, cash, instruments,
// market observations, xingcheng signals, risk decisions, fund nav and
// transactions, paper accounts — and mark empty/absent stores honestly
// (data_state + data_plane fields) rather than fabricating data.
//
// Write/control commands (settings set, autotrade control, broker
// import submit/rollback, mobile instruction intake) fail closed with
// typed pending codes: they require the governed outbox or the
// investment-mobile engine channel, neither of which is reachable from
// this host (direct_cross_tool_connection=false; the transport-store
// successor is deferred). AI-owned analysis (xingcheng via the
// information layer) answers with a deterministic local projection that
// is explicitly marked advisory + AI_CHANNEL_NOT_CONNECTED.
//
// Commands outside the allowlist stay PERMISSION_DENIED at the surface
// boundary.
using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal sealed class AiAssistantExecutor
    : IGovernedCommandExecutor, IWsCommandSurface
{
    private static readonly HashSet<string> OwnedCommands = new(
        StringComparer.Ordinal)
    {
        // Renderer surface (src/ui/workspace/pages/*, XingchengPanel).
        "investment_ai_portfolio_analysis",
        "investment_ai_report",
        "investment_ai_research_search",
        "investment_ai_risk",
        "investment_assets_allocation",
        "investment_assets_exposure",
        "investment_assets_summary",
        "investment_assets_valuation",
        "investment_autotrade_control",
        "investment_autotrade_overview",
        "investment_autotrade_performance",
        "investment_autotrade_reports",
        "investment_autotrade_strategies",
        "investment_backtest_results",
        "investment_broker_accounts",
        "investment_broker_import_rollback",
        "investment_broker_import_submit",
        "investment_broker_imports",
        "investment_broker_sims",
        "investment_broker_status",
        "investment_fund_analysis",
        "investment_fund_nav",
        "investment_fund_portfolio",
        "investment_fund_recommendations",
        "investment_fund_transactions",
        "investment_market_history",
        "investment_market_quote",
        "investment_market_status",
        "investment_monitor_events",
        "investment_monitor_overview",
        "investment_monitor_recommendations",
        "investment_monitor_reports",
        "investment_monitor_risk",
        "investment_perf_health",
        "investment_settings_get",
        "investment_settings_set",
        "investment_sim_executions",
        "investment_sim_performance",
        "investment_sim_status",
        "investment_strategy_list",
        "investment_trade_mode",
        "investment_tw_analysis",
        "investment_tw_portfolio",
        "investment_tw_recommendations",
        "investment_us_analysis",
        "investment_us_portfolio",
        "investment_us_recommendations",
        // Codex registry (INVESTMENT_AI_CONSULT /
        // INVESTMENT_STAR_MEMORY_LIST) lowercase frame names.
        "investment_ai_consult",
        "investment_star_memory_list",
        // xingcheng -> ai-assistant relay (tool_routes.json).
        "investment_mobile_get_snapshot",
        "investment_mobile_submit_instruction",
    };

    private readonly GovernedEnvironment _env;
    private readonly string _runtimeRoot;
    private readonly string _stateDir;

    public AiAssistantExecutor(GovernedEnvironment env)
    {
        _env = env;
        // The investment-manager business layer is ai-assistant-owned
        // (mobile_business_layer_owner) but physically stored in the
        // mobile deployment root — no separate business layer exists
        // (separate_mobile_business_layer=false).
        _runtimeRoot = Path.Combine(env.ProjectRoot,
            "Standalone tools", "investment-mobile", "runtime");
        _stateDir = Path.Combine(_runtimeRoot, "state");
    }

    public bool OwnsCommand(string command) =>
        OwnedCommands.Contains(command)
        || (command.StartsWith("investment_watch_",
                StringComparison.Ordinal)
            && command.Length < 128);

    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
        => ExecuteWsAsync(command, payload, requestId, null,
            cancellationToken);

    public Task<(string Event, JsonObject Result)> ExecuteWsAsync(
        string command, JsonObject payload, string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken cancellationToken)
    {
        if (!OwnsCommand(command))
            throw new PermissionDeniedException();
        var result = Dispatch(command, payload);
        result["ok"] ??= true;
        result["tool_id"] = _env.ToolId;
        result["request_id"] = requestId;
        return Task.FromResult(($"{command}_result", result));
    }

    // ---------- dispatch ----------

    private JsonObject Dispatch(string command, JsonObject payload)
        => command switch
        {
            // ---- assets / portfolio ---------------------------------
            "investment_assets_summary" => AssetsSummary(),
            "investment_assets_valuation" => AssetsValuation(),
            "investment_assets_allocation" => AssetsAllocation(),
            "investment_assets_exposure" => AssetsExposure(),
            "investment_tw_portfolio" => MarketPortfolio("tw"),
            "investment_us_portfolio" => MarketPortfolio("us"),
            "investment_fund_portfolio" => FundPortfolio(),
            // ---- funds ----------------------------------------------
            "investment_fund_nav" => FundNav(),
            "investment_fund_transactions" => FundTransactions(),
            // ---- market ---------------------------------------------
            "investment_market_status" => MarketStatus(),
            "investment_market_quote" => MarketQuote(payload),
            "investment_market_history" => MarketHistory(payload),
            // ---- autotrade ------------------------------------------
            "investment_autotrade_overview" => AutotradeOverview(),
            "investment_autotrade_performance" =>
                Empty("performance",
                    "no-execution-store",
                    "模擬/實盤成交紀錄儲存尚未建立；目前無任何成交資料"),
            "investment_autotrade_strategies" =>
                Empty("strategies",
                    "no-strategy-store",
                    "策略儲存尚未建立；目前無任何已登錄策略"),
            "investment_autotrade_reports" =>
                Empty("reports",
                    "no-report-store",
                    "操盤報告儲存尚未建立"),
            "investment_autotrade_control" => FailClosed(
                "CONTROL_CHANNEL_PENDING",
                "操盤控制（halt/recover/mode）必須經由 governed channel "
                    + "送往 investment-mobile 引擎宿主；本工具禁止跨工具"
                    + "直連，傳輸後繼尚未上線，命令 fail-closed"),
            // ---- broker ---------------------------------------------
            "investment_broker_accounts" => BrokerAccounts(),
            "investment_broker_status" => BrokerStatus(),
            "investment_broker_sims" => BrokerSims(),
            "investment_broker_imports" =>
                Empty("imports",
                    "no-import-store",
                    "匯入批次紀錄儲存尚未建立；offline-accounts 日誌為空",
                    extra: ("batches", new JsonArray())),
            "investment_broker_import_submit" => FailClosed(
                "IMPORT_WRITE_PATH_PENDING",
                "券商匯入需經 governed outbox 寫入 gptbridge_trading"
                    + "（broker_confirmed=false CHECK）；寫入路徑尚未上線"),
            "investment_broker_import_rollback" => FailClosed(
                "IMPORT_WRITE_PATH_PENDING",
                "匯入回滾需經 governed outbox；寫入路徑尚未上線"),
            // ---- monitor / risk --------------------------------------
            "investment_monitor_events" => MonitorEvents(),
            "investment_monitor_overview" => MonitorOverview(),
            "investment_monitor_reports" =>
                Empty("reports", "no-report-store", "監控報告儲存尚未建立"),
            "investment_monitor_risk" => MonitorRisk(),
            "investment_monitor_recommendations" =>
                Recommendations(null),
            // ---- simulation ------------------------------------------
            "investment_sim_status" => SimStatus(),
            "investment_sim_executions" =>
                Empty("executions",
                    "no-execution-store",
                    "模擬成交紀錄儲存尚未建立；目前無任何成交"),
            "investment_sim_performance" =>
                Empty("performance",
                    "no-execution-store",
                    "無成交紀錄可計算績效"),
            // ---- strategies / backtest --------------------------------
            "investment_strategy_list" =>
                Empty("strategies",
                    "no-strategy-store",
                    "策略儲存尚未建立；目前無任何已登錄策略"),
            "investment_backtest_results" =>
                Empty("results",
                    "no-backtest-store",
                    "回測結果儲存尚未建立；目前無任何回測紀錄"),
            // ---- settings / mode --------------------------------------
            "investment_settings_get" => SettingsGet(),
            "investment_settings_set" => FailClosed(
                "SETTINGS_WRITE_PATH_PENDING",
                "runtime/settings/trading.json 為 investment-mobile "
                    + "受管設定層；本工具僅讀取投影，寫入需經 governed "
                    + "outbox，路徑尚未上線"),
            "investment_trade_mode" => TradeMode(),
            // ---- recommendations --------------------------------------
            "investment_tw_recommendations" => Recommendations("tw"),
            "investment_us_recommendations" => Recommendations("us"),
            "investment_fund_recommendations" =>
                Recommendations("fund"),
            // ---- AI analysis (advisory; xingcheng path pending) -------
            "investment_tw_analysis" => LocalAnalysis("tw"),
            "investment_us_analysis" => LocalAnalysis("us"),
            "investment_fund_analysis" => LocalAnalysis("fund"),
            "investment_ai_portfolio_analysis" => LocalAnalysis("all"),
            "investment_ai_risk" => LocalRisk(),
            "investment_ai_report" => LocalReport(),
            "investment_ai_research_search" => FailClosed(
                "SEARCH_PROVIDER_UNAVAILABLE",
                "即時市場搜尋由內建瀏覽器（embedded-browser-view）持有；"
                    + "後端無對應管道，命令 fail-closed"),
            "investment_ai_consult" => FailClosed(
                "AI_CHANNEL_NOT_CONNECTED",
                "AI 諮詢需經 governed AI channel 送往 xingcheng；"
                    + "transport-store 後繼尚未上線，無法投遞"),
            "investment_star_memory_list" => FailClosed(
                "AI_CHANNEL_NOT_CONNECTED",
                "star memory 由 xingcheng 持有，需經 governed AI channel "
                    + "查詢；transport-store 後繼尚未上線"),
            // ---- perf / health ----------------------------------------
            "investment_perf_health" => PerfHealth(),
            // ---- xingcheng -> ai-assistant relay ----------------------
            "investment_mobile_get_snapshot" => MobileSnapshot(),
            "investment_mobile_submit_instruction" => FailClosed(
                "INSTRUCTION_DATA_PLANE_PENDING",
                "行動指令受理需經 governed outbox 寫入業務層；寫入路徑"
                    + "尚未上線，指令不落地、不產生任何下單動作"),
            // ---- watchlist --------------------------------------------
            _ when command.StartsWith("investment_watch_",
                StringComparison.Ordinal) => Empty("items",
                "no-watchlist-store",
                "自選股/監看清單儲存尚未建立",
                extra: ("watch", new JsonArray())),
            _ => throw new PermissionDeniedException(),
        };

    // ---------- portfolio projections ----------

    private JsonObject AssetsSummary()
    {
        var accounts = ReadJsonArray("accounts.json");
        var registry = ReadJsonObject("investment-accounts.json");
        var cash = ReadJsonArray("cash-balances.json");
        var instruments = ReadJsonArray("instruments.json");
        double cashTotal = 0;
        foreach (var c in cash)
            cashTotal += Num(c, "available");
        var summary = new JsonObject
        {
            ["account_count"] = accounts.Count,
            ["registered_accounts"] = registry?.Count ?? 0,
            ["instrument_count"] = instruments.Count,
            ["cash_total"] = cashTotal,
            ["positions_total"] = 0,
            ["total_value"] = cashTotal,
            ["currency"] = "TWD",
            ["position_store"] = "absent",
        };
        return Ok(new JsonObject
        {
            ["summary"] = summary,
            ["accounts"] = accounts,
            ["cash_balances"] = cash,
            ["positions"] = new JsonArray(),
            ["rows"] = (JsonArray)accounts.DeepClone(),
            ["data_state"] = accounts.Count > 0
                ? "partial-no-positions" : "empty",
        });
    }

    private JsonObject AssetsValuation()
    {
        var cash = ReadJsonArray("cash-balances.json");
        double cashTotal = 0;
        foreach (var c in cash)
            cashTotal += Num(c, "available");
        return Ok(new JsonObject
        {
            ["valuation"] = new JsonObject
            {
                ["total_value"] = cashTotal,
                ["cash"] = cashTotal,
                ["positions_value"] = 0,
                ["currency"] = "TWD",
                ["components"] = new JsonArray(),
            },
            ["positions"] = new JsonArray(),
            ["data_state"] = "cash-only-no-positions",
        });
    }

    private JsonObject AssetsAllocation()
    {
        var accounts = ReadJsonArray("accounts.json");
        var byMarket = new JsonArray();
        var markets = new HashSet<string>(StringComparer.Ordinal);
        foreach (var a in accounts)
        {
            var m = Str(a, "market");
            if (m.Length > 0 && markets.Add(m))
                byMarket.Add(new JsonObject
                {
                    ["market"] = m,
                    ["value"] = 0,
                    ["weight"] = 0,
                    ["account_count"] = CountMarket(accounts, m),
                });
        }
        return Ok(new JsonObject
        {
            ["allocation"] = byMarket,
            ["overlaps"] = new JsonArray(),
            ["shared_exposures"] = new JsonArray(),
            ["recommendations"] = SignalRecommendations(null),
            ["data_state"] = "accounts-only-no-valuations",
        });
    }

    private JsonObject AssetsExposure()
    {
        var signals = ReadJsonl("signals.jsonl");
        var exposures = new JsonArray();
        foreach (var s in signals)
            exposures.Add(new JsonObject
            {
                ["instrument_id"] = Str(s, "instrument_id"),
                ["market"] = Str(s, "market"),
                ["source"] = "signal:" + Str(s, "source"),
                ["advisory"] = true,
            });
        return Ok(new JsonObject
        {
            ["shared_exposures"] = exposures,
            ["overlaps"] = new JsonArray(),
            ["exposure"] = new JsonObject
            {
                ["instruments"] = exposures.Count,
                ["positions_value"] = 0,
            },
            ["data_state"] = "signals-only-no-positions",
        });
    }

    private JsonObject MarketPortfolio(string market)
    {
        var accounts = ReadJsonArray("accounts.json")
            .OfType<JsonObject>()
            .Where(a => string.Equals(Str(a, "market"), market,
                StringComparison.OrdinalIgnoreCase))
            .ToList();
        var acctArr = new JsonArray();
        foreach (var a in accounts)
            acctArr.Add(a.DeepClone());
        var signals = ReadJsonl("signals.jsonl")
            .Where(s => string.Equals(Str(s, "market"), market,
                StringComparison.OrdinalIgnoreCase))
            .ToList();
        return Ok(new JsonObject
        {
            ["market"] = market,
            ["accounts"] = acctArr,
            ["positions"] = new JsonArray(),
            ["holdings"] = new JsonArray(),
            ["summary"] = new JsonObject
            {
                ["account_count"] = accounts.Count,
                ["positions"] = 0,
                ["signal_count"] = signals.Count,
                ["market_value"] = 0,
            },
            ["signals"] = ToArray(signals),
            ["data_state"] = "no-position-store",
        });
    }

    private JsonObject FundPortfolio()
    {
        var txns = ReadJsonl("fund-transactions.jsonl");
        var positions = new JsonArray();
        var byInstrument = new Dictionary<string, (double Units,
            double Amount)>(StringComparer.Ordinal);
        foreach (var t in txns)
        {
            var id = Str(t, "instrument_id");
            if (id.Length == 0)
                continue;
            var cur = byInstrument.TryGetValue(id, out var v)
                ? v : (Units: 0.0, Amount: 0.0);
            var units = Num(t, "units");
            var sign = string.Equals(Str(t, "kind"), "redemption",
                StringComparison.OrdinalIgnoreCase) ? -1.0 : 1.0;
            byInstrument[id] = (cur.Units + sign * units,
                cur.Amount + sign * Num(t, "amount"));
        }
        var navs = ReadJsonl("fund-nav.jsonl");
        foreach (var kv in byInstrument)
        {
            var nav = navs.LastOrDefault(n =>
                Str(n, "instrument_id") == kv.Key);
            positions.Add(new JsonObject
            {
                ["instrument_id"] = kv.Key,
                ["units"] = kv.Value.Item1,
                ["invested"] = kv.Value.Item2,
                ["nav"] = nav is null ? 0 : Num(nav, "nav"),
                ["nav_date"] = nav is null ? "" : Str(nav, "nav_date"),
                ["source"] = "manual-journal",
            });
        }
        return Ok(new JsonObject
        {
            ["positions"] = positions,
            ["funds"] = positions,
            ["data_state"] = positions.Count > 0
                ? "manual-journal" : "empty",
        });
    }

    // ---------- funds ----------

    private JsonObject FundNav()
    {
        var navs = ReadJsonl("fund-nav.jsonl");
        var latest = navs
            .GroupBy(n => Str(n, "instrument_id"))
            .Select(g => (JsonObject)g.Last().DeepClone());
        var latestArr = new JsonArray();
        foreach (var n in latest)
            latestArr.Add(n);
        return Ok(new JsonObject
        {
            ["navs"] = ToArray(navs),
            ["latest"] = latestArr,
            ["history"] = ToArray(navs),
            ["stale"] = navs.Count == 0,
            ["data_state"] = navs.Count > 0 ? "manual-journal" : "empty",
        });
    }

    private JsonObject FundTransactions()
    {
        var txns = ReadJsonl("fund-transactions.jsonl");
        return Ok(new JsonObject
        {
            ["transactions"] = ToArray(txns),
            ["rows"] = ToArray(txns),
            ["data_state"] = txns.Count > 0 ? "manual-journal" : "empty",
        });
    }

    // ---------- market ----------

    private JsonObject MarketStatus()
    {
        var gate = ReadJsonObject("broker-offline-gate.json");
        var obs = ReadJsonArray("market-observations.json");
        var trading = ReadJsonObject(
            Path.Combine("..", "settings", "trading.json"));
        var latest = obs.Count > 0 ? obs[^1] : null;
        return Ok(new JsonObject
        {
            ["status"] = new JsonObject
            {
                ["trading_mode"] = trading is null
                    ? "analysis" : Str(trading, "mode"),
                ["broker_network_enabled"] =
                    gate?["broker_network_enabled"]
                        ?.GetValue<bool>() ?? false,
                ["live_trading_enabled"] =
                    gate?["live_trading_enabled"]
                        ?.GetValue<bool>() ?? false,
                ["observation_count"] = obs.Count,
                ["live_feed"] = "none",
            },
            ["latest_observation"] = latest?.DeepClone(),
            ["observed_at"] = latest is null ? 0 : Num(latest, "observed_at"),
            ["data_state"] = obs.Count > 0
                ? "point-observations" : "empty",
        });
    }

    private JsonObject MarketQuote(JsonObject payload)
    {
        var symbol = Str(payload, "symbol");
        var instrumentId = Str(payload, "instrument_id");
        var obs = ReadJsonArray("market-observations.json");
        var instruments = ReadJsonArray("instruments.json");
        JsonObject? match = null;
        foreach (var o in obs.OfType<JsonObject>().Reverse())
        {
            var oid = Str(o, "instrument_id");
            if (instrumentId.Length > 0 && oid == instrumentId
                || symbol.Length > 0 && oid.Contains(":" + symbol + ":"))
            {
                match = o;
                break;
            }
        }
        JsonObject? instrument = null;
        foreach (var i in instruments.OfType<JsonObject>())
            if (Str(i, "symbol") == symbol
                || Str(i, "instrument_id") == instrumentId)
            {
                instrument = i;
                break;
            }
        var quote = match is null ? null : new JsonObject
        {
            ["instrument_id"] = Str(match, "instrument_id"),
            ["price"] = Num(match, "price"),
            ["currency"] = Str(match, "currency"),
            ["observed_at"] = Num(match, "observed_at"),
            ["source"] = Str(match, "source"),
            ["stale"] = true,
        };
        return Ok(new JsonObject
        {
            ["quote"] = quote,
            ["instrument"] = instrument?.DeepClone(),
            ["data_state"] = match is null
                ? "no-quote" : "point-observation",
        });
    }

    private JsonObject MarketHistory(JsonObject payload)
    {
        var obs = ReadJsonArray("market-observations.json");
        var history = new JsonArray();
        foreach (var o in obs)
            history.Add(new JsonObject
            {
                ["instrument_id"] = Str(o, "instrument_id"),
                ["price"] = Num(o, "price"),
                ["observed_at"] = Num(o, "observed_at"),
                ["kind"] = Str(o, "kind"),
            });
        return Ok(new JsonObject
        {
            ["history"] = history,
            ["candles"] = new JsonArray(),
            ["data_state"] = "point-observations-only-no-candles",
        });
    }

    // ---------- autotrade ----------

    private JsonObject AutotradeOverview()
    {
        var trading = ReadJsonObject(
            Path.Combine("..", "settings", "trading.json"));
        var signals = ReadJsonl("signals.jsonl");
        var decisions = ReadJsonl("decisions.jsonl");
        var audit = ReadJsonl("trading-audit.jsonl");
        return Ok(new JsonObject
        {
            ["product"] = "星澄 AI 投資管理與自動操盤系統",
            ["mode"] = trading is null ? "analysis" : Str(trading, "mode"),
            ["trading_mode"] = trading is null
                ? "analysis" : Str(trading, "mode"),
            ["ai_mode"] = trading is null
                ? "deterministic" : Str(trading, "ai_mode"),
            ["autotrade_state"] = "journal-only",
            ["live_phase_locked"] = true,
            ["orders"] = 0,
            ["executions"] = 0,
            ["recorded_signals"] = signals.Count,
            ["risk_decisions"] = decisions.Count,
            ["trace_entries"] = audit.Count,
            ["engine_channel"] = "investment-mobile-host",
            ["engine_reachable"] = false,
            ["data_state"] = "journal-only",
        });
    }

    // ---------- broker ----------

    private JsonObject BrokerAccounts()
        => Ok(new JsonObject
        {
            ["accounts"] = ReadJsonArray("accounts.json"),
            ["rows"] = ReadJsonArray("accounts.json"),
            ["data_state"] = "registry-journal",
        });

    private JsonObject BrokerStatus()
    {
        var gate = ReadJsonObject("broker-offline-gate.json");
        return Ok(new JsonObject
        {
            ["status"] = gate?.DeepClone() ?? new JsonObject
            {
                ["broker_network_enabled"] = false,
                ["live_trading_enabled"] = false,
            },
            ["gate"] = gate?.DeepClone(),
            ["data_state"] = gate is null ? "absent" : "governed-default",
        });
    }

    private JsonObject BrokerSims()
    {
        var sims = ReadJsonArray(
            Path.Combine("simulation", "paper_accounts.json"));
        return Ok(new JsonObject
        {
            ["simulations"] = sims,
            ["sims"] = ReadJsonArray(
                Path.Combine("simulation", "paper_accounts.json")),
            ["data_state"] = sims.Count > 0 ? "journal" : "empty",
        });
    }

    // ---------- monitor ----------

    private JsonObject MonitorEvents()
    {
        var events = ReadJsonl(
            Path.Combine("monitoring", "events.jsonl"));
        var notifications = ReadJsonl(
            Path.Combine("monitoring", "notifications.jsonl"));
        return Ok(new JsonObject
        {
            ["events"] = ToArray(events),
            ["notifications"] = ToArray(notifications),
            ["data_state"] = "empty-journal",
        });
    }

    private JsonObject MonitorOverview()
    {
        var events = ReadJsonl(
            Path.Combine("monitoring", "events.jsonl"));
        var audit = ReadJsonl("trading-audit.jsonl");
        return Ok(new JsonObject
        {
            ["status"] = new JsonObject
            {
                ["monitor_events"] = events.Count,
                ["audit_entries"] = audit.Count,
                ["rules"] = 0,
                ["alerts"] = 0,
            },
            ["events"] = ToArray(events),
            ["faults"] = new JsonArray(),
            ["data_state"] = "empty-journal",
        });
    }

    private JsonObject MonitorRisk()
    {
        var audit = ReadJsonl("trading-audit.jsonl");
        var riskEvents = new JsonArray();
        foreach (var e in audit)
        {
            if (!Str(e, "type").StartsWith("risk.",
                    StringComparison.Ordinal))
                continue;
            riskEvents.Add(e.DeepClone());
        }
        var decisions = ReadJsonl("decisions.jsonl");
        return Ok(new JsonObject
        {
            ["events"] = riskEvents,
            ["faults"] = new JsonArray(),
            ["decisions"] = ToArray(decisions),
            ["risk_backend"] = "native",
            ["data_state"] = "audit-journal",
        });
    }

    // ---------- simulation ----------

    private JsonObject SimStatus()
    {
        var sims = ReadJsonArray(
            Path.Combine("simulation", "paper_accounts.json"));
        return Ok(new JsonObject
        {
            ["status"] = new JsonObject
            {
                ["paper_accounts"] = sims.Count,
                ["executions"] = 0,
                ["state"] = sims.Count > 0 ? "registered" : "empty",
            },
            ["accounts"] = sims,
            ["data_state"] = sims.Count > 0 ? "journal" : "empty",
        });
    }

    // ---------- settings / mode ----------

    private JsonObject SettingsGet()
    {
        var trading = ReadJsonObject(
            Path.Combine("..", "settings", "trading.json"));
        var gate = ReadJsonObject("broker-offline-gate.json");
        return Ok(new JsonObject
        {
            ["settings"] = trading?.DeepClone() ?? new JsonObject(),
            ["view"] = new JsonObject
            {
                ["trading"] = trading?.DeepClone(),
                ["broker_gate"] = gate?.DeepClone(),
            },
            ["settings_owner"] = "investment-mobile",
            ["writable"] = false,
            ["write_path"] = "governed-outbox-pending",
            ["data_state"] = "read-only-projection",
        });
    }

    private JsonObject TradeMode()
    {
        var trading = ReadJsonObject(
            Path.Combine("..", "settings", "trading.json"));
        var gate = ReadJsonObject("broker-offline-gate.json");
        return Ok(new JsonObject
        {
            ["mode"] = trading is null ? "analysis" : Str(trading, "mode"),
            ["trading_mode"] = trading is null
                ? "analysis" : Str(trading, "mode"),
            ["ai_mode"] = trading is null
                ? "deterministic" : Str(trading, "ai_mode"),
            ["live_phase_locked"] = true,
            ["live_trading_enabled"] =
                gate?["live_trading_enabled"]?.GetValue<bool>() ?? false,
            ["broker_network_enabled"] =
                gate?["broker_network_enabled"]?.GetValue<bool>() ?? false,
            ["risk_limits"] = trading?["risk_limits"]?.DeepClone(),
            ["data_state"] = "settings-journal",
        });
    }

    // ---------- recommendations / analysis ----------

    private JsonObject Recommendations(string? market)
    {
        var recs = SignalRecommendations(market);
        return Ok(new JsonObject
        {
            ["recommendations"] = recs,
            ["rows"] = recs.DeepClone(),
            ["advisory"] = true,
            ["authority"] = "advisory-only-never-orders",
            ["data_state"] = recs.Count > 0
                ? "signal-derived" : "empty",
        });
    }

    private JsonArray SignalRecommendations(string? market)
    {
        var signals = ReadJsonl("signals.jsonl");
        var recs = new JsonArray();
        foreach (var s in signals)
        {
            if (market is not null && !string.Equals(
                    Str(s, "market"), market,
                    StringComparison.OrdinalIgnoreCase))
                continue;
            recs.Add(new JsonObject
            {
                ["signal_id"] = Str(s, "signal_id"),
                ["instrument_id"] = Str(s, "instrument_id"),
                ["market"] = Str(s, "market"),
                ["action"] = Str(s, "side"),
                ["confidence"] = Num(s, "confidence"),
                ["price"] = Num(s, "price"),
                ["source"] = Str(s, "source"),
                ["created_at"] = Num(s, "created_at"),
                ["advisory"] = true,
            });
        }
        return recs;
    }

    private JsonObject LocalAnalysis(string market)
    {
        var summary = AssetsSummary();
        var signals = ReadJsonl("signals.jsonl")
            .Where(s => market == "all" || string.Equals(
                Str(s, "market"), market,
                StringComparison.OrdinalIgnoreCase))
            .ToList();
        return Ok(new JsonObject
        {
            ["market"] = market,
            ["analysis"] = new JsonObject
            {
                ["backend"] = "deterministic-local",
                ["advisory"] = true,
                ["ai_channel"] = "AI_CHANNEL_NOT_CONNECTED",
                ["summary"] = summary["summary"]?.DeepClone(),
                ["signal_count"] = signals.Count,
                ["signals"] = ToArray(signals),
                ["note"] = "本地決定性投影；星澄 AI 分析需經 governed "
                    + "AI channel，目前 transport 後繼未上線",
            },
            ["data_state"] = "deterministic-local",
        });
    }

    private JsonObject LocalRisk()
    {
        var decisions = ReadJsonl("decisions.jsonl");
        var trading = ReadJsonObject(
            Path.Combine("..", "settings", "trading.json"));
        return Ok(new JsonObject
        {
            ["risk"] = new JsonObject
            {
                ["backend"] = "native",
                ["decisions_evaluated"] = decisions.Count,
                ["latest"] = decisions.Count > 0
                    ? decisions[^1].DeepClone() : null,
                ["risk_limits"] = trading?["risk_limits"]?.DeepClone(),
                ["posture"] = "fail-closed-zero-limits",
            },
            ["data_state"] = "decision-journal",
        });
    }

    private JsonObject LocalReport()
        => Ok(new JsonObject
        {
            ["report"] = new JsonObject
            {
                ["backend"] = "deterministic-local",
                ["advisory"] = true,
                ["ai_channel"] = "AI_CHANNEL_NOT_CONNECTED",
                ["portfolio"] = AssetsSummary()["summary"]?.DeepClone(),
                ["generated_by"] = "ai-assistant-local-projection",
            },
            ["reports"] = new JsonArray(),
            ["data_state"] = "deterministic-local",
        });

    // ---------- health ----------

    private JsonObject PerfHealth()
    {
        var files = new JsonObject();
        foreach (var name in new[]
        {
            "accounts.json", "cash-balances.json", "instruments.json",
            "market-observations.json", "broker-offline-gate.json",
            "signals.jsonl", "decisions.jsonl", "fund-nav.jsonl",
            "fund-transactions.jsonl", "trading-audit.jsonl",
        })
            files[name] = File.Exists(Path.Combine(_stateDir, name));
        return Ok(new JsonObject
        {
            ["status"] = new JsonObject
            {
                ["runtime_root_present"] = Directory.Exists(_runtimeRoot),
                ["state_files"] = files,
                ["signals"] = ReadJsonl("signals.jsonl").Count,
                ["decisions"] = ReadJsonl("decisions.jsonl").Count,
            },
            ["data_plane"] = "runtime-journal-read-only",
            ["pending_seams"] = new JsonArray(
                "data-plane:gptbridge_trading-outbox",
                "relay:investment-mobile->xingcheng"),
            ["data_state"] = "healthy",
        });
    }

    // ---------- xingcheng relay ----------

    private JsonObject MobileSnapshot()
    {
        var summary = AssetsSummary();
        var trading = ReadJsonObject(
            Path.Combine("..", "settings", "trading.json"));
        return Ok(new JsonObject
        {
            ["snapshot"] = new JsonObject
            {
                ["summary"] = summary["summary"]?.DeepClone(),
                ["accounts"] = ReadJsonArray("accounts.json"),
                ["cash_balances"] = ReadJsonArray("cash-balances.json"),
                ["positions"] = new JsonArray(),
                ["trading_mode"] = trading is null
                    ? "analysis" : Str(trading, "mode"),
                ["signals"] = ReadJsonl("signals.jsonl").Count,
            },
            ["data_state"] = "journal-projection",
        });
    }

    // ---------- helpers ----------

    private static JsonObject Ok(JsonObject body)
    {
        body["ok"] = true;
        if (!body.ContainsKey("data_plane"))
            body["data_plane"] = "investment-mobile-runtime-journal";
        return body;
    }

    private static JsonObject Empty(string field, string state,
        string message, (string Key, JsonNode? Value)? extra = null)
    {
        var body = new JsonObject
        {
            ["ok"] = true,
            [field] = new JsonArray(),
            ["rows"] = new JsonArray(),
            ["data_state"] = state,
            ["empty"] = true,
            ["message"] = message,
        };
        if (extra is { } e)
            body[e.Key] = e.Value;
        return body;
    }

    private static JsonObject FailClosed(string code, string message)
        => new()
        {
            ["ok"] = false,
            ["error_code"] = code,
            ["message"] = message,
            ["fail_closed"] = true,
        };

    private static string Str(JsonNode? node, string key)
        => node is JsonObject o && o[key] is JsonValue v
            && v.TryGetValue<string>(out var s) ? s : "";

    private static double Num(JsonNode? node, string key)
    {
        if (node is JsonObject o && o[key] is JsonValue v)
        {
            if (v.TryGetValue<double>(out var d))
                return d;
            if (v.TryGetValue<string>(out var s)
                && double.TryParse(s, out var parsed))
                return parsed;
        }
        return 0;
    }

    private static int CountMarket(JsonArray accounts, string market)
        => accounts.OfType<JsonObject>().Count(a =>
            string.Equals(Str(a, "market"), market,
                StringComparison.OrdinalIgnoreCase));

    private static JsonArray ToArray(IEnumerable<JsonObject> items)
    {
        var arr = new JsonArray();
        foreach (var i in items)
            arr.Add(i.DeepClone());
        return arr;
    }

    private JsonArray ReadJsonArray(string relative)
    {
        try
        {
            var path = Path.Combine(_stateDir, relative);
            if (!File.Exists(path))
                return new JsonArray();
            var node = JsonNode.Parse(File.ReadAllText(path));
            return node as JsonArray ?? new JsonArray();
        }
        catch (JsonException) { return new JsonArray(); }
        catch (IOException) { return new JsonArray(); }
    }

    private JsonObject? ReadJsonObject(string relative)
    {
        try
        {
            var path = Path.Combine(_stateDir, relative);
            if (!File.Exists(path))
                return null;
            return JsonNode.Parse(File.ReadAllText(path))
                as JsonObject;
        }
        catch (JsonException) { return null; }
        catch (IOException) { return null; }
    }

    private List<JsonObject> ReadJsonl(string relative)
    {
        var items = new List<JsonObject>();
        try
        {
            var path = Path.Combine(_stateDir, relative);
            if (!File.Exists(path))
                return items;
            foreach (var line in File.ReadLines(path))
            {
                if (line.Trim().Length == 0)
                    continue;
                if (JsonNode.Parse(line) is JsonObject obj)
                    items.Add(obj);
            }
        }
        catch (JsonException) { }
        catch (IOException) { }
        return items;
    }

    public JsonObject Health() => new()
    {
        ["executor_state"] = "ai-assistant",
        ["surface_size"] = OwnedCommands.Count,
        ["surface_families"] = new JsonArray(
            "investment_*", "investment_watch_*",
            "investment_mobile_*"),
        ["data_plane"] = "investment-mobile-runtime-journal-read-only",
        ["data_root"] = _stateDir,
        ["data_root_present"] = Directory.Exists(_stateDir),
        ["pending_seams"] = new JsonArray(
            "data-plane:gptbridge_trading-outbox",
            "relay:investment-mobile->xingcheng",
            "control:ai-assistant->investment-mobile"),
        ["transport_state"] = _env.SidecarDeferred
            ? "deferred" : "sidecar",
    };
}
