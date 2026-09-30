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
