// MultiLaneMaturation.cs — ``star-multilane-plan/v1`` +
// ``star-multilane-verdict/v1`` (capability maturation closure
// directive §47-§53, §108).
//
// §47-§48 a single capability may run up to 3 candidate lanes, but all
// lanes share the same source checkpoint, canonical capability id,
// architecture generation and base dataset snapshot — lanes are an
// in-candidate competition, never parallel candidates (§93).
//
// §49 lanes may differ ONLY in: learning_rate, curriculum,
// sample_weighting, trainable_expert_set, microbatch,
// gradient_accumulation. Any other divergent key denies the plan.
//
// §50 lane ladder: 10-step warmup → 25-step discard check → 50-step
// pilot → FAST eval. §51 a clearly lagging lane is killed early — the
// plane never burns full budget for fairness. §52 the winner extends
// 200 → regression → 400 → 600 on demand.
//
// §53 step count is never the success metric: the winner is decided by
// capability_gain, regression pass, floor status, TTQM and compute
// efficiency — capability evidence, not throughput.
//
// §108 a lane denied by resources/performance is PERFORMANCE_BLOCKED —
// excluded from comparison but never recorded as a capability failure;
// a true failure requires the capability itself to miss its floor.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class MultiLaneMaturation
{
    public const string PlanFormat = "star-multilane-plan/v1";
    public const string VerdictFormat = "star-multilane-verdict/v1";

    public const int MaxLanes = 3;
    public static readonly int[] LaneLadder = { 10, 25, 50 };
    public static readonly int[] WinnerLadder = { 200, 400, 600 };

    // §49 the only keys lanes may differ on.
    private static readonly HashSet<string> DivergentKeys =
        new(StringComparer.Ordinal)
        {
            "learning_rate", "curriculum", "sample_weighting",
            "trainable_expert_set", "microbatch",
            "gradient_accumulation",
        };

    // §48 identical across lanes — a divergent invariant denies.
    private static readonly string[] InvariantKeys =
    {
        "source_checkpoint", "capability", "architecture",
        "base_dataset_snapshot",
    };

    private static string Str(JsonElement r, string k, string d = "") =>
        r.ValueKind == JsonValueKind.Object &&
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;

    private static double DNum(JsonElement r, string k, double d = 0) =>
        r.ValueKind == JsonValueKind.Object &&
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number &&
        v.TryGetDouble(out double n) ? n : d;

    private static bool Truthy(JsonElement r, string k) =>
        r.ValueKind == JsonValueKind.Object &&
        r.TryGetProperty(k, out var v) &&
        (v.ValueKind == JsonValueKind.True ||
         v.ValueKind == JsonValueKind.Number &&
         v.TryGetDouble(out double n) && n != 0);

    // ----------------------------------------------------- plan -----

    /// <summary>Validate a multi-lane plan: 1-3 lanes, all invariants
    /// identical, only §49 keys may differ between lanes, canonical
    /// capability id, xc-fused-1 architecture. Returns
    /// ok:false + violations (fail-closed for consumers).</summary>
    public static Dictionary<string, object?> ValidatePlan(
        JsonElement el)
    {
        var violations = new List<string>();
        var lanes = new List<JsonElement>();
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty("lanes", out var l) &&
            l.ValueKind == JsonValueKind.Array)
            lanes = l.EnumerateArray()
                .Where(x => x.ValueKind == JsonValueKind.Object)
                .ToList();
        if (lanes.Count == 0)
            violations.Add("plan carries no lanes");
        if (lanes.Count > MaxLanes)
            violations.Add(
                $"{lanes.Count} lanes exceeds the {MaxLanes}-lane " +
                "ceiling (§48)");

        string canonical = "";
        var invariants = new Dictionary<string, string>();
        foreach (var (lane, i) in lanes.Select((x, i) => (x, i)))
        {
            // §48 invariants must be present and identical.
            foreach (var key in InvariantKeys)
            {
                string v = Str(lane, key);
                if (v.Length == 0)
                {
                    violations.Add(
                        $"lane[{i}] missing invariant '{key}'");
                    continue;
                }
                if (invariants.TryGetValue(key, out string? first))
                {
                    if (first != v)
                        violations.Add(
                            $"lane[{i}] '{key}' diverges ('{v}' != " +
                            $"'{first}') — lanes share one source " +
                            "checkpoint / capability / architecture / " +
                            "base dataset (§48)");
                }
                else invariants[key] = v;
            }
            if (canonical.Length == 0 &&
                invariants.TryGetValue("capability", out string? cap))
            {
                string? resolved = CapabilityRegistry.Resolve(cap);
                if (resolved == null)
                    violations.Add(
                        $"capability '{cap}' is not in the " +
                        "CapabilityRegistry (§1: lanes exist only for " +
                        "canonical capabilities)");
                else canonical = resolved;
            }
        }

        // §49 diff audit — union of per-lane property keys minus the
        // invariants and identity/metadata keys must all be in the
        // allowed divergent set.
        var allowed = new HashSet<string>(DivergentKeys,
            StringComparer.Ordinal);
        foreach (var k in InvariantKeys) allowed.Add(k);
        foreach (var k in new[] { "lane_id", "name", "seed",
                                  "resource_hint", "notes" })
            allowed.Add(k);
        for (int i = 0; i < lanes.Count; ++i)
            foreach (var prop in lanes[i].EnumerateObject())
                if (!allowed.Contains(prop.Name))
                    violations.Add(
                        $"lane[{i}] varies '{prop.Name}' — only " +
                        $"{string.Join('/', DivergentKeys)} may " +
                        "differ between lanes (§49)");

        if (invariants.TryGetValue("architecture", out string? arch) &&
            arch != ArchitectureTaxonomy.CanonicalArchitecture)
            violations.Add(
                $"architecture '{arch}' != canonical " +
                $"'{ArchitectureTaxonomy.CanonicalArchitecture}' " +
                "(§48: lanes never fork the architecture)");

        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = PlanFormat,
            ["capability_id"] = canonical,
            ["lane_count"] = lanes.Count,
            ["invariants"] = invariants.ToDictionary(
                kv => kv.Key, kv => (object?)kv.Value),
            ["lane_ladder"] = LaneLadder.Cast<object?>().ToList(),
            ["winner_ladder"] = WinnerLadder.Cast<object?>().ToList(),
            ["allowed_divergent"] =
                DivergentKeys.OrderBy(x => x).Cast<object?>().ToList(),
            ["violations"] = violations.Cast<object?>().ToList(),
            ["verdict"] = violations.Count == 0
                ? "MULTILANE_PLAN_VALID" : "MULTILANE_PLAN_DENIED",
        };
    }

    // -------------------------------------------------- verdict -----

    /// <summary>§53 winner selection on capability evidence. Each lane
    /// result carries: lane_id, status ("complete"|"performance_
    /// blocked"|"killed"), capability_gain (float), regression_pass,
    /// floor_pass, ttqm (s, lower better), compute_seconds. Order:
    /// floor PASS → regression PASS → capability_gain → compute
    /// efficiency. PERFORMANCE_BLOCKED lanes are excluded but
    /// reported — never counted as capability failures (§108).</summary>
    public static Dictionary<string, object?> SelectWinner(
        JsonElement el)
    {
        var lanes = new List<Dictionary<string, object?>>();
        var blocked = new List<object?>();
        var failed = new List<object?>();
        var killed = new List<object?>();
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty("results", out var r) &&
            r.ValueKind == JsonValueKind.Array)
            foreach (var lane in r.EnumerateArray())
            {
                if (lane.ValueKind != JsonValueKind.Object) continue;
                string id = Str(lane, "lane_id", "?");
                string status = Str(lane, "status", "complete");
                var row = new Dictionary<string, object?>
                {
                    ["lane_id"] = id,
                    ["status"] = status,
                    ["capability_gain"] =
                        DNum(lane, "capability_gain"),
                    ["regression_pass"] =
                        Truthy(lane, "regression_pass"),
                    ["floor_pass"] = Truthy(lane, "floor_pass"),
                    ["ttqm_seconds"] = DNum(lane, "ttqm_seconds"),
                    ["compute_seconds"] =
                        DNum(lane, "compute_seconds"),
                };
                switch (status)
                {
                    case "performance_blocked":
                        row["verdict"] = "PERFORMANCE_BLOCKED";
                        blocked.Add(row);
                        break;
                    case "killed":
                        row["verdict"] = "EARLY_KILLED";
                        killed.Add(row);
                        break;
                    default:
                        // §108: a completed lane that missed its floor
                        // is a capability failure; blocked lanes never
                        // reach this bucket.
                        if (!(bool)row["floor_pass"]!)
                        {
                            row["verdict"] = "CAPABILITY_FLOOR_MISS";
                            failed.Add(row);
                        }
                        else lanes.Add(row);
                        break;
                }
            }

        // §53 rank: floor PASS → regression PASS → gain → efficiency.
        var ranked = lanes
            .OrderByDescending(x => (bool)x["regression_pass"]!)
            .ThenByDescending(x => (double)x["capability_gain"]!)
            .ThenBy(x => (double)x["compute_seconds"]!)
            .ThenBy(x => (double)x["ttqm_seconds"]!)
            .ToList();
        var winner = ranked.FirstOrDefault();
        var report = new Dictionary<string, object?>
        {
            ["ok"] = winner != null,
            ["format"] = VerdictFormat,
            ["winner"] = winner,
            ["ranked"] = ranked.Cast<object?>().ToList(),
            ["performance_blocked"] = blocked,
            ["capability_failures"] = failed,
            ["early_killed"] = killed,
            ["rule"] = "winner = floor PASS + regression PASS + " +
                       "capability gain + compute efficiency; " +
                       "PERFORMANCE_BLOCKED lanes are excluded, never " +
                       "failures (§53/§108)",
            ["verdict"] = winner != null
                ? "LANE_WINNER_SELECTED"
                : (blocked.Count > 0 && lanes.Count == 0 &&
                   failed.Count == 0
                    ? "ALL_LANES_PERFORMANCE_BLOCKED"
                    : "NO_WINNER"),
        };
        return report;
    }
}
