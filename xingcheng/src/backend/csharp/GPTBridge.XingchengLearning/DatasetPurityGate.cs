// DatasetPurityGate.cs — ``star-dataset-purity-gate/v1``
// (capability maturation closure directive §105).
//
// §105 every formal maturity evaluation must confirm dataset purity on
// four axes before the result can stand as evidence:
//   train_eval_overlap   — candidate training content vs the formal
//                          eval suite items (§7 suite disjointness,
//                          also enforced at build time by
//                          RECOVERY_SUITE_OVERLAP)
//   regression_leakage   — candidate training content vs protected
//                          capabilities' regression suites (§35:
//                          protected regression data must never be
//                          visible to candidate training)
//   golden_leakage       — golden items must never enter training
//                          (§32; MaturityStandard.GoldenGate denies
//                          trained_on tasks — this gate denies the
//                          content itself)
//   holdout_leakage      — holdout items must never enter training and
//                          may not be synthetically regenerated into
//                          recovery data (§33/§34)
//
// The gate is fail-closed: any leaked content hash returns
// verdict=DATASET_PURITY_VIOLATION with the violated axes and offending
// hashes (consumers must treat ok:false as a hard block). Eval/
// regression prompt sets are
// derived in-process from the same suite builders the recovery lane
// emits — no second suite definition exists.
//
// Input contract (``--dataset-purity --file <json>``):
//   {
//     "capability":            "<id or alias>",        // required
//     "train_prompts":         ["..."],                // raw prompts
//     "train_hashes":          ["sha256", ...],        // or pre-hashed
//     "golden_hashes":         ["sha256", ...],        // optional
//     "holdout_hashes":        ["sha256", ...],        // optional
//     "extra_protected_hashes":["sha256", ...]         // optional
//   }
// train_prompts are normalized (trim) then sha256'd; train_hashes are
// compared verbatim (lowercased hex expected).

using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class DatasetPurityGate
{
    public const string Format = "star-dataset-purity-gate/v1";

    private static string Hash(string s) =>
        Convert.ToHexString(System.Security.Cryptography.SHA256
            .HashData(Encoding.UTF8.GetBytes(s.Trim())))
            .ToLowerInvariant();

    private static HashSet<string> HashSet(JsonElement el, string key)
    {
        var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty(key, out var v) &&
            v.ValueKind == JsonValueKind.Array)
            foreach (var item in v.EnumerateArray())
                if (item.ValueKind == JsonValueKind.String &&
                    item.GetString() is { Length: > 0 } s)
                    set.Add(key == "train_prompts"
                        ? Hash(s) : s.ToLowerInvariant());
        return set;
    }

    /// <summary>canonical id → the sequence member spelling the suite
    /// builders key on (``reading`` → ``reading_grounding``); null when
    /// the capability has no recovery eval suite.</summary>
    private static string? SequenceIdFor(string canonical)
    {
        foreach (var spec in Maturation300M.Sequence)
            if (CapabilityRegistry.Resolve(spec.Id) == canonical)
                return spec.Id;
        return null;
    }

    /// <summary>Run the four-axis purity check for one candidate's
    /// training set. Returns the report — ok:false /
    /// verdict=DATASET_PURITY_VIOLATION on any leak (fail-closed for
    /// consumers); throws only when the input itself cannot be
    /// evaluated (unknown capability, empty train set).</summary>
    public static Dictionary<string, object?> Check(
        JsonElement el, string toolRoot)
    {
        string capRaw = el.TryGetProperty("capability", out var c) &&
            c.ValueKind == JsonValueKind.String
                ? c.GetString() ?? "" : "";
        string? canonical = CapabilityRegistry.Resolve(capRaw);
        if (canonical == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                capRaw.Length > 0 ? capRaw : "(missing)");

        var train = HashSet(el, "train_hashes");
        foreach (var h in HashSet(el, "train_prompts"))
            train.Add(h);
        if (train.Count == 0)
            throw new ExecutorError("DATASET_PURITY_VIOLATION",
                "no train_hashes/train_prompts supplied — a formal " +
                "purity check cannot pass on an empty input set");

        // §105 axis 1 — the capability's own formal eval suite.
        var evalHashes = new HashSet<string>(
            StringComparer.OrdinalIgnoreCase);
        if (SequenceIdFor(canonical) is string ownSeq)
            evalHashes.UnionWith(
                InstructionRecovery.SuitePromptHashes(ownSeq));

        // §105 axis 2 — regression suite: graph-derived deps plus every
        // protected capability (§35: protected regression data is
        // invisible to candidate training).
        var regressionHashes = new HashSet<string>(
            StringComparer.OrdinalIgnoreCase);
        var regressionIds = new SortedSet<string>(
            StringComparer.Ordinal);
        foreach (var dep in CapabilityGraph.RegressionSuiteFor(
                     canonical, toolRoot))
            regressionIds.Add(dep);
        foreach (var p in CapabilityMaturityService.Protected(toolRoot))
            regressionIds.Add(p);
        foreach (var dep in regressionIds)
            if (dep != canonical &&
                SequenceIdFor(dep) is string seq)
            {
                regressionHashes.UnionWith(
                    InstructionRecovery.SuitePromptHashes(seq));
            }
        regressionHashes.ExceptWith(evalHashes);

        // §105 axes 3/4 — caller-supplied golden / holdout hash sets
        // (their corpora live outside this process; the gate checks
        // whatever is declared protected).
        var golden = HashSet(el, "golden_hashes");
        var holdout = HashSet(el, "holdout_hashes");
        var extra = HashSet(el, "extra_protected_hashes");
        var protectedAll = new HashSet<string>(
            StringComparer.OrdinalIgnoreCase);
        protectedAll.UnionWith(evalHashes);
        protectedAll.UnionWith(regressionHashes);
        protectedAll.UnionWith(golden);
        protectedAll.UnionWith(holdout);
        protectedAll.UnionWith(extra);

        var leaked = train
            .Where(protectedAll.Contains).OrderBy(x => x).ToList();
        string Axis(HashSet<string> set) =>
            train.Any(set.Contains) ? "LEAK" : "PASS";
        var axes = new Dictionary<string, object?>
        {
            ["train_eval_overlap"] = Axis(evalHashes),
            ["regression_leakage"] = Axis(regressionHashes),
            ["golden_leakage"] = Axis(golden),
            ["holdout_leakage"] = Axis(holdout),
        };

        var report = new Dictionary<string, object?>
        {
            ["ok"] = leaked.Count == 0,
            ["format"] = Format,
            ["capability_id"] = canonical,
            ["train_hashes"] = train.Count,
            ["eval_suite_hashes"] = evalHashes.Count,
            ["regression_suite_hashes"] = regressionHashes.Count,
            ["regression_capabilities"] =
                regressionIds.Cast<object?>().ToList(),
            ["golden_hashes"] = golden.Count,
            ["holdout_hashes"] = holdout.Count,
            ["axes"] = axes,
            ["leaked_hashes"] =
                leaked.Take(16).Cast<object?>().ToList(),
            ["leak_count"] = leaked.Count,
            ["checked_at"] = XcPaths.IsoNow(),
            ["verdict"] = leaked.Count == 0
                ? "DATASET_PURE" : "DATASET_PURITY_VIOLATION",
        };
        if (leaked.Count > 0)
            report["violated_axes"] = axes
                .Where(kv => (string)kv.Value! == "LEAK")
                .Select(kv => kv.Key).Cast<object?>().ToList();
        return report;
    }
}
