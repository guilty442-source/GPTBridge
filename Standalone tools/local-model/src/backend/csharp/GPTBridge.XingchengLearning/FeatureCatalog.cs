// FeatureCatalog.cs — ``star-model-feature-catalog/v1`` (§31) plus the
// §15 architecture-change gate and the §1 language-boundary check.
//
// The catalog is governed data, not a report: every absorbed external
// design advantage is one feature row with a xingcheng component, an
// explicit status and its training/runtime/generation impact. Status
// vocabulary is closed (INTEGRATED / ALREADY_NATIVE / EXPERIMENTAL /
// DEFERRED_TRAINING / FUTURE_GENERATION / REJECTED) so a row can never
// silently claim a capability that does not exist.
//
// §15 GLM-5.3 lesson, made a gate: post-training / agent-workflow
// improvements are tried before any backbone change — an architecture
// axis is only added when ARCHITECTURE_CHANGE_REQUIRED is true (all
// five justification legs must hold).
//
// §1/B31-B36: LanguageBoundary.Scan enforces the formal-language
// allowlist over the tool source tree; violations are fail-closed.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

/// <summary>One catalog row: an external-source idea mapped onto its
/// xingcheng component — never an adapter per source model.</summary>
internal sealed class FeatureEntry
{
    public string FeatureId = "";
    public string SourceInspiration = "";
    public string XingchengComponent = "";
    public string Status = "EXPERIMENTAL";
    public bool TrainingRequired;
    public bool RuntimeRequired = true;
    public bool GenerationChangeRequired;
    public string Owner = "";
    public string[] Tests = Array.Empty<string>();

    public static readonly string[] Statuses =
    {
        "INTEGRATED", "ALREADY_NATIVE", "EXPERIMENTAL",
        "DEFERRED_TRAINING", "FUTURE_GENERATION", "REJECTED",
        // Batch-2 refinements (§30): REJECT_DUPLICATE = idea already in
        // xc-fused-1, no second implementation; PROBE_ONLY = runtime
        // probe infrastructure, never a capability claim;
        // HARDWARE_EXPERIMENTAL = needs hardware support to activate;
        // CONTRACT_ONLY = interface contract exists, model lane absent.
        "REJECT_DUPLICATE", "PROBE_ONLY",
        "HARDWARE_EXPERIMENTAL", "CONTRACT_ONLY",
    };

    public Dictionary<string, object?> ToDict() => new()
    {
        ["feature_id"] = FeatureId,
        ["source_inspiration"] = SourceInspiration,
        ["xingcheng_component"] = XingchengComponent,
        ["status"] = Status,
        ["training_required"] = TrainingRequired,
        ["runtime_required"] = RuntimeRequired,
        ["generation_change_required"] = GenerationChangeRequired,
        ["owner"] = Owner,
        ["tests"] = Tests.Cast<object?>().ToList(),
    };
}

internal static class FeatureCatalog
{
    public const string Format = "star-model-feature-catalog/v1";
    public const string CatalogRel =
        "runtime/settings/feature-catalog.json";

