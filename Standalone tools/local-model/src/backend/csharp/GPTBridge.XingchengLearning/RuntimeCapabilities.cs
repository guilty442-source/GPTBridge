// RuntimeCapabilities.cs — ``star-runtime-capabilities/v1`` unified
// runtime capability layer (architecture-convergence contract §4).
//
// Single resolution point between the C# governed orchestration plane
// and the C++ NativeInferenceEngine: one request envelope carries every
// runtime-controllable axis — reasoning budget, tool policy, structured
// output, grounding, long-horizon tasking, vision budget, precision
// profile, context budget, KV/state budget, coding mode and deployment
// profile — and Resolve() turns it into one typed, bounded plan.
//
// Hard rules (fail-closed):
//   * capability layers never change architecture, weights or
//     generation semantics — they only bound resources and contracts;
//   * deployment profiles never produce a new checkpoint;
//   * context budgets above the bundle's trained max_position_embeddings
//     stay PROBE-ONLY and can never resolve to a production default;
//   * precision profiles other than REFERENCE_FP64 require the parity
//     evidence the precision suite produces — BF16 is a candidate, never
//     a silent promote.
//
// §9  ReasoningPolicy (NONE/LOW/NORMAL/HIGH): orchestration budgets
//     only — never a claim that the model "reasons better".
// §13 Deployment profiles (REFERENCE/BALANCED/FAST/LOW_MEMORY/EDGE):
//     precision / threads / device / KV mode / cache / vision /
//     reasoning budgets only.
// §28 ContextBudgetManager: probe lengths 2048..32768, fail-closed over
//     the model's trained context.
// §36 fail-closed error codes live in ConvErr.

namespace GPTBridge.XingchengLearning;

/// <summary>Fail-closed error codes added by the convergence contract
/// (§36). Kept as constants so every layer shares one vocabulary.</summary>
internal static class ConvErr
{
    public const string LanguageBoundaryViolation = "LANGUAGE_BOUNDARY_VIOLATION";
    public const string ToolSchemaInvalid = "TOOL_SCHEMA_INVALID";
    public const string ToolDecisionInvalid = "TOOL_DECISION_INVALID";
    public const string StructuredParseFailed = "STRUCTURED_PARSE_FAILED";
    public const string StructuredSchemaFailed = "STRUCTURED_SCHEMA_FAILED";
    public const string StructuredRepairFailed = "STRUCTURED_REPAIR_FAILED";
    public const string GroundingUnsupportedClaim = "GROUNDING_UNSUPPORTED_CLAIM";
    public const string StateModelMismatch = "STATE_MODEL_MISMATCH";
    public const string StateGenerationMismatch = "STATE_GENERATION_MISMATCH";
    public const string BundleProvenanceInvalid = "BUNDLE_PROVENANCE_INVALID";
    public const string PrecisionParityFailed = "PRECISION_PARITY_FAILED";
    public const string VisionBudgetParityFailed = "VISION_BUDGET_PARITY_FAILED";
    public const string SpeculativeDecoderUnavailable = "SPECULATIVE_DECODER_UNAVAILABLE";
    public const string ArchitectureChangeNotJustified = "ARCHITECTURE_CHANGE_NOT_JUSTIFIED";

    public static readonly string[] All =
    {
        LanguageBoundaryViolation, ToolSchemaInvalid, ToolDecisionInvalid,
        StructuredParseFailed, StructuredSchemaFailed, StructuredRepairFailed,
        GroundingUnsupportedClaim, StateModelMismatch, StateGenerationMismatch,
        BundleProvenanceInvalid, PrecisionParityFailed,
        VisionBudgetParityFailed, SpeculativeDecoderUnavailable,
        ArchitectureChangeNotJustified,
    };
}

/// <summary>§9 reasoning effort — orchestration budgets only.</summary>
internal enum ReasoningMode { NONE = 0, LOW = 1, NORMAL = 2, HIGH = 3 }

