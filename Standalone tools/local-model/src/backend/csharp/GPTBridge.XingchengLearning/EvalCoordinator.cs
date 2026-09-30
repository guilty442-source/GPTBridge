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
          "refusal-quality", "zh-tw-naturalness",
          // inference efficiency plane (§47) — measured evidence, no
          // hardcoded gains.
          "expert-residency", "expert-offload", "hybrid-prefix",
          "rag-prefix", "prefill-artifact", "pd-pipeline",
          "memory-tier" };

    private static readonly string[] ResultRequired =
        { "suite", "case", "generation", "bundle", "pass", "metric",
          "threshold", "failure", "timestamp", "artifact_hash" };

    /// <summary>§37 evaluation_scope values — a result says WHAT layer
    /// it measured, so a capability score can never masquerade as a
    /// core/architecture statement.</summary>
    public static readonly string[] Scopes =
        { "CORE", "COMPONENT", "RUNTIME", "CAPABILITY", "SERVICE",
          "GOVERNANCE" };

    /// <summary>Default scope per suite (§37).</summary>
    public static string ScopeFor(string suite) => suite switch
    {
        "runtime-parity" or "precision-parity" or "kv-cache" or
        "recurrent-state" or "long-context" => "RUNTIME",
        "moe-routing" or "vision" => "COMPONENT",
        "citation" or "rag" or "agent" or "coding" or "fim" or
        "tool-decision" or "structured-output" or
        "creative-writing" or "roleplay" or "story-continuation" or
        "character-dialogue" or "style-transfer" or "brainstorm" or
        "world-building" or "long-form-continuity" or
        "refusal-quality" or "zh-tw-naturalness" => "CAPABILITY",
        "generation-migration" or "bundle-provenance" => "GOVERNANCE",
        _ => "SERVICE",
    };

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
        // §37: evaluation_scope is optional input but always resolved
        // in output; a supplied scope must be a legal value.
        string scope =
            el.TryGetProperty("evaluation_scope", out var es) &&
            es.ValueKind == JsonValueKind.String &&
            (es.GetString() ?? "").Length > 0
                ? es.GetString()! : ScopeFor(suite);
        if (!Scopes.Contains(scope))
            throw new ExecutorError(
                "EVAL_RESULT_INVALID", $"bad evaluation_scope {scope}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ResultFormat,
            ["suite"] = suite,
            ["evaluation_scope"] = scope,
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