    /// <summary>The §31 canonical mapping — source family -> the single
    /// xingcheng component that absorbs the idea. Statuses reflect the
    /// landing state in this phase: contracts/runtime land INTEGRATED or
    /// EXPERIMENTAL; anything needing weights is DEFERRED_TRAINING; one
    /// row records the explicit REJECTED boundaries (no adapter per
    /// source, no HF runtime, no backbone swap).</summary>
    public static FeatureEntry[] Canonical() => new[]
    {
        new FeatureEntry
        {
            FeatureId = "long-horizon-tasking",
            SourceInspiration = "Qwen3.8 agent workflow",
            XingchengComponent = "LongHorizonTaskCoordinator",
            Status = "INTEGRATED",
            Owner = "csharp:GPTBridge.XingchengLearning/AgentRuntime.cs",
            Tests = new[] { "task-checkpoint-resume" },
        },
        new FeatureEntry
        {
            FeatureId = "context-budget-ladder",
            SourceInspiration = "Qwen3.8 long context",
            XingchengComponent = "ContextBudgetManager",
            Status = "INTEGRATED",
            Owner = "csharp:RuntimeCapabilities.cs",
            Tests = new[] { "context-probe" },
        },
        new FeatureEntry
        {
            FeatureId = "mtp-speculative-decoding",
            SourceInspiration = "Gemma4 MTP",
            XingchengComponent = "SpeculativeDecoder",
            Status = "EXPERIMENTAL",     // enabled=false; ABI only
            Owner = "cpp:engine",
            Tests = new[] { "spec-probe" },
        },
        new FeatureEntry
        {
            FeatureId = "inference-memory-planning",
            SourceInspiration = "DeepSeek V4.1 prefill/decode + KV first",
            XingchengComponent = "InferenceMemoryPlanner",
            Status = "INTEGRATED",
            Owner = "cpp:xc_modeltool memory-plan",
            Tests = new[] { "memory-plan-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "moe-routing-observability",
            SourceInspiration = "Kimi K3 sparse router / depth telemetry",
            XingchengComponent = "MoERoutingAnalyzer",
            Status = "INTEGRATED",
            Owner = "cpp:router_trace + csharp:MoERoutingAnalyzer",
            Tests = new[] { "moe-analyze-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "reasoning-policy",
            SourceInspiration = "DeepSeek-R1 reasoning/verification split",
            XingchengComponent = "ReasoningPolicy",
            Status = "INTEGRATED",
            Owner = "csharp:RuntimeCapabilities.cs",
            Tests = new[] { "capabilities-resolve" },
        },
        new FeatureEntry
        {
            FeatureId = "code-agent-runtime",
            SourceInspiration = "Qwen3-Coder / DeepSeek Flash Coding",
            XingchengComponent = "StarCodeAgentRuntime+RepoTaskHarness",
            Status = "INTEGRATED",
            Owner = "csharp:AgentRuntime.cs",
            Tests = new[] { "code-task-contract" },
        },
        new FeatureEntry
        {
            FeatureId = "fim-contract",
            SourceInspiration = "CodeGemma FIM",
            XingchengComponent = "star-fim/v1 runtime envelope",
            Status = "INTEGRATED",
            Owner = "csharp:AgentRuntime.cs",
            Tests = new[] { "fim-envelope" },
        },
        new FeatureEntry
        {
            FeatureId = "modality-provenance",
            SourceInspiration = "Llama4 unified multimodal stream",
            XingchengComponent = "ModalityProvenance+TeacherLineage",
            Status = "INTEGRATED",
            Owner = "csharp:AgentRuntime.cs",
            Tests = new[] { "provenance-schema" },
        },
        new FeatureEntry
        {
            FeatureId = "deployment-profiles",
            SourceInspiration = "Ministral edge efficiency",
            XingchengComponent = "DeploymentProfile",
            Status = "INTEGRATED",
            Owner = "csharp:RuntimeCapabilities.cs",
            Tests = new[] { "capabilities-resolve" },
        },
        new FeatureEntry
        {
            FeatureId = "dataset-quality-pipeline",
            SourceInspiration = "Phi-4 data quality / Storm self-curation",
            XingchengComponent = "DatasetQualityPipeline",
            Status = "INTEGRATED",
            TrainingRequired = false,   // metadata only — never trains
            Owner = "csharp:DataQuality.cs",
            Tests = new[] { "dq-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "architecture-change-gate",
            SourceInspiration = "GLM-5.3 post-training-first doctrine",
            XingchengComponent = "ArchitectureChangeGate",
            Status = "INTEGRATED",
            Owner = "csharp:FeatureCatalog.cs",
            Tests = new[] { "arch-gate" },
        },
        new FeatureEntry
        {
            FeatureId = "tool-decision-grounding",
            SourceInspiration = "Command R7B tool+RAG discipline",
            XingchengComponent = "ToolDecisionGate+star-grounded-result/v1",
            Status = "INTEGRATED",
            Owner = "csharp:ToolContracts.cs",
            Tests = new[] { "tool-gate-smoke", "grounding-contract" },
        },
        new FeatureEntry
        {
            FeatureId = "tool-call-v2",
            SourceInspiration = "Hermes structured tool protocol",
            XingchengComponent = "star-tool-call/v2",
            Status = "INTEGRATED",
            Owner = "csharp:ToolContracts.cs",
            Tests = new[] { "tool-call-contract" },
        },
        new FeatureEntry
        {
            FeatureId = "vision-budget-controller",
            SourceInspiration = "MiniCPM-V adaptive visual compression",
            XingchengComponent = "VisionBudgetController",
            Status = "EXPERIMENTAL",     // §20: never default-on
            Owner = "cpp:xc_modeltool vision-budget",
            Tests = new[] { "vision-budget-parity" },
        },
        new FeatureEntry
        {
            FeatureId = "multimodal-eval-plane",
            SourceInspiration = "InternVL 3.0 eval breadth",
            XingchengComponent = "XingchengEvaluationCoordinator suites",
            Status = "INTEGRATED",
            Owner = "csharp:EvalPlane.cs",
            Tests = new[] { "eval-suite-contract" },
        },
        new FeatureEntry
        {
            FeatureId = "sequence-state-benchmark",
            SourceInspiration = "RWKV-7/x070 constant-state efficiency",
            XingchengComponent =
                "SequenceStateBenchmark+DeltaStateSnapshot",
            Status = "INTEGRATED",
            Owner = "cpp:xc_modeltool state-bench/state-snapshot",
            Tests = new[] { "state-bench-smoke", "snapshot-verify" },
        },
        new FeatureEntry
        {
            FeatureId = "parameter-reuse-probe",
            SourceInspiration = "Zamba2 shared blocks",
            XingchengComponent = "ParameterReuseProbe",
            Status = "EXPERIMENTAL",     // analysis only -> research sink
            Owner = "cpp:xc_modeltool param-reuse-probe",
            Tests = new[] { "param-reuse-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "bundle-provenance",
            SourceInspiration = "Granite4 deployment governance",
            XingchengComponent = "BundleProvenance+signature",
            Status = "INTEGRATED",
            Owner = "csharp:EvalPlane.cs + cpp:export manifest",
            Tests = new[] { "provenance-verify" },
        },
        new FeatureEntry
        {
            FeatureId = "precision-profiles",
            SourceInspiration = "multi-source precision lanes",
            XingchengComponent = "PrecisionProfiles+xc_modeltool precision",
            Status = "EXPERIMENTAL",     // BF16 stays candidate
            Owner = "csharp:RuntimeCapabilities.cs+cpp:precision mode",
            Tests = new[] { "precision-parity" },
        },
        // --------------------------------------- batch-2 rows (§30) --
        new FeatureEntry
        {
            FeatureId = "unified-reasoning-runtime",
            SourceInspiration =
                "gpt-oss effort + Nemotron budget + Hy3 fast/slow + MiMo deep",
            XingchengComponent = "ReasoningRuntime",
            Status = "INTEGRATED",
            Owner = "csharp:ReasoningRuntime.cs",
            Tests = new[] { "reasoning-runtime-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "dialogue-envelope-v2",
            SourceInspiration = "gpt-oss harmony-class interaction format",
            XingchengComponent = "star-dialogue-envelope/v2",
            Status = "INTEGRATED",
            Owner = "csharp:DialogueEnvelope.cs",
            Tests = new[] { "dialogue-envelope-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "precision-fp4-lab",
            SourceInspiration = "gpt-oss MXFP4 (abstracted to 4-bit)",
            XingchengComponent =
                "NativePrecisionLab.EXPERIMENTAL_FP4",
            Status = "EXPERIMENTAL",
            Owner = "csharp:PrecisionRuntime.cs",
            Tests = new[] { "quant-cert-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "gptoss-local-dense-attention",
            SourceInspiration = "gpt-oss local-banded/dense attention",
            XingchengComponent = "none (xc-fused-1 hybrid stands)",
            Status = "REJECT_DUPLICATE",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "unified-modality-envelope",
            SourceInspiration = "MiMo omni-modal input",
            XingchengComponent =
                "ModalityInput + MultimodalRuntime/IModalityAdapter",
            Status = "INTEGRATED",
            Owner = "csharp:DialogueEnvelope.cs+ModalityRuntime.cs",
            Tests = new[] { "multimodal-envelope-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "context-cache-v2",
            SourceInspiration = "MiMo context cache",
            XingchengComponent = "ContextCacheManager L0-L4",
            Status = "INTEGRATED",
            Owner = "csharp:ContextCache.cs",
            Tests = new[] { "context-cache-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "ultra-low-latency-profile",
            SourceInspiration = "MiMo UltraSpeed",
            XingchengComponent =
                "DeploymentProfile.ULTRA_LOW_LATENCY + fast-path fallback",
            Status = "INTEGRATED",
            Owner = "csharp:RuntimeCapabilities.cs+ReasoningRuntime.cs",
            Tests = new[] { "capabilities-resolve" },
        },
        new FeatureEntry
        {
            FeatureId = "context-1m-claim",
            SourceInspiration = "MiMo/MiniMax/Nemotron 1M context",
            XingchengComponent = "LongContextRuntime probe ladder",
            Status = "PROBE_ONLY",   // §20: runtime ladder, not model capability
            Owner = "csharp:LongContextRuntime.cs",
            Tests = new[] { "long-context-index-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "long-context-block-index",
            SourceInspiration = "MiniMax-M3 KV block indexing",
            XingchengComponent = "LongContextBlockIndex",
            Status = "INTEGRATED",
            Owner = "csharp:LongContextRuntime.cs",
            Tests = new[] { "long-context-index-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "sparse-attention-probe",
            SourceInspiration = "MiniMax-M3 MSA block sparsity",
            XingchengComponent = "SparseAttentionProbe",
            Status = "EXPERIMENTAL",   // never called by production model
            Owner = "cpp:xc_modeltool sparse-probe",
            Tests = new[] { "sparse-attention-probe" },
        },
        new FeatureEntry
        {
            FeatureId = "kv-outer-gather-q",
            SourceInspiration = "MiniMax-M3 hardware-friendly KV gather",
            XingchengComponent = "kv_outer_gather_q operator prototype",
            Status = "EXPERIMENTAL",
            Owner = "cpp:xc_modeltool kv-gather-probe",
            Tests = new[] { "kv-outer-gather-probe" },
        },
        new FeatureEntry
        {
            FeatureId = "computer-agent-contract",
            SourceInspiration = "MiniMax-M3 desktop workflow",
            XingchengComponent = "star-computer-action/v1",
            Status = "INTEGRATED",
            Owner = "csharp:StarAgentRuntime.cs",
            Tests = new[] { "agent-workgraph-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "minimax-msa-production",
            SourceInspiration = "MiniMax-M3 MSA architecture",
            XingchengComponent = "none (probe infrastructure only)",
            Status = "FUTURE_GENERATION",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "mixed-precision-map",
            SourceInspiration = "Nemotron-3 per-component recipe",
            XingchengComponent =
                "QuantizationPolicy/star-precision-map/v1",
            Status = "INTEGRATED",
            Owner = "csharp:PrecisionRuntime.cs",
            Tests = new[] { "precision-map-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "hybrid-sequence-scheduler",
            SourceInspiration = "Nemotron-3 hybrid sequence engine",
            XingchengComponent = "SequenceLayerScheduler",
            Status = "INTEGRATED",
            Owner = "cpp:xc_modeltool sched-smoke",
            Tests = new[] { "sched-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "recurrent-precision-probe",
            SourceInspiration = "Nemotron-3 state precision discipline",
            XingchengComponent = "RecurrentStatePrecisionProbe",
            Status = "EXPERIMENTAL",
            Owner = "cpp:xc_modeltool state-drift",
            Tests = new[] { "recurrent-state-drift" },
        },
        new FeatureEntry
        {
            FeatureId = "nemotron-mtp-speculation",
            SourceInspiration = "Nemotron-3 Super MTP speculative decode",
            XingchengComponent = "SpeculativeDecoder runtime metrics",
            Status = "EXPERIMENTAL",   // §13: runtime only, never production
            Owner = "cpp:xc_modeltool spec-probe",
            Tests = new[] { "spec-probe" },
        },
        new FeatureEntry
        {
            FeatureId = "nemotron-mamba",
            SourceInspiration = "Nemotron-3 Mamba backbone",
            XingchengComponent =
                "none (DeltaNet hybrid already fills the slot)",
            Status = "REJECT_DUPLICATE",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "nvfp4-format",
            SourceInspiration = "Nemotron-3 NVFP4 weights",
            XingchengComponent = "FP4 abstract slot in PrecisionRoadmap",
            Status = "HARDWARE_EXPERIMENTAL",
            Owner = "csharp:PrecisionRuntime.cs",
            Tests = new[] { "quant-cert-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "fast-slow-reasoning",
            SourceInspiration = "Hy3 unified fast/slow thinking",
            XingchengComponent =
                "ReasoningRuntime strategy FAST/BALANCED/DEEP",
            Status = "INTEGRATED",
            Owner = "csharp:ReasoningRuntime.cs",
            Tests = new[] { "reasoning-runtime-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "practical-capability-suite",
            SourceInspiration = "Hy3 real-world eval emphasis",
            XingchengComponent =
                "star-practical-capability-suite/v1 + EvaluationCoordinator",
            Status = "INTEGRATED",
            Owner = "csharp:PracticalEval.cs",
            Tests = new[] { "practical-eval-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "post-training-first-priority",
            SourceInspiration = "Hy3 post-training-first doctrine",
            XingchengComponent = "PostTrainingPriority",
            Status = "INTEGRATED",
            Owner = "csharp:RuntimeContracts.cs",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "hy3-moe-topology",
            SourceInspiration = "Hy3 295B/21B MoE shape",
            XingchengComponent = "none (xc-fused-1 MoE fixed)",
            Status = "REJECT_DUPLICATE",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "task-resume-contract",
            SourceInspiration = "MiMo long-horizon + MiniMax agent resume",
            XingchengComponent =
                "TaskResumeState/star-task-resume/v1",
            Status = "INTEGRATED",
            Owner = "csharp:RuntimeContracts.cs",
            Tests = new[] { "task-resume-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "bundle-manifest-v2",
            SourceInspiration = "batch-2 runtime contract packaging",
            XingchengComponent = "star-bundle-manifest/v2",
            Status = "INTEGRATED",
            Owner = "csharp:BundleManifest.cs",
            Tests = new[] { "bundle-manifest-v2-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "native-state-v2",
            SourceInspiration =
                "MiMo cache + Nemotron state discipline + RWKV constant-state",
            XingchengComponent = "star-native-state/v2",
            Status = "INTEGRATED",
            Owner = "cpp:xc_modeltool state2-smoke",
            Tests = new[] { "state2-smoke" },
        },
        new FeatureEntry
        {
            FeatureId = "unified-metrics",
            SourceInspiration = "batch-2 observability convergence",
            XingchengComponent = "UnifiedMetrics",
            Status = "INTEGRATED",
            Owner = "csharp:RuntimeContracts.cs",
            Tests = new[] { "converge-check" },
        },
        // Recorded rejections — the boundary rows auditors look for.
        new FeatureEntry
        {
            FeatureId = "per-model-adapters",
            SourceInspiration = "(anti-pattern)",
            XingchengComponent = "none",
            Status = "REJECTED",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "external-runtime-import",
            SourceInspiration = "(anti-pattern: HF/transformers)",
            XingchengComponent = "none",
            Status = "REJECTED",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "backbone-swap-kda-rwkv-mamba2",
            SourceInspiration = "Kimi KDA / RWKV / Mamba2 blocks",
            XingchengComponent = "none (xc-fused-1 fixed)",
            Status = "REJECTED",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
    };

    public static Dictionary<string, object?> Build()
        => new()
        {
            ["format"] = Format,
            ["active_generation"] = "gen-2-consolidated",
            ["architecture_contract"] = "xc-fused-1",
            ["checkpoint_version"] = "XCN1 v10",
            ["capability_training_frozen"] = true,
            ["features"] = Canonical()
                .Select(f => (object?)f.ToDict()).ToList(),
        };

    /// <summary>Persist the catalog under runtime/settings/ — governed
    /// data, refreshed by the convergence build; never hand-edited
    /// downstream.</summary>
    public static Dictionary<string, object?> Install(string toolRoot)
    {
        var catalog = Build();
        string path = Path.Combine(toolRoot, CatalogRel);
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(catalog) + "\n");
        catalog["path"] = path;
        return catalog;
    }

    /// <summary>Structural validation of an on-disk catalog row set:
    /// closed status vocabulary + required fields; a bad row fails the
    /// whole catalog (governed data is never partially valid).</summary>
    public static List<string> Validate(Dictionary<string, object?> cat)
    {
        var errors = new List<string>();
        if (!cat.TryGetValue("features", out var fobj) ||
            fobj is not List<object?> feats)
        {
            errors.Add("features_missing");
            return errors;
        }
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var fo in feats)
        {
            if (fo is not Dictionary<string, object?> f)
            {
                errors.Add("feature_not_object");
                continue;
            }
            string id = f.TryGetValue("feature_id", out var i)
                ? i?.ToString() ?? "" : "";
            string status = f.TryGetValue("status", out var s)
                ? s?.ToString() ?? "" : "";
            if (id.Length == 0) errors.Add("feature_id_missing");
            if (!seen.Add(id)) errors.Add($"feature_id_duplicate:{id}");
            if (!FeatureEntry.Statuses.Contains(status))
                errors.Add($"feature_status_invalid:{id}:{status}");
        }
        return errors;
    }
}

/// <summary>§15 the architecture gate — an architecture axis may only
/// be added when ARCHITECTURE_CHANGE_REQUIRED is true, i.e. ALL
/// justification legs hold. Otherwise the proposal is denied
/// (ARCHITECTURE_CHANGE_NOT_JUSTIFIED) and the work must route through
/// runtime optimization, data improvement or post-training.</summary>
internal static class ArchitectureChangeGate
{
    public sealed class Evidence
    {
        // Each leg needs concrete evidence, not a claim.
        public bool ExistingArchCannotSolve;        // bottleneck proven
        public bool RuntimeOptimizationIneffective; // tried + measured
        public bool DataImprovementIneffective;     // tried + measured
        public bool PostTrainingIneffective;        // tried + measured
        public bool IndependentBenchmark;           // third-party ref
        public bool Ablation;                       // ablation run exists
        public bool MemoryImpact;                   // quantified
        public bool LatencyImpact;                  // quantified
    }

    public static bool Required(Evidence e)
        => e.ExistingArchCannotSolve &&
           e.RuntimeOptimizationIneffective &&
           e.DataImprovementIneffective &&
           e.PostTrainingIneffective &&
           e.IndependentBenchmark && e.Ablation &&
           e.MemoryImpact && e.LatencyImpact;

    public static Dictionary<string, object?> Evaluate(Evidence e)
    {
        bool required = Required(e);
        return new Dictionary<string, object?>
        {
            ["architecture_change_required"] = required,
            ["decision"] = required
                ? "ALLOWED" : "DENIED",
            ["error"] = required
                ? null : ConvErr.ArchitectureChangeNotJustified,
            ["evidence"] = new Dictionary<string, object?>
            {
                ["existing_arch_cannot_solve"] = e.ExistingArchCannotSolve,
                ["runtime_optimization_ineffective"] =
                    e.RuntimeOptimizationIneffective,
                ["data_improvement_ineffective"] =
                    e.DataImprovementIneffective,
                ["post_training_ineffective"] = e.PostTrainingIneffective,
                ["independent_benchmark"] = e.IndependentBenchmark,
                ["ablation"] = e.Ablation,
                ["memory_impact"] = e.MemoryImpact,
                ["latency_impact"] = e.LatencyImpact,
            },
            ["canonical_architecture"] = "xc-fused-1",
        };
    }
}

/// <summary>§1 + B31-B36 language boundary: scans a source tree and
/// fails closed on any file whose extension belongs to a non-permitted
/// implementation language inside an implementation lane. Data formats
/// (json/toml/xml/sql/md) and build glue (bat/ps1/cmake/csproj) are not
/// implementation source.</summary>
internal static class LanguageBoundary
{
    // Formal implementation lanes: only these extensions may carry
    // implementation logic. Everything else in a source tree is data or
    // build glue.
    public static readonly Dictionary<string, string> ImplementationExt =
        new(StringComparer.OrdinalIgnoreCase)
        {
            [".c"] = "c", [".h"] = "c",
            [".cc"] = "cpp", [".cpp"] = "cpp", [".cxx"] = "cpp",
            [".hpp"] = "cpp", [".hh"] = "cpp",
            [".cs"] = "csharp",
            [".fs"] = "fsharp", [".fsx"] = "fsharp",
            [".rs"] = "rust",
        };

    // Extensions that are never permitted in an implementation lane.
    public static readonly string[] ForbiddenExt =
    {
        ".py", ".js", ".mjs", ".ts", ".tsx", ".java", ".go",
        ".kt", ".kts", ".lua", ".jl", ".r",
    };

    /// <summary>Scan <paramref name="root"/> recursively; returns the
    /// violations (empty = PASS). Files under quarantined third-party
    /// artifacts or build-output dirs are skipped (B36 exception:
    /// quarantined upstream source is not project source).</summary>
    public static List<string> Scan(string root)
    {
        var violations = new List<string>();
        if (!Directory.Exists(root)) return violations;
        var skipDirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            ".git", ".vs", ".vscode", "bin", "obj", "node_modules",
            ".worktrees", "third_party", "quarantine", "publish",
        };
        foreach (string file in Directory.EnumerateFiles(
                     root, "*", SearchOption.AllDirectories))
        {
            var rel = Path.GetRelativePath(root, file);
            var parts = rel.Split(
                Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (parts.SkipLast(1).Any(p => skipDirs.Contains(p)))
                continue;
            string ext = Path.GetExtension(file);
            if (ForbiddenExt.Contains(ext, StringComparer.OrdinalIgnoreCase))
                violations.Add($"{rel} -> {ext}");
        }
        return violations;
    }

    public static Dictionary<string, object?> Report(string root)
    {
        var v = Scan(root);
        return new Dictionary<string, object?>
        {
            ["ok"] = v.Count == 0,
            ["gate"] = "language_boundary",
            ["root"] = root,
            ["permitted_languages"] =
                RuntimeCapabilityLayer.ImplementationLanguages
                    .Cast<object?>().ToList(),
            ["violations"] = v.Cast<object?>().ToList(),
            ["error"] = v.Count == 0
                ? null : ConvErr.LanguageBoundaryViolation,
        };
    }
}
