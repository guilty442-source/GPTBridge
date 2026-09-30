// BundleManifest.cs — star-bundle-manifest/v2 (§33).
//
// Weights checkpoint is untouched (XCN1 v10 stands); the manifest
// carries the runtime contracts a bundle supports. §32: none of these
// fields may live in the checkpoint — agent policy, reasoning policy,
// deployment profile, context-cache config, tool policy, hardware
// capability and evaluation results are manifest/runtime/DB data.
// §33 last rule: when weights do not change, a v2 manifest never mints
// a new model generation.

namespace GPTBridge.XingchengLearning;

internal static class BundleManifestV2
{
    public const string Format = "star-bundle-manifest/v2";

    /// <summary>Contract versions pinned by this manifest.</summary>
    public const string RuntimeContract = "star-runtime-capabilities/v1";
    public const string DialogueContract = DialogueEnvelope.Format;
    public const string ToolContract = "star-tool-call/v2";
    public const string StateContract = "star-native-state/v2";

    /// <summary>§33 required field names — a v2 manifest is complete
    /// only when all are present.</summary>
    public static readonly string[] RequiredFields =
    {
        "runtime_contract_version", "dialogue_contract_version",
        "tool_contract_version", "state_contract_version",
        "precision_map", "supported_modalities", "supported_profiles",
        "context_probe_max", "speculative_decode_available",
        "sparse_attention_available", "hardware_requirements",
        "feature_catalog_version",
    };

    /// <summary>Build a v2 manifest document for a bundle. Every
    /// capability claim is schema-gated: speculative decode and sparse
    /// attention report availability of the *infrastructure*, never
    /// production enablement (§13: speculation is runtime-only, MTP
    /// export still discards).</summary>
    public static Dictionary<string, object?> Build(
        QuantizationPolicy precisionMap,
        long contextProbeMax,
        Dictionary<string, object?> hardwareRequirements)
    {
        var m = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["runtime_contract_version"] = RuntimeContract,
            ["dialogue_contract_version"] = DialogueContract,
            ["tool_contract_version"] = ToolContract,
            ["state_contract_version"] = StateContract,
            ["precision_map"] = precisionMap.ToDict(),
            ["supported_modalities"] = new List<object?>
            {
                new Dictionary<string, object?>
                    { ["modality"] = "TEXT", ["capability"] = "ACTIVE" },
                new Dictionary<string, object?>
                    { ["modality"] = "IMAGE", ["capability"] = "ACTIVE" },
                new Dictionary<string, object?>
                    { ["modality"] = "VIDEO",
                      ["capability"] = "CONTRACT_ONLY" },
                new Dictionary<string, object?>
                    { ["modality"] = "AUDIO",
                      ["capability"] = "CONTRACT_ONLY" },
                new Dictionary<string, object?>
                    { ["modality"] = "DOCUMENT",
                      ["capability"] = "PREPROCESSING" },
            },
            ["supported_profiles"] = DeploymentProfile.Names
                .Cast<object?>().ToList(),
            ["context_probe_max"] = contextProbeMax,
            ["speculative_decode_available"] = "INFRASTRUCTURE_ONLY",
            ["sparse_attention_available"] = "PROBE_ONLY",
            ["hardware_requirements"] = hardwareRequirements,
            ["feature_catalog_version"] = FeatureCatalog.Format,
            // §33 honesty fields — a manifest bump is not a model bump.
            ["produces_new_generation"] = false,
            ["weights_checkpoint"] = "XCN1 v10",
            ["architecture"] = "xc-fused-1",
            ["generation"] = "gen-2-consolidated",
        };
        Validate(m);
        return m;
    }

    /// <summary>Fail-closed completeness check.</summary>
    public static void Validate(Dictionary<string, object?> manifest)
    {
        var missing = RequiredFields
            .Where(f => !manifest.ContainsKey(f) || manifest[f] is null)
            .ToList();
        if (missing.Count > 0)
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"manifest v2 missing: {string.Join(",", missing)}");
    }
}
