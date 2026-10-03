using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Native audit-engine gate — parity with
/// governance_rule.execution.audit.native_audit_gate: staleness-checked
/// engine build, bounded run, manifest/report reconciliation, provenance.
/// With the Python lane retired, delegated manifest rows or an unavailable
/// engine are fail-closed (there is no Python oracle to fall back to).
/// </summary>
internal static class AuditGate
{
    private const string EngineRelative =
        "native/test_suites/bin/audit-engine.exe";
    private const string ManifestRelative =
        "governance_rule/execution/audit/audit_checks_manifest.json";
    private const string ReportRelative =
        "native/test_suites/bin/audit-report.json";
    private const int EngineTimeoutMs = 25_000;

    public sealed class Result
    {
        public string Status = "delegated"; // pass|fail|delegated|timeout
        public double ElapsedSeconds;
        public int Passed;
        public int Failed;
        public int Delegated;
        public List<string> Errors = new();
        public string Note = "";

        public string Summary() => Status switch
        {
            "delegated" => $"native audit engine delegated: {Note}",
            "timeout" => $"native audit engine timeout after " +
                         $"{ElapsedSeconds:F2}s (fail-closed)",
            _ => $"native audit engine: {Passed} pass / {Failed} fail / " +
                 $"{Delegated} delegated in {ElapsedSeconds:F2}s",
        };
    }

    private static string EngineExe(string root) =>
        Path.Combine(root, EngineRelative.Replace('/', Path.DirectorySeparatorChar));

    private static string ManifestPath(string root) =>
        Path.Combine(root, ManifestRelative.Replace('/', Path.DirectorySeparatorChar));

    private static readonly string[] EngineDeps =
    {
        "native/audit/audit_engine.cpp",
        "native/include/audit_engine.h",
    };

    private static string? FindVcvars()
    {
        var roots = new[]
        {
            @"E:\Program Files\Microsoft Visual Studio\18\Community",
            @"C:\Program Files\Microsoft Visual Studio\2022\Community",
            @"C:\Program Files\Microsoft Visual Studio\2022\BuildTools",
        };
        foreach (var root in roots)
        {
            var candidate = Path.Combine(
                root, "VC", "Auxiliary", "Build", "vcvars64.bat");
            if (File.Exists(candidate))
                return candidate;
        }
        return null;
    }

    private static string SourceDigest(string root)
    {
        using var sha = System.Security.Cryptography.SHA256.Create();
        foreach (var dep in EngineDeps)
        {
            var path = Path.Combine(root,
                dep.Replace('/', Path.DirectorySeparatorChar));
            sha.TransformBlock(
                Encoding.UTF8.GetBytes(Path.GetFileName(path)),
                0, Path.GetFileName(path).Length, null, 0);
            if (File.Exists(path))
            {
                var bytes = File.ReadAllBytes(path);
                sha.TransformBlock(bytes, 0, bytes.Length, null, 0);
            }
        }
        sha.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
        return Convert.ToHexString(sha.Hash!).ToLowerInvariant();
    }

    private static bool EngineStale(string root, string exe)
    {
        var exeTime = File.GetLastWriteTimeUtc(exe);
        foreach (var dep in EngineDeps)
        {
            var path = Path.Combine(root,
                dep.Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(path)
                && File.GetLastWriteTimeUtc(path) > exeTime)
                return true;
        }
        var digestFile = exe + ".sha256";
        try
        {
            var recorded = File.ReadAllText(digestFile).Trim();
            return recorded != SourceDigest(root);
        }
        catch (IOException)
        {
            return true;
        }
    }

