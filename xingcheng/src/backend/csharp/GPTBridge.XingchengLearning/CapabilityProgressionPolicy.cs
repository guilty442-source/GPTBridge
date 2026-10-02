// CapabilityProgressionPolicy.cs — progression facade over the
// ordered maturation sequence (capability unification directive
// §14-§16).
//
// The CapabilityRegistry answers "which capabilities exist?"; this
// policy answers "which capability may train now?". They are
// different questions and must never merge (§16): the registry is the
// canonical catalog, progression is a *policy* that paces one
// capability at a time through the fixed 300M order with
// freeze-after-pass, governor reopen and regression dependencies.
// Implementation state stays in Maturation300M — this facade only
// re-expresses it in registry terms so callers consume
// canonical capability_id values.

namespace GPTBridge.XingchengLearning;

internal static class CapabilityProgressionPolicy
{
    /// <summary>The only capability a training lane may declare right
    /// now — the sequence head while the phase is active; null once
    /// every capability is resolved (phase complete).</summary>
    public static string? NextAllowed(string toolRoot)
    {
        var state = Maturation300M.LoadState(toolRoot);
        string phase = state.TryGetValue("phase", out object? ph)
            ? ph?.ToString() ?? "" : "";
        if (phase != Maturation300M.PhaseId) return null;
        return Maturation300M.Head(state)?.Id;
    }

    /// <summary>Progression status in registry terms: phase, the
    /// next-allowed capability, per-capability stage and the graph
    /// regression dependencies each sequenced capability carries.</summary>
    public static Dictionary<string, object?> Status(string toolRoot)
    {
        var state = Maturation300M.LoadState(toolRoot);
        string phase = state.TryGetValue("phase", out object? ph)
            ? ph?.ToString() ?? Maturation300M.PhaseId
            : Maturation300M.PhaseId;
        var caps = state.TryGetValue("capabilities", out object? raw) &&
                   raw is Dictionary<string, object?> c
            ? c : new Dictionary<string, object?>();
        var rows = new List<object?>();
        foreach (var spec in Maturation300M.Sequence)
        {
            string stage = caps.TryGetValue(spec.Id, out object? srow) &&
                           srow is Dictionary<string, object?> sr &&
                           sr.TryGetValue("status", out object? sv)
                ? sv?.ToString() ?? "pending" : "pending";
            rows.Add(new Dictionary<string, object?>
            {
                ["capability_id"] = spec.Id,
                ["stage"] = stage,
                ["weight_version"] =
                    Maturation300M.WeightVersionFor(
                        Maturation300M.IndexOf(spec.Id)),
                ["eval_kind"] = spec.EvalKind,
                ["regression_dependencies"] =
                    CapabilityGraph.RequiresClosure(spec.Id)
                        .Cast<object?>().ToList(),
            });
        }
        var head = phase == Maturation300M.PhaseId
            ? Maturation300M.Head(state)?.Id : null;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-capability-progression/v1",
            ["phase"] = phase,
            ["model_scale"] = Maturation300M.ModelScale,
            ["architecture_generation"] =
                Maturation300M.ArchitectureGeneration,
            ["next_allowed_capability"] = head,
            ["sequence"] = rows,
            ["checked_at"] = XcPaths.IsoNow(),
        };
    }

    /// <summary>Admission check for a declared capability: unknown ids
    /// fail closed through the registry; ordering is delegated to the
    /// maturation guard (which already fails closed).</summary>
    public static void GuardAdmission(string toolRoot, string capability)
    {
        if (capability.Length > 0 &&
            CapabilityRegistry.Resolve(capability) == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{capability}' is not in the " +
                "CapabilityRegistry");
        Maturation300M.GuardSequence(toolRoot, capability);
    }
}
