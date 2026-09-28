using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Mandatory native-test push gate + push/convergence evidence +
/// release checkpoints — parity with git_tiers.push_gate,
/// release_checkpoint and repo_sync.
/// </summary>
internal static class PushGate
{
    private const string OrchestratorReleaseRelative =
        "native/test_suites/csharp/bin/Release/net10.0/TestSuiteOrchestrator.exe";
    private const string OrchestratorDebugRelative =
        "native/test_suites/csharp/bin/Debug/net10.0/TestSuiteOrchestrator.exe";
    private const string OrchestratorProjectRelative =
        "native/test_suites/csharp/TestSuiteOrchestrator.csproj";
    private const string OrchestratorSourceRelative =
        "native/test_suites/csharp/Program.cs";
    private const string BinRelative = "native/test_suites/bin";
    private const string BuildScriptRelative =
        "native/test_suites/build.ps1";
    private const string StateFilename = "gptbridge-push-gate.json";

    private static readonly string[] DepRoots =
    {
        "native/test_suites", "native/core", "native/include",
        "native/tool_runtime", "native/audit",
        "Standalone tools/local-model/src/backend/cpp",
    };

    private static readonly HashSet<string> CodeSuffixes =
        new(StringComparer.OrdinalIgnoreCase)
        { ".c", ".cpp", ".h", ".hpp" };

