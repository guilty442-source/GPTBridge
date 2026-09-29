// 星澄 AI 投資管理與自動操盤系統 — governed tool-host entry point.
//
// This is the manifest runtime.native_entry for investment-mobile: the
// retired src/channel_runtime.py lane (B167/B38) is replaced by this
// governed C# host. The shared ToolHostProgram validates the injected
// governed environment (identity, sealed manifest, IPC token/port,
// transport sidecar) — any failure exits 13 PERMISSION_DENIED, so the
// main-system backend reports SOURCE_RUNTIME_EXITED rather than
// running ungoverned.
//
// Channel binding: the submit-side governed transport successor has
// not landed yet, so the xingcheng relay client is constructed
// unbound — every snapshot/instruction request returns the honest
// fail-closed AI_CHANNEL_NOT_CONNECTED instead of fabricating data.
// The trading engine cluster composes signal intake → strategy →
// native risk → OMS with default_mode ANALYSIS unless the governed
// runtime/settings/trading.json selects SHADOW/PAPER. LIVE stays
// phase-locked: selecting it fails the engine closed.

using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;
using InvestmentMobile.Oms;
using InvestmentMobile.Service;

namespace InvestmentMobile.ToolHost;

/// <summary>Executor binding the sealed command contract to the
/// service; PERMISSION_DENIED propagates as the host's denied exit.</summary>
internal sealed class InvestmentMobileExecutor : IGovernedCommandExecutor
{
    private readonly InvestmentMobileService _service;

    public InvestmentMobileExecutor(InvestmentMobileService service)
        => _service = service;

    public async Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
    {
        var requester =
            payload["_governed_requester_actor"]?.GetValue<string>();
        payload.Remove("_governed_requester_actor");
        if (_service.ExecuteGate(requester, command) != "ALLOW")
            throw new PermissionDeniedException();
        var result = await _service.HandleAsync(
            command, payload, requester ?? "", cancellationToken);
        if (result.Event == "PERMISSION_DENIED")
            throw new PermissionDeniedException();
        return result;
    }

    public JsonObject Health() => new()
    {
        ["executor_state"] = "investment-mobile",
        ["started"] = _service.Started,
    };
}

internal static class Program
{
    /// <summary>runtime/settings/trading.json — governed settings layer
    /// (manifest settings_owner=investment-mobile). Absent → ANALYSIS +
    /// empty risk limits, which fail every order closed.</summary>
    private static (TradingMode Mode, RiskLimits Limits)
        LoadTradingSettings(string toolRoot)
    {
        var mode = TradingMode.Analysis;
        var limits = new RiskLimits();
        var path = Path.Combine(
            toolRoot, "runtime", "settings", "trading.json");
        try
        {
            if (!File.Exists(path))
                return (mode, limits);
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var el = doc.RootElement;
            if (el.TryGetProperty("mode", out var m)
                && m.ValueKind == JsonValueKind.String
                && Enum.TryParse<TradingMode>(
                    m.GetString(), ignoreCase: true, out var parsed))
                mode = parsed;
            if (el.TryGetProperty("risk_limits", out var rl)
                && rl.ValueKind == JsonValueKind.Object)
                limits = new RiskLimits
                {
                    MaxOrderNotional =
                        GetDouble(rl, "max_order_notional"),
                    MaxPositionNotional =
                        GetDouble(rl, "max_position_notional"),
                    MaxDailyLoss = GetDouble(rl, "max_daily_loss"),
                    MaxOrdersPerDay =
                        (int)GetDouble(rl, "max_orders_per_day"),
                    MaxSinglePositionWeight =
                        GetDouble(rl, "max_single_position_weight"),
                    RequirePrice =
                        (int)GetDouble(rl, "require_price"),
                    AllowedMarketMask =
                        (uint)GetDouble(rl, "allowed_market_mask"),
                    MaxOpenOrders =
                        (int)GetDouble(rl, "max_open_orders"),
                    MinCashBuffer =
                        GetDouble(rl, "min_cash_buffer"),
                };
        }
        catch (JsonException)
        {
            // Corrupt governed settings → fail closed, keep defaults.
            return (TradingMode.Analysis, new RiskLimits());
        }
        return (mode, limits);

        static double GetDouble(JsonElement el, string name) =>
            el.TryGetProperty(name, out var v)
                && v.ValueKind == JsonValueKind.Number
                    ? v.GetDouble() : 0.0;
    }

    public static async Task<int> Main() =>
        await ToolHostProgram.RunAsync(env =>
        {
            var (mode, limits) = LoadTradingSettings(env.ToolRoot);
            var cluster = new TradingEngineCluster(
                Path.Combine(env.ToolRoot, "runtime", "state"),
                mode, limits);
            var service = new InvestmentMobileService(
                new XingchengChannelClient(null), cluster);
            return (IGovernedCommandExecutor)
                new InvestmentMobileExecutor(service);
        }, "1.0.0", processingChannels: ["system"]);
}
