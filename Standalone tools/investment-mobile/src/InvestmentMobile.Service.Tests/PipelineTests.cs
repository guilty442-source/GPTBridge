// 星澄 AI 投資管理與自動操盤系統 — signal/strategy/risk/pipeline tests.
//
// Locks the AI boundary: signals and proposals enter through the
// intake only; every proposal still crosses the OMS risk + mode gates;
// no path reaches a broker without governed ApiVerified.

using System.Text.Json.Nodes;
using InvestmentMobile.Oms;
using InvestmentMobile.Service;
using Xunit;

namespace InvestmentMobile.Service.Tests;

public sealed class PipelineTests
{
    private static string TempDir()
    {
        var dir = Path.Combine(
            Path.GetTempPath(), $"imsvc-{Guid.NewGuid():N}");
        Directory.CreateDirectory(dir);
        return dir;
    }

    private static readonly RiskLimits PermissiveLimits = new()
    {
        MaxOrderNotional = 1_000_000,
        MaxPositionNotional = 10_000_000,
        MaxDailyLoss = 1_000_000,
        MaxOrdersPerDay = 100,
        MaxSinglePositionWeight = 1.0,
        RequirePrice = 1,
        AllowedMarketMask = 0b111,
        MaxOpenOrders = 100,
        MinCashBuffer = -1,   // cash gate disabled for unit paths
    };

    private sealed class StaticRisk(bool approved, string reason = "")
        : IRiskGate
    {
        public RiskDecision Evaluate(TradeProposal p) =>
            approved
                ? new RiskDecision(true, Array.Empty<string>())
                : new RiskDecision(false, new[] { reason });
    }

    private static TradeProposal Proposal(
        double qty = 10, double? price = 100, string market = "tw") =>
        new("2330", market, "buy", qty, price,
            "signal-follow", "sig-test");

    // ---------- SignalBook ----------

    [Fact]
    public void Record_rejects_missing_instrument()
    {
        var book = new SignalBook(
            TempDir(), new ManagedStrategyEvaluator());
        var res = book.Record(new TradingSignal());
        Assert.False(res["ok"]!.GetValue<bool>());
        Assert.Equal("INSTRUMENT_REQUIRED",
            res["error_code"]!.GetValue<string>());
        Assert.Empty(book.Signals());
    }

    [Fact]
    public void Record_appends_jsonl_and_reads_back()
    {
        var dir = TempDir();
        var book = new SignalBook(dir, new ManagedStrategyEvaluator());
        var res = book.Record(new TradingSignal
        {
            InstrumentId = "2330", Market = "tw", Confidence = 0.9,
            Quantity = 10, Price = 100,
        });
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.Equal("signal", res["recorded"]!.GetValue<string>());
        var rows = book.Signals();
        Assert.Single(rows);
        Assert.Equal("2330", rows[0].InstrumentId);
        Assert.True(File.Exists(
            Path.Combine(dir, "signals.jsonl")));
    }

    [Fact]
    public void Evaluate_emits_proposals_only_for_qualifying_signals()
    {
        var book = new SignalBook(
            TempDir(), new ManagedStrategyEvaluator());
        book.Record(new TradingSignal
        {
            InstrumentId = "2330", Market = "tw", Confidence = 0.9,
            Quantity = 10, Price = 100,
        });
        book.Record(new TradingSignal
        {
            InstrumentId = "2317", Market = "tw", Confidence = 0.1,
            Quantity = 5, Price = 50,
        });
        var proposals = book.Evaluate();
        Assert.Single(proposals);
        Assert.Equal("2330", proposals[0].InstrumentId);
        Assert.Equal("signal-follow", proposals[0].StrategyId);
    }

    [Fact]
    public void Evaluate_zero_quantity_not_emitted()
    {
        var book = new SignalBook(
            TempDir(), new ManagedStrategyEvaluator());
        book.Record(new TradingSignal
        {
            InstrumentId = "2330", Market = "tw", Confidence = 0.9,
            Quantity = 0, Price = 100,
        });
        Assert.Empty(book.Evaluate());
    }

