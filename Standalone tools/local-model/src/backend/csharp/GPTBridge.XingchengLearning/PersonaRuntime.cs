// PersonaRuntime.cs — §8/§9 Persona runtime.
//
// Persona controls expression, role, tone, creative style, and
// interaction. It never controls governance authority, tool permission,
// system permission, safety boundary, or generation lifecycle.
// Hard limit: PERSONA_CANNOT_GRANT_AUTHORITY.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class PersonaRuntime
{
    public const string Format = "star-persona/v1";

    public sealed class Persona
    {
        public string PersonaId = "";
        public string Name = "";
        public string LanguageStyle = "zh-TW";
        public string Tone = "neutral";
        public string Verbosity = "medium";
        public string Formality = "medium";
        public string Creativity = "low";
        public string Humor = "none";
        public string Role = "";
        public string FictionalSetting = "";
        public string SpeakingPattern = "";
        public string FormatPreferences = "";
        public string BehaviorConstraints = "";
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "PERSONA_INVALID", "persona must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "PERSONA_INVALID", $"expected format {Format}");
        var required = new[]
        {
            "persona_id", "name", "language_style", "tone", "verbosity",
            "formality", "creativity", "humor", "role", "fictional_setting",
            "speaking_pattern", "format_preferences", "behavior_constraints",
        };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "PERSONA_INVALID", $"missing field {k}");
        string personaId = el.GetProperty("persona_id").GetString() ?? "";
        if (personaId.Length == 0)
            throw new ExecutorError(
                "PERSONA_INVALID", "persona_id empty");
        string language = el.GetProperty("language_style").GetString() ?? "";
        if (language != "zh-TW")
            throw new ExecutorError(
                "PERSONA_INVALID", "only zh-TW is supported");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["persona_id"] = personaId,
            ["language_style"] = language,
        };
    }

    public static bool CanGrantAuthority(string personaId) => false;

    public static Dictionary<string, object?> ValidateFile(
        string toolRoot, string file)
    {
        var el = ToolContracts.ReadJson(file, "PERSONA_INVALID");
        var r = Validate(el);
        try
        {
            string dir = Path.Combine(toolRoot, XcPaths.LogsRel);
            Directory.CreateDirectory(dir);
            File.AppendAllText(
                Path.Combine(dir, "personas.jsonl"),
                CanonicalJson.Canonical(el) + "\n");
        }
        catch { /* ledger append is best-effort */ }
        return r;
    }
}
