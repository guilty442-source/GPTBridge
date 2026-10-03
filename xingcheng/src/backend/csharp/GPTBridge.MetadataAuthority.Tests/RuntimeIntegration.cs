using System.Diagnostics;
using System.Reflection;
using System.Text.Json;

internal static class RuntimeIntegration
{
    internal static int Run()
    {
        var sourceRoot = Path.GetFullPath("xingcheng");
        var executable = Path.Combine(sourceRoot, "src/backend/rust/xstore/target/release/xstore.exe");
        var runtime = Path.Combine(sourceRoot, "src/backend/csharp/GPTBridge.XingchengLearning/bin/Release/net10.0/xc-learning.exe");
        var assembly = Assembly.LoadFile(Path.ChangeExtension(runtime, ".dll"));
        if (assembly.GetReferencedAssemblies().Any(r => r.Name is "Npgsql")) throw new Exception("PRODUCTION_PG_REFERENCE");
        var root = Path.Combine(Path.GetTempPath(), "gptbridge-native-metadata-runtime-" + Guid.NewGuid().ToString("N"));
        var native = Path.Combine(root, "src/backend/rust/xstore/target/release/xstore.exe");
        Directory.CreateDirectory(Path.GetDirectoryName(native)!); File.Copy(executable, native);
        var store = Path.Combine(root, "xingcheng/runtime/store");
        (int Exit, JsonElement Report) Run(string exe, params string[] args)
        {
            var start = new ProcessStartInfo(exe) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true };
            start.Environment.Remove("GPTBRIDGE_POSTGRES_DSN"); start.Environment.Remove("XINGCHENG_SHARED_PG_SCHEMA");
            foreach (var argument in args) start.ArgumentList.Add(argument);
            using var child = Process.Start(start)!;
            var stdout = child.StandardOutput.ReadToEndAsync(); var stderr = child.StandardError.ReadToEndAsync();
            if (!child.WaitForExit(60000)) { child.Kill(true); throw new Exception("RUNTIME_TIMEOUT"); }
            var text = stdout.GetAwaiter().GetResult();
            if (text.Length == 0) throw new Exception(stderr.GetAwaiter().GetResult());
            return (child.ExitCode, JsonDocument.Parse(text).RootElement.Clone());
        }
        var denied = Run(runtime, "--tool-root", root, "--db-status");
        if (denied.Exit == 0) throw new Exception("MISSING_AUTHORITY_ACCEPTED");
        var input = Path.Combine(root, "fixture-transition.json");
        File.WriteAllText(input, JsonSerializer.Serialize(new { record_type = "migration_marker", record = new { record_id = "fixture-only", format = "star-metadata-authority-transition/v1", new_authority = "xstore", old_authority = "postgresql", timestamp = "2026-10-02T00:00:00Z" } }));
        var put = Run(native, "metadata-put", "--store", store, "--op", "put", "--params", "@" + input);
        if (put.Exit != 0) throw new Exception("FIXTURE_AUTHORITY_FAILED");
        var status = Run(runtime, "--tool-root", root, "--db-status");
        if (status.Exit != 0) throw new Exception("PG_FREE_RUNTIME_FAILED:" + status.Report);
        var statePath = Path.Combine(root, "xingcheng/runtime/state/self-learning.json");
        var atomic = assembly.GetType("GPTBridge.XingchengLearning.ModelLifecycle", true)!.GetMethod("AtomicWrite", BindingFlags.NonPublic | BindingFlags.Static)!;
        var projection = assembly.GetType("GPTBridge.XingchengLearning.NativeStateProjection", true)!;
        var read = projection.GetMethod("ReadAllText", BindingFlags.Public | BindingFlags.Static)!;
        var exists = projection.GetMethod("Exists", BindingFlags.Public | BindingFlags.Static)!;
        atomic.Invoke(null, new object[] { statePath, "{\"format\":\"star-self-learning-state/v1\",\"trained_example_total\":7}" });
        File.WriteAllText(statePath, "{\"trained_example_total\":999}");
        if (!((string)read.Invoke(null, new object[] { statePath })!).Contains("7")) throw new Exception("FORGED_PROJECTION_ACCEPTED");
        File.Delete(statePath);
        if (!(bool)exists.Invoke(null, new object[] { statePath })!) throw new Exception("STATE_LOST_WITH_PROJECTION");
        using var loaded = JsonDocument.Parse((string)read.Invoke(null, new object[] { statePath })!);
        if (loaded.RootElement.GetProperty("trained_example_total").GetInt32() != 7) throw new Exception("STATE_RESTORE_FAILED");
        var save = projection.GetMethod("TrySave", BindingFlags.NonPublic | BindingFlags.Static)!;
        using var barrier = new Barrier(2);
        var outcomes = new string[2];
        var writers = Enumerable.Range(0, 2).Select(i => new Thread(() =>
        {
            read.Invoke(null, new object[] { statePath }); barrier.SignalAndWait();
            try { save.Invoke(null, new object[] { statePath, "{\"trained_example_total\":" + (8 + i) + "}" }); outcomes[i] = "committed"; }
            catch (TargetInvocationException error) { outcomes[i] = error.InnerException?.GetType().GetProperty("Code")?.GetValue(error.InnerException)?.ToString() ?? "unknown"; }
        })).ToArray();
        foreach (var writer in writers) writer.Start(); foreach (var writer in writers) writer.Join();
        if (outcomes.Count(result => result == "committed") != 1 || outcomes.Count(result => result == "XSTORE_REVISION_CONFLICT") != 1)
            throw new Exception("STATE_CONCURRENT_WRITER_NOT_SERIALIZED:" + string.Join(',', outcomes));
        File.WriteAllText(statePath, (string)read.Invoke(null, new object[] { statePath })!);
        foreach (var name in new[] { "model-maturation-300m.json", "capability-maturity.json", "model-maturity.json", "production-certification.json" })
            File.WriteAllText(Path.Combine(Path.GetDirectoryName(statePath)!, name), "{\"format\":\"fixture\",\"capabilities\":{}}");
        var backfill = assembly.GetType("GPTBridge.XingchengLearning.RuntimeStateBackfill", true)!.GetMethod("Run", BindingFlags.Public | BindingFlags.Static)!;
        var first = (Dictionary<string, object?>)backfill.Invoke(null, new object[] { root })!;
        if (!(bool)first["ok"]! || (int)first["written_count"]! != 4 || (int)first["deduplicated_count"]! != 1) throw new Exception("BACKFILL_FIRST_RECEIPT_INVALID");
        var repeated = (Dictionary<string, object?>)backfill.Invoke(null, new object[] { root })!;
        if (!(bool)repeated["ok"]! || (int)repeated["written_count"]! != 0 || (int)repeated["deduplicated_count"]! != 5) throw new Exception("BACKFILL_NOT_IDEMPOTENT");
        File.WriteAllText(statePath, "{\"trained_example_total\":999}");
        var conflict = (Dictionary<string, object?>)backfill.Invoke(null, new object[] { root })!;
        if ((bool)conflict["ok"]! || (int)conflict["failed_count"]! != 1) throw new Exception("BACKFILL_OVERWROTE_CANONICAL_STATE");
        var generationPath = Path.Combine(root, "xingcheng/runtime/state/generation/state.json");
        atomic.Invoke(null, new object[] { generationPath, "{\"active_generation\":\"fixture-N\"}" });
        File.Delete(generationPath); Directory.Delete(Path.GetDirectoryName(generationPath)!);
        if (!(bool)exists.Invoke(null, new object[] { generationPath })!) throw new Exception("GENERATION_CANONICAL_STATE_LOST");
        if (!((string)read.Invoke(null, new object[] { generationPath })!).Contains("fixture-N")) throw new Exception("GENERATION_RESTORE_FAILED");
        var directoryExists = projection.GetMethod("DirectoryExists", BindingFlags.Public | BindingFlags.Static)!;
        if (!(bool)directoryExists.Invoke(null, new object[] { Path.GetDirectoryName(generationPath)! })!) throw new Exception("GENERATION_DIRECTORY_RESTORE_FAILED");
        var enumerate = projection.GetMethod("EnumerateFiles", BindingFlags.Public | BindingFlags.Static)!;
        if (!((IEnumerable<string>)enumerate.Invoke(null, new object[] { Path.GetDirectoryName(generationPath)!, "*.json" })!).Contains(Path.GetFullPath(generationPath))) throw new Exception("GENERATION_MANIFEST_ENUMERATION_LOST");
        var snap = Run(native, "metadata-snapshot", "--store", store);
        if (snap.Exit != 0) throw new Exception("SNAPSHOT_FAILED");
        var hash = snap.Report.GetProperty("snapshot_sha256").GetString()!;
        File.WriteAllText(Path.Combine(store, "objects", hash[..2], hash + ".bin"), "corrupt");
        var corrupt = Run(runtime, "--tool-root", root, "--db-status");
        if (corrupt.Exit == 0) throw new Exception("CORRUPT_SNAPSHOT_STARTUP_ACCEPTED");
        var emptyEvidence = Path.Combine(root, "empty-evidence.json"); File.WriteAllText(emptyEvidence, "{}");
        var certify = assembly.GetType("GPTBridge.XingchengLearning.ProductionClosure", true)!
            .GetMethod("AxisMark", BindingFlags.Public | BindingFlags.Static)!;
        try { certify.Invoke(null, new object[] { root, "runtime", "PASS", emptyEvidence, "fixture" }); throw new Exception("EMPTY_CERTIFICATION_ACCEPTED"); }
        catch (TargetInvocationException error) when (error.InnerException?.Message.Contains("executed passing verdict") == true) { }
        var shortLog = Path.Combine(root, "short-soak.jsonl");
        File.WriteAllLines(shortLog, Enumerable.Range(0, 6).Select(i => JsonSerializer.Serialize(new {
            format = "star-runtime-soak-sample/v1", elapsed_s = i, alive = true, rss_bytes = 1000, commit_bytes = 1000,
            handles = 10, vram_bytes = 1000, probe_latency_ms = 1, probe_http = 200, error_count = 0,
            metadata_integrity = true, audit_integrity = true,
        })));
        var analyze = assembly.GetType("GPTBridge.XingchengLearning.ProductionSoak", true)!.GetMethod("Analyze", BindingFlags.Public | BindingFlags.Static)!;
        var analysis = (Dictionary<string, object?>)analyze.Invoke(null, new object[] { shortLog })!;
        if ((bool)analysis["ok"]!) throw new Exception("SHORT_SOAK_CERTIFIED");
        File.WriteAllText(emptyEvidence, "{\"ok\":true}");
        var evidenceHash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(emptyEvidence))).ToLowerInvariant();
        var current = assembly.GetType("GPTBridge.XingchengLearning.ProductionClosure", true)!.GetMethod("EvidenceCurrent", BindingFlags.NonPublic | BindingFlags.Static)!;
        var row = new Dictionary<string, object?> { ["state"] = "PASS", ["evidence"] = emptyEvidence, ["evidence_sha256"] = evidenceHash };
        if (!(bool)current.Invoke(null, new object[] { row })!) throw new Exception("CURRENT_PROOF_DENIED");
        File.WriteAllText(emptyEvidence, "{\"ok\":false}");
        if ((bool)current.Invoke(null, new object[] { row })!) throw new Exception("ALTERED_PROOF_ACCEPTED");
        // ---- fault injection: resource-grant revoke / governor loss
        // (spec §56-§58 resize/revoke, §75 governor-unavailable fail-closed)
        var govDir = Path.Combine(root, "main-system", "runtime", "state");
        Directory.CreateDirectory(Path.Combine(govDir, "resource-requests"));
        Directory.CreateDirectory(Path.Combine(govDir, "resource-grants"));
        var govState = Path.Combine(govDir, "resource-governor.json");
        File.WriteAllText(govState, "{\"interval\":20}");
        var clientType = assembly.GetType(
            "GPTBridge.XingchengLearning.ResourceGovernorClient", true)!;
        var client = Activator.CreateInstance(clientType, root)!;
        var currentGrant = clientType.GetMethod("CurrentGrant")!;
        var reqType = assembly.GetType(
            "GPTBridge.XingchengLearning.ResourceRequest", true)!;
        var req = Activator.CreateInstance(reqType)!;
        reqType.GetField("RequestId")!.SetValue(req, "rr-fi-g1");
        reqType.GetField("WorkloadId")!.SetValue(req, "fi-g1");
        // silent governor → DEFERRED, never an ungranted admit
        var reply1 = clientType.GetMethod("Request")!
            .Invoke(client, new object?[] { req, 2 })!;
        if (reply1.GetType().GetField("Response")!.GetValue(reply1)
                ?.ToString() != "Deferred")
            throw new Exception("GOVERNOR_SILENCE_NOT_DEFERRED");
        // granted → revoked rewrite: the consumer must drop the grant
        var grantPath = Path.Combine(govDir, "resource-grants",
            "rr-fi-g2.json");
        File.WriteAllText(grantPath, JsonSerializer.Serialize(new
        {
            response = "GRANTED", request_id = "rr-fi-g2",
            grant = new
            {
                grant_id = "rg-fi", workload_class = "training",
                cpu_threads_max = 4,
                valid_until_s =
                    DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 300,
            },
        }));
        if (currentGrant.Invoke(client, new object?[] { "rr-fi-g2" })
                == null)
            throw new Exception("GRANT_NOT_VISIBLE");
        File.WriteAllText(grantPath, JsonSerializer.Serialize(new
        {
            response = "REVOKED", request_id = "rr-fi-g2",
            reason = "pressure-shed",
        }));
        if (currentGrant.Invoke(client, new object?[] { "rr-fi-g2" })
                != null)
            throw new Exception("REVOKED_GRANT_STILL_HELD");
        // governor state file absent → Unavailable (fail-closed)
        File.Delete(govState);
        var reply3 = clientType.GetMethod("Request")!
            .Invoke(client, new object?[] { req, 1 })!;
        if (!(bool)reply3.GetType().GetField("Unavailable")!
                .GetValue(reply3)!)
            throw new Exception("GOVERNOR_ABSENT_NOT_FAILCLOSED");
        Console.WriteLine(JsonSerializer.Serialize(new { artifact = "pg-free-runtime-integration", passed = 22, failed = 0, fixture = root }));
        return 22;
    }
}
