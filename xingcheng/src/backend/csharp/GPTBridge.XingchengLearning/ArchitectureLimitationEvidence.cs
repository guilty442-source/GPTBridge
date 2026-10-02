// ArchitectureLimitationEvidence.cs — ``star-architecture-limitation-evidence/v1``
// (capability unification directive §52-§54, §72).
//
// When a capability plateaus, the system must NOT silently grow the
// model. It records an architecture-limitation record here; the
// ArchitectureGate then requires such a record before any canonical
// architecture mutation (§54: no limitation evidence → no
// architecture change, including the ≤1B ACTIVE_PARAMS ceiling
// pressure in §72).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ArchitectureLimitationEvidence
{
    public const string Format =
        "star-architecture-limitation-evidence/v1";
    private const int MaxRecords = 128;    // bounded like evidence.jsonl

    private static string Dir(string toolRoot) =>
        Path.Combine(toolRoot, "xingcheng", "runtime", "state");
    private static string Store(string toolRoot) =>
        Path.Combine(Dir(toolRoot), "architecture-limitations.jsonl");

    // §53 required fields — all must be present for a record to stand.
    private static readonly string[] Required =
    {
        "capability", "training_attempts", "data_quality",
        "scale_tested", "regression", "plateau_evidence",
        "kernel_resource_exclusions", "reason",
    };

    /// <summary>Validate and append a limitation record; returns the
    /// stored record. Fails closed on unknown capability ids.</summary>
    public static Dictionary<string, object?> Record(
        JsonElement el, string toolRoot)
    {
        string capRaw = CapabilityEvidence.Str(el, "capability");
        string? cap = CapabilityRegistry.Resolve(capRaw);
        if (cap == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN", capRaw);

        var missing = Required
            .Where(k => !el.TryGetProperty(k, out var v) ||
                v.ValueKind is JsonValueKind.Null)
            .ToList();
        if (missing.Count > 0)
            throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                $"missing fields: {string.Join(',', missing)}");

        long attempts = el.TryGetProperty("training_attempts",
            out var ta) && ta.ValueKind == JsonValueKind.Number &&
            ta.TryGetInt64(out long n) ? n : 0;
        var spec = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["capability_id"] = cap,
            ["training_attempts"] = attempts,
            ["data_quality"] =
                CapabilityEvidence.Str(el, "data_quality"),
            ["scale_tested"] =
                CapabilityEvidence.Str(el, "scale_tested"),
            ["regression"] =
                CapabilityEvidence.Str(el, "regression"),
            ["plateau_evidence"] =
                CapabilityEvidence.Str(el, "plateau_evidence"),
            ["kernel_resource_exclusions"] =
                CapabilityEvidence.Str(el,
                    "kernel_resource_exclusions"),
            ["reason"] = CapabilityEvidence.Str(el, "reason"),
            ["recorded_at"] =
                DateTimeOffset.UtcNow.ToString("o"),
        };
        spec["limitation_hash"] = CapabilityEvidence.Hash(spec);

        var store = Store(toolRoot);
        var lines = File.Exists(store)
            ? File.ReadAllLines(store).Where(l => l.Length > 0)
                .ToList() : new List<string>();
        lines.Add(JsonSerializer.Serialize(spec));
        File.WriteAllLines(store,
            lines.Count > MaxRecords
                ? lines.Skip(lines.Count - MaxRecords) : lines);
        return spec;
    }

    /// <summary>Latest limitation records (optionally filtered to a
    /// capability); newest first. Used by §54 gate checks.</summary>
    public static Dictionary<string, object?> Status(
        string toolRoot, string? capabilityInput = null)
    {
        string? cap = capabilityInput is { Length: > 0 }
            ? CapabilityRegistry.Resolve(capabilityInput) : null;
        if (capabilityInput is { Length: > 0 } && cap == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                capabilityInput);
        var rows = ReadAll(toolRoot)
            .Where(r => cap == null ||
                r.TryGetValue("capability_id", out var c) &&
                c?.ToString() == cap)
            .ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["capability_id"] = cap,
            ["count"] = rows.Count,
            ["latest"] = rows.Take(8).ToList(),
        };
    }

    /// <summary>True when at least one limitation record exists for
    /// the capability — the §54 precondition checked by the
    /// architecture gate.</summary>
    public static bool HasFor(string toolRoot, string capabilityId) =>
        ReadAll(toolRoot).Any(r =>
            r.TryGetValue("capability_id", out var c) &&
            c?.ToString() == capabilityId);

    private static List<Dictionary<string, object?>> ReadAll(
        string toolRoot)
    {
        var rows = new List<Dictionary<string, object?>>();
        var store = Store(toolRoot);
        if (!File.Exists(store)) return rows;
        foreach (var line in File.ReadAllLines(store)
                     .Where(l => l.Length > 0))
            try
            {
                var d = JsonSerializer.Deserialize<
                    Dictionary<string, object?>>(line);
                if (d != null) rows.Add(d);
            }
            catch { /* skip corrupt tail */ }
        rows.Reverse();
        return rows;
    }
}
