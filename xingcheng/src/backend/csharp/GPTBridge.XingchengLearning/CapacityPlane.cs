// CapacityPlane.cs — capacity & active-parameter formal spec
// (capacity directive §0-§57). Complements ProductScale (tier table)
// and SiliconRuntime (hardware surface); this plane owns the
// capability-side contracts:
//
//   SixParamMetrics    §4/§5 — SOURCE/DISTILLED/UNIQUE/ACTIVE/
//                      TRAINABLE/RESIDENT + storage bytes; a record
//                      reporting only "model params" fails validation.
//   CommonFloorGate    §32-§34 — common+shared >=600M fails with
//                      COMMON_FLOOR_TOO_HIGH; routed active budget =
//                      active_ceiling - common - shared.
//   EffectiveCompute   §37-§38 — active params × executed tokens +
//                      transfers + MTP verification, not just params.
//   NativeDistillationPlane §9-§15 — logit/hidden/capability/routing
//                      lanes + reasoning-compression verdict
//                      (THINKING_COMPRESSION_REGRESSION) + quant-aware
//                      + teacher/student precision separation.
//   GenerationPipeline §7/§54 — ordered stages; promotion requires
//                      every stage green.
//   ExpertLifecycle    §16-§19 — similarity -> MERGE/DISTILL/
//                      FACTORIZE/PRUNE candidates; dead-expert gate;
//                      magnitude-only pruning denied.
//   QuantizationPolicy §20-§26 — mixed-precision table: router FP32,
//                      common/shared BF16, routed hot BF16-INT8 /
//                      warm INT8 / cold INT4; INT4 on common/shared
//                      denied; student trains at production precision.
//   CapacityCeiling    §51-§53 — total ceiling configurable (20B now);
//                      ACTIVE_PARAMS <=1B is the permanent invariant.
//   CapacityKpis       §56 — the fixed KPI field set.
//
// All fail codes follow §55. Contracts only — no weights mutated.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapacityPlane
{
    public const string MetricsFormat = "star-capacity-metrics/v1";
    public const string DistillFormat = "star-distillation-contract/v1";
    public const string PipelineFormat = "star-generation-pipeline/v1";
    public const string EffectiveFormat = "star-effective-compute/v1";
    public const string CompressFormat = "star-compression-report/v1";
    public const string KpiFormat = "star-capacity-kpis/v1";

    // §0 hard limits.
    public const long DefaultTotalCeiling = 20_000_000_000L;
    public const long ActiveCeiling = 1_000_000_000L;
    public const long ActivePreferredMin = 400_000_000L;
    public const long ActivePreferredMax = 700_000_000L;
    // §32 common floor.
    public const long CommonFloorTargetMin = 300_000_000L;
    public const long CommonFloorTargetMax = 500_000_000L;
    public const long CommonFloorHard = 600_000_000L;
    // codex architecture-tool-local-model: every publishable model
    // configuration must sit inside [300M, 20B] inclusive — below 300M
    // or above 20B can never become a formal model; any baseline
    // claiming complete capability may not be under 300M. Parameter
    // count bounds scale/resources only — it never substitutes for
    // capability tests, quality evidence, or release conditions.
    public const long PublishableMinParams = 300_000_000L;
    public const long PublishableMaxParams = DefaultTotalCeiling;

    /// <summary>§4 the six parameter counts — "model params" alone is
    /// never an acceptable report.</summary>
    public static readonly string[] ParamMetrics =
    {
        "SOURCE_PARAMS", "DISTILLED_TOTAL_PARAMS", "UNIQUE_PARAMS",
        "ACTIVE_PARAMS", "TRAINABLE_PARAMS", "RESIDENT_PARAMS",
    };
    /// <summary>§5 storage metrics — quantization changes bytes, not
    /// parameter count.</summary>
    public static readonly string[] StorageMetrics =
    {
        "BF16_WEIGHT_BYTES", "QUANTIZED_WEIGHT_BYTES",
        "GPU_RESIDENT_BYTES", "RAM_RESIDENT_BYTES", "NVME_BYTES",
    };

    /// <summary>§56 fixed KPI set.</summary>
    public static readonly string[] Kpis =
    {
        "total_params", "unique_params", "active_params",
        "active_ratio", "trainable_params", "gpu_resident_params",
        "ram_resident_params", "nvme_params", "compressed_bytes",
        "reasoning_tokens", "transfer_bytes_per_token",
        "effective_compute",
    };

    /// <summary>§7 the formal generation pipeline — training never
    /// reaches production directly.</summary>
    public static readonly string[] Pipeline =
    {
        "TRAIN", "CAPABILITY_RECOVERY", "DISTILL", "STRUCTURAL_PRUNE",
        "QUANTIZE", "COMPRESS", "CALIBRATE", "EVALUATE", "CERTIFY",
        "PROMOTE",
    };

    /// <summary>§9 distillation lanes.</summary>
    public static readonly string[] DistillLanes =
    {
        "LOGIT_DISTILLATION", "HIDDEN_STATE_DISTILLATION",
        "CAPABILITY_DISTILLATION", "ROUTING_DISTILLATION",
    };

    // ------------------------------------------------ ceilings ----

    /// <summary>§51-§53: total ceiling is a tunable (20B now, may
    /// grow); the 1B active ceiling is permanent and independent —
    /// TOTAL can scale, execution cost must not follow.</summary>
    public static Dictionary<string, object?> CeilingReport(
        long totalCeiling = DefaultTotalCeiling)
        => new()
        {
            ["ok"] = true,
            ["format"] = "star-capacity-ceiling/v1",
            ["current_total_capacity_ceiling"] = totalCeiling,
            ["active_params_ceiling"] = ActiveCeiling,
            ["active_preferred"] = new Dictionary<string, object?>
            {
                ["min"] = ActivePreferredMin,
                ["max"] = ActivePreferredMax,
            },
            ["rule"] = "ACTIVE_PARAMS<=1B is permanent; "
                       + "TOTAL ceiling may grow without raising it",
        };

    /// <summary>§35/§53: any candidate whose normal inference exceeds
    /// the active ceiling is rejected before benchmarking — regardless
    /// of total size.</summary>
    public static void RequireWithinActiveCeiling(long activeParams)
    {
        if (activeParams > ActiveCeiling)
            throw new ExecutorError("ACTIVE_PARAMETER_CEILING_EXCEEDED",
                $"active {activeParams} exceeds hard ceiling " +
                $"{ActiveCeiling} — rejected before benchmark (§35)");
    }

    /// <summary>Publishable-scale boundary (codex
    /// architecture-tool-local-model): formal model configurations must
    /// total [300M, 20B] inclusive. A boundary check only — capability
    /// evidence remains a separate release condition.</summary>
    public static void RequirePublishableScale(long totalParams,
                                             string subject)
    {
        if (totalParams < PublishableMinParams ||
            totalParams > PublishableMaxParams)
            throw new ExecutorError("MODEL_SCALE_OUT_OF_PUBLISHABLE_BAND",
                $"{subject} total_params {totalParams} outside the " +
                $"publishable band [{PublishableMinParams}, " +
                $"{PublishableMaxParams}] — cannot become a formal model");
    }

    /// <summary>Total declared parameters of a publishable artifact:
    /// a bundle directory's manifest.json ``param_count``. Returns
    /// null when the artifact does not declare a scale (bare files are
    /// not publishable configurations).</summary>
    public static long? BundleTotalParams(string path)
    {
        if (!Directory.Exists(path)) return null;
        string manifest = Path.Combine(path, "manifest.json");
        if (!File.Exists(manifest)) return null;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(manifest));
            if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                doc.RootElement.TryGetProperty("param_count", out var pc) &&
                pc.ValueKind == JsonValueKind.Number &&
                pc.TryGetInt64(out long total))
                return total;
        }
        catch (Exception ex) when (ex is JsonException or IOException
                                       or UnauthorizedAccessException)
        {
            return null;
        }
        return null;
    }

    /// <summary>Fail-closed gate for weights ACTIVATION: a bundle dir
    /// being promoted to a formal model must declare param_count in its
    /// manifest and sit inside the publishable band. Bare checkpoint
    /// files are not publishable configurations and are not gated
    /// here.</summary>
    public static void EnforcePublishableOnActivate(string artifactPath)
    {
        if (!Directory.Exists(artifactPath)) return;
        long? total = BundleTotalParams(artifactPath);
        if (!total.HasValue || total.Value <= 0)
            throw new ExecutorError("MODEL_SCALE_UNVERIFIABLE",
                $"activating bundle '{artifactPath}' has no readable " +
                "manifest.json param_count — publishable scale cannot " +
                "be proven");
        RequirePublishableScale(total.Value, artifactPath);
    }

    /// <summary>§32-§34: common+shared floor gate and the routed
    /// active budget it leaves under the 1B ceiling.</summary>
    public static Dictionary<string, object?> CommonFloorGate(
        long common, long shared)
    {
        long floor = common + shared;
        var rep = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-common-floor/v1",
            ["common_core"] = common, ["shared_expert"] = shared,
            ["common_plus_shared"] = floor,
            ["target_band"] = new Dictionary<string, object?>
            {
                ["min"] = CommonFloorTargetMin,
                ["max"] = CommonFloorTargetMax,
                ["preferred_max"] = 400_000_000L,
            },
            // §33 routed active = ceiling minus floor.
            ["routed_active_budget"] = ActiveCeiling - floor,
            ["routed_active_preferred"] =
                new Dictionary<string, object?>
                { ["min"] = 150_000_000L, ["max"] = 300_000_000L },
        };
        if (floor >= CommonFloorHard)
        {
            rep["verdict"] = "COMMON_FLOOR_TOO_HIGH";
            rep["ok"] = false;
        }
        else rep["verdict"] = "FLOOR_OK";
        return rep;
    }

    // ----------------------------------------------- six metrics --

    /// <summary>§4/§5: validate a metrics record carries all six
    /// parameter counts and all five storage counts.</summary>
    public static Dictionary<string, object?> ValidateMetrics(
        JsonElement el)
    {
        var missing = ParamMetrics.Concat(StorageMetrics)
            .Where(k => !el.TryGetProperty(k, out _))
            .Cast<object?>().ToList();
        if (missing.Count > 0)
            throw new ExecutorError("ACTIVE_PARAMS_MISSING",
                "capacity record incomplete: " +
                string.Join(",", missing.Cast<string>()));
        long active = Num(el, "ACTIVE_PARAMS");
        long total = Num(el, "DISTILLED_TOTAL_PARAMS");
        RequireWithinActiveCeiling(active);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = MetricsFormat,
            ["metrics_complete"] = true,
            ["active_ratio"] = total > 0
                ? Math.Round((double)active / total, 6) : 0.0,
        };
    }

    // ------------------------------------------ effective compute --

    /// <summary>§37-§38: effective compute = active params × executed
    /// tokens (+ executed-layer share + expert transfer + MTP
    /// verification), because a 600M-active model running 10x
    /// reasoning tokens is not cheap.</summary>
    public static Dictionary<string, object?> EffectiveCompute(
        long activeParams, long executedTokens, long executedLayers,
        long totalLayers, long expertTransferBytes,
        long mtpVerifiedTokens)
    {
        double layerShare = totalLayers > 0
            ? (double)executedLayers / totalLayers : 1.0;
        double ec = (double)activeParams * executedTokens * layerShare
            + expertTransferBytes * 0.001
            + mtpVerifiedTokens * activeParams * 0.1;
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = EffectiveFormat,
            ["active_params"] = activeParams,
            ["executed_tokens"] = executedTokens,
            ["executed_layers"] = executedLayers,
            ["expert_transfer_bytes"] = expertTransferBytes,
            ["mtp_verified_tokens"] = mtpVerifiedTokens,
            ["effective_compute"] = ec,
            ["note"] = "thinking tokens multiply cost — FAST/LOW "
                       + "thinking is the optimization target (§14)",
        };
    }

    // ------------------------------------------------ distillation --

    /// <summary>§9-§15 NativeDistillationPlane contract — four lanes
    /// plus reasoning compression; student keeps its own Top-2
    /// (§12) and trains at production precision (§25-§26).</summary>
    public static Dictionary<string, object?> DistillationContract()
        => new()
        {
            ["ok"] = true, ["format"] = DistillFormat,
            ["plane"] = "NativeDistillationPlane",
            ["lanes"] = DistillLanes.Cast<object?>().ToList(),
            ["logit"] = new Dictionary<string, object?>
            {
                ["signal"] = "teacher top-N probabilities + "
                            + "temperature + confidence",
                ["storage"] = "top-N only — never full teacher logits",
            },
            ["capability"] = new Dictionary<string, object?>
            {
                ["per_capability"] = new[]
                {
                    "instruction", "context", "math", "coding", "tool",
                    "rag", "thinking", "vision",
                },
                ["rule"] = "global perplexity alone is not a "
                           + "distillation metric (§11)",
            },
            ["routing"] = new Dictionary<string, object?>
            {
                ["learns"] = new[]
                {
                    "expert_affinity", "routing_confidence",
                    "specialization_boundaries",
                },
                ["student_top_k"] = 2,
            },
            ["quantization_aware"] = true,
            ["teacher_precision"] = "higher (BF16+) — teacher is not "
                                    + "production (§8/§26)",
            ["student_precision"] = "production-like — quantization "
                                    + "loss observed during distill",
            ["shared_knowledge_first"] =
                "cross-capability knowledge -> Common Core / Shared "
                + "Expert; specialized -> Routed Experts (§15)",
        };

    /// <summary>§14 ReasoningCompressionDistillation verdict: the
    /// student wins when capability is within tolerance at materially
    /// fewer reasoning tokens; otherwise the compression is a
    /// regression.</summary>
    public static Dictionary<string, object?> ReasoningCompression(
        long teacherTokens, long studentTokens,
        double teacherScore, double studentScore,
        double tolerance = 0.02)
    {
        double delta = teacherScore - studentScore;
        bool regressed = delta > tolerance;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-reasoning-compression/v1",
            ["teacher_reasoning_tokens"] = teacherTokens,
            ["student_reasoning_tokens"] = studentTokens,
            ["compression"] = teacherTokens > 0
                ? Math.Round((double)studentTokens / teacherTokens, 4)
                : 0.0,
            ["capability_delta"] = delta,
            ["verdict"] = regressed
                ? "THINKING_COMPRESSION_REGRESSION"
                : studentTokens < teacherTokens
                    ? "STUDENT_WINS" : "NO_COMPRESSION_GAIN",
        };
    }

    /// <summary>§11 capability distillation gate: compare per-capability
    /// scores, never just a global metric.</summary>
    public static Dictionary<string, object?> CapabilityDistillGate(
        IReadOnlyDictionary<string, double> teacher,
        IReadOnlyDictionary<string, double> student,
        double tolerance = 0.02)
    {
        var regressions = new List<object?>();
        foreach (var kv in teacher)
        {
            double s = student.TryGetValue(kv.Key, out var v)
                ? v : 0.0;
            if (kv.Value - s > tolerance)
                regressions.Add(new Dictionary<string, object?>
                {
                    ["capability"] = kv.Key,
                    ["teacher"] = kv.Value, ["student"] = s,
                });
        }
        if (regressions.Count > 0)
            throw new ExecutorError("DISTILLATION_REGRESSION",
                "capability regressions: " +
                string.Join(",", regressions
                    .Cast<Dictionary<string, object?>>()
                    .Select(r => (string)r["capability"]!)));
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["verdict"] = "DISTILLATION_CERTIFIED",
            ["capabilities_checked"] = teacher.Count,
        };
    }

    // ------------------------------------------------- quantization --

    /// <summary>§20-§24/§32 (precision directive): the mixed-precision
    /// table. Validation fails closed on a too-aggressive choice.</summary>
    public static readonly (string Component, string[] Allowed)[]
        PrecisionTable =
        {
            ("router", new[] { "FP32" }),
            ("common_core", new[] { "BF16", "INT8_CERTIFIED" }),
            ("shared_expert", new[] { "BF16", "INT8_CERTIFIED" }),
            ("routed_hot", new[] { "BF16", "INT8" }),
            ("routed_warm", new[] { "INT8" }),
            ("routed_cold", new[] { "INT4", "INT8" }),
            ("kv_cache", new[] { "INT8", "BF16" }),
            ("delta_state", new[] { "BF16", "FP32" }),
        };

    public static Dictionary<string, object?> ValidatePrecision(
        string component, string precision, bool certified = false)
    {
        var row = PrecisionTable.FirstOrDefault(
            r => r.Component == component);
        if (row.Component == null)
            throw new ExecutorError("QUANTIZATION_REGRESSION",
                $"unknown component '{component}'");
        string want = precision.ToUpperInvariant();
        bool ok = row.Allowed.Contains(want) ||
                  (want == "INT8" && certified &&
                   row.Allowed.Contains("INT8_CERTIFIED"));
        if (!ok)
            throw new ExecutorError("QUANTIZATION_REGRESSION",
                $"{component}@{precision} violates the precision table " +
                $"({string.Join("/", row.Allowed)})");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-precision-policy/v1",
            ["component"] = component, ["precision"] = want,
            ["policy"] = "mixed precision — never one bit for all (§20)",
        };
    }

    /// <summary>§27-§30: three compression levels reported separately.</summary>
    public static Dictionary<string, object?> CompressionReport(
        long paramCompressed, long precisionSavedBytes,
        long workingSetBytes)
        => new()
        {
            ["ok"] = true, ["format"] = CompressFormat,
            ["A_parameter_compression"] = new Dictionary<string, object?>
            {
                ["params_removed"] = paramCompressed,
                ["via"] = new[] { "distillation", "pruning",
                                  "expert_dedup", "shared_params" },
            },
            ["B_precision_compression"] =
                new Dictionary<string, object?>
            {
                ["bytes_saved"] = precisionSavedBytes,
                ["via"] = new[] { "BF16", "INT8", "INT4",
                                  "block_groupwise" },
            },
            ["C_working_set_compression"] =
                new Dictionary<string, object?>
            {
                ["bytes"] = workingSetBytes,
                ["via"] = new[] { "top2", "expert_residency",
                                  "prefix_cache", "kv_int8",
                                  "delta_state", "thinking_auto",
                                  "mtp" },
            },
        };

    // ---------------------------------------------- expert lifecycle --

    /// <summary>§16-§19: expert lifecycle gate. Similarity evidence
    /// produces MERGE/DISTILL/FACTORIZE candidates; utilization +
    /// contribution + replaceability drive dead-expert pruning; a
    /// magnitude-only prune without a capability gate is denied.</summary>
    public static Dictionary<string, object?> ExpertLifecycle(
        int expertId, double weightSimilarity, double utilization,
        double capabilityContribution, bool replaceable,
        bool capabilityGatePassed)
    {
        var candidates = new List<object?>();
        if (weightSimilarity > 0.9)
            candidates.AddRange(new object?[]
                { "MERGE", "DISTILL", "FACTORIZE" });
        if (utilization < 0.001 &&
            capabilityContribution < 0.005 && replaceable)
            candidates.Add("PRUNE");
        if (capabilityContribution > 0.05 && utilization > 0.3 &&
            weightSimilarity < 0.3)
            candidates.Add("SPLIT_CANDIDATE");   // overloaded & mixed
        bool prunable = candidates.Contains("PRUNE");
        if (prunable && !capabilityGatePassed)
            throw new ExecutorError("EXPERT_PRUNING_REGRESSION",
                $"expert {expertId} pruning denied — capability gate "
                + "did not pass (§19: magnitude alone never ships)");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-expert-lifecycle/v1",
            ["expert_id"] = expertId,
            ["weight_similarity"] = weightSimilarity,
            ["utilization"] = utilization,
            ["capability_contribution"] = capabilityContribution,
            ["candidates"] = candidates,
            ["prunable"] = prunable,
            ["rule"] = "expert count is not only-up; merge/split are "
                       + "evidence-driven (§18)",
        };
    }

    // ---------------------------------------------------- pipeline --

    /// <summary>§7/§54: promotion requires every pipeline stage plus
    /// the §54 legs. A record missing any stage is rejected.</summary>
    public static Dictionary<string, object?> PromotionGate(
        IReadOnlyDictionary<string, bool> stages)
    {
        var required = Pipeline.Concat(new[]
        {
            "CAPABILITY", "DISTILLATION", "QUANTIZATION", "COMPRESSION",
            "ACTIVE_COMPUTE", "MEMORY", "LATENCY", "REGRESSION",
            "LINEAGE",
        });
        var missing = required
            .Where(s => !stages.TryGetValue(s, out bool ok) || !ok)
            .Cast<object?>().ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = missing.Count == 0,
            ["format"] = PipelineFormat,
            ["pipeline"] = Pipeline.Cast<object?>().ToList(),
            ["unmet"] = missing,
            ["verdict"] = missing.Count == 0
                ? "PROMOTION_ELIGIBLE" : "PROMOTION_BLOCKED",
        };
    }

    /// <summary>§56 fixed KPI record.</summary>
    public static Dictionary<string, object?> KpiReport(JsonElement el)
    {
        var rep = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = KpiFormat,
        };
        foreach (string k in Kpis)
            rep[k] = el.TryGetProperty(k, out var v) &&
                     v.ValueKind == JsonValueKind.Number
                ? (object?)v.Clone() : null;
        rep["kpi_set_fixed"] = true;
        return rep;
    }

    // ----------------------------------------- JsonElement input --

    public static Dictionary<string, object?> EffectiveCompute(
        JsonElement el)
        => EffectiveCompute(
            Num(el, "active_params"), Num(el, "executed_tokens"),
            Num(el, "executed_layers", 1), Num(el, "total_layers", 1),
            Num(el, "expert_transfer_bytes"),
            Num(el, "mtp_verified_tokens"));

    public static Dictionary<string, object?> ReasoningCompression(
        JsonElement el)
        => ReasoningCompression(
            Num(el, "teacher_reasoning_tokens"),
            Num(el, "student_reasoning_tokens"),
            NumF(el, "teacher_score"), NumF(el, "student_score"),
            el.TryGetProperty("tolerance", out var t) &&
                t.ValueKind == JsonValueKind.Number
                    ? t.GetDouble() : 0.02);

    public static Dictionary<string, object?> ValidatePrecision(
        JsonElement el)
        => ValidatePrecision(
            Str(el, "component"), Str(el, "precision"),
            el.TryGetProperty("certified", out var c) &&
                c.ValueKind == JsonValueKind.True);

    public static Dictionary<string, object?> ExpertLifecycle(
        JsonElement el)
        => ExpertLifecycle(
            (int)Num(el, "expert_id"), NumF(el, "weight_similarity"),
            NumF(el, "utilization"), NumF(el, "capability_contribution"),
            el.TryGetProperty("replaceable", out var r) &&
                r.ValueKind == JsonValueKind.True,
            el.TryGetProperty("capability_gate_passed", out var g) &&
                g.ValueKind == JsonValueKind.True);

    public static Dictionary<string, object?> PromotionGate(
        JsonElement el)
    {
        var stages = new Dictionary<string, bool>();
        foreach (var p in el.EnumerateObject())
            if (p.Value.ValueKind is JsonValueKind.True or
                    JsonValueKind.False)
                stages[p.Name] = p.Value.ValueKind ==
                    JsonValueKind.True;
        return PromotionGate(stages);
    }

    internal static long Num(JsonElement el, string k, long d = 0)
        => el.TryGetProperty(k, out var v) &&
           v.ValueKind == JsonValueKind.Number
            ? v.GetInt64() : d;

    internal static string Str(JsonElement el, string k)
        => el.TryGetProperty(k, out var v) &&
           v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    internal static double NumF(JsonElement el, string k, double d = 0.0)
        => el.TryGetProperty(k, out var v) &&
           v.ValueKind == JsonValueKind.Number
            ? v.GetDouble() : d;
}