/// <summary>§9 bounded policy: the mode only retunes budgets, never
/// model capability.</summary>
internal sealed class ReasoningPolicy
{
    public ReasoningMode Mode = ReasoningMode.NORMAL;
    public long TokenBudget = 4096;
    public int ExpertBudget = 0;          // 0 = model default top_k
    public int VerificationRounds = 0;
    public int ToolBudget = 8;
    public int RagDepth = 1;
    public int SelfCheckCount = 0;

    public static ReasoningPolicy For(string mode)
    {
        var p = new ReasoningPolicy();
        // Batch-2 aliases (§2.1): OFF is the canonical name for NONE,
        // MEDIUM for NORMAL; AUTO defers strategy choice to
        // ReasoningRuntime and resolves here as the default lane.
        switch ((mode ?? "MEDIUM").ToUpperInvariant())
        {
            case "OFF": goto case "NONE";
            case "MEDIUM": goto case "NORMAL";
            case "AUTO": goto case "NORMAL";
            case "NONE":
                p.Mode = ReasoningMode.NONE;
                p.TokenBudget = 1024;
                p.VerificationRounds = 0;
                p.ToolBudget = 0;
                p.RagDepth = 0;
                p.SelfCheckCount = 0;
                break;
            case "LOW":
                p.Mode = ReasoningMode.LOW;
                p.TokenBudget = 2048;
                p.VerificationRounds = 0;
                p.ToolBudget = 4;
                p.RagDepth = 1;
                p.SelfCheckCount = 0;
                break;
            case "NORMAL":
                break;
            case "HIGH":
                p.Mode = ReasoningMode.HIGH;
                p.TokenBudget = 16384;
                p.VerificationRounds = 2;
                p.ToolBudget = 16;
                p.RagDepth = 3;
                p.SelfCheckCount = 1;
                break;
            default:
                throw new ExecutorError(
                    ConvErr.ToolDecisionInvalid,
                    $"unknown reasoning mode '{mode}'");
        }
        return p;
    }

    public Dictionary<string, object?> ToDict() => new()
    {
        ["mode"] = Mode.ToString(),
        ["token_budget"] = TokenBudget,
        ["expert_budget"] = ExpertBudget,
        ["verification_rounds"] = VerificationRounds,
        ["tool_budget"] = ToolBudget,
        ["rag_depth"] = RagDepth,
        ["self_check_count"] = SelfCheckCount,
        // Explicit honesty field (§9): a higher mode widens runtime
        // budgets; it never changes what the weights can do.
        ["capability_claim"] = "runtime_orchestration_only",
    };
}

/// <summary>§13 deployment profile — resource selection only; never a
/// checkpoint, architecture or semantics change.</summary>
internal sealed class DeploymentProfile
{
    public const string Reference = "REFERENCE";
    public const string Balanced = "BALANCED";
    public const string Fast = "FAST";
    public const string LowMemory = "LOW_MEMORY";
    public const string Edge = "EDGE";

    public static readonly string[] Names =
        { Reference, Balanced, Fast, LowMemory, Edge, UltraLowLatency };

    public const string UltraLowLatency = "ULTRA_LOW_LATENCY";

    public string Name = Balanced;
    public string Precision = "REFERENCE_FP64";   // §26 profile name
    public int Threads = 0;                        // 0 = host default
    public string Device = "cpu";                  // cpu | cuda
    public string KvMode = "fp64";                 // fp64 | int8 | paged
    public long CacheBudgetBytes = 0;              // 0 = unbounded
    public int VisionPatchBudget = 0;              // 0 = bundle default
    public string Reasoning = "NORMAL";

