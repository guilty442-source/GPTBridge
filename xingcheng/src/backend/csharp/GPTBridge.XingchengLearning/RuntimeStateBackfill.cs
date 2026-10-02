using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class RuntimeStateBackfill
{
    public static Dictionary<string, object?> Run(string root)
    {
        var metadata = new NativeMetadataClient(root, "xingcheng-runtime-backfill");
        var paths = new[] { "self-learning.json", "model-maturation-300m.json", "capability-maturity.json", "model-maturity.json", "production-certification.json" }
            .Select(name => Path.Combine(root, "xingcheng/runtime/state", name)).ToList();
        paths.AddRange(Directory.EnumerateFiles(root, "lifecycle.json", SearchOption.AllDirectories));
        var generationDirectory = Path.Combine(root, "xingcheng/runtime/state/generation");
        if (Directory.Exists(generationDirectory)) paths.AddRange(Directory.EnumerateFiles(generationDirectory, "*.json"));
        foreach (var name in new[] { "capability-registry.promoted.json", "capability-baseline-300m.json", "hardware-baseline-300m.json" })
        {
            var path = Path.Combine(root, "xingcheng/runtime/state", name); if (File.Exists(path)) paths.Add(path);
        }
        int sourceCount = 0, written = 0, deduplicated = 0;
        var failures = new List<string>(); string? lastIdentity = null;
        foreach (var path in paths.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            lastIdentity = Path.GetRelativePath(root, path).Replace('\\', '/');
            if (!File.Exists(path)) { failures.Add("SOURCE_STATE_MISSING:" + lastIdentity); continue; }
            sourceCount++;
            try
            {
                var bytes = File.ReadAllText(path);
                using var document = JsonDocument.Parse(bytes);
                var canonical = CanonicalJson.Canonical(document.RootElement);
                if (NativeStateProjection.Exists(path))
                {
                    using var current = JsonDocument.Parse(NativeStateProjection.ReadAllText(path));
                    if (canonical != CanonicalJson.Canonical(current.RootElement)) throw new InvalidOperationException("CANONICAL_STATE_CONFLICT");
                    deduplicated++;
                }
                else if (NativeStateProjection.TrySave(path, bytes)) written++;
                else throw new InvalidOperationException("STATE_PATH_NOT_REGISTERED");
            }
            catch (Exception error) { failures.Add(lastIdentity + ":" + error.Message); }
        }
        var evidencePath = Path.Combine(root, CapabilityEvidence.Rel);
        if (File.Exists(evidencePath))
            foreach (var line in File.ReadLines(evidencePath).Where(line => !string.IsNullOrWhiteSpace(line)))
            {
                sourceCount++;
                try
                {
                    using var json = JsonDocument.Parse(line);
                    var original = (Dictionary<string, object?>)ModelLifecycle.Decode(json.RootElement)!;
                    var normalized = CapabilityEvidence.Normalize(original);
                    var identity = (string)normalized["evidence_hash"]!;
                    lastIdentity = "capability-evidence:" + identity;
                    if (!Equals(original.GetValueOrDefault("evidence_hash"), identity)) throw new InvalidOperationException("EVIDENCE_HASH_MISMATCH");
                    var existing = metadata.Get(NativeMetadataClient.Types.CapabilityEvidence, identity);
                    if (existing is not null) deduplicated++;
                    else
                    {
                        metadata.PutRecord(NativeMetadataClient.Types.CapabilityEvidence,
                            new Dictionary<string, object?> { ["record_id"] = identity, ["document"] = original }, operationId: lastIdentity);
                        written++;
                    }
                }
                catch (Exception error) { failures.Add(lastIdentity + ":" + error.Message); }
            }
        var verification = metadata.Verify();
        return new() { ["ok"] = failures.Count == 0, ["source_count"] = sourceCount,
            ["written_count"] = written, ["deduplicated_count"] = deduplicated, ["failed_count"] = failures.Count,
            ["last_source_identity"] = lastIdentity, ["metadata_root"] = verification.GetProperty("head_hash").GetString(),
            ["failures"] = failures, ["scope"] = "runtime-state-and-capability-evidence; PostgreSQL history remains LEGACY_MIGRATION_ONLY" };
    }
}
