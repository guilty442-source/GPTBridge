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
        // §22/§23 quarantine: NON_CANONICAL_EXPERIMENTAL = training-
        // verified but outside xc-fused-1 — production load is a
        // CANONICAL_CONTRACT_VIOLATION; TRAINER_ONLY_EXPERIMENTAL =
        // trainer-probe evidence only, no serving semantics.
        "NON_CANONICAL_EXPERIMENTAL", "TRAINER_ONLY_EXPERIMENTAL",
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

    /// <summary>Bridge to the record-based catalog row: identical
    /// fields, keeps the batch-2 object-initializer rows readable.</summary>
    public FeatureCatalog.Feature ToFeature() => new(
        FeatureId, SourceInspiration, XingchengComponent, Status,
        TrainingRequired, RuntimeRequired, GenerationChangeRequired,
        Owner, Tests);
}

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
        // Scale directive (§64): topology-touching experiment and
        // training-pipeline futures, distinct from runtime experiments.
        "EXPERIMENTAL_ARCHITECTURE", "FUTURE_TRAINING",
        // Trinity/Step-3.7/Falcon2/Ling absorption (§69): governed
        // training-curriculum integration for progressive context.
        "INTEGRATED_TRAINING",
        // Batch-2 refinements (§30): REJECT_DUPLICATE = idea already in
        // xc-fused-1, no second implementation;
        // HARDWARE_EXPERIMENTAL = needs hardware support to activate;
        // CONTRACT_ONLY = interface contract exists, model lane absent.
        "REJECT_DUPLICATE",
        "HARDWARE_EXPERIMENTAL", "CONTRACT_ONLY",
        // §22/§23 quarantine: NON_CANONICAL_EXPERIMENTAL = training-
        // verified but outside xc-fused-1 — production load is a
        // CANONICAL_CONTRACT_VIOLATION; TRAINER_ONLY_EXPERIMENTAL =
        // trainer-probe evidence only, no serving semantics.
        "NON_CANONICAL_EXPERIMENTAL", "TRAINER_ONLY_EXPERIMENTAL",
    };

    public sealed record Feature(
        string FeatureId, string SourceInspiration,
        string XingchengComponent, string Status,
        bool TrainingRequired, bool RuntimeRequired,
        bool GenerationChangeRequired, string Owner,
        string[] Tests);

    /// <summary>Canonical catalog ??the §31 mapping, embedded so the
    /// catalog is versioned with the code that implements it.</summary>
    private static readonly Feature[] CanonicalMain =
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
        // ---- Trinity/Step-3.7/Falcon2/Ling absorption (§69) ----
        new("f-extreme-sparsity-stability", "Trinity-Large",
            "ExtremeSparsityStabilityPlane (entropy/utilization/" +
            "starvation/hotspot/flip-rate)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "extreme-sparsity-stability" }),
        new("f-blockwise-expert-quant", "Ling-2.6",
            "BlockwiseQuantizationContract (per-block scales, " +
            "per-class precision policy)",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "cpp-runtime",
            new[] { "quant-cert", "blockwise-quant-probe" }),
        new("f-thinking-efficiency", "Ling-2.6/Step-3.7",
            "ThinkingEfficiencyPolicy + reasoning redundancy metrics",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "thinking-efficiency", "reasoning-redundancy-probe" }),
        new("f-progressive-context", "Falcon 2",
            "Progressive context curriculum + per-level expansion gate",
            "INTEGRATED_TRAINING", false, true, false,
            "csharp-runtime",
            new[] { "progressive-context-check" }),
        new("f-attention-density-7to1", "Ling-2.6",
            "AttentionDensityProbe 3:1 vs 7:1 (next generation only)",
            "FUTURE_GENERATION", false, true, false,
            "cpp-runtime",
            new[] { "attention-density-probe" }),
        new("f-dense-anchor", "Trinity-Large",
            "DenseAnchorProbe (collapse-triggered stability anchor only)",
            "EXPERIMENTAL_ARCHITECTURE", false, true, false,
            "cpp-runtime",
            new[] { "dense-anchor-probe" }),
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
        new("f-residency-tiers", "scale directive",
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
            "Full decoder on NPU — deferred (dynamic "
            + "DeltaNet/MoE/KV state unsuitable for first wave)",
            "DEFERRED", false, false, false, "governance",
            Array.Empty<string>()),
        new("f-runtime-host", "silicon directive",
            "XingchengRuntimeHost single-owner + "
            + "SystemArtifactRegistry",
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
        // ---- model-efficiency/scale directive (Liquid/Solar/OLMo/
        //      Arctic/Arctic-Embed absorption, §64) ----
        new("f-hardware-aware-scale", "scale directive",
            "HardwareAwareScaleSearch — hardware-in-the-loop shape " +
            "search (never hand-picked sizes)",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "hardware-scale-search" }),
        new("f-depth-scale", "scale directive",
            "DepthScalePlanner — depth before width, canonical " +
            "3:1 schedule preserved",
            "EXPERIMENTAL_TRAINING", false, true, false,
            "csharp-runtime",
            new[] { "depth-scale-probe" }),
        new("f-depth-inheritance", "scale directive",
            "star-depth-inheritance/v1 layer lineage",
            "EXPERIMENTAL_TRAINING", false, true, false,
            "csharp-runtime",
            new[] { "depth-inheritance-probe" }),
        new("f-fine-grained-expert-scale", "scale directive",
            "FineGrainedExpertScalingPolicy — count grows, top_k " +
            "fixed, shared/routed separated",
            "EXPERIMENTAL_ARCHITECTURE", false, true, false,
            "csharp-runtime",
            new[] { "expert-granularity-probe",
                    "expert-specialization" }),
        new("f-adaptive-retrieval-representation", "scale directive",
            "AdaptiveRetrievalRepresentation — FULL/MEDIUM/COMPACT " +
            "dims + tiered precision + two-stage retrieval",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "adaptive-embedding-probe",
                    "retrieval-compression-bench" }),
        new("f-matryoshka-embedding", "scale directive",
            "head-dim truncation for self-trained embeddings",
            "FUTURE_TRAINING", false, true, false,
            "training",
            new[] { "adaptive-embedding-probe" }),
        // ---- product-tier directive (300M dev / 1B STANDARD /
        //      20B EXTREME_SPARSE) ----
        new("f-scale-tiers", "product-tier directive",
            "star-scale-tier/v1 — xc-300m-dev / xc-1b-standard / " +
            "xc-20b-extreme-sparse, one xc-fused-1 core (§0-§1)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "scale-tiers" }),
        new("f-active-compute-gate", "product-tier directive",
            "ActiveComputeGate — total-params-only reports rejected; " +
            "tier active ceilings + ratio enforced (§47)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "active-compute-gate" }),
        new("f-model-identity", "product-tier directive",
            "star-model-identity/v1 — arch + scale + weight + " +
            "runtime + bundle hash (§2)",
            "INTEGRATED_SCHEMA", false, true, false,
            "csharp-runtime",
            new[] { "model-identity" }),
        new("f-scale-residency", "product-tier directive",
            "star-scale-residency/v1 — 1B all-hot GPU, 20B " +
            "GPU/RAM/NVMe bands (§28-§32)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "residency-plan" }),
        new("f-trainable-budget", "product-tier directive",
            "star-trainable-budget/v1 — 20B sparse optimizer " +
            "100-500M bound, dormant experts skipped (§35-§38)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "trainable-budget" }),
        new("f-sparse-thinking", "product-tier directive",
            "star-thinking-levels/v1 — OFF..MAX + AUTO; system-1 " +
            "answers without decode (§48-§51)",
            "EXPERIMENTAL_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "thinking-levels" }),
        new("f-extreme-sparse-planner", "product-tier directive",
            "EXTREME_SPARSE scale_class — active-first ranking + " +
            "256E sweep (§61, §23)",
            "INTEGRATED_RUNTIME", false, true, false,
            "csharp-runtime",
            new[] { "hardware-scale-search" }),
        // ---- capacity & active-parameter formal spec (capacity
        //      directive §0-§57) ----
        new("f-capacity-metrics", "capacity directive",
            "star-capacity-metrics/v1 — six param metrics + five " +
            "storage metrics measured from bundle manifest (§4-§5)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "capacity-metrics" }),
        new("f-common-floor-gate", "capacity directive",
            "common+shared >=600M -> COMMON_FLOOR_TOO_HIGH; routed " +
            "active budget = 1B - floor (§32-§34)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "common-floor-gate" }),
        new("f-distillation-plane", "capacity directive",
            "NativeDistillationPlane — LOGIT/HIDDEN/CAPABILITY/" +
            "ROUTING lanes + reasoning compression + quant-aware " +
            "(§9-§15)",
            "FUTURE_TRAINING", false, true, false, "training",
            new[] { "distillation-contract",
                    "reasoning-compression" }),
        new("f-quantization-policy", "capacity directive",
            "mixed-precision table — router FP32, common/shared " +
            "BF16, routed cold INT4 (§20-§26)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "quantization-validate" }),
        new("f-effective-compute", "capacity directive",
            "EffectiveActiveCompute — active*tokens*layers + " +
            "transfers + MTP verify (§37-§38)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "effective-compute" }),
        new("f-expert-lifecycle", "capacity directive",
            "dedup/split/merge/dead-prune gates — capability-gated, " +
            "never magnitude-only (§16-§19)",
            "EXPERIMENTAL_ARCHITECTURE", false, true, false,
            "training",
            new[] { "expert-lifecycle-gate" }),
        new("f-generation-pipeline", "capacity directive",
            "TRAIN->...->PROMOTE ten-stage contract + §54 promotion " +
            "legs (§7/§54)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "promotion-gate" }),
        // ---- XC-1B Mature Standard (maturity directive §1-§40) ----
        new("f-maturity-baseline", "maturity directive",
            "star-maturity-baseline/v1 registry — per-capability " +
            "floors, versioned, single source (§29-§30)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "maturity-baseline", "maturity-registry" }),
        new("f-capability-floor", "maturity directive",
            "per-capability minimum + no-compensation + BASE_MODEL " +
            "layer gate + THINK_OFF first (§5-§9, §28)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "capability-floor-gate" }),
        new("f-retention-gates", "maturity directive",
            "per-capability retention through distill/compress/" +
            "quantize — mean never hides an item (§10-§12)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "retention-gate" }),
        new("f-golden-usability", "maturity directive",
            "eval-only golden suite + stability across seeds/runs/" +
            "context; leakage denied (§31-§33)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "golden-gate" }),
        new("f-xc1b-certification", "maturity directive",
            "nine-cert bundle + promotion AND + replacement gate " +
            "(§13, §27, §35)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "certification-gate", "maturity-promotion" }),
        new("f-300m-to-1b-gate", "maturity directive",
            "lab tier proves process readiness, not mature floors " +
            "(§37)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "maturity-checks" }),
        // ---- NativeTrainingAccelerationPlane (acceleration
        //      directive §0-§74) ----
        new("f-training-telemetry", "acceleration directive",
            "star-training-telemetry/v1 — 17-field metric set + " +
            "step breakdown coverage (§1/§65)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "training-telemetry-validate" }),
        new("f-bottleneck-classifier", "acceleration directive",
            "step-breakdown -> DATA/CPU/LAUNCH/MEMORY/COMPUTE/" +
            "OPTIMIZER/EVAL/CHECKPOINT bound (§66)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "bottleneck-classify" }),
        new("f-eval-tiering", "acceleration directive",
            "FAST/REGRESSION/FULL eval cadence — per-step full " +
            "eval denied (§37-§40)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "eval-tier-policy" }),
        new("f-training-pilot", "acceleration directive",
            "50/200/400/600-step ladder + <=3 LR pilot (§42-§43)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "training-pilot" }),
        new("f-batch-planner", "acceleration directive",
            "TrainingBatchPlanner — bucket + free VRAM -> " +
            "microbatch/accum/workspace (§7-§10)",
            "EXPERIMENTAL_TRAINING", false, true, false,
            "training",
            new[] { "training-batch-plan" }),
        new("f-training-precision", "acceleration directive",
            "BF16 compute / FP32 sensitive / FP64 oracle-only; " +
            "FP8-FP4 not primary (§2-§3)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "training-precision-policy" }),
        new("f-distill-artifact-cache", "acceleration directive",
            "top-N logits + residual mass, shared across student " +
            "candidates (§48-§51)",
            "EXPERIMENTAL_TRAINING", false, true, false,
            "training",
            new[] { "distill-artifact-validate" }),
        new("f-time-to-quality", "acceleration directive",
            "TIME_TO_QUALIFIED_MODEL KPI + " +
            "CapabilityGain/GPU-s (§0/§68-§69)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "time-to-quality" }),
        new("f-speed-capability-gate", "acceleration directive",
            "speed change with capability regression = FAIL (§67)",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "speed-gate" }),
        new("f-cuda-language-policy", "cuda directive",
            "star-cuda-language/v1 — self-authored CUDA C++ via " +
            "NVRTC, no external compute libraries",
            "INTEGRATED_GOVERNANCE", false, true, false,
            "csharp-runtime",
            new[] { "cuda-language-check" }),
    };

    /// <summary>Devin-lane rows (batch-1 + batch-2 §30), kept in the
    /// FeatureEntry object-initializer shape; merged into Canonical
    /// alongside the main-lane records — ids are disjoint.</summary>
    private static FeatureEntry[] DevinRows() => new[]
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
        // §22/§23/§45 quarantine rows — verified in training, fenced
        // from production: loading one under an xc-fused-1 claim is
        // CANONICAL_CONTRACT_VIOLATION.
        new FeatureEntry
        {
            FeatureId = "csa-production",
            SourceInspiration =
                "DeepSeek compressed sparse attention",
            XingchengComponent =
                "trainer path only — serving unimplemented",
            Status = "NON_CANONICAL_EXPERIMENTAL",
            RuntimeRequired = false,
            Owner = "cpp:xingcheng_trainer --csacheck",
            Tests = new[] { "csacheck" },
        },
        new FeatureEntry
        {
            FeatureId = "mla-production",
            SourceInspiration = "DeepSeek multi-head latent attention",
            XingchengComponent =
                "trainer path only — not xc-fused-1 canonical",
            Status = "NON_CANONICAL_EXPERIMENTAL",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
        new FeatureEntry
        {
            FeatureId = "aux-free-lb-bias",
            SourceInspiration =
                "DeepSeek aux-loss-free load balancing",
            XingchengComponent =
                "trainer path only — canonical serving router is fixed",
            Status = "TRAINER_ONLY_EXPERIMENTAL",
            RuntimeRequired = false,
            Owner = "governance",
            Tests = Array.Empty<string>(),
        },
    };

    /// <summary>Canonical catalog — the §31 mapping. Union of the
    /// main-lane record rows and the devin-lane FeatureEntry rows;
    /// ids are disjoint so the union cannot produce a
    /// feature_id_duplicate failure.</summary>
    public static readonly Feature[] Canonical =
        CanonicalMain
            .Concat(DevinRows().Select(e => e.ToFeature()))
            .ToArray();

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

    /// <summary>Install the canonical catalog (atomic write). Alias of
    /// Emit retained for callers that predate the Emit rename.</summary>
    public static Dictionary<string, object?> Install(string toolRoot)
        => Emit(toolRoot);

    /// <summary>Structural validation of an in-memory catalog row set:
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
            if (fo is not Dictionary<string, object?> fd)
            {
                errors.Add("feature_not_object");
                continue;
            }
            string id = fd.TryGetValue("feature_id", out var i)
                ? i?.ToString() ?? "" : "";
            string status = fd.TryGetValue("status", out var s)
                ? s?.ToString() ?? "" : "";
            if (id.Length == 0) errors.Add("feature_id_missing");
            if (!seen.Add(id)) errors.Add($"feature_id_duplicate:{id}");
            if (!Statuses.Contains(status))
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
