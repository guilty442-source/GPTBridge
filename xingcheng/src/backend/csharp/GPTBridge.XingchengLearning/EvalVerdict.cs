// EvalVerdict.cs — the C# eval-verdict authority.
//
// LANGUAGE-ARCHITECTURE MIGRATION (2026-10-03, human-governor
// directive — F# retirement): eval-verdict ownership moved from F#
// (xc-eval, star-fsharp-eval-verdict/v1) to this C# governance lane
// under the B81 LANGUAGE-OWNERSHIP lanes + B139 rebind
// (codex-amendment-request-xingcheng-language-ownership-20261001).
// Parity evidence for the flip: every recorded production verdict
// pair agreed — 0 disagreements across the certification
// evaluations (see Evaluation.VerdictParity /
// star-eval-verdict-parity-report/v1). There is exactly one
// production verdict authority — the retired F# lane may only run
// through the parity-verification surface and never decides again.
//
// Semantics mirror Program.fs exactly (same defaults, same eps, same
// fail-closed edges):
//   star-native-eval-suite/v1: perplexity regression %, generation
//   requirement (default OFF, matching F# — note the older C#
//   SelfLearning.CompareMetrics defaults it ON; that drift is intentional
//   to surface here, not to fix silently), absolute + baseline-ratio
//   tokens/sec gates.
//   star-capability-suite/v1: per-category pass_rate must not drop below
//   baseline (eps 1e-9); a run without a baseline cannot prove
//   non-regression and fails closed.

namespace GPTBridge.XingchengLearning;

internal static class EvalVerdict
{
    public const string Format = "star-csharp-eval-verdict/v1";

    private const double Eps = 1e-9;

    private static double? TryNum(object? value) => value switch
    {
        double d when double.IsFinite(d) => d,
        float f when float.IsFinite(f) => f,
        int i => i,
        long l => l,
        decimal m => (double)m,
        _ => null,
    };

    private static double NumOr(
        IReadOnlyDictionary<string, object?> map, string key, double dflt)
        => map.TryGetValue(key, out object? v) && TryNum(v) is double n
            ? n : dflt;

    private static bool BoolOr(
        IReadOnlyDictionary<string, object?> map, string key, bool dflt)
        => map.TryGetValue(key, out object? v) && v is bool b ? b : dflt;

    private static string StrOr(
        IReadOnlyDictionary<string, object?> map, string key, string dflt)
        => map.TryGetValue(key, out object? v) && v is string s ? s : dflt;

    private static Dictionary<string, object?> Child(
        IReadOnlyDictionary<string, object?> map, string key)
        => map.TryGetValue(key, out object? v) &&
           v is Dictionary<string, object?> d
            ? d : new Dictionary<string, object?>();

    private static (Dictionary<string, object?> comparison, bool passed)
        EvalNative(
            IReadOnlyDictionary<string, object?> gates,
            IReadOnlyDictionary<string, object?> cand,
            IReadOnlyDictionary<string, object?> baseline)
    {
        double maxRegr = NumOr(gates, "max_perplexity_regression_pct", 5.0);
        bool requireGen = BoolOr(gates, "require_generation", false);
        double minTps = NumOr(gates, "min_tokens_per_second", 0.0);
        double tpsRatio = NumOr(gates, "min_tps_baseline_ratio", 0.0);

        double? basePpl = baseline.TryGetValue("perplexity", out object? bv)
            ? TryNum(bv) : null;
        double? candPpl = cand.TryGetValue("perplexity", out object? cv)
            ? TryNum(cv) : null;
        bool deltaOk = false;
        double pplDelta = 0.0;
        bool pplOk = true;
        if (basePpl is double b && candPpl is double &&
            b > 0.0)
        {
            pplDelta = (candPpl.Value - b) / b * 100.0;
            pplOk = pplDelta <= maxRegr;
            deltaOk = true;
        }

        bool genOk = !requireGen ||
            (cand.TryGetValue("generation_ok", out object? g) &&
             g is bool ok && ok);

        double candTps = NumOr(cand, "tokens_per_second", 0.0);
        double? baseTps = baseline.TryGetValue("tokens_per_second",
                                               out object? bt)
            ? TryNum(bt) : null;
        bool tpsOk = minTps <= 0.0 || candTps >= minTps;
        bool tpsRatioOk = true;
        if (tpsRatio > 0.0)
            tpsRatioOk = baseTps is double tb && tb > 0.0 &&
                         candTps >= tb * tpsRatio;
        tpsOk = tpsOk && tpsRatioOk;

        bool passed = pplOk && genOk && tpsOk;
        var cmp = new Dictionary<string, object?>
        {
            ["perplexity_delta_pct"] = deltaOk ? (object?)pplDelta : null,
            ["perplexity_ok"] = pplOk,
            ["generation_ok"] = genOk,
            ["tokens_per_second_ok"] = tpsOk,
            ["tokens_per_second_ratio_ok"] = tpsRatioOk,
            ["baseline_tokens_per_second"] = baseTps is double tbv
                ? (object?)tbv : null,
            ["verdict_owner"] = "csharp",
        };
        return (cmp, passed);
    }

