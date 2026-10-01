// Evaluation.cs — governed adapter evaluation lane.
//
// Ports run_evaluation (native_eval_suite + capability_eval) onto the
// native xc_modeltool subprocesses instead of the retired Python
// evaluators:
//
//   star-native-eval-suite/v1  -> xc_modeltool eval
//       --bundle <cand> --suite <suite> [--baseline-bundle <base>]
//       stdout JSON: {passed, candidate, baseline, comparison}
//       exit 0 = pass, 2 = gate failed, 1 = engine/tool error
//
//   star-capability-suite/v1   -> xc_modeltool capability
//       baseline evaluated first into a report file, then
//       --baseline-report <file> on the candidate run yields
//       {passed, report, comparison} with the per-category
//       regression gate (CATEGORIES list, pass_rate must not drop).
//
// Every evaluation is recorded through the repository's governed path
// (transformer_adapter_evaluation + audit event); a tool/subprocess
// failure records passed=0 rather than aborting the cycle silently —
// matching the Python lane where suite execution errors produced a
// failed evaluation row, not a crash.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class Evaluation
{
    public const string NativeEvalFormat = "star-native-eval-suite/v1";
    public const string CapabilityFormat = "star-capability-suite/v1";

    /// <summary>Resolve an artifact reference to a bundle directory.
    /// Accepts a bundle dir or its manifest.json; anything else fails
    /// closed (retired .pt lineage is not evaluable on the native engine).</summary>
    public static string BundleDirOf(string artifactPath)
    {
        if (Directory.Exists(artifactPath))
            return Path.GetFullPath(artifactPath);
        if (File.Exists(artifactPath) &&
            Path.GetFileName(artifactPath) == "manifest.json")
            return Path.GetFullPath(Path.GetDirectoryName(artifactPath)!);
        throw new ExecutorError("EVAL_ARTIFACT_UNSUPPORTED",
            $"artifact is not a native bundle: {artifactPath}");
    }

    private static string SuiteId(Dictionary<string, object?> suite, string path)
        => TransformerTrainingRepository.Str(suite, "suite_id")
           ?? Path.GetFileNameWithoutExtension(path);

    private static Dictionary<string, object?> LoadSuite(string suitePath)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(suitePath));
        if (doc.RootElement.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("EVAL_SUITE_INVALID", suitePath);
        var map = new Dictionary<string, object?>();
        foreach (var p in doc.RootElement.EnumerateObject())
            map[p.Name] = ModelLifecycle.Decode(p.Value);
        return map;
    }

    private static string SuiteFormat(Dictionary<string, object?> suite)
        => TransformerTrainingRepository.Str(suite, "format_version") ?? "";

    private static Dictionary<string, object?> ParseStdoutJson(
        NativeTools.RunResult run, string code)
    {
        string tail = run.StdoutTail.Trim();
        int start = tail.IndexOf('{');
        if (start < 0)
            throw new ExecutorError(code,
                $"tool produced no JSON (exit {run.ExitCode})");
        using var doc = JsonDocument.Parse(tail[start..]);
        var map = new Dictionary<string, object?>();
        foreach (var p in doc.RootElement.EnumerateObject())
            map[p.Name] = ModelLifecycle.Decode(p.Value);
        return map;
    }

    private static Dictionary<string, object?> Child(
        IReadOnlyDictionary<string, object?> map, string key)
    {
        return map.TryGetValue(key, out object? v) &&
               v is Dictionary<string, object?> d
            ? d : new Dictionary<string, object?>();
    }

    /// <summary>Evaluate a candidate bundle against one suite and record
    /// the result in the governed repository. Returns
    /// {ok, passed, comparison, evaluation|error}.</summary>
    public static Dictionary<string, object?> RunEvaluation(
        TransformerTrainingRepository repo,
        string adapterId,
        string candidateArtifact,
        string suitePath,
        string? baselineArtifact,
        string evaluatedBy,
        string? corpusManifest = null,
        bool chat = false)
    {
        string stderrLog = Path.Combine(
            repo.ToolRoot, "runtime", "logs",
            $"eval-stderr-{Environment.ProcessId}-{Guid.NewGuid():N}.log");
        Dictionary<string, object?> suite;
        try
        {
            suite = LoadSuite(suitePath);
        }
        catch (Exception ex)
        {
            return Fail(repo, adapterId, Path.GetFileNameWithoutExtension(suitePath),
                        $"suite-unreadable:{ex.Message}", evaluatedBy);
        }
        string suiteId = SuiteId(suite, suitePath);
        string format = SuiteFormat(suite);
        var gates = Child(suite, "quality_gates");

        string candidateBundle, baselineBundle;
        try
        {
            candidateBundle = BundleDirOf(candidateArtifact);
            baselineBundle = baselineArtifact != null
                ? BundleDirOf(baselineArtifact) : "";
        }
        catch (ExecutorError ex)
        {
            return Fail(repo, adapterId, suiteId, ex.Message, evaluatedBy);
        }

        bool passed;
        Dictionary<string, object?> adapterMetrics;
        Dictionary<string, object?> baselineMetrics;
        Dictionary<string, object?> comparison;
        string suiteSha = TransformerTrainingRepository.Sha256File(suitePath);
        try
        {
            if (format == CapabilityFormat)
            {
                // Baseline report first (live evaluation against the active
                // bundle), then the candidate with --baseline-report so the
                // per-category regression gate runs inside the tool.
                string? baselineReportPath = null;
                baselineMetrics = new Dictionary<string, object?>();
                if (baselineBundle.Length > 0)
                {
                    baselineReportPath = Path.Combine(
                        Path.GetTempPath(),
                        $"xc-cap-base-{Guid.NewGuid():N}.json");
                    var baseRun = NativeTools.Run(
                        NativeTools.ModelToolExe(repo.ToolRoot),
                        new[] { "capability", "--bundle", baselineBundle,
                                "--suite", suitePath },
                        repo.ToolRoot, stderrLog, timeoutS: 7200);
                    var baseOut = ParseStdoutJson(
                        baseRun, "EVAL_TOOL_FAILED");
                    File.WriteAllText(
                        baselineReportPath,
                        CanonicalJson.PlainDict(
                            Child(baseOut, "report")),
                        new System.Text.UTF8Encoding(false));
                    baselineMetrics = Child(baseOut, "report");
                }
                try
                {
                    var args = new List<string>
                    {
                        "capability", "--bundle", candidateBundle,
                        "--suite", suitePath,
                    };
                    if (baselineReportPath != null)
                        args.AddRange(new[] { "--baseline-report", baselineReportPath });
                    if (corpusManifest != null)
                        args.AddRange(new[] { "--corpus-manifest", corpusManifest });
                    if (chat) args.Add("--chat");
                    var run = NativeTools.Run(
                        NativeTools.ModelToolExe(repo.ToolRoot), args,
                        repo.ToolRoot, stderrLog, timeoutS: 7200);
                    var output = ParseStdoutJson(run, "EVAL_TOOL_FAILED");
                    passed = run.ExitCode == 0 &&
                             TransformerTrainingRepository
                                 .Truthy(output.GetValueOrDefault("passed"));
                    var report = Child(output, "report");
                    adapterMetrics = report;
                    comparison = Child(output, "comparison");
                    if (baselineMetrics.Count == 0)
                        baselineMetrics = new Dictionary<string, object?>();
                }
                finally
                {
                    if (baselineReportPath != null)
                        try { File.Delete(baselineReportPath); } catch { }
                }
            }
            else if (format == NativeEvalFormat || format == "")
            {
                var args = new List<string>
                {
                    "eval", "--bundle", candidateBundle,
                    "--suite", suitePath,
                };
                if (baselineBundle.Length > 0)
                    args.AddRange(new[] { "--baseline-bundle", baselineBundle });
                var run = NativeTools.Run(
                    NativeTools.ModelToolExe(repo.ToolRoot), args,
                    repo.ToolRoot, stderrLog, timeoutS: 7200);
                var output = ParseStdoutJson(run, "EVAL_TOOL_FAILED");
                if (run.ExitCode != 0 && run.ExitCode != 2)
                    throw new ExecutorError("EVAL_TOOL_FAILED",
                        $"modeltool eval exited {run.ExitCode}");
                passed = TransformerTrainingRepository
                    .Truthy(output.GetValueOrDefault("passed"));
                adapterMetrics = Child(output, "candidate");
                baselineMetrics = Child(output, "baseline");
                comparison = Child(output, "comparison");
            }
            else
            {
                return Fail(repo, adapterId, suiteId,
                            $"unknown-suite-format:{format}", evaluatedBy);
            }
        }
        catch (ExecutorError ex)
        {
            return Fail(repo, adapterId, suiteId, ex.Message, evaluatedBy);
        }

        var evaluation = repo.RecordAdapterEvaluation(
            adapterId, suiteId,
            baselineMetrics: baselineMetrics,
            adapterMetrics: adapterMetrics,
            comparison: comparison,
            qualityGates: gates,
            passed: passed,
            evaluatedBy: evaluatedBy,
            suiteSha256: suiteSha);
        if (!passed)
            RecordFailures(repo.ToolRoot, suiteId, candidateBundle,
                           adapterMetrics);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["passed"] = passed,
            ["suite"] = suiteId,
            ["format"] = format,
            ["comparison"] = comparison,
            ["evaluation"] = evaluation,
        };
    }

    /// <summary>Every failed suite run feeds the capability failure
    /// pool — per category where the report carries them, one generic
    /// record otherwise. Recording never throws (bounded pool).</summary>
    private static void RecordFailures(
        string toolRoot, string suiteId, string candidateBundle,
        Dictionary<string, object?> adapterMetrics)
    {
        string generation = "";
        try
        {
            string mp = Path.Combine(candidateBundle, "manifest.json");
            if (File.Exists(mp))
            {
                using var doc =
                    System.Text.Json.JsonDocument.Parse(
                        File.ReadAllText(mp));
                if (doc.RootElement.TryGetProperty(
                        "architecture_generation", out var g))
                    generation = g.GetString() ?? "";
            }
        }
        catch (Exception) { }
        var cats = Child(adapterMetrics, "categories");
        int recorded = 0;
        foreach (var kv in cats)
        {
            if (kv.Value is not Dictionary<string, object?> c) continue;
            double rate = 1.0;
            if (c.TryGetValue("pass_rate", out var pr))
                rate = Convert.ToDouble(pr);
            else if (c.TryGetValue("passed", out var p) &&
                     c.TryGetValue("evaluated", out var e) &&
                     Convert.ToDouble(e) > 0)
                rate = Convert.ToDouble(p) / Convert.ToDouble(e);
            if (rate >= 1.0) continue;
            FailurePool.Record(
                toolRoot, $"suite:{suiteId}/{kv.Key}", generation,
                FailurePool.ClassForSuite(kv.Key), "category pass",
                $"pass_rate={rate:0.###}", candidateBundle,
                "medium", reproducible: true);
            ++recorded;
        }
        if (recorded == 0)
            FailurePool.Record(
                toolRoot, $"suite:{suiteId}", generation,
                FailurePool.ClassForSuite(suiteId), "suite pass",
                "SUITE_FAILED", candidateBundle,
                "medium", reproducible: true);
    }

    private static Dictionary<string, object?> Fail(
        TransformerTrainingRepository repo,
        string adapterId, string suiteId, string error, string evaluatedBy)
    {
        var empty = new Dictionary<string, object?>();
        var evaluation = repo.RecordAdapterEvaluation(
            adapterId, suiteId,
            baselineMetrics: empty, adapterMetrics: empty,
            comparison: new Dictionary<string, object?>
            {
                ["passed"] = false,
                ["regressions"] = new List<object?>(),
                ["error"] = error,
            },
            qualityGates: empty,
            passed: false,
            evaluatedBy: evaluatedBy);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["passed"] = false,
            ["suite"] = suiteId,
            ["error"] = error,
            ["evaluation"] = evaluation,
        };
    }
}
