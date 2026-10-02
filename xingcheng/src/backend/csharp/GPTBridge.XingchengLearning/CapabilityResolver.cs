// CapabilityResolver.cs — the single canonical capability resolver
// (authority-convergence directive §15–§16, §78–§80).
//
// Every governance entry that accepts a capability name goes through
// this resolver — never a local alias table, never a free string
// (§15/§79). ``Require`` canonicalizes aliases and validates against
// the CapabilityRegistry: an unregistered name fails
// CAPABILITY_UNKNOWN (§16) and is never auto-created. Descriptor,
// dependency, eval-mapping, runtime-profile and failure-pool lookups
// all derive from the resolved canonical id, so no surface can fork
// its own capability vocabulary.

namespace GPTBridge.XingchengLearning;

internal static class CapabilityResolver
{
    /// <summary>Canonicalize any historical spelling to the registry
    /// capability_id. Empty input returns empty (an untagged lane
    /// stays untagged — the caller decides whether a tag is
    /// required); an unresolvable non-empty name fails
    /// CAPABILITY_UNKNOWN (§16).</summary>
    public static string Require(string name)
    {
        string raw = (name ?? "").Trim();
        if (raw.Length == 0) return "";
        string? id = CapabilityRegistry.Resolve(raw);
        if (id == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{raw}' is not in the CapabilityRegistry");
        return id;
    }

    /// <summary>Canonical id + descriptor row. Throws
    /// CAPABILITY_UNKNOWN for unregistered or empty input — use for
    /// entries where a capability tag is mandatory (§63).</summary>
    public static CapabilityDescriptor Descriptor(string name)
    {
        string id = Require(name);
        if (id.Length == 0)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                "capability id required");
        return CapabilityRegistry.Get(id)
            ?? throw new ExecutorError("CAPABILITY_UNKNOWN", name);
    }

    /// <summary>Ordered REQUIRES closure from the canonical graph —
    /// the dependency set every recovery/admission decision must
    /// respect (§17). Unknown names fail closed.</summary>
    public static string[] Dependencies(string name)
    {
        string id = Require(name);
        return id.Length == 0 ? Array.Empty<string>()
            : CapabilityGraph.RequiresClosure(id);
    }

    /// <summary>Auto-derived regression suite (§18): the capability,
    /// its REQUIRES closure, REGRESSES_WITH neighbourhood and every
    /// protected capability — never a hand-picked list.</summary>
    public static string[] RegressionSuite(string name, string toolRoot)
    {
        string id = Require(name);
        if (id.Length == 0)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                "capability id required for regression suite");
        return CapabilityGraph.RegressionSuiteFor(id, toolRoot);
    }

    /// <summary>Evaluation surfaces bound to the capability
    /// (CapabilityEvaluationMap) — a canonical id only input.</summary>
    public static string[] EvalSurfaces(string name)
    {
        string id = Require(name);
        return id.Length == 0 ? Array.Empty<string>()
            : CapabilityEvaluationMap.SurfacesFor(id);
    }

    /// <summary>§86 runtime profile lookup: returns the
    /// ``star-capability-runtime-profile/v1`` document — requirements
    /// only, never kernel selection.</summary>
    public static Dictionary<string, object?> RuntimeProfile(
        string name) => CapabilityRuntimeProfile.Emit(name);

    /// <summary>Failure-pool class for a canonical capability — the
    /// pool layout is a storage detail, never a second vocabulary:
    /// callers resolve first, then bucket. Unknown/non-empty input
    /// fails CAPABILITY_UNKNOWN before this point (§15).</summary>
    public static string PoolClass(string canonicalId) =>
        canonicalId switch
        {
            "instruction_following" => "instruction",
            "context_tracking" or "long_context" => "context",
            "multi_turn" => "multi-turn",
            "structured_output" => "structured-output",
            "tool_calling" or "tool_formatting"
                or "external_tool_execution" => "tool-call",
            "reading" => "reading",
            "rag" or "retrieval_understanding"
                or "citation_attribution"
                or "context_integration" => "rag",
            "grounding" => "grounding",
            "math" => "math",
            "coding" => "coding",
            "vision" or "vision_execution" => "vision",
            "native_thinking" => "thinking",
            "expert_routing" => "routing",
            _ => "reasoning",
        };

    /// <summary>One-call admission answer for a governance surface:
    /// canonical id, descriptor, dependencies and eval surfaces.
    /// Untagged input returns ok=false without throwing.</summary>
    public static Dictionary<string, object?> Inspect(string name)
    {
        string id = Require(name);
        if (id.Length == 0)
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["format"] = CapabilityDescriptor.Format,
                ["capability"] = name ?? "",
                ["capability_id"] = null,
            };
        var d = CapabilityRegistry.Get(id)!;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = CapabilityDescriptor.Format,
            ["capability"] = name ?? "",
            ["capability_id"] = id,
            ["descriptor"] = d.ToDict(),
            ["dependencies"] = CapabilityGraph.RequiresClosure(id)
                .Cast<object?>().ToList(),
            ["eval_surfaces"] = CapabilityEvaluationMap.SurfacesFor(id)
                .Cast<object?>().ToList(),
            ["failure_pool_class"] = PoolClass(id),
        };
    }
}
