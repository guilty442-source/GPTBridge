// MaturityChecks.cs — acceptance battery for MaturityStandard
// (maturity directive §1-§40). Contract-level: gates, fail codes and
// the registry are exercised in memory; a scratch baseline dir proves
// file emission. Report format: star-maturity-checks/v1.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class MaturityChecks
{
    public const string ReportFormat = "star-maturity-checks/v1";

    private sealed record CheckResult(string Name, bool Ok,
                                      string Detail);

    private static CheckResult Check(string name, Func<bool> run,
                                     string detail = "")
    {
        try { return new(name, run(), detail); }
        catch (Exception ex)
        {
            return new(name, false,
                $"{ex.GetType().Name}: {ex.Message}");
        }
    }

    private static bool ExpectError(string code, Action act)
    {
        try { act(); return false; }
        catch (ExecutorError ee) { return ee.ErrorCode == code; }
    }

    private static JsonElement J(string json)
        => JsonDocument.Parse(json).RootElement.Clone();

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        var checks = new List<CheckResult>
        {
            // §3 stage ladder: only the last stage is mature.
            Check("stage-ladder", () =>
                MaturityStandard.Stages.Length == 6 &&
                MaturityStandard.Stages[^1] == "XC-1B-STANDARD" &&
                MaturityStandard.Stages[0] == "XC-1B-TRAINING"),

            // §29/§30 registry + seeded baseline file.
            Check("baseline-registry", () =>
            {
                var seed = MaturityStandard.SeedBaseline(toolRoot);
                var reg = MaturityStandard.BaselineRegistry(toolRoot);
                return ((List<object?>)reg["registered"]!)
                           .Cast<string>().Contains("XC1B_STANDARD_V1") &&
                       ((List<object?>)seed["floors"]!).Count ==
                           MaturityStandard.Capabilities.Length;
            }),

            // §6/§7 floor: one failing capability fails everything —
            // high math cannot rescue coding.
            Check("capability-floor", () =>
            {
                var floors = new Dictionary<string, double>
                    { ["math"] = 0.5, ["coding"] = 0.5 };
                var pass = MaturityStandard.FloorGate(
                    new Dictionary<string, double>
                        { ["math"] = 0.9, ["coding"] = 0.5 },
                    floors, "BASE_MODEL");
                bool fail = ExpectError(
                    "MATURITY_CAPABILITY_FLOOR_FAILED", () =>
                        MaturityStandard.FloorGate(
                            new Dictionary<string, double>
                                { ["math"] = 0.99, ["coding"] = 0.1 },
                            floors, "RUNTIME_AUGMENTED"));
                return (bool)pass["ok"]! && fail;
            }),

            // §8: a base-model failure uses its own code — runtime
            // augmentation can never substitute for the base layer.
            Check("base-model-layer", () =>
                ExpectError("BASE_MODEL_CAPABILITY_FAILED", () =>
                    MaturityStandard.FloorGate(
                        new Dictionary<string, double>
                            { ["reading"] = 0.2 },
                        new Dictionary<string, double>
                            { ["reading"] = 0.6 },
                        "BASE_MODEL"))),

            // §9: THINK_OFF must meet the floor unaided — the verdict
            // reports AUTO augmentation but never rescues.
            Check("thinking-off-first", () =>
            {
                bool denied = ExpectError(
                    "BASE_MODEL_CAPABILITY_FAILED", () =>
                        MaturityStandard.MaturityVerdict(
                            new Dictionary<string, double>
                                { ["math"] = 0.3 },
                            new Dictionary<string, double>
                                { ["math"] = 0.9 },   // AUTO rescues
                            new Dictionary<string, double>
                                { ["math"] = 0.5 }));
                var pass = MaturityStandard.MaturityVerdict(
                    new Dictionary<string, double> { ["math"] = 0.6 },
                    new Dictionary<string, double> { ["math"] = 0.9 },
                    new Dictionary<string, double> { ["math"] = 0.5 });
                return denied &&
                    (string)pass["verdict"]! == "MATURE" &&
                    ((List<object?>)pass["thinking_augmented"]!)
                        .Cast<string>().Contains("math");
            }),

            // §10-§12: per-phase retention codes; a single capability
            // below its retention floor fails the phase.
            Check("retention-gates", () =>
            {
                var pre = new Dictionary<string, double>
                    { ["math"] = 0.8, ["coding"] = 0.7 };
                var minRet = new Dictionary<string, double>
                    { ["math"] = 0.95, ["coding"] = 0.95 };
                var ok_ = MaturityStandard.RetentionGate(
                    "distill", pre,
                    new Dictionary<string, double>
                        { ["math"] = 0.78, ["coding"] = 0.68 },
                    minRet);
                bool distill = ExpectError(
                    "POST_DISTILLATION_REGRESSION", () =>
                        MaturityStandard.RetentionGate(
                            "distill", pre,
                            new Dictionary<string, double>
                                { ["math"] = 0.8, ["coding"] = 0.5 },
                            minRet));
                bool compress = ExpectError(
                    "POST_COMPRESSION_REGRESSION", () =>
                        MaturityStandard.RetentionGate(
                            "compress", pre,
                            new Dictionary<string, double>
                                { ["math"] = 0.7, ["coding"] = 0.7 },
                            minRet));
                bool quant = ExpectError(
                    "POST_QUANTIZATION_REGRESSION", () =>
                        MaturityStandard.RetentionGate(
                            "quantize", pre,
                            new Dictionary<string, double>
                                { ["math"] = 0.8, ["coding"] = 0.4 },
                            minRet));
                return (bool)ok_["ok"]! && distill && compress && quant;
            }),

            // §31-§33 golden suite: leakage denied, stability required.
            Check("golden-usability", () =>
            {
                var pass = MaturityStandard.GoldenGate(J("""
                    {"tasks":[
                     {"name":"json","runs":5,"runs_passed":5},
                     {"name":"math","runs":3,"runs_passed":3}]}
                    """));
                bool leak = ExpectError("GOLDEN_USABILITY_FAILED",
                    () => MaturityStandard.GoldenGate(J("""
                        {"tasks":[{"name":"json","trained_on":true,
                                   "runs":5,"runs_passed":5}]}
                        """)));
                bool unstable = ExpectError("GOLDEN_USABILITY_FAILED",
                    () => MaturityStandard.GoldenGate(J("""
                        {"tasks":[{"name":"json","runs":5,
                                   "runs_passed":4}]}
                        """)));
                return (bool)pass["ok"]! && leak && unstable;
            }),

            // §35 nine certs all required.
            Check("certification", () =>
            {
                var all = MaturityStandard.Certs.ToDictionary(
                    c => c, _ => true);
                var pass = MaturityStandard.Certification(all);
                var missing = new Dictionary<string, bool>(all);
                missing.Remove("QuantizationCert");
                var inc = MaturityStandard.Certification(missing);
                return (string)pass["verdict"]! == "XC1B_CERTIFIED" &&
                       (string)inc["verdict"]! ==
                           "CERTIFICATION_INCOMPLETE";
            }),

            // §13 promotion AND.
            Check("promotion-decision", () =>
            {
                var all = new[]
                    {
                        "ALL_CAPABILITY_FLOORS_PASS",
                        "DISTILLATION_PASS", "COMPRESSION_PASS",
                        "QUANTIZATION_PASS", "RUNTIME_PASS",
                        "RESOURCE_PASS", "REGRESSION_PASS",
                    }.ToDictionary(c => c, _ => true);
                var ok_ = MaturityStandard.PromotionDecision(all);
                var hold = new Dictionary<string, bool>(all)
                    { ["QUANTIZATION_PASS"] = false };
                var denied = MaturityStandard.PromotionDecision(hold);
                return (string)ok_["verdict"]! == "PROMOTE" &&
                       (string)denied["verdict"]! == "HOLD";
            }),

            // §27/§28 replacement: a single regression denies.
            Check("replacement-gate", () =>
            {
                var prev = new Dictionary<string, double>
                    { ["math"] = 0.8, ["coding"] = 0.6 };
                var pass = MaturityStandard.ReplacementGate(
                    prev, new Dictionary<string, double>
                        { ["math"] = 0.85, ["coding"] = 0.65 });
                bool denied = ExpectError(
                    "STANDARD_REPLACEMENT_REGRESSION", () =>
                        MaturityStandard.ReplacementGate(
                            prev, new Dictionary<string, double>
                                { ["math"] = 0.95, ["coding"] = 0.55 }));
                return (string)pass["verdict"]! ==
                           "REPLACEMENT_ALLOWED" && denied;
            }),

            // §37 300M -> 1B readiness gate.
            Check("300m-to-1b-gate", () =>
            {
                var all = new[]
                    {
                        "capability_training_paths_operational",
                        "eval_method_complete",
                        "regression_gate_complete",
                        "distillation_pipeline_operational",
                        "compression_pipeline_operational",
                    }.ToDictionary(c => c, _ => true);
                var ok_ = MaturityStandard.ThreeHundredMGate(all);
                var no = MaturityStandard.ThreeHundredMGate(
                    new Dictionary<string, bool>(all)
                    { ["distillation_pipeline_operational"] = false });
                return (string)ok_["verdict"]! ==
                           "BUILD_1B_CANDIDATE" &&
                       (string)no["verdict"]! == "LAB_INCOMPLETE";
            }),

            // §17/§18: 1B is a class band, not an exact count.
            Check("standard-class-band", () =>
                MaturityStandard.StandardClassMin == 800_000_000L &&
                MaturityStandard.StandardClassMax == 1_200_000_000L),
        };

        int passed = checks.Count(c => c.Ok);
        return new Dictionary<string, object?>
        {
            ["ok"] = passed == checks.Count,
            ["format"] = ReportFormat,
            ["passed"] = passed,
            ["total"] = checks.Count,
            ["weights_mutated"] = false,
            ["checks"] = checks.Select(c => (object?)
                new Dictionary<string, object?>
                {
                    ["name"] = c.Name,
                    ["ok"] = c.Ok,
                    ["detail"] = c.Detail,
                }).ToList(),
        };
    }
}
