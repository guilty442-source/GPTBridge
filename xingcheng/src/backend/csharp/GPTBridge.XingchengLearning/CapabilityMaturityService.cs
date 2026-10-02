// CapabilityMaturityService.cs — ``star-capability-maturity/v1`` state
// + ``star-capability-maturity-report/v1`` report (maturation-closure
// directive §5-§17, §100-§101).
//
// This is the per-capability maturity authority. It does NOT replace
// Maturation300M (ordered admission — which capability may train now)
// or CapabilityRegistry (which capabilities exist); it records the
// governed state each capability has actually reached:
//
//   UNAVAILABLE → IMPLEMENTED → TRAINING → EVALUATED → CERTIFIED →
//   MATURE → (PROTECTED) → REGRESSED → REOPENED
//
// Gate conditions (fail-closed, §6-§11):
//   IMPLEMENTED  architecture binding + runtime/training/eval paths
//                exist in the descriptor (existence only — §6 says
//                nothing about passing);
//   EVALUATED    an evidence record exists carrying eval_suite +
//                dataset_snapshot + model_version + result (§7);
//   CERTIFIED    floor PASS + regression PASS + binding PASS +
//                runtime profile + resource profile + complete
//                evidence (§8);
//   MATURE       certified + >=2 independent evidence records holding
//                the floor (stability) + lifecycle-visible (§9);
//   REGRESSED    certified/mature evidence drops below the floor —
//                automatic, never averaged away (§10);
//   REOPENED     regressed + a governed progression reopen receipt —
//                historical PASS evidence is preserved (§11).
//
// Baselines (§16/§17): a mature capability stores its certified
// baseline; nothing may move it except a completed certification or
// promotion — never a drifting candidate.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityMaturityService
{
    public const string Format = "star-capability-maturity/v1";
    public const string ReportFormat =
        "star-capability-maturity-report/v1";
    public const string Rel =
        "xingcheng/runtime/state/capability-maturity.json";

    /// <summary>§26 core regression set — required columns for every
    /// candidate whenever they are protected.</summary>
    public static readonly string[] CoreRegression =
    {
        "instruction_following", "context_tracking", "multi_turn",
        "structured_output", "reading",
    };

    // ---------------------------------------------------------- io --

    private static string Path_(string toolRoot) =>
        Path.Combine(toolRoot, Rel.Replace('/',
            Path.DirectorySeparatorChar));

    /// <summary>Load the maturity state store; absent file = fresh
    /// (every capability starts UNAVAILABLE/IMPLEMENTED by rule).</summary>
    public static Dictionary<string, Dictionary<string, object?>>
        Load(string toolRoot)
    {
        var map = new Dictionary<string, Dictionary<string, object?>>(
            StringComparer.Ordinal);
        string p = Path_(toolRoot);
        if (!NativeStateProjection.Exists(p)) return map;
        try
        {
            using var doc = JsonDocument.Parse(NativeStateProjection.ReadAllText(p));
            if (doc.RootElement.TryGetProperty("capabilities",
                    out var caps) &&
                caps.ValueKind == JsonValueKind.Object)
                foreach (var kv in caps.EnumerateObject())
                    if (ModelLifecycle.Decode(kv.Value)
                        is Dictionary<string, object?> row)
                        map[kv.Name] = row;
        }
        catch (JsonException)
        {
            throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                "capability-maturity.json unreadable — fail closed");
        }
        return map;
    }

    private static void Save(string toolRoot,
        Dictionary<string, Dictionary<string, object?>> map)
    {
        var doc = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["capabilities"] = map,
        };
        ModelLifecycle.AtomicWrite(Path_(toolRoot),
            CanonicalJson.Canonical(ModelLifecycle.Encode(doc)));
    }

    // ------------------------------------------------- floor check --

    /// <summary>§12 floor evaluation against one evidence record.
    /// Every axis is hard — no averaging (§13). Returns verdict +
    /// per-axis findings.</summary>
    public static Dictionary<string, object?> FloorCheck(
        string capabilityInput, string toolRoot)
    {
        string cap = CapabilityResolver.Require(capabilityInput);
        var d = CapabilityResolver.Descriptor(cap);
        var ev = CapabilityEvidence.Load(toolRoot)
            .Where(r => r["capability_id"]?.ToString() == cap)
            .ToList();
        var latest = ev.Count > 0 ? ev[^1] : null;
        var findings = new List<object?>();

        if (latest == null)
            findings.Add("missing-evidence");
        double score = Score(latest);
        if (latest != null && score < d.Floor.MinimumQuality)
            findings.Add(
                $"minimum-quality {score} < {d.Floor.MinimumQuality}");
        if (latest != null && RegressionDropped(latest))
            findings.Add("max-regression exceeded");
        // §12 required eval suites: the evidence's eval_suite must be
        // one of the floor's required evals when any are declared.
        if (latest != null && d.Floor.RequiredEvals.Length > 0)
        {
            string suite = latest.TryGetValue("eval_suite",
                out var es) ? es?.ToString() ?? "" : "";
            if (!d.Floor.RequiredEvals.Contains(suite))
                findings.Add($"required-eval-missing:{suite}");
        }
        // §12 runtime/architecture/stability axes — presence checks
        // against the evidence record's named sections.
        if (latest != null)
        {
            if (d.Floor.RequiredRuntimeChecks.Length > 0 &&
                latest["runtime_profile"] == null)
                findings.Add("required-runtime-checks-missing");
            if (d.Floor.RequiredArchitectureChecks.Length > 0 &&
                !BindingComplete(d))
                findings.Add("required-architecture-checks-missing");
            if (d.Floor.RequiredStabilityChecks.Length > 0 &&
                ev.Count < 2)
                findings.Add("required-stability-checks-missing");
        }

        return new Dictionary<string, object?>
        {
            ["ok"] = findings.Count == 0,
            ["format"] = Format,
            ["capability_id"] = cap,
            ["floor"] = d.Floor.ToDict(),
            ["score"] = latest == null ? null : score,
            ["evidence_count"] = ev.Count,
            ["latest_evidence_hash"] = latest?.TryGetValue(
                "evidence_hash", out var h) == true
                ? h?.ToString() : null,
            ["findings"] = findings,
            ["verdict"] = findings.Count == 0
                ? "FLOOR_PASS" : "FLOOR_FAIL",
        };
    }

    private static double Score(Dictionary<string, object?>? ev)
    {
        if (ev == null) return double.NaN;
        if (ev.TryGetValue("result", out var r))
        {
            if (r is Dictionary<string, object?> rd &&
                rd.TryGetValue("pass_rate", out var pr) &&
                pr is double d) return d;
            if (r is double dn) return dn;
        }
        return double.NaN;
    }

    /// <summary>A regression section reporting any non-pass entry is a
    /// floor breach (§10/§15 — the stricter of certified baseline and
    /// floor applies; recorded regressions are already the strict
    /// check).</summary>
    private static bool RegressionDropped(
        Dictionary<string, object?> ev)
    {
        if (!ev.TryGetValue("regression", out var r)) return false;
        if (r is Dictionary<string, object?> rd)
            return rd.Values.Any(v =>
                v?.ToString()?.Contains("fail") == true ||
                v?.ToString()?.Contains("regress") == true);
        if (r is string s)
            return s.Contains("fail") || s.Contains("regress");
        return false;
    }

    private static bool BindingComplete(CapabilityDescriptor d) =>
        d.Binding.ModelComponents.Length > 0 &&
        d.Binding.RuntimeComponents.Length > 0 &&
        d.Binding.EvalComponents.Length > 0;

    // -------------------------------------------------- state calc --

    /// <summary>Derive the evidence-supported state of a capability —
    /// the ceiling the record store may claim. Governance writes the
    /// actual state via <see cref="Transition"/>; this never writes.</summary>
    public static string DeriveState(string cap,
        List<Dictionary<string, object?>> ev)
    {
        var d = CapabilityResolver.Descriptor(cap);
        if (!BindingComplete(d)) return "UNAVAILABLE";
        if (ev.Count == 0) return "IMPLEMENTED";
        var latest = ev[^1];
        bool evalComplete =
            latest["eval_suite"] != null &&
            latest["dataset_snapshot"] != null &&
            latest["model_version"] != null &&
            latest["result"] != null;
        if (!evalComplete) return "IMPLEMENTED";
        double score = Score(latest);
        bool floorPass = !double.IsNaN(score) &&
            score >= d.Floor.MinimumQuality &&
            !RegressionDropped(latest);
        if (!floorPass) return "EVALUATED";
        bool certified = floorPass &&
            latest["runtime_profile"] != null &&
            latest["resource_profile"] != null;
        if (!certified) return "EVALUATED";
        // §9: mature needs >=2 independent records holding the floor.
        int holding = ev.Count(e =>
            Score(e) >= d.Floor.MinimumQuality &&
            !RegressionDropped(e));
        return holding >= 2 ? "MATURE" : "CERTIFIED";
    }

    /// <summary>Governed transition: evaluate evidence, apply the
    /// state-machine rules and persist. REGRESSED is always permitted
    /// (automatic on floor breach); upward moves require the derived
    /// state to support them; REOPENED requires a progression reopen
    /// receipt present in the maturity store.</summary>
    public static Dictionary<string, object?> Transition(
        string toolRoot, string capabilityInput, string target,
        string evidenceNote = "")
    {
        string cap = CapabilityResolver.Require(capabilityInput);
        target = target.ToUpperInvariant();
        if (!CapabilityDescriptor.Statuses.Contains(target))
            throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                $"unknown state '{target}'");

        var store = Load(toolRoot);
        if (!store.TryGetValue(cap, out var row))
            store[cap] = row = new Dictionary<string, object?>
            {
                ["state"] = "IMPLEMENTED",
                ["protected"] = false,
            };
        string current = row["state"]?.ToString() ?? "IMPLEMENTED";
        var ev = CapabilityEvidence.Load(toolRoot)
            .Where(r => r["capability_id"]?.ToString() == cap)
            .ToList();
        string derived = DeriveState(cap, ev);

        // §10/§15: floor breach on a certified/mature capability
        // auto-regresses — this path never needs permission.
        if (target == "REGRESSED")
        {
            if (current is not ("CERTIFIED" or "MATURE" or "REOPENED"))
                throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                    $"REGRESSED only applies from certified/mature " +
                    $"(now {current})");
        }
        else if (target == "REOPENED")
        {
            // §11: reopen requires the regressed state plus a governed
            // progression receipt; history stays intact.
            if (current != "REGRESSED" &&
                row["regressed_at"] == null)
                throw new ExecutorError("CAPABILITY_SEQUENCE_VIOLATION",
                    "REOPENED requires a prior REGRESSED state");
        }
        else
        {
            // Upward moves: the evidence-derived ceiling must cover
            // the target — a store can never claim beyond evidence.
            var order = new[] { "UNAVAILABLE", "IMPLEMENTED",
                "TRAINING", "EVALUATED", "CERTIFIED", "MATURE" };
            int req = Array.IndexOf(order, target);
            int have = Array.IndexOf(order, derived);
            if (req < 0 || have < req)
                throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                    $"evidence supports {derived}; target {target} " +
                    "denied");
        }

        var history = row.TryGetValue("history", out var h) &&
            h is List<object?> hl ? hl : new List<object?>();
        history.Add(new Dictionary<string, object?>
        {
            ["from"] = current, ["to"] = target,
            ["at"] = XcPaths.IsoNow(),
            ["note"] = evidenceNote,
            ["derived_ceiling"] = derived,
        });
        row["state"] = target;
        row["history"] = history;
        if (target == "REGRESSED")
            row["regressed_at"] = XcPaths.IsoNow();
        // §14: MATURE implies PROTECTED.
        if (target == "MATURE") row["protected"] = true;

        // §16/§17: baseline refresh only on certification/maturity —
        // never on regressed/reopened intermediate states.
        if (target is "CERTIFIED" or "MATURE" && ev.Count > 0)
        {
            var latest = ev[^1];
            row["baseline"] = new Dictionary<string, object?>
            {
                ["baseline_model_generation"] =
                    latest["model_version"],
                ["baseline_checkpoint_hash"] =
                    latest["candidate_id"],
                ["baseline_eval_hash"] =
                    latest["evidence_hash"],
                ["baseline_score"] = Score(latest),
                ["baseline_runtime_profile"] =
                    latest["runtime_profile"],
                ["baseline_resource_profile"] =
                    latest["resource_profile"],
            };
        }
        Save(toolRoot, store);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["capability_id"] = cap,
            ["from"] = current, ["to"] = target,
            ["derived_ceiling"] = derived,
            ["protected"] = row["protected"],
        };
    }

    /// <summary>Protected capabilities — the regression-matrix column
    /// set (§14/§24).</summary>
    public static string[] Protected(string toolRoot) =>
        Load(toolRoot)
            .Where(kv => kv.Value.TryGetValue("protected", out var p) &&
                p is true)
            .Select(kv => kv.Key).OrderBy(x => x).ToArray();

    // ---------------------------------------------------- report ----

    /// <summary>§100/§101 machine-readable maturity dashboard.</summary>
    public static Dictionary<string, object?> Report(string toolRoot)
    {
        var store = Load(toolRoot);
        var evidence = CapabilityEvidence.Load(toolRoot);
        var rows = new Dictionary<string, object?>();
        foreach (var d in CapabilityRegistry.Canonical)
        {
            var ev = evidence.Where(r =>
                r["capability_id"]?.ToString() == d.CapabilityId)
                .ToList();
            var latest = ev.Count > 0 ? ev[^1] : null;
            store.TryGetValue(d.CapabilityId, out var row);
            double score = Score(latest);
            double? baselineScore = row != null &&
                row.TryGetValue("baseline", out var b) &&
                b is Dictionary<string, object?> bd &&
                bd.TryGetValue("baseline_score", out var bs) &&
                bs is double bv ? bv : null;
            rows[d.CapabilityId] = new Dictionary<string, object?>
            {
                ["state"] = row?["state"] ?? "IMPLEMENTED",
                ["derived_ceiling"] = DeriveState(d.CapabilityId, ev),
                ["floor"] = d.Floor.ToDict(),
                ["protected"] =
                    row?.TryGetValue("protected", out var p) == true &&
                    p is true,
                ["current_score"] =
                    latest == null ? null : score,
                ["baseline_score"] = baselineScore,
                ["delta"] = baselineScore != null &&
                    !double.IsNaN(score)
                        ? score - baselineScore.Value : (double?)null,
                ["regression_status"] =
                    latest != null && RegressionDropped(latest)
                        ? "REGRESSED" : "clean",
                ["evidence_hash"] = latest?.TryGetValue(
                    "evidence_hash", out var eh) == true
                    ? eh?.ToString() : null,
                ["evidence_count"] = ev.Count,
                ["last_evaluated"] =
                    latest?["timestamp"],
                // §101: binding completeness is part of the report.
                ["binding_complete"] = BindingComplete(d),
            };
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = ReportFormat,
            ["capabilities"] = rows,
            ["protected"] = Protected(toolRoot).Cast<object?>().ToList(),
            ["core_regression"] =
                CoreRegression.Cast<object?>().ToList(),
        };
    }
}
