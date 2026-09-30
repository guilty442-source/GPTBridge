// ProductScale.cs — the formal Xingcheng product tiers
// (scale-tier directive §0-§66): ONE architecture (xc-fused-1,
// HybridCausalDecoder) carried by three scale profiles.
//
//   xc-300m-dev              development / capability lab
//   xc-1b-standard           default production — total 0.9-1.1B,
//                            active 450-650M (ceiling 800M)
//   xc-20b-extreme-sparse    high capacity, low active — total
//                            18-22B, active <=700M (ceiling 1B =>
//                            LOW_SPARSITY_FAILURE)
//
//   Tiers             the three canonical star-scale-profile/v1
//                     records with their budget contracts (§6-8,
//                     §12-13, §20-25, §60).
//   Identity          §2 star-model-identity/v1 — arch + scale +
//                     weight + runtime + bundle hash.
//   ActiveGate        §47 ActiveComputeGate — a scale that reports
//                     only total_params is rejected outright.
//   ValidateTier      a candidate profile against its tier budgets
//                     (common floor, shared budget, top_k, expert
//                     whitelist, active ceiling, active ratio).
//   ResidencyPlan     §28-§32 residency contract per tier.
//   TrainableBudget   §35-§38 sparse-training bounds.
//   ThinkingLevels    §49-§51 sparse-thinking contract.
//
// CAPABILITY_TRAINING_FROZEN: contracts only — no weights created.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ProductScale
{
    public const string TierFormat = "star-scale-tier/v1";
    public const string IdentityFormat = "star-model-identity/v1";
    public const string ActiveGateFormat = "star-active-compute-gate/v1";
    public const string TierCheckFormat = "star-scale-tier-check/v1";
    public const string ResidencyFormat = "star-scale-residency/v1";
    public const string TrainableFormat = "star-trainable-budget/v1";
    public const string ThinkingFormat = "star-thinking-levels/v1";

    // §9 parameter layers — every scale decomposes this way.
    public static readonly string[] ParamLayers =
        { "COMMON_CORE", "SHARED_EXPERT", "ROUTED_EXPERT_BANK" };
    // §27 the only residency bands.
    public static readonly string[] ResidencyBands =
        { "GPU", "RAM", "NVME_COLD" };
    // §49 thinking levels; AUTO is the default (§50).
    public static readonly string[] ThinkingLevels =
        { "OFF", "FAST", "LOW", "MEDIUM", "HIGH", "MAX", "AUTO" };

    // ------------------------------------------------------- tiers --

    /// <summary>The canonical tier table — budgets are design targets
    /// per §6-§13, §20-§25; exact shapes come from
    /// HardwareAwareScaleSearch, never hand-fixed (§12/§25).</summary>
    public static Dictionary<string, object?> Tiers()
    {
        Dictionary<string, object?> Tier(
            string profile, string role, long totalMin, long totalMax,
            long activeTargetMin, long activeTargetMax,
            long activeCeiling, double ratioTarget,
            long commonMin, long commonMax, long commonHard,
            long sharedMin, long sharedMax,
            long[] expertCandidates, long expertPrimary) => new()
        {
            ["scale_profile"] = profile,
            ["architecture_profile"] = "xc-fused-1",
            ["role"] = role,
            ["total_params_target"] = new Dictionary<string, object?>
                { ["min"] = totalMin, ["max"] = totalMax },
            ["active_params_target"] = new Dictionary<string, object?>
            {
                ["min"] = activeTargetMin, ["max"] = activeTargetMax,
                ["hard_ceiling"] = activeCeiling,
            },
            ["active_ratio_target"] = ratioTarget,
            ["common_core_budget"] = new Dictionary<string, object?>
            {
                ["min"] = commonMin, ["max"] = commonMax,
                ["hard_max"] = commonHard,
            },
            ["shared_expert_budget"] =
                new Dictionary<string, object?>
                { ["min"] = sharedMin, ["max"] = sharedMax },
            ["top_k"] = 2,
            ["shared_experts"] = 1,
            ["routed_expert_candidates"] =
                expertCandidates.Cast<object?>().ToList(),
            ["routed_expert_primary"] = expertPrimary,
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TierFormat,
            ["one_core_rule"] =
                "xc-fused-1 / HybridCausalDecoder for every tier — " +
                "scale is a scale_profile, never a second core (§1)",
            ["param_layers"] = ParamLayers,
            ["scale_order_rule"] =
                "Capability > Correctness > Active Efficiency > " +
                "Memory > Raw Total Param Count (§66)",
            ["tiers"] = new List<object?>
            {
                Tier("xc-300m-dev",
                     "development / capability lab — verifies training," +
                     " runtime, migration; never decides final shape " +
                     "(§62)",
                     250_000_000, 350_000_000,
                     250_000_000, 350_000_000, 350_000_000, 1.0,
                     200_000_000, 280_000_000, 350_000_000,
                     15_000_000, 40_000_000,
                     new long[] { 4, 8 }, 8),
                Tier("xc-1b-standard",
                     "STANDARD — default production / primary training " +
                     "and capability model (§3)",
                     900_000_000, 1_100_000_000,
                     450_000_000, 650_000_000, 800_000_000, 0.65,
                     300_000_000, 400_000_000, 500_000_000,
                     50_000_000, 100_000_000,
                     new long[] { 16, 32 }, 16),
                Tier("xc-20b-extreme-sparse",
                     "EXTREME_SPARSE — knowledge + specialization " +
                     "capacity at sub-1B active cost (§4, §61)",
                     18_000_000_000L, 22_000_000_000L,
                     500_000_000, 700_000_000, 1_000_000_000, 0.035,
                     300_000_000, 450_000_000, 500_000_000,
                     50_000_000, 150_000_000,
                     new long[] { 64, 128, 256 }, 128),
            },
            ["probe_scales"] =
                new long[] { 2_000_000_000L, 3_000_000_000L,
                             4_000_000_000L, 8_000_000_000L },
            ["note"] = "2B/3B/4B/8B are probe scales only — production " +
                       "is 300M -> 1B -> 20B (§43-44)",
        };
    }

    /// <summary>Typed view over the tier registry.</summary>
    public static List<Dictionary<string, object?>> TierDicts()
        => ((List<object?>)Tiers()["tiers"]!)
            .Select(t => (Dictionary<string, object?>)t).ToList();

    // ------------------------------------------- profile records --

    /// <summary>Where the canonical scale-profile contracts live —
    /// one JSON per tier plus an index.</summary>
    public const string ProfileRelDir =
        "xingcheng/runtime/scale-profiles";

    /// <summary>§63: the framework exists before the parameters. Every
    /// shape dimension stays PENDING until HardwareAwareScaleSearch /
    /// ExpertGranularityPlanner resolve it; the profile still carries
    /// its budget, residency, precision and training contracts so a
    /// resolved candidate has something to be validated against.</summary>
    public static Dictionary<string, object?> ProfileRecord(
        Dictionary<string, object?> tier)
    {
        string scale = (string)tier["scale_profile"]!;
        bool is20b = scale == "xc-20b-extreme-sparse";
        bool is1b = scale == "xc-1b-standard";
        var record = new Dictionary<string, object?>
        {
            ["format"] = "star-scale-profile/v1",
            // PENDING is a first-class state: the file is the contract
            // framework; a gate that needs numbers must fail closed,
            // not read nulls.
            ["parameter_state"] = "PENDING",
            ["scale_profile"] = scale,
            ["architecture_profile"] = "xc-fused-1",
            ["role"] = tier["role"],
            ["budgets"] = new Dictionary<string, object?>
            {
                ["total_params"] = tier["total_params_target"],
                ["active_params"] = tier["active_params_target"],
                ["active_ratio"] = tier["active_ratio_target"],
                ["common_core"] = tier["common_core_budget"],
                ["shared_expert"] = tier["shared_expert_budget"],
            },
            ["parameters_pending"] = new[]
            {
                "hidden", "layers", "heads", "kv_heads",
                "expert_intermediate", "expert_count",
                "shared_intermediate", "context_target", "precision",
            },
            ["parameter_resolution"] =
                "HardwareAwareScaleSearch -> ExpertGranularityPlanner " +
                "-> ActiveComputeGate -> ScalePromotionGate (§63)",
            ["shape"] = null,
            ["weight_version"] = null,
            ["runtime_version"] = null,
            ["bundle_hash"] = null,
            ["topology"] = new Dictionary<string, object?>
            {
                ["top_k"] = tier["top_k"],
                ["shared_experts"] = tier["shared_experts"],
                ["routed_expert_candidates"] =
                    tier["routed_expert_candidates"],
                ["routed_expert_primary"] =
                    tier["routed_expert_primary"],
                ["schedule"] = "delta3:full1 canonical period",
            },
        };
        // §33 precision policy — contract skeleton per component.
        record["precision_policy"] = new Dictionary<string, object?>
        {
            ["common_core"] = "BF16",
            ["shared_expert"] = "BF16",
            ["router"] = "FP32",
            ["hot_routed"] = "BF16/INT8 execution candidate",
            ["warm_routed"] = is20b ? "INT8 storage" : null,
            ["cold_routed"] = is20b ? "INT4 preferred storage" : null,
            ["kv"] = "INT8",
            ["delta_state"] = "BF16 candidate",
        };
        // §28-§32 residency policy skeleton.
        record["residency_policy"] = is20b
            ? new Dictionary<string, object?>
            {
                ["common_core"] = "GPU", ["shared_expert"] = "GPU",
                ["hot_routed"] = "GPU", ["warm_routed"] = "RAM",
                ["cold_routed"] = "NVME_COLD",
                ["gpu_resident_target"] = "~0.8B-2B equivalent",
                ["ram_resident_target"] = "~2B-5B equivalent",
                ["preload_cold"] = "forbidden (§32)",
            }
            : is1b
            ? new Dictionary<string, object?>
            {
                ["preferred"] = "all weights GPU-resident when " +
                                "hardware allows (§28)",
                ["force_offload"] = "forbidden",
            }
            : new Dictionary<string, object?>
            {
                ["preferred"] = "GPU-resident dev model",
            };
        // §34-§38 training policy skeleton.
        record["training_policy"] = is20b
            ? new Dictionary<string, object?>
            {
                ["trainable_params_target"] =
                    "100M-500M (preferred <=300M)",
                ["frozen_optimizer_state"] = false,
                ["dormant_expert_backward"] = false,
                ["full_parameter_rounds"] = "forbidden (§35)",
            }
            : new Dictionary<string, object?>
            {
                ["modes"] = new[]
                {
                    "sft", "parameter_efficient_sft",
                    "capability_training",
                },
                ["constraint"] = "ONE CAPABILITY AT A TIME (§34)",
            };
        return record;
    }

    /// <summary>Write the three canonical profile contracts plus an
    /// index under <paramref name="toolRoot"/>. Pure file emission —
    /// no weights, no state mutation.</summary>
    public static Dictionary<string, object?> SeedProfiles(
        string toolRoot)
    {
        string dir = Path.Combine(
            toolRoot,
            ProfileRelDir.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(dir);
        var written = new List<object?>();
        foreach (var tier in TierDicts())
        {
            string scale = (string)tier["scale_profile"]!;
            var rec = ProfileRecord(tier);
            string file = Path.Combine(dir, scale + ".json");
            File.WriteAllText(file, CanonicalJson.PrettyDict(rec));
            written.Add(scale);
        }
        var index = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-scale-profile-index/v1",
            ["profiles"] = written,
            ["parameter_state"] = "PENDING",
            ["one_core_rule"] = "xc-fused-1 everywhere; scale is a " +
                                "profile, never a second core (§1)",
        };
        File.WriteAllText(Path.Combine(dir, "index.json"),
            TransformerTrainingRepository.EmitJson(index));
        index["dir"] = ProfileRelDir;
        index["files"] = written
            .Select(s => (object?)(s + ".json")).ToList();
        return index;
    }

    // ---------------------------------------------------- identity --

    /// <summary>§2 model identity: five fields, all required; arch must
    /// stay xc-fused-1 and the scale name must come from the tier
    /// registry (or a probe scale).</summary>
    public static Dictionary<string, object?> Identity(JsonElement el)
    {
        string[] required =
        {
            "architecture_profile", "scale_profile", "weight_version",
            "runtime_version", "bundle_hash",
        };
        var missing = required
            .Where(k => !el.TryGetProperty(k, out var v) ||
                        v.ValueKind != JsonValueKind.String ||
                        (v.GetString() ?? "").Length == 0)
            .Cast<object?>().ToList();
        if (missing.Count > 0)
            throw new ExecutorError("SCALE_PROFILE_INVALID",
                "model identity missing: " +
                string.Join(",", missing.Cast<string>()));
        var names = TierDicts()
            .Select(t => (string)t["scale_profile"]!).ToList();
        string scale = el.GetProperty("scale_profile").GetString()!;
        bool known = names.Contains(scale);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = IdentityFormat,
            ["architecture_profile"] =
                el.GetProperty("architecture_profile").GetString(),
            ["scale_profile"] = scale,
            ["scale_profile_registered"] = known,
            ["weight_version"] =
                el.GetProperty("weight_version").GetString(),
            ["runtime_version"] =
                el.GetProperty("runtime_version").GetString(),
            ["bundle_hash"] =
                el.GetProperty("bundle_hash").GetString(),
            ["verdict"] = known ? "IDENTITY_VALID"
                                : "SCALE_PROFILE_UNREGISTERED",
        };
    }

    // ------------------------------------------------- active gate --

    /// <summary>§47 ActiveComputeGate: the gate refuses any record that
    /// hides behind total_params — active_params, active_flops,
    /// active_layers, active_experts and thinking_steps must all be
    /// present. When the record names a tier, the tier's active ceiling
    /// and ratio are enforced (§6-§8).</summary>
    public static Dictionary<string, object?> ActiveGate(JsonElement el)
    {
        string[] required =
        {
            "active_params", "active_flops", "active_layers",
            "active_experts", "thinking_steps",
        };
        var missing = required
            .Where(k => !el.TryGetProperty(k, out _))
            .Cast<object?>().ToList();
        if (missing.Count > 0)
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["format"] = ActiveGateFormat,
                ["verdict"] = "ACTIVE_PARAMS_MISSING",
                ["missing"] = missing,
                ["rule"] = "a scale that reports only total_params " +
                           "is rejected (§47)",
            };
        long active = Num(el, "active_params");
        long total = Num(el, "total_params", active);
        string scale = Str(el, "scale_profile");
        var violations = new List<object?>();
        string verdict = "ACTIVE_VALID";

        // tier-specific bounds
        if (scale == "xc-1b-standard")
        {
            if (active > 800_000_000)
            { violations.Add($"active {active} exceeds 1B ceiling 800M");
              verdict = "ACTIVE_CEILING_EXCEEDED"; }
            if (total < 900_000_000 || total > 1_100_000_000)
                violations.Add(
                    $"total {total} outside 0.9B-1.1B band (§6)");
        }
        else if (scale == "xc-20b-extreme-sparse")
        {
            if (active > 1_000_000_000)
            { violations.Add($"active {active} exceeds 20B ceiling " +
                             "1B"); verdict = "LOW_SPARSITY_FAILURE"; }
            if (total < 18_000_000_000L || total > 22_000_000_000L)
                violations.Add(
                    $"total {total} outside 18B-22B band (§7)");
            if (total > 0 && verdict == "ACTIVE_VALID" &&
                (double)active / total > 0.035)
            { violations.Add($"active ratio " +
                             $"{(double)active / total:P2} exceeds " +
                             "3.5% target (§8)");
              verdict = "LOW_SPARSITY_FAILURE"; }
        }
        else if (scale == "xc-300m-dev")
        {
            // dev tier: contract presence only.
        }
        else if (scale.Length > 0)
        {
            violations.Add($"unregistered scale_profile '{scale}'");
            verdict = "SCALE_TIER_INVALID";
        }

        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = ActiveGateFormat,
            ["scale_profile"] = scale,
            ["active_params"] = active,
            ["active_flops"] = Num(el, "active_flops"),
            ["active_layers"] = Num(el, "active_layers"),
            ["active_experts"] = Num(el, "active_experts"),
            ["thinking_steps"] = Num(el, "thinking_steps"),
            ["total_params"] = total,
            ["active_ratio"] = total > 0
                ? Math.Round((double)active / total, 4) : null,
            ["violations"] = violations,
            ["verdict"] = verdict,
        };
    }

    // ----------------------------------------------- tier validate --

    /// <summary>Validate a candidate profile record against the tier it
    /// declares — layer budgets, expert whitelist, top_k, and the
    /// active-gate metrics all checked together.</summary>
    public static Dictionary<string, object?> ValidateTier(
        JsonElement el)
    {
        string scale = Str(el, "scale_profile");
        var tier = TierDicts().FirstOrDefault(
            t => (string)t["scale_profile"]! == scale);
        if (tier == null)
            throw new ExecutorError("SCALE_TIER_INVALID",
                $"scale_profile '{scale}' is not a registered tier");
        var violations = new List<object?>();
        long total = Num(el, "total_params");
        long active = Num(el, "active_params");
        long common = Num(el, "common_core_params");
        long shared = Num(el, "shared_expert_params");
        long ec = Num(el, "expert_count");
        long topK = Num(el, "top_k", 2);

        if (topK != 2)
            violations.Add($"top_k {topK} != 2 — Top-2 is permanent " +
                           "primary (§19)");
        var tb = (Dictionary<string, object?>)
            tier["total_params_target"]!;
        if (total < (long)tb["min"]! || total > (long)tb["max"]!)
            violations.Add($"total {total} outside tier band");
        var cb = (Dictionary<string, object?>)
            tier["common_core_budget"]!;
        if (common > (long)cb["hard_max"]!)
            violations.Add($"common_core {common} exceeds tier " +
                           $"hard budget {cb["hard_max"]} (§11-13)");
        if (common > 600_000_000 && scale == "xc-20b-extreme-sparse")
            violations.Add("common_core >600M on 20B — architecture " +
                           "efficiency must be re-evaluated (§13)");
        var sb = (Dictionary<string, object?>)
            tier["shared_expert_budget"]!;
        if (shared > 0 && shared > (long)sb["max"]!)
            violations.Add($"shared_expert {shared} exceeds tier " +
                           $"budget {sb["max"]} (§16)");
        var allowed = ((List<object?>)
            tier["routed_expert_candidates"]!)
            .Cast<long>().ToList();
        if (ec > 0 && !allowed.Contains(ec))
            violations.Add($"expert_count {ec} not in tier " +
                           $"candidates [{string.Join(",", allowed)}]");
        var gate = ActiveGate(el);
        if (gate["ok"] is bool g && !g)
            violations.AddRange(
                ((List<object?>)gate["violations"]!)
                    .Cast<object?>());
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = TierCheckFormat,
            ["scale_profile"] = scale,
            ["violations"] = violations,
            ["active_gate"] = gate["verdict"],
            ["verdict"] = violations.Count == 0
                ? "TIER_VALID" : "SCALE_TIER_INVALID",
        };
    }

    // --------------------------------------------------- residency --

    /// <summary>§28-§32 residency contract. 1B keeps everything hot if
    /// the hardware allows; 20B splits GPU (common+shared+hot routed) /
    /// RAM (warm routed + staging) / NVMe (cold routed, INT4/INT8).</summary>
    public static Dictionary<string, object?> ResidencyPlan(
        JsonElement el)
    {
        string scale = Str(el, "scale_profile");
        var violations = new List<object?>();
        if (scale == "xc-20b-extreme-sparse")
        {
            foreach (var (layer, band) in new[]
                     {
                         ("common_core", Str(el, "common_residency")),
                         ("shared_expert", Str(el, "shared_residency")),
                         ("hot_routed", Str(el, "hot_routed_residency")),
                     })
                if (band != "GPU")
                    violations.Add($"{layer} residency '{band}' — " +
                                   "20B requires GPU (§29)");
            string cold = Str(el, "cold_routed_residency", "NVME_COLD");
            if (cold != "NVME_COLD")
                violations.Add("cold routed must be NVME_COLD (§29)");
            if (Str(el, "cold_precision", "INT4") is var cp &&
                cp != "INT4" && cp != "INT8")
                violations.Add($"cold precision '{cp}' — §33 allows " +
                               "INT4 preferred / INT8");
            long gpuRes = Num(el, "gpu_resident_params");
            if (gpuRes > 4_000_000_000L)
                violations.Add($"gpu_resident {gpuRes} approaches " +
                               "total — 20B GPU residency targets " +
                               "~0.8B-2B (§30)");
            if (el.TryGetProperty("ram_holds_full_weights", out var rw)
                && rw.ValueKind == JsonValueKind.True)
                violations.Add("RAM must not hold the full 20B " +
                               "(§31)");
            if (el.TryGetProperty("preload_cold_to_ram", out var pc) &&
                pc.ValueKind == JsonValueKind.True)
                violations.Add("cold preload to RAM at startup is " +
                               "forbidden (§32)");
        }
        else if (scale == "xc-1b-standard")
        {
            // §28: offload is permitted but never forced.
            if (el.TryGetProperty("force_offload", out var fo) &&
                fo.ValueKind == JsonValueKind.True)
                violations.Add("1B prefers full GPU residency — " +
                               "never offload for its own sake (§28)");
        }
        else if (scale.Length == 0)
            throw new ExecutorError("SCALE_TIER_INVALID",
                "scale_profile required");
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = ResidencyFormat,
            ["scale_profile"] = scale,
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "RESIDENCY_VALID" : "SCALE_TIER_INVALID",
        };
    }

    // -------------------------------------------------- trainable ---

    /// <summary>§35-§38 sparse-training budget: 20B capability rounds
    /// train 100M-500M (preferred <=300M) params; frozen params carry
    /// zero optimizer state; a routed expert not activated this step
    /// gets no backward and no optimizer update.</summary>
    public static Dictionary<string, object?> TrainableBudget(
        JsonElement el)
    {
        string scale = Str(el, "scale_profile");
        long trainable = Num(el, "trainable_params");
        long total = Num(el, "total_params");
        var violations = new List<object?>();
        if (scale == "xc-20b-extreme-sparse")
        {
            if (trainable > 500_000_000)
                violations.Add($"trainable {trainable} exceeds the " +
                               "500M bound (§36)");
            if (trainable > 300_000_000)
                violations.Add($"trainable {trainable} exceeds the " +
                               "300M preferred bound (§36)");
            if (trainable >= total)
                violations.Add("20B must never run full-parameter " +
                               "update rounds (§35)");
            if (el.TryGetProperty("frozen_optimizer_state", out var fo)
                && fo.ValueKind == JsonValueKind.True)
                violations.Add("frozen params must own zero Adam " +
                               "moments (§37)");
            if (el.TryGetProperty("dormant_expert_backward", out var db)
                && db.ValueKind == JsonValueKind.True)
                violations.Add("dormant routed experts take no " +
                               "backward/update (§38)");
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = TrainableFormat,
            ["scale_profile"] = scale,
            ["trainable_params"] = trainable,
            ["trainable_fraction"] = total > 0
                ? Math.Round((double)trainable / total, 6) : null,
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "TRAINABLE_VALID" : "SCALE_TIER_INVALID",
        };
    }

    // ---------------------------------------------------- thinking --

    /// <summary>§48-§51 sparse-thinking contract: levels OFF..MAX with
    /// AUTO default; AUTO dispatches on system-1 confidence, task
    /// complexity, uncertainty, tool requirement, latency and resource
    /// budgets; System-1 answers boolean/choice/ordinal directly —
    /// decode is never started for those (§51).</summary>
    public static Dictionary<string, object?> ThinkingContract()
        => new()
        {
            ["ok"] = true, ["format"] = ThinkingFormat,
            ["levels"] = ThinkingLevels,
            ["default"] = "AUTO",
            ["auto_inputs"] = new[]
            {
                "system1_confidence", "task_complexity",
                "uncertainty", "tool_requirement",
                "latency_budget", "resource_budget",
            },
            ["system1_direct_types"] = new[]
                { "BOOLEAN", "CHOICE", "ORDINAL" },
            ["rule"] = "thinking is part of active compute — simple " +
                       "questions spend zero thinking steps (§48-51)",
        };

    // ---------------------------------------------------- helpers ---

    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) && v.ValueKind ==
            JsonValueKind.Number && v.TryGetInt64(out long n) ? n : d;
    private static string Str(JsonElement r, string k, string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;
}
