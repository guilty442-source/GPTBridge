// Curriculum.cs — CapabilityAnnealingCurriculum (efficiency/scale
// directive §14-§23, §39-§40). OLMo-2-style staged training where data
// quality ANNEALS upward per stage — never a bigger blob late. Stages:
//
//   FOUNDATION           §16 broad/clean/dedup/provenanced — a NEW
//                        scale generation's general capability; the
//                        current 300M never re-runs it.
//   REFINEMENT           §17 higher quality/density/difficulty; generic
//                        low-quality data fraction shrinks.
//   CAPABILITY_RECOVERY  §18 failure-driven, verified, targeted rows —
//                        the live stage (instruction/context lanes).
//   POST_TRAINING        §19 SFT -> DPO -> RL, strictly in order; RL
//                        stays frozen until released.
//
//   StagePolicy        per-stage data contract (§16-20).
//   ValidateStage      a planned run's legality — wrong stage /
//                        wrong kind / RL while frozen => denied.
//   Mixture            §21 star-training-mixture/v1 — every round's
//                        data mix is versioned and replayable.
//   ReproRecord        §22 full reproducibility record — a bare final
//                        checkpoint is not a training record.
//   DataOrderProbe     §23 ordering-sensitivity pilot verdict.
//
// Emits: star-curriculum-stage/v1, star-training-mixture/v1,
// star-training-repro/v1, star-data-order-probe/v1.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class Curriculum
{
    public const string StageFormat = "star-curriculum-stage/v1";
    public const string MixtureFormat = "star-training-mixture/v1";
    public const string ReproFormat = "star-training-repro/v1";
    public const string OrderProbeFormat = "star-data-order-probe/v1";

    public static readonly string[] Stages =
    {
        "FOUNDATION", "REFINEMENT", "CAPABILITY_RECOVERY",
        "POST_TRAINING",
    };
    public static readonly string[] PostTrainingOrder =
        { "SFT", "DPO", "RL" };
    public static readonly string[] QualityTiers =
        { "T0_raw", "T1_clean", "T2_verified", "T3_failure_driven" };

    private static string Str(JsonElement r, string k, string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;
    private static double DNum(JsonElement r, string k, double d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;

    // ------------------------------------------------- stage policy --

    /// <summary>§15-20 the data contract each stage must satisfy.</summary>
    public static Dictionary<string, object?> StagePolicy(string stage)
    {
        Dictionary<string, object?> p = stage switch
        {
            "FOUNDATION" => new()
            {
                ["data_kinds"] = new[] { "broad", "clean",
                                       "deduplicated", "provenanced" },
                ["min_quality_tier"] = "T1_clean",
                ["max_low_quality_fraction"] = 0.20,
                ["requires_failure_relevance"] = false,
                ["requires_single_capability"] = false,
            },
            "REFINEMENT" => new()
            {
                ["data_kinds"] = new[] { "high_quality",
                                       "high_information_density",
                                       "hard", "professional" },
                ["min_quality_tier"] = "T2_verified",
                ["max_low_quality_fraction"] = 0.05,
                ["requires_failure_relevance"] = false,
                ["requires_single_capability"] = false,
            },
            "CAPABILITY_RECOVERY" => new()
            {
                ["data_kinds"] = new[] { "failure_driven", "verified",
                                       "high_value", "targeted" },
                ["min_quality_tier"] = "T3_failure_driven",
                ["max_low_quality_fraction"] = 0.0,
                ["requires_failure_relevance"] = true,
                ["requires_single_capability"] = true,   // §40
                ["sequence_policy"] = "short, pretokenized, packed",
            },
            "POST_TRAINING" => new()
            {
                ["data_kinds"] = new[] { "verified", "preference_pairs",
                                       "reward_verified" },
                ["min_quality_tier"] = "T2_verified",
                ["phase_order"] = PostTrainingOrder,
                ["rl_frozen_until_released"] = true,     // §19
                ["requires_single_capability"] = false,
            },
            _ => throw new ExecutorError("TRAINING_STAGE_INVALID",
                $"unknown stage '{stage}'"),
        };
        p["stage"] = stage;
        p["format"] = StageFormat;
        p["order"] = Array.IndexOf(Stages, stage);
        return p;
    }

    /// <summary>Validate a planned run against the stage contract —
    /// CAPABILITY_RECOVERY additionally demands the single-capability
    /// freeze posture; POST_TRAINING enforces phase order and the RL
    /// freeze; FOUNDATION on an existing generation is refused unless
    /// `new_scale_generation` is declared (§16).</summary>
    public static Dictionary<string, object?> ValidateStage(
        JsonElement el)
    {
        string stage = Str(el, "stage");
        var policy = StagePolicy(stage);        // throws on unknown
        var violations = new List<object?>();

        if (stage == "FOUNDATION" &&
            !el.TryGetProperty("new_scale_generation", out var ng))
            violations.Add("FOUNDATION requires new_scale_generation " +
                           "— the current generation never re-runs " +
                           "foundation (§16)");
        if (stage == "CAPABILITY_RECOVERY")
        {
            string cap = Str(el, "capability");
            if (cap.Length == 0 || el.TryGetProperty("capabilities",
                    out var many) &&
                many.ValueKind == JsonValueKind.Array &&
                many.GetArrayLength() > 1)
                violations.Add(
                    "CAPABILITY_RECOVERY admits exactly one " +
                    "capability (§40 — ONE CAPABILITY AT A TIME)");
            string mode = Str(el, "capability_training_mode");
            if (mode.Length > 0 && mode != "SINGLE_CAPABILITY_RECOVERY")
                violations.Add(
                    $"mode '{mode}' is not SINGLE_CAPABILITY_RECOVERY");
        }
        if (stage == "POST_TRAINING")
        {
            string phase = Str(el, "phase");
            if (!PostTrainingOrder.Contains(phase))
                violations.Add(
                    $"phase '{phase}' not in SFT|DPO|RL order (§19)");
            if (phase == "RL" &&
                !(el.TryGetProperty("rl_released", out var rr) &&
                  rr.ValueKind == JsonValueKind.True))
                violations.Add("RL stays FROZEN until released (§19)");
            string done = Str(el, "completed_phases");
            int want = Array.IndexOf(PostTrainingOrder, phase);
            if (phase == "DPO" && !done.Contains("SFT"))
                violations.Add("DPO before SFT violates phase order");
            if (phase == "RL" &&
                !(done.Contains("SFT") && done.Contains("DPO")))
                violations.Add("RL requires completed SFT+DPO (§19)");
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = StageFormat,
            ["stage"] = stage,
            ["policy"] = policy,
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "STAGE_VALID" : "TRAINING_STAGE_INVALID",
        };
    }

    // -------------------------------------------------- mixture -----

    /// <summary>§21 star-training-mixture/v1: every mixture round is a
    /// versioned record — each entry needs dataset, weight, capability,
    /// quality_tier, stage, hash. Replayable by construction.</summary>
    public static Dictionary<string, object?> Mixture(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("entries", out var entries) ||
            entries.ValueKind != JsonValueKind.Array)
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                "mixture requires an entries[] array");
        var problems = new List<object?>();
        var normalized = new List<object?>();
        double wsum = 0;
        int i = 0;
        foreach (var e in entries.EnumerateArray())
        {
            string need(string k) =>
                e.ValueKind == JsonValueKind.Object &&
                e.TryGetProperty(k, out var v) &&
                v.ValueKind == JsonValueKind.String
                    ? v.GetString() ?? "" : "";
            string ds = need("dataset"), cap = need("capability"),
                   qt = need("quality_tier"), st = need("stage"),
                   h = need("hash");
            double w = e.ValueKind == JsonValueKind.Object
                ? DNum(e, "weight") : 0;
            foreach (var (k, v) in new[]
                     { ("dataset", ds), ("quality_tier", qt),
                       ("stage", st), ("hash", h) })
                if (v.Length == 0)
                    problems.Add($"entries[{i}] missing {k}");
            if (qt.Length > 0 && !QualityTiers.Contains(qt))
                problems.Add($"entries[{i}] bad quality_tier '{qt}'");
            if (st.Length > 0 && !Stages.Contains(st))
                problems.Add($"entries[{i}] bad stage '{st}'");
            if (w <= 0) problems.Add($"entries[{i}] weight must be >0");
            wsum += w;
            normalized.Add(new Dictionary<string, object?>
            {
                ["dataset"] = ds, ["weight"] = w,
                ["capability"] = cap, ["quality_tier"] = qt,
                ["stage"] = st, ["hash"] = h,
            });
            i++;
        }
        string mixSha = TransformerTrainingRepository.Sha256Text(
            string.Concat(normalized.Select(n =>
                n is Dictionary<string, object?> d
                    ? $"{d["dataset"]}:{d["weight"]}:{d["hash"]};"
                    : "")));
        return new Dictionary<string, object?>
        {
            ["ok"] = problems.Count == 0,
            ["format"] = MixtureFormat,
            ["mixture_sha256"] = mixSha,
            ["entries"] = normalized,
            ["total_weight"] = Math.Round(wsum, 6),
            ["violations"] = problems,
            ["verdict"] = problems.Count == 0
                ? "MIXTURE_VALID" : "TRAINING_STAGE_INVALID",
        };
    }

    // --------------------------------------------------- repro ------

    /// <summary>§22 OLMo-style reproducibility: a bare final checkpoint
    /// is not a record. Every required field must be present.</summary>
    public static readonly string[] ReproRequired =
    {
        "model_source", "data_order_seed", "dataset_sha256",
        "optimizer", "lr_schedule", "precision", "hardware_profile",
        "runtime_version", "evaluation_version",
    };
    public static Dictionary<string, object?> ReproRecord(
        JsonElement el)
    {
        var missing = ReproRequired
            .Where(k => !el.TryGetProperty(k, out var v) ||
                        v.ValueKind == JsonValueKind.Null ||
                        (v.ValueKind == JsonValueKind.String &&
                         (v.GetString() ?? "").Length == 0))
            .Cast<object?>().ToList();
        var rec = new Dictionary<string, object?>
        {
            ["ok"] = missing.Count == 0,
            ["format"] = ReproFormat,
            ["missing_fields"] = missing,
            ["verdict"] = missing.Count == 0
                ? "REPRODUCIBLE" : "TRAINING_STAGE_INVALID",
        };
        foreach (var k in ReproRequired)
            if (el.TryGetProperty(k, out var v))
                rec[k] = v.ValueKind == JsonValueKind.String
                    ? v.GetString() : v.ToString();
        return rec;
    }

    // ------------------------------------------------ data-order ----

    /// <summary>§23 data-order-probe: identical small dataset under N
    /// deterministic orderings; variance across pilots decides whether
    /// the trainer is ordering-sensitive. Input: pilots[] each with
    /// ordering_id/seed/final_loss/capability_score/router_entropy.</summary>
    public static Dictionary<string, object?> DataOrderProbe(
        JsonElement el)
    {
        if (!el.TryGetProperty("pilots", out var pilots) ||
            pilots.ValueKind != JsonValueKind.Array)
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                "data-order probe requires pilots[]");
        double Var(IEnumerable<double> xs)
        {
            var a = xs.ToArray();
            if (a.Length == 0) return 0;
            double m = a.Average();
            return a.Sum(x => (x - m) * (x - m)) / a.Length;
        }
        var losses = new List<double>();
        var caps = new List<double>();
        var routers = new List<double>();
        var rows = new List<object?>();
        foreach (var p in pilots.EnumerateArray())
        {
            if (p.ValueKind != JsonValueKind.Object) continue;
            losses.Add(DNum(p, "final_loss"));
            caps.Add(DNum(p, "capability_score"));
            routers.Add(DNum(p, "router_entropy"));
            rows.Add(new Dictionary<string, object?>
            {
                ["ordering_id"] = Str(p, "ordering_id"),
                ["seed"] = DNum(p, "seed"),
                ["final_loss"] = DNum(p, "final_loss"),
                ["capability_score"] = DNum(p, "capability_score"),
                ["router_entropy"] = DNum(p, "router_entropy"),
            });
        }
        double lv = Var(losses), cv = Var(caps), rv = Var(routers);
        double lossTol = DNum(el, "loss_var_tol", 0.01);
        double capTol = DNum(el, "capability_var_tol", 0.0025);
        bool sensitive = lv > lossTol || cv > capTol;
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = OrderProbeFormat,
            ["pilots"] = rows,
            ["loss_variance"] = Math.Round(lv, 8),
            ["capability_variance"] = Math.Round(cv, 8),
            ["router_variance"] = Math.Round(rv, 8),
            ["tolerances"] = new Dictionary<string, object?>
            {
                ["loss_var_tol"] = lossTol,
                ["capability_var_tol"] = capTol,
            },
            ["verdict"] = sensitive
                ? "ORDER_SENSITIVE" : "ORDER_STABLE",
            ["note"] = sensitive
                ? "curriculum must pin data_order_seed and record it " +
                  "in every star-training-repro/v1 (§22-23)"
                : "ordering variance within tolerance",
        };
    }
}
