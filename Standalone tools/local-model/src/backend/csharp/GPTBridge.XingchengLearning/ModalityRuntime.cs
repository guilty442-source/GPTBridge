// ModalityRuntime.cs — unified multimodal runtime (§21) on the
// Nemotron-style single-decoder adapter contract (§14).
//
//   IModalityAdapter: Encode() Project() Budget() CacheKey() Validate()
//     — every modality enters the ONE LLM decoder through the same
//     interface; reasoning stays in the single center (§14).
//
//   Status (§21):     Text ACTIVE | Image ACTIVE (existing Vision Early
//                     Fusion) | Video CONTRACT_ONLY | Audio
//                     CONTRACT_ONLY | Document preprocessing path.
//   §4.2 rule:        audio/video adapters exist as contracts and
//                     report UNAVAILABLE — a registered adapter never
//                     implies a model capability.
//   §15:              VideoSamplingPolicy is contract + metadata
//                     plumbing only — no video encoder this round.

namespace GPTBridge.XingchengLearning;

/// <summary>§14 native modality-adapter interface — every modality
/// funnelled into the single decoder implements exactly this.</summary>
internal interface IModalityAdapter
{
    string Modality { get; }
    /// <summary>ACTIVE | CONTRACT_ONLY — §4.2 honesty field.</summary>
    string Capability { get; }
    /// <summary>Validate input contract; fail-closed on malformed
    /// input or unsupported modality state.</summary>
    void Validate(ModalityInput input);
    /// <summary>Encode the input to model-plane features. CONTRACT_ONLY
    /// adapters always throw UNAVAILABLE.</summary>
    Dictionary<string, object?> Encode(ModalityInput input);
    /// <summary>Project features into the decoder embedding space.</summary>
    Dictionary<string, object?> Project(ModalityInput input);
    /// <summary>Token budget for this input under the active profile.</summary>
    long Budget(ModalityInput input);
    /// <summary>Cache-address component for ContextCacheManager keys.</summary>
    string CacheKey(ModalityInput input);
}

internal sealed class TextAdapter : IModalityAdapter
{
    public string Modality => "TEXT";
    public string Capability => "ACTIVE";
    public void Validate(ModalityInput i)
    {
        if (i.Modality != "TEXT")
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"TextAdapter received {i.Modality}");
    }
    public Dictionary<string, object?> Encode(ModalityInput i)
    {
        Validate(i);
        return new() { ["kind"] = "tokenized_input",
                       ["hash"] = i.ContentHash };
    }
    public Dictionary<string, object?> Project(ModalityInput i)
        => Encode(i);   // text needs no projection — direct embedding
    public long Budget(ModalityInput i)
        => i.TokenBudget > 0 ? i.TokenBudget : 8192;
    public string CacheKey(ModalityInput i) => i.ContentHash;
}

/// <summary>Image path: runs through existing Vision Early Fusion
/// (xc-fused-1 canonical) — the adapter is the contract wrapper, not a
/// second vision lane.</summary>
internal sealed class VisionAdapter : IModalityAdapter
{
    public string Modality => "IMAGE";
    public string Capability => "ACTIVE";
    public void Validate(ModalityInput i)
    {
        if (i.Modality != "IMAGE")
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"VisionAdapter received {i.Modality}");
        if (i.Dimensions.Length == 0)
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                "image input requires dimensions");
    }
    public Dictionary<string, object?> Encode(ModalityInput i)
    {
        Validate(i);
        return new() { ["kind"] = "vision_patches",
                       ["dimensions"] = i.Dimensions,
                       ["hash"] = i.ContentHash,
                       ["path"] = "vision_early_fusion" };
    }
    public Dictionary<string, object?> Project(ModalityInput i)
        => new() { ["kind"] = "vision_projection",
                   ["from"] = Encode(i) };
    public long Budget(ModalityInput i)
        => i.TokenBudget > 0 ? i.TokenBudget : 2048;
    public string CacheKey(ModalityInput i) => i.ContentHash;
}

