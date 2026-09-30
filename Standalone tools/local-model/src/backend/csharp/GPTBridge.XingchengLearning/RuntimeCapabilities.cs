// RuntimeCapabilities.cs ??``star-runtime-capabilities/v1`` (§4).
//
// The single runtime-capability layer between the C# governed
// orchestration plane and the C++ NativeInferenceEngine. All runtime
// knobs ??reasoning budget, tool policy, structured output, grounding,
// long-horizon tasks, vision budget, precision profile, context budget,
// KV/state budget, coding mode and deployment profile ??resolve here
// and nowhere else. No component reads its own ad-hoc flags.
//
// Knobs only re-shape orchestration/runtime behaviour; they can never
// change model weights, architecture semantics or generation lineage.
// Unknown fields and invalid values fail closed.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class RuntimeCapabilities
{
    public const string Format = "star-runtime-capabilities/v1";
    public const string Rel = "runtime/settings/runtime-capabilities.json";

    public static readonly string[] ReasoningModes =
        { "NONE", "LOW", "NORMAL", "HIGH" };
    public static readonly string[] PrecisionProfiles =
        { "REFERENCE_FP64", "PRODUCTION_BF16", "COMPACT_FP8",
          "EDGE_INT8" };
    public static readonly string[] DeploymentProfiles =
        { "REFERENCE", "BALANCED", "FAST", "LOW_MEMORY", "EDGE" };
    public static readonly string[] VisionProfiles =
        { "FULL", "BALANCED", "COMPACT" };
    public static readonly string[] CodingModes =
        { "off", "proposal_only" };
    public static readonly string[] KvModes =
        { "fp64", "int8", "paged" };
    public static readonly int[] ContextProbeSizes =
        { 2048, 4096, 8192, 16384, 32768 };

    private static string Path_(string toolRoot)
        => Path.Combine(
            toolRoot, Rel.Replace('/', Path.DirectorySeparatorChar));

    // ------------------------------------------------- resolve knobs --

    /// <summary>Resolved capability profile. Every field is a runtime
    /// policy value; nothing here is a weight/architecture knob.</summary>
    public sealed class Profile
    {
        // reasoning policy (§9) ??budgets only; HIGH never claims the
        // model got smarter.
        public string ReasoningMode = "NORMAL";
        public int ReasoningTokenBudget = 4096;
        public int ReasoningVerificationRounds = 1;
        public int ReasoningToolBudget = 4;
        public int ReasoningRagDepth = 2;
        public int ReasoningSelfCheckCount = 1;
        // tool policy (§4/§16)
        public string[] ToolsAllowed = { };
        public bool ToolRequiresConfirmation = true;
        // structured output (§18)
        public bool StructuredOutputEnabled = false;
        public bool StructuredRepairOnce = true;
        // grounding (§16)
        public bool GroundingRequired = false;
        public double GroundingMinSupport = 0.0;
        // long-horizon tasks (§5)
        public bool LongHorizonEnabled = false;
        public int LongHorizonCheckpointInterval = 8;
        // vision budget (§20) ??experimental; FULL is the only
        // production default until parity benchmarks exist.
        public string VisionProfile = "FULL";
        public int VisionPatchBudget = 64;
        // precision profile (§26)
        public string PrecisionProfile = "REFERENCE_FP64";
        // context budget (§28) ??probe-only ceiling.
        public int ContextBudgetTokens = 8192;
        // kv/state budget (§7/§27)
        public string KvMode = "fp64";
        public long KvBudgetBytes = 0;          // 0 = engine default
        public long PrefixCacheBudgetBytes = 0;
        public int PrefixCacheMaxEntries = 0;
        public long RecurrentStateBudgetBytes = 0;
        // coding mode (§10)
        public string CodingMode = "off";
        // deployment profile (§13) ??expands into the knobs above.
        public string DeploymentProfile = "REFERENCE";
        public int Threads = 0;                 // 0 = engine default
        public string Device = "cpu";           // cpu|cuda

        public Dictionary<string, object?> ToDict()
            => new()
            {
                ["format"] = Format,
                ["reasoning"] = new Dictionary<string, object?>
                {
                    ["mode"] = ReasoningMode,
                    ["token_budget"] = ReasoningTokenBudget,
                    ["verification_rounds"] = ReasoningVerificationRounds,
                    ["tool_budget"] = ReasoningToolBudget,
                    ["rag_depth"] = ReasoningRagDepth,
                    ["self_check_count"] = ReasoningSelfCheckCount,
                },
                ["tool_policy"] = new Dictionary<string, object?>
                {
                    ["allowed"] = ToolsAllowed.Cast<object?>().ToList(),
                    ["requires_confirmation"] = ToolRequiresConfirmation,
                },
                ["structured_output"] = new Dictionary<string, object?>
                {
                    ["enabled"] = StructuredOutputEnabled,
                    ["repair_once"] = StructuredRepairOnce,
                },
                ["grounding"] = new Dictionary<string, object?>
                {
                    ["required"] = GroundingRequired,
                    ["min_support"] = GroundingMinSupport,
                },
                ["long_horizon"] = new Dictionary<string, object?>
                {
                    ["enabled"] = LongHorizonEnabled,
                    ["checkpoint_interval_steps"] =
                        LongHorizonCheckpointInterval,
                },
                ["vision_budget"] = new Dictionary<string, object?>
                {
                    ["profile"] = VisionProfile,
                    ["patch_budget"] = VisionPatchBudget,
                    ["experimental"] = VisionProfile != "FULL",
                },
                ["precision_profile"] = PrecisionProfile,
                ["context_budget"] = new Dictionary<string, object?>
                {
                    ["max_tokens"] = ContextBudgetTokens,
                    ["probe_sizes"] =
                        ContextProbeSizes.Cast<object?>().ToList(),
                    ["probe_only"] = true,
                },
                ["kv_state_budget"] = new Dictionary<string, object?>
                {
                    ["kv_mode"] = KvMode,
                    ["kv_budget_bytes"] = KvBudgetBytes,
                    ["prefix_cache_bytes"] = PrefixCacheBudgetBytes,
                    ["prefix_cache_max_entries"] = PrefixCacheMaxEntries,
                    ["recurrent_state_bytes"] = RecurrentStateBudgetBytes,
                },
                ["coding_mode"] = CodingMode,
                ["deployment"] = new Dictionary<string, object?>
                {
                    ["profile"] = DeploymentProfile,
                    ["threads"] = Threads,
                    ["device"] = Device,
                },
            };
    }

    // --------------------------------------------- deployment tables --

    /// <summary>§13 deployment profiles re-shape only runtime knobs ??    /// precision / threads / device / kv mode / cache + vision +
    /// reasoning budgets. Never architecture, never weights.</summary>
    public static void ApplyDeploymentProfile(Profile p)
    {
        switch (p.DeploymentProfile)
        {
            case "REFERENCE":
                p.PrecisionProfile = "REFERENCE_FP64";
                p.KvMode = "fp64"; p.Device = "cpu";
                break;
            case "BALANCED":
                p.PrecisionProfile = "PRODUCTION_BF16";
                p.KvMode = "int8"; p.Device = "cpu";
                break;
            case "FAST":
                p.PrecisionProfile = "PRODUCTION_BF16";
                p.KvMode = "paged"; p.Device = "cuda";
                break;
            case "LOW_MEMORY":
                p.PrecisionProfile = "COMPACT_FP8";
                p.KvMode = "int8"; p.Device = "cpu";
                p.VisionPatchBudget =
                    Math.Min(p.VisionPatchBudget, 32);
                break;
            case "EDGE":
                p.PrecisionProfile = "EDGE_INT8";
                p.KvMode = "int8"; p.Device = "cpu";
                p.VisionPatchBudget =
                    Math.Min(p.VisionPatchBudget, 16);
                p.ReasoningTokenBudget =
                    Math.Min(p.ReasoningTokenBudget, 1024);
                break;
        }
    }

    /// <summary>§9 reasoning-mode expansion ??budgets only.</summary>
    public static void ApplyReasoningMode(Profile p)
    {
        switch (p.ReasoningMode)
        {
            case "NONE":
                p.ReasoningTokenBudget = 0;
                p.ReasoningVerificationRounds = 0;
                p.ReasoningToolBudget = 0;
                p.ReasoningRagDepth = 0;
                p.ReasoningSelfCheckCount = 0;
                break;
            case "LOW":
                p.ReasoningTokenBudget = 1024;
                p.ReasoningVerificationRounds = 1;
                p.ReasoningToolBudget = 2;
                p.ReasoningRagDepth = 1;
                p.ReasoningSelfCheckCount = 0;
                break;
            case "NORMAL":
                p.ReasoningTokenBudget = 4096;
                p.ReasoningVerificationRounds = 1;
                p.ReasoningToolBudget = 4;
                p.ReasoningRagDepth = 2;
                p.ReasoningSelfCheckCount = 1;
                break;
            case "HIGH":
                p.ReasoningTokenBudget = 16384;
                p.ReasoningVerificationRounds = 2;
                p.ReasoningToolBudget = 8;
                p.ReasoningRagDepth = 4;
                p.ReasoningSelfCheckCount = 2;
                break;
        }
    }

    // ----------------------------------------------------- load/parse --

    public static Profile Load(string toolRoot)
    {
        string path = Path_(toolRoot);
        if (!File.Exists(path))
        {
            var d = new Profile();
            ApplyDeploymentProfile(d);
            return d;
        }
        return Parse(File.ReadAllText(path), path);
    }

    public static Profile Parse(string json, string source)
    {
        JsonElement root;
        try { root = JsonDocument.Parse(json).RootElement; }
        catch (JsonException e)
        {
            throw new ExecutorError(
                "RUNTIME_CAPABILITY_INVALID",
                $"{source}: invalid json: {e.Message}");
        }
        if (root.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "RUNTIME_CAPABILITY_INVALID", $"{source}: not object");
        var p = new Profile();
        string fmt = root.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "RUNTIME_CAPABILITY_INVALID",
                $"{source}: expected {Format}");

        // fail-closed field allowlist ??unknown knobs would silently
        // diverge the resolved profile.
        var known = new HashSet<string>(StringComparer.Ordinal)
        {
            "format", "reasoning", "tool_policy", "structured_output",
            "grounding", "long_horizon", "vision_budget",
            "precision_profile", "context_budget", "kv_state_budget",
            "coding_mode", "deployment",
        };
        foreach (var prop in root.EnumerateObject())
            if (!known.Contains(prop.Name))
                throw new ExecutorError(
                    "RUNTIME_CAPABILITY_INVALID",
                    $"{source}: unknown field {prop.Name}");

        if (root.TryGetProperty("reasoning", out var rs) &&
            rs.ValueKind == JsonValueKind.Object)
            p.ReasoningMode =
                Get(rs, "mode", p.ReasoningMode);
        if (root.TryGetProperty("precision_profile", out var pp) &&
            pp.ValueKind == JsonValueKind.String)
            p.PrecisionProfile = pp.GetString()!;
        if (root.TryGetProperty("coding_mode", out var cm) &&
            cm.ValueKind == JsonValueKind.String)
            p.CodingMode = cm.GetString()!;
        if (root.TryGetProperty("vision_budget", out var vb) &&
            vb.ValueKind == JsonValueKind.Object)
        {
            p.VisionProfile = Get(vb, "profile", p.VisionProfile);
            p.VisionPatchBudget =
                (int)GetNum(vb, "patch_budget", p.VisionPatchBudget);
        }
        if (root.TryGetProperty("context_budget", out var cb) &&
            cb.ValueKind == JsonValueKind.Object)
            p.ContextBudgetTokens =
                (int)GetNum(cb, "max_tokens", p.ContextBudgetTokens);
        if (root.TryGetProperty("kv_state_budget", out var kb) &&
            kb.ValueKind == JsonValueKind.Object)
        {
            p.KvMode = Get(kb, "kv_mode", p.KvMode);
            p.KvBudgetBytes =
                (long)GetNum(kb, "kv_budget_bytes", p.KvBudgetBytes);
            p.PrefixCacheBudgetBytes =
                (long)GetNum(kb, "prefix_cache_bytes",
                             p.PrefixCacheBudgetBytes);
            p.PrefixCacheMaxEntries =
                (int)GetNum(kb, "prefix_cache_max_entries",
                            p.PrefixCacheMaxEntries);
            p.RecurrentStateBudgetBytes =
                (long)GetNum(kb, "recurrent_state_bytes",
                             p.RecurrentStateBudgetBytes);
        }
        if (root.TryGetProperty("tool_policy", out var tp) &&
            tp.ValueKind == JsonValueKind.Object)
        {
            if (tp.TryGetProperty("allowed", out var al) &&
                al.ValueKind == JsonValueKind.Array)
                p.ToolsAllowed = al.EnumerateArray()
                    .Select(x => x.GetString() ?? "")
                    .Where(s => s.Length > 0).ToArray();
            p.ToolRequiresConfirmation =
                GetBool(tp, "requires_confirmation",
                        p.ToolRequiresConfirmation);
        }
        if (root.TryGetProperty("structured_output", out var so) &&
            so.ValueKind == JsonValueKind.Object)
        {
            p.StructuredOutputEnabled =
                GetBool(so, "enabled", p.StructuredOutputEnabled);
            p.StructuredRepairOnce =
                GetBool(so, "repair_once", p.StructuredRepairOnce);
        }
        if (root.TryGetProperty("grounding", out var gr) &&
            gr.ValueKind == JsonValueKind.Object)
        {
            p.GroundingRequired =
                GetBool(gr, "required", p.GroundingRequired);
            p.GroundingMinSupport =
                GetNum(gr, "min_support", p.GroundingMinSupport);
        }
        if (root.TryGetProperty("long_horizon", out var lh) &&
            lh.ValueKind == JsonValueKind.Object)
        {
            p.LongHorizonEnabled =
                GetBool(lh, "enabled", p.LongHorizonEnabled);
            p.LongHorizonCheckpointInterval =
                (int)GetNum(lh, "checkpoint_interval_steps",
                            p.LongHorizonCheckpointInterval);
        }
        if (root.TryGetProperty("deployment", out var dp) &&
            dp.ValueKind == JsonValueKind.Object)
        {
            p.DeploymentProfile =
                Get(dp, "profile", p.DeploymentProfile);
            p.Threads = (int)GetNum(dp, "threads", p.Threads);
            p.Device = Get(dp, "device", p.Device);
        }

        // enum validation ??fail closed before expansion.
        void Need(string name, string v, string[] allowed)
        {
            if (!allowed.Contains(v))
                throw new ExecutorError(
                    "RUNTIME_CAPABILITY_INVALID",
                    $"{source}: {name}={v} not in " +
                    string.Join("/", allowed));
        }
        Need("reasoning.mode", p.ReasoningMode, ReasoningModes);
        Need("precision_profile", p.PrecisionProfile, PrecisionProfiles);
        Need("vision_budget.profile", p.VisionProfile, VisionProfiles);
        Need("coding_mode", p.CodingMode, CodingModes);
        Need("kv_state_budget.kv_mode", p.KvMode, KvModes);
        Need("deployment.profile", p.DeploymentProfile,
             DeploymentProfiles);
        if (p.Device is not ("cpu" or "cuda"))
            throw new ExecutorError(
                "RUNTIME_CAPABILITY_INVALID",
                $"{source}: deployment.device={p.Device}");
        if (p.ContextBudgetTokens > 32768)
            throw new ExecutorError(
                "RUNTIME_CAPABILITY_INVALID",
                $"{source}: context_budget exceeds probe ceiling 32768 " +
                "(production default stays the trained context)");
        if (p.VisionProfile != "FULL")
        {
            // §20: adaptive vision compression is EXPERIMENTAL ??a
            // non-FULL profile must stay explicitly marked.
            if (p.VisionPatchBudget > 64)
                p.VisionPatchBudget = 64;
        }

        // expansion order: deployment pins its table values, then
        // reasoning mode pins its budget table ??explicit job fields in
        // the file were already absorbed above; table wins for the
        // derived budgets so two profiles can never diverge silently.
        ApplyDeploymentProfile(p);
        ApplyReasoningMode(p);
        return p;
    }

    private static string Get(JsonElement o, string k, string d)
        => o.TryGetProperty(k, out var v) &&
           v.ValueKind == JsonValueKind.String
               ? v.GetString() ?? d : d;

    private static double GetNum(JsonElement o, string k, double d)
        => o.TryGetProperty(k, out var v) &&
           v.ValueKind == JsonValueKind.Number &&
           v.TryGetDouble(out double n) ? n : d;

    private static bool GetBool(JsonElement o, string k, bool d)
        => o.TryGetProperty(k, out var v) &&
           v.ValueKind is JsonValueKind.True or JsonValueKind.False
               ? v.GetBoolean() : d;

    // ------------------------------------------------------- commands --

    public static Dictionary<string, object?> Status(string toolRoot)
    {
        var p = Load(toolRoot);
        string path = Path_(toolRoot);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["settings_file"] = File.Exists(path) ? Rel : "(defaults)",
            ["resolved"] = p.ToDict(),
        };
    }

    public static Dictionary<string, object?> Validate(string file)
    {
        if (string.IsNullOrEmpty(file) || !File.Exists(file))
            throw new ExecutorError(
                "RUNTIME_CAPABILITY_INVALID", "profile file missing");
        var p = Parse(File.ReadAllText(file), file);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["file"] = file,
            ["resolved"] = p.ToDict(),
        };
    }
}

