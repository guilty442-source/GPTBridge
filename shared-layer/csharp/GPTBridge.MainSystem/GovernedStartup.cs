// Governed startup engine — C# port of core_system/governed_startup_*.
//
// Contract parity with governed_startup_types.py / governed_startup_verify.py /
// governed_startup_signal.py (A192/E167 deadlock-free-governed-startup,
// A191/E166 dependency-classification): the DAG shape, cycle detection,
// classification violations, phase-order check, core-ready check and the
// failure/status signal payloads are identical so the audit plane can diff
// the two implementations during the migration window.

using System.Text.Json.Nodes;

namespace GPTBridge.MainSystem;

/// <summary>A192 SINGLE-FLIGHT startup generation record.</summary>
public sealed record StartupGeneration
{
    public required string GenerationId { get; init; }
    public required string ReleaseId { get; init; }
    public required string StartedAt { get; init; }
    public string CurrentPhase { get; set; } = "";
    public bool CoreReady { get; set; }
    public bool DeferredActive { get; set; }
}

/// <summary>A192 PHASE-5 acyclic-critical-path dependency DAG.</summary>
public sealed class DependencyDag
{
    private readonly IReadOnlyList<DependencyDeclaration> _dependencies;

    public DependencyDag(IReadOnlyList<DependencyDeclaration> dependencies) =>
        _dependencies = dependencies;

    public IReadOnlyList<DependencyDeclaration> Dependencies => _dependencies;

    public IReadOnlyList<DependencyDeclaration> CoreCritical =>
        _dependencies.Where(d => d.IsCoreCritical).ToList();

    public IReadOnlyList<DependencyDeclaration> CapabilityCritical =>
        _dependencies.Where(d => d.IsCapabilityCritical).ToList();

    public IReadOnlyList<DependencyDeclaration> Optional =>
        _dependencies.Where(d => d.IsOptional).ToList();

    /// <summary>
    /// required_by graph cycle check — mirrors the Python DFS exactly:
    /// an edge required_by -> identity is recorded per declaration.
    /// </summary>
    public bool IsAcyclic
    {
        get
        {
            var graph = new Dictionary<string, List<string>>();
            foreach (var dep in _dependencies)
            {
                graph.TryAdd(dep.Identity, []);
                if (dep.RequiredBy.Length > 0 && dep.RequiredBy != dep.Identity)
                {
                    graph.TryAdd(dep.RequiredBy, []);
                    graph[dep.RequiredBy].Add(dep.Identity);
                }
            }

            var visited = new HashSet<string>();
            var inStack = new HashSet<string>();

            bool HasCycle(string node)
            {
                if (inStack.Contains(node)) return true;
                if (!visited.Add(node)) return false;
                inStack.Add(node);
                foreach (var neighbor in graph.GetValueOrDefault(node, []))
                    if (HasCycle(neighbor)) return true;
                inStack.Remove(node);
                return false;
            }

            return !graph.Keys.Any(HasCycle);
        }
    }

    /// <summary>
    /// Submission ordering — deepest consumer chain first, matching
    /// phases_execution._dependency_start_order so a bounded pool starts
    /// prerequisites before their dependents.
    /// </summary>
    public IReadOnlyList<DependencyDeclaration> StartOrder()
    {
        var byIdentity = _dependencies.ToDictionary(d => d.Identity);
        var depth = new Dictionary<string, int>();

        int Depth(string identity, HashSet<string> seen)
        {
            if (depth.TryGetValue(identity, out var cached)) return cached;
            if (!byIdentity.TryGetValue(identity, out var dep)
                || !byIdentity.ContainsKey(dep.RequiredBy)
                || !seen.Add(identity))
            {
                return depth[identity] = 0;
            }
            return depth[identity] = 1 + Depth(dep.RequiredBy, seen);
        }

        return _dependencies
            .OrderByDescending(d => Depth(d.Identity, []))
            .ToList();
    }

    public JsonObject AsJson() => new()
    {
        ["dependencies"] = new JsonArray(_dependencies
            .Select(d => (JsonNode)new JsonObject
            {
                ["identity"] = d.Identity,
                ["owner"] = d.Owner,
                ["required_by"] = d.RequiredBy,
                ["criticality"] = d.Criticality,
                ["readiness_contract"] = d.ReadinessContract,
                ["deadline"] = d.Deadline,
                ["retry_budget"] = d.RetryBudget,
                ["shutdown_order"] = d.ShutdownOrder,
                ["activation"] = d.Activation,
            }).ToArray()),
        ["is_acyclic"] = IsAcyclic,
        ["core_critical"] = new JsonArray(CoreCritical
            .Select(d => (JsonNode)JsonValue.Create(d.Identity)!).ToArray()),
        ["capability_critical"] = new JsonArray(CapabilityCritical
            .Select(d => (JsonNode)JsonValue.Create(d.Identity)!).ToArray()),
        ["optional"] = new JsonArray(Optional
            .Select(d => (JsonNode)JsonValue.Create(d.Identity)!).ToArray()),
    };
}

