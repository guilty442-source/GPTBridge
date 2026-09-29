// 星澄 AI 投資管理與自動操盤系統 — risk gate over the native C core
// (native/risk/risk_core.dll), C# production binding.
//
// Fail-closed contract preserved verbatim: any missing limit, zero
// quantity or unavailable engine rejects the order. The reason codes
// mirror risk_reason_name for audit trails.

using System.Runtime.InteropServices;
using InvestmentMobile.Oms;

namespace InvestmentMobile.Service;

/// <summary>Risk limits — mirrors the C RiskLimits struct.</summary>
[StructLayout(LayoutKind.Sequential)]
public struct RiskLimits
{
    public double MaxOrderNotional;
    public double MaxPositionNotional;
    public double MaxDailyLoss;
    public int MaxOrdersPerDay;
    public double MaxSinglePositionWeight;
    public int RequirePrice;
    /// <summary>bit0=tw bit1=us bit2=fund.</summary>
    public uint AllowedMarketMask;
    public int MaxOpenOrders;
    public double MinCashBuffer;
}

/// <summary>Order input — mirrors the C RiskOrderInput struct.</summary>
[StructLayout(LayoutKind.Sequential)]
public struct RiskOrderInput
{
    public uint MarketBit;
    public int Side;
    public double Quantity;
    public double Notional;
    public double ExistingPositionNotional;
    public double ExistingPositionValue;
    public double TotalPortfolioValue;
    public int DailyOrderCount;
    public double DailyRealizedPnl;
    public int OpenOrderCount;
    public double CashAfter;
}

public static class RiskCodes
{
    public static string ReasonName(int code) => code switch
    {
        0 => "approved",
        1 => "market_not_whitelisted",
        2 => "quantity_invalid",
        3 => "no_price",
        4 => "order_notional_exceeds_limit",
        5 => "daily_order_limit",
        6 => "daily_loss_limit_breached",
        7 => "position_notional_exceeds_limit",
        8 => "position_weight_exceeds_limit",
        9 => "too_many_open_orders",
        10 => "cash_below_minimum_buffer",
        _ => "unknown",
    };

    public static uint MarketBit(string market) => market switch
    {
        "tw" => 1u,
        "us" => 2u,
        "fund" => 4u,
        _ => 0u,
    };
}

/// <summary>IRiskGate over the native risk core. When the dll is
/// absent the gate still answers — every order denied with
/// RISK_ENGINE_UNAVAILABLE (fail-closed, never silently permissive).</summary>
public sealed class NativeRiskGate : IRiskGate
{
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int RiskEvaluateNative(
        in RiskLimits limits, in RiskOrderInput order);

    private readonly RiskEvaluateNative? _native;
    private RiskLimits _limits;

    public NativeRiskGate(string? dllPath, RiskLimits limits)
    {
        _limits = limits;
        if (dllPath is null || !File.Exists(dllPath))
            return;
        try
        {
            var lib = NativeLibrary.Load(dllPath);
            if (NativeLibrary.TryGetExport(
                    lib, "risk_evaluate_order", out var ptr))
                _native = Marshal
                    .GetDelegateForFunctionPointer<RiskEvaluateNative>(
                        ptr);
        }
        catch { /* engine unavailable — gate stays closed */ }
    }

    /// <summary>Locate ``native/risk/risk_core.dll`` by walking up to
    /// the tool root — the same anchor strategy as the signal book.</summary>
    public static string? FindRiskDll() =>
        SignalBook.FindNativeDll(
            Path.Combine("native", "risk", "risk_core.dll"));

    public static NativeRiskGate CreateDefault(RiskLimits limits) =>
        new(FindRiskDll(), limits);

    public RiskLimits Limits
    {
        get => _limits;
        set => _limits = value;   // governed runtime reload only
    }

    public string Backend => _native is not null ? "native" : "closed";

    /// <summary>Evaluation context supplied per order (position, daily
    /// counters, cash projection come from the portfolio side).</summary>
    public RiskDecision EvaluateWith(
        TradeProposal proposal, RiskOrderInput context)
    {
        if (_native is null)
            return new RiskDecision(
                false, new[] { "RISK_ENGINE_UNAVAILABLE" });
        context.MarketBit = RiskCodes.MarketBit(proposal.Market);
        context.Side = proposal.Side is "buy" or "subscribe" ? 1 : -1;
        context.Quantity = proposal.Quantity;
        context.Notional = proposal.EffectiveNotional;
        var code = _native(in _limits, in context);
        return code == 0
            ? new RiskDecision(true, Array.Empty<string>())
            : new RiskDecision(
                false, new[] { RiskCodes.ReasonName(code) });
    }

    /// <summary>IRiskGate surface — evaluates the proposal alone; the
    /// host supplies portfolio context through <see
    /// cref="EvaluateWith"/>.  A bare call uses zero context, which the
    /// C core treats conservatively (cash_after=0 etc.).</summary>
    public RiskDecision Evaluate(TradeProposal proposal) =>
        EvaluateWith(proposal, new RiskOrderInput());
}
