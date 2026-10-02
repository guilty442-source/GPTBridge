// ai-assistant governed executor — declares the registered investment
// command surface (codex command directory: INVESTMENT_WATCH_* +
// INVESTMENT_AI_CONSULT + INVESTMENT_STAR_MEMORY_LIST, plus the
// renderer's ``investment_*`` frame names) on the WS lane.
//
// Every command answers with a typed INVESTMENT_DATA_PLANE_PENDING
// result: the formal data authority (PostgreSQL ``gptbridge_trading``,
// module_id 'ai-assistant', RLS-gated) and the governed
// investment-mobile → xingcheng relay both require the transport-store
// successor, which is not yet running for this host. The retired
// Python analysis fabric (B167/B38) is gone; there is no native data
// plane to delegate to, and inventing one — or reading
// investment-mobile's state files, which the codex forbids
// (direct access to other tools' data) — would violate fail-closed
// doctrine. Commands outside the registered surface stay
// PERMISSION_DENIED at the allowlist boundary.
//
// NOTE: this file sits inside the
// devin-desktop-investment-mobile-availability-fixes-20261002 claim
// scope; if that worker lands a richer implementation, this file is
// superseded by it.

using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal sealed class AiAssistantExecutor
    : IGovernedCommandExecutor, IWsCommandSurface
{
    private static readonly HashSet<string> OwnedCommands = new(
        StringComparer.Ordinal)
    {
        // Renderer surface (src/ui/workspace/*).
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
    };

    private readonly GovernedEnvironment _env;

    public AiAssistantExecutor(GovernedEnvironment env) => _env = env;

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
        return Task.FromResult(($"{command}_result", new JsonObject
        {
            ["ok"] = false,
            ["tool_id"] = _env.ToolId,
            ["error_code"] = "INVESTMENT_DATA_PLANE_PENDING",
            ["message"] = "投資資料面（PostgreSQL gptbridge_trading 受管"
                + " outbox 與 investment-mobile → xingcheng 中繼）"
                + "的原生傳輸後繼尚未上線；命令 fail-closed，"
                + "不產生任何分析或交易資料",
            ["data_plane_state"] = "authority-write-path-port-pending",
            ["relay_chain"] = "investment-mobile -> xingcheng -> "
                + "ai-assistant",
        }));
    }

    public JsonObject Health() => new()
    {
        ["executor_state"] = "ai-assistant",
        ["surface_size"] = OwnedCommands.Count,
        ["surface_families"] = new JsonArray(
            "investment_*", "investment_watch_*"),
        ["pending_seams"] = new JsonArray(
            "data-plane:gptbridge_trading-outbox",
            "relay:investment-mobile->xingcheng"),
    };
}
