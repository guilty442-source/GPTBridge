// CapabilityDelta.cs — ``star-capability-delta/v1`` promotion report
// (capability unification directive §90-§92).
//
// Candidate promotion is never decided by a model checksum alone
// (§90): the delta report compares a baseline registry snapshot with
// a candidate registry snapshot capability-by-capability and reports
// improved / unchanged / regressed / unsupported / newly_certified
// (§91). Any protected capability falling below its floor blocks
// promotion outright (§92); a regression on any certified-or-better
// capability does too — capabilities never compensate for each other
// (§67).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityDelta
{
    public const string Format = "star-capability-delta/v1";

    private static readonly Dictionary<string, int> Rank =
        new(StringComparer.Ordinal)
        {
            ["UNAVAILABLE"] = 0, ["IMPLEMENTED"] = 1,
            ["TRAINING"] = 2, ["EVALUATED"] = 3,
            ["CERTIFIED"] = 4, ["MATURE"] = 5,
        };

    private static int RankOf(string status) =>
        status == "REGRESSED" ? -1
            : Rank.TryGetValue(status, out int r) ? r : 0;

    private static Dictionary<string, JsonElement> RowsOf(
        JsonElement root)
    {
        var map = new Dictionary<string, JsonElement>(
            StringComparer.Ordinal);
        if (root.TryGetProperty("capabilities", out var caps) &&
            caps.ValueKind == JsonValueKind.Array)
            foreach (var c in caps.EnumerateArray())
                if (c.TryGetProperty("capability_id", out var id) &&
                    id.ValueKind == JsonValueKind.String)
                    map[id.GetString()!] = c;
        return map;
    }

    /// <summary>Compare a baseline registry document with a candidate
    /// registry document (both ``star-capability-registry/v1``).
    /// Returns the delta report; ``ok=false`` + ``promotion_blocked``
    /// when §92 triggers.</summary>
    public static Dictionary<string, object?> Compare(
        string baselineFile, string candidateFile)
    {
        if (!File.Exists(baselineFile) || !File.Exists(candidateFile))
            throw new ExecutorError("CAPABILITY_DELTA_INVALID",
                "baseline or candidate registry file missing");
        JsonElement baseline, candidate;
        try
        {
            baseline = JsonDocument.Parse(
                File.ReadAllText(baselineFile)).RootElement.Clone();
            candidate = JsonDocument.Parse(
                File.ReadAllText(candidateFile)).RootElement.Clone();
        }
        catch (JsonException)
        {
            throw new ExecutorError("CAPABILITY_DELTA_INVALID",
                "registry file not valid json");
        }
        var baseRows = RowsOf(baseline);
        var candRows = RowsOf(candidate);
        var improved = new List<object?>();
        var unchanged = new List<object?>();
        var regressed = new List<object?>();
        var unsupported = new List<object?>();
        var newlyCertified = new List<object?>();
        var blocked = new List<object?>();

        foreach (var (id, crow) in candRows)
        {
            string cst = crow.TryGetProperty("status", out var cs) &&
                         cs.ValueKind == JsonValueKind.String
                ? cs.GetString() ?? "UNAVAILABLE" : "UNAVAILABLE";
            bool cProtected =
                crow.TryGetProperty("protected", out var cp) &&
                cp.ValueKind == JsonValueKind.True;
            if (!baseRows.TryGetValue(id, out var brow))
            {
                // Unknown to the baseline — a new row is only
                // "newly_certified" when it already carries a
                // certification-grade status.
                if (RankOf(cst) >= Rank["CERTIFIED"])
                    newlyCertified.Add(id);
                else
                    unsupported.Add(id);
                continue;
            }
            string bst = brow.TryGetProperty("status", out var bs) &&
                         bs.ValueKind == JsonValueKind.String
                ? bs.GetString() ?? "UNAVAILABLE" : "UNAVAILABLE";
            bool bProtected =
                brow.TryGetProperty("protected", out var bp) &&
                bp.ValueKind == JsonValueKind.True;
            int dr = RankOf(cst) - RankOf(bst);
            if (dr > 0)
            {
                if (RankOf(bst) < Rank["CERTIFIED"] &&
                    RankOf(cst) >= Rank["CERTIFIED"])
                    newlyCertified.Add(id);
                else
                    improved.Add(id);
            }
            else if (dr < 0 || cst == "REGRESSED")
            {
                regressed.Add(id);
                // §92/§68: a protected capability below floor blocks
                // the promotion; REGRESSED always does (§69).
                if (bProtected || cProtected ||
                    RankOf(bst) >= Rank["CERTIFIED"])
                    blocked.Add(id);
            }
            else
            {
                unchanged.Add(id);
            }
        }
        // Baseline rows the candidate dropped entirely.
        foreach (var (id, _) in baseRows)
            if (!candRows.ContainsKey(id))
                unsupported.Add(id);

        return new Dictionary<string, object?>
        {
            ["ok"] = blocked.Count == 0,
            ["format"] = Format,
            ["generated_at"] = XcPaths.IsoNow(),
            ["baseline_file"] = baselineFile,
            ["candidate_file"] = candidateFile,
            ["improved"] = improved.OrderBy(x => x).ToList(),
            ["unchanged"] = unchanged.OrderBy(x => x).ToList(),
            ["regressed"] = regressed.OrderBy(x => x).ToList(),
            ["unsupported"] = unsupported.OrderBy(x => x).ToList(),
            ["newly_certified"] =
                newlyCertified.OrderBy(x => x).ToList(),
            ["promotion_blocked"] = blocked.Count > 0,
            ["blocking_capabilities"] =
                blocked.OrderBy(x => x).ToList(),
        };
    }
}
