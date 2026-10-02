// CapabilityConsistency.cs — capability-architecture consistency
// checks (capability unification directive §86-§88, §102).
//
// The release-gate "Capability Architecture Consistency Gate" (§101)
// runs on these checks: every finding is a named violation class so
// the gate can fail closed on any of them. Read-only — checks never
// mutate state.

namespace GPTBridge.XingchengLearning;

internal static class CapabilityConsistency
{
    /// <summary>Run every §102 consistency class over the registry,
    /// graph, eval map and progression state. Returns ok plus a
    /// findings list grouped by violation class.</summary>
    public static Dictionary<string, object?> Run(string toolRoot)
    {
        var findings = new List<object?>();
        void Flag(string cls, string id, string detail) =>
            findings.Add(new Dictionary<string, object?>
            {
                ["class"] = cls,
                ["capability_id"] = id,
                ["detail"] = detail,
            });

        CheckDuplicatesAndAliases(Flag);
        CheckKnownConsumers(Flag);
        CheckBindings(Flag);
        CheckRegressionCoverage(Flag);
        CheckEvidence(toolRoot, Flag);
        CheckClassPlaneConfusion(Flag);
        CheckEvalMap(Flag);

        var byClass = findings
            .GroupBy(f => (string)((Dictionary<string, object?>)f!)
                           ["class"]!)
            .ToDictionary(g => g.Key,
                          g => (object?)g.Count());
        return new Dictionary<string, object?>
        {
            ["ok"] = findings.Count == 0,
            ["format"] = "star-capability-consistency/v1",
            ["checked_at"] = XcPaths.IsoNow(),
            ["finding_count"] = findings.Count,
            ["by_class"] = byClass,
            ["findings"] = findings,
        };
    }

    // duplicate_capability + alias collisions (§102).
    private static void CheckDuplicatesAndAliases(
        Action<string, string, string> flag)
    {
        var ids = new HashSet<string>(StringComparer.Ordinal);
        var aliasOwner = new Dictionary<string, string>(
            StringComparer.Ordinal);
        foreach (var d in CapabilityRegistry.Canonical)
        {
            if (!ids.Add(d.CapabilityId))
                flag("duplicate_capability", d.CapabilityId,
                     "canonical id defined twice");
            foreach (var a in d.Aliases)
            {
                string key = a.Trim().ToLowerInvariant()
                    .Replace('-', '_').Replace(' ', '_');
                if (aliasOwner.TryGetValue(key, out string? owner) &&
                    owner != d.CapabilityId)
                    flag("duplicate_capability", d.CapabilityId,
                         $"alias '{a}' already owned by {owner}");
                else
                    aliasOwner[key] = d.CapabilityId;
            }
        }
    }

    // unknown_capability (§102): every capability referenced by the
    // maturation sequence, the failure pool and the single-capability
    // recovery lane must resolve to a canonical id.
    private static void CheckKnownConsumers(
        Action<string, string, string> flag)
    {
        foreach (var spec in Maturation300M.Sequence)
            if (CapabilityRegistry.Resolve(spec.Id) == null)
                flag("unknown_capability", spec.Id,
                     "maturation sequence id not in registry");
        foreach (var cls in FailurePool.Classes)
            if (CapabilityRegistry.Resolve(cls) == null &&
                cls is not ("reasoning" or "system1"))
                flag("unknown_capability", cls,
                     "failure-pool class has no canonical capability");
    }

    // missing_architecture_binding (§87): a capability lacking model
    // or eval components is CAPABILITY_ARCHITECTURE_INCOMPLETE.
    private static void CheckBindings(
        Action<string, string, string> flag)
    {
        foreach (var d in CapabilityRegistry.Canonical)
        {
            // SERVICE_AUGMENTED rows bind to the service surface only
            // (§8) — they never carry model components.
            bool ok = d.CapabilityClass == "SERVICE_AUGMENTED"
                ? d.Binding.RuntimeComponents.Length > 0
                : d.Binding.Complete;
            if (!ok)
                flag("missing_architecture_binding", d.CapabilityId,
                     "binding lacks required components");
        }
    }

