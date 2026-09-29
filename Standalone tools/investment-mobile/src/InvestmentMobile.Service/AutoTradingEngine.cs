// 星澄 AI 投資管理與自動操盤系統 — AutoTradingEngine (C#).
//
// Bounded orchestration tick over the existing signal book, strategy
// evaluation, optional 星澄 advisory and the OMS risk/mode gates.
// Contract (manifest trading_system.autotrading):
//   scope SHADOW/PAPER only — LIVE stays phase-locked, ANALYSIS yields
//   mode-blocked evidence orders;
//   pipeline MarketEvent → DataValidation → StrategyEvaluation →
//   TradingSignal → AIAnalysis(optional) → TradeProposal →
//   RiskDecision → Order;
//   ai_integration DETERMINISTIC | AI_ASSISTED — stale/unavailable
//   evidence blocks dependent trades (MODEL_BLOCKED); AI never lifts a
//   halt (HALT requires a human-authorized resume).

using System.Text.Json.Nodes;
using InvestmentMobile.Oms;

namespace InvestmentMobile.Service;

public enum AutoTradingState
{
    Created,
    Ready,
    Running,
    Paused,
    RiskHalted,
    DataBlocked,
    ModelBlocked,
    Recovering,
    Stopped,
    Failed,
}

public enum AiIntegrationMode
{
    Deterministic,
    AiAssisted,
}

/// <summary>One pipeline-trace entry per drained signal/proposal.</summary>
public sealed class AutotradeTrace
{
    public required string SignalId { get; init; }
    public required string Stage { get; init; }
    public required string Outcome { get; init; }
    public string? OrderId { get; init; }
    public string? Detail { get; init; }
}

public sealed class AutoTradingEngine
{
    private readonly OrderManagementSystem _oms;
    private readonly SignalBook _book;
    private readonly AiSignalIntake _intake;
    private readonly XingchengChannelClient? _advisory;
    private readonly HashSet<string> _processed = new();
    private readonly List<AutotradeTrace> _trace = new();

    public AutoTradingState State { get; private set; } =
        AutoTradingState.Created;
    public AiIntegrationMode AiMode { get; set; } =
        AiIntegrationMode.Deterministic;

    public AutoTradingEngine(
        OrderManagementSystem oms,
        SignalBook book,
        AiSignalIntake intake,
        XingchengChannelClient? advisoryChannel = null)
    {
        _oms = oms;
        _book = book;
        _intake = intake;
        _advisory = advisoryChannel;
        if (_oms.Mode == TradingMode.Live)
            State = AutoTradingState.Failed;
        else
            State = AutoTradingState.Ready;
    }

    /// <summary>Configure is refused outright under LIVE — the phase
    /// lock leaves the engine unusable rather than half-armed.</summary>
    public string Configure()
    {
        if (_oms.Mode == TradingMode.Live)
        {
            State = AutoTradingState.Failed;
            return "LIVE_PHASE_LOCKED";
        }
        if (State == AutoTradingState.Created)
            State = AutoTradingState.Ready;
        return "OK";
    }

    public IReadOnlyList<AutotradeTrace> Trace => _trace;

    /// <summary>Human halt — immediate.</summary>
    public void Halt() => State = AutoTradingState.RiskHalted;

    /// <summary>Resume requires an authorized human actor — the AI
    /// path cannot lift a halt (contract: AI never lifts a halt). The
    /// caller supplies the adjudication result; nothing internal to
    /// the engine can set <paramref name="authorized"/>.</summary>
    public bool Resume(bool authorized)
    {
        if (State != AutoTradingState.RiskHalted)
            return false;
        if (!authorized)
            return false;
        State = AutoTradingState.Ready;
        return true;
    }

    public void Pause()
    {
        if (State == AutoTradingState.Running
            || State == AutoTradingState.Ready)
            State = AutoTradingState.Paused;
    }

    /// <summary>
    /// One bounded tick: drain new signals and AI proposals, evaluate
    /// each through strategy → optional advisory → risk+mode OMS gates.
    /// Advisory is consulted only under AI_ASSISTED; a missing channel
    /// under that mode marks the tick MODEL_BLOCKED (fail-closed for
    /// dependent trades, events preserved).
    /// </summary>
    public async Task<IReadOnlyList<AutotradeTrace>> RunOnceAsync(
        CancellationToken ct = default)
    {
        if (State is AutoTradingState.Paused
            or AutoTradingState.RiskHalted
            or AutoTradingState.Stopped
            or AutoTradingState.Failed)
            return Array.Empty<AutotradeTrace>();

        var modelBlocked = AiMode == AiIntegrationMode.AiAssisted
            && (_advisory is null || !_advisory.Connected);
        if (modelBlocked)
            State = AutoTradingState.ModelBlocked;
        else if (State == AutoTradingState.ModelBlocked)
            State = AutoTradingState.Ready;

        State = AutoTradingState.Running;
        var emitted = new List<AutotradeTrace>();

        foreach (var proposal in PipelineProposals())
        {
            ct.ThrowIfCancellationRequested();
            var trace = new AutotradeTrace
            {
                SignalId = proposal.SignalId,
                Stage = "risk_decision",
                Outcome = "",
            };
            if (AiMode == AiIntegrationMode.AiAssisted)
            {
                var advisory = await _advisory!.SubmitInstructionAsync(
                    new JsonObject
                    {
                        ["operation"] = "ai_analysis",
                        ["instruction"] = "trade-proposal-review",
                        ["proposal"] = new JsonObject
                        {
                            ["instrument_id"] = proposal.InstrumentId,
                            ["market"] = proposal.Market,
                            ["side"] = proposal.Side,
                            ["quantity"] = proposal.Quantity,
                            ["price"] = proposal.Price,
                            ["strategy_id"] = proposal.StrategyId,
                            ["signal_id"] = proposal.SignalId,
                        },
                    });
                if (XingchengRoute.OkIsFalse(advisory))
                {
                    emitted.Add(new AutotradeTrace
                    {
                        SignalId = proposal.SignalId,
                        Stage = "ai_analysis",
                        Outcome = "evidence-unavailable",
                        Detail = advisory["error_code"]
                            ?.GetValue<string>(),
                    });
                    continue;   // dependent trade blocked
                }
            }
            var order = _oms.Submit(proposal);
            emitted.Add(new AutotradeTrace
            {
                SignalId = proposal.SignalId,
                Stage = "order",
                Outcome = order.Status.ToString(),
                OrderId = order.OrderId,
                Detail = order.Rejection.Length > 0
                    ? order.Rejection : null,
            });
            if (order.Status == OrderStatus.RiskRejected
                && order.Rejection.Contains("daily_loss"))
                State = AutoTradingState.RiskHalted;
        }
        _trace.AddRange(emitted);
        if (State == AutoTradingState.Running)
            State = modelBlocked
                ? AutoTradingState.ModelBlocked
                : AutoTradingState.Ready;
        return emitted;
    }

    /// <summary>New signal-book proposals + drained AI intake
    /// proposals (signal-follow evaluation + dedup).</summary>
    private IEnumerable<TradeProposal> PipelineProposals()
    {
        foreach (var proposal in _book.Evaluate())
            if (_processed.Add(proposal.SignalId + proposal.StrategyId))
                yield return proposal;
        foreach (var proposal in _intake.DrainProposals())
            yield return proposal;
    }
}
