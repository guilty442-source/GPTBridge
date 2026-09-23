// C# orchestration for the native test suites (no Python).
// §10.60.1: TestSuiteOrchestrator is the SOLE native suite orchestrator.
//
// Modes:
//   (default)  aggregate <bin>/native-report.json → orchestration report
//   --run      orchestrate the suite fleet itself: bounded concurrency,
//              per-suite timeout, process cleanup, typed per-suite results,
//              consolidated native-report.json + artifact hash (SHA-256) +
//              source-revision verification against the build manifest.
//
// The suite list comes from the current build manifest
// (<bin>/suite-manifest.json, emitted by build.ps1); with --require-manifest
// a missing/malformed manifest fails closed instead of globbing.
//
// G99: every BLOCKED case is classified through
// <test_suites>/suite_criticality.json — a BLOCKED case on a
// release-critical suite (the default for unregistered suites) makes the
// verdict FAIL, matching push_gate.py; only suites registered
// "experimental" keep INCOMPLETE_EVIDENCE.  A missing/malformed registry
// with blocked cases is itself FAIL (fail-closed).
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

// G99: load the per-suite criticality registry that lives next to bin/.
static JsonObject? LoadCriticality(string binDir)
{
    var path = Path.GetFullPath(
        Path.Combine(binDir, "..", "suite_criticality.json"));
    try { return JsonNode.Parse(File.ReadAllText(path)) as JsonObject; }
    catch (Exception) { return null; }
}

// G99: project one BLOCKED case through the registry.  Unknown suites
// default to release-critical so unclassified evidence never stays
// non-blocking.
static string Trunc(string? s, int max = 200) =>
    s == null ? "" : (s.Length > max ? s[..max] : s);

static JsonObject ClassifyBlocked(string suiteStem, JsonNode? caseNode,
                                  JsonObject? registry)
{
    var suites = registry?["suites"] as JsonObject;
    var entry = suites?[suiteStem] as JsonObject;
    var defaults = registry?["defaults"] as JsonObject;
    string Field(JsonObject? e, string key, string fallback) =>
        e?[key]?.GetValue<string>() ?? fallback;
    return new JsonObject
    {
        ["blocked_suite"] = suiteStem,
        ["blocked_case"] = caseNode?["name"]?.GetValue<string>() ?? "?",
        ["blocked_reason"] = Trunc(
            caseNode?["detail"]?.GetValue<string>()),
        ["affected_capability"] = Field(entry, "affected_capability",
                                        "unregistered-suite"),
        ["release_impact"] = Field(entry, "release_impact", "unclassified"),
        ["required_evidence"] = Field(
            entry, "required_evidence",
            Field(defaults, "required_evidence", "suite PASS report")),
        ["criticality"] = Field(
            entry, "criticality",
            Field(defaults, "criticality", "release-critical")),
    };
}

if (args.Contains("--run"))
{
    return await RunSuites(args);
}
return AggregateReport(args);

