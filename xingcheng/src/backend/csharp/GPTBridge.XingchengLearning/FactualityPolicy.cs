// FactualityPolicy.cs — §17 Factuality vs creativity separation.
//
// Modes: FACTUAL_STRICT (no free addition of unknown facts),
// GROUNDED (requires evidence), GENERAL (allows general model
// knowledge), FICTIONAL (allows creation of characters, places,
// events, worlds). Fictional content must be marked as fictional
// context and never written to canonical knowledge.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class FactualityPolicy
{
    public const string Format = "star-factuality-policy/v1";

    public static readonly string[] Modes =
    {
        "FACTUAL_STRICT", "GROUNDED", "GENERAL", "FICTIONAL",
    };

    public sealed class Policy
    {
        public string Mode = "GENERAL";
        public bool MarkFictionalContext = true;
        public bool AllowCanonicalWrite = false;
        public double ConfidenceThreshold = 0.7;
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "FACTUALITY_POLICY_INVALID", "policy must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "FACTUALITY_POLICY_INVALID", $"expected format {Format}");
        string mode = el.GetProperty("mode").GetString() ?? "";
        if (!Modes.Contains(mode))
            throw new ExecutorError(
                "FACTUALITY_POLICY_INVALID", $"bad mode {mode}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format, ["mode"] = mode,
        };
    }

    public static bool CanWriteToCanonical(string mode) => false;

    public static bool MustMarkFictional(string mode) =>
        mode == "FICTIONAL";
}
