// PrecisionRuntime.cs — ONE precision system (§26 closing rule):
// NativePrecisionLab (§3), QuantizationPolicy / star-precision-map/v1
// (§3.1 / §11), PrecisionRoadmap (§26) and star-quant-cert/v1 (§28).
//
//   §3   EXPERIMENTAL_FP8 / EXPERIMENTAL_FP4 join REFERENCE_FP64 /
//        PRODUCTION_BF16. FP4 is an abstract "4-bit weight format" —
//        never MXFP4/NVFP4 as model semantics; the hardware backend
//        decides the actual encoding.
//   §3.1/§11  per-component precision — a whole-model single-precision
//        flag is forbidden (§11.1); a compatibility adapter maps the
//        legacy scalar into a map.
//   §26  Stages 0..4, FP64->BF16->FP8->FP4->hardware-specific; jumping
//        FP64 straight to FP4 production is forbidden.
//   §28  every promotion passes star-quant-cert/v1; any critical
//        regression blocks promotion (fail-closed).

namespace GPTBridge.XingchengLearning;

/// <summary>§3 lab profiles — production set lives in
/// RuntimeCapabilities.PrecisionProfiles; experimental profiles live
/// here so production resolution stays closed.</summary>
internal static class NativePrecisionLab
{
    public const string Format = "star-precision-lab/v1";

    public const string ExperimentalFp8 = "EXPERIMENTAL_FP8";
    public const string ExperimentalFp4 = "EXPERIMENTAL_FP4";

    /// <summary>All lab profiles — production names live in
    /// PrecisionProfiles (RuntimeCapabilities.cs); experimental
    /// profiles are defined here so production resolution stays
    /// closed.</summary>
    public static readonly string[] Profiles =
        { PrecisionProfiles.ReferenceFp64,
          PrecisionProfiles.ProductionBf16,
          PrecisionProfiles.CompactFp8,
          PrecisionProfiles.EdgeInt8,
          ExperimentalFp8, ExperimentalFp4 };

    /// <summary>Abstract precision vocab (§3.1) — FP4 is "4-bit weight
    /// format" only; backend encoding is a hardware decision.</summary>
    public static readonly string[] Precisions =
        { "FP64", "FP32", "BF16", "FP16", "FP8", "FP4", "INT8" };

    public static void ValidatePrecision(string p)
    {
        if (!Precisions.Contains(p))
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"unknown precision '{p}'");
    }
}

/// <summary>§3.1/§11 QuantizationPolicy — one precision per component;
/// star-precision-map/v1 document. No single model_precision flag may
/// drive all components (§11.1).</summary>
internal sealed class QuantizationPolicy
{
    public const string Format = "star-precision-map/v1";

    /// <summary>§3.1 closed component list.</summary>
    public static readonly string[] Components =
        { "embedding", "attention_qkv", "attention_out", "moe_expert",
          "shared_expert", "router", "lm_head", "kv_cache",
          "recurrent_state", "vision_projection" };

    private readonly Dictionary<string, string> _map = new();

    public string this[string component] => _map[component];

    /// <summary>Set a component precision — closed vocab on both
    /// sides.</summary>
    public void Set(string component, string precision)
    {
        if (!Components.Contains(component))
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"unknown component '{component}'");
        NativePrecisionLab.ValidatePrecision(precision);
        _map[component] = precision;
    }

    /// <summary>§11 Nemotron-style default map — router stays high
    /// precision; experts/kv may go lower.</summary>
    public static QuantizationPolicy Default()
    {
        var p = new QuantizationPolicy();
        p.Set("embedding", "BF16");
        p.Set("attention_qkv", "BF16");
        p.Set("attention_out", "BF16");
        p.Set("moe_expert", "BF16");
        p.Set("shared_expert", "BF16");
        p.Set("router", "FP32");       // §11 — router never quantizes first
        p.Set("lm_head", "BF16");
        p.Set("kv_cache", "INT8");
        p.Set("recurrent_state", "FP64"); // §12 — drift accumulates
        p.Set("vision_projection", "BF16");
        return p;
    }

    /// <summary>§11.1 compatibility adapter — legacy scalar
    /// model_precision expands to a map (router and recurrent_state
    /// keep their floors regardless).</summary>
    public static QuantizationPolicy FromLegacyScalar(string precision)
    {
        var p = Default();
        foreach (var c in Components)
            if (c is not ("router" or "recurrent_state"))
                p.Set(c, precision);
        return p;
    }

    /// <summary>Complete + valid => serialize. A partial map fails
    /// closed — ambiguity is worse than absence.</summary>
    public Dictionary<string, object?> ToDict()
    {
        var missing = Components.Where(c => !_map.ContainsKey(c)).ToList();
        if (missing.Count > 0)
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"precision map incomplete: {string.Join(",", missing)}");
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["components"] = _map.ToDictionary(
                kv => kv.Key, kv => (object?)kv.Value),
        };
    }
}