    public static DeploymentProfile For(string name)
    {
        var p = new DeploymentProfile();
        switch ((name ?? Balanced).ToUpperInvariant())
        {
            case Reference:
                p.Name = Reference;
                p.Precision = "REFERENCE_FP64";
                p.Device = "cpu";
                p.KvMode = "fp64";
                break;
            case Balanced:
                p.Name = Balanced;
                p.Precision = "REFERENCE_FP64";
                p.Device = "cpu";
                p.KvMode = "fp64";
                break;
            case Fast:
                p.Name = Fast;
                p.Precision = "PRODUCTION_BF16";   // candidate lane
                p.Device = "cuda";
                p.KvMode = "fp64";
                break;
            case LowMemory:
                p.Name = LowMemory;
                p.Precision = "REFERENCE_FP64";
                p.Device = "cpu";
                p.KvMode = "int8";
                p.VisionPatchBudget = 128;
                break;
            case Edge:
                p.Name = Edge;
                p.Precision = "EDGE_INT8";          // schema only (§26)
                p.Device = "cpu";
                p.KvMode = "int8";
                p.VisionPatchBudget = 64;
                p.Reasoning = "LOW";
                break;
            // §6 fast-path profile (batch-2 reasoning contract): the
            // lowest-latency lane; carries the rollback rule in
            // ReasoningRuntime.ResolveDeploymentFallback.
            case UltraLowLatency:
                p.Name = UltraLowLatency;
                p.Precision = "EDGE_INT8";          // schema only (§26)
                p.Device = "cpu";
                p.KvMode = "int8";
                p.VisionPatchBudget = 32;
                p.Reasoning = "NONE";
                break;
            default:
                throw new ExecutorError(
                    ConvErr.ToolDecisionInvalid,
                    $"unknown deployment profile '{name}'");
        }
        return p;
    }

    public Dictionary<string, object?> ToDict() => new()
    {
        ["profile"] = Name,
        ["precision"] = Precision,
        ["threads"] = Threads,
        ["device"] = Device,
        ["kv_mode"] = KvMode,
        ["cache_budget_bytes"] = CacheBudgetBytes,
        ["vision_patch_budget"] = VisionPatchBudget,
        ["reasoning"] = Reasoning,
        // §13 invariant, surfaced for audits: a profile never mints a
        // checkpoint and never touches weights or semantics.
        ["produces_checkpoint"] = false,
        ["changes_semantics"] = false,
    };
}

/// <summary>§26 precision profile names; only REFERENCE_FP64 is a
/// production lane today — PRODUCTION_BF16 is a candidate that must
/// carry precision-parity evidence, COMPACT_FP8/EDGE_INT8 are schema
/// placeholders for the same governed promotion path.</summary>
internal static class PrecisionProfiles
{
    public const string ReferenceFp64 = "REFERENCE_FP64";
    public const string ProductionBf16 = "PRODUCTION_BF16";
    public const string CompactFp8 = "COMPACT_FP8";
    public const string EdgeInt8 = "EDGE_INT8";
    public static readonly string[] Names =
        { ReferenceFp64, ProductionBf16, CompactFp8, EdgeInt8 };

    /// <summary>Profiles usable for production traffic. BF16 stays a
    /// candidate until its parity suite passes.</summary>
    public static bool IsProduction(string profile)
        => profile == ReferenceFp64;

    public static bool IsKnown(string profile)
        => Names.Contains(profile, StringComparer.Ordinal);
}

/// <summary>§28 context budget: probe-only above the model's trained
/// context; a request that exceeds the probe allowlist or the bundle's
/// hard context fails closed.</summary>
internal sealed class ContextBudgetManager
{
    public static readonly long[] ProbeLadders =
        { 2048, 4096, 8192, 16384, 32768 };

    /// <param name="trainedContext">the bundle's
    /// max_position_embeddings — the hard ceiling.</param>
    public Dictionary<string, object?> Resolve(
        long requested, long trainedContext)
    {
        if (trainedContext <= 0)
            throw new ExecutorError(
                "CONTEXT_MODEL_UNKNOWN", "bundle context limit unknown");
        bool probeAllowed = ProbeLadders.Contains(requested);
        var d = new Dictionary<string, object?>
        {
            ["requested_tokens"] = requested,
            ["trained_context"] = trainedContext,
            ["allowed"] = requested <= trainedContext,
            ["probe_ladder"] = ProbeLadders.Cast<object?>().ToList(),
            ["is_probe_length"] = probeAllowed,
        };
        if (requested > trainedContext)
        {
            // Above the trained context the answer is never "stretch the
            // model": production stays at the trained length, and only
            // ladder-registered probe lengths may run as experiments.
            d["production_default"] = false;
            d["resolution"] = probeAllowed
                ? "probe_only" : "denied";
            if (!probeAllowed)
                d["error"] = "CONTEXT_BUDGET_EXCEEDS_TRAINED";
        }
        else
        {
            d["resolution"] = "in_context";
        }
        return d;
    }
}

