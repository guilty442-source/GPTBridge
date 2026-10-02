// SteerabilityController.cs — §14 Steerability engine.
//
// Priority: Governance → Safety → User Explicit Constraint →
// Task Contract → Persona → Style Default.
// Persona must never override the user's explicit current request.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class SteerabilityController
{
    public const string Format = "star-steerability/v1";

    public static readonly string[] PriorityOrder =
    {
        "governance", "safety", "user_explicit", "task_contract",
        "persona", "style_default",
    };

    public sealed class SteerRequest
    {
        public string Tone = "";
        public string Style = "";
        public string Length = "";
        public string Format = "";
        public string PersonaId = "";
        public string Language = "zh-TW";
        public string Creativity = "";
        public string ReasoningEffort = "";
        public string CitationPreference = "";
        public string OutputStructure = "";
    }

    public sealed class SteerResult
    {
        public string SelectedTone = "";
        public string SelectedStyle = "";
        public string SelectedLength = "";
        public string SelectedFormat = "";
        public string SelectedLanguage = "zh-TW";
        public string SelectedCreativity = "";
        public string SelectedReasoningEffort = "";
        public string SelectedCitationPreference = "";
        public string SelectedOutputStructure = "";
        public List<string> AppliedPriority = new();
        public List<string> Rejected = new();
    }

    public static SteerResult Resolve(
        SteerRequest request,
        Dictionary<string, string>? personaDefaults,
        Dictionary<string, string>? styleDefaults)
    {
        var result = new SteerResult();
        var applied = new List<string>();
        var rejected = new List<string>();

        string Select(string userVal, string? personaVal, string? styleVal)
        {
            if (!string.IsNullOrEmpty(userVal))
            {
                applied.Add("user_explicit");
                if (personaVal != null && personaVal != userVal)
                    rejected.Add($"persona:{personaVal}");
                if (styleVal != null && styleVal != userVal)
                    rejected.Add($"style:{styleVal}");
                return userVal;
            }
            if (personaVal != null && !string.IsNullOrEmpty(personaVal))
            {
                applied.Add("persona");
                if (styleVal != null && styleVal != personaVal)
                    rejected.Add($"style:{styleVal}");
                return personaVal;
            }
            if (styleVal != null && !string.IsNullOrEmpty(styleVal))
            {
                applied.Add("style_default");
                return styleVal;
            }
            return "";
        }

        result.SelectedTone = Select(
            request.Tone, personaDefaults?.GetValueOrDefault("tone"),
            styleDefaults?.GetValueOrDefault("tone"));
        result.SelectedStyle = Select(
            request.Style, personaDefaults?.GetValueOrDefault("style"),
            styleDefaults?.GetValueOrDefault("style"));
        result.SelectedLength = Select(
            request.Length, personaDefaults?.GetValueOrDefault("length"),
            styleDefaults?.GetValueOrDefault("length"));
        result.SelectedFormat = Select(
            request.Format, personaDefaults?.GetValueOrDefault("format"),
            styleDefaults?.GetValueOrDefault("format"));
        result.SelectedLanguage = Select(
            request.Language, personaDefaults?.GetValueOrDefault("language"),
            styleDefaults?.GetValueOrDefault("language"));
        result.SelectedCreativity = Select(
            request.Creativity, personaDefaults?.GetValueOrDefault("creativity"),
            styleDefaults?.GetValueOrDefault("creativity"));
        result.SelectedReasoningEffort = Select(
            request.ReasoningEffort,
            personaDefaults?.GetValueOrDefault("reasoning_effort"),
            styleDefaults?.GetValueOrDefault("reasoning_effort"));
        result.SelectedCitationPreference = Select(
            request.CitationPreference,
            personaDefaults?.GetValueOrDefault("citation_preference"),
            styleDefaults?.GetValueOrDefault("citation_preference"));
        result.SelectedOutputStructure = Select(
            request.OutputStructure,
            personaDefaults?.GetValueOrDefault("output_structure"),
            styleDefaults?.GetValueOrDefault("output_structure"));

        result.AppliedPriority = applied;
        result.Rejected = rejected;
        return result;
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "STEERABILITY_INVALID", "steer request must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "STEERABILITY_INVALID", $"expected format {Format}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
        };
    }
}
