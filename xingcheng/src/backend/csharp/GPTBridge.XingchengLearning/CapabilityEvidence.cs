// CapabilityEvidence.cs — ``star-capability-evidence/v1``
// (capability unification directive §20-§24, §88).
//
// A capability is only real when evidence exists (§21: IMPLEMENTED
// never equals CERTIFIED). Every evidence record carries the full
// §20 field set — capability_id, model_version, candidate_id,
// dataset_snapshot, eval_suite, baseline, result, regression,
// runtime_profile, resource_profile, timestamp and a content-bound
// evidence_hash — and is appended to a bounded JSONL chain under
// runtime/state. RUNTIME_AUGMENTED rows must separate model and
// runtime contributions (§7); a record that cannot say which side it
// measures is rejected.

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityEvidence
{
    public const string Format = "star-capability-evidence/v1";
    public const string Rel =
        "xingcheng/runtime/state/capability-evidence.jsonl";
    public const int MaxRecords = 4096;

    /// <summary>§7 evidence attribution: which side produced the
    /// measured result — required so runtime gains are never claimed
    /// as weight gains.</summary>
    public static readonly string[] ContributionKinds =
        { "model", "runtime", "service", "mixed" };

    private static string Path_(string toolRoot) =>
        Path.Combine(toolRoot, Rel.Replace('/', Path.DirectorySeparatorChar));

    /// <summary>Content-bound evidence hash: sha256 over the canonical
    /// JSON of the §20 fields — any tampering invalidates the
    /// record.</summary>
    public static string HashRecord(Dictionary<string, object?> rec)
    {
        var body = new SortedDictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var k in new[]
        {
            "capability_id", "model_version", "candidate_id",
            "dataset_snapshot", "eval_suite", "baseline", "result",
            "regression", "runtime_profile", "resource_profile",
            "contribution", "timestamp",
        })
            body[k] = rec.TryGetValue(k, out object? v) ? v : null;
        string canonical = CanonicalJson.Canonical(
            ModelLifecycle.Encode(body));
        return Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
    }

    /// <summary>Validate a candidate evidence document against the
    /// §20 contract. Returns the normalized record (hash stamped) or
    /// throws ExecutorError — evidence never partially records.</summary>
    public static Dictionary<string, object?> Normalize(
        Dictionary<string, object?> doc)
    {
        if (!doc.TryGetValue("format", out object? f) ||
            f?.ToString() != Format)
            throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                $"format must be {Format}");
        string? capRaw = doc.TryGetValue("capability_id", out object? c)
            ? c?.ToString() : null;
        string? cap = capRaw == null
            ? null : CapabilityRegistry.Resolve(capRaw);
        if (cap == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"evidence capability_id '{capRaw}' is not in the " +
                "CapabilityRegistry");
        var rec = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["capability_id"] = cap,
        };
        foreach (var k in new[] { "model_version", "candidate_id",
                                  "dataset_snapshot", "eval_suite",
                                  "baseline", "result", "regression",
                                  "runtime_profile", "resource_profile" })
            rec[k] = doc.TryGetValue(k, out object? v) ? v : null;
        string contribution =
            doc.TryGetValue("contribution", out object? cb)
                ? cb?.ToString() ?? "" : "";
        if (!ContributionKinds.Contains(contribution))
            throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                $"contribution must be one of " +
                string.Join("|", ContributionKinds));
        rec["contribution"] = contribution;
        // §7: RUNTIME_AUGMENTED evidence must attribute its measured
        // side — "mixed" without a runtime_profile is not evidence of
        // either side.
        var d = CapabilityRegistry.Get(cap)!;
        if (d.CapabilityClass == "RUNTIME_AUGMENTED" &&
            contribution == "mixed" && rec["runtime_profile"] == null)
            throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                "RUNTIME_AUGMENTED mixed evidence requires a " +
                "runtime_profile");
        rec["timestamp"] =
            doc.TryGetValue("timestamp", out object? ts) &&
            ts?.ToString() is { Length: > 0 } t
                ? t : XcPaths.IsoNow();
        rec["evidence_hash"] = HashRecord(rec);
        return rec;
    }

    /// <summary>Append a validated record to the bounded evidence
    /// chain (oldest entries evicted past MaxRecords — the chain is an
    /// operational store, history lives in git/audit).</summary>
    public static Dictionary<string, object?> Record(
        string toolRoot, Dictionary<string, object?> doc)
    {
        var rec = Normalize(doc);
        var metadata = new NativeMetadataClient(toolRoot, "xingcheng-capability-evidence");
        var identity = (string)rec["evidence_hash"]!;
        metadata.PutRecord(NativeMetadataClient.Types.CapabilityEvidence,
            new Dictionary<string, object?> { ["record_id"] = identity, ["document"] = rec },
            operationId: "capability-evidence:" + identity);
        var existing = Load(toolRoot);
        if (existing.Count > MaxRecords)
            existing = existing.Skip(existing.Count - MaxRecords)
                               .ToList();
        var sb = new StringBuilder();
        foreach (var r in existing)
            sb.Append(CanonicalJson.Canonical(
                ModelLifecycle.Encode(r))).Append('\n');
        ModelLifecycle.AtomicWrite(Path_(toolRoot), sb.ToString());
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["recorded"] = rec,
            ["evidence_count"] = existing.Count,
        };
    }

    /// <summary>Load every stored record (newest last). Corrupt lines
    /// fail closed — evidence integrity is never silently
    /// repaired.</summary>
    public static List<Dictionary<string, object?>> Load(string toolRoot)
    {
        var list = new List<Dictionary<string, object?>>();
        var metadata = new NativeMetadataClient(toolRoot, "xingcheng-capability-evidence");
        foreach (var row in metadata.Query(NativeMetadataClient.Types.CapabilityEvidence, null, 1000000))
        {
            if (row.GetValueOrDefault("document") is not Dictionary<string, object?>)
                throw new ExecutorError("CAPABILITY_EVIDENCE_CORRUPT", "Canonical evidence document missing.");
            if (row.GetValueOrDefault("document") is Dictionary<string, object?> rec)
            {
                if (rec.TryGetValue("evidence_hash", out object? h) &&
                    h?.ToString() != HashRecord(rec))
                    throw new ExecutorError("CAPABILITY_EVIDENCE_CORRUPT",
                        "evidence_hash mismatch — chain tampered");
                list.Add(rec);
            }
        }
        return list.OrderBy(record => record.GetValueOrDefault("timestamp")?.ToString(), StringComparer.Ordinal)
            .ThenBy(record => record.GetValueOrDefault("evidence_hash")?.ToString(), StringComparer.Ordinal).ToList();
    }

    /// <summary>Latest evidence summary, optionally filtered to one
    /// capability.</summary>
    public static Dictionary<string, object?> Status(
        string toolRoot, string capability)
    {
        var all = Load(toolRoot);
        if (capability.Length > 0)
        {
            string? canon = CapabilityRegistry.Resolve(capability);
            if (canon == null)
                throw new ExecutorError("CAPABILITY_UNKNOWN",
                    $"capability '{capability}' is not in the " +
                    "CapabilityRegistry");
            all = all.Where(r =>
                r["capability_id"]?.ToString() == canon).ToList();
        }
        var latest = new Dictionary<string, object?>();
        foreach (var r in all)
            latest[r["capability_id"]!.ToString()!] = r;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["evidence_count"] = all.Count,
            ["latest_by_capability"] = latest,
        };
    }
}
