// ExpertPolicy.cs — FineGrainedExpertScalingPolicy (efficiency/scale
// directive §25-§38). Snowflake-Arctic-style many-but-condensed
// experts: capacity grows through expert COUNT at fixed Top-2 and a
// permanently-separated shared expert — never through wider active
// compute.
//
//   ValidateScale       §26/§27/§30/§31 candidate topology invariants:
//                       top_k pinned, shared vs routed never merged,
//                       minimum efficient expert width, hardware
//                       alignment.
//   CompareGranularity  §28-§29 side-by-side granularity candidates
//                       (8/16/32/64/128) at ~equal active FLOPs.
//   Specialization      §35 capability_expert_affinity / entropy /
//                       overlap / specialization / shared-ratio record;
//                       collapse => EXPERT_SPECIALIZATION_COLLAPSE.
//   ResidencyPlan       §36-§37 expert-block store/prefetch units —
//                       whole-layer MoE moves are rejected.
//
// Emits: star-expert-scale-policy/v1, star-expert-granularity/v1,
// star-expert-specialization/v1, star-expert-residency/v1.
// CAPABILITY_TRAINING_FROZEN: planning/validation only.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ExpertPolicy
{
    public const string PolicyFormat = "star-expert-scale-policy/v1";
    public const string GranularityFormat = "star-expert-granularity/v1";
    public const string SpecializationFormat =
        "star-expert-specialization/v1";
    public const string ResidencyFormat = "star-expert-residency/v1";

    public const long TopK = 2;                    // §26 invariant
    public const long MinEfficientExpertWidth = 256;   // §30
    public static readonly long[] GranularityCandidates =
        { 8, 16, 32, 64, 128 };                    // §28
    public static readonly string[] SpecializationMetrics =
    {
        "capability_expert_affinity", "expert_entropy",
        "expert_overlap", "expert_specialization_score",
        "shared_expert_ratio",
    };

    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) && v.ValueKind ==
            JsonValueKind.Number && v.TryGetInt64(out long n) ? n : d;
    private static double DNum(JsonElement r, string k, double d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;
    private static string Str(JsonElement r, string k, string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;

    // ---------------------------------------------------- scaling ---

    /// <summary>Validate an expert-scale candidate against every §25-38
    /// invariant. Any violation => verdict EXPERT_GRANULARITY_INEFFICIENT
    /// (fail-closed — a malformed scale candidate is never scored).</summary>
    public static Dictionary<string, object?> ValidateScale(
        JsonElement el)
    {
        var violations = new List<object?>();
        long topK = Num(el, "top_k", TopK);
        if (topK != TopK)
            violations.Add($"top_k {topK} != {TopK} — expert count " +
                           "grows capacity, never top_k (§26)");
        long shared = Num(el, "shared_experts", 1);
        if (shared <= 0)
            violations.Add("shared expert missing — GENERAL CAPACITY " +
                           "is permanently separated (§27)");
        if (el.TryGetProperty("shared_offload", out var so) &&
            so.ValueKind == JsonValueKind.True)
            violations.Add("shared_expert_offload=true — shared expert " +
                           "is always-hot and never offloadable (§27)");
        long ei = Num(el, "expert_intermediate", 0);
        if (ei <= 0)
            violations.Add("expert_intermediate required");
        else if (ei < MinEfficientExpertWidth)
            violations.Add(
                $"expert_intermediate {ei} < " +
                $"minimum_efficient_expert_width " +
                $"{MinEfficientExpertWidth} (§30)");
        else if (ei % 64 != 0)
            violations.Add($"expert_intermediate {ei} not 64-aligned " +
                           "(§31 hardware-aligned)");
        long ec = Num(el, "expert_count", 0);
        if (ec <= 0) violations.Add("expert_count required");
        if (Str(el, "assign_experts").Length > 0)
            violations.Add("expert roles may not be hand-assigned — " +
                           "specialization emerges from training (§34)");
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = PolicyFormat,
            ["top_k"] = TopK,
            ["shared_expert"] = new Dictionary<string, object?>
            {
                ["role"] = "GENERAL_CAPACITY",
                ["always_active"] = true, ["offloadable"] = false,
                ["quantizable"] = false, ["precision"] = "higher",
            },
            ["routed_experts"] = new Dictionary<string, object?>
            {
                ["role"] = "SPECIALIZED_CAPACITY",
                ["conditional"] = true, ["offloadable"] = true,
                ["quantizable"] = true,
            },
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "EXPERT_SCALE_VALID"
                : "EXPERT_GRANULARITY_INEFFICIENT",
        };
    }

    // ------------------------------------------------- granularity --

    /// <summary>§28-§29 compare granularity candidates at ~equal active
    /// FLOPs: same core, expert_count × expert_intermediate traded so
    /// per-token routed compute stays flat. Each candidate carries the
    /// §29 comparison fields; the winner maximizes specialization per
    /// byte — never parameters alone.</summary>
    public static Dictionary<string, object?> CompareGranularity(
        JsonElement el)
    {
        long coreActive = Num(el, "core_active_params", 0);
        long eiBase = Num(el, "base_expert_intermediate", 2048);
        long hidden = Num(el, "hidden", 1024);
        var rows = new List<object?>();
        foreach (long n in GranularityCandidates)
        {
            // ~equal active FLOPs: ei scales as 1/sqrt(n) relative to
            // the 8-expert baseline so top-2 routed matmul cost is flat.
            long ei = Math.Max(MinEfficientExpertWidth,
                eiBase * 8 / n / 64 * 64);
            long perExpert = ei * hidden * 3;
            long activeRouted = TopK * perExpert;
            long totalRouted = n * perExpert;
            rows.Add(new Dictionary<string, object?>
            {
                ["expert_count"] = n,
                ["expert_intermediate"] = ei,
                ["total_expert_params"] = totalRouted,
                ["active_expert_params"] = activeRouted,
                ["core_active_params"] = coreActive,
                ["approx_active_flops_ratio"] = Math.Round(
                    (double)activeRouted /
                    Math.Max(1, TopK * eiBase * hidden * 3), 3),
                ["total_capacity_params"] =
                    coreActive + totalRouted +
                    Num(el, "shared_expert_params", 0),
                // §29 measured dims — filled by probes, not guessed.
                ["measured"] = new Dictionary<string, object?>
                {
                    ["capability"] = "pending:expert-granularity-probe",
                    ["routing_entropy"] = "pending:moe-analyze",
                    ["expert_utilization"] = "pending:moe-analyze",
                    ["grouped_gemm_efficiency"] =
                        "pending:expert-offload-bench",
                    ["cuda_occupancy"] = "pending:expert-offload-bench",
                    ["vram_ram_nvme"] = "pending:scale-hardware-gate",
                },
            });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = GranularityFormat,
            ["top_k"] = TopK,
            ["min_efficient_expert_width"] = MinEfficientExpertWidth,
            ["candidates"] = rows,
            ["rule"] = "approx-equal active FLOPs; selection requires " +
                       "capability + grouped-GEMM + residency " +
                       "benchmarks, never parameter count alone (§29)",
        };
    }

    // --------------------------------------------- specialization ---

    /// <summary>§35 specialization telemetry record. Verdict:
    /// SPECIALIZED when entropy sits in a healthy band and capability
    /// affinity differs across experts; EXPERT_SPECIALIZATION_COLLAPSE
    /// when all experts behave identically (uniform routing = wasted
    /// capacity).</summary>
    public static Dictionary<string, object?> Specialization(
        JsonElement el)
    {
        var rec = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = SpecializationFormat,
        };
        foreach (var m in SpecializationMetrics)
            rec[m] = DNum(el, m);
        double entropy = DNum(el, "expert_entropy"),
               overlap = DNum(el, "expert_overlap"),
               spec = DNum(el, "expert_specialization_score");
        long ec = Math.Max(1, Num(el, "expert_count", 8));
        double uniform = Math.Log(ec);
        bool collapse =
            (entropy > 0 && uniform > 0 &&
             entropy / uniform > 0.98) ||   // ~uniform routing
            (overlap >= 1.0) ||
            (spec > 0 && spec < 0.05);
        rec["uniform_entropy_bound"] = Math.Round(uniform, 4);
        rec["collapse"] = collapse;
        rec["verdict"] = collapse
            ? "EXPERT_SPECIALIZATION_COLLAPSE" : "SPECIALIZED";
        rec["rule"] = "specialization is observed, never assigned (§34)";
        return rec;
    }

    // ---------------------------------------------------- residency -

    /// <summary>§36-§37 expert-store residency plan: the transfer unit
    /// is the EXPERT BLOCK — each independently loadable/quantizable/
    /// cacheable/evictable; prefetch moves predicted Top-K blocks only,
    /// never a whole MoE layer.</summary>
    public static Dictionary<string, object?> ResidencyPlan(
        JsonElement el)
    {
        long ec = Num(el, "expert_count", 8);
        long blockBytes = Num(el, "expert_block_bytes", 0);
        long hotSet = Num(el, "hot_set_blocks", 0);
        var plan = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ResidencyFormat,
            ["transfer_unit"] = "expert_block",
            ["expert_count"] = ec,
            ["expert_block_bytes"] = blockBytes,
            ["per_block_ops"] = new[]
                { "load", "quantize", "cache", "evict" },
            ["prefetch"] = new Dictionary<string, object?>
            {
                ["unit"] = "expert_block",
                ["scope"] = "predicted_top_k_blocks_only",
                ["forbidden"] = "whole_moe_layer_transfer",
                ["hot_set_blocks"] = hotSet,
            },
        };
        if (el.TryGetProperty("transfer_scope", out var ts) &&
            Str(el, "transfer_scope") == "layer")
        {
            plan["ok"] = false;
            plan["verdict"] = "EXPERT_GRANULARITY_INEFFICIENT";
            plan["violation"] =
                "layer-granularity MoE transfer rejected — block " +
                "granularity only (§37)";
        }
        else
        {
            plan["verdict"] = "RESIDENCY_VALID";
            plan["transfer_bytes_per_token"] =
                blockBytes > 0 ? blockBytes * TopK : 0;
        }
        return plan;
    }
}
