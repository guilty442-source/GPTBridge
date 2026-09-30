// 星澄 AI 投資管理與自動操盤系統 — signal book + strategy evaluation
// (C# production port of trading/strategy_engine.py).
//
// The strategy engine is the ONLY component allowed to convert signals
// into proposals. Signal intake is append-only JSONL
// (runtime/state/signals.jsonl) — the same shape the retired Python
// wrote, so existing ledgers stay readable.

using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using InvestmentMobile.Oms;

namespace InvestmentMobile.Service;

/// <summary>Tolerant numeric reads — governed payloads may carry int,
/// long or double values (JsonValue.GetValue&lt;double&gt; throws on a
/// typed-int node). Zero/negative still means "absent".</summary>
internal static class JsonNums
{
    public static double Num(JsonNode? node) => node switch
    {
        null => 0.0,
        JsonValue v when v.TryGetValue<double>(out var d) => d,
        JsonValue v when v.TryGetValue<float>(out var f) => f,
        JsonValue v when v.TryGetValue<int>(out var i) => i,
        JsonValue v when v.TryGetValue<long>(out var l) => l,
        JsonValue v when v.TryGetValue<decimal>(out var m) =>
            (double)m,
        _ => 0.0,
    };

    public static double? NumOrNull(JsonNode? node) =>
        node is null ? null : Num(node);
}

/// <summary>Recorded signal — parity with contracts.TradingSignal.</summary>
public sealed class TradingSignal
{
    public string InstrumentId { get; init; } = "";
    public string Market { get; init; } = "";
    public string Side { get; init; } = "buy";
    public double Confidence { get; init; }
    public double? Price { get; init; }
    public double Quantity { get; init; }
    public string Rationale { get; init; } = "";
    public string Source { get; init; } = "xingcheng";
    public string SignalId { get; init; } =
        $"sig-{Guid.NewGuid():N}"[..16];

    public JsonObject ToJson() => new()
    {
        ["instrument_id"] = InstrumentId,
        ["market"] = Market,
        ["side"] = Side,
        ["confidence"] = Confidence,
        ["price"] = Price is null ? null : JsonValue.Create(Price),
        ["quantity"] = Quantity,
        ["rationale"] = Rationale,
        ["source"] = Source,
        ["signal_id"] = SignalId,
    };

    public static TradingSignal FromJson(JsonObject row) => new()
    {
        InstrumentId = row["instrument_id"]?.GetValue<string>() ?? "",
        Market = row["market"]?.GetValue<string>() ?? "",
        Side = row["side"]?.GetValue<string>() ?? "buy",
        Confidence = JsonNums.Num(row["confidence"]),
        Price = JsonNums.NumOrNull(row["price"]),
        Quantity = JsonNums.Num(row["quantity"]),
        Rationale = row["rationale"]?.GetValue<string>() ?? "",
        Source = row["source"]?.GetValue<string>() ?? "xingcheng",
        SignalId = row["signal_id"]?.GetValue<string>()
            ?? $"sig-{Guid.NewGuid():N}"[..16],
    };
}

/// <summary>Managed evaluation seam — the strategy_evaluate_signal
/// contract (confidence >= min_confidence && quantity > 0, capped by
/// max_quantity). The native strategy_engine.dll is preferred; the
/// managed rule is the same contract when the ABI is absent — identical
/// fallback semantics to the retired façade.</summary>
public interface IStrategyEvaluator
{
    /// <summary>Returns (quantity, notional) when the signal qualifies,
    /// else null.</summary>
    (double Quantity, double Notional)? Emit(
        double confidence, double quantity, double price,
        double minConfidence, double maxQuantity);
}

/// <summary>Managed rule — identical to the C++ flat ABI contract.</summary>
public sealed class ManagedStrategyEvaluator : IStrategyEvaluator
{
    public (double, double)? Emit(
        double confidence, double quantity, double price,
        double minConfidence, double maxQuantity)
    {
        if (confidence < minConfidence || quantity <= 0.0)
            return null;
        var capped = maxQuantity > 0.0 && quantity > maxQuantity
            ? maxQuantity : quantity;
        return (capped, capped * price);
    }
}

/// <summary>Binding to native/strategy/strategy_engine.dll via
/// explicit export lookup — fails over to the managed rule when the
/// dll or the entry point is absent (identical fallback semantics to
/// the retired ctypes façade).</summary>
public sealed class NativeStrategyEvaluator : IStrategyEvaluator
{
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int EvaluateSignalNative(
        double confidence, double quantity, double price,
        double minConfidence, double maxQuantity,
        out double outQuantity, out double outNotional);

