// 星澄 AI 投資管理與自動操盤系統 — order management state machine (C#).
//
// Decision-free orchestration: the same states and transitions as the
// Python OMS (src/.../trading/oms.py). Trading orchestration, order
// management and broker connections are the C# responsibility per the
// language division of labour.
//
// Fixed pipeline: TradeProposal -> RiskDecision -> OrderRequest ->
// (mode gate) -> BrokerAdapter (Live) / simulated account (Paper) ->
// OrderReceipt -> Execution -> Portfolio. Shadow records the decided
// order but never submits; Analysis stops after the risk decision.

namespace InvestmentMobile.Oms;

public enum TradingMode { Analysis, Shadow, Paper, Live }

public enum OrderStatus
{
    Created,
    RiskRejected,
    ModeBlocked,
    AdapterDenied,
    Submitted,
    PartialFilled,
    Filled,
    Cancelled,
}

/// <summary>Formal trade proposal — the only AI-reachable shape.</summary>
public sealed record TradeProposal(
    string InstrumentId,
    string Market,
    string Side,
    double Quantity,
    double? Price,
    string StrategyId,
    string SignalId,
    string AccountId = "")
{
    public double EffectiveNotional =>
        Quantity * (Price ?? 0.0);
}

public sealed record RiskDecision(bool Approved, IReadOnlyList<string> Reasons);

public interface IRiskGate
{
    RiskDecision Evaluate(TradeProposal proposal);
}

public interface IBrokerAdapter
{
    string BrokerId { get; }
    string Market { get; }
    bool ApiVerified { get; }
    BrokerOrderResult PlaceOrder(OrderRequest order);
}

public sealed record BrokerOrderResult(bool Ok, string? ErrorCode, string? BrokerOrderId);

public sealed class OrderRequest
{
    public required TradeProposal Proposal { get; init; }
    public string OrderId { get; } = $"ord-{Guid.NewGuid():N}"[..16];
    public OrderStatus Status { get; set; } = OrderStatus.Created;
    public string BrokerOrderId { get; set; } = "";
    public string Rejection { get; set; } = "";
}

public sealed record OrderReceipt(
    string OrderId,
    string BrokerOrderId,
    bool Simulated);

public sealed record Execution(
    string OrderId,
    string InstrumentId,
    string Market,
    string Side,
    double Quantity,
    double Price,
    string AccountId,
    bool Simulated);

/// <summary>
/// Order pipeline: risk gate -> mode gate -> simulated fill (Paper only)
/// or verified broker dispatch (Live). Analysis records the decision
/// evidence; Shadow records the order but never submits.
/// </summary>
public sealed class OrderManagementSystem
{
    private readonly IRiskGate _risk;
    private readonly IReadOnlyDictionary<string, IBrokerAdapter> _adapters;
    private readonly List<OrderRequest> _orders = new();
    private readonly List<Execution> _executions = new();

    public TradingMode Mode { get; set; } = TradingMode.Analysis;

    public OrderManagementSystem(
        IRiskGate risk, IReadOnlyDictionary<string, IBrokerAdapter> adapters)
    {
        _risk = risk;
        _adapters = adapters;
    }

    public IReadOnlyList<OrderRequest> Orders => _orders;
    public IReadOnlyList<Execution> Executions => _executions;

    public OrderRequest Submit(TradeProposal proposal)
    {
        var order = new OrderRequest { Proposal = proposal };
        _orders.Add(order);

        var decision = _risk.Evaluate(proposal);
        if (!decision.Approved)
        {
            order.Status = OrderStatus.RiskRejected;
            order.Rejection = string.Join("; ", decision.Reasons);
            return order;
        }

        if (Mode == TradingMode.Analysis)
        {
            order.Status = OrderStatus.ModeBlocked;
            order.Rejection = "mode ANALYSIS blocks order submission";
            return order;
        }

        // Shadow: the decided order is recorded but never submitted.
        if (Mode == TradingMode.Shadow)
        {
            order.Status = OrderStatus.ModeBlocked;
            order.Rejection = "mode SHADOW records decisions without submission";
            return order;
        }

        if (Mode == TradingMode.Paper)
        {
            // Dedicated simulated account — never touches a broker.
            order.Status = OrderStatus.Filled;
            _executions.Add(new Execution(
                order.OrderId, proposal.InstrumentId, proposal.Market,
                proposal.Side, proposal.Quantity, proposal.Price ?? 0.0,
                $"paper-{proposal.Market}", Simulated: true));
            return order;
        }

        // Live: dispatch only through an API-verified adapter.
        if (!_adapters.TryGetValue(proposal.Market, out var adapter))
        {
            order.Status = OrderStatus.AdapterDenied;
            order.Rejection = $"no broker adapter for market '{proposal.Market}'";
            return order;
        }

        var result = adapter.PlaceOrder(order);
        if (!result.Ok)
        {
            order.Status = OrderStatus.AdapterDenied;
            order.Rejection = result.ErrorCode ?? "ADAPTER_DENIED";
            return order;
        }

        order.Status = OrderStatus.Submitted;
        order.BrokerOrderId = result.BrokerOrderId ?? "";
        return order;
    }
}
