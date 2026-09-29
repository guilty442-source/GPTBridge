// 星澄 AI 投資管理與自動操盤系統 — trading engine cluster (C# port of
// trading/engine_service.py composition surface, integration scope).
//
// Composes the native pipeline: SignalBook → strategy evaluation →
// optional 星澄 advisory → NativeRiskGate → OMS mode gate. Only the
// commands backed by ported native components are owned; every other
// domain command stays unowned and fails closed PERMISSION_DENIED —
// an honest "not ported yet" instead of a fabricated result.
//
// AI boundary: signals/proposals enter via ingest; orders exist only
// as pipeline output. Nothing here exposes OrderRequest, broker
// adapters or risk-limit mutation to a caller.

using System.Text.Json.Nodes;
using InvestmentMobile.Oms;

namespace InvestmentMobile.Service;

public sealed class TradingEngineCluster
{
    /// <summary>Owned engine commands — the ported subset of the
    /// retired engine surface (signal intake, autotrade cycle/halt/
    /// recover/overview/risk-check). Domains without a native
    /// successor (backtest/fund/market-data batches/…) remain unowned
    /// until their governed ports land.</summary>
    private static readonly IReadOnlySet<string> EngineCommands =
        new HashSet<string>
        {
            "investment-mobile-signal-ingest",
            "investment-mobile-signal-list",
            "investment-mobile-autotrade-cycle",
            "investment-mobile-autotrade-halt",
            "investment-mobile-autotrade-recover",
            "investment-mobile-autotrade-overview",
            "investment-mobile-autotrade-risk-check",
            "investment-mobile-autotrade-ai-mode",
        };

    private static readonly IReadOnlyList<string> EngineNames =
        new[] { "strategy", "risk", "oms", "broker-adapter" };

    public SignalBook Signals { get; }
    public AiSignalIntake Intake { get; }
    public AutoTradingEngine Autotrade { get; }
    public OrderManagementSystem Oms { get; }
    public NativeRiskGate Risk { get; }

    public TradingEngineCluster(
        string stateDir,
        TradingMode mode,
        RiskLimits limits,
        IReadOnlyDictionary<string, IBrokerAdapter>? adapters = null,
        IXingchengChannel? advisoryChannel = null,
        IStrategyEvaluator? evaluator = null)
    {
        Signals = new SignalBook(stateDir, evaluator);
        Intake = new AiSignalIntake(Signals);
        Risk = NativeRiskGate.CreateDefault(limits);
        Oms = new OrderManagementSystem(
            Risk, adapters ?? new Dictionary<string, IBrokerAdapter>
            {
                ["tw"] = new CathaySecuritiesAdapter(),
                ["us"] = new FubonSubBrokerageAdapter(),
                ["fund"] = new FundPlatformAdapter("unverified"),
            })
        { Mode = mode };
        Autotrade = new AutoTradingEngine(
            Oms, Signals, Intake,
            advisoryChannel is null
                ? null
                : new XingchengChannelClient(advisoryChannel));
    }

    public static bool Owns(string command) =>
        EngineCommands.Contains(command);

    /// <summary>Health/status contribution merged into the service
    /// status result — mirrors the retired engines + trading_mode
    /// fields.</summary>
    public void ContributeStatus(JsonObject status)
    {
        status["product"] = "星澄 AI 投資管理與自動操盤系統";
        status["trading_mode"] =
            Oms.Mode.ToString().ToLowerInvariant();
        status["autotrade_state"] =
            Autotrade.State.ToString().ToLowerInvariant();
        status["ai_mode"] =
            Autotrade.AiMode.ToString().ToLowerInvariant();
        status["risk_backend"] = Risk.Backend;
        status["strategy_backend"] = Signals.Backend;
        status["engines"] = new JsonArray(
            EngineNames.Select(n => (JsonNode)n).ToArray());
    }

