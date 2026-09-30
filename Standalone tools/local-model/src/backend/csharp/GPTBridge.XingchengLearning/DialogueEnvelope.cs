// DialogueEnvelope.cs — ``star-dialogue-envelope/v2`` (§2.2) +
// ModalityInput (§4.1).
//
// Harmony-class ideas absorbed as a native contract — no external
// tokenizer/chat-template runtime is ever consulted (§2.2 last rule).
//
//   Roles         : system | developer | user | assistant | tool
//   Content types : text | structured | tool_call | tool_result |
//                   evidence | vision | audio | video
//   Required per  : role, content_type, content, request_id,
//   message         sequence, visibility, provenance
//
// visibility:   PUBLIC (user-visible) | INTERNAL (reasoning/tool
//               traces — §2.3 keeps them out of final_response)
// provenance:   {source, generation, produced_by} — where the message
//               content originated.
//
// §4.1 ModalityInput is the unified per-modality input record; AUDIO and
// VIDEO are contract-only this round (§4.2 — no native audio/video
// capability is claimed).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class DialogueEnvelope
{
    public const string Format = "star-dialogue-envelope/v2";

    public static readonly string[] Roles =
        { "system", "developer", "user", "assistant", "tool" };
    public static readonly string[] ContentTypes =
        { "text", "structured", "tool_call", "tool_result", "evidence",
          "vision", "audio", "video" };
    public static readonly string[] Visibilities =
        { "PUBLIC", "INTERNAL" };
    public static readonly string[] RequiredFields =
        { "role", "content_type", "content", "request_id", "sequence",
          "visibility", "provenance" };

    public sealed class Provenance
    {
        public string Source = "";         // human | model | tool | rag
        public string Generation = "";     // active generation tag
        public string ProducedBy = "";     // component name

        public Dictionary<string, object?> ToDict() => new()
        {
            ["source"] = Source,
            ["generation"] = Generation,
            ["produced_by"] = ProducedBy,
        };

        public static Provenance Parse(Dictionary<string, object?> d)
            => new()
            {
                Source = d.TryGetValue("source", out var s)
                    ? s?.ToString() ?? "" : "",
                Generation = d.TryGetValue("generation", out var g)
                    ? g?.ToString() ?? "" : "",
                ProducedBy = d.TryGetValue("produced_by", out var p)
                    ? p?.ToString() ?? "" : "",
            };
    }

    /// <summary>Validate + normalize one envelope message. Any contract
    /// breach fails closed (STRUCTURED_SCHEMA_FAILED — a malformed
    /// message is never passed downstream).</summary>
    public static Dictionary<string, object?> ValidateMessage(
        Dictionary<string, object?> msg)
    {
        foreach (string key in RequiredFields)
            if (!msg.ContainsKey(key) || msg[key] is null)
                throw new ExecutorError(
                    ConvErr.StructuredSchemaFailed,
                    $"envelope missing: {key}");
        string role = msg["role"]?.ToString() ?? "";
        if (!Roles.Contains(role))
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed, $"unknown role '{role}'");
        string ctype = msg["content_type"]?.ToString() ?? "";
        if (!ContentTypes.Contains(ctype))
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                $"unknown content_type '{ctype}'");
        string vis = msg["visibility"]?.ToString() ?? "";
        if (!Visibilities.Contains(vis))
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                $"unknown visibility '{vis}'");
        if (msg["provenance"] is not Dictionary<string, object?> prov)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                "provenance must be an object");
        var p = Provenance.Parse(prov);
        if (p.Source.Length == 0)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                "provenance.source required");
        if (msg["sequence"] is not (long or int or double))
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                "sequence must be a number");
        if ((msg["request_id"]?.ToString() ?? "").Length == 0)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed, "empty request_id");
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["role"] = role,
            ["content_type"] = ctype,
            ["content"] = msg["content"],
            ["request_id"] = msg["request_id"]!.ToString()!,
            ["sequence"] = msg["sequence"],
            ["visibility"] = vis,
            ["provenance"] = p.ToDict(),
        };
    }

    /// <summary>Validate a whole transcript (ordered by sequence —
    /// input order must already be monotonic; a regressed sequence
    /// number fails closed).</summary>
    public static List<Dictionary<string, object?>> ValidateTranscript(
        List<Dictionary<string, object?>> messages)
    {
        var norm = new List<Dictionary<string, object?>>(messages.Count);
        long lastSeq = -1;
        foreach (var m in messages)
        {
            var v = ValidateMessage(m);
            long seq = Convert.ToInt64(v["sequence"]);
            if (seq <= lastSeq)
                throw new ExecutorError(
                    ConvErr.StructuredSchemaFailed,
                    $"sequence regression at {seq}");
            lastSeq = seq;
            norm.Add(v);
        }
        return norm;
    }
}