/// <summary>Unified failure taxonomy: every error code — C#
/// constants and native tool codes alike — maps to exactly one class.
/// Classification is prefix-ordered and total: an unrecognized code
/// lands in RUNTIME rather than nowhere.</summary>
internal static class FailureTaxonomy
{
    public static readonly string[] Categories =
    {
        "ARCHITECTURE", "CHECKPOINT", "RUNTIME", "STATE", "CACHE",
        "CUDA", "TRAINER", "TOOL", "STRUCTURED_OUTPUT", "GROUNDING",
        "GENERATION", "LIFECYCLE", "CAPABILITY", "LANGUAGE_BOUNDARY",
    };

    // Explicit table first, then longest-prefix fallbacks.
    private static readonly (string Prefix, string Category)[] Rules =
    {
        ("LANGUAGE_BOUNDARY", "LANGUAGE_BOUNDARY"),
        ("CANONICAL_CONTRACT_VIOLATION", "ARCHITECTURE"),
        ("ARCHITECTURE_", "ARCHITECTURE"),
        ("ARCH_", "ARCHITECTURE"),
        ("XCN", "CHECKPOINT"),
        ("CHECKPOINT", "CHECKPOINT"),
        ("BUNDLE_", "CHECKPOINT"),
        ("PROVENANCE", "CHECKPOINT"),
        ("CKPT_", "CHECKPOINT"),
        ("STATE_", "STATE"),
        ("SNAPSHOT_", "STATE"),
        ("DELTA_STATE", "STATE"),
        ("KV_", "CACHE"),
        ("CACHE_", "CACHE"),
        ("PREFIX_", "CACHE"),
        ("CUDA_", "CUDA"),
        ("TRAINER_", "TRAINER"),
        ("TRAIN_", "TRAINER"),
        ("PROBE_", "TRAINER"),
        ("TOOL_", "TOOL"),
        ("STRUCTURED_", "STRUCTURED_OUTPUT"),
        ("FIM_", "STRUCTURED_OUTPUT"),
        ("GROUNDING_", "GROUNDING"),
        ("CITATION_", "GROUNDING"),
        ("GENERATION_", "GENERATION"),
        ("GEN_", "GENERATION"),
        ("RELEASE_GATE_", "LIFECYCLE"),
        ("LIFECYCLE_", "LIFECYCLE"),
        ("RETENTION_", "LIFECYCLE"),
        ("DATASET_", "LIFECYCLE"),
        ("CAPABILITY_", "CAPABILITY"),
        ("PRECISION_", "CAPABILITY"),
        ("VISION_", "CAPABILITY"),
        ("EVAL_", "CAPABILITY"),
        ("QUANT_", "CAPABILITY"),
    };

    public static string Classify(string code)
    {
        code = (code ?? "").Trim().ToUpperInvariant();
        foreach (var (prefix, cat) in Rules)
            if (code.StartsWith(prefix, StringComparison.Ordinal))
                return cat;
        return "RUNTIME";   // SPECULATIVE_*, SERVE_*, MEMORY_*, etc.
    }
}

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