    // missing_regression_dependency (§102): graph REQUIRES edges must
    // be visible in the descriptor's regression_dependencies (or be
    // reachable transitively).
    private static void CheckRegressionCoverage(
        Action<string, string, string> flag)
    {
        foreach (var e in CapabilityGraph.Seed)
        {
            if (e.Type != "REQUIRES") continue;
            var d = CapabilityRegistry.Get(e.From);
            if (d == null) continue;
            var closure = CapabilityGraph.RequiresClosure(e.From);
            if (!closure.Contains(e.To) &&
                !d.RegressionDependencies.Contains(e.To))
                flag("missing_regression_dependency", e.From,
                     $"requires {e.To} not in regression_dependencies");
        }
    }

    // missing_evidence (§102): CERTIFIED/MATURE status requires frozen
    // progression stage with an evidence reference (§21: IMPLEMENTED
    // never equals CERTIFIED).
    private static void CheckEvidence(string toolRoot,
        Action<string, string, string> flag)
    {
        var state = Maturation300M.LoadState(toolRoot);
        if (!state.TryGetValue("capabilities", out object? raw) ||
            raw is not Dictionary<string, object?> caps)
            return;
        foreach (var p in caps)
        {
            if (p.Value is not Dictionary<string, object?> cap)
                continue;
            string st = cap.TryGetValue("status", out object? s)
                ? s?.ToString() ?? "" : "";
            if (st != "frozen") continue;
            bool hasEvidence =
                cap.TryGetValue("evidence", out object? ev) &&
                ev is string es && es.Length > 0;
            if (!hasEvidence)
                flag("missing_evidence", p.Key,
                     "frozen capability lacks gate evidence");
        }
    }

    // capability_runtime_confusion (§102): class/plane mismatches —
    // a MODEL_NATIVE row must carry model weights; a
    // SERVICE_AUGMENTED row must not be owned by COMPUTE; a
    // RUNTIME_AUGMENTED row must name runtime components.
    private static void CheckClassPlaneConfusion(
        Action<string, string, string> flag)
    {
        foreach (var d in CapabilityRegistry.Canonical)
        {
            if (d.CapabilityClass == "MODEL_NATIVE" &&
                !d.ModelDependency.Contains("weights"))
                flag("capability_runtime_confusion", d.CapabilityId,
                     "MODEL_NATIVE without model_weights dependency");
            if (d.CapabilityClass == "SERVICE_AUGMENTED" &&
                d.OwnerPlane == "COMPUTE")
                flag("capability_runtime_confusion", d.CapabilityId,
                     "SERVICE_AUGMENTED owned by COMPUTE");
            if (d.CapabilityClass == "RUNTIME_AUGMENTED" &&
                d.Binding.RuntimeComponents.Length == 0)
                flag("capability_runtime_confusion", d.CapabilityId,
                     "RUNTIME_AUGMENTED without runtime components");
        }
    }

    // orphan_eval (§102): eval-map surfaces whose capability ids all
    // fail to resolve; unmapped registry capabilities surface as
    // orphan_training_path when they sit in the maturation sequence.
    private static void CheckEvalMap(
        Action<string, string, string> flag)
    {
        foreach (var (surface, caps) in CapabilityEvaluationMap.Map)
        {
            if (caps.Length == 0 ||
                caps.All(c => CapabilityRegistry.Resolve(c) == null))
                flag("orphan_eval", surface,
                     "eval surface maps to no canonical capability");
        }
        var sequenced = Maturation300M.Sequence
            .Select(s => s.Id).ToHashSet(StringComparer.Ordinal);
        foreach (var d in CapabilityRegistry.Canonical)
            if (sequenced.Contains(d.CapabilityId) &&
                CapabilityEvaluationMap
                    .SurfacesFor(d.CapabilityId).Length == 0)
                flag("orphan_training_path", d.CapabilityId,
                     "sequenced capability has no eval surface");
    }
}
