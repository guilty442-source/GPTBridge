// CorpusRunner.cs — governed bridge from the C# control lane into the
// Rust data lane: ``xcorpus corpus`` (file corpus -> packed XCB1 +
// manifest) followed by ``xstore snapshot`` (content-addressed pin of
// the emitted output dir).
//
// Ownership: C# decides policy inputs (registry, tokenizer, limits) and
// registers evidence; Rust owns scanning, normalization, dedup, packing
// and persistence. C# never reimplements the corpus formats — the
// subprocess JSON receipts are the contract.

namespace GPTBridge.XingchengLearning;

internal static class CorpusRunner
{
    /// <summary>Run ``xcorpus corpus`` then pin the output directory with
    /// ``xstore snapshot``. Returns the combined governed result; any
    /// subprocess failure is a hard ExecutorError (fail-closed — a
    /// corpus that cannot be pinned is not registered).</summary>
    public static Dictionary<string, object?> Run(
        string toolRoot, IReadOnlyDictionary<string, string> opts)
    {
        string exe = NativeTools.RustExe(toolRoot, "xcorpus");
        if (exe.Length == 0)
            throw new ExecutorError("EXECUTOR_CORPUS_UNAVAILABLE",
                "xcorpus.exe missing under " +
                "src/backend/rust/xcorpus/target/release — " +
                "corpus processing is owned by the Rust lane");

        string Req(string k) => opts.TryGetValue(k, out string? v) &&
                                v.Length > 0
            ? v
            : throw new ExecutorError("RECOVERY_ARG_MISSING",
                $"--{k} required");

        string registry = Req("registry");
        string root = Req("root");
        string tokenizer = Req("tokenizer");
        // Corpus output + snapshot staging are xingcheng-owned data —
        // the export target must resolve inside the domain roots.
        string outDir = DataBoundary.AssertInside(toolRoot,
            Req("out"));
        string stderrLog = Path.Combine(
            toolRoot, "xingcheng", "runtime", "logs",
            "xcorpus-stderr.log");

        var args = new List<string>
        {
            "corpus",
            "--registry", registry,
            "--root", root,
            "--tokenizer", tokenizer,
            "--out", outDir,
        };
        foreach (var (k, dflt) in new[]
                 {
                     ("max-len", ""), ("val-ratio", ""),
                     ("max-docs", ""), ("max-doc-chars", ""),
                     ("max-tokens", ""), ("jobs", ""),
                 })
            if (opts.TryGetValue(k, out string? v) && v.Length > 0)
                args.AddRange(new[] { $"--{k}", v });
        if (opts.TryGetValue("policy", out string? pol) &&
            pol.Length > 0)
            args.AddRange(new[] { "--policy", pol });

        var run = NativeTools.Run(exe, args, toolRoot, stderrLog,
            timeoutS: 7200);
        if (run.ExitCode != 0)
            throw new ExecutorError("RECOVERY_CORPUS_FAILED",
                $"xcorpus corpus failed (exit {run.ExitCode}); " +
                $"see {stderrLog}");
        var summary = ParseSingle(run.StdoutTail, "corpus");

        // ── pin: the emitted directory becomes a content-addressed
        // snapshot in the Rust artifact store ──────────────────────
        string storeExe = NativeTools.RustExe(toolRoot, "xstore");
        Dictionary<string, object?> snapshot;
        if (storeExe.Length == 0)
        {
            snapshot = new Dictionary<string, object?>
            {
                ["status"] = "not-deployed",
            };
        }
        else
        {
            string name = $"corpus-{DateTime.UtcNow:yyyyMMdd-HHmmss}";
            var srun = NativeTools.Run(storeExe,
                new[] { "snapshot",
                        "--store", NativeTools.ArtifactStoreDir(toolRoot),
                        "--src", outDir,
                        "--name", name },
                toolRoot,
                Path.Combine(toolRoot, "xingcheng", "runtime", "logs",
                             "xstore-stderr.log"),
                timeoutS: 1800);
            if (srun.ExitCode != 0)
                throw new ExecutorError("EXECUTOR_STORE_PIN_FAILED",
                    $"xstore snapshot failed for {outDir}");
            snapshot = ParseSingle(srun.StdoutTail, "snapshot");
            snapshot["status"] = "pinned";
        }

        return new Dictionary<string, object?>
        {
            ["format"] = "star-corpus-run/v1",
            ["corpus"] = summary,
            ["snapshot"] = snapshot,
        };
    }

    private static Dictionary<string, object?> ParseSingle(
        string stdout, string tag)
    {
        // Rust tools emit exactly one JSON object on stdout.
        using var doc = System.Text.Json.JsonDocument.Parse(
            stdout.Trim());
        var decoded = ModelLifecycle.Decode(doc.RootElement)
            as Dictionary<string, object?>;
        return decoded ?? new Dictionary<string, object?>
        {
            ["raw"] = stdout.Trim(),
        };
    }
}