    private readonly IStrategyEvaluator _managed =
        new ManagedStrategyEvaluator();
    private readonly EvaluateSignalNative? _native;

    public NativeStrategyEvaluator(string? dllPath = null)
    {
        if (dllPath is null || !File.Exists(dllPath))
            return;
        try
        {
            var lib = NativeLibrary.Load(dllPath);
            if (NativeLibrary.TryGetExport(
                    lib, "strategy_evaluate_signal", out var ptr))
                _native = Marshal
                    .GetDelegateForFunctionPointer<EvaluateSignalNative>(
                        ptr);
        }
        catch { /* dll/load failure — managed rule takes over */ }
    }

    public string Backend => _native is not null ? "native" : "managed";

    public (double, double)? Emit(
        double confidence, double quantity, double price,
        double minConfidence, double maxQuantity)
    {
        if (_native is not null)
        {
            if (_native(confidence, quantity, price,
                    minConfidence, maxQuantity,
                    out var q, out var n) == 1)
                return (q, n);
            return null;
        }
        return _managed.Emit(
            confidence, quantity, price, minConfidence, maxQuantity);
    }
}

/// <summary>Append-only signal book (signals.jsonl) + the
/// ``signal-follow`` evaluation that converts qualifying signals into
/// TradeProposals — read-only, identical to the retired engine.</summary>
public sealed class SignalBook
{
    private readonly string _path;
    private readonly IStrategyEvaluator _evaluator;
    private readonly object _lock = new();

    /// <summary>signal-follow minimum confidence (retired default 0.5).</summary>
    public double MinConfidence { get; set; } = 0.5;

    public SignalBook(string stateDir, IStrategyEvaluator? evaluator = null)
    {
        _path = Path.Combine(stateDir, "signals.jsonl");
        _evaluator = evaluator ?? new NativeStrategyEvaluator(
            FindNativeDll());
    }

    private static string? FindNativeDll() => FindNativeDll(
        Path.Combine("native", "strategy", "strategy_engine.dll"));

    /// <summary>Walk up from the base directory to the tool root that
    /// holds ``native/`` — robust for both test bins and the deployed
    /// dist/ layout.</summary>
    public static string? FindNativeDll(string relative)
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, relative);
            if (File.Exists(candidate))
                return candidate;
            if (File.Exists(Path.Combine(
                    dir.FullName, "manifest.json")))
                break;
            dir = dir.Parent;
        }
        return null;
    }

    public string Backend => _evaluator is NativeStrategyEvaluator n
        ? n.Backend : "managed";

    /// <summary>Record a signal — instrument_id required.</summary>
    public JsonObject Record(TradingSignal signal)
    {
        if (signal.InstrumentId.Length == 0)
            return new JsonObject
            {
                ["ok"] = false, ["error_code"] = "INSTRUMENT_REQUIRED",
            };
        var line = signal.ToJson().ToJsonString(
            new JsonSerializerOptions { WriteIndented = false });
        lock (_lock)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            File.AppendAllText(_path, line + "\n");
        }
        return new JsonObject
        {
            ["ok"] = true, ["signal_id"] = signal.SignalId,
            ["recorded"] = "signal",
        };
    }

    /// <summary>All recorded signals (bounded tail read).</summary>
    public IReadOnlyList<TradingSignal> Signals(int limit = 100)
    {
        if (!File.Exists(_path)) return Array.Empty<TradingSignal>();
        var rows = new List<TradingSignal>();
        foreach (var line in File.ReadLines(_path))
        {
            if (line.Length == 0) continue;
            try
            {
                if (JsonNode.Parse(line) is JsonObject row)
                    rows.Add(TradingSignal.FromJson(row));
            }
            catch (JsonException) { /* skip malformed line */ }
        }
        return rows.Skip(Math.Max(0, rows.Count - limit)).ToList();
    }

    /// <summary>signal-follow evaluation — converts qualifying signals
    /// into TradeProposals. Read-only: never mutates the book.</summary>
    public IReadOnlyList<TradeProposal> Evaluate(string? signalId = null)
    {
        var proposals = new List<TradeProposal>();
        foreach (var row in Signals(limit: 10000))
        {
            if (signalId is not null && row.SignalId != signalId)
                continue;
            var emitted = _evaluator.Emit(
                row.Confidence, row.Quantity, row.Price ?? 0.0,
                MinConfidence, 0.0);
            if (emitted is null) continue;
            proposals.Add(new TradeProposal(
                row.InstrumentId, row.Market, row.Side,
                emitted.Value.Quantity, row.Price,
                "signal-follow", row.SignalId));
        }
        return proposals;
    }
}