/// <summary>§4.2/§14 contract-only adapter — parses input contracts,
/// reports UNAVAILABLE for every real operation.</summary>
internal sealed class UnavailableModalityAdapter : IModalityAdapter
{
    private readonly string _modality;
    public UnavailableModalityAdapter(string modality) =>
        _modality = modality;
    public string Modality => _modality;
    public string Capability => "CONTRACT_ONLY";
    public void Validate(ModalityInput i)
    {
        if (i.Modality != _modality)
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"{_modality}Adapter received {i.Modality}");
    }
    private ExecutorError Unavailable() => new(
        ConvErr.SpeculativeDecoderUnavailable,
        $"{_modality} adapter is CONTRACT_ONLY — no native {_modality} capability");
    public Dictionary<string, object?> Encode(ModalityInput i)
    { Validate(i); throw Unavailable(); }
    public Dictionary<string, object?> Project(ModalityInput i)
    { Validate(i); throw Unavailable(); }
    public long Budget(ModalityInput i)
    { Validate(i); return i.TokenBudget; }   // budgeting is a contract op
    public string CacheKey(ModalityInput i)
    { Validate(i); return i.ContentHash; }
}

/// <summary>§15 video sampling — contract + frame metadata only.
/// No video encoder exists; UNAVAILABLE adapters already guarantee
/// that nothing downstream pretends otherwise.</summary>
internal static class VideoSamplingPolicy
{
    public const string Format = "star-video-sampling/v1";
    public static readonly string[] Modes =
        { "UNIFORM", "SCENE_CHANGE", "KEYFRAME", "ADAPTIVE" };

    /// <summary>Validate a sampling plan: closed-mode vocab, positive
    /// frame budget, every sampled frame hash-addressed.</summary>
    public static Dictionary<string, object?> Plan(
        string mode, int frameBudget,
        List<Dictionary<string, object?>> frameMetadata)
    {
        if (!Modes.Contains(mode))
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"unknown video sampling mode '{mode}'");
        if (frameBudget <= 0)
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                "frame_budget must be positive");
        var frames = frameMetadata
            .OrderBy(f => Convert.ToInt64(f["frame_index"]))
            .Take(frameBudget).ToList();
        foreach (var f in frames)
            if (!f.ContainsKey("frame_hash"))
                throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                    "sampled frame missing frame_hash");
        return new Dictionary<string, object?>
        {
            ["format"] = Format, ["mode"] = mode,
            ["frame_budget"] = frameBudget,
            ["frames"] = frames,
            ["sampling_trace"] = frames.Select(f => f["frame_index"])
                .ToList(),
        };
    }
}

/// <summary>§21 MultimodalRuntime — ONE orchestrator; the modality
/// registry is closed and capability-labelled.</summary>
internal sealed class MultimodalRuntime
{
    public const string Format = "star-multimodal-runtime/v1";

    private readonly Dictionary<string, IModalityAdapter> _adapters = new()
    {
        ["TEXT"] = new TextAdapter(),
        ["IMAGE"] = new VisionAdapter(),
        ["VIDEO"] = new UnavailableModalityAdapter("VIDEO"),
        ["AUDIO"] = new UnavailableModalityAdapter("AUDIO"),
        // DOCUMENT rides the text path after preprocessing/grounding —
        // a dedicated adapter is a future contract, not a claim.
        ["DOCUMENT"] = new TextAdapter(),
    };

    public IModalityAdapter AdapterFor(string modality)
    {
        if (!_adapters.TryGetValue(modality.ToUpperInvariant(),
                                   out var a))
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"no adapter for modality '{modality}'");
        return a;
    }

    public Dictionary<string, object?> Capabilities() => new()
    {
        ["format"] = Format,
        ["modalities"] = _adapters.ToDictionary(
            kv => kv.Key, kv => (object?)new Dictionary<string, object?>
            {
                ["capability"] = kv.Value.Capability,
                ["adapter"] = kv.Value.GetType().Name,
            }),
        ["decoder"] = "single xc-fused-1 decoder — no per-modality decoders",
    };
}