    private static string Rel(string root, string relative) =>
        Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar));

    private sealed class Config
    {
        public bool Enabled = true;
        public bool AutoBuild = true;
        public double BuildTimeoutS = 600;
        public double SuiteTimeoutS = 30;
        public double RunBudgetS = 120;
        public double LockWaitS = 30;
        public int MaxParallel = 4;
        public string? ConfigError;
    }

    private static Config LoadConfig(string root)
    {
        var config = new Config();
        JsonObject? entry;
        try
        {
            var path = Rel(root, "main-system/config/automation-flows.json");
            var node = JsonNode.Parse(File.ReadAllText(path));
            entry = node?["flows"]?["git-automation"]?["push_gate"]
                as JsonObject;
        }
        catch (Exception error)
        {
            config.ConfigError = error.GetType().Name;
            return config;
        }
        if (entry is null)
            return config;
        if (entry["enabled"] is { } e) config.Enabled = e.GetValue<bool>();
        if (entry["auto_build"] is { } a)
            config.AutoBuild = a.GetValue<bool>();
        if (entry["build_timeout_s"]?.GetValue<double>() is double b && b > 0)
            config.BuildTimeoutS = b;
        if (entry["suite_timeout_s"]?.GetValue<double>() is double s && s > 0)
            config.SuiteTimeoutS = s;
        if (entry["run_budget_s"]?.GetValue<double>() is double r && r > 0)
            config.RunBudgetS = r;
        if (entry["lock_timeout_s"]?.GetValue<double>() is double l0 && l0 > 0)
            config.LockWaitS = l0;
        if (entry["lock_wait_s"]?.GetValue<double>() is double l && l > 0)
            config.LockWaitS = l;
        if (entry["max_parallel_suites"]?.GetValue<int>() is int p && p >= 1)
            config.MaxParallel = Math.Min(8, p);
        return config;
    }

    private static double NewestSourceTime(string root)
    {
        double newest = 0;
        foreach (var rel in DepRoots)
        {
            var depRoot = Rel(root, rel);
            if (!Directory.Exists(depRoot))
                continue;
            foreach (var file in Directory.EnumerateFiles(
                         depRoot, "*", SearchOption.AllDirectories))
            {
                if (!CodeSuffixes.Contains(Path.GetExtension(file)))
                    continue;
                var time = new DateTimeOffset(
                    File.GetLastWriteTimeUtc(file)).ToUnixTimeSeconds();
                if (time > newest)
                    newest = time;
            }
        }
        return newest;
    }

    private static List<string> SuiteExes(string binDir) =>
        Directory.Exists(binDir)
            ? Directory.EnumerateFiles(binDir, "*_suite.exe")
                .Where(p => !Path.GetFileName(p).StartsWith('_'))
                .OrderBy(p => p, StringComparer.Ordinal).ToList()
            : new List<string>();

    private static JsonObject? SuiteManifest(string binDir)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(
                Path.Combine(binDir, "suite-manifest.json")));
            return node as JsonObject;
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
    }

    private static List<string> StaleArtifacts(
        string binDir, JsonObject? manifest)
    {
        var stale = new List<string>();
        if (manifest?["suites"] is not JsonArray suites)
            return stale;
        foreach (var row in suites.OfType<JsonObject>())
        {
            var exe = row["exe"]?.GetValue<string>() ?? "";
            var expected =
                row["sha256"]?.GetValue<string>()?.ToLowerInvariant() ?? "";
            if (exe.Length == 0)
                continue;
            var path = Path.Combine(binDir, exe);
            string actual;
            try { actual = Canon.Sha256File(path); }
            catch (IOException) { actual = ""; }
            if (expected.Length == 0 || actual != expected)
                stale.Add(row["name"]?.GetValue<string>() ?? exe);
        }
        return stale;
    }

    private static string? OrchestratorExe(string root)
    {
        foreach (var rel in new[]
                 { OrchestratorReleaseRelative, OrchestratorDebugRelative })
        {
            var path = Rel(root, rel);
            if (File.Exists(path))
                return path;
        }
        return null;
    }

    private static bool OrchestratorStale(string root, string exe)
    {
        var exeTime = File.GetLastWriteTimeUtc(exe);
        foreach (var rel in new[]
                 { OrchestratorSourceRelative, OrchestratorProjectRelative })
        {
            var src = Rel(root, rel);
            if (File.Exists(src)
                && File.GetLastWriteTimeUtc(src) > exeTime)
                return true;
        }
        return false;
    }

    private static string? EnsureOrchestrator(string root, double timeoutS)
    {
        var exe = OrchestratorExe(root);
        if (exe is not null && !OrchestratorStale(root, exe))
            return exe;
        var run = Git.Exec("dotnet", root, new[]
        {
            "build", Rel(root, OrchestratorProjectRelative),
            "-c", "Release", "--nologo", "-v", "q",
        }, (int)(timeoutS * 1000));
        if (run.Code != 0)
            return null;
        return OrchestratorExe(root);
    }

    private static bool BinariesStale(string root, List<string> exes) =>
        exes.Count == 0
        || NewestSourceTime(root) > new DateTimeOffset(
            exes.Select(File.GetLastWriteTimeUtc).Min())
            .ToUnixTimeSeconds();

    private static string StatePath(string root) =>
        Path.Combine(Git.CommonDir(root), StateFilename);

    private static JsonObject ReadState(string path)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(path));
            return node as JsonObject ?? new JsonObject();
        }
        catch (IOException) { return new JsonObject(); }
        catch (JsonException) { return new JsonObject(); }
    }

    private static void WriteState(string path, JsonObject state)
    {
        try
        {
            using var document = JsonDocument.Parse(state.ToJsonString());
            Canon.WriteJsonAtomic(
                path, Canon.Indented(document.RootElement) + "\n");
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    /// <summary>mandatory_test_gate parity — the sole C# orchestrator is
    /// invoked once; its report decides the verdict. Fail-closed on any
    /// missing piece.</summary>
    public static JsonObject MandatoryTestGate(string root)
    {
        var config = LoadConfig(root);
        var gate = new JsonObject
        {
            ["gate"] = "mandatory-tests",
            ["harness"] = "native-test-suite/v1",
            ["orchestrator"] = "native-test-orchestrator/v2",
            ["passed"] = false,
            ["skipped"] = false,
            ["suites"] = new JsonArray(),
            ["totals"] = new JsonObject
            {
                ["pass"] = 0, ["fail"] = 0,
                ["blocked"] = 0, ["cases"] = 0,
            },
            ["failures"] = new JsonArray(),
            ["rebuilt"] = false,
            ["incomplete_evidence"] = false,
            ["blocked_classifications"] = new JsonArray(),
            ["duration_ms"] = 0,
            ["detail"] = "",
        };
        var started = Stopwatch.StartNew();
        JsonObject Done(string detail)
        {
            gate["duration_ms"] =
                (int)started.Elapsed.TotalMilliseconds;
            gate["detail"] = detail;
            return gate;
        }
        if (config.ConfigError is not null)
            return Done($"config-error:{config.ConfigError}");
        if (!config.Enabled)
        {
            gate["skipped"] = true;
            return Done("push_gate.enabled=false (governed config); " +
                        "mandatory-test PASS precondition unmet");
        }
        var binDir = Rel(root, BinRelative);
        var commonDir = Git.CommonDir(root);
        var lockPath = Path.Combine(commonDir, "gptbridge-automation",
                                    "push-test-gate.lock");
        Directory.CreateDirectory(Path.GetDirectoryName(lockPath)!);
        var deadline = DateTime.UtcNow.AddSeconds(config.LockWaitS);
        ProcessFileLock? gateLock = null;
        while (gateLock is null)
        {
            try { gateLock = ProcessFileLock.Acquire(lockPath); }
            catch (LockBusyException)
            {
                if (DateTime.UtcNow >= deadline)
                    return Done($"gate-busy:{Path.GetFileName(lockPath)}");
                Thread.Sleep(500);
            }
        }
        JsonObject? manifest = null;
        JsonObject report = new();
        string verdict = "FAIL";
        try
        {
            var exe = EnsureOrchestrator(root, 120);
            if (exe is null)
                return Done(
                    "orchestrator-unavailable: TestSuiteOrchestrator " +
                    "missing and `dotnet build` failed");
            var exes = SuiteExes(binDir);
            manifest = SuiteManifest(binDir);
            var hashStale = StaleArtifacts(binDir, manifest);
            if (BinariesStale(root, exes)
                || manifest?["revision"] is null
                || hashStale.Count > 0)
            {
                if (!config.AutoBuild)
                    return Done(
                        "test-binaries-stale-or-missing " +
                        "(push_gate.auto_build=false)");
                var build = Git.Exec("powershell.exe", root, new[]
                {
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", Rel(root, BuildScriptRelative),
                }, (int)(config.BuildTimeoutS * 1000));
                if (build.TimedOut)
                    return Done("build-timeout");
                exes = SuiteExes(binDir);
                manifest = SuiteManifest(binDir);
                if (exes.Count == 0 || BinariesStale(root, exes)
                    || StaleArtifacts(binDir, manifest).Count > 0)
                    return Done($"build-failed:rc={build.Code}");
                if (manifest?["revision"] is null)
                    return Done("suite-manifest-missing");
                gate["rebuilt"] = true;
            }
            var run = Git.Exec(exe, binDir, new[]
            {
                "--run", "--bin", binDir,
                "--max-parallel", config.MaxParallel.ToString(),
                "--suite-timeout-s",
                ((int)config.SuiteTimeoutS).ToString(),
                "--require-manifest",
            }, (int)(config.RunBudgetS * 1000));
            if (run.TimedOut)
                return Done(
                    $"run-budget-exceeded:{config.RunBudgetS}s " +
                    "(orchestrator)");
            try
            {
                report = JsonNode.Parse(File.ReadAllText(Path.Combine(
                    binDir, "native-orchestration-report.json")))
                    as JsonObject ?? new JsonObject();
            }
            catch (IOException)
            {
                return Done($"orchestration-report-missing:rc={run.Code}");
            }
            catch (JsonException)
            {
                return Done($"orchestration-report-missing:rc={run.Code}");
            }
            gate["suites"] = report["suites"]?.DeepClone()
                             ?? new JsonArray();
            gate["totals"] = new JsonObject
            {
                ["pass"] = report["passed"]?.GetValue<int>() ?? 0,
                ["fail"] = report["failed"]?.GetValue<int>() ?? 0,
                ["blocked"] = report["blocked"]?.GetValue<int>() ?? 0,
                ["cases"] = report["cases"]?.GetValue<int>() ?? 0,
            };
            gate["failures"] = new JsonArray(
                (report["failures"] as JsonArray ?? new JsonArray())
                .OfType<JsonNode>().Take(8)
                .Select(n => n.DeepClone()).ToArray());
            gate["blocked_classifications"] =
                report["blocked_classifications"]?.DeepClone()
                ?? new JsonArray();
            gate["artifact_hash"] =
                report["artifact_hash"]?.DeepClone();
            gate["stale_artifacts"] =
                report["stale_artifacts"]?.DeepClone() ?? new JsonArray();
            gate["timing"] = report["timing"]?.DeepClone()
                             ?? new JsonObject();
            verdict = (report["verdict"]?.GetValue<string>() ?? "FAIL")
                .ToUpperInvariant();
        }
        finally
        {
            gateLock.Dispose();
        }

        gate["source_revision"] = manifest?["revision"]?.DeepClone();
        var head = Git.Run(root, new[] { "rev-parse", "HEAD" });
        gate["head_revision"] =
            head.Code == 0 ? head.Stdout.Trim() : null;
        if (gate["source_revision"] is not null
            && gate["head_revision"] is not null)
            gate["revision_match"] =
                gate["source_revision"]!.GetValue<string>()
                == gate["head_revision"]!.GetValue<string>();
        var totals = gate["totals"]!;
        var fail = totals["fail"]!.GetValue<int>();
        var blocked = totals["blocked"]!.GetValue<int>();
        var cases = totals["cases"]!.GetValue<int>();
        gate["incomplete_evidence"] = blocked > 0;
        if (fail > 0)
        {
            var failures = (gate["failures"] as JsonArray)!
                .Select(n => n?.GetValue<string>()).Take(4);
            return Done($"{fail} failed case(s): " +
                        string.Join("; ", failures));
        }
        if (cases == 0)
            return Done("no-test-cases-executed");
        if (report["blocked_unclassified"]?.GetValue<bool>() == true)
            return Done(
                "blocked-unclassified: suite_criticality.json " +
                "missing/malformed");
        var critical = (gate["blocked_classifications"] as JsonArray)!
            .OfType<JsonObject>()
            .Where(c => c["criticality"]?.GetValue<string>()
                        != "experimental")
            .ToList();
        if (critical.Count > 0)
        {
            var first = critical[0];
            return Done(
                $"blocked-release-critical: " +
                $"{first["blocked_suite"]}/{first["blocked_case"]} " +
                $"(+{critical.Count - 1} more) — " +
                $"{first["affected_capability"]} unverified");
        }
        if (verdict is not ("PASS" or "INCOMPLETE_EVIDENCE"))
            return Done($"orchestrator-verdict-{verdict.ToLowerInvariant()}");
        gate["passed"] = true;
        var suffix = blocked > 0
            ? $" ({blocked} blocked — incomplete evidence)" : "";
        var pass = totals["pass"]!.GetValue<int>();
        var suiteCount = (gate["suites"] as JsonArray)!.Count;
        return Done(
            $"{pass} pass / {blocked} blocked across {suiteCount} " +
            $"suites{suffix}");
    }

    /// <summary>The push branch of synchronize(): fetch → ancestor →
    /// mandatory test gate → push → evidence. Returns the failure string
    /// or null on success.</summary>
    public static string? Execute(string projectRoot, string mainPath)
    {
        var fetched = TierGate.Execute(projectRoot, mainPath,
            new[] { "fetch", "origin", "main" }, Sync.Actor);
        if (!fetched.Allowed || fetched.Result!.Code != 0)
            return "error:fetch-origin-main";
        var remoteIsAncestor = Git.Run(mainPath, new[]
        {
            "merge-base", "--is-ancestor", "origin/main", "main",
        });
        if (remoteIsAncestor.Code != 0)
            return "error:remote-main-diverged";
        var testGate = MandatoryTestGate(projectRoot);
        if (testGate["passed"]?.GetValue<bool>() != true)
        {
            RecordPushEvidence(projectRoot, testGate, pushed: false,
                detail: testGate["detail"]?.GetValue<string>() ?? "");
            return "error:push-test-gate";
        }
        var pushed = TierGate.Execute(projectRoot, mainPath,
            new[] { "push", "origin", "main" }, Sync.Actor);
        if (!pushed.Allowed || pushed.Result!.Code != 0)
        {
            var detail = !pushed.Allowed
                ? pushed.Detail
                : (pushed.Result!.Stderr.Length > 0
                    ? pushed.Result!.Stderr
                    : pushed.Result!.Stdout).Trim();
            RecordPushEvidence(projectRoot, testGate, pushed: false,
                detail: detail[..Math.Min(160, detail.Length)]);
            return $"error:push:{detail[..Math.Min(160, detail.Length)]}";
        }
        RecordPushEvidence(projectRoot, testGate, pushed: true);
        return null;
    }

    private static (int? Ahead, int? Behind) AheadBehind(string worktree)
    {
        var ahead = Git.Run(worktree,
            new[] { "rev-list", "--count", "origin/main..main" });
        var behind = Git.Run(worktree,
            new[] { "rev-list", "--count", "main..origin/main" });
        return (
            ahead.Code == 0 && int.TryParse(ahead.Stdout.Trim(), out var a)
                ? a : null,
            behind.Code == 0 && int.TryParse(behind.Stdout.Trim(), out var b)
                ? b : null);
    }

    private static JsonObject SyncState(string worktree)
    {
        var local = Git.RevParse(worktree, "refs/heads/main");
        var origin = Git.RevParse(worktree, "refs/remotes/origin/main");
        string Pairwise()
        {
            if (local.Length == 0 || origin.Length == 0)
                return "missing";
            if (local == origin)
                return "equal";
            if (Git.Run(worktree, new[]
                    { "merge-base", "--is-ancestor", origin, local })
                    .Code == 0)
                return "ahead";
            if (Git.Run(worktree, new[]
                    { "merge-base", "--is-ancestor", local, origin })
                    .Code == 0)
                return "behind";
            return "diverged";
        }
        var relation = Pairwise();
        var state = (local.Length == 0 || origin.Length == 0)
            ? "MISSING_REF"
            : relation switch
            {
                "equal" => "IN_SYNC",
                "ahead" => "LOCAL_AHEAD",
                "behind" => "ORIGIN_AHEAD",
                _ => "DIVERGED",
            };
        return new JsonObject
        {
            ["state"] = state,
            ["local_main_sha"] = local,
            ["origin_main_sha"] = origin,
        };
    }

    /// <summary>record_convergence_evidence parity (§10.69-F①/D④).</summary>
    public static void RecordConvergence(
        string projectRoot, bool pushed = false)
    {
        var mainPath = Sync.ListWorktrees(projectRoot)
            .FirstOrDefault(w => BranchPolicy.IsMain(w.Branch))?.Path
            ?? projectRoot;
        var state = SyncState(mainPath);
        var (ahead, behind) = AheadBehind(mainPath);
        var inSync = state["state"]!.GetValue<string>() == "IN_SYNC"
                     && ahead == 0;
        var path = StatePath(projectRoot);
        var prior = ReadState(path);
        var streak = prior["convergence"]?["consecutive_in_sync"]
            ?.GetValue<int>() ?? 0;
        streak = inSync ? streak + 1 : 0;
        var now = Canon.UtcNow();
        var convergence = new JsonObject
        {
            ["state"] = state["state"]!.DeepClone(),
            ["ahead"] = ahead.HasValue ? ahead.Value : null,
            ["behind"] = behind.HasValue ? behind.Value : null,
            ["ahead_zero"] = inSync,
            ["consecutive_in_sync"] = streak,
            ["last_checked"] = now,
            ["local_main_sha"] = state["local_main_sha"]!.DeepClone(),
            ["origin_main_sha"] = state["origin_main_sha"]!.DeepClone(),
        };
        var record = ReadState(path);
        record["schema"] = "gptbridge-push-gate/v1";
        record["updated_at"] = now;
        record["convergence"] = convergence;
        WriteState(path, record);
        Ledger.Audit(projectRoot, 1, "sync-state origin/main", Sync.Actor,
            true,
            $"sync_state={state["state"]} ahead={ahead} behind={behind} " +
            $"streak={streak} pushed={pushed}",
            operation: "convergence-check", worktree: mainPath,
            phase: "result",
            result: inSync ? "in-sync"
                : state["state"]!.GetValue<string>().ToLowerInvariant(),
            returncode: inSync ? 0 : 1);
    }

    /// <summary>record_push_evidence parity (C④/F④).</summary>
    public static void RecordPushEvidence(
        string projectRoot, JsonObject? testGate, bool pushed,
        string detail = "")
    {
        var mainPath = Sync.ListWorktrees(projectRoot)
            .FirstOrDefault(w => BranchPolicy.IsMain(w.Branch))?.Path
            ?? projectRoot;
        var localSha = Git.RevParse(mainPath, "main");
        var originSha = Git.RevParse(mainPath, "origin/main");
        var gateSummary = "none";
        if (testGate is not null)
        {
            var totals = testGate["totals"];
            gateSummary =
                $"tests={(testGate["passed"]?.GetValue<bool>() == true ? "pass" : "fail")}" +
                $" skipped={testGate["skipped"]?.GetValue<bool>() ?? false}" +
                $" suites={(testGate["suites"] as JsonArray)?.Count ?? 0}" +
                $" pass={totals?["pass"]?.GetValue<int>() ?? 0}" +
                $" fail={totals?["fail"]?.GetValue<int>() ?? 0}" +
                $" blocked={totals?["blocked"]?.GetValue<int>() ?? 0}" +
                $" rebuilt={testGate["rebuilt"]?.GetValue<bool>() ?? false}" +
                $" ms={testGate["duration_ms"]?.GetValue<int>()}";
        }
        var now = Canon.UtcNow();
        var path = StatePath(projectRoot);
        var record = ReadState(path);
        record["schema"] = "gptbridge-push-gate/v1";
        record["updated_at"] = now;
        record["last_test_gate"] = testGate?.DeepClone();
        record["last_push"] = new JsonObject
        {
            ["at"] = now,
            ["result"] = pushed ? "pushed" : "denied",
            ["local_main_sha"] = localSha,
            ["origin_main_sha"] = originSha,
            ["test_gate"] = gateSummary,
            ["detail"] = detail[..Math.Min(300, detail.Length)],
        };
        WriteState(path, record);
        Ledger.Audit(projectRoot, 2, "push origin main", Sync.Actor,
            pushed,
            $"mandatory-test-gate[{gateSummary}] {detail}".Trim()
                [..Math.Min(500,
                    $"mandatory-test-gate[{gateSummary}] {detail}".Trim().Length)],
            operation: "push", worktree: mainPath, phase: "result",
            result: pushed ? "pushed" : "denied",
            returncode: pushed ? 0 : 1);
    }
}

/// <summary>release_checkpoint parity — durable merge evidence.</summary>
internal static class ReleaseCheckpoint
{
    public static JsonObject Record(
        string projectRoot, string mainPath, string auditResult = "",
        string actor = "governance/release")
    {
        var local = Git.RevParse(mainPath, "refs/heads/main");
        var origin = Git.RevParse(mainPath, "refs/remotes/origin/main");
        var timestamp = Canon.UtcNow();
        var checkpoint = new JsonObject
        {
            ["timestamp"] = timestamp,
            ["main_sha"] = local,
            ["origin_sha"] = origin,
            ["governance_audit"] = auditResult,
            ["audit_sequence"] = null,
            ["queue_id"] = "",
            ["sync_state"] = local.Length == 0 || origin.Length == 0
                ? "MISSING_REF"
                : local == origin ? "IN_SYNC" : "LOCAL_AHEAD",
        };
        var dir = Path.Combine(Git.CommonDir(projectRoot),
            "gptbridge-automation", "releases");
        Directory.CreateDirectory(dir);
        var name = timestamp.Replace(":", "") + "-" +
                   (local.Length > 0 ? local[..12] : "none");
        var path = Path.Combine(dir, name + ".json");
        using var document = JsonDocument.Parse(checkpoint.ToJsonString());
        Canon.WriteJsonAtomic(
            path, Canon.Indented(document.RootElement));
        checkpoint["path"] = path;
        return checkpoint;
    }
}
