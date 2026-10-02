using System.Text.Json;

namespace GPTBridge.XingchengLearning;

// Release evidence only. A transition marker cannot replace executed
// snapshot verification or prove removal of the production PG dependency.
internal static class NativeMetadataProductionGate
{
    /// <summary>Any non-framework assembly on a binary's reference list
    /// means an external runtime dependency is linked (§50-§53). The
    /// probe is generic by design — naming a removed vendor package in
    /// source would itself trip the native-only source scan.</summary>
    internal static bool HasExternalReference(
        System.Reflection.Assembly assembly)
        => assembly.GetReferencedAssemblies().Any(r =>
            r.Name is { Length: > 0 } n &&
            !n.StartsWith("System", StringComparison.Ordinal) &&
            !n.StartsWith("Microsoft", StringComparison.Ordinal) &&
            !n.StartsWith("netstandard", StringComparison.Ordinal) &&
            !n.StartsWith("mscorlib", StringComparison.Ordinal));

    internal static Dictionary<string, object?> Evaluate(
        IReadOnlyDictionary<string, object?>? marker, JsonElement verification,
        bool postgresRuntimeDependency)
    {
        var failures = new List<string>();
        bool True(JsonElement value, string field) => value.ValueKind == JsonValueKind.Object
            && value.TryGetProperty(field, out var item) && item.ValueKind == JsonValueKind.True;
        bool Hash(object? value) => value is string hash && hash.Length == 64
            && hash.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');
        if (marker is null || !Equals(marker.GetValueOrDefault("new_authority"), "xstore"))
            failures.Add("AUTHORITY_NOT_XSTORE");
        if (marker is null || !Equals(marker.GetValueOrDefault("format"), MetadataAuthority.TransitionFormat))
            failures.Add("AUTHORITY_TRANSITION_MISSING");
        foreach (var root in new[] { "metadata_root", "audit_root", "snapshot_root", "parity_report_hash" })
            if (marker is null || !Hash(marker.GetValueOrDefault(root))) failures.Add("TRANSITION_ROOT_MISSING:" + root);
        var gateProof = marker is null ? default : JsonSerializer.SerializeToElement(marker.GetValueOrDefault("gates"));
        foreach (var gate in new[] { "backfill", "parity", "audit_verify", "crash_recovery", "concurrent_writer", "metadata_snapshot", "index_rebuild" })
            if (!True(gateProof, gate)) failures.Add("TRANSITION_GATE_EVIDENCE_MISSING:" + gate);
        if (!True(verification, "ok") || !True(verification, "schema_identity_ok")
            || !True(verification, "invariants_ok")) failures.Add("METADATA_VERIFY_FAILED");
        if (verification.ValueKind != JsonValueKind.Object
            || !verification.TryGetProperty("receipts", out var receipts) || !True(receipts, "ok"))
            failures.Add("AUDIT_VERIFY_FAILED");
        if (!True(verification, "index_fresh")) failures.Add("INDEX_REBUILD_EVIDENCE_MISSING");
        // A count/list of manifests proves existence only, not integrity.
        if (verification.ValueKind != JsonValueKind.Object
            || !verification.TryGetProperty("snapshots", out var snapshots) || !True(snapshots, "verified"))
            failures.Add("SNAPSHOT_VERIFY_EVIDENCE_MISSING");
        if (postgresRuntimeDependency) failures.Add("POSTGRES_RUNTIME_DEPENDENCY_PRESENT");
        return new()
        {
            ["gate"] = MetadataAuthority.GateId,
            ["ok"] = failures.Count == 0,
            ["status"] = failures.Count == 0 ? "PASS" : "INCOMPLETE_EVIDENCE",
            ["failures"] = failures,
        };
    }
}
