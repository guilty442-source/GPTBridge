// ArchitectureGate.cs — §15 GLM-5.3 lesson: prefer post-training and
// agent-workflow improvements over backbone churn. The gate
// ARCHITECTURE_CHANGE_REQUIRED must be true before any new
// architecture axis is even proposed. Runtime-only work like this
// task never satisfies it — and must never try to.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ArchitectureGate
{
    /// <summary>Every condition must be evidenced for a change to be
    /// justified. Any missing/false condition fails closed with
    /// ARCHITECTURE_CHANGE_NOT_JUSTIFIED.</summary>
    private static readonly string[] Conditions =
        {
            "existing_architecture_cannot_solve",
            "runtime_optimization_ineffective",
            "data_improvement_ineffective",
            "post_training_path_ineffective",
            "independent_benchmark",
            "ablation_done",
            "memory_impact_assessed",
            "latency_impact_assessed",
        };

    /// <summary>Evaluate a justification document
    /// {"format":"star-architecture-justification/v1",
    ///  "conditions":{...each condition: bool}, "evidence":{...}}.
    /// §54 (capability unification): a document that claims
    /// ``existing_architecture_cannot_solve`` for a named capability
    /// must be backed by an ``star-architecture-limitation-evidence/v1``
    /// record in the governed state — the claim alone is never
    /// enough.</summary>
    public static Dictionary<string, object?> Evaluate(
        JsonElement el, string toolRoot = "")
    {
        var unmet = new List<object?>();
        var evidence = new Dictionary<string, object?>();
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty("conditions", out var c) &&
            c.ValueKind == JsonValueKind.Object)
        {
            foreach (var cond in Conditions)
            {
                bool met = c.TryGetProperty(cond, out var v) &&
                    v.ValueKind == JsonValueKind.True;
                evidence[cond] = met;
                if (!met) unmet.Add(cond);
            }
        }
        else
            unmet.AddRange(Conditions.Cast<object?>());

        // §54: claiming the architecture cannot solve a named
        // capability requires recorded plateau evidence
        // (ArchitectureLimitationEvidence) — the claim is
        // demoted to unmet when the ledger has none.
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty("capability", out var capEl) &&
            capEl.ValueKind == JsonValueKind.String &&
            capEl.GetString() is { Length: > 0 } capName)
        {
            string? cap = CapabilityRegistry.Resolve(capName);
            bool claimed = evidence.TryGetValue(
                "existing_architecture_cannot_solve", out var e) &&
                e is true;
            if (claimed && cap != null && toolRoot.Length > 0 &&
                !ArchitectureLimitationEvidence.HasFor(toolRoot, cap))
            {
                evidence["existing_architecture_cannot_solve"] = false;
                if (!unmet.Contains("existing_architecture_cannot_solve"))
                    unmet.Add("existing_architecture_cannot_solve");
                evidence["limitation_evidence_missing"] = true;
            }
            evidence["limitation_capability"] = cap ?? capName;
        }

        bool required = unmet.Count == 0;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-architecture-gate/v1",
            ["architecture_change_required"] = required,
            ["error_if_forced"] =
                required ? null : "ARCHITECTURE_CHANGE_NOT_JUSTIFIED",
            ["unmet_conditions"] = unmet,
            ["conditions"] = evidence,
            ["rule"] = "post-training/agent-workflow first; " +
                       "backbone changes require all conditions",
        };
    }
}