    /// <summary>Bounded command dispatch — every handler is a single
    /// bounded operation; scheduling stays with the governed host.</summary>
    public async Task<JsonObject> HandleAsync(
        string command, JsonObject payload, string requester,
        CancellationToken ct = default)
    {
        switch (command)
        {
            case "investment-mobile-signal-ingest":
                return Intake.SubmitSignal(payload);
            case "investment-mobile-signal-list":
                return new JsonObject
                {
                    ["ok"] = true,
                    ["signals"] = new JsonArray(Signals.Signals(
                        limit: (int)JsonNums.Num(payload["limit"]) is var l
                            && l > 0 ? l : 100)
                        .Select(s => (JsonNode)s.ToJson()).ToArray()),
                };
            case "investment-mobile-autotrade-cycle":
            {
                var trace = await Autotrade.RunOnceAsync(ct);
                return new JsonObject
                {
                    ["ok"] = true,
                    ["state"] = Autotrade.State.ToString()
                        .ToLowerInvariant(),
                    ["emitted"] = trace.Count,
                    ["trace"] = new JsonArray(trace.Select(
                        t => (JsonNode)new JsonObject
                        {
                            ["signal_id"] = t.SignalId,
                            ["stage"] = t.Stage,
                            ["outcome"] = t.Outcome,
                            ["order_id"] = t.OrderId,
                            ["detail"] = t.Detail,
                        }).ToArray()),
                };
            }
            case "investment-mobile-autotrade-halt":
                Autotrade.Halt();
                return new JsonObject
                {
                    ["ok"] = true,
                    ["state"] = "risk_halted",
                };
            case "investment-mobile-autotrade-recover":
                // Human-authorized resume: only the governance main
                // actor counts — tool/self/AI callers cannot lift a halt.
                if (requester != XingchengRoute.GovernanceMainActor)
                    return new JsonObject
                    {
                        ["ok"] = false,
                        ["error_code"] = "RESUME_REQUIRES_GOVERNANCE",
                    };
                if (!Autotrade.Resume(authorized: true))
                    return new JsonObject
                    {
                        ["ok"] = false,
                        ["error_code"] = "RESUME_NOT_AUTHORIZED",
                    };
                return new JsonObject
                {
                    ["ok"] = true,
                    ["state"] = Autotrade.State.ToString()
                        .ToLowerInvariant(),
                };
            case "investment-mobile-autotrade-ai-mode":
            {
                var mode = (payload["mode"]?.GetValue<string>() ?? "")
                    .Trim().ToLowerInvariant();
                Autotrade.AiMode = mode == "ai_assisted"
                    ? AiIntegrationMode.AiAssisted
                    : AiIntegrationMode.Deterministic;
                return new JsonObject
                {
                    ["ok"] = true,
                    ["ai_mode"] =
                        Autotrade.AiMode.ToString().ToLowerInvariant(),
                };
            }
            case "investment-mobile-autotrade-risk-check":
            {
                // Decision-only evaluation — returns the risk verdict
                // without creating any order (A297 boundary).
                var decision = Risk.EvaluateWith(
                    new TradeProposal(
                        InstrumentId: payload["instrument_id"]
                            ?.GetValue<string>() ?? "",
                        Market: payload["market"]
                            ?.GetValue<string>() ?? "",
                        Side: payload["side"]
                            ?.GetValue<string>() ?? "buy",
                        Quantity: JsonNums.Num(payload["quantity"]),
                        Price: JsonNums.NumOrNull(payload["price"]),
                        StrategyId: "risk-check",
                        SignalId: payload["signal_id"]
                            ?.GetValue<string>() ?? ""),
                    new RiskOrderInput
                    {
                        CashAfter = JsonNums.Num(payload["cash_after"]),
                        TotalPortfolioValue =
                            JsonNums.Num(payload["portfolio_value"]),
                        ExistingPositionNotional =
                            JsonNums.Num(payload["position_notional"]),
                        ExistingPositionValue =
                            JsonNums.Num(payload["position_value"]),
                        DailyOrderCount =
                            (int)JsonNums.Num(payload["daily_orders"]),
                        DailyRealizedPnl =
                            JsonNums.Num(payload["daily_pnl"]),
                        OpenOrderCount =
                            (int)JsonNums.Num(payload["open_orders"]),
                    });
                return new JsonObject
                {
                    ["ok"] = true,
                    ["approved"] = decision.Approved,
                    ["reasons"] = new JsonArray(decision.Reasons.Select(
                        r => (JsonNode)r).ToArray()),
                    ["backend"] = Risk.Backend,
                };
            }
            case "investment-mobile-autotrade-overview":
            {
                var status = new JsonObject { ["ok"] = true };
                ContributeStatus(status);
                status["orders"] = Oms.Orders.Count;
                status["executions"] = Oms.Executions.Count;
                status["recorded_signals"] = Signals.Signals().Count;
                status["pending_ai_proposals"] =
                    Intake.PendingProposals.Count;
                status["trace_entries"] = Autotrade.Trace.Count;
                return status;
            }
            default:
                return new JsonObject
                {
                    ["ok"] = false,
                    ["error_code"] = "COMMAND_NOT_OWNED",
                    ["command"] = command,
                };
        }
    }
}