/// <summary>§26 PrecisionRoadmap — promotion order is fixed; a profile
/// at stage N may only promote to N+1 (never leap to production).</summary>
internal static class PrecisionRoadmap
{
    public static readonly (int stage, string name)[] Stages =
    {
        (0, "FP64 reference"),
        (1, "BF16 production candidate"),
        (2, "FP8 component-wise"),
        (3, "FP4 component-wise"),
        (4, "hardware-specific optimized format"),
    };

    /// <summary>Stage for a profile — production INT8 lane (kv/edge)
    /// sits at stage 1 alongside the BF16 candidate lane; experimental
    /// FP8=2, FP4=3, hardware-specific=4.</summary>
    public static int StageOf(string profile) => profile switch
    {
        PrecisionProfiles.ReferenceFp64 => 0,
        PrecisionProfiles.ProductionBf16 => 1,
        PrecisionProfiles.EdgeInt8 => 1,
        PrecisionProfiles.CompactFp8 => 2,
        NativePrecisionLab.ExperimentalFp8 => 2,
        NativePrecisionLab.ExperimentalFp4 => 3,
        _ => -1,
    };

    /// <summary>Promotion is one stage at a time — FP64→FP4 production
    /// is forbidden (§26).</summary>
    public static bool CanPromote(int fromStage, int toStage)
        => fromStage >= 0 && toStage == fromStage + 1;
}

/// <summary>§28 star-quant-cert/v1 — certification record for one
/// precision map. Every listed metric is checked; any critical
/// regression blocks promotion.</summary>
internal sealed class QuantCert
{
    public const string Format = "star-quant-cert/v1";

    /// <summary>§28 required checks.</summary>
    public static readonly string[] Checks =
        { "logit_mae", "logit_max_error", "top1_agreement",
          "top5_agreement", "generation_agreement", "long_stream_drift",
          "moe_routing_agreement", "tool_call_format",
          "structured_output_validity", "vision_parity" };

    /// <summary>Critical checks — a regression on any one blocks
    /// promotion regardless of the rest.</summary>
    public static readonly string[] CriticalChecks =
        { "top1_agreement", "generation_agreement",
          "moe_routing_agreement", "tool_call_format",
          "structured_output_validity", "vision_parity" };

    /// <summary>Evaluate a cert run: results = check -> {passed:bool,
    /// critical_regression:bool}. Missing check = fail-closed.</summary>
    public static Dictionary<string, object?> Evaluate(
        Dictionary<string, Dictionary<string, object?>> results)
    {
        var missing = Checks.Where(c => !results.ContainsKey(c)).ToList();
        var critical = CriticalChecks.Where(c =>
            results.TryGetValue(c, out var r) &&
            r.TryGetValue("critical_regression", out var cr) &&
            cr is bool b && b).ToList();
        bool passed = missing.Count == 0 && critical.Count == 0 &&
            Checks.All(c =>
                results.TryGetValue(c, out var r) &&
                r.TryGetValue("passed", out var p) && p is bool ok && ok);
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["status"] = missing.Count > 0 ? "INCOMPLETE"
                       : critical.Count > 0 ? "BLOCKED_CRITICAL"
                       : passed ? "CERTIFIED" : "FAILED",
            ["missing_checks"] = missing,
            ["critical_regressions"] = critical,
            ["promotable"] = passed,
        };
    }
}
