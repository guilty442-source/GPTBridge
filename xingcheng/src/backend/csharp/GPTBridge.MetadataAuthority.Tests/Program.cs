using System.Text.Json;
using GPTBridge.XingchengLearning;

var marker = new Dictionary<string, object?> { ["format"] = MetadataAuthority.TransitionFormat, ["new_authority"] = "xstore" };
foreach (var root in new[] { "metadata_root", "audit_root", "snapshot_root", "parity_report_hash" }) marker[root] = new string('a', 64);
marker["gates"] = new Dictionary<string, bool> { ["backfill"] = true, ["parity"] = true, ["audit_verify"] = true, ["crash_recovery"] = true, ["concurrent_writer"] = true, ["metadata_snapshot"] = true, ["index_rebuild"] = true };
var valid = JsonSerializer.SerializeToElement(new { ok = true, schema_identity_ok = true, invariants_ok = true, index_fresh = true, receipts = new { ok = true }, snapshots = new { verified = true } });
var passed = 0;
void Check(bool expected, IReadOnlyDictionary<string, object?>? transition, JsonElement verify, bool postgres = false)
{
    if (!Equals(expected, NativeMetadataProductionGate.Evaluate(transition, verify, postgres)["ok"])) throw new Exception("METADATA_AUTHORITY_GATE_REGRESSION");
    passed++;
}
Check(true, marker, valid);
Check(false, marker, valid, true);
Check(false, null, valid);
foreach (var gate in ((Dictionary<string, bool>)marker["gates"]!).Keys)
{
    var proof = new Dictionary<string, bool>((Dictionary<string, bool>)marker["gates"]!) { [gate] = false };
    var invalid = new Dictionary<string, object?>(marker) { ["gates"] = proof };
    Check(false, invalid, valid);
}
foreach (var field in marker.Keys)
{
    var incomplete = new Dictionary<string, object?>(marker); incomplete.Remove(field);
    Check(false, incomplete, valid);
}
foreach (var field in new[] { "ok", "schema_identity_ok", "invariants_ok", "index_fresh", "receipts", "snapshots" })
{
    var incomplete = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(valid)!; incomplete.Remove(field);
    Check(false, marker, JsonSerializer.SerializeToElement(incomplete));
}
Check(false, marker, JsonSerializer.SerializeToElement(new { ok = true, schema_identity_ok = true, invariants_ok = true, index_fresh = true, receipts = new { ok = true }, snapshots = new { snapshot_count = 1 } }));
Console.WriteLine(JsonSerializer.Serialize(new { passed, failed = 0 }));
if (args.Contains("--live"))
{
    var root = Path.GetFullPath("xingcheng");
    JsonElement Native(string verb, params string[] options)
    {
        var start = new System.Diagnostics.ProcessStartInfo(Path.Combine(root, "src/backend/rust/xstore/target/release/xstore.exe"))
        { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true };
        start.ArgumentList.Add(verb);
        start.ArgumentList.Add("--store"); start.ArgumentList.Add(Path.Combine(root, "xingcheng/runtime/store"));
        foreach (var option in options) start.ArgumentList.Add(option);
        using var child = System.Diagnostics.Process.Start(start)!;
        var stdout = child.StandardOutput.ReadToEndAsync(); var stderr = child.StandardError.ReadToEndAsync();
        if (!child.WaitForExit(60000)) { child.Kill(true); throw new Exception("NATIVE_TIMEOUT"); }
        if (child.ExitCode != 0) throw new Exception(stderr.GetAwaiter().GetResult());
        return JsonDocument.Parse(stdout.GetAwaiter().GetResult()).RootElement.Clone();
    }
    var queried = Native("metadata-query", "--type", "migration_marker", "--limit", "4096");
    var transitions = queried.GetProperty("records").EnumerateArray().Select(r => r.GetProperty("payload"))
        .Where(p => p.TryGetProperty("format", out var f) && f.GetString() == MetadataAuthority.TransitionFormat)
        .OrderBy(p => p.GetProperty("timestamp").GetString(), StringComparer.Ordinal).ToList();
    var latest = transitions.Count == 0 ? null : JsonSerializer.Deserialize<Dictionary<string, object?>>(transitions[^1]);
    if (latest is not null)
        foreach (var key in latest.Keys.ToArray())
            if (latest[key] is JsonElement e && e.ValueKind == JsonValueKind.String) latest[key] = e.GetString();
    var assembly = System.Reflection.Assembly.LoadFile(Path.Combine(root, "src/backend/csharp/GPTBridge.XingchengLearning/bin/Release/net10.0/xc-learning.dll"));
    var report = NativeMetadataProductionGate.Evaluate(latest, Native("metadata-verify"), assembly.GetReferencedAssemblies().Any(r => r.Name is { Length: > 0 } n && !n.StartsWith("System") && !n.StartsWith("Microsoft") && !n.StartsWith("netstandard") && !n.StartsWith("mscorlib")));
    Console.WriteLine(JsonSerializer.Serialize(report));
}

namespace GPTBridge.XingchengLearning
{
    internal static class MetadataAuthority
    {
        public const string TransitionFormat = "star-metadata-authority-transition/v1";
        public const string GateId = "NATIVE_METADATA_AUTHORITY_GATE";
    }
}
