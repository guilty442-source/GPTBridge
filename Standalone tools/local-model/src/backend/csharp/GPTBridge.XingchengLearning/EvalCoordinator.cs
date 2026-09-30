// EvalCoordinator.cs — §30 unified evaluation plane (C# side).
//
// One registry of eval suites, one result contract
// star-eval-result/v1. C++ xc_modeltool executes the suites that need
// the native engine; this coordinator validates results and records
// them for generation certification.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class EvalCoordinator
{
    public const string ResultFormat = "star-eval-result/v1";

    /// <summary>Formal suite names (§30). Capability-training gates
    /// stay owned by Evaluation.cs; this plane is evidence-only.</summary>
    public static readonly string[] Suites =
        { "runtime-parity", "precision-parity", "kv-cache",
          "recurrent-state", "long-context", "tool-decision",
          "structured-output", "citation", "rag", "agent", "coding",
          "fim", "vision", "moe-routing", "generation-migration",
          "bundle-provenance",
          // creative regression plane (§26) — eval-only while
          // capability training is frozen.
          "creative-writing", "roleplay", "story-continuation",
          "character-dialogue", "style-transfer", "brainstorm",
          "world-building", "long-form-continuity",
          "refusal-quality", "zh-tw-naturalness" };

    private static readonly string[] ResultRequired =
        { "suite", "case", "generation", "bundle", "pass", "metric",
          "threshold", "failure", "timestamp", "artifact_hash" };

    /// <summary>Validate a star-eval-result/v1 record.</summary>
    public static Dictionary<string, object?> ValidateResult(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "EVAL_RESULT_INVALID", "eval result must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != ResultFormat)
            throw new ExecutorError(
                "EVAL_RESULT_INVALID",
                $"expected format {ResultFormat}");
        foreach (var k in ResultRequired)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "EVAL_RESULT_INVALID", $"missing field {k}");
        string suite = el.GetProperty("suite").GetString() ?? "";
        if (!Suites.Contains(suite))
            throw new ExecutorError(
                "EVAL_RESULT_INVALID", $"unknown suite {suite}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ResultFormat,
            ["suite"] = suite,
            ["pass"] = el.GetProperty("pass").GetBoolean(),
        };
    }

    public static Dictionary<string, object?> SuitesStatus()
        => new()
        {
            ["ok"] = true,
            ["format"] = ResultFormat,
            ["suites"] = Suites.Cast<object?>().ToList(),
            ["executor"] = "xc_modeltool eval (native) / C# gates " +
                           "(contract suites)",
        };
}
