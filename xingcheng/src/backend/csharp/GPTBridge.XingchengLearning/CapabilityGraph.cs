// CapabilityGraph.cs — ``star-capability-graph/v1`` (capability
// unification directive §12-§13, §45).
//
// Dependency graph over the CapabilityRegistry. Explicit dependencies
// replace implicit "suite passed ⇒ capability mature" reasoning
// (§13: a tool_call_format PASS never certifies tool_calling — its
// REQUIRES closure must hold too). EVALUATED_BY edges are generated
// from CapabilityEvaluationMap so the eval surface has exactly one
// source of truth. §45: the regression suite for a training lane is
// derived from the graph — never assembled by hand.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityGraph
{
    public const string Format = "star-capability-graph/v1";
    public const string Rel =
        "xingcheng/runtime/state/capability-graph.json";

    // §12 closed edge vocabulary.
    public static readonly string[] EdgeTypes =
    {
        "REQUIRES", "SUPPORTS", "REGRESSES_WITH", "SHARES_DATA_WITH",
        "SHARES_EXPERT_WITH", "EVALUATED_BY",
    };

    public sealed record Edge(string From, string To, string Type);

    /// <summary>Structural seed edges (§13, §32-§37). REQUIRES is
    /// read from → to: ``from`` depends on ``to``.</summary>
    public static readonly Edge[] Seed =
    {
        // §13 tool_calling prerequisites.
        new("tool_calling", "instruction_following", "REQUIRES"),
        new("tool_calling", "structured_output", "REQUIRES"),
        new("tool_calling", "context_tracking", "REQUIRES"),
        new("tool_calling", "tool_formatting", "REQUIRES"),
        // Dialogue chain.
        new("multi_turn", "context_tracking", "REQUIRES"),
        new("structured_output", "instruction_following", "REQUIRES"),
        new("coding", "structured_output", "REQUIRES"),
        // §37 RAG decomposition.
        new("rag", "retrieval_understanding", "REQUIRES"),
        new("rag", "grounding", "REQUIRES"),
        new("rag", "citation_attribution", "REQUIRES"),
        new("rag", "context_integration", "REQUIRES"),
        new("citation_attribution", "grounding", "REQUIRES"),
        new("vision_execution", "vision", "REQUIRES"),
        // SUPPORTS — helps but is not a hard prerequisite.
        new("native_thinking", "math", "SUPPORTS"),
        new("native_thinking", "coding", "SUPPORTS"),
        new("long_context", "context_tracking", "SUPPORTS"),
        new("external_tool_execution", "tool_calling", "SUPPORTS"),
        new("resource_governance", "speculative_decode", "SUPPORTS"),
        // Shared-weight regression couplings (§44).
        new("tool_calling", "structured_output", "REGRESSES_WITH"),
        new("tool_calling", "instruction_following", "REGRESSES_WITH"),
        new("multi_turn", "context_tracking", "REGRESSES_WITH"),
        new("reading", "rag", "REGRESSES_WITH"),
        new("coding", "structured_output", "REGRESSES_WITH"),
        new("math", "native_thinking", "REGRESSES_WITH"),
        new("structured_output", "instruction_following",
            "REGRESSES_WITH"),
        // Shared data / expert couplings.
        new("instruction_following", "multi_turn", "SHARES_DATA_WITH"),
        new("instruction_following", "tool_calling",
            "SHARES_DATA_WITH"),
        new("reading", "rag", "SHARES_DATA_WITH"),
        new("math", "coding", "SHARES_DATA_WITH"),
        new("expert_routing", "math", "SHARES_EXPERT_WITH"),
        new("expert_routing", "coding", "SHARES_EXPERT_WITH"),
        new("expert_routing", "native_thinking", "SHARES_EXPERT_WITH"),
    };

    private static string Path_(string toolRoot) =>
        Path.Combine(toolRoot, Rel.Replace('/', Path.DirectorySeparatorChar));

    /// <summary>Full edge set: structural seeds plus EVALUATED_BY
    /// edges derived from the eval map (capability → surface).</summary>
    public static List<Edge> All()
    {
        var edges = new List<Edge>(Seed);
        foreach (var d in CapabilityRegistry.Canonical)
            foreach (var surface in
                     CapabilityEvaluationMap.SurfacesFor(d.CapabilityId))
                edges.Add(new Edge(d.CapabilityId, surface,
                                   "EVALUATED_BY"));
        return edges;
    }

    /// <summary>Transitive REQUIRES closure of a capability
    /// (excluding the capability itself).</summary>
    public static string[] RequiresClosure(string capabilityId)
    {
        string? start = CapabilityRegistry.Resolve(capabilityId);
        if (start == null) return Array.Empty<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var stack = new Stack<string>();
        stack.Push(start);
        while (stack.Count > 0)
        {
            string cur = stack.Pop();
            foreach (var e in Seed)
                if (e.Type == "REQUIRES" && e.From == cur &&
                    seen.Add(e.To))
                    stack.Push(e.To);
        }
        return seen.ToArray();
    }

    /// <summary>§45 derived regression suite for a capability under
    /// training: itself, its REQUIRES closure, its REGRESSES_WITH
    /// neighbourhood (both directions) and every protected
    /// (frozen/mature) capability — floors must hold (§68/§92).</summary>
    public static string[] RegressionSuiteFor(
        string capabilityId, string toolRoot)
    {
        string? canonical = CapabilityRegistry.Resolve(capabilityId);
        if (canonical == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{capabilityId}' is not in the registry");
        var suite = new HashSet<string>(StringComparer.Ordinal)
            { canonical };
        foreach (var c in RequiresClosure(canonical)) suite.Add(c);
        foreach (var e in Seed)
            if (e.Type == "REGRESSES_WITH")
            {
                if (e.From == canonical) suite.Add(e.To);
                if (e.To == canonical) suite.Add(e.From);
            }
        // Protected capabilities always join the regression suite.
        var matState = Maturation300M.LoadState(toolRoot);
        if (matState.TryGetValue("capabilities", out object? raw) &&
            raw is Dictionary<string, object?> caps)
            foreach (var p in caps)
                if (p.Value is Dictionary<string, object?> cap &&
                    cap.TryGetValue("status", out object? s) &&
                    s?.ToString() == "frozen" &&
                    CapabilityRegistry.Resolve(p.Key) is string id)
                    suite.Add(id);
        return suite.OrderBy(s => s, StringComparer.Ordinal).ToArray();
    }

    /// <summary>Emit and persist the graph document.</summary>
    public static Dictionary<string, object?> Emit(string toolRoot)
    {
        var edges = All();
        var rows = edges.Select(e => (object?)new Dictionary<string,
            object?>
        {
            ["from"] = e.From, ["to"] = e.To, ["type"] = e.Type,
        }).ToList();
        var byType = edges.GroupBy(e => e.Type)
            .ToDictionary(g => g.Key,
                          g => (object?)g.Count());
        var doc = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["generated_at"] = XcPaths.IsoNow(),
            ["node_count"] = CapabilityRegistry.Canonical.Length,
            ["edge_count"] = edges.Count,
            ["edge_types"] = EdgeTypes.Cast<object?>().ToList(),
            ["edges"] = rows,
            ["summary"] = new Dictionary<string, object?>
                { ["by_type"] = byType },
        };
        ModelLifecycle.AtomicWrite(
            Path_(toolRoot), CanonicalJson.PrettyDict(doc) + "\n");
        doc["ok"] = true;
        doc["graph_file"] = Rel;
        return doc;
    }

    /// <summary>Graph integrity: every edge type is in the §12
    /// vocabulary and every capability endpoint resolves to a
    /// canonical id (EVALUATED_BY targets are eval surfaces, not
    /// capabilities).</summary>
    public static List<string> Validate()
    {
        var errors = new List<string>();
        foreach (var e in All())
        {
            if (!EdgeTypes.Contains(e.Type))
                errors.Add($"bad_edge_type:{e.Type}");
            if (CapabilityRegistry.Resolve(e.From) == null)
                errors.Add($"unknown_edge_from:{e.From}");
            if (e.Type != "EVALUATED_BY" &&
                CapabilityRegistry.Resolve(e.To) == null)
                errors.Add($"unknown_edge_to:{e.To}");
        }
        return errors;
    }
}
