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
        // Efficiency plane (§50): runtime-only integration that never
        // touches weights/architecture/generation.
        "INTEGRATED_RUNTIME", "CERTIFIED_AFTER_BENCH",
        // Laya/MiMo absorption (§47): capability-plane surfaces are
        // schemas/gates, never weights or architecture.
        "EXPERIMENTAL_RUNTIME", "EXPERIMENTAL_CAPABILITY",
        "INTEGRATED_SCHEMA", "EXPERIMENTAL_TRAINING",
        "INTEGRATED_GOVERNANCE",
        // Silicon plane (§84): discovery-first surfaces and deferred
        // placements — PROBE_ONLY = real probe exists, no production
        // path claimed; DEFERRED = contract recorded, intentionally
        // not built this phase.
        "PROBE_ONLY", "DEFERRED",
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
        // ---- community fine-tuning phase (§32) ----
        new("f-command-grounding", "Command-R",
            "star-grounded-result/v2 + GroundingGate",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "grounded-v2 validate", "grounding gate" }),
        new("f-command-evidence", "Command-R",
            "DocumentEvidenceGraph",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "graph query", "conflict detection" }),
        new("f-command-necessity", "Command-R",
            "RetrievalDecisionGate -> ActionDecisionGate",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "rag-decide" }),
        new("f-hermes-persona", "Hermes-class",
            "PersonaRuntime + authority isolation",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "persona-validate", "injection guard" }),
        new("f-hermes-creative", "Hermes-class",
            "CreativeMode + StyleProfile",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "creative-profile", "style-profile" }),
        new("f-hermes-roleplay", "Hermes-class",
            "RoleplaySession + NarrativeMemory",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "roleplay session", "memory isolation" }),
        new("f-hermes-repair", "Hermes-class",
            "SchemaRepair (once, evidence-recorded)",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "structured-validate --repair" }),
        new("f-hermes-refusal", "Hermes-class",
            "RefusalDecisionGate + star-refusal-eval/v1",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "refusal-decide", "refusal-eval" }),
        new("f-storm-targeted", "Llama-3.1-Storm",
            "ModuleSensitivityRegistry (metadata only)",
            "DEFERRED_TRAINING", false, false, false,
            "csharp-runtime",
            new[] { "module-sensitivity" }),
        new("f-storm-merge", "Llama-3.1-Storm",
            "SLERP/TIES/DARE model merge",
            "REJECTED", false, false, true, "governance",
            Array.Empty<string>()),
        new("f-uncensored", "community-uncensored",
            "UncensoredMode / governance bypass",
            "REJECTED", false, false, true, "governance",
            Array.Empty<string>()),
        // ---- inference efficiency plane (§50) ----
        new("f-expert-residency", "vLLM/SGLang-class",
            "ExpertResidencyManager + async prefetch",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "expert-residency", "expert-offload-bench" }),
        new("f-expert-quant-storage", "vLLM/SGLang-class",
            "Quantized expert host storage (INT8/FP8/FP4)",
            "EXPERIMENTAL", false, false, false, "cpp-runtime",
            new[] { "expert-quant-parity" }),
        new("f-hybrid-prefix", "vLLM/SGLang-class",
            "HybridPrefixCache v2 (KV + DeltaStateSnapshot)",
            "INTEGRATED", false, true, false, "cpp-runtime",
            new[] { "hybrid-prefix-smoke", "rag-prefix-bench" }),
        new("f-rag-prefix", "vLLM/SGLang-class",
            "RagPrefixManifest + RAG evidence cache",
            "INTEGRATED", false, true, false, "csharp-runtime",
            new[] { "rag-prefix-manifest", "evidence-cache" }),
        new("f-pd-disaggregation", "vLLM/SGLang-class",
            "PrefillDecodeScheduler + star-prefill-artifact/v1",
            "CERTIFIED_AFTER_BENCH", false, true, false,
            "cpp-runtime",
            new[] { "pd-pipeline-bench", "prefill-artifact" }),
        // ---- Laya + MiMo-V2.6 absorption (§47) ----
        new("f-system1-head", "Laya",
            "NativeSystemOneHead + star-typed-decision/v1",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "cpp-runtime",
            new[] { "system1-smoke", "system1-abstention" }),
        new("f-decision-calibration", "Laya",
            "DecisionCalibrationLayer + star-decision-calibration/v1",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "system1-calibration" }),
        new("f-typed-decision", "Laya",
            "TypedDecision contract (BOOLEAN/CHOICE/ORDINAL/CONFIDENCE)",
            "EXPERIMENTAL_CAPABILITY", false, true, false,
            "csharp-runtime",
            new[] { "system1-choice", "system1-score" }),
        new("f-agent-trajectory", "MiMo-V2.6",
            "star-agent-trajectory/v1 (unified agent schema)",
            "INTEGRATED_SCHEMA", false, true, false,
            "csharp-runtime",
            new[] { "trajectory-schema" }),
        new("f-multi-harness", "MiMo-V2.6",
            "HarnessRegistry + seen/unseen overfit detection",
            "EXPERIMENTAL_TRAINING", false, true, false,
            "csharp-runtime",
            new[] { "multi-harness-eval" }),
        new("f-router-stability", "MiMo-V2.6",
            "RouterStabilityPolicy + RouterStabilityGate",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "router-stability" }),
        new("f-groupwise-grading", "MiMo-V2.6",
            "GroupwiseTrajectoryEvaluator + cost-aware reward",
            "EXPERIMENTAL_TRAINING", false, true, false,
            "csharp-runtime",
            new[] { "groupwise-eval" }),
        new("f-reward-integrity", "MiMo-V2.6",
            "RewardIntegrityGate (grader->verifier->adversarial)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "reward-integrity" }),
        new("f-mtp-drafter", "MiMo-V2.6",
            "NativeMtpDrafter (main-decoder verified speculation)",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "cpp-runtime",
            new[] { "mtp-runtime", "mtp-speedup" }),
        // ---- NativeScaleEfficiencyPlane (scale directive §50) ----
        new("f-scale-metrics", "scale directive",
            "star-scale-metrics/v1 seven-parameter accounting",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "scale-metrics", "scale-status" }),
        new("f-mapped-expert-store", "scale directive",
            "MappedExpertStore XEB1 (block-aligned, checksummed, " +
            "generation/bundle-bound)",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "expert-store-build", "expert-store-read" }),
        new("f-expert-prefetch", "scale directive",
            "ExpertPrefetchPlanner (router hints + transition + " +
            "session affinity)",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "prefetch-probe" }),
        new("f-scale-tiers", "scale directive",
            "DEVICE_HOT/HOST_WARM/NVME_COLD expert tiers",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "low-resource-sim" }),
        new("f-delta-precision", "scale directive",
            "DeltaStatePrecisionProbe fp64->bf16->fp16",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "delta-precision-probe" }),
        new("f-scale-planner", "scale directive",
            "NativeScalePlanner ranking + ScaleHardwareGate + " +
            "TrainingWorkingSetPlanner",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "scale-sim", "scale-hardware-gate" }),
        new("f-resource-cert", "scale directive",
            "star-resource-cert/v1 + star-scale-promotion-gate/v1",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "scale-resource-cert", "scale-promotion-gate" }),
        new("f-factorized-experts", "scale directive",
            "FactorizedExpertBank (shape/memory estimators only)",
            "FUTURE_GENERATION", false, false, false,
            "cpp-runtime",
            new[] { "future-scale-probe" }),
        new("f-conditional-depth", "scale directive",
            "ConditionalDepthProbe (FUTURE_ARCHITECTURE_CANDIDATE)",
            "FUTURE_GENERATION", false, false, false,
            "cpp-runtime",
            new[] { "future-scale-probe" }),
        new("f-cross-layer-sharing", "scale directive",
            "SharedBlockProbe (parameter/memory estimation only)",
            "FUTURE_GENERATION", false, false, false,
            "cpp-runtime",
            new[] { "future-scale-probe" }),
        // ---- NativeMemoryCudaPlane (memory/CUDA directive) ----
        new("f-unified-memory-manager", "memory/cuda directive",
            "UnifiedCudaMemoryManager + 5-tier lifetime",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "memplane-probe", "cuda-plane-checks" }),
        new("f-cuda-device-pool", "memory/cuda directive",
            "CudaDevicePool (MallocAsync, high-water reuse, no trim)",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "memplane-probe" }),
        new("f-pinned-host-pool", "memory/cuda directive",
            "PinnedHostPool ring (bounded max_pinned_host_bytes)",
            "INTEGRATED_RUNTIME", false, true, false, "cpp-runtime",
            new[] { "memplane-probe" }),
        new("f-memplane-pressure", "memory/cuda directive",
            "CudaMemoryBudget + 8-step pressure ladder",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "cuda-plane-checks" }),
        new("f-cuda-graph-decode", "memory/cuda directive",
            "DecodeCudaGraph (batch-1/known-batch capture+replay)",
            "EXPERIMENTAL", false, true, false, "cpp-runtime",
            new[] { "decode-graph-parity" }),
        new("f-bf16-production", "memory/cuda directive",
            "BF16 production compute primary (FP64 oracle only)",
            "EXPERIMENTAL", false, true, false, "cpp-runtime",
            new[] { "bf16-certification" }),
        // ---- NativeSiliconEfficiencyPlane (silicon directive §84) --
        new("f-npu-backend", "silicon directive",
            "WindowsMlNpuBackend EP discovery (probe-first)",
            "PROBE_ONLY", false, true, false, "cpp-runtime",
            new[] { "npu-discovery" }),
        new("f-npu-system1", "silicon directive",
            "System-1 typed decisions on NPU when present",
            "CERTIFIED_AFTER_BENCH", false, true, false,
            "cpp-runtime",
            new[] { "npu-system1-bench" }),
        new("f-npu-embedding", "silicon directive",
            "Embedding on NPU when present",
            "CERTIFIED_AFTER_BENCH", false, true, false,
            "cpp-runtime",
            new[] { "npu-embedding-bench" }),
        new("f-npu-reranker", "silicon directive",
            "Reranker on NPU when present",
            "CERTIFIED_AFTER_BENCH", false, true, false,
            "cpp-runtime",
            new[] { "npu-embedding-bench" }),
        new("f-npu-prefill", "silicon directive",
            "NpuPrefillProbe (static-shape chunk prefill)",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "cpp-runtime",
            new[] { "npu-prefill-bench" }),
        new("f-npu-decoder", "silicon directive",
            "Full HybridCausalDecoder on NPU — deferred (dynamic "
            + "DeltaNet/MoE/KV state unsuitable for first wave)",
            "DEFERRED", false, false, false, "governance",
            Array.Empty<string>()),
        new("f-runtime-host", "silicon directive",
            "XingchengRuntimeHost single-owner + "
            "SystemArtifactRegistry",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "single-runtime-owner", "artifact-dedup" }),
        new("f-silicon-broker", "silicon directive",
            "SiliconExecutionBroker op->device routing + QoS",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "silicon-routing-bench" }),
        new("f-cpu-scheduler", "silicon directive",
            "CpuCoreScheduler (work classes, P/E + SMT aware)",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "cpu-affinity-probe", "cpu-bf16-bench" }),
        new("f-parameter-freeze", "silicon directive",
            "star-parameter-freeze-map/v1 + sparse optimizer state",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "parameter-freeze-probe",
                    "sparse-optimizer-probe" }),
        new("f-expert-granularity", "silicon directive",
            "ExpertGranularityPlanner (top-k fixed, aligned widths)",
            "EXPERIMENTAL_TRAINING", false, true, false,
            "csharp-runtime",
            new[] { "expert-granularity-probe" }),
        new("f-param-efficiency", "silicon directive",
            "star-parameter-efficiency/v1 accounting",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "parameter-efficiency-report" }),
    };

    private static string Path_(string toolRoot)
        => Path.Combine(
            toolRoot, Rel.Replace('/', Path.DirectorySeparatorChar));

    public static Dictionary<string, object?> FeatureDict(Feature f)
    {
        // §14: every feature carries exactly one primary axis under
        // star-architecture-taxonomy/v1.
        var cls = ArchitectureTaxonomy.Classify(f.XingchengComponent);
        return new()
        {
            ["feature_id"] = f.FeatureId,
            ["source_inspiration"] = f.SourceInspiration,
            ["xingcheng_component"] = f.XingchengComponent,
            ["status"] = f.Status,
            ["primary_axis"] = cls.PrimaryAxis,
            ["secondary_tags"] = cls.SecondaryTags
                .Cast<object?>().ToList(),
            ["architecture_affecting"] = cls.ArchitectureAffecting,
            ["checkpoint_affecting"] = cls.CheckpointAffecting,
            ["runtime_only"] = cls.RuntimeOnly,
            ["training_only"] = cls.TrainingOnly,
            ["capability_only"] = cls.CapabilityOnly,
            ["training_required"] = f.TrainingRequired,
            ["runtime_required"] = f.RuntimeRequired,
            ["generation_change_required"] =
                f.GenerationChangeRequired,
            ["owner"] = f.Owner,
            ["tests"] = f.Tests.Cast<object?>().ToList(),
        };
    }

    /// <summary>Persist the canonical catalog (atomic write) ??the file
    /// is the governance artifact; the code is its source of truth.</summary>
    public static Dictionary<string, object?> Emit(string toolRoot)
    {
        var features = new List<object?>();
        var byStatus = new Dictionary<string, int>();
        var bySource = new Dictionary<string, int>();
        var byAxis = new Dictionary<string, int>();
        foreach (var f in Canonical)
        {
            var fd = FeatureDict(f);
            features.Add(fd);
            byStatus[f.Status] =
                byStatus.GetValueOrDefault(f.Status) + 1;
            bySource[f.SourceInspiration] =
                bySource.GetValueOrDefault(f.SourceInspiration) + 1;
            string ax = (string)fd["primary_axis"]!;
            byAxis[ax] = byAxis.GetValueOrDefault(ax) + 1;
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
                ["by_primary_axis"] = byAxis.ToDictionary(
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
            // §14 taxonomy: when primary_axis is present it must be one
            // of the legal axes; exactly one primary is implied by the
            // schema (single string field).
            if (el.TryGetProperty("primary_axis", out var pa))
            {
                if (pa.ValueKind != JsonValueKind.String ||
                    !ArchitectureTaxonomy.Axes.Contains(
                        pa.GetString()))
                    failures.Add(new Dictionary<string, object?>
                        { ["feature_id"] = id,
                          ["error"] =
                              $"bad primary_axis {pa}" });
            }
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
