// ArchitectureTaxonomy.cs — single-core-axis consolidation
// (star-architecture-taxonomy/v1, star-model-core/v1).
//
// Xingcheng has exactly ONE model core: HybridCausalDecoder — a causal,
// autoregressive, decoder-only hybrid of DeltaNet recurrent layers and
// periodic Full Attention layers. Everything else is an orthogonal
// axis: model components, expert mechanism, position, modality,
// training, runtime state, runtime optimization, precision,
// capability, governance, or experimental research.
//
//   ModelCoreContract   star-model-core/v1 — the canonical core
//                       description (topology only; never runtime
//                       policy) + a deterministic contract hash.
//   LayerSchedule       full_attention_interval -> [D,D,D,A,...] —
//                       the single dispatch rule (§22).
//   Taxonomy            the 12-axis classification; every feature has
//                       exactly one primary_axis (§13/§14).
//   DriftGate           job/checkpoint/bundle/runtime contract hashes
//                       must agree or load fails ARCHITECTURE_CONTRACT_DRIFT.
//   VersionDimensions   architecture/weight/runtime/state/bundle/
//                       capability/evaluation are separate version
//                       fields — never one rolled-up number (§34/§36).

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ArchitectureTaxonomy
{
    public const string TaxonomyFormat = "star-architecture-taxonomy/v1";
    public const string CoreFormat = "star-model-core/v1";
    public const string CanonicalArchitecture = "xc-fused-1";

    public const string ModelCore = "HybridCausalDecoder";

    // §13: the only legal top-level axes.
    public static readonly string[] Axes =
    {
        "MODEL_CORE", "MODEL_COMPONENT", "EXPERT_AXIS",
        "POSITION_AXIS", "MODALITY_AXIS", "TRAINING_AXIS",
        "STATE_AXIS", "RUNTIME_OPTIMIZATION_AXIS", "PRECISION_AXIS",
        "CAPABILITY_AXIS", "GOVERNANCE_AXIS",
        "EXPERIMENTAL_ARCHITECTURE",
    };

    public sealed record Classification(
        string PrimaryAxis,
        string[] SecondaryTags,
        bool ArchitectureAffecting,
        bool CheckpointAffecting,
        bool RuntimeOnly,
        bool TrainingOnly,
        bool CapabilityOnly);

    private static Classification C(
        string axis, string[]? tags = null,
        bool arch = false, bool ckpt = false,
        bool rt = false, bool tr = false, bool cap = false)
        => new(axis, tags ?? Array.Empty<string>(),
               arch, ckpt, rt, tr, cap);

    // Canonical classification of every component the system knows.
    // Keyed by component token (matched case-insensitively, substring
    // allowed against the catalog's xingcheng_component string).
    private static readonly (string Token, Classification Cls)[] Map =
    {
        // MODEL_CORE — exactly one entry can ever classify here.
        ("hybridcausaldecoder",
            C("MODEL_CORE", arch: true, ckpt: true)),
        // MODEL_COMPONENT — parts inside the core.
        ("deltanet",        C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("deltarecurrent",  C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("fullattention",   C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("gqa",             C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("attentionoutputgate",
                            C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("qknorm",          C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("rmsnorm",         C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("swiglu",          C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("embedding",       C("MODEL_COMPONENT", arch: true, ckpt: true)),
        ("lmhead",          C("MODEL_COMPONENT", arch: true, ckpt: true)),
        // EXPERT_AXIS — model-side MoE mechanism; residency/offload is
        // runtime optimization, split below.
        ("moe",             C("EXPERT_AXIS",
                              new[] { "model_component" },
                              arch: true, ckpt: true)),
        ("sharedexpert",    C("EXPERT_AXIS", arch: true, ckpt: true)),
        ("router",          C("EXPERT_AXIS", arch: true, ckpt: true)),
        ("expertresidency", C("RUNTIME_OPTIMIZATION_AXIS",
                              new[] { "expert_axis" }, rt: true)),
        ("expertoffload",   C("RUNTIME_OPTIMIZATION_AXIS",
                              new[] { "expert_axis" }, rt: true)),
        ("expertprefetch",  C("RUNTIME_OPTIMIZATION_AXIS",
                              new[] { "expert_axis" }, rt: true)),
        // POSITION_AXIS.
        ("rope",            C("POSITION_AXIS", arch: true, ckpt: true)),
        ("yarn",            C("POSITION_AXIS", arch: true, ckpt: true)),
        // MODALITY_AXIS — vision early fusion feeds the same core.
        ("vision",          C("MODALITY_AXIS", arch: true, ckpt: true)),
        ("modality",        C("MODALITY_AXIS", rt: true)),
        // TRAINING_AXIS — MTP is a training auxiliary, not an
        // inference core.
        ("mtp",             C("TRAINING_AXIS", arch: true, ckpt: true,
                              tr: true)),
        ("sft",             C("TRAINING_AXIS", tr: true)),
        ("dpo",             C("TRAINING_AXIS", tr: true)),
        ("grpo",            C("TRAINING_AXIS", tr: true)),
        ("selflearning",    C("GOVERNANCE_AXIS")),
        ("dataset",         C("GOVERNANCE_AXIS")),
        // STATE_AXIS — all runtime sequence state, single owner.
        ("kvcache",         C("STATE_AXIS",
                              new[] { "runtime_optimization" },
                              rt: true)),
        ("kvint8",          C("STATE_AXIS",
                              new[] { "precision", "runtime_optimization" },
                              rt: true)),
        ("prefixcache",     C("STATE_AXIS",
                              new[] { "runtime_optimization", "cache" },
                              rt: true)),
        ("deltastate",      C("STATE_AXIS", rt: true)),
        ("state",           C("STATE_AXIS", rt: true)),
        ("memoryplanner",   C("RUNTIME_OPTIMIZATION_AXIS", rt: true)),
        ("memoryplan",      C("RUNTIME_OPTIMIZATION_AXIS", rt: true)),
        // RUNTIME_OPTIMIZATION_AXIS.
        ("prefilldecode",   C("RUNTIME_OPTIMIZATION_AXIS", rt: true)),
        ("batchscheduling", C("RUNTIME_OPTIMIZATION_AXIS", rt: true)),
        ("speculative",     C("RUNTIME_OPTIMIZATION_AXIS", rt: true)),
        ("cuda",            C("RUNTIME_OPTIMIZATION_AXIS", rt: true)),
        ("deployment",      C("RUNTIME_OPTIMIZATION_AXIS", rt: true)),
        // PRECISION_AXIS — storage/compute policy, never identity.
        ("precision",       C("PRECISION_AXIS", rt: true)),
        ("fp64",            C("PRECISION_AXIS", rt: true)),
        ("bf16",            C("PRECISION_AXIS", rt: true)),
        ("fp16",            C("PRECISION_AXIS", rt: true)),
        ("fp8",             C("PRECISION_AXIS", rt: true)),
        ("fp4",             C("PRECISION_AXIS", rt: true)),
        ("int8",            C("PRECISION_AXIS", rt: true)),
        ("int4",            C("PRECISION_AXIS", rt: true)),
        // CAPABILITY_AXIS.
        ("rag",             C("CAPABILITY_AXIS",
                              new[] { "grounding" }, cap: true)),
        ("grounding",       C("CAPABILITY_AXIS", cap: true)),
        ("citation",        C("CAPABILITY_AXIS", cap: true)),
        ("nativethinking",  C("CAPABILITY_AXIS", rt: true, cap: true)),
        ("thinking",        C("CAPABILITY_AXIS", rt: true, cap: true)),
        ("persona",         C("CAPABILITY_AXIS", cap: true)),
        ("roleplay",        C("CAPABILITY_AXIS", cap: true)),
        ("creative",        C("CAPABILITY_AXIS", cap: true)),
        ("agent",           C("CAPABILITY_AXIS", cap: true)),
        ("coding",          C("CAPABILITY_AXIS", cap: true)),
        ("tool",            C("CAPABILITY_AXIS", cap: true)),
        ("fim",             C("CAPABILITY_AXIS", cap: true)),
        ("refusal",         C("GOVERNANCE_AXIS",
                              new[] { "capability_axis" })),
        // GOVERNANCE_AXIS.
        ("lifecycle",       C("GOVERNANCE_AXIS")),
        ("generationmigration", C("GOVERNANCE_AXIS")),
        ("provenance",      C("GOVERNANCE_AXIS")),
        ("audit",           C("GOVERNANCE_AXIS")),
        ("retention",       C("GOVERNANCE_AXIS")),
        ("catalog",         C("GOVERNANCE_AXIS")),
        ("gate",            C("GOVERNANCE_AXIS")),
        ("eval",            C("GOVERNANCE_AXIS")),
        ("teacher",         C("GOVERNANCE_AXIS")),
        ("xcn",             C("GOVERNANCE_AXIS")),
        // EXPERIMENTAL_ARCHITECTURE — research, never canonical core.
        ("mla",             C("EXPERIMENTAL_ARCHITECTURE")),
        ("csa",             C("EXPERIMENTAL_ARCHITECTURE")),
        ("gemma4",          C("EXPERIMENTAL_ARCHITECTURE")),
        ("kda",             C("EXPERIMENTAL_ARCHITECTURE")),
        ("mamba",           C("EXPERIMENTAL_ARCHITECTURE")),
        ("rwkv",            C("EXPERIMENTAL_ARCHITECTURE")),
        ("attnres",         C("EXPERIMENTAL_ARCHITECTURE")),
        ("latentmoe",       C("EXPERIMENTAL_ARCHITECTURE")),
        ("msa",             C("EXPERIMENTAL_ARCHITECTURE")),
        ("speculativedecoder",
                            C("EXPERIMENTAL_ARCHITECTURE")),
        ("research",        C("EXPERIMENTAL_ARCHITECTURE")),
        // Catalog coverage helpers.
        ("retrieval",       C("CAPABILITY_AXIS",
                              new[] { "grounding" }, cap: true)),
        ("modulesensitivity", C("TRAINING_AXIS", tr: true)),
        ("merge",           C("GOVERNANCE_AXIS")),
        // Laya/MiMo absorption (§47) — decision/capability plane and
        // agent-learning governance; never architecture.
        ("nativesystemonehead", C("CAPABILITY_AXIS",
                              new[] { "runtime_augmentation" },
                              rt: true, cap: true)),
        ("systemone",       C("CAPABILITY_AXIS", rt: true,
                              cap: true)),
        ("system1",         C("CAPABILITY_AXIS", rt: true,
                              cap: true)),
        ("typeddecision",   C("CAPABILITY_AXIS", rt: true,
                              cap: true)),
        ("decisioncalibration", C("CAPABILITY_AXIS", rt: true)),
        ("mtpdrafter",      C("RUNTIME_OPTIMIZATION_AXIS",
                              rt: true)),
        ("routerstability", C("GOVERNANCE_AXIS",
                              new[] { "expert_axis" })),
        ("rewardintegrity", C("GOVERNANCE_AXIS")),
        ("groupwise",       C("TRAINING_AXIS", tr: true)),
        ("harness",         C("TRAINING_AXIS", tr: true)),
        ("agenttrajectory", C("GOVERNANCE_AXIS")),
    };

    /// <summary>Classify a component string to exactly one primary
    /// axis. Longest matching token wins so "prefix cache" does not
    /// classify under "cache"… order: more specific tokens first —
    /// iterate in declaration order, require substring match.</summary>
    public static Classification Classify(string component)
    {
        string c = component.Replace(" ", "").Replace("_", "")
                            .Replace("-", "").ToLowerInvariant();
        // Longest matching token wins — "latentmoe" must not be
        // swallowed by "moe", nor "speculativedecoder" by "speculative".
        Classification? best = null; int bestLen = -1;
        foreach (var (tok, cls) in Map)
            if (tok.Length > bestLen &&
                c.Contains(tok.ToLowerInvariant()))
            { best = cls; bestLen = tok.Length; }
        if (best is not null) return best;
        // Unmapped features classify as capability evidence — visible
        // but never architecture.
        return C("CAPABILITY_AXIS", cap: true);
    }

    // -------------------------------------------------- layer schedule
    // §22: the ONLY derivation of per-layer dispatch. layer types:
    //   D = DELTA_RECURRENT, A = FULL_ATTENTION. interval 4 pins
    //   D D D A repeating (xc-fused-1).

    public static List<string> LayerSchedule(long layerCount,
                                             long fullAttentionInterval)
    {
        if (layerCount <= 0)
            throw new ExecutorError("LAYER_SCHEDULE_INVALID",
                "layer_count must be > 0");
        if (fullAttentionInterval <= 0)
            // No periodic attention — all recurrent.
            return Enumerable.Repeat("DELTA_RECURRENT",
                                     (int)layerCount).ToList();
        var sched = new List<string>((int)layerCount);
        for (int i = 0; i < layerCount; ++i)
            sched.Add((i + 1) % fullAttentionInterval == 0
                          ? "FULL_ATTENTION" : "DELTA_RECURRENT");
        return sched;
    }

    // -------------------------------------------------- core contract
    // §16/§21: xc-fused-1 locks ONLY the decoder topology + the
    // architecture-affecting components. Cache/precision/CUDA/offload/
    // thinking/RAG/tooling never appear here.

    public static Dictionary<string, object?> CoreContract(
        string architecture, long layers = 12,
        long hidden = 768, long heads = 12,
        long kvHeads = 4, long experts = 16)
    {
        if (architecture != CanonicalArchitecture)
            throw new ExecutorError("ARCHITECTURE_UNKNOWN",
                $"no canonical contract for '{architecture}'");
        var contract = new Dictionary<string, object?>
        {
            ["format"] = CoreFormat,
            ["architecture_generation"] = architecture,
            ["core_type"] = "hybrid_causal_decoder",
            ["core_name"] = ModelCore,
            ["causal"] = true,
            ["autoregressive"] = true,
            ["encoder"] = false,
            ["cross_attention"] = false,
            ["layer_count"] = layers,
            ["layer_schedule"] =
                LayerSchedule(layers, 4).Cast<object?>().ToList(),
            ["hidden_size"] = hidden,
            ["head_geometry"] = new Dictionary<string, object?>
            {
                ["type"] = "gqa",
                ["query_heads"] = heads,
                ["kv_heads"] = kvHeads,
                ["qk_norm"] = true,
                ["output_gate"] = true,
            },
            ["ffn_type"] = "swiglu_moe",
            ["norm_type"] = "rmsnorm",
            ["expert_topology"] = new Dictionary<string, object?>
            {
                ["router"] = "sigmoid",
                ["top_k"] = 2,
                ["experts"] = experts,
                ["interval"] = 1,
                ["shared_experts"] = 1,
                ["shared_expert_gate"] = true,
                ["aux_loss_weight"] = 0.001,
            },
            ["position_contract"] = new Dictionary<string, object?>
            {
                ["type"] = "partial_rope",
                ["rotary_factor"] = 0.5,
                ["extension"] = "yarn",
                ["yarn_min_factor"] = 2.0,
            },
            ["modality_contract"] = new Dictionary<string, object?>
            {
                ["text"] = "canonical",
                ["vision"] = "canonical_early_fusion",
                ["audio"] = "contract_only",
                ["video"] = "contract_only",
            },
            ["training_contract"] = new Dictionary<string, object?>
            {
                ["mtp_depth"] = 1,
                ["mtp_loss_weight_min"] = 0.1,
                ["note"] = "mtp is a training auxiliary — excluded " +
                           "from serving bundles",
            },
        };
        contract["architecture_contract_hash"] = ContractHash(contract);
        return contract;
    }

    /// <summary>Deterministic contract fingerprint: sha256 over the
    /// canonical JSON of the contract sans the hash field itself.</summary>
    public static string ContractHash(Dictionary<string, object?> c)
    {
        var copy = new Dictionary<string, object?>(c);
        copy.Remove("architecture_contract_hash");
        string canonical = CanonicalJson.CanonicalDict(copy);
        return Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
    }

    // ------------------------------------------------------ drift gate
    // §26: job/checkpoint/bundle/runtime must all resolve to the same
    // contract hash.

    public static Dictionary<string, object?> DriftGate(
        string jobHash, string checkpointHash,
        string bundleHash, string runtimeHash)
    {
        var hashes = new List<object?>()
            { jobHash, checkpointHash, bundleHash, runtimeHash };
        var distinct = hashes.Cast<string>()
            .Where(h => h.Length > 0).Distinct().ToList();
        bool consistent = distinct.Count <= 1;
        if (!consistent)
            throw new ExecutorError("ARCHITECTURE_CONTRACT_DRIFT",
                "contract hash mismatch: " +
                string.Join(",", distinct));
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-architecture-drift-gate/v1",
            ["hashes"] = hashes,
            ["consistent"] = true,
        };
    }

    // ------------------------------------------------ version dims ---
    // §34-§36: seven separate version fields; a single rolled-up
    // number is forbidden.

    public static readonly string[] VersionFields =
    {
        "architecture_generation", "weight_version", "runtime_version",
        "state_contract_version", "bundle_version",
        "capability_version", "evaluation_version",
    };

    public static Dictionary<string, object?> VersionDimensions(
        string toolRoot)
    {
        var state = ReadState(toolRoot);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-version-dimensions/v1",
            ["architecture_generation"] = Str(state,
                "architecture_generation", "current-compatible-profile"),
            ["weight_version"] = Str(state, "weight_version", ""),
            ["runtime_version"] = Str(state,
                "runtime_version", "xc-native-cpp23"),
            ["state_contract_version"] = Str(state,
                "state_contract_version", "star-native-state/v2"),
            ["bundle_version"] = Str(state, "bundle_version", ""),
            ["capability_version"] = Str(state, "capability_version", ""),
            ["evaluation_version"] = Str(state, "evaluation_version", ""),
            ["active_generation"] = Str(state,
                "active_generation", "gen-2-consolidated"),
            ["candidate_architecture"] = Str(state,
                "candidate_architecture", CanonicalArchitecture),
        };
    }

    private static string Str(Dictionary<string, object?> d,
                              string k, string dflt)
        => d.TryGetValue(k, out var v) && v is string s && s.Length > 0
            ? s : dflt;

    private static Dictionary<string, object?> ReadState(string toolRoot)
    {
        string p = Path.Combine(toolRoot,
            "xingcheng/runtime/state/generation/state.json"
                .Replace('/', Path.DirectorySeparatorChar));
        if (!File.Exists(p)) return new Dictionary<string, object?>();
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(p));
            var m = new Dictionary<string, object?>();
            foreach (var pr in doc.RootElement.EnumerateObject())
                m[pr.Name] = ModelLifecycle.Decode(pr.Value);
            return m;
        }
        catch { return new Dictionary<string, object?>(); }
    }

    // ------------------------------------------------------- emit ----

    /// <summary>The taxonomy document: axes + the one model core +
    /// component examples per axis.</summary>
    public static Dictionary<string, object?> Emit()
    {
        var examples = new Dictionary<string, object?>
        {
            ["MODEL_CORE"] = new[] { "HybridCausalDecoder" }
                .Cast<object?>().ToList(),
            ["MODEL_COMPONENT"] = new object?[]
                { "DeltaNet", "FullAttention", "GQA",
                  "AttentionOutputGate", "QKNorm", "RMSNorm", "SwiGLU",
                  "Embedding", "LMHead" }.ToList(),
            ["EXPERT_AXIS"] = new object?[]
                { "MoE", "Router", "SharedExpert", "TopK",
                  "ExpertDispatch" }.ToList(),
            ["POSITION_AXIS"] = new object?[]
                { "RoPE", "PartialRoPE", "YaRN" }.ToList(),
            ["MODALITY_AXIS"] = new object?[]
                { "VisionEarlyFusion", "Audio(contract)",
                  "Video(contract)" }.ToList(),
            ["TRAINING_AXIS"] = new object?[]
                { "CE", "MoEBalanceLoss", "RouterZLoss", "MTP",
                  "SFT", "DPO", "GRPO" }.ToList(),
            ["STATE_AXIS"] = new object?[]
                { "AttentionKV", "PagedKV", "KV-INT8", "PrefixCache",
                  "DeltaRecurrentState", "VisionPrefixState",
                  "ThinkingBranchState", "SpeculativeTempState" }
                .ToList(),
            ["RUNTIME_OPTIMIZATION_AXIS"] = new object?[]
                { "PrefixCaching", "ExpertResidency", "ExpertOffloading",
                  "RouterAwarePrefetch", "PrefillDecodeDisaggregation",
                  "BatchScheduling", "MemoryPlanning", "CUDA",
                  "SpeculativeDecoding" }.ToList(),
            ["PRECISION_AXIS"] = new object?[]
                { "FP64", "BF16", "FP16", "FP8", "FP4", "INT8",
                  "KV-INT8" }.ToList(),
            ["CAPABILITY_AXIS"] = new object?[]
                { "Instruction", "ContextTracking", "Reading", "Math",
                  "Coding", "ToolCall", "RAG", "Grounding", "Persona",
                  "Roleplay", "Creative", "NativeThinking", "Agent",
                  "VisionUnderstanding" }.ToList(),
            ["GOVERNANCE_AXIS"] = new object?[]
                { "XCN10", "GenerationMigration", "ModelLifecycle",
                  "SelfLearning", "Retention", "Audit", "Provenance",
                  "FeatureCatalog", "CapabilityRegistry",
                  "ArchitectureGate", "LanguageBoundary",
                  "ReleaseGate" }.ToList(),
            ["EXPERIMENTAL_ARCHITECTURE"] = new object?[]
                { "MLA", "CSA", "Gemma4", "KDA", "Mamba", "RWKV",
                  "AttnRes", "LatentMoE", "MSA" }.ToList(),
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = TaxonomyFormat,
            ["model_core_count"] = 1,
            ["model_core"] = ModelCore,
            ["core_type"] = "hybrid_causal_decoder",
            ["canonical_architecture"] = CanonicalArchitecture,
            ["axes"] = Axes.Cast<object?>().ToList(),
            ["axis_examples"] = examples,
            ["rule"] = "one primary classification per feature; " +
                       "runtime/precision/capability/governance never " +
                       "create architecture generations",
        };
    }
}