    [Fact]
    public void Managed_evaluator_caps_quantity()
    {
        var ev = new ManagedStrategyEvaluator();
        var emitted = ev.Emit(0.9, 100, 10, 0.5, 30);
        Assert.Equal((30.0, 300.0), emitted);
        Assert.Null(ev.Emit(0.4, 100, 10, 0.5, 30));
    }

    [Fact]
    public void Native_strategy_engine_abi_evaluates_identically()
    {
        var dll = SignalBook.FindNativeDll(
            Path.Combine("native", "strategy",
                "strategy_engine.dll"));
        Assert.NotNull(dll);   // built by native/build.cmd
        var ev = new NativeStrategyEvaluator(dll);
        Assert.Equal("native", ev.Backend);
        Assert.Equal((30.0, 300.0), ev.Emit(0.9, 100, 10, 0.5, 30));
        Assert.Null(ev.Emit(0.4, 100, 10, 0.5, 30));
        Assert.Null(ev.Emit(0.9, 0, 10, 0.5, 30));
    }

    // ---------- NativeRiskGate ----------

    [Fact]
    public void Risk_gate_without_engine_fails_closed()
    {
        var gate = new NativeRiskGate(null, PermissiveLimits);
        Assert.Equal("closed", gate.Backend);
        var decision = gate.Evaluate(Proposal());
        Assert.False(decision.Approved);
        Assert.Contains("RISK_ENGINE_UNAVAILABLE", decision.Reasons);
    }

    [Fact]
    public void Native_risk_core_abi_evaluates_orders()
    {
        var dll = NativeRiskGate.FindRiskDll();
        Assert.NotNull(dll);   // built by native/build.cmd
        var gate = new NativeRiskGate(dll, PermissiveLimits);
        Assert.Equal("native", gate.Backend);

        var ok = gate.EvaluateWith(Proposal(), new RiskOrderInput
        {
            CashAfter = 5000,
        });
        Assert.True(ok.Approved);

        var rejected = gate.EvaluateWith(
            Proposal(qty: 0), new RiskOrderInput());
        Assert.False(rejected.Approved);
        Assert.Contains("quantity_invalid", rejected.Reasons);
    }

    [Fact]
    public void RiskCodes_maps_names_and_market_bits()
    {
        Assert.Equal("approved", RiskCodes.ReasonName(0));
        Assert.Equal("order_notional_exceeds_limit",
            RiskCodes.ReasonName(4));
        Assert.Equal(1u, RiskCodes.MarketBit("tw"));
        Assert.Equal(0u, RiskCodes.MarketBit("mars"));
    }

    // ---------- AiSignalIntake ----------

    [Fact]
    public void Intake_submit_signal_records()
    {
        var book = new SignalBook(
            TempDir(), new ManagedStrategyEvaluator());
        var intake = new AiSignalIntake(book);
        var res = intake.SubmitSignal(new JsonObject
        {
            ["instrument_id"] = "2330", ["market"] = "tw",
            ["confidence"] = 0.8, ["quantity"] = 5, ["price"] = 100,
        });
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.Single(book.Signals());
    }

    [Fact]
    public void Intake_submit_proposal_validates_and_queues()
    {
        var intake = new AiSignalIntake(
            new SignalBook(TempDir(), new ManagedStrategyEvaluator()));
        var bad = intake.SubmitProposal(
            new JsonObject { ["quantity"] = 10 });
        Assert.False(bad["ok"]!.GetValue<bool>());
        Assert.Equal("INVALID_PROPOSAL",
            bad["error_code"]!.GetValue<string>());

        var good = intake.SubmitProposal(new JsonObject
        {
            ["instrument_id"] = "2330", ["market"] = "tw",
            ["side"] = "buy", ["quantity"] = 10, ["price"] = 100,
        });
        Assert.True(good["ok"]!.GetValue<bool>());
        Assert.Single(intake.DrainProposals());
        Assert.Empty(intake.DrainProposals());
    }

