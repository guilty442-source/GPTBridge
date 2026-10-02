// MaturityStandard.cs — XC-1B Mature Standard (maturity directive
// §1-§40). Contracts only — no weights, no evals executed here; this
// plane owns the *rules* every candidate, baseline and promotion must
// satisfy.
//
//   Stages              §3 — TRAINING -> DISTILLED -> COMPRESSED ->
//                       QUANTIZED -> CERTIFIED -> STANDARD; only the
//                       last is mature (§40).
//   BaselineRegistry    §29/§30 — star-maturity-baseline/v1; one
//                       registry, no thresholds scattered in code
//                       (per-capability metric + floor + dataset/eval
//                       version + runtime mode + precision + hw).
//   FloorGate           §5-§8/§28 — every capability >= floor; no
//                       averaging, no compensation; BASE_MODEL layer
//                       must pass unaided — runtime/RAG/tool can only
//                       add.
//   ThinkingGate        §9 — THINK_OFF must meet floor first;
//                       THINK_AUTO cannot rescue a base-model gap.
//   RetentionGate       §10-§12 — per-capability retention through
//                       distillation / compression / quantization;
//                       mean retention never hides a per-item loss.
//   GoldenUsability     §31-§33 — eval-only suite (training leakage
//                       denied), stability across seeds/runs/context.
//   Certification       §35 — nine certs; all required.
//   PromotionDecision   §13 — the seven-condition AND.
//   ReplacementGate     §27 — a new standard never regresses vs the
//                       previous mature baseline.
//   ThreeHundredMGate   §37 — what 300M must prove before a 1B
//                       candidate may be cut.
//
// §36 fail codes: MATURITY_CAPABILITY_FLOOR_FAILED,
// POST_DISTILLATION_REGRESSION, POST_COMPRESSION_REGRESSION,
// POST_QUANTIZATION_REGRESSION, GOLDEN_USABILITY_FAILED,
// BASE_MODEL_CAPABILITY_FAILED, STANDARD_REPLACEMENT_REGRESSION.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class MaturityStandard
{
    public const string BaselineFormat = "star-maturity-baseline/v1";
    public const string CertFormat = "star-xc1b-certification/v1";
    public const string GoldenFormat = "star-golden-usability/v1";

    /// <summary>§3 lifecycle stages — only STANDARD is mature.</summary>
    public static readonly string[] Stages =
    {
        "XC-1B-TRAINING", "XC-1B-DISTILLED", "XC-1B-COMPRESSED",
        "XC-1B-QUANTIZED", "XC-1B-CERTIFIED", "XC-1B-STANDARD",
    };

    /// <summary>§5 capability dimensions of XC1B_MATURITY_BASELINE —
    /// maturation-closure §82/§122: the standard references the
    /// CapabilityRegistry directly; it never maintains a second
    /// capability list. Non-capability axes (system1 runtime,
    /// thinking-mode gating) are evaluated through their own
    /// contracts, not listed as capabilities here.</summary>
    public static readonly string[] Capabilities =
        CapabilityRegistry.Canonical
            .Select(d => d.CapabilityId).ToArray();

    /// <summary>§8 evaluation layers — the BASE_MODEL floor is the
    /// maturity claim; augmentation may only add.</summary>
    public static readonly string[] EvalLayers =
        { "BASE_MODEL", "RUNTIME_AUGMENTED", "SERVICE" };

    /// <summary>§9 thinking modes are gated separately.</summary>
    public static readonly string[] ThinkModes = { "THINK_OFF", "THINK_AUTO" };

    /// <summary>§35 certification legs.</summary>
    public static readonly string[] Certs =
    {
        "CapabilityCert", "DistillationCert", "CompressionCert",
        "QuantizationCert", "RuntimeCert", "MemoryCert", "CudaCert",
        "LifecycleCert", "ProvenanceCert",
    };

    /// <summary>§18 1B is a class, not an exact count.</summary>
    public const long StandardClassMin = 800_000_000L;
    public const long StandardClassMax = 1_200_000_000L;

    // ---------------------------------------------- baseline --------

    /// <summary>§29/§30 baseline record: every capability carries its
    /// own metric + minimum + dataset/eval versions + required runtime
    /// mode + precision + hardware — "feels usable" is not a floor.</summary>
    public static Dictionary<string, object?> BaselineEntry(
        string capability, string metric, double minimum,
        string datasetVersion, string evalVersion,
        string runtimeMode, string precision, string hardware)
        => new()
        {
            ["capability"] = capability, ["metric"] = metric,
            ["minimum_standard"] = minimum,
            ["dataset_version"] = datasetVersion,
            ["eval_version"] = evalVersion,
            ["required_runtime_mode"] = runtimeMode,
            ["precision"] = precision,
            ["hardware_conditions"] = hardware,
        };

    /// <summary>Registered baselines — v1 is the canonical floor every
    /// promotion reads (§30).</summary>
    public static Dictionary<string, object?> BaselineRegistry(
        string toolRoot)
    {
        string dir = Path.Combine(
            toolRoot,
            "xingcheng/runtime/maturity-baselines"
                .Replace('/', Path.DirectorySeparatorChar));
        var baselines = new List<object?>();
        if (Directory.Exists(dir))
            foreach (string f in Directory.GetFiles(dir, "*.json"))
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(f));
                baselines.Add(doc.RootElement.TryGetProperty(
                                  "baseline_id", out var b)
                                  ? b.GetString() :
                                  Path.GetFileNameWithoutExtension(f));
            }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-maturity-baseline-registry/v1",
            ["registry_dir"] = "xingcheng/runtime/maturity-baselines",
            ["registered"] = baselines,
            ["canonical"] = "XC1B_STANDARD_V1",
            ["capabilities"] = Capabilities.Cast<object?>().ToList(),
            ["rule"] = "training, eval, promotion and lifecycle read "
                       + "the same registry — thresholds never live "
                       + "inside code paths (§30)",
        };
    }

    /// <summary>Emit the XC1B_STANDARD_V1 baseline contract file —
    /// floors are explicit per capability; PENDING where the suite has
    /// not yet produced a measured floor (fail-closed reads).</summary>
    public static Dictionary<string, object?> SeedBaseline(
        string toolRoot)
    {
        string dir = Path.Combine(
            toolRoot,
            "xingcheng/runtime/maturity-baselines"
                .Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(dir);
        var floors = CapabilityRegistry.Canonical.Select(d =>
            (object?)BaselineEntry(
                d.CapabilityId, "pass_rate", -1.0,
                "PENDING", "PENDING",
                d.CapabilityClass == "RUNTIME_AUGMENTED"
                    ? "RUNTIME_AUGMENTED"
                    : d.CapabilityClass == "SERVICE_AUGMENTED"
                        ? "SERVICE" : "BASE_MODEL",
                "production_mixed", "reference_gpu"))
            .ToList();
        var rec = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = BaselineFormat,
            ["baseline_id"] = "XC1B_STANDARD_V1",
            ["scale_class"] = new Dictionary<string, object?>
            {
                ["min"] = StandardClassMin,
                ["max"] = StandardClassMax,
                ["rule"] = "1B class = capability + product level, "
                           + "not an exact parameter count (§17/§18)",
            },
            ["floors"] = floors,
            ["pending"] = true,
            ["no_compensation"] =
                "high scores never offset a failing capability (§7)",
        };
        string file = Path.Combine(dir, "XC1B_STANDARD_V1.json");
        File.WriteAllText(file, CanonicalJson.PrettyDict(rec));
        rec["written"] = "xingcheng/runtime/maturity-baselines/" +
                         "XC1B_STANDARD_V1.json";
        return rec;
    }

    // ------------------------------------------------ floor gate ---

    /// <summary>§6/§7/§28 per-capability floor — one failure fails the
    /// whole candidate; no averaging, no cross-capability
    /// compensation.</summary>
    public static Dictionary<string, object?> FloorGate(
        IReadOnlyDictionary<string, double> scores,
        IReadOnlyDictionary<string, double> floors,
        string layer)
    {
        var failures = new List<object?>();
        foreach (var kv in floors)
        {
            double s = scores.TryGetValue(kv.Key, out var v) ? v : 0.0;
            if (s + 1e-9 < kv.Value)
                failures.Add(new Dictionary<string, object?>
                {
                    ["capability"] = kv.Key,
                    ["score"] = s, ["floor"] = kv.Value,
                });
        }
        if (failures.Count > 0)
        {
            string code = layer == "BASE_MODEL"
                ? "BASE_MODEL_CAPABILITY_FAILED"
                : "MATURITY_CAPABILITY_FLOOR_FAILED";
            throw new ExecutorError(code,
                $"capability floor failed at {layer}: " +
                string.Join(",", failures
                    .Cast<Dictionary<string, object?>>()
                    .Select(f => (string)f["capability"]!)));
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = "star-capability-floor/v1",
            ["layer"] = layer,
            ["capabilities_checked"] = floors.Count,
            ["rule"] = "ALL >= minimum_standard — average is never "
                       + "the criterion (§6)",
        };
    }

    /// <summary>§8/§9: the maturity verdict — base layer must pass the
    /// floor unaided *and* THINK_OFF must independently meet it;
    /// augmented layers are reported but can never substitute.</summary>
    public static Dictionary<string, object?> MaturityVerdict(
        IReadOnlyDictionary<string, double> baseOffScores,
        IReadOnlyDictionary<string, double> baseAutoScores,
        IReadOnlyDictionary<string, double> floors)
    {
        var rep = FloorGate(baseOffScores, floors, "BASE_MODEL");
        // §9: AUTO is informational unless it ALSO meets the floor on
        // every capability (it can exceed, never rescue).
        var augmented = new List<object?>();
        foreach (var kv in baseAutoScores)
        {
            double off = baseOffScores.TryGetValue(kv.Key, out var v)
                ? v : 0.0;
            if (kv.Value - off > 1e-9)
                augmented.Add(kv.Key);
        }
        rep["thinking_augmented"] = augmented;
        rep["verdict"] = "MATURE";
        return rep;
    }

    // ------------------------------------------------- retention ---

    /// <summary>§10-§12 retention gate: every capability's post score
    /// must keep >= threshold of its pre score. Phase selects the fail
    /// code: distill | compress | quantize.</summary>
    public static Dictionary<string, object?> RetentionGate(
        string phase,
        IReadOnlyDictionary<string, double> pre,
        IReadOnlyDictionary<string, double> post,
        IReadOnlyDictionary<string, double> minRetention)
    {
        string code = phase switch
        {
            "distill" => "POST_DISTILLATION_REGRESSION",
            "compress" => "POST_COMPRESSION_REGRESSION",
            "quantize" => "POST_QUANTIZATION_REGRESSION",
            _ => "POST_COMPRESSION_REGRESSION",
        };
        var regressions = new List<object?>();
        foreach (var kv in minRetention)
        {
            double preS = pre.TryGetValue(kv.Key, out var pv)
                ? pv : 0.0;
            double postS = post.TryGetValue(kv.Key, out var sv)
                ? sv : 0.0;
            double retention = preS > 0 ? postS / preS : 0.0;
            if (retention + 1e-9 < kv.Value)
                regressions.Add(new Dictionary<string, object?>
                {
                    ["capability"] = kv.Key,
                    ["pre"] = preS, ["post"] = postS,
                    ["retention"] = retention,
                    ["required"] = kv.Value,
                });
        }
        if (regressions.Count > 0)
            throw new ExecutorError(code,
                $"{phase} retention regressions: " +
                string.Join(",", regressions
                    .Cast<Dictionary<string, object?>>()
                    .Select(r => (string)r["capability"]!)));
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-retention-gate/v1",
            ["phase"] = phase,
            ["capabilities_checked"] = minRetention.Count,
        };
    }

    // -------------------------------------------------- golden -----

    /// <summary>§31-§33 Golden Usability Suite — evaluation-only
    /// (leakage denied) and stable: every task must pass min_runs
    /// across seeds/context variants, not occasionally succeed.</summary>
    public static Dictionary<string, object?> GoldenGate(
        JsonElement suite)
    {
        if (!suite.TryGetProperty("tasks", out var tasks) ||
            tasks.ValueKind != JsonValueKind.Array)
            throw new ExecutorError("GOLDEN_USABILITY_FAILED",
                "golden suite has no tasks");
        var failures = new List<object?>();
        foreach (var t in tasks.EnumerateArray())
        {
            string name = t.TryGetProperty("name", out var n)
                ? n.GetString() ?? "?" : "?";
            bool trained = t.TryGetProperty("trained_on", out var tr) &&
                           tr.ValueKind == JsonValueKind.True;
            if (trained)
            {
                failures.Add(
                    $"{name}:trained_on_leakage_denied(§32)");
                continue;
            }
            long runs = t.TryGetProperty("runs", out var r) &&
                        r.ValueKind == JsonValueKind.Number
                            ? r.GetInt64() : 0;
            long passed = t.TryGetProperty("runs_passed", out var p) &&
                          p.ValueKind == JsonValueKind.Number
                              ? p.GetInt64() : 0;
            if (runs < 3 || passed < runs)
                failures.Add($"{name}:{passed}/{runs} "
                             + "(stability required, §33)");
        }
        if (failures.Count > 0)
            throw new ExecutorError("GOLDEN_USABILITY_FAILED",
                string.Join(";", failures.Cast<string>()));
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = GoldenFormat,
            ["tasks"] = tasks.GetArrayLength(),
            ["stability_rule"] = "multiple seeds + decode runs + "
                                 + "context variation (§33)",
        };
    }

    // -------------------------------------------- certification -----

    /// <summary>§35 nine-cert bundle — every cert must be present and
    /// passing; a missing cert is a failure, not a skip.</summary>
    public static Dictionary<string, object?> Certification(
        IReadOnlyDictionary<string, bool> certs)
    {
        var missing = Certs
            .Where(c => !certs.TryGetValue(c, out bool ok) || !ok)
            .Cast<object?>().ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = missing.Count == 0,
            ["format"] = CertFormat,
            ["required"] = Certs.Cast<object?>().ToList(),
            ["missing_or_failed"] = missing,
            ["verdict"] = missing.Count == 0
                ? "XC1B_CERTIFIED" : "CERTIFICATION_INCOMPLETE",
        };
    }

    /// <summary>§13 the promotion AND — every condition must hold; the
    /// final artifact (post-quantize) is the only evidence that counts
    /// (§4/§21).</summary>
    public static Dictionary<string, object?> PromotionDecision(
        IReadOnlyDictionary<string, bool> conditions)
    {
        string[] required =
        {
            "ALL_CAPABILITY_FLOORS_PASS", "DISTILLATION_PASS",
            "COMPRESSION_PASS", "QUANTIZATION_PASS", "RUNTIME_PASS",
            "RESOURCE_PASS", "REGRESSION_PASS",
        };
        var unmet = required
            .Where(c => !conditions.TryGetValue(c, out bool ok) || !ok)
            .Cast<object?>().ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = unmet.Count == 0,
            ["format"] = "star-maturity-promotion/v1",
            ["unmet"] = unmet,
            ["evidence"] = "final quantized production artifact only "
                           + "(§4/§21)",
            ["verdict"] = unmet.Count == 0 ? "PROMOTE" : "HOLD",
        };
    }

    /// <summary>§27 replacement gate: a new standard must meet or
    /// exceed the previous mature baseline on every mature capability —
    /// an average gain never excuses a single regression (§28).</summary>
    public static Dictionary<string, object?> ReplacementGate(
        IReadOnlyDictionary<string, double> previousBaseline,
        IReadOnlyDictionary<string, double> candidate,
        double tolerance = 0.0)
    {
        var regressions = new List<object?>();
        foreach (var kv in previousBaseline)
        {
            double c = candidate.TryGetValue(kv.Key, out var v)
                ? v : 0.0;
            if (c + tolerance + 1e-9 < kv.Value)
                regressions.Add(new Dictionary<string, object?>
                {
                    ["capability"] = kv.Key,
                    ["previous"] = kv.Value, ["candidate"] = c,
                });
        }
        if (regressions.Count > 0)
            throw new ExecutorError("STANDARD_REPLACEMENT_REGRESSION",
                "candidate regresses vs mature baseline: " +
                string.Join(",", regressions
                    .Cast<Dictionary<string, object?>>()
                    .Select(r => (string)r["capability"]!)));
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-standard-replacement/v1",
            ["capabilities_checked"] = previousBaseline.Count,
            ["verdict"] = "REPLACEMENT_ALLOWED",
        };
    }

    /// <summary>§37 300M -> 1B gate: the lab tier must prove the
    /// *processes* (training path, eval method, regression gate,
    /// distillation and compression pipelines) — not the mature
    /// capability floors themselves.</summary>
    public static Dictionary<string, object?> ThreeHundredMGate(
        IReadOnlyDictionary<string, bool> readiness)
    {
        string[] required =
        {
            "capability_training_paths_operational",
            "eval_method_complete", "regression_gate_complete",
            "distillation_pipeline_operational",
            "compression_pipeline_operational",
        };
        var unmet = required
            .Where(c => !readiness.TryGetValue(c, out bool ok) || !ok)
            .Cast<object?>().ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = unmet.Count == 0,
            ["format"] = "star-300m-to-1b-gate/v1",
            ["unmet"] = unmet,
            ["verdict"] = unmet.Count == 0
                ? "BUILD_1B_CANDIDATE" : "LAB_INCOMPLETE",
            ["note"] = "300M proves the process; 1B must prove the "
                       + "capability (§15-§16)",
        };
    }
}
