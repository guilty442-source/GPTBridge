// StarAgentRuntime.cs — ONE agent runtime (§22) absorbing gpt-oss
// tool use, MiMo long-horizon, MiniMax agent/computer, Nemotron
// collaborative agents and Hy3 agent ideas. No per-source agent stacks
// (§22 last rule).
//
//   §9   star-computer-action/v1 — OBSERVE/LOCATE/PROPOSE_* /VERIFY.
//        Everything is PROPOSE-scoped: no system_execution, no
//        external_execution, no cross-tool authority. Xingcheng's
//        existing permission rules dominate.
//   §23  AgentWorkGraph — bounded multi-node orchestration with
//        max_nodes / max_depth / max_parallel / budgets. Unbounded
//        agent recursion is forbidden.
//   §24  ResultIntegrator — schema validation, evidence
//        reconciliation, conflict detection, confidence aggregation;
//        conflicting high-confidence evidence returns
//        CONFLICT_REQUIRES_RESOLUTION, never a silent pick.
//   §25  Cache-aware execution — every work node consults
//        ContextCacheManager before running an expensive step.

namespace GPTBridge.XingchengLearning;

// ------------------------------------------------- §9 computer actions

/// <summary>§9 star-computer-action/v1 — proposal-scoped UI/computer
/// actions. PROPOSE_* verbs produce an action proposal record for a
/// governed executor; nothing here executes.</summary>
internal static class ComputerActionContract
{
    public const string Format = "star-computer-action/v1";
    public static readonly string[] Actions =
        { "OBSERVE", "LOCATE", "PROPOSE_CLICK", "PROPOSE_TYPE",
          "PROPOSE_SCROLL", "PROPOSE_OPEN", "VERIFY" };

    /// <summary>Validate one action record — closed action vocab,
    /// proposal-only authority. Any claim of execution authority fails
    /// closed.</summary>
    public static Dictionary<string, object?> Validate(
        Dictionary<string, object?> action)
    {
        string act = action.TryGetValue("action", out var a)
            ? a?.ToString() ?? "" : "";
        if (!Actions.Contains(act))
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                $"unknown computer action '{act}'");
        string authority = action.TryGetValue("authority", out var au)
            ? au?.ToString() ?? "PROPOSE" : "PROPOSE";
        if (authority is not ("OBSERVE" or "PROPOSE" or "VERIFY"))
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                $"authority escalation rejected: '{authority}'");
        var norm = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["action"] = act,
            ["authority"] = authority,
            ["target"] = action.TryGetValue("target", out var t)
                ? t : null,
            ["payload"] = action.TryGetValue("payload", out var p)
                ? p : null,
            ["requires_executor"] = act.StartsWith("PROPOSE_"),
        };
        return norm;
    }
}

// ------------------------------------------------------- §23 workgraph

internal enum WorkNodeState
{
    WAITING, READY, RUNNING, VERIFYING, DONE, FAILED, CANCELLED,
}

internal sealed class AgentWorkNode
{
    public required string TaskId;
    public string Role = "worker";            // planner | worker | verifier
    public string Capability = "";            // required capability tag
    public Dictionary<string, object?> Input = new();
    public List<string> Dependencies = new(); // task_ids
    public long BudgetTokens;                 // per-node token budget
    public int Depth;                          // graph depth of this node
    public WorkNodeState State = WorkNodeState.WAITING;
    public Dictionary<string, object?>? Result;
}

/// <summary>§23 AgentWorkGraph — bounded DAG of agent nodes. Bounded
/// fan-out, bounded depth, bounded parallelism and aggregate budgets.
/// Nodes run when their dependencies are DONE; a failed dependency
/// fails the node closed.</summary>
internal sealed class AgentWorkGraph
{
    public const string Format = "star-agent-workgraph/v1";

    public int MaxNodes = 32;
    public int MaxDepth = 4;
    public int MaxParallel = 4;
    public long ToolBudget = 64;
    public long TimeBudgetS = 600;

    private readonly List<AgentWorkNode> _nodes = new();
    public IReadOnlyList<AgentWorkNode> Nodes => _nodes;

