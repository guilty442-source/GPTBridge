// 星澄 AI 投資管理與自動操盤系統 — the ONLY AI entry point (C# port
// of trading/ai_boundary.py).
//
// 星澄 may submit TradingSignal payloads and TradeProposal-shaped
// analysis payloads through this boundary:
//   - signals go to the signal book (advisory)
//   - proposals are recorded for traceability and enter the same
//     governed pipeline as strategy-generated proposals — they never
//     skip risk
//   - no AI path can modify risk limits, create OrderRequest, or reach
//     a broker adapter (those types are unreachable from here)

using System.Text.Json.Nodes;
using InvestmentMobile.Oms;

namespace InvestmentMobile.Service;

public sealed class AiSignalIntake
{
    private readonly SignalBook _book;
    private readonly List<TradeProposal> _proposals = new();

    public AiSignalIntake(SignalBook book)
    {
        _book = book;
    }

    /// <summary>Recorded AI proposals (traceability — still must pass
    /// the risk + mode gates downstream).</summary>
    public IReadOnlyList<TradeProposal> PendingProposals => _proposals;

    /// <summary>submit_signal — advisory signal into the book.</summary>
    public JsonObject SubmitSignal(JsonObject payload)
    {
        var signal = new TradingSignal
        {
            InstrumentId =
                payload["instrument_id"]?.GetValue<string>() ?? "",
            Market = payload["market"]?.GetValue<string>() ?? "",
            Side = payload["side"]?.GetValue<string>() ?? "buy",
            Confidence = JsonNums.Num(payload["confidence"]),
            Price = JsonNums.NumOrNull(payload["price"]),
            Quantity = JsonNums.Num(payload["quantity"]),
            Rationale =
                payload["rationale"]?.GetValue<string>() ?? "",
            Source = payload["source"]?.GetValue<string>()
                ?? "xingcheng",
        };
        return _book.Record(signal);
    }

    /// <summary>submit_proposal — proposal-shaped payload validated
    /// then queued for the governed pipeline. INVALID_PROPOSAL is
    /// fail-closed.</summary>
    public JsonObject SubmitProposal(JsonObject payload)
    {
        var proposal = new TradeProposal(
            InstrumentId:
                payload["instrument_id"]?.GetValue<string>() ?? "",
            Market: payload["market"]?.GetValue<string>() ?? "",
            Side: payload["side"]?.GetValue<string>() ?? "buy",
            Quantity: JsonNums.Num(payload["quantity"]),
            Price: JsonNums.NumOrNull(payload["price"]),
            StrategyId:
                payload["strategy_id"]?.GetValue<string>()
                ?? "ai-proposal",
            SignalId: payload["signal_id"]?.GetValue<string>() ?? "");
        if (proposal.InstrumentId.Length == 0 || proposal.Quantity <= 0)
            return new JsonObject
            {
                ["ok"] = false, ["error_code"] = "INVALID_PROPOSAL",
            };
        _proposals.Add(proposal);
        return new JsonObject
        {
            ["ok"] = true,
            ["proposal_id"] = $"prop-{_proposals.Count}",
        };
    }

    /// <summary>Drain queued proposals — the autotrading tick consumes
    /// them into the same risk+mode gated path.</summary>
    public IReadOnlyList<TradeProposal> DrainProposals()
    {
        var drained = _proposals.ToList();
        _proposals.Clear();
        return drained;
    }
}