static int AggregateReport(string[] args)
{
    var reportPath = args.Length > 0 && !args[0].StartsWith("--")
        ? args[0]
        : Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "bin", "native-report.json");
    reportPath = Path.GetFullPath(reportPath);

    if (!File.Exists(reportPath))
    {
        Console.Error.WriteLine($"native report not found: {reportPath}");
        return 2;
    }

    var cases = JsonNode.Parse(File.ReadAllText(reportPath))?["cases"]?.AsArray() ?? new JsonArray();
    var bySuite = cases
        .GroupBy(item => item?["suite"]?.GetValue<string>() ?? "UNKNOWN")
        .OrderBy(group => group.Key)
        .Select(group => new
        {
            suite = group.Key,
            pass = group.Count(item => item?["status"]?.GetValue<string>() == "PASS"),
            fail = group.Count(item => item?["status"]?.GetValue<string>() == "FAIL"),
            blocked = group.Count(item => item?["status"]?.GetValue<string>() == "BLOCKED"),
            total_ms = Math.Round(group.Sum(item => item?["ms"]?.GetValue<double>() ?? 0.0), 3),
        })
        .ToList();

    var failed = bySuite.Sum(item => item.fail);
    var blocked = bySuite.Sum(item => item.blocked);
    var passed = bySuite.Sum(item => item.pass);

    // G99: classify blocked cases; release-critical blocked → FAIL.
    var registry = LoadCriticality(Path.GetDirectoryName(reportPath)!);
    var blockedClassifications = new JsonArray();
    foreach (var c in cases)
    {
        if (c?["status"]?.GetValue<string>() != "BLOCKED") continue;
        var stem = c?["suite_exe"]?.GetValue<string>()
            ?? c?["suite"]?.GetValue<string>() ?? "UNKNOWN";
        blockedClassifications.Add(ClassifyBlocked(stem, c, registry));
    }
    var blockedUnclassified = blocked > 0
        && (registry?["suites"] as JsonObject) == null;
    var criticalBlocked = blockedClassifications.Count(item =>
        item?["criticality"]?.GetValue<string>() != "experimental");
    var deny = failed > 0 || blockedUnclassified || criticalBlocked > 0;

    var output = new JsonObject
    {
        ["orchestrator"] = "native-test-orchestrator/v1",
        ["language"] = "csharp",
        ["source_report"] = reportPath,
        ["generated_at"] = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"),
        ["suites"] = JsonSerializer.SerializeToNode(bySuite),
        ["passed"] = passed,
        ["failed"] = failed,
        ["blocked"] = blocked,
        ["blocked_classifications"] = blockedClassifications,
        ["blocked_release_critical"] = criticalBlocked,
        ["blocked_unclassified"] = blockedUnclassified,
        ["verdict"] = deny ? "FAIL"
            : (blocked > 0 ? "INCOMPLETE_EVIDENCE" : "PASS"),
    };

    var outPath = Path.Combine(Path.GetDirectoryName(reportPath)!, "native-orchestration-report.json");
    File.WriteAllText(outPath, output.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));

    Console.WriteLine($"suites={bySuite.Count} PASS={passed} FAIL={failed} BLOCKED={blocked} verdict={output["verdict"]}");
    foreach (var suite in bySuite)
    {
        Console.WriteLine($"  {suite.suite}: PASS={suite.pass} FAIL={suite.fail} BLOCKED={suite.blocked} ({suite.total_ms} ms)");
    }
    Console.WriteLine($"report: {outPath}");
    return deny ? 1 : 0;
}