/// <summary>§4.1 ModalityInput — the unified per-modality input record
/// carried on a v2 envelope message's ``content`` when content_type is
/// vision/audio/video.</summary>
internal sealed class ModalityInput
{
    public const string Format = "star-modality-input/v1";
    public static readonly string[] Modalities =
        { "TEXT", "IMAGE", "VIDEO", "AUDIO", "DOCUMENT" };

    public string Modality = "TEXT";       // modality_id type
    public string MimeType = "";
    public string ContentHash = "";        // sha256:<hex>
    public long SizeBytes;
    public double DurationS;               // audio/video; 0 otherwise
    public string Dimensions = "";         // e.g. "1280x720"
    public string SamplingPolicy = "";     // video: see VideoSamplingPolicy
    public long TokenBudget;               // modality token budget
    public string CachePolicy = "default"; // cache tier hint
    public string Source = "";             // origin (path/uri/inline)

    public void Validate()
    {
        if (!Modalities.Contains(Modality))
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                $"unknown modality '{Modality}'");
        if (MimeType.Length == 0)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed, "mime_type required");
        if (!ContentHash.StartsWith("sha256:", StringComparison.Ordinal))
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                "content_hash must be sha256:<hex>");
        if (SizeBytes < 0 || DurationS < 0 || TokenBudget < 0)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                "negative modality field");
        // §4.2: audio/video are contract-only — present, parseable, but
        // never claim native processing.
        if (Modality is "AUDIO" or "VIDEO")
            Capability = "CONTRACT_ONLY";
    }

    /// <summary>ACTIVE | CONTRACT_ONLY — set during Validate().</summary>
    public string Capability { get; private set; } = "ACTIVE";

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = Format,
        ["modality"] = Modality,
        ["mime_type"] = MimeType,
        ["content_hash"] = ContentHash,
        ["size_bytes"] = SizeBytes,
        ["duration_s"] = DurationS,
        ["dimensions"] = Dimensions,
        ["sampling_policy"] = SamplingPolicy,
        ["token_budget"] = TokenBudget,
        ["cache_policy"] = CachePolicy,
        ["source"] = Source,
        ["capability"] = Capability,
    };

    public static ModalityInput Parse(Dictionary<string, object?> d)
    {
        var m = new ModalityInput
        {
            Modality = d.TryGetValue("modality", out var v)
                ? v?.ToString() ?? "TEXT" : "TEXT",
            MimeType = d.TryGetValue("mime_type", out var mt)
                ? mt?.ToString() ?? "" : "",
            ContentHash = d.TryGetValue("content_hash", out var ch)
                ? ch?.ToString() ?? "" : "",
            SizeBytes = d.TryGetValue("size_bytes", out var sb)
                && sb is long or int ? Convert.ToInt64(sb) : 0,
            DurationS = d.TryGetValue("duration_s", out var ds)
                && ds is long or int or double
                    ? Convert.ToDouble(ds) : 0,
            Dimensions = d.TryGetValue("dimensions", out var dm)
                ? dm?.ToString() ?? "" : "",
            SamplingPolicy = d.TryGetValue("sampling_policy", out var sp)
                ? sp?.ToString() ?? "" : "",
            TokenBudget = d.TryGetValue("token_budget", out var tb)
                && tb is long or int ? Convert.ToInt64(tb) : 0,
            CachePolicy = d.TryGetValue("cache_policy", out var cp)
                ? cp?.ToString() ?? "default" : "default",
            Source = d.TryGetValue("source", out var src)
                ? src?.ToString() ?? "" : "",
        };
        m.Validate();
        return m;
    }
}