/// <summary>§4 unified runtime capability envelope — the single request
/// contract between the C# plane and NativeInferenceEngine.</summary>
internal sealed class RuntimeCapabilityRequest
{
    public const string Format = "star-runtime-capabilities/v1";

    public string Reasoning = "NORMAL";            // §9
    public string ToolPolicy = "auto";             // auto|required|denied
    public string StructuredOutput = "";           // schema id or ""
    public bool GroundingRequired = false;         // §16 grounded result
    public bool LongHorizon = false;               // §5 task coordinator
    public int VisionBudget = 0;                   // §20 patch budget
    public string PrecisionProfile =               // §26
        PrecisionProfiles.ReferenceFp64;
    public long ContextBudget = 0;                 // §28 (0 = default)
    public long KvStateBudgetBytes = 0;            // §27 (0 = default)
    public string CodingMode = "";                 // §10 star-code-task
    public string Deployment =                     // §13
        DeploymentProfile.Balanced;
    /// <summary>§16 AUTO input — optional typed task assessment.</summary>
    public TaskAssessment? Assessment;

    public static RuntimeCapabilityRequest Parse(
        Dictionary<string, object?> req)
    {
        var r = new RuntimeCapabilityRequest();
        string Str(string k, string d)
            => req.TryGetValue(k, out var v) && v is string s ? s : d;
        long Num(string k, long d)
            => req.TryGetValue(k, out var v) &&
               v is long or int ? Convert.ToInt64(v) : d;
        bool Bool(string k, bool d)
            => req.TryGetValue(k, out var v) && v is bool b ? b : d;

        r.Reasoning = Str("reasoning", r.Reasoning);
        r.ToolPolicy = Str("tool_policy", r.ToolPolicy);
        r.StructuredOutput = Str("structured_output", r.StructuredOutput);
        r.GroundingRequired = Bool("grounding_required", false);
        r.LongHorizon = Bool("long_horizon", false);
        r.VisionBudget = (int)Num("vision_budget", 0);
        r.PrecisionProfile = Str("precision_profile", r.PrecisionProfile);
        r.ContextBudget = Num("context_budget", 0);
        r.KvStateBudgetBytes = Num("kv_state_budget_bytes", 0);
        r.CodingMode = Str("coding_mode", r.CodingMode);
        r.Deployment = Str("deployment_profile", r.Deployment);
        if (req.TryGetValue("task_assessment", out var ta) &&
            ta is Dictionary<string, object?> td)
        {
            double Score(string k) =>
                td.TryGetValue(k, out var v) && v is long or int or double
                    ? Convert.ToDouble(v) : 0;
            r.Assessment = new TaskAssessment
            {
                ComplexityScore = Score("complexity"),
                RiskScore = Score("risk"),
                ToolNeed = Score("tool_need"),
                EvidenceNeed = Score("evidence_need"),
            };
        }
        return r;
    }
}

/// <summary>The single resolver: one request -> one bounded, typed plan.
/// All axis logic lives here — never scattered across CLIs (§4).</summary>
internal static class RuntimeCapabilityLayer
{
    public const string Format = RuntimeCapabilityRequest.Format;

