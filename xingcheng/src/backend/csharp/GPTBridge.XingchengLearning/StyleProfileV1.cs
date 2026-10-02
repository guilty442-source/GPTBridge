// StyleProfileV1.cs — star-style-profile/v1 (§23).
//
// Built-in linguistic behavior profiles. Each profile defines only
// linguistic behavior — never permissions, tools, or governance.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class StyleProfileV1
{
    public const string Format = "star-style-profile/v1";

    public static readonly string[] BuiltInProfiles =
    {
        "neutral", "professional", "concise", "technical",
        "friendly", "creative", "literary", "roleplay",
        "brainstorm", "storytelling",
    };

    public sealed class Profile
    {
        public string ProfileId = "";
        public string Name = "";
        public string Tone = "neutral";
        public string Verbosity = "medium";
        public string Formality = "medium";
        public string Creativity = "low";
        public string Language = "zh-TW";
        public string PunctuationStyle = "traditional";
        public string VocabularyPreference = "taiwan";
        public string DescriptionDensity = "medium";
        public string DialogueDensity = "medium";
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "STYLE_PROFILE_INVALID", "style profile must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "STYLE_PROFILE_INVALID", $"expected format {Format}");
        var required = new[]
        {
            "profile_id", "name", "tone", "verbosity", "formality",
            "creativity", "language", "punctuation_style",
            "vocabulary_preference", "description_density", "dialogue_density",
        };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "STYLE_PROFILE_INVALID", $"missing field {k}");
        string profileId = el.GetProperty("profile_id").GetString() ?? "";
        if (!BuiltInProfiles.Contains(profileId))
            throw new ExecutorError(
                "STYLE_PROFILE_INVALID", $"unknown profile {profileId}");
        string language = el.GetProperty("language").GetString() ?? "";
        if (language != "zh-TW")
            throw new ExecutorError(
                "STYLE_PROFILE_INVALID",
                "only zh-TW is supported as the primary language");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["profile_id"] = profileId,
            ["language"] = language,
        };
    }

    public static Dictionary<string, object?> ValidateFile(
        string toolRoot, string file)
    {
        var el = ToolContracts.ReadJson(file, "STYLE_PROFILE_INVALID");
        var r = Validate(el);
        try
        {
            string dir = Path.Combine(toolRoot, XcPaths.LogsRel);
            Directory.CreateDirectory(dir);
            File.AppendAllText(
                Path.Combine(dir, "style-profiles.jsonl"),
                CanonicalJson.Canonical(el) + "\n");
        }
        catch { /* ledger append is best-effort */ }
        return r;
    }
}