    // ---------- AutoTradingEngine ----------

    [Fact]
    public void Engine_live_mode_fails_closed()
    {
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Live };
        var engine = new AutoTradingEngine(
            oms, new SignalBook(TempDir(), new ManagedStrategyEvaluator()),
            new AiSignalIntake(
                new SignalBook(TempDir(), new ManagedStrategyEvaluator())));
        Assert.Equal(AutoTradingState.Failed, engine.State);
        Assert.Equal("LIVE_PHASE_LOCKED", engine.Configure());
        Assert.Equal(AutoTradingState.Failed, engine.State);
    }

    [Fact]
    public void Halt_requires_authorized_resume()
    {
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Paper };
        var engine = new AutoTradingEngine(
            oms, new SignalBook(TempDir(), new ManagedStrategyEvaluator()),
            new AiSignalIntake(
                new SignalBook(TempDir(), new ManagedStrategyEvaluator())));
        engine.Halt();
        Assert.Equal(AutoTradingState.RiskHalted, engine.State);
        Assert.False(engine.Resume(authorized: false));   // AI cannot lift a halt
        Assert.Equal(AutoTradingState.RiskHalted, engine.State);
    }

    [Fact]
    public void Authorized_human_can_resume()
    {
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Paper };
        var engine = new AutoTradingEngine(
            oms, new SignalBook(TempDir(), new ManagedStrategyEvaluator()),
            new AiSignalIntake(
                new SignalBook(TempDir(), new ManagedStrategyEvaluator())));
        engine.Halt();
        Assert.True(engine.Resume(authorized: true));
    }

    [Fact]
    public async Task Tick_paper_fills_through_normal_gates()
    {
        var dir = TempDir();
        var book = new SignalBook(dir, new ManagedStrategyEvaluator());
        book.Record(new TradingSignal
        {
            InstrumentId = "2330", Market = "tw", Confidence = 0.9,
            Quantity = 10, Price = 100,
        });
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Paper };
        var intake = new AiSignalIntake(
            new SignalBook(TempDir(), new ManagedStrategyEvaluator()));
        var engine = new AutoTradingEngine(oms, book, intake);
        var trace = await engine.RunOnceAsync();
        Assert.Single(trace);
        Assert.Equal(OrderStatus.Filled.ToString(), trace[0].Outcome);
        Assert.Single(oms.Executions);
        Assert.True(oms.Executions[0].Simulated);
        // processed once — the next tick does not resubmit
        Assert.Empty(await engine.RunOnceAsync());
    }

    [Fact]
    public async Task Tick_shadow_records_without_submission()
    {
        var book = new SignalBook(
            TempDir(), new ManagedStrategyEvaluator());
        book.Record(new TradingSignal
        {
            InstrumentId = "2330", Market = "tw", Confidence = 0.9,
            Quantity = 10, Price = 100,
        });
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Shadow };
        var engine = new AutoTradingEngine(
            oms, book,
            new AiSignalIntake(
                new SignalBook(TempDir(),
                    new ManagedStrategyEvaluator())));
        var trace = await engine.RunOnceAsync();
        Assert.Single(trace);
        Assert.Equal(
            OrderStatus.ModeBlocked.ToString(), trace[0].Outcome);
        Assert.Empty(oms.Executions);
    }

    [Fact]
    public async Task Ai_proposal_still_crosses_risk_gate()
    {
        var oms = new OrderManagementSystem(
            new StaticRisk(false, "position_weight_exceeds_limit"),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Paper };
        var intake = new AiSignalIntake(
            new SignalBook(TempDir(), new ManagedStrategyEvaluator()));
        intake.SubmitProposal(new JsonObject
        {
            ["instrument_id"] = "2330", ["market"] = "tw",
            ["side"] = "buy", ["quantity"] = 10, ["price"] = 100,
        });
        var engine = new AutoTradingEngine(
            oms,
            new SignalBook(TempDir(), new ManagedStrategyEvaluator()),
            intake);
        var trace = await engine.RunOnceAsync();
        Assert.Single(trace);
        Assert.Equal(
            OrderStatus.RiskRejected.ToString(), trace[0].Outcome);
        Assert.Empty(oms.Executions);
    }

    [Fact]
    public async Task Ai_assisted_missing_channel_blocks_dependent()
    {
        var book = new SignalBook(
            TempDir(), new ManagedStrategyEvaluator());
        book.Record(new TradingSignal
        {
            InstrumentId = "2330", Market = "tw", Confidence = 0.9,
            Quantity = 10, Price = 100,
        });
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Paper };
        var engine = new AutoTradingEngine(
            oms, book,
            new AiSignalIntake(
                new SignalBook(TempDir(),
                    new ManagedStrategyEvaluator())),
            advisoryChannel:
                new XingchengChannelClient(null))
        { AiMode = AiIntegrationMode.AiAssisted };
        var trace = await engine.RunOnceAsync();
        Assert.Equal(
            AutoTradingState.ModelBlocked, engine.State);
        // dependent trades blocked — advisory evidence unavailable
        Assert.Single(trace);
        Assert.Equal("ai_analysis", trace[0].Stage);
        Assert.Equal("evidence-unavailable", trace[0].Outcome);
        Assert.Empty(oms.Orders);
    }

    [Fact]
    public async Task Ai_assisted_with_channel_consults_then_orders()
    {
        var stub = new ContractStub();
        var book = new SignalBook(
            TempDir(), new ManagedStrategyEvaluator());
        book.Record(new TradingSignal
        {
            InstrumentId = "2330", Market = "tw", Confidence = 0.9,
            Quantity = 10, Price = 100,
        });
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Paper };
        var engine = new AutoTradingEngine(
            oms, book,
            new AiSignalIntake(
                new SignalBook(TempDir(),
                    new ManagedStrategyEvaluator())),
            advisoryChannel: new XingchengChannelClient(stub))
        { AiMode = AiIntegrationMode.AiAssisted };
        var trace = await engine.RunOnceAsync();
        Assert.Single(trace);
        Assert.Equal(OrderStatus.Filled.ToString(), trace[0].Outcome);
        Assert.Equal("ai_analysis",
            stub.LastPayload!["operation"]!.GetValue<string>());
    }

    private sealed class ContractStub : IXingchengChannel
    {
        public JsonObject? LastPayload;
        public JsonObject Request(
            string targetToolId, string command, JsonObject payload)
        {
            LastPayload = payload;
            return new JsonObject { ["ok"] = true };
        }
    }

    // ---------- OMS boundary (AI cannot reach a broker unverified) ----------

    private sealed class CountingAdapter : IBrokerAdapter
    {
        public int Calls;
        public string BrokerId => "TEST";
        public string Market => "tw";
        public bool ApiVerified => false;
        public BrokerOrderResult PlaceOrder(OrderRequest order)
        {
            Calls++;
            return new BrokerOrderResult(
                false, "BROKER_API_UNVERIFIED", null);
        }
    }

    [Fact]
    public void Live_unverified_broker_denied()
    {
        var adapter = new CountingAdapter();
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>
            { ["tw"] = adapter })
        { Mode = TradingMode.Live };
        var order = oms.Submit(Proposal());
        Assert.Equal(OrderStatus.AdapterDenied, order.Status);
    }

    [Fact]
    public void Analysis_mode_blocks_after_risk_decision()
    {
        var oms = new OrderManagementSystem(
            new StaticRisk(true),
            new Dictionary<string, IBrokerAdapter>())
        { Mode = TradingMode.Analysis };
        var order = oms.Submit(Proposal());
        Assert.Equal(OrderStatus.ModeBlocked, order.Status);
        Assert.Contains("ANALYSIS", order.Rejection);
    }
}