    private static (Dictionary<string, object?> comparison, bool passed)
        EvalCapability(
            IReadOnlyDictionary<string, object?> cand,
            IReadOnlyDictionary<string, object?> baseline)
    {
        var regs = new List<object?>();
        var bcats = Child(baseline, "categories");
        var ccats = Child(cand, "categories");
        foreach (var kv in bcats)
        {
            double? brate = kv.Value is Dictionary<string, object?> binfo &&
                            binfo.TryGetValue("pass_rate", out object? br)
                ? TryNum(br) : null;
            double? crate = ccats.TryGetValue(kv.Key, out object? ci) &&
                            ci is Dictionary<string, object?> cinfo &&
                            cinfo.TryGetValue("pass_rate", out object? cr)
                ? TryNum(cr) : null;
            if (brate is double bb && crate is double cb &&
                cb < bb - Eps)
                regs.Add(new Dictionary<string, object?>
                {
                    ["category"] = kv.Key,
                    ["baseline"] = bb,
                    ["candidate"] = cb,
                });
        }
        bool noRegression = regs.Count == 0;
        var cmp = new Dictionary<string, object?>
        {
            ["passed"] = noRegression,
            ["regressions"] = regs,
            ["baseline_suite"] = StrOr(baseline, "suite_id", ""),
            ["candidate_suite"] = StrOr(cand, "suite_id", ""),
            ["verdict_owner"] = "csharp",
        };
        return (cmp, noRegression && baseline.Count > 0);
    }

    /// <summary>Re-derive the verdict in C#. Throws ExecutorError on
    /// unknown suite format (mirrors EVAL_SUITE_FORMAT_UNKNOWN).</summary>
    public static Dictionary<string, object?> Evaluate(
        string format,
        IReadOnlyDictionary<string, object?> gates,
        IReadOnlyDictionary<string, object?> candidate,
        IReadOnlyDictionary<string, object?> baseline)
    {
        (Dictionary<string, object?> cmp, bool passed) =
            format == Evaluation.CapabilityFormat
                ? EvalCapability(candidate, baseline)
                : format == Evaluation.NativeEvalFormat || format == ""
                    ? EvalNative(gates, candidate, baseline)
                    : throw new ExecutorError(
                        "EVAL_SUITE_FORMAT_UNKNOWN",
                        $"unknown suite format: {format}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["passed"] = passed,
            ["comparison"] = cmp,
        };
    }

    /// <summary>Parity record against the retired F# verdict —
    /// parity-verification surface only, never a gate path. Never
    /// throws: a port failure is recorded as evidence.</summary>
    public static Dictionary<string, object?> Parity(
        string format,
        IReadOnlyDictionary<string, object?> gates,
        IReadOnlyDictionary<string, object?> candidate,
        IReadOnlyDictionary<string, object?> baseline,
        bool fsharpPassed)
    {
        try
        {
            var verdict = Evaluate(format, gates, candidate, baseline);
            bool csharpPassed =
                TransformerTrainingRepository.Truthy(
                    verdict.GetValueOrDefault("passed"));
            return new Dictionary<string, object?>
            {
                ["agree"] = fsharpPassed == csharpPassed,
                ["fsharp_passed"] = fsharpPassed,
                ["csharp_passed"] = csharpPassed,
                ["csharp_comparison"] =
                    verdict.GetValueOrDefault("comparison"),
            };
        }
        catch (Exception exc)
        {
            return new Dictionary<string, object?>
            {
                ["agree"] = null,
                ["fsharp_passed"] = fsharpPassed,
                ["error"] = exc.Message.Length > 300
                    ? exc.Message[..300] : exc.Message,
            };
        }
    }
}
