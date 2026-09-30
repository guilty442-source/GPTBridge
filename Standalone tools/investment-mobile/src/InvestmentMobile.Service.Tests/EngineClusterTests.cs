// 星澄 AI 投資管理與自動操盤系統 — engine-cluster command surface.
//
// The cluster owns only the ported commands; everything else fails
// closed. Recover requires the governance main actor — self/tool/AI
// requesters cannot lift a halt.

using System.Text.Json.Nodes;
using InvestmentMobile.Oms;
using InvestmentMobile.Service;
using Xunit;

namespace InvestmentMobile.Service.Tests;

public sealed class EngineClusterTests
{
    private static string TempDir()
    {
        var dir = Path.Combine(
            Path.GetTempPath(), $"imcl-{Guid.NewGuid():N}");
        Directory.CreateDirectory(dir);
        return dir;
    }

    private static TradingEngineCluster Cluster(
        TradingMode mode = TradingMode.Paper) =>
        new(TempDir(), mode,
            new RiskLimits { AllowedMarketMask = 0b111 },
            adapters: new Dictionary<string, IBrokerAdapter>());

    private const string Main = "governance/main-system";
    private const string Self = "governance/tool/investment-mobile";

    [Theory]
    [InlineData("investment-mobile-signal-ingest", true)]
    [InlineData("investment-mobile-autotrade-cycle", true)]
    [InlineData("investment-mobile-autotrade-halt", true)]
    [InlineData("investment-mobile-autotrade-recover", true)]
    [InlineData("investment-mobile-autotrade-overview", true)]
    [InlineData("investment-mobile-autotrade-risk-check", true)]
    [InlineData("investment-mobile-backtest-run", false)]
    [InlineData("investment-mobile-fund-analyze", false)]
    public void Owns_only_ported_engine_commands(
        string command, bool expected) =>
        Assert.Equal(expected, TradingEngineCluster.Owns(command));

    [Fact]
    public async Task Signal_ingest_records_into_book()
    {
        var cluster = Cluster();
        var res = await cluster.HandleAsync(
            "investment-mobile-signal-ingest",
            new JsonObject
            {
                ["instrument_id"] = "2330", ["market"] = "tw",
                ["confidence"] = 0.8, ["quantity"] = 5,
                ["price"] = 100,
            }, Self);
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.Single(cluster.Signals.Signals());
    }

    [Fact]
    public async Task Signal_list_returns_recorded()
    {
        var cluster = Cluster();
        await cluster.HandleAsync("investment-mobile-signal-ingest",
            new JsonObject { ["instrument_id"] = "2330" }, Self);
        var res = await cluster.HandleAsync(
            "investment-mobile-signal-list", new JsonObject(), Self);
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.Single((JsonArray)res["signals"]!);
    }

    [Fact]
    public async Task Halt_then_recover_requires_main_actor()
    {
        var cluster = Cluster();
        await cluster.HandleAsync(
            "investment-mobile-autotrade-halt", new JsonObject(), Main);
        Assert.Equal(AutoTradingState.RiskHalted,
            cluster.Autotrade.State);

        // self/tool requester cannot lift the halt
        var denied = await cluster.HandleAsync(
            "investment-mobile-autotrade-recover",
            new JsonObject(), Self);
        Assert.False(denied["ok"]!.GetValue<bool>());
        Assert.Equal("RESUME_REQUIRES_GOVERNANCE",
            denied["error_code"]!.GetValue<string>());
        Assert.Equal(AutoTradingState.RiskHalted,
            cluster.Autotrade.State);

        var ok = await cluster.HandleAsync(
            "investment-mobile-autotrade-recover",
            new JsonObject(), Main);
        Assert.True(ok["ok"]!.GetValue<bool>());
        Assert.NotEqual(AutoTradingState.RiskHalted,
            cluster.Autotrade.State);
    }

    [Fact]
    public async Task Risk_check_is_decision_only()
    {
        var cluster = Cluster();
        var res = await cluster.HandleAsync(
            "investment-mobile-autotrade-risk-check",
            new JsonObject
            {
                ["instrument_id"] = "2330", ["market"] = "tw",
                ["quantity"] = 10, ["price"] = 100,
                ["cash_after"] = 5000,
            }, Self);
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.True(res.ContainsKey("approved"));
        // decision-only: no order was created
        Assert.Empty(cluster.Oms.Orders);
    }

    [Fact]
    public async Task Cycle_consumes_signals_through_gates()
    {
        var cluster = Cluster(TradingMode.Paper);
        await cluster.HandleAsync("investment-mobile-signal-ingest",
            new JsonObject
            {
                ["instrument_id"] = "2330", ["market"] = "tw",
                ["confidence"] = 0.9, ["quantity"] = 10,
                ["price"] = 100,
            }, Self);
        var res = await cluster.HandleAsync(
            "investment-mobile-autotrade-cycle",
            new JsonObject(), Main);
        Assert.True(res["ok"]!.GetValue<bool>());
        // native risk dll absent in test env → fail-closed rejection,
        // or a paper fill when present; either way the signal crossed
        // the gates and was processed once.
        Assert.Equal(1, res["emitted"]!.GetValue<int>());
    }

    [Fact]
    public async Task Overview_reports_state_and_counts()
    {
        var cluster = Cluster();
        var res = await cluster.HandleAsync(
            "investment-mobile-autotrade-overview",
            new JsonObject(), Main);
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.Equal("paper",
            res["trading_mode"]!.GetValue<string>());
        Assert.True(res.ContainsKey("autotrade_state"));
        Assert.True(res.ContainsKey("risk_backend"));
    }

    [Fact]
    public async Task Service_routes_engine_commands_via_handle_async()
    {
        var cluster = Cluster();
        var svc = new InvestmentMobileService(
            new XingchengChannelClient(null), cluster);
        Assert.Equal("ALLOW",
            svc.ExecuteGate(Self,
                "investment-mobile-signal-ingest"));
        var (evt, res) = await svc.HandleAsync(
            "investment-mobile-signal-ingest",
            new JsonObject { ["instrument_id"] = "2330" }, Self);
        Assert.Equal("investment-mobile-signal-ingest_result", evt);
        Assert.True(res["ok"]!.GetValue<bool>());
    }

    [Fact]
    public void Status_includes_engine_fields_when_composed()
    {
        var cluster = Cluster();
        var svc = new InvestmentMobileService(
            new XingchengChannelClient(null), cluster);
        var status = svc.StatusResult("investment-mobile-status");
        Assert.Equal("星澄 AI 投資管理與自動操盤系統",
            status["product"]!.GetValue<string>());
        Assert.Equal("paper",
            status["trading_mode"]!.GetValue<string>());
        Assert.NotNull(status["engines"]);
    }

    [Fact]
    public void Live_mode_cluster_fails_closed()
    {
        var cluster = Cluster(TradingMode.Live);
        Assert.Equal(AutoTradingState.Failed,
            cluster.Autotrade.State);
    }
}
