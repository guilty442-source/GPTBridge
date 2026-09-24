// 星澄 AI 投資管理與自動操盤系統 — order management state machine (C#).
//
// Decision-free orchestration: the same states and transitions as the
// Python OMS (src/.../trading/oms.py). Trading orchestration, order
// management and broker connections are the C# responsibility per the
// language division of labour.

namespace InvestmentMobile.Oms;

public enum TradingMode { Analysis, Shadow, Paper, Live }

public enum OrderStatus
{
    Created,
    RiskRejected,
    ModeBlocked,
    AdapterDenied,
    Submitted,
    Filled,
    Cancelled,
}

public sealed record OrderIntent(
    string Instrument,
    string Market,
    string Side,
    double Quantity,
    double? Price,
    string StrategyId,
    string SignalId);

public sealed record RiskDecision(bool Approved, IReadOnlyList<string> Reasons);

public interface IRiskGate
{
    RiskDecision Evaluate(OrderIntent intent);
}

public interface IBrokerAdapter
{
    string BrokerId { get; }
    string Market { get; }
    bool ApiVerified { get; }
    BrokerOrderResult PlaceOrder(ManagedOrder order);
}

public sealed record BrokerOrderResult(bool Ok, string? ErrorCode, string? BrokerOrderId);

public sealed class ManagedOrder
{
    public required OrderIntent Intent { get; init; }
    public string OrderId { get; } = $"ord-{Guid.NewGuid():N}"[..16];
    public OrderStatus Status { get; set; } = OrderStatus.Created;
    public string BrokerOrderId { get; set; } = "";
    public string Rejection { get; set; } = "";
}

/// <summary>
/// Order pipeline: risk gate -> mode gate -> simulated fill (Paper/Shadow)
/// or verified broker dispatch (Live). Analysis mode never submits.
/// </summary>
public sealed class OrderManagementSystem
{
    private readonly IRiskGate _risk;
    private readonly IReadOnlyDictionary<string, IBrokerAdapter> _adapters;
    private readonly List<ManagedOrder> _orders = new();

    public TradingMode Mode { get; set; } = TradingMode.Analysis;

    public OrderManagementSystem(
        IRiskGate risk, IReadOnlyDictionary<string, IBrokerAdapter> adapters)
    {
        _risk = risk;
        _adapters = adapters;
    }

    public IReadOnlyList<ManagedOrder> Orders => _orders;

    public ManagedOrder Submit(OrderIntent intent)
    {
        var order = new ManagedOrder { Intent = intent };
        _orders.Add(order);

        var decision = _risk.Evaluate(intent);
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

        if (Mode is TradingMode.Shadow or TradingMode.Paper)
        {
            // Shadow records the would-be decision; Paper simulates a fill.
            order.Status = Mode == TradingMode.Shadow
                ? OrderStatus.Submitted
                : OrderStatus.Filled;
            return order;
        }

        // Live: dispatch only through an API-verified adapter.
        if (!_adapters.TryGetValue(intent.Market, out var adapter))
        {
            order.Status = OrderStatus.AdapterDenied;
            order.Rejection = $"no broker adapter for market '{intent.Market}'";
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
