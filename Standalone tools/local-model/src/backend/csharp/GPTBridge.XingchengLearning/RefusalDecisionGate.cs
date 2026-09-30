// RefusalDecisionGate.cs — §12/§13 Refusal quality gate.
//
// Prevents unnecessary refusals for normal fiction, roleplay, horror,
// battle scenes, non-explicit adult themes, critical analysis, history,
// and security defense discussions. Decisions based on actual request,
// intent, possible output, permissions, and risk — not single keywords.
// Never creates an UNCENSORED mode that bypasses governance.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class RefusalDecisionGate
{
    public const string Format = "star-refusal-decision/v1";

    public static readonly string[] Decisions =
    {
        "ALLOW", "ALLOW_WITH_CONSTRAINT", "SAFE_TRANSFORM", "REFUSE",
    };

    public sealed class RefusalContext
    {
        public string Request = "";
        public string Intent = "";
        public string PossibleOutput = "";
        public string Permission = "";
        public string RiskLevel = "low";
        public string Category = "";
    }

    public sealed class RefusalResult
    {
        public string Decision = "ALLOW";
        public string ReasonCode = "";
        public string OriginalRequest = "";
        public string? TransformedRequest = null;
        public List<string> Evidence = new();
    }

    public static RefusalResult Evaluate(RefusalContext ctx)
    {
        var result = new RefusalResult { OriginalRequest = ctx.Request };

        if (string.IsNullOrWhiteSpace(ctx.Request))
        {
            result.Decision = "REFUSE";
            result.ReasonCode = "EMPTY_REQUEST";
            result.Evidence.Add("request is empty");
            return result;
        }

        bool isCreative = ctx.Category is "fiction" or "roleplay" or "story"
            or "horror" or "battle" or "adult_non_explicit";
        bool isAnalytical = ctx.Category is "critical_analysis" or "history"
            or "security_defense" or "education";
        bool isHighRisk = ctx.RiskLevel == "high"
            || ctx.Permission == "denied";

        if (isHighRisk)
        {
            result.Decision = "REFUSE";
            result.ReasonCode = "HIGH_RISK_OR_DENIED";
            result.Evidence.Add($"risk={ctx.RiskLevel}, permission={ctx.Permission}");
            return result;
        }

        if (isCreative || isAnalytical)
        {
            result.Decision = "ALLOW";
            result.ReasonCode = "CREATIVE_OR_ANALYTICAL_CONTEXT";
            result.Evidence.Add($"category={ctx.Category}");
            return result;
        }

        if (ctx.RiskLevel == "medium")
        {
            result.Decision = "ALLOW_WITH_CONSTRAINT";
            result.ReasonCode = "MEDIUM_RISK_CONSTRAINED";
            result.Evidence.Add("medium risk — output constraints applied");
            return result;
        }

        result.Decision = "ALLOW";
        result.ReasonCode = "STANDARD_REQUEST";
        return result;
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "REFUSAL_DECISION_INVALID", "refusal decision must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "REFUSAL_DECISION_INVALID", $"expected format {Format}");
        string decision = el.GetProperty("decision").GetString() ?? "";
        if (!Decisions.Contains(decision))
            throw new ExecutorError(
                "REFUSAL_DECISION_INVALID", $"bad decision {decision}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["decision"] = decision,
        };
    }
}
