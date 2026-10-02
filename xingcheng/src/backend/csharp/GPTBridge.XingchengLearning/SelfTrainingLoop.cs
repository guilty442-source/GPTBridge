// SelfTrainingLoop.cs — AutonomousCapabilityRecoveryLoop (self-
// training directive §0-§85), C# governance/contract layer.
//
// The loop may discover failures, classify capability, generate and
// verify candidate data, snapshot immutable datasets, run bounded
// single-capability recovery pilots, evaluate, distill, compress,
// stage — and hand the final artifact to the Promotion Gate. It may
// NEVER promote itself, modify canonical architecture, governance,
// maturity thresholds, promotion policy, or bypass verifier /
// provenance (§0/§47/§83).
//
// Contracts only — admission decisions, validators, receipts; no
// weights, no training side effects. The weight path itself stays in
// xingcheng_trainer.exe behind CapabilityFreeze + JobExecutor.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class SelfTrainingLoop
{
    public const string LoopFormat =
        "star-autonomous-recovery-loop/v1";
    public const string AdmitFormat = "star-self-training-admit/v1";
    public const string ClassifyFormat =
        "star-self-training-failure/v1";
    public const string PurityFormat =
        "star-capability-purity/v1";
    public const string TriggerFormat =
        "star-min-training-trigger/v1";
    public const string CircuitFormat =
        "star-self-training-circuit/v1";
    public const string ReceiptFormat =
        "star-self-training-decision/v1";

    // §76 data contracts owned by this plane.
    public static readonly string[] DataContracts =
    {
        "star-self-training-failure/v1",
        "star-self-training-sample/v1",
        "star-self-training-verification/v1",
        "star-self-training-run/v1",
        "star-self-training-decision/v1",
    };

    // §77 fail codes.
    public static readonly string[] FailCodes =
    {
        "SELF_TRAINING_DISABLED",
        "CAPABILITY_CLASSIFICATION_UNCERTAIN",
        "FAILURE_ATTRIBUTION_UNCERTAIN",
        "SELF_VERIFICATION_DENIED",
        "VERIFICATION_FAILED",
        "NOVELTY_TOO_LOW",
        "EVAL_DATA_LEAKAGE",
        "CAPABILITY_IMPURE_DATASET",
        "AUTONOMOUS_TRAINING_REGRESSION",
        "SELF_TRAINING_CIRCUIT_OPEN",
        "UNVERIFIED_TEACHER_DENIED",
        "PROMOTION_GATE_DENIED",
    };

    // §3 modes, ordered — each admits strictly more than the last.
    public static readonly string[] Modes =
    {
        "OFF", "COLLECT_ONLY", "DATASET_BUILD", "PILOT_ONLY",
        "GOVERNED_AUTONOMOUS",
    };

    // §4 pipeline stages (promotion itself is never a loop stage —
    // §47: the loop ends at STAGE).
    public static readonly string[] Stages =
    {
        "collect", "classify", "generate", "verify", "snapshot",
        "train", "eval_fast", "eval_regression", "eval_full",
        "distill", "compress", "quantize", "stage",
    };

    // §7 closed capability vocabulary — one recovery job, one
    // capability.
    public static readonly string[] Capabilities =
    {
        "INSTRUCTION", "CONTEXT", "MULTI_TURN", "STRUCTURED", "TOOL",
        "READING", "RAG", "MATH", "CODING", "VISION", "SYSTEM1",
        "THINKING",
    };

    // §9 causes that are never model-capability failures.
    public static readonly string[] NonModelCauses =
    {
        "tool_outage", "rag_retrieval_failure", "corrupt_document",
        "network_failure", "invalid_external_data",
    };

    // §15 verifier hierarchy — later tiers may only corroborate.
    public static readonly string[] VerifierTiers =
    {
        "DETERMINISTIC", "REFERENCE_BASED", "INDEPENDENT_MODEL",
        "SELF_CONSISTENCY",
    };

    // §29 bounded training envelope.
    public static readonly int[] StepLadder = { 50, 200, 400, 600 };
    public const int MaxLrCandidates = 3;

    // §75 typed receipt vocabulary — every autonomous decision leaves
    // one of these.
    public static readonly string[] ReceiptKinds =
    {
        "WHY_TRAIN", "WHY_SAMPLE_ACCEPTED", "WHY_SAMPLE_REJECTED",
        "WHY_EXTEND", "WHY_STOP", "WHY_STAGE", "WHY_REJECT",
    };

    // §55 failure-memory states.
    public static readonly string[] FailureStates =
        { "OPEN", "TRAINED", "RESOLVED", "REGRESSED" };

    // §56 recovery priority order (300M lane).
    public static readonly string[] RecoveryOrder =
    {
        "INSTRUCTION", "CONTEXT", "MULTI_TURN", "STRUCTURED", "TOOL",
        "READING", "RAG", "MATH", "CODING", "VISION",
    };

    private static string Str(JsonElement r, string k,
        string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;
    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number &&
        v.TryGetInt64(out long n) ? n : d;
    private static double DNum(JsonElement r, string k,
        double d = double.NaN) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;
    private static bool Truthy(JsonElement r, string k) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.True;

    // ------------------------------------------------------- mode --

    private static int ModeRank(string mode) =>
        Array.FindIndex(Modes, m => m == mode.ToUpperInvariant());

    // §3/§79-§82 stage admission matrix. OFF admits nothing; the loop
    // never admits "promote" — promotion is ConvergenceGate +
    // MaturityBaseline + Lifecycle, not a loop stage (§47).
    public static bool StageAdmitted(string mode, string stage)
    {
        int rank = ModeRank(mode);
        if (rank <= 0) return false;
        switch (stage)
        {
            case "collect":
                return true;                       // every live mode
            case "classify" or "generate" or "verify" or "snapshot":
                return rank >= 2;                  // DATASET_BUILD+
            case "train" or "eval_fast":
                return rank >= 3;                  // PILOT_ONLY+
            case "eval_regression" or "distill" or "compress" or
                 "quantize" or "eval_full" or "stage":
                return rank >= 4;                  // GOVERNED_AUTONOMOUS
            case "promote":
                return false;                      // never the loop
            default:
                return false;
        }
    }

    public static Dictionary<string, object?> Admit(
        JsonElement el, string toolRoot)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        string mode = Str(el, "mode", policy.SelfTrainingMode);
        string stage = Str(el, "stage");
        if (ModeRank(mode) < 0)
            return new()
            {
                ["ok"] = false, ["format"] = AdmitFormat,
                ["verdict"] = "SELF_TRAINING_DISABLED",
                ["error"] = $"unknown mode '{mode}'",
            };
        bool ok = StageAdmitted(mode, stage);
        return new()
        {
            ["ok"] = ok,
            ["format"] = AdmitFormat,
            ["mode"] = mode.ToUpperInvariant(),
            ["stage"] = stage,
            ["verdict"] = ok ? "STAGE_ADMITTED" : "STAGE_DENIED",
            ["error"] = ok ? null :
                stage == "promote" ? "PROMOTION_GATE_DENIED"
                : mode.ToUpperInvariant() == "OFF"
                    ? "SELF_TRAINING_DISABLED"
                    : "MODE_INSUFFICIENT",
            ["rule"] = "loop never admits promote — §47: " +
                       "ConvergenceGate + MaturityBaseline + Lifecycle",
        };
    }

    // ------------------------------------------------- classify ----

    // §8-§9 validate a failure-classification record: capability must
    // be in the closed vocabulary, attribution must be model-side,
    // confidence below threshold routes to REVIEW_REQUIRED (never
    // auto-training).
    public static Dictionary<string, object?> ClassifyCheck(
        JsonElement el)
    {
        var bad = new List<object?>();
        string cap = Str(el, "primary_capability").ToUpperInvariant();
        if (!Capabilities.Contains(cap))
            bad.Add(new Dictionary<string, object?>
            {
                ["field"] = "primary_capability",
                ["got"] = cap == "" ? "MISSING" : cap,
            });
        string attr = Str(el, "attribution", "model");
        if (NonModelCauses.Contains(attr))
            return new()
            {
                ["ok"] = false, ["format"] = ClassifyFormat,
                ["verdict"] = "FAILURE_ATTRIBUTION_UNCERTAIN",
                ["error"] = $"{attr} is not a model-capability " +
                            "failure (§9)",
                ["violations"] = bad,
            };
        double conf = DNum(el, "confidence", 0.0);
        double minConf = DNum(el, "min_confidence", 0.6);
        bool review = conf < minConf || cap == "" ||
                      bad.Count > 0;
        return new()
        {
            ["ok"] = !review,
            ["format"] = ClassifyFormat,
            ["primary_capability"] = cap,
            ["confidence"] = conf,
            ["verdict"] = review
                ? "REVIEW_REQUIRED" : "CLASSIFIED",
            ["error"] = review && bad.Count == 0
                ? "CAPABILITY_CLASSIFICATION_UNCERTAIN" : null,
            ["violations"] = bad,
            ["rule"] = "uncertain primary capability never reaches " +
                       "training (§8)",
        };
    }

    // ---------------------------------------------------- purity ---

    // §24 a SINGLE_CAPABILITY_RECOVERY dataset may carry exactly one
    // primary capability; secondary tags are allowed, mixed primaries
    // are not.
    public static Dictionary<string, object?> PurityCheck(
        JsonElement el)
    {
        var primaries = new HashSet<string>(
            StringComparer.OrdinalIgnoreCase);
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty("samples", out var s) &&
            s.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in s.EnumerateArray())
            {
                string p = Str(item, "primary_capability");
                if (p.Length > 0) primaries.Add(p);
            }
        }
        else
        {
            string p = Str(el, "primary_capability");
            if (p.Length > 0) primaries.Add(p);
        }
        bool ok = primaries.Count == 1;
        return new()
        {
            ["ok"] = ok,
            ["format"] = PurityFormat,
            ["primary_capabilities"] =
                primaries.Cast<object?>().ToList(),
            ["verdict"] = ok
                ? "CAPABILITY_PURE" : "CAPABILITY_IMPURE_DATASET",
            ["rule"] = "one recovery job = one primary capability " +
                       "(§7/§24)",
        };
    }

    // --------------------------------------------------- trigger ---

    // §67-§68 MinimumTrainingTrigger — no useful data, no run.
    public static Dictionary<string, object?> TriggerCheck(
        JsonElement el)
    {
        long verified = Num(el, "verified_failure_count", 0);
        long minVerified = Num(el, "min_verified", 8);
        double novelty = DNum(el, "novelty_mean", 0.0);
        double minNovelty = DNum(el, "min_novelty", 0.3);
        double severity = DNum(el, "severity_max", 0.0);
        double minSev = DNum(el, "min_severity", 0.4);
        long windowS = Num(el, "window_seconds", 0);
        long minWindow = Num(el, "min_window_seconds", 300);
        var reasons = new List<object?>();
        if (verified < minVerified)
            reasons.Add($"verified_failure_count {verified} < " +
                        minVerified);
        if (novelty < minNovelty)
            reasons.Add($"novelty_mean {novelty:F2} < {minNovelty}");
        if (severity < minSev)
            reasons.Add($"severity_max {severity:F2} < {minSev}");
        if (windowS > 0 && windowS < minWindow)
            reasons.Add($"window_seconds {windowS} < {minWindow}");
        bool ok = reasons.Count == 0;
        return new()
        {
            ["ok"] = ok,
            ["format"] = TriggerFormat,
            ["verdict"] = ok
                ? "TRAINING_TRIGGERED" : "KEEP_COLLECTING",
            ["reasons"] = reasons,
            ["rule"] = "§67: insufficient verified novel failures -> " +
                       "stay in COLLECT (no run)",
        };
    }

    // --------------------------------------------------- circuit ---

    // §72 circuit breaker: consecutive training failures, repeated
    // regression or verification anomalies snap the loop back to
    // COLLECT_ONLY.
    public static Dictionary<string, object?> CircuitCheck(
        JsonElement el)
    {
        long fails = Num(el, "consecutive_train_failures", 0);
        long regressions = Num(el, "regression_count", 0);
        bool anomaly = Truthy(el, "verification_anomaly");
        long maxFails = Num(el, "max_consecutive_failures", 3);
        long maxReg = Num(el, "max_regressions", 2);
        bool open = fails >= maxFails || regressions >= maxReg ||
                    anomaly;
        return new()
        {
            ["ok"] = !open,
            ["format"] = CircuitFormat,
            ["consecutive_failures"] = fails,
            ["regressions"] = regressions,
            ["verification_anomaly"] = anomaly,
            ["verdict"] = open
                ? "SELF_TRAINING_CIRCUIT_OPEN" : "CIRCUIT_CLOSED",
            ["forced_mode"] = open ? "COLLECT_ONLY" : null,
            ["rule"] = "§72: repeated failure/regression/anomaly -> " +
                       "COLLECT_ONLY",
        };
    }

    // ---------------------------------------------------- receipt --

    // §75 every autonomous decision must emit one typed receipt with
    // machine-readable reasons — a decision without a receipt is a
    // black-box mutation and denied.
    public static Dictionary<string, object?> ReceiptCheck(
        JsonElement el)
    {
        var bad = new List<object?>();
        string kind = Str(el, "receipt").ToUpperInvariant();
        if (!ReceiptKinds.Contains(kind))
            bad.Add("receipt kind not in WHY_* vocabulary");
        if (Str(el, "reason").Length == 0)
            bad.Add("reason missing");
        if (Str(el, "evidence_ref").Length == 0)
            bad.Add("evidence_ref missing");
        return new()
        {
            ["ok"] = bad.Count == 0,
            ["format"] = ReceiptFormat,
            ["receipt"] = kind,
            ["verdict"] = bad.Count == 0
                ? "RECEIPT_VALID" : "RECEIPT_INVALID",
            ["violations"] = bad,
        };
    }

    // --------------------------------------------------- contract --

    // §0-§85 the whole loop contract in one emission.
    public static Dictionary<string, object?> Contract(
        string toolRoot)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        var admission = new Dictionary<string, object?>();
        foreach (string m in Modes)
            admission[m] = Stages.Where(s => StageAdmitted(m, s))
                                 .Cast<object?>().ToList();
        return new()
        {
            ["ok"] = true,
            ["format"] = LoopFormat,
            ["mode"] = policy.SelfTrainingMode,
            ["modes"] = Modes.Cast<object?>().ToList(),
            ["stage_admission"] = admission,
            ["loop_never"] = new object?[]
            {
                "promote_self", "modify_canonical_architecture",
                "modify_governance", "modify_maturity_threshold",
                "modify_promotion_policy", "bypass_verifier",
                "bypass_provenance",
            },
            ["capabilities"] = Capabilities.Cast<object?>().ToList(),
            ["recovery_order"] =
                RecoveryOrder.Cast<object?>().ToList(),
            ["non_model_causes"] =
                NonModelCauses.Cast<object?>().ToList(),
            ["verifier_tiers"] =
                VerifierTiers.Cast<object?>().ToList(),
            ["self_consistency_rule"] =
                "never sole production evidence (§15)",
            ["training_envelope"] = new Dictionary<string, object?>
            {
                ["step_ladder"] =
                    StepLadder.Cast<object?>().ToList(),
                ["max_lr_candidates"] = MaxLrCandidates,
                ["kind_default"] = "sft",
                ["rl"] = "frozen",
                ["dpo"] = "governance_opt_in_only",
            },
            ["gates"] = new object?[]
            {
                "FailureAttributionGate", "NoveltyGate",
                "EvaluationLeakageGate", "DatasetQualityScore",
                "CapabilityPurity", "MinimumTrainingTrigger",
                "CircuitBreaker", "PromotionGate(external)",
            },
            ["data_contracts"] =
                DataContracts.Cast<object?>().ToList(),
            ["fail_codes"] = FailCodes.Cast<object?>().ToList(),
            ["receipt_kinds"] =
                ReceiptKinds.Cast<object?>().ToList(),
            ["failure_states"] =
                FailureStates.Cast<object?>().ToList(),
            ["telemetry"] = new object?[]
            {
                "failure_count", "generated_samples",
                "verified_samples", "rejected_samples",
                "training_tokens", "steps", "gpu_seconds",
                "time_to_target", "capability_gain",
                "regression_count", "distillation_retention",
                "compression_retention",
            },
            ["kpi"] = new object?[]
            {
                "ResolvedFailuresPerGPUHour",
                "CapabilityGainPerGPUSecond",
                "CapabilityGainPerTrainingToken",
                "TimeToQualifiedModel",
            },
            ["invariants"] = new object?[]
            {
                "real_human_data_preserved_52",
                "synthetic_ratio_bounded_53",
                "capability_anchor_set_54",
                "golden_suite_never_trained_51",
                "one_capability_per_job_7",
                "active_params_max_1b_61",
                "kill_switch_71", "budget_policy_69",
                "inference_priority_70", "audit_chain_74",
            },
        };
    }
}