public static class GovernedStartup
{
    /// <summary>A192 FORBID:circular-startup-gate — strict phase order.</summary>
    public static JsonObject VerifyPhaseOrder(
        IReadOnlyList<string> required, IReadOnlyList<string> completed)
    {
        var violations = new List<string>();
        var lastIndex = -1;
        foreach (var phase in required)
        {
            var index = completed.ToList().IndexOf(phase);
            if (index < 0)
            {
                violations.Add($"missing-phase:{phase}");
                continue;
            }
            if (index <= lastIndex)
                violations.Add($"out-of-order:{phase}");
            lastIndex = index;
        }
        return new JsonObject
        {
            ["ok"] = violations.Count == 0,
            ["basis"] = "A192/E167",
            ["completed_phases"] = new JsonArray(completed
                .Select(p => (JsonNode)JsonValue.Create(p)!).ToArray()),
            ["required_phases"] = new JsonArray(required
                .Select(p => (JsonNode)JsonValue.Create(p)!).ToArray()),
            ["violations"] = new JsonArray(violations
                .Select(v => (JsonNode)JsonValue.Create(v)!).ToArray()),
            ["circular_gate"] = false,
            ["no_skip"] = violations.Count == 0,
        };
    }

    /// <summary>A192 CORE-READY — every declared condition must hold.</summary>
    public static JsonObject VerifyCoreReady(
        IReadOnlyList<string> conditions,
        IReadOnlyDictionary<string, bool> observed)
    {
        var missing = conditions
            .Where(c => !observed.TryGetValue(c, out var ok) || !ok)
            .ToList();
        return new JsonObject
        {
            ["ok"] = missing.Count == 0,
            ["basis"] = "A192/E167",
            ["conditions"] = new JsonObject(conditions
                .Select(c => new KeyValuePair<string, JsonNode?>(
                    c, observed.TryGetValue(c, out var ok) && ok))
                .ToArray()),
            ["missing"] = new JsonArray(missing
                .Select(m => (JsonNode)JsonValue.Create(m)!).ToArray()),
            ["core_ready"] = missing.Count == 0,
        };
    }

    /// <summary>
    /// A191 classification check: criticality must come from the certified
    /// manifest contract, never from the service name.
    /// </summary>
    public static JsonObject VerifyDependencyClassification(
        DependencyDag dag, StartupManifest manifest)
    {
        var violations = new List<string>();
        var noFixed = manifest.NoFixedCriticalityServices
            .Select(s => s.ToLowerInvariant()).ToHashSet();
        foreach (var dep in dag.Dependencies)
        {
            if (!manifest.CriticalityClasses.Contains(dep.Criticality))
                violations.Add(
                    $"invalid-criticality:{dep.Identity}:{dep.Criticality}");
            if (noFixed.Contains(dep.Identity.ToLowerInvariant())
                && dep.IsCoreCritical && dep.RequiredBy.Length == 0)
            {
                violations.Add(
                    $"name-based-criticality:{dep.Identity}:" +
                    "core-critical-without-required-by");
            }
        }
        return new JsonObject
        {
            ["ok"] = violations.Count == 0,
            ["basis"] = "A191/E166",
            ["is_acyclic"] = dag.IsAcyclic,
            ["core_critical"] = new JsonArray(dag.CoreCritical
                .Select(d => (JsonNode)JsonValue.Create(d.Identity)!).ToArray()),
            ["capability_critical"] = new JsonArray(dag.CapabilityCritical
                .Select(d => (JsonNode)JsonValue.Create(d.Identity)!).ToArray()),
            ["optional"] = new JsonArray(dag.Optional
                .Select(d => (JsonNode)JsonValue.Create(d.Identity)!).ToArray()),
            ["violations"] = new JsonArray(violations
                .Select(v => (JsonNode)JsonValue.Create(v)!).ToArray()),
            ["legacy_fixed_sequence"] = false,
        };
    }

    /// <summary>A192 FAILURE signal (signal-only authority).</summary>
    public static JsonObject StartupFailureSignal(
        string failureScope,
        string failedDependency = "",
        string generationId = "")
    {
        var isCore = failureScope == "core-critical";
        return new JsonObject
        {
            ["signal_type"] = "startup-failure",
            ["authority"] = "signal-only",
            ["basis"] = "A192/E167",
            ["failure_scope"] = failureScope,
            ["failed_dependency"] = failedDependency,
            ["generation_id"] = generationId,
            ["action"] = isCore
                ? "failed-generation+owned-reverse-DAG-cleanup+" +
                  "typed-user-visible-cause"
                : "affected-capability-degraded-only+retry-budget",
            ["retry"] = "fresh-generation-or-owned-node-retry",
            ["backoff"] = "exponential+jitter+circuit-breaker",
            ["manual_approval_required"] = false,
        };
    }

    /// <summary>A192 STATUS snapshot.</summary>
    public static JsonObject StartupStatus(
        StartupGeneration generation,
        DependencyDag? dag = null,
        IReadOnlyDictionary<string, bool>? coreReadyConditions = null)
    {
        return new JsonObject
        {
            ["generation_id"] = generation.GenerationId,
            ["release_id"] = generation.ReleaseId,
            ["started_at"] = generation.StartedAt,
            ["current_phase"] = generation.CurrentPhase,
            ["core_ready"] = generation.CoreReady,
            ["deferred_active"] = generation.DeferredActive,
            ["dag"] = dag?.AsJson(),
            ["core_ready_conditions"] = new JsonObject(
                (coreReadyConditions ??
                    new Dictionary<string, bool>())
                .Select(kv => new KeyValuePair<string, JsonNode?>(
                    kv.Key, kv.Value)).ToArray()),
            ["basis"] = "A192/E167",
            ["single_flight"] = true,
            ["user_action_required"] = false,
        };
    }
}
