// PracticalEval.cs — §18 star-practical-capability-suite/v1 (Hy3
// real-world evaluation) + the batch-2 EvaluationCoordinator.
//
// Real tasks, not benchmark-shaped ones: each case is scored on
// task_success, correctness, completion, tool_efficiency, latency,
// resource_cost and recovery_count — not just a scalar accuracy.
//
// EvaluationCoordinator is the single front door for suite runs:
// star-native-eval-suite/v1 and star-capability-suite/v1 continue to
// flow through Evaluation.RunEvaluation; practical suites record
// case-level results with the same governed repository path.

namespace GPTBridge.XingchengLearning;

/// <summary>§18 suite contract — the 12 case categories are a closed
/// vocabulary; a case outside it cannot be recorded.</summary>
internal static class PracticalCapabilitySuite
{
    public const string Format = "star-practical-capability-suite/v1";

    public static readonly string[] Categories =
    {
        "real_dialogue", "long_instruction", "context_follow",
        "multi_tool", "document_work", "coding_workflow",
        "recovery", "ambiguity", "conflicting_evidence",
        "agent_resume", "structured_output", "grounded_answer",
    };

    /// <summary>§18 per-case record — all six metrics required.</summary>
    public static readonly string[] CaseMetrics =
    {
        "task_success", "correctness", "completion",
        "tool_efficiency", "latency", "resource_cost",
        "recovery_count",
    };

    /// <summary>Validate one case record — closed category + complete
    /// metrics, fail-closed.</summary>
    public static Dictionary<string, object?> ValidateCase(
        Dictionary<string, object?> c)
    {
        string cat = c.TryGetValue("category", out var v)
            ? v?.ToString() ?? "" : "";
        if (!Categories.Contains(cat))
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"unknown practical category '{cat}'");
        var missing = CaseMetrics
            .Where(m => !c.ContainsKey(m)).ToList();
        if (missing.Count > 0)
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"case missing metrics: {string.Join(",", missing)}");
        return c;
    }

    /// <summary>Validate a suite document (suite_id + cases[]).</summary>
    public static Dictionary<string, object?> ValidateSuite(
        Dictionary<string, object?> suite)
    {
        if (!suite.ContainsKey("suite_id"))
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                "suite missing suite_id");
        if (suite["cases"] is not List<object?> cases || cases.Count == 0)
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                "suite has no cases");
        foreach (var c in cases.OfType<Dictionary<string, object?>>())
            ValidateCase(c);
        suite["format_version"] = Format;
        return suite;
    }

    /// <summary>Aggregate case results into per-category pass rates —
    /// same regression-gate shape as star-capability-suite/v1 so the
    /// existing comparison lane works unchanged.</summary>
    public static Dictionary<string, object?> Aggregate(
        List<Dictionary<string, object?>> caseResults)
    {
        var perCat = Categories.ToDictionary(c => c, _ => (pass: 0, n: 0));
        foreach (var c in caseResults)
        {
            string cat = c["category"]!.ToString()!;
            var t = perCat[cat];
            bool ok = c.TryGetValue("task_success", out var ts)
                      && ts is bool b && b;
            perCat[cat] = (t.pass + (ok ? 1 : 0), t.n + 1);
        }
        var categories = perCat.ToDictionary(
            kv => kv.Key,
            kv => (object?)new Dictionary<string, object?>
            {
                ["cases"] = kv.Value.n,
                ["pass_rate"] = kv.Value.n > 0
                    ? (double)kv.Value.pass / kv.Value.n : 0.0,
            });
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["categories"] = categories,
            ["total_cases"] = caseResults.Count,
        };
    }
}

/// <summary>§22/§37 EvaluationCoordinator — one entry point for every
/// suite format. Practical suites execute through the capability lane
/// once the tool grows a mode for them; until then they record
/// DEFERRED_EXECUTION rather than faking a run.</summary>
internal static class EvaluationCoordinator
{
    /// <summary>Route by suite format. Unknown formats fail closed —
    /// the coordinator never guesses an executor.</summary>
    public static Dictionary<string, object?> Run(
        TransformerTrainingRepository repo,
        string adapterId, string candidateArtifact,
        string suitePath, string? baselineArtifact,
        string evaluatedBy, string? corpusManifest = null,
        bool chat = false, string? suiteFormatOverride = null)
    {
        if (suiteFormatOverride == PracticalCapabilitySuite.Format)
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["passed"] = false,
                ["status"] = "DEFERRED_EXECUTION",
                ["reason"] = "practical suite executor not yet wired",
            };
        return Evaluation.RunEvaluation(
            repo, adapterId, candidateArtifact, suitePath,
            baselineArtifact, evaluatedBy, corpusManifest, chat);
    }
}
