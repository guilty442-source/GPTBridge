// DialogueEnvelopeV2.cs — star-dialogue-envelope/v2 (§15).
//
// Unified instruction priority: Governance → System Contract →
// Application Contract → User Request → Persona → Style.
// Conflicts are resolved explicitly, never left to the model to guess.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class DialogueEnvelopeV2
{
    public const string Format = "star-dialogue-envelope/v2";

    public static readonly string[] InstructionLayers =
    {
        "governance", "system_contract", "application_contract",
        "user_request", "persona", "style",
    };

    public sealed class Envelope
    {
        public string EnvelopeId = "";
        public string InteractionMode = "CHAT";
        public string FactualityMode = "GENERAL";
        public string StyleProfile = "neutral";
        public string? PersonaId = null;
        public Dictionary<string, string> Instructions = new();
        public List<string> RejectedInstructions = new();
        public string TraceId = "";
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "ENVELOPE_INVALID", "dialogue envelope must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "ENVELOPE_INVALID", $"expected format {Format}");
        var required = new[]
        {
            "envelope_id", "interaction_mode", "factuality_mode",
            "style_profile", "instructions", "rejected_instructions",
            "trace_id",
        };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "ENVELOPE_INVALID", $"missing field {k}");
        if (el.GetProperty("instructions").ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "ENVELOPE_INVALID", "instructions must be an object");
        if (el.GetProperty("rejected_instructions").ValueKind
            != JsonValueKind.Array)
            throw new ExecutorError(
                "ENVELOPE_INVALID", "rejected_instructions must be an array");
        string mode = el.GetProperty("interaction_mode").GetString() ?? "";
        var validModes = new[]
        {
            "CHAT", "CREATIVE", "ROLEPLAY", "RESEARCH",
            "AGENT", "CODING", "DOCUMENT",
        };
        if (!validModes.Contains(mode))
            throw new ExecutorError(
                "ENVELOPE_INVALID", $"bad interaction_mode {mode}");
        string factuality = el.GetProperty("factuality_mode").GetString() ?? "";
        var validFactuality = new[]
        {
            "FACTUAL_STRICT", "GROUNDED", "GENERAL", "FICTIONAL",
        };
        if (!validFactuality.Contains(factuality))
            throw new ExecutorError(
                "ENVELOPE_INVALID", $"bad factuality_mode {factuality}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["interaction_mode"] = mode,
            ["factuality_mode"] = factuality,
            ["instruction_count"] =
                el.GetProperty("instructions").EnumerateObject().Count(),
            ["rejected_count"] =
                el.GetProperty("rejected_instructions").GetArrayLength(),
        };
    }

    public static Dictionary<string, object?> ValidateFile(
        string toolRoot, string file)
    {
        var el = ToolContracts.ReadJson(file, "ENVELOPE_INVALID");
        var r = Validate(el);
        try
        {
            string dir = Path.Combine(toolRoot, XcPaths.LogsRel);
            Directory.CreateDirectory(dir);
            File.AppendAllText(
                Path.Combine(dir, "dialogue-envelopes.jsonl"),
                CanonicalJson.Canonical(el) + "\n");
        }
        catch { /* ledger append is best-effort */ }
        return r;
    }
}
