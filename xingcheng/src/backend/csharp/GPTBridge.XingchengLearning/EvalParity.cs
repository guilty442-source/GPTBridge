// EvalParity.cs — the retired F# evaluator's only remaining surface:
// parity verification against the C# verdict authority
// (F# retirement, human-governor directive 2026-10-03).
//
// xc-eval.exe may run ONLY through VerdictParity here — it never
// decides a production verdict, gains no new duties, and a missing
// executable reports unavailable instead of failing any production
// path. The single production verdict authority is EvalVerdict.cs
// (star-csharp-eval-verdict/v1); this file exists so the retirement
// stays auditable, not to keep a second evaluator alive.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class EvalParity
{
    /// <summary>Resolve the retired F# evaluation-verdict executable —
    /// parity-verification surface only. Missing → EVAL_OWNER_UNAVAILABLE
    /// inside the parity path; a production verdict never reaches here.</summary>
    private static string XcEvalExe(string execRoot)
    {
        string path = Path.Combine(
            execRoot, "src", "backend", "fsharp",
            "GPTBridge.XingchengEval", "publish", "xc-eval.exe");
        if (!File.Exists(path))
            throw new ExecutorError(
                "EVAL_OWNER_UNAVAILABLE",
                $"xc-eval.exe missing: {path}");
        return path;
    }

    /// <summary>Ask the retired F# evaluator for its verdict on the
    /// measurements — parity-verification surface only (VerdictParity).
    /// Never called from a production verdict path.</summary>
    private static Dictionary<string, object?> FsharpVerdict(
        string execRoot, TransformerTrainingRepository repo,
        string format, Dictionary<string, object?> gates,
        Dictionary<string, object?> adapterMetrics,
        Dictionary<string, object?> baselineMetrics,
        Dictionary<string, object?> engineComparison,
        bool enginePassed,
        string stderrLog, out bool passed)
    {
        var input = new Dictionary<string, object?>
        {
            ["format"] = format,
            ["quality_gates"] = gates,
            ["candidate"] = adapterMetrics,
            ["baseline"] = baselineMetrics,
            ["engine_comparison"] = engineComparison,
        };
        string inputPath = Path.Combine(
            Path.GetTempPath(), $"xc-eval-in-{Guid.NewGuid():N}.json");
        File.WriteAllText(inputPath, CanonicalJson.PlainDict(input),
                          new System.Text.UTF8Encoding(false));
        try
        {
            var run = NativeTools.Run(
                XcEvalExe(execRoot),
                new[] { "gate", "--input", inputPath },
                repo.ToolRoot, stderrLog, timeoutS: 120);
            var verdict = Evaluation.ParseStdoutJson(
                run, "EVAL_VERDICT_FAILED");
            if (run.ExitCode == 1 ||
                !TransformerTrainingRepository.Truthy(
                    verdict.GetValueOrDefault("ok")))
                throw new ExecutorError("EVAL_VERDICT_FAILED",
                    $"xc-eval exited {run.ExitCode}");
            passed = TransformerTrainingRepository.Truthy(
                verdict.GetValueOrDefault("passed"));
            var cmp = Evaluation.Child(verdict, "comparison");
            cmp["engine_passed"] = enginePassed;
            cmp["engine_comparison"] = engineComparison;
            return cmp;
        }
        finally
        {
            try { File.Delete(inputPath); } catch { }
        }
    }

    /// <summary>Parity-verification surface (F# retirement): the
    /// retired F# evaluator's only remaining role. With a fixture file
    /// ({format, quality_gates, candidate, baseline}) both verdicts
    /// re-run on it and agreement is reported; without one, every
    /// recorded adapter_evaluation verdict pair in the store is
    /// scanned and the agreement distribution is emitted as
    /// star-eval-verdict-parity-report/v1.</summary>
    public static Dictionary<string, object?> VerdictParity(
        TransformerTrainingRepository repo, string? fixturePath)
    {
        if (fixturePath != null)
            return FixtureParity(repo, fixturePath);

        int pairs = 0, agree = 0, disagree = 0, csharpOwned = 0;
        var disagreements = new List<object?>();
        foreach (var rec in repo.Meta().Query(
                     NativeMetadataClient.Types.Evaluation, null,
                     1000000))
        {
            if (!rec.TryGetValue("comparison_json", out object? cjv) ||
                cjv is not string cjs || cjs.Length == 0)
                continue;
            Dictionary<string, object?>? cmp;
            try
            {
                cmp = ModelLifecycle.Decode(
                    JsonDocument.Parse(cjs).RootElement)
                    as Dictionary<string, object?>;
            }
            catch (Exception) { cmp = null; }
            if (cmp == null) continue;
            if ("csharp".Equals(TransformerTrainingRepository.Str(
                    cmp, "verdict_owner")))
                ++csharpOwned;
            if (!cmp.TryGetValue("csharp_parity", out object? pv) ||
                pv is not Dictionary<string, object?> p)
                continue;
            ++pairs;
            if (TransformerTrainingRepository.Truthy(
                    p.GetValueOrDefault("agree")))
                ++agree;
            else
            {
                ++disagree;
                disagreements.Add(new Dictionary<string, object?>
                {
                    ["record_id"] = rec.GetValueOrDefault("record_id"),
                    ["parity"] = p,
                });
            }
        }
        return new Dictionary<string, object?>
        {
            ["format"] = "star-eval-verdict-parity-report/v1",
            ["ok"] = disagree == 0,
            ["verdict_pairs"] = pairs,
            ["agree"] = agree,
            ["disagree"] = disagree,
            ["csharp_owned_verdicts"] = csharpOwned,
            ["verdict_authority"] = "csharp",
            ["disagreements"] = disagreements,
        };
    }

    /// <summary>Single-fixture parity check: re-derive both verdicts on
    /// one recorded measurement set. A missing/retired F# executable
    /// reports unavailable — parity verification never fails closed
    /// onto the production path it no longer belongs to.</summary>
    private static Dictionary<string, object?> FixtureParity(
        TransformerTrainingRepository repo, string fixturePath)
    {
        var fixture = Evaluation.LoadSuite(fixturePath);
        string format = TransformerTrainingRepository.Str(
                            fixture, "format")
                        ?? Evaluation.SuiteFormat(fixture);
        var gates = Evaluation.Child(fixture, "quality_gates");
        var candidate = Evaluation.Child(fixture, "candidate");
        var baseline = Evaluation.Child(fixture, "baseline");
        var engineCmp = Evaluation.Child(fixture, "engine_comparison");
        string execRoot = Path.GetDirectoryName(repo.ToolRoot)!;
        string stderrLog = Path.Combine(
            repo.ToolRoot, "runtime", "logs",
            $"eval-parity-stderr-{Environment.ProcessId}-" +
            $"{Guid.NewGuid():N}.log");
        try
        {
            var fsharp = FsharpVerdict(
                execRoot, repo, format, gates, candidate, baseline,
                engineCmp, enginePassed: false, stderrLog,
                out bool fsharpPassed);
            var parity = EvalVerdict.Parity(
                format, gates, candidate, baseline, fsharpPassed);
            return new Dictionary<string, object?>
            {
                ["format"] = "star-eval-verdict-parity-report/v1",
                ["ok"] = true,
                ["suite_format"] = format,
                ["fsharp_comparison"] = fsharp,
                ["parity"] = parity,
                ["agree"] = parity.GetValueOrDefault("agree"),
            };
        }
        catch (ExecutorError ex)
        {
            return new Dictionary<string, object?>
            {
                ["format"] = "star-eval-verdict-parity-report/v1",
                ["ok"] = false,
                ["suite_format"] = format,
                ["error"] = ex.Message,
                ["fsharp"] = "unavailable",
            };
        }
    }
}