    /// <summary>Add a node — every bound is enforced at insert.</summary>
    public AgentWorkNode Add(AgentWorkNode n)
    {
        if (_nodes.Count >= MaxNodes)
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                $"workgraph node cap {MaxNodes}");
        if (n.Depth > MaxDepth)
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                $"workgraph depth cap {MaxDepth}");
        if (n.TaskId.Length == 0)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed, "task_id required");
        if (_nodes.Any(x => x.TaskId == n.TaskId))
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                $"duplicate task_id '{n.TaskId}'");
        _nodes.Add(n);
        return n;
    }

    /// <summary>Tick one scheduling round: mark READY nodes whose deps
    /// are DONE, fail nodes whose deps FAILED/CANCELLED. Returns the
    /// newly-ready nodes — the caller executes them (parallelism is
    /// capped here, not by the scheduler).</summary>
    public List<AgentWorkNode> Schedule()
    {
        var done = _nodes.Where(n => n.State == WorkNodeState.DONE)
            .Select(n => n.TaskId).ToHashSet();
        var dead = _nodes.Where(n => n.State is WorkNodeState.FAILED
                                   or WorkNodeState.CANCELLED)
            .Select(n => n.TaskId).ToHashSet();
        var ready = new List<AgentWorkNode>();
        int running = _nodes.Count(n =>
            n.State is WorkNodeState.RUNNING or WorkNodeState.VERIFYING);
        foreach (var n in _nodes)
        {
            if (n.State != WorkNodeState.WAITING) continue;
            if (n.Dependencies.Any(d => dead.Contains(d)))
            {
                n.State = WorkNodeState.FAILED;   // fail-closed
                continue;
            }
            if (!n.Dependencies.All(d => done.Contains(d))) continue;
            if (running + ready.Count >= MaxParallel) continue;
            n.State = WorkNodeState.READY;
            ready.Add(n);
        }
        return ready;
    }

    /// <summary>Terminal check — true when no node can still progress
    /// (everything DONE/FAILED/CANCELLED or nothing schedulable).</summary>
    public bool Settled() =>
        _nodes.All(n => n.State is WorkNodeState.DONE
                             or WorkNodeState.FAILED
                             or WorkNodeState.CANCELLED)
        || Schedule().Count == 0
           && _nodes.All(n => n.State is not (WorkNodeState.RUNNING
                                              or WorkNodeState.VERIFYING));
}

// ------------------------------------------------- §24 result merging

/// <summary>§24 ResultIntegrator — merges multiple expert results.
/// Conflicting high-confidence evidence never resolves by picking
/// one — it surfaces CONFLICT_REQUIRES_RESOLUTION.</summary>
internal static class ResultIntegrator
{
    public const string Format = "star-result-integrator/v1";

    /// <param name="results">each: {task_id, payload, evidence[],
    /// confidence} — confidence in [0,1].</param>
    public static Dictionary<string, object?> Integrate(
        List<Dictionary<string, object?>> results)
    {
        if (results.Count == 0)
            return new() { ["format"] = Format,
                           ["status"] = "NO_RESULTS" };
        // Evidence reconciliation: group evidence keys by value; two
        // keys with different values at confidence >= 0.8 = conflict.
        var highConf = new Dictionary<string, HashSet<string>>();
        bool conflict = false;
        double confidenceSum = 0;
        foreach (var r in results)
        {
            double conf = r.TryGetValue("confidence", out var c)
                && c is long or int or double
                    ? Convert.ToDouble(c) : 0;
            confidenceSum += conf;
            if (conf < 0.8 || r["evidence"]
                    is not List<object?> evs) continue;
            foreach (var e in evs.OfType<Dictionary<string, object?>>())
            {
                string k = e.TryGetValue("key", out var kk)
                    ? kk?.ToString() ?? "" : "";
                string v = e.TryGetValue("value", out var vv)
                    ? vv?.ToString() ?? "" : "";
                if (!highConf.TryGetValue(k, out var seen))
                    highConf[k] = seen = new HashSet<string>();
                seen.Add(v);
                if (seen.Count > 1) conflict = true;
            }
        }
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["status"] = conflict
                ? "CONFLICT_REQUIRES_RESOLUTION"
                : "MERGED",
            ["merged_confidence"] = confidenceSum / results.Count,
            ["conflict_keys"] = highConf
                .Where(kv => kv.Value.Count > 1)
                .Select(kv => (object?)kv.Key).ToList(),
            ["contributors"] = results.Count,
        };
    }
}

// ------------------------------------------------ §22 unified runtime

/// <summary>§22 StarAgentRuntime — composition of the governed
/// components. Everything it offers already exists or is defined in
/// this batch; the runtime is the single entry point so callers never
/// reach past the composition.</summary>
internal sealed class StarAgentRuntime
{
    public const string Format = "star-agent-runtime/v1";

    /// <summary>Long-horizon coordination is served by the existing
    /// static LongHorizonTaskCoordinator (AgentRuntime.cs) — composed
    /// here by reference, not duplicated (§22).</summary>
    public static Type CoordinatorType =>
        typeof(LongHorizonTaskCoordinator);
    public ToolDecisionGate Gate { get; } = new();
    public ContextCacheManager Cache { get; } = new();
    public LongContextRuntime LongContext { get; } = new();

    /// <summary>§25 cache-aware pre-execution check — a work node
    /// consults the cache tiers before any expensive step. Returns the
    /// hit record when found.</summary>
    public (Dictionary<string, object?>? value, CacheHitRecord? hit)
        CheckCaches(CacheKey key, string activeGeneration)
        => Cache.GetAny(
            new[] { CacheLevel.L1_SESSION, CacheLevel.L2_PREFIX,
                    CacheLevel.L3_CONTENT, CacheLevel.L4_ARTIFACT },
            key, activeGeneration);
}
