using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal static partial class PushGate
{
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

}