    /// <param name="trainedContext">bundle max_position_embeddings; 0
    /// when unknown (context checks then fail closed).</param>
    public static Dictionary<string, object?> Resolve(
        Dictionary<string, object?> request, long trainedContext)
    {
        var req = RuntimeCapabilityRequest.Parse(request);
        var policy = ReasoningPolicy.For(req.Reasoning);
        var profile = DeploymentProfile.For(req.Deployment);
        // §2.1/§19: every reasoning mode resolves through the ONE
        // ReasoningRuntime — canonical names OFF/LOW/MEDIUM/HIGH map
        // to the batch-1 internal enum (NONE->OFF, NORMAL->MEDIUM).
        string rtMode = req.Reasoning.ToUpperInvariant() switch
        {
            "NONE" => "OFF", "NORMAL" => "MEDIUM",
            var m => m,
        };
        var rt = ReasoningRuntime.Resolve(rtMode, req.Assessment);

        if (!PrecisionProfiles.IsKnown(req.PrecisionProfile))
            throw new ExecutorError(
                ConvErr.PrecisionParityFailed,
                $"unknown precision profile '{req.PrecisionProfile}'");
        // A non-reference precision on a non-candidate lane resolves but
        // is marked provisional — the precision suite must carry it.
        string precisionState =
            PrecisionProfiles.IsProduction(req.PrecisionProfile)
                ? "production"
                : req.PrecisionProfile == PrecisionProfiles.ProductionBf16
                    ? "candidate_requires_parity"
                    : "schema_only";

        var ctx = new ContextBudgetManager().Resolve(
            req.ContextBudget > 0 ? req.ContextBudget : trainedContext,
            trainedContext);

        string toolPolicy = req.ToolPolicy.ToLowerInvariant();
        if (toolPolicy is not ("auto" or "required" or "denied"))
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                $"unknown tool_policy '{req.ToolPolicy}'");
        if (toolPolicy == "required" && policy.ToolBudget <= 0)
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                "tool_policy=required conflicts with reasoning=NONE " +
                "(tool_budget=0)");

        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["resolved"] = true,
            ["reasoning"] = policy.ToDict(),
            // §19 unified output — the resolved mode/strategy/budget
            // plan; the only sanctioned reasoning-contract source.
            ["reasoning_runtime"] = rt,
            ["tool_policy"] = new Dictionary<string, object?>
            {
                ["mode"] = toolPolicy,
                ["budget"] = policy.ToolBudget,
            },
            ["structured_output"] = new Dictionary<string, object?>
            {
                ["schema"] = req.StructuredOutput,
                ["enabled"] = req.StructuredOutput.Length > 0,
                ["contract"] = "star-structured-output/v1",
            },
            ["grounding"] = new Dictionary<string, object?>
            {
                ["required"] = req.GroundingRequired,
                ["contract"] = "star-grounded-result/v1",
                ["rag_depth"] = policy.RagDepth,
            },
            ["long_horizon"] = new Dictionary<string, object?>
            {
                ["enabled"] = req.LongHorizon,
                ["contract"] = "star-long-horizon-task/v1",
            },
            ["vision"] = new Dictionary<string, object?>
            {
                ["budget"] = req.VisionBudget > 0
                    ? req.VisionBudget : profile.VisionPatchBudget,
                ["controller"] = "VisionBudgetController",
                ["mode"] = "EXPERIMENTAL",   // §20: never default-on
            },
            ["precision"] = new Dictionary<string, object?>
            {
                ["profile"] = req.PrecisionProfile,
                ["state"] = precisionState,
            },
            ["context"] = ctx,
            ["kv_state"] = new Dictionary<string, object?>
            {
                ["budget_bytes"] = req.KvStateBudgetBytes,
                ["kv_mode"] = profile.KvMode,
                ["manager"] = "NativeStateManager",
            },
            ["coding_mode"] = new Dictionary<string, object?>
            {
                ["enabled"] = req.CodingMode.Length > 0,
                ["task"] = req.CodingMode,
                ["contract"] = "star-code-task/v1",
            },
            ["deployment"] = profile.ToDict(),
        };
    }

    /// <summary>Language allowlist (§1 / B31-B36): the only permitted
    /// implementation languages. Used by FeatureCatalog entries and the
    /// governance gate; Python stays retired with zero role.</summary>
    public static readonly string[] ImplementationLanguages =
        { "c", "cpp", "csharp", "fsharp", "rust" };

    public static readonly string[] DataFormats =
        { "json", "toml", "xml", "sql", "markdown" };
}
