// FeatureCatalog.cs ??``star-model-feature-catalog/v1`` (§31).
//
// Formal governance data (not a research report): the canonical mapping
// from external model inspirations to the xingcheng components that
// absorb their worthwhile designs. Each record states whether the
// absorption needs training (frozen), a generation change, or is pure
// runtime/contract work. The catalog is governance truth; the CLI only
// validates and reports it.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class FeatureCatalog
{
    public const string Format = "star-model-feature-catalog/v1";
    public const string Rel =
        "xingcheng/runtime/state/feature-catalog.json";

    public static readonly string[] Statuses =
    {
        "INTEGRATED", "ALREADY_NATIVE", "EXPERIMENTAL",
        "DEFERRED_TRAINING", "FUTURE_GENERATION", "REJECTED",
    };

    public sealed record Feature(
        string FeatureId, string SourceInspiration,
        string XingchengComponent, string Status,
        bool TrainingRequired, bool RuntimeRequired,
        bool GenerationChangeRequired, string Owner,
        string[] Tests);

    /// <summary>Canonical catalog ??the §31 mapping, embedded so the
    /// catalog is versioned with the code that implements it.</summary>
    public static readonly Feature[] Canonical =
    {
        new("f-qwen-agent", "Qwen3.8",
            "LongHorizonTaskCoordinator / ContextBudgetManager",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "caps-validate", "task checkpoint/resume" }),
        new("f-gemma-mtp", "Gemma4",
            "SpeculativeDecoder contract (disabled)",
            "EXPERIMENTAL", false, true, false, "cpp-runtime",
            new[] { "synthetic drafter probe" }),
        new("f-dsv-cache", "DeepSeek-V4.1",
            "InferenceMemoryPlanner / KV Budget Manager",
            "INTEGRATED", false, true, false, "cpp-runtime",
            new[] { "cache-smoke", "memplan" }),
        new("f-kimi-telemetry", "Kimi-K3",
            "MoERoutingAnalyzer / depth telemetry",
            "INTEGRATED", false, true, false, "cpp-runtime",
            new[] { "router quantiles", "depth norms" }),
        new("f-r1-reasoning", "DeepSeek-R1",
            "ReasoningPolicy (budgets only)",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "caps-validate" }),
        new("f-qwen-coder", "Qwen3-Coder",
            "StarCodeAgentRuntime / star-code-task/v1",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "code-task validate", "harness" }),
        new("f-llama4-modality", "Llama-4",
            "ModalityProvenance / TeacherLineage schema",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "modality record" }),
        new("f-ministral-deploy", "Ministral",
            "Deployment Profiles",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "caps-validate" }),
        new("f-phi4-data", "Phi-4",
            "DatasetQualityPipeline",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "quality pipeline" }),
        new("f-glm-gate", "GLM-5.3",
            "ARCHITECTURE_CHANGE_REQUIRED gate",
            "INTEGRATED", false, false, false, "governance",
            new[] { "gate evaluation" }),
        new("f-command-tools", "Command-R7B",
            "ToolDecisionGate / star-grounded-result/v1",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "tool gate", "grounding record" }),
        new("f-hermes-tool", "Qwen3.8+Hermes",
            "star-tool-call/v2",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "tool contract" }),
        new("f-storm-curation", "Llama-3.1-Storm",
            "Self-curation metadata in DatasetQualityPipeline",
            "INTEGRATED", false, false, false, "csharp-runtime",
            new[] { "quality pipeline" }),
        new("f-minicpm-vision", "MiniCPM-V",
            "VisionBudgetController (EXPERIMENTAL)",
            "EXPERIMENTAL", false, true, false, "cpp-runtime",
            new[] { "vision parity benchmark" }),
        new("f-internvl-eval", "InternVL-3.0",
            "Multimodal eval categories",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "eval suite" }),
        new("f-codegemma-fim", "CodeGemma",
            "star-fim/v1",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "fim envelope" }),
        new("f-dsv-flashcoding", "DeepSeek-V4.1-FlashCoding",
            "RepoTaskHarness",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "harness" }),
        new("f-rwkv-state", "RWKV-7/x070",
            "SequenceStateBenchmark / DeltaStateSnapshot",
            "INTEGRATED", false, true, false, "cpp-runtime",
            new[] { "state bench", "snapshot round-trip" }),
        new("f-zamba-reuse", "Zamba-2",
            "ParameterReuseProbe -> FutureArchitectureResearch",
            "EXPERIMENTAL", false, false, false, "cpp-runtime",
            new[] { "reuse probe report" }),
        new("f-granite-provenance", "Granite-4.0",
            "Bundle Provenance + optional signature",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "provenance-check" }),
        new("f-kda-attnres", "Kimi-K3",
            "KDA/AttnRes/Stable-LatentMoE into xc-fused-1",
            "REJECTED", false, false, true, "governance",
            Array.Empty<string>()),
        new("f-mamba2", "Zamba-2",
            "Mamba2 blocks into xc-fused-1",
            "REJECTED", false, false, true, "governance",
            Array.Empty<string>()),
        new("f-distill-train", "all sources",
            "Any distillation/merge/weight migration this phase",
            "DEFERRED_TRAINING", true, false, false, "governance",
            Array.Empty<string>()),
    };

    private static string Path_(string toolRoot)
        => Path.Combine(
            toolRoot, Rel.Replace('/', Path.DirectorySeparatorChar));

    public static Dictionary<string, object?> FeatureDict(Feature f)
        => new()
        {
            ["feature_id"] = f.FeatureId,
            ["source_inspiration"] = f.SourceInspiration,
            ["xingcheng_component"] = f.XingchengComponent,
            ["status"] = f.Status,
            ["training_required"] = f.TrainingRequired,
            ["runtime_required"] = f.RuntimeRequired,
            ["generation_change_required"] =
                f.GenerationChangeRequired,
            ["owner"] = f.Owner,
            ["tests"] = f.Tests.Cast<object?>().ToList(),
        };

    /// <summary>Persist the canonical catalog (atomic write) ??the file
    /// is the governance artifact; the code is its source of truth.</summary>
    public static Dictionary<string, object?> Emit(string toolRoot)
    {
        var features = new List<object?>();
        var byStatus = new Dictionary<string, int>();
        var bySource = new Dictionary<string, int>();
        foreach (var f in Canonical)
        {
            features.Add(FeatureDict(f));
            byStatus[f.Status] =
                byStatus.GetValueOrDefault(f.Status) + 1;
            bySource[f.SourceInspiration] =
                bySource.GetValueOrDefault(f.SourceInspiration) + 1;
        }
        var doc = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["generated_at"] = XcPaths.IsoNow(),
            ["active_generation"] =
                GenerationMigration.CurrentGeneration(toolRoot),
            ["capability_training_frozen"] = true,
            ["features"] = features,
            ["summary"] = new Dictionary<string, object?>
            {
                ["total"] = Canonical.Length,
                ["by_status"] = byStatus.ToDictionary(
                    kv => kv.Key, kv => (object?)kv.Value),
                ["by_source"] = bySource.ToDictionary(
                    kv => kv.Key, kv => (object?)kv.Value),
            },
        };
        Directory.CreateDirectory(Path.GetDirectoryName(Path_(toolRoot))!);
        ModelLifecycle.AtomicWrite(
            Path_(toolRoot), CanonicalJson.PrettyDict(doc) + "\n");
        doc["ok"] = true;
        doc["catalog_file"] = Rel;
        return doc;
    }

    /// <summary>Schema validation for a catalog file (any producer).</summary>
    public static Dictionary<string, object?> Validate(string file)
    {
        if (string.IsNullOrEmpty(file) || !File.Exists(file))
            throw new ExecutorError(
                "FEATURE_REGISTRY_INVALID", "catalog file missing");
        JsonElement root;
        try { root = JsonDocument.Parse(File.ReadAllText(file))
                                     .RootElement; }
        catch (JsonException)
        {
            throw new ExecutorError(
                "FEATURE_REGISTRY_INVALID", "catalog not valid json");
        }
        if (root.ValueKind != JsonValueKind.Object ||
            !root.TryGetProperty("format", out var f) ||
            f.GetString() != Format)
            throw new ExecutorError(
                "FEATURE_REGISTRY_INVALID", "bad catalog format");
        if (!root.TryGetProperty("features", out var feats) ||
            feats.ValueKind != JsonValueKind.Array)
            throw new ExecutorError(
                "FEATURE_REGISTRY_INVALID", "features missing");
        var ids = new HashSet<string>(StringComparer.Ordinal);
        var failures = new List<object?>();
        var required = new[]
            { "feature_id", "source_inspiration", "xingcheng_component",
              "status", "training_required", "runtime_required",
              "generation_change_required", "owner", "tests" };
        foreach (var el in feats.EnumerateArray())
        {
            string id = el.TryGetProperty("feature_id", out var i)
                ? i.GetString() ?? "" : "";
            foreach (var k in required)
            {
                if (!el.TryGetProperty(k, out _))
                    failures.Add(new Dictionary<string, object?>
                        { ["feature_id"] = id, ["missing"] = k });
            }
            if (id.Length == 0 || !ids.Add(id))
                failures.Add(new Dictionary<string, object?>
                    { ["feature_id"] = id, ["error"] = "duplicate" });
            if (el.TryGetProperty("status", out var s) &&
                s.ValueKind == JsonValueKind.String &&
                !Statuses.Contains(s.GetString()))
                failures.Add(new Dictionary<string, object?>
                    { ["feature_id"] = id,
                      ["error"] = $"bad status {s.GetString()}" });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = failures.Count == 0,
            ["format"] = Format,
            ["file"] = file,
            ["feature_count"] = feats.GetArrayLength(),
            ["failures"] = failures,
        };
    }
}
