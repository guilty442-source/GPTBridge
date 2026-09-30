// CreativeMode.cs — §11 Creative Writing Mode.
//
// Profile controls temperature, top_p, repetition penalty, style
// diversity, description density, dialogue density, narrative
// continuity, and variation budget. Never modifies safety rules,
// tool permissions, or data access permissions.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CreativeMode
{
    public const string Format = "star-creative-mode/v1";

    public static readonly string[] Profiles =
    {
        "PRECISE", "BALANCED", "CREATIVE", "ROLEPLAY", "STORY", "BRAINSTORM",
    };

    public sealed class CreativeProfile
    {
        public string ProfileId = "BALANCED";
        public double Temperature = 0.7;
        public double TopP = 0.9;
        public double RepetitionPenalty = 1.05;
        public double StyleDiversity = 0.5;
        public double DescriptionDensity = 0.5;
        public double DialogueDensity = 0.5;
        public double NarrativeContinuity = 0.7;
        public double VariationBudget = 0.3;
    }

    public static CreativeProfile Resolve(string profileId)
    {
        return profileId switch
        {
            "PRECISE" => new CreativeProfile
            {
                ProfileId = profileId, Temperature = 0.3, TopP = 0.85,
                RepetitionPenalty = 1.1, StyleDiversity = 0.2,
                DescriptionDensity = 0.3, DialogueDensity = 0.2,
                NarrativeContinuity = 0.9, VariationBudget = 0.1,
            },
            "BALANCED" => new CreativeProfile
            {
                ProfileId = profileId, Temperature = 0.7, TopP = 0.9,
                RepetitionPenalty = 1.05, StyleDiversity = 0.5,
                DescriptionDensity = 0.5, DialogueDensity = 0.5,
                NarrativeContinuity = 0.7, VariationBudget = 0.3,
            },
            "CREATIVE" => new CreativeProfile
            {
                ProfileId = profileId, Temperature = 0.9, TopP = 0.95,
                RepetitionPenalty = 1.0, StyleDiversity = 0.8,
                DescriptionDensity = 0.7, DialogueDensity = 0.6,
                NarrativeContinuity = 0.5, VariationBudget = 0.6,
            },
            "ROLEPLAY" => new CreativeProfile
            {
                ProfileId = profileId, Temperature = 0.8, TopP = 0.92,
                RepetitionPenalty = 1.02, StyleDiversity = 0.7,
                DescriptionDensity = 0.6, DialogueDensity = 0.8,
                NarrativeContinuity = 0.6, VariationBudget = 0.5,
            },
            "STORY" => new CreativeProfile
            {
                ProfileId = profileId, Temperature = 0.85, TopP = 0.93,
                RepetitionPenalty = 1.0, StyleDiversity = 0.75,
                DescriptionDensity = 0.8, DialogueDensity = 0.7,
                NarrativeContinuity = 0.8, VariationBudget = 0.5,
            },
            "BRAINSTORM" => new CreativeProfile
            {
                ProfileId = profileId, Temperature = 1.0, TopP = 0.98,
                RepetitionPenalty = 0.95, StyleDiversity = 1.0,
                DescriptionDensity = 0.4, DialogueDensity = 0.3,
                NarrativeContinuity = 0.3, VariationBudget = 0.9,
            },
            _ => new CreativeProfile(),
        };
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "CREATIVE_MODE_INVALID", "creative mode must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "CREATIVE_MODE_INVALID", $"expected format {Format}");
        string profileId = el.GetProperty("profile_id").GetString() ?? "";
        if (!Profiles.Contains(profileId))
            throw new ExecutorError(
                "CREATIVE_MODE_INVALID", $"unknown profile {profileId}");
        var profile = Resolve(profileId);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["profile_id"] = profileId,
            ["temperature"] = profile.Temperature,
            ["top_p"] = profile.TopP,
            ["repetition_penalty"] = profile.RepetitionPenalty,
        };
    }
}