static async Task<int> RunSuites(string[] args)
{
    var gateSw = Stopwatch.StartNew();  // G97: total_gate_time
    string Opt(string flag, string fallback)
    {
        var i = Array.IndexOf(args, flag);
        return i >= 0 && i + 1 < args.Length ? args[i + 1] : fallback;
    }

    var binDir = Path.GetFullPath(
        Opt("--bin", Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "bin")));
    var maxParallel = Math.Clamp(int.Parse(Opt("--max-parallel", "4")), 1, 8);
    var suiteTimeoutS = Math.Clamp(int.Parse(Opt("--suite-timeout-s", "300")), 1, 3600);
    var requireManifest = args.Contains("--require-manifest");

    // ---- suite list: current build manifest (dynamic; never hardcoded) ----
    var manifestPath = Path.Combine(binDir, "suite-manifest.json");
    var manifestSource = "manifest";
    string? builtRevision = null;
    var suites = new List<(string Name, string Exe)>();
    var expectedHashes = new Dictionary<string, string>(
        StringComparer.OrdinalIgnoreCase);
    JsonNode? manifest = null;
    try
    {
        manifest = JsonNode.Parse(File.ReadAllText(manifestPath));
        builtRevision = manifest?["revision"]?.GetValue<string>();
        foreach (var s in manifest?["suites"]?.AsArray() ?? new JsonArray())
        {
            var name = s?["name"]?.GetValue<string>();
            var exe = s?["exe"]?.GetValue<string>();
            var sha = s?["sha256"]?.GetValue<string>();
            if (!string.IsNullOrEmpty(name) && !string.IsNullOrEmpty(exe))
                suites.Add((name, exe));
            if (!string.IsNullOrEmpty(exe) && !string.IsNullOrEmpty(sha))
                expectedHashes[exe!] = sha!;
        }
    }
    catch (Exception) { manifest = null; }

    // G99 artifact freshness: bind each suite binary to the SHA-256 the
    // build recorded — content, not timestamps.  A stale/wrong-version
    // exe (rebuilt partially, swapped binary, drifted manifest) is never
    // executed for evidence; it is recorded as a stale artifact and the
    // run denies.
    var staleArtifacts = new List<string>();
    var exeHashes = new Dictionary<string, string?>(
        StringComparer.OrdinalIgnoreCase);
    if (manifestSource == "manifest")
    {
        foreach (var (name, exe) in suites)
        {
            var exePath = Path.Combine(binDir, exe);
            string? actual = null;
            try
            {
                actual = Convert.ToHexString(
                    SHA256.HashData(await File.ReadAllBytesAsync(exePath)))
                    .ToLowerInvariant();
            }
            catch (Exception) { actual = null; }
            exeHashes[exe] = actual;
            if (!expectedHashes.TryGetValue(exe, out var expected)
                || actual == null
                || !string.Equals(actual, expected,
                    StringComparison.OrdinalIgnoreCase))
                staleArtifacts.Add($"{name}:{exe}");
        }
    }
    if (suites.Count == 0)
    {
        if (requireManifest)
        {
            Console.Error.WriteLine($"suite-manifest.json missing/malformed at {manifestPath} (--require-manifest)");
            return 2;
        }
        manifestSource = "glob-fallback";
        foreach (var p in Directory.GetFiles(binDir, "*_suite.exe").OrderBy(p => p))
        {
            var stem = Path.GetFileNameWithoutExtension(p);
            if (!stem.StartsWith("_"))
                suites.Add((stem, Path.GetFileName(p)));
        }
    }

    // ---- bounded-parallel execution ----
    var staleNames = new HashSet<string>(
        staleArtifacts.Select(a => a.Split(':', 2)[0]));
    var semaphore = new SemaphoreSlim(maxParallel);
    var tasks = suites
        .Where(s => !staleNames.Contains(s.Name))
        .Select(async suite =>
    {
        await semaphore.WaitAsync();
        try
        {
        var exePath = Path.Combine(binDir, suite.Exe);
        var jsonPath = Path.Combine(binDir, suite.Name + ".json");
        var sw = Stopwatch.StartNew();
        var timedOut = false;
        int? rc = null;
        try
        {
            try { File.Delete(jsonPath); } catch (IOException) { }
            // Streams are NOT redirected: a suite may spawn grandchildren
            // that inherit the pipe and keep it open forever — draining
            // would hang the orchestrator.  Inheriting console output is
            // also what build.ps1 does.
            var psi = new ProcessStartInfo(exePath, suite.Name)
            {
                WorkingDirectory = binDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
            };
            using var proc = Process.Start(psi)!;
            // Close stdin immediately: a suite that reads the console would
            // otherwise block on the orchestrator's inherited pipe until the
            // suite timeout.  EOF is the correct non-interactive contract.
            proc.StandardInput.Dispose();
            try
            {
                await proc.WaitForExitAsync().WaitAsync(
                    TimeSpan.FromSeconds(suiteTimeoutS));
            }
            catch (TimeoutException)
            {
                timedOut = true;
                try { proc.Kill(entireProcessTree: true); } catch (Exception) { }
            }
            if (!timedOut) rc = proc.ExitCode;
        }
        catch (Exception)
        {
            timedOut = false;
            rc = null; // spawn error → report-missing path below
        }
        sw.Stop();

        if (timedOut)
        {
            // Keys match the suite report schema exactly (passed/failed/
            // blocked) — a timeout must surface as a counted failure,
            // never a zero-counted report.
            File.WriteAllText(jsonPath,
                "{ \"suite\": \"" + suite.Name + "\", \"cases\": [ { \"name\": " +
                "\"suite_timeout\", \"status\": \"FAIL\", \"detail\": \"exceeded " +
                suiteTimeoutS + "s\" } ], \"passed\": 0, \"failed\": 1, \"blocked\": 0 }");
        }

        // G97: result collection is timed separately so the gate can
        // decompose suite wall time into process startup, actual test
        // execution and report collection.
        var collectSw = Stopwatch.StartNew();
        JsonNode? report = null;
        try { report = JsonNode.Parse(File.ReadAllText(jsonPath)); }
        catch (Exception) { report = null; }
        collectSw.Stop();
        var cases = report?["cases"]?.AsArray();
        if (cases == null)
        {
            var detail = timedOut ? $"timeout>{suiteTimeoutS}s"
                : rc == null ? "spawn-error"
                : $"report-missing:rc={rc}";
            cases = new JsonArray(new JsonObject
            {
                ["suite"] = suite.Name,
                ["name"] = "suite_execution",
                ["status"] = "FAIL",
                ["detail"] = detail,
            });
            report = new JsonObject
            {
                ["cases"] = cases, ["passed"] = 0,
                ["failed"] = 1, ["blocked"] = 0,
            };
        }
        var testMs = cases?
            .Sum(c => c?["ms"]?.GetValue<double>() ?? 0.0) ?? 0.0;
        var startupMs = Math.Max(
            0.0, sw.Elapsed.TotalMilliseconds
                - testMs - collectSw.Elapsed.TotalMilliseconds);
        return new
        {
            suite = suite.Name,
            exe = suite.Exe,
            pass = report?["passed"]?.GetValue<int>() ?? 0,
            fail = report?["failed"]?.GetValue<int>() ?? 0,
            blocked = report?["blocked"]?.GetValue<int>() ?? 0,
            returncode = rc,
            timed_out = timedOut,
            duration_ms = sw.ElapsedMilliseconds,
            process_startup_ms = Math.Round(startupMs, 3),
            actual_test_ms = Math.Round(testMs, 3),
            result_collection_ms =
                Math.Round(collectSw.Elapsed.TotalMilliseconds, 3),
            cases,
        };
        }
        finally
        {
            semaphore.Release();
        }
    }).ToList();
    var results = (await Task.WhenAll(tasks)).ToList();
    // Stale artifacts never ran: emit a counted FAIL row so a skipped
    // binary can never read as absent evidence.
    foreach (var (name, exe) in suites.Where(s => staleNames.Contains(s.Name)))
    {
        results.Add(new
        {
            suite = name,
            exe,
            pass = 0,
            fail = 1,
            blocked = 0,
            returncode = (int?)null,
            timed_out = false,
            duration_ms = 0L,
            process_startup_ms = 0.0,
            actual_test_ms = 0.0,
            result_collection_ms = 0.0,
            cases = (JsonArray?)new JsonArray(new JsonObject
            {
                ["suite"] = name,
                ["name"] = "stale_artifact",
                ["status"] = "FAIL",
                ["detail"] =
                    "exe sha256 differs from suite-manifest.json record",
            }),
        });
    }
    results = results.OrderBy(r => r.suite).ToList();
    File.AppendAllText(Path.Combine(binDir, "_orch_progress.log"),
        $"{DateTime.UtcNow:HH:mm:ss.fff} all-suites-done\n");

    // ---- consolidated report (same schema the Python gate consumes) ----
    // G99: tag each case with its owning suite stem so downstream
    // classification can key on the manifest/exe name rather than the
    // free-form "suite" constant inside the case.
    var allCases = new JsonArray();
    foreach (var r in results)
        foreach (var c in r.cases!)
        {
            var clone = c?.DeepClone();
            if (clone is JsonObject co) co["suite_exe"] = r.suite;
            allCases.Add(clone);
        }
    var reportOut = new JsonObject
    {
        ["harness"] = "native-test-suite/v1",
        ["cases"] = allCases,
    };
    var nativeReportPath = Path.Combine(binDir, "native-report.json");
    var nativeJson = reportOut.ToJsonString();
    File.WriteAllText(nativeReportPath, nativeJson);
    File.AppendAllText(Path.Combine(binDir, "_orch_progress.log"),
        $"{DateTime.UtcNow:HH:mm:ss.fff} native-report-written\n");
    var artifactHash = Convert.ToHexString(
        SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(nativeJson)))
        .ToLowerInvariant();

    // ---- source-revision verification ----
    string? currentRevision = null;
    try
    {
        var gitPsi = new ProcessStartInfo("git", "rev-parse HEAD")
        {
            WorkingDirectory = Path.GetFullPath(
                Path.Combine(binDir, "..", "..", "..")),
            RedirectStandardOutput = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        using var git = Process.Start(gitPsi)!;
        git.StandardInput.Dispose();
        var gitOut = git.StandardOutput.ReadToEndAsync();
        var exited = await git.WaitForExitAsync()
            .WaitAsync(TimeSpan.FromSeconds(10))
            .ContinueWith(t => t.Status == TaskStatus.RanToCompletion);
        if (exited)
            currentRevision = (await gitOut).Trim();
        else
        {
            try { git.Kill(entireProcessTree: true); } catch (Exception) { }
            currentRevision = null;
        }
    }
    catch (Exception) { currentRevision = null; }

    var failed = results.Sum(r => r.fail);
    var blocked = results.Sum(r => r.blocked);
    var passed = results.Sum(r => r.pass);

    // G99: classify every BLOCKED case through suite_criticality.json;
    // release-critical blocked (or a missing registry) flips the verdict
    // to FAIL, identical to push_gate.py.
    var registry = LoadCriticality(binDir);
    var blockedClassifications = new JsonArray();
    foreach (var r in results)
        foreach (var c in r.cases!)
            if (c?["status"]?.GetValue<string>() == "BLOCKED")
                blockedClassifications.Add(
                    ClassifyBlocked(r.suite, c, registry));
    var blockedUnclassified = blocked > 0
        && (registry?["suites"] as JsonObject) == null;
    var criticalBlocked = blockedClassifications.Count(item =>
        item?["criticality"]?.GetValue<string>() != "experimental");
    // A suite exiting with an unexpected code is a failure in its own
    // right even when its case report parses clean (parity with the
    // Python gate's ``suite_exit`` rule).
    var suiteExitFailures = results.Count(
        r => r.returncode is int c && c != 0 && c != 1);
    var deny = failed > 0 || suiteExitFailures > 0
        || blockedUnclassified || criticalBlocked > 0
        || staleArtifacts.Count > 0;

    // G97: surface the first failed cases so the consuming gate can deny
    // with an actionable detail without re-reading every suite report.
    var failures = new JsonArray();
    foreach (var r in results)
    {
        foreach (var c in r.cases!)
        {
            if (c?["status"]?.GetValue<string>() == "BLOCKED") continue;
            if (c?["status"]?.GetValue<string>() == "PASS") continue;
            if (failures.Count >= 8) break;
            failures.Add(
                $"{r.suite}/{c?["name"]?.GetValue<string>() ?? "?"}: " +
                Trunc(c?["detail"]?.GetValue<string>(), 120));
        }
        if (r.returncode is int code && code != 0 && code != 1
            && failures.Count < 8)
            failures.Add($"{r.suite}/suite_exit: rc={code}");
    }

    gateSw.Stop();
    // G97: the mandated gate-time decomposition — total gate wall time,
    // per-suite process startup overhead, actual in-test execution time
    // and result-collection time.  Startup share decides whether suites
    // may be merged (evidence-gated, never assumed).
    var timing = new JsonObject
    {
        ["total_gate_time_ms"] = Math.Round(gateSw.Elapsed.TotalMilliseconds, 3),
        ["process_startup_time_ms"] =
            Math.Round(results.Sum(r => r.process_startup_ms), 3),
        ["actual_test_time_ms"] =
            Math.Round(results.Sum(r => r.actual_test_ms), 3),
        ["result_collection_time_ms"] =
            Math.Round(results.Sum(r => r.result_collection_ms), 3),
    };

    var orchReport = new JsonObject
    {
        ["orchestrator"] = "native-test-orchestrator/v2",
        ["language"] = "csharp",
        ["mode"] = "run",
        ["generated_at"] = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"),
        ["bin_dir"] = binDir,
        ["manifest_source"] = manifestSource,
        ["max_parallel"] = maxParallel,
        ["suite_timeout_s"] = suiteTimeoutS,
        ["source_revision"] = currentRevision,
        ["manifest_built_revision"] = builtRevision,
        ["revision_match"] = builtRevision == null ? (JsonNode?)null
            : JsonValue.Create(builtRevision == currentRevision),
        ["artifact_hash"] = artifactHash,
        ["stale_artifacts"] = JsonSerializer.SerializeToNode(staleArtifacts),
        ["suite_manifest_sha256"] = manifestSource == "manifest"
            ? Convert.ToHexString(SHA256.HashData(
                File.ReadAllBytes(manifestPath))).ToLowerInvariant()
            : null,
        ["timing"] = timing,
        ["suites"] = JsonSerializer.SerializeToNode(results.Select(r => new
        {
            r.suite, r.exe, r.pass, r.fail, r.blocked,
            r.returncode, r.timed_out, r.duration_ms,
            r.process_startup_ms, r.actual_test_ms,
            r.result_collection_ms,
            exe_sha256 = exeHashes.TryGetValue(r.exe, out var h) ? h : null,
        }).ToList()),
        ["passed"] = passed,
        ["failed"] = failed + suiteExitFailures,
        ["blocked"] = blocked,
        ["cases"] = allCases.Count,
        ["failures"] = failures,
        ["blocked_classifications"] = blockedClassifications,
        ["blocked_release_critical"] = criticalBlocked,
        ["blocked_unclassified"] = blockedUnclassified,
        ["verdict"] = deny ? "FAIL"
            : (blocked > 0 ? "INCOMPLETE_EVIDENCE" : "PASS"),
    };
    var orchPath = Path.Combine(binDir, "native-orchestration-report.json");
    File.WriteAllText(orchPath,
        orchReport.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));

    Console.WriteLine(
        $"run: suites={results.Count} parallel={maxParallel} " +
        $"PASS={passed} FAIL={failed} BLOCKED={blocked} " +
        $"verdict={orchReport["verdict"]} hash={artifactHash[..12]}");
    foreach (var r in results)
        Console.WriteLine(
            $"  {r.suite}: PASS={r.pass} FAIL={r.fail} BLOCKED={r.blocked} " +
            $"rc={r.returncode?.ToString() ?? "-"} {r.duration_ms}ms" +
            (r.timed_out ? " TIMEOUT" : ""));
    Console.WriteLine($"report: {nativeReportPath}");
    Console.WriteLine($"orchestration: {orchPath}");
    return deny ? 1 : 0;
}
