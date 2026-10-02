using System.Text.Json;

namespace GPTBridge.XingchengLearning;

// Runtime state is committed through Rust. JSON paths are export locations.
internal static class NativeStateProjection
{
    private static readonly ThreadLocal<Dictionary<string, long>> Revisions = new(() => new(StringComparer.OrdinalIgnoreCase));
    private static (NativeMetadataClient Client, string Type, string Id)? Binding(string path)
    {
        var full = Path.GetFullPath(path);
        string? type = Path.GetFileName(full) switch
        {
            "lifecycle.json" => NativeMetadataClient.Types.Lifecycle,
            "self-learning.json" when full.Replace('\\', '/').Contains("/runtime/state/") => NativeMetadataClient.Types.SelfLearning,
            "model-maturation-300m.json" => NativeMetadataClient.Types.Maturation,
            "capability-maturity.json" => NativeMetadataClient.Types.CapabilityMaturity,
            "model-maturity.json" => NativeMetadataClient.Types.CapabilityMaturity,
            "production-certification.json" => NativeMetadataClient.Types.SchemaMetadata,
            "capability-registry.promoted.json" => NativeMetadataClient.Types.CapabilityDelta,
            "capability-baseline-300m.json" => NativeMetadataClient.Types.CapabilityFloor,
            "hardware-baseline-300m.json" => NativeMetadataClient.Types.CapabilityFloor,
            _ when full.Replace('\\', '/').Contains("/runtime/state/generation/") => NativeMetadataClient.Types.Generation,
            _ => null,
        };
        if (type is null) return null;
        for (var directory = new DirectoryInfo(Path.GetDirectoryName(full)!); directory is not null; directory = directory.Parent)
        {
            if (!Directory.Exists(Path.Combine(directory.FullName, "src/backend/rust/xstore"))) continue;
            var id = Path.GetRelativePath(directory.FullName, full).Replace('\\', '/');
            return (new NativeMetadataClient(directory.FullName), type, id);
        }
        return null; // Unregistered scratch artifact; not production state.
    }
    public static bool Exists(string path)
    {
        var binding = Binding(path);
        if (binding is null) return File.Exists(path);
        var b = binding.Value; var record = b.Client.Get(b.Type, b.Id);
        Revisions.Value![Path.GetFullPath(path)] = record is null ? 0 : Convert.ToInt64(record["revision"]);
        return record is not null;
    }
    public static bool DirectoryExists(string directory)
    {
        var probe = Path.Combine(directory, "state.json");
        return Binding(probe) is null ? Directory.Exists(directory) : EnumerateFiles(directory, "*.json").Any();
    }
    public static IEnumerable<string> EnumerateFiles(string directory, string pattern)
    {
        var binding = Binding(Path.Combine(directory, "state.json"));
        if (binding is null) return Directory.EnumerateFiles(directory, pattern);
        var b = binding.Value;
        var prefix = b.Id[..b.Id.LastIndexOf('/')] + "/";
        return b.Client.Query(b.Type, null, 1000000)
            .Select(record => record.GetValueOrDefault("record_id")?.ToString() ?? "")
            .Where(id => id.StartsWith(prefix, StringComparison.Ordinal) && !id[prefix.Length..].Contains('/')
                && System.IO.Enumeration.FileSystemName.MatchesSimpleExpression(pattern, Path.GetFileName(id)))
            .Select(id => Path.GetFullPath(Path.Combine(directory, Path.GetFileName(id)))).ToArray();
    }
    public static string ReadAllText(string path)
    {
        var binding = Binding(path);
        if (binding is null) return File.ReadAllText(path);
        var b = binding.Value; var record = b.Client.Get(b.Type, b.Id) ?? throw new IOException("NATIVE_STATE_MISSING:" + b.Id);
        Revisions.Value![Path.GetFullPath(path)] = Convert.ToInt64(record["revision"]);
        if (record.GetValueOrDefault("document") is not Dictionary<string, object?> document)
            throw new InvalidOperationException("NATIVE_STATE_INVALID:" + b.Id);
        return CanonicalJson.CanonicalDict(document);
    }
    internal static bool TrySave(string path, string payload)
    {
        var binding = Binding(path);
        if (binding is null) return false;
        var b = binding.Value; var full = Path.GetFullPath(path);
        using var json = JsonDocument.Parse(payload);
        if (ModelLifecycle.Decode(json.RootElement) is not Dictionary<string, object?> document)
            throw new InvalidOperationException("NATIVE_STATE_INVALID:" + b.Id);
        if (!Revisions.Value!.TryGetValue(full, out var expected))
        {
            var current = b.Client.Get(b.Type, b.Id);
            expected = current is null ? 0 : Convert.ToInt64(current["revision"]);
        }
        b.Client.PutRecord(b.Type, new Dictionary<string, object?> { ["record_id"] = b.Id, ["document"] = document }, expectedRevision: expected);
        // Refresh from the committed record; never infer authority from a file.
        var stored = b.Client.Get(b.Type, b.Id) ?? throw new InvalidOperationException("NATIVE_STATE_COMMIT_MISSING");
        Revisions.Value![full] = Convert.ToInt64(stored["revision"]);
        return true;
    }
}
