// 星澄 AI 投資管理與自動操盤系統 — broker adapters (C#).
//
// Independent adapter architecture. Every adapter starts unverified:
// until the official trading API is verified, PlaceOrder fails closed.
// 國泰綜合證券 (TW) / 富邦證券複委託 (US) / extensible fund platforms.

namespace InvestmentMobile.Oms;

public abstract class BrokerAdapterBase : IBrokerAdapter
{
    public abstract string BrokerId { get; }
    public abstract string Market { get; }
    public abstract string Label { get; }

    /// <summary>Set only by the governed verification pipeline after the
    /// official broker API has been validated — never self-certified.</summary>
    public bool ApiVerified { get; internal set; }

    public virtual BrokerOrderResult PlaceOrder(ManagedOrder order)
    {
        if (!ApiVerified)
            return new BrokerOrderResult(
                false, "BROKER_API_UNVERIFIED", null);
        return PlaceVerified(order);
    }

    protected virtual BrokerOrderResult PlaceVerified(ManagedOrder order)
        => new(false, "BROKER_NOT_CONNECTED", null);
}

public sealed class CathaySecuritiesAdapter : BrokerAdapterBase
{
    public override string BrokerId => "cathay-tw";
    public override string Market => "tw";
    public override string Label => "國泰綜合證券";
}

public sealed class FubonSubBrokerageAdapter : BrokerAdapterBase
{
    public override string BrokerId => "fubon-us-sub";
    public override string Market => "us";
    public override string Label => "富邦證券複委託";
}

public sealed class FundPlatformAdapter : BrokerAdapterBase
{
    public FundPlatformAdapter(string platformId) => PlatformId = platformId;
    public string PlatformId { get; }
    public override string BrokerId => $"fund-platform-{PlatformId}";
    public override string Market => "fund";
    public override string Label => "基金平台（可擴充）";
}
