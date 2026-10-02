// CapabilityEvaluationMap.cs — ``star-capability-eval-map/v1``
// (capability unification directive §17-§19, §85).
//
// Maps every evaluation surface (capability-suite category, recovery
// suite, probe kind) onto canonical capability_id values — one
// capability may carry many evals (§18) and one eval may support many
// capabilities (§19). Existing category spellings keep their names;
// the map is the single translation layer, so callers never fork
// their own name vocabulary.

namespace GPTBridge.XingchengLearning;

internal static class CapabilityEvaluationMap
{
    public const string Format = "star-capability-eval-map/v1";

    /// <summary>Evaluation surface → canonical capability ids.
    /// Keys are the existing suite/category spellings (§85: eval
    /// categories are not renamed).</summary>
    public static readonly (string Surface, string[] Capabilities)[]
        Map =
    {
        // The 10-category capability suite (existing categories kept).
        ("zh-TW", new[]
            { "instruction_following", "reading", "multi_turn" }),
        ("en", new[]
            { "instruction_following", "reading", "multi_turn" }),
        ("math", new[] { "math" }),
        ("code", new[] { "coding" }),
        ("reading", new[] { "reading" }),
        ("multi_turn", new[] { "multi_turn", "context_tracking" }),
        ("context_tracking", new[] { "context_tracking" }),
        ("instruction", new[] { "instruction_following" }),
        ("tool_call_format", new[]
            { "tool_calling", "structured_output", "tool_formatting" }),
        ("expert_routing", new[] { "expert_routing" }),
        // Recovery / probe surfaces.
        ("structured_output", new[]
            { "structured_output", "tool_calling" }),
        ("tool_calling", new[]
            { "tool_calling", "structured_output" }),
        ("tool-decision", new[] { "tool_calling" }),
        ("fim", new[] { "coding", "structured_output" }),
        ("rag", new[]
            { "rag", "grounding", "citation_attribution",
              "retrieval_understanding", "context_integration" }),
        ("citation", new[] { "citation_attribution", "rag" }),
        ("vision", new[] { "vision", "vision_execution" }),
        ("system1", new[] { "native_thinking" }),
        ("thinking", new[] { "native_thinking" }),
        ("native_thinking", new[] { "native_thinking" }),
        ("thinking_eval", new[] { "native_thinking" }),
        ("long-context", new[] { "long_context", "context_tracking" }),
        ("mtp-speedup", new[] { "speculative_decode" }),
        ("prefix-reuse", new[] { "prefix_reuse" }),
        ("moe-routing", new[] { "expert_routing" }),
    };

    /// <summary>Canonical capability ids an evaluation surface feeds;
    /// empty when the surface is unmapped (orphan eval — §102).</summary>
    public static string[] CapabilitiesFor(string surface)
    {
        foreach (var (s, caps) in Map)
            if (string.Equals(s, surface, StringComparison.Ordinal) ||
                string.Equals(s, surface,
                              StringComparison.OrdinalIgnoreCase))
                return caps;
        return Array.Empty<string>();
    }

    /// <summary>All evaluation surfaces that feed one canonical
    /// capability (§18: one capability, many evals).</summary>
    public static string[] SurfacesFor(string capabilityId)
    {
        string? canonical = CapabilityRegistry.Resolve(capabilityId);
        if (canonical == null) return Array.Empty<string>();
        return Map.Where(m => m.Capabilities.Contains(canonical))
                  .Select(m => m.Surface)
                  .ToArray();
    }

    /// <summary>Emit the map document: surface → capability ids plus
    /// the reverse capability → surfaces index, with orphan surfaces
    /// and unmapped registry capabilities flagged.</summary>
    public static Dictionary<string, object?> Emit()
    {
        var forward = new Dictionary<string, object?>();
        var reverse = new Dictionary<string, List<string>>();
        var orphanSurfaces = new List<object?>();
        foreach (var (surface, caps) in Map)
        {
            var resolved = caps
                .Select(c => CapabilityRegistry.Resolve(c) ?? c)
                .Distinct().ToList();
            forward[surface] = resolved.Cast<object?>().ToList();
            if (resolved.Count == 0) orphanSurfaces.Add(surface);
            foreach (var c in resolved)
            {
                if (!reverse.TryGetValue(c, out var l))
                    reverse[c] = l = new List<string>();
                l.Add(surface);
            }
        }
        var unmapped = CapabilityRegistry.Canonical
            .Where(d => !reverse.ContainsKey(d.CapabilityId) &&
                        d.CapabilityClass != "SERVICE_AUGMENTED")
            .Select(d => (object?)d.CapabilityId).ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["generated_at"] = XcPaths.IsoNow(),
            ["surfaces"] = forward,
            ["by_capability"] = reverse.ToDictionary(
                kv => kv.Key,
                kv => (object?)kv.Value.Cast<object?>().ToList()),
            ["orphan_surfaces"] = orphanSurfaces,
            ["unmapped_capabilities"] = unmapped,
        };
    }
}