    private static bool BuildEngine(string root)
    {
        var vcvars = FindVcvars();
        if (vcvars is null)
            return false;
        var exe = EngineExe(root);
        Directory.CreateDirectory(Path.GetDirectoryName(exe)!);
        var src = Path.Combine(root, "native", "audit", "audit_engine.cpp");
        var include = Path.Combine(root, "native", "include");
        var bat = "@echo off\r\n" +
            $"call \"{vcvars}\" >nul || exit /b 1\r\n" +
            "cl /nologo /std:c++latest /utf-8 /O2 /EHsc " +
            $"/DGPTBRIDGE_AUDIT_ENGINE_CLI /I\"{include}\" " +
            $"/Fe:\"{exe}\" /Fo:\"{Path.GetDirectoryName(exe)}\\\\\" " +
            $"\"{src}\" >nul || exit /b 1\r\n";
        var batPath = Path.Combine(
            Path.GetDirectoryName(exe)!, "_audit_engine_build.bat");
        File.WriteAllText(batPath, bat, Encoding.ASCII);
        try
        {
            var startInfo = new ProcessStartInfo("cmd", $"/c \"{batPath}\"")
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using var process = Process.Start(startInfo)!;
            if (!process.WaitForExit(300_000))
            {
                try { process.Kill(entireProcessTree: true); }
                catch (InvalidOperationException) { }
                return false;
            }
            var ok = process.ExitCode == 0 && File.Exists(exe);
            if (ok)
                File.WriteAllText(exe + ".sha256",
                    SourceDigest(root) + "\n", Encoding.ASCII);
            return ok;
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>
    /// Codex authority imported_at probe (parity with
    /// codex_postgresql.authority_state) via the governed CodexPipeline
    /// read lane — never a raw SQL client: --authority-state emits the
    /// authority row through PgDsn.Readonly, so this gate rides the same
    /// credential boundary as every other governed read and no external
    /// SQL binary is required. Anything missing or unreadable is treated
    /// as stale (fail-closed regeneration request).
    /// </summary>
    private static double? CodexAuthorityEpoch(string root)
    {
        var exe = SqlSync.PipelineExe();
        if (exe is null)
            return null;
        var run = Git.Exec(exe, null,
            new[] { "--authority-state" }, 60_000);
        if (run.TimedOut || run.Code != 0)
            return null;
        var text = run.Stdout.Trim();
        var start = text.IndexOf('{');
        if (start < 0)
            return null;
        try
        {
            if (JsonNode.Parse(text[start..]) is not JsonObject row)
                return null;
            var imported = row["imported_at"]?.GetValue<string>();
            if (string.IsNullOrWhiteSpace(imported))
                return null;
            // CanonJson PyStr(datetime): "YYYY-MM-DD HH:MM:SS[.ffffff]";
            // codex_authority_state.imported_at is timestamptz read back
            // as UTC without a zone marker.
            return DateTime.TryParse(imported,
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AssumeUniversal
                | System.Globalization.DateTimeStyles.AdjustToUniversal,
                out var at)
                ? new DateTimeOffset(at).ToUnixTimeSeconds()
                : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary>
    /// Manifest freshness (parity with _refresh_manifest_if_stale):
    /// stale when absent, the codex authority is newer, or any audit
    /// module source is newer.  Python regeneration is retired, so a
    /// stale manifest is fail-closed rather than silently regenerated.
    /// </summary>
    private static string? ManifestStaleReason(string root)
    {
        var manifest = ManifestPath(root);
        if (!File.Exists(manifest))
            return "manifest-missing";
        var manifestTime = File.GetLastWriteTimeUtc(manifest);
        var authority = CodexAuthorityEpoch(root);
        if (authority is null)
            return "codex-authority-unreadable";
        if (authority.Value >
            new DateTimeOffset(manifestTime).ToUnixTimeSeconds())
            return "codex-newer-than-manifest";
        var auditDir = Path.Combine(root, "governance_rule",
            "execution", "audit");
        if (Directory.Exists(auditDir))
        {
            foreach (var module in Directory.EnumerateFiles(auditDir, "*.py"))
            {
                if (File.GetLastWriteTimeUtc(module) > manifestTime)
                    return $"audit-module-newer:{Path.GetFileName(module)}";
            }
        }
        return null;
    }

    public static Result Run(string root)
    {
        var result = new Result();
        var exe = EngineExe(root);
        if (!File.Exists(exe) || EngineStale(root, exe))
        {
            if (!BuildEngine(root))
            {
                result.Note =
                    "audit-engine.exe unavailable and unbuildable; " +
                    "python oracle retired — no delegated coverage";
                return result;
            }
        }
        var stale = ManifestStaleReason(root);
        if (stale is not null)
        {
            // Governed refresh lane — the C# port of
            // export_audit_manifest.build_manifest replaces the
            // retired Python regeneration path.
            try { ManifestExport.Refresh(root); }
            catch (Exception error)
            {
                result.Status = "fail";
                result.Errors.Add(
                    $"manifest refresh required ({stale}) and the " +
                    $"governed C# exporter failed: {error.Message}");
                return result;
            }
            var stillStale = ManifestStaleReason(root);
            if (stillStale is not null)
            {
                result.Status = "fail";
                result.Errors.Add(
                    $"manifest still stale after governed refresh: " +
                    stillStale);
                return result;
            }
        }
        var manifest = ManifestPath(root);
        var reportPath = Path.Combine(root,
            ReportRelative.Replace('/', Path.DirectorySeparatorChar));
        var started = Stopwatch.StartNew();
        var run = Git.Exec(exe, root, new[]
        {
            "--manifest", manifest, "--root", root,
            "--report", reportPath,
        }, EngineTimeoutMs);
        result.ElapsedSeconds = started.Elapsed.TotalSeconds;
        if (run.TimedOut)
        {
            result.Status = "timeout";
            result.Errors.Add(
                $"native audit engine exceeded {EngineTimeoutMs / 1000}s");
            return result;
        }
        if (run.Code == -1 && run.Stderr.StartsWith("spawn:"))
        {
            result.Note =
                $"engine spawn failed ({run.Stderr}); python oracle retired";
            return result;
        }
        result.Status = run.Code == 0 ? "pass" : "fail";
        JsonObject report;
        var reportText = run.Stdout;
        try
        {
            if (File.Exists(reportPath))
                reportText = File.ReadAllText(reportPath);
        }
        catch (IOException) { }
        try
        {
            report = JsonNode.Parse(reportText) as JsonObject
                     ?? new JsonObject();
        }
        catch (JsonException)
        {
            report = new JsonObject();
        }
        result.Passed = report["passed"]?.GetValue<int>() ?? 0;
        result.Failed = report["failed"]?.GetValue<int>() ?? 0;
        result.Delegated = report["delegated"]?.GetValue<int>() ?? 0;
        if (report["manifest_ok"]?.GetValue<bool>() == false)
        {
            result.Status = "fail";
            result.Errors.Add(
                $"manifest error: {report["manifest_error"] ?? "unknown"}");
        }
        // Evidence reconciliation — identical to the Python gate.
        var manifestIds = new HashSet<string>(StringComparer.Ordinal);
        var manifestDelegated = new HashSet<string>(StringComparer.Ordinal);
        try
        {
            var manifestDoc = JsonNode.Parse(
                File.ReadAllText(manifest)) as JsonObject;
            var checks = manifestDoc?["checks"] as JsonArray
                         ?? new JsonArray();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var check in checks.OfType<JsonObject>())
            {
                var id = check["id"]?.GetValue<string>() ?? "";
                manifestIds.Add(id);
                if (!seen.Add(id))
                    result.Errors.Add(
                        "manifest contains duplicate check ids");
                if (check["kind"]?.GetValue<string>() == "delegated")
                    manifestDelegated.Add(id);
            }
        }
        catch (IOException)
        {
            result.Errors.Add(
                "manifest unreadable during report reconciliation");
        }
        catch (JsonException)
        {
            result.Errors.Add(
                "manifest unreadable during report reconciliation");
        }
        var reportChecks = report["checks"] as JsonArray ?? new JsonArray();
        var reportIds = new List<string>();
        foreach (var check in reportChecks.OfType<JsonObject>())
            reportIds.Add(check["id"]?.GetValue<string>() ?? "");
        var dupes = reportIds.GroupBy(i => i)
            .Where(g => g.Count() > 1).Select(g => g.Key).ToList();
        if (dupes.Count > 0)
            result.Errors.Add(
                $"report contains duplicate result rows: " +
                $"{string.Join(',', dupes.Take(5))}");
        var reportIdSet = reportIds.ToHashSet(StringComparer.Ordinal);
        var missing = manifestIds.Except(reportIdSet).ToList();
        if (missing.Count > 0)
            result.Errors.Add(
                $"report missing results for {missing.Count} manifest " +
                $"checks: {string.Join(',', missing.Take(5))}");
        var foreign = reportIdSet.Except(manifestIds).ToList();
        if (foreign.Count > 0)
            result.Errors.Add(
                $"report contains {foreign.Count} ids absent from " +
                $"manifest: {string.Join(',', foreign.Take(5))}");
        foreach (var check in reportChecks.OfType<JsonObject>())
        {
            var status = check["status"]?.GetValue<string>();
            var id = check["id"]?.GetValue<string>() ?? "?";
            if (status == "FAIL")
                result.Errors.Add(
                    $"{id}: {check["detail"]?.GetValue<string>()}");
            else if (status == "DELEGATED")
            {
                if (!manifestDelegated.Contains(id))
                    result.Errors.Add(
                        $"{id}: engine-delegated without manifest " +
                        "delegated row — check would never execute");
                // A manifest delegated row cannot execute: the Python
                // delegated lane is retired — fail closed.
                else
                    result.Errors.Add(
                        $"{id}: delegated check unexecutable — " +
                        "python audit lane retired");
            }
            else if (status != "PASS")
            {
                result.Errors.Add(
                    $"{id}: unknown report status '{status}' — " +
                    "evidence not trustworthy");
            }
        }
        if (result.Errors.Count > 0)
            result.Status = "fail";
        // Provenance (G96/G99) — same bindings as the Python gate.
        try
        {
            var provenance = new JsonObject
            {
                ["engine_sha256"] = Canon.Sha256File(exe),
                ["manifest_sha256"] = Canon.Sha256File(manifest),
            };
            var head = Git.Run(root, new[] { "rev-parse", "HEAD" },
                               timeoutMs: 10_000);
            provenance["source_revision"] =
                head.Code == 0 ? head.Stdout.Trim() : null;
            var binDir = Path.GetDirectoryName(exe)!;
            var suiteManifest = Path.Combine(binDir, "suite-manifest.json");
            if (File.Exists(suiteManifest))
            {
                provenance["suite_manifest_sha256"] =
                    Canon.Sha256File(suiteManifest);
                try
                {
                    var doc = JsonNode.Parse(
                        File.ReadAllText(suiteManifest)) as JsonObject;
                    provenance["suite_manifest_revision"] =
                        doc?["revision"]?.GetValue<string>();
                }
                catch (JsonException)
                {
                    provenance["suite_manifest_revision"] = null;
                }
            }
            var orchReport = Path.Combine(
                binDir, "native-orchestration-report.json");
            if (File.Exists(orchReport))
            {
                try
                {
                    var doc = JsonNode.Parse(
                        File.ReadAllText(orchReport)) as JsonObject;
                    provenance["suite_artifact_hash"] =
                        doc?["artifact_hash"]?.GetValue<string>();
                    provenance["suite_verdict"] =
                        doc?["verdict"]?.GetValue<string>();
                }
                catch (JsonException)
                {
                    provenance["suite_artifact_hash"] = null;
                }
            }
            try
            {
                var reportDoc = JsonNode.Parse(
                    File.ReadAllText(reportPath)) as JsonObject
                    ?? new JsonObject();
                reportDoc["provenance"] = provenance;
                Canon.WriteJsonAtomic(reportPath,
                    Canon.Indented(
                        JsonDocument.Parse(reportDoc.ToJsonString())
                            .RootElement) + "\n");
            }
            catch (IOException error)
            {
                result.Errors.Add(
                    $"audit report provenance write failed: {error.Message}");
                result.Status = "fail";
            }
            catch (JsonException error)
            {
                result.Errors.Add(
                    $"audit report provenance write failed: {error.Message}");
                result.Status = "fail";
            }
        }
        catch (IOException error)
        {
            result.Errors.Add($"provenance hashing failed: {error.Message}");
            result.Status = "fail";
        }
        if (run.Code != 0 && result.Errors.Count == 0)
            result.Errors.Add(
                $"audit engine exited {run.Code}: " +
                $"{run.Stderr.Trim()[..Math.Min(200, run.Stderr.Trim().Length)]}");
        return result;
    }
}
