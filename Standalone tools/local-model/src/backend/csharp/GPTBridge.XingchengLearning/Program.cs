// Program.cs — governed entry points for the xingcheng learning lane.
//
// CLI surface preserves the retired Python module's contract:
//   --status              policy + state + training-window snapshot
//   --run-once [--force]  one governed self-learning cycle
//   --enable / --disable  policy kill switch (fail-closed when off)
//   --retention           dry-run sweep (default) | --apply | --status
//   --run-jobs [N]        drain queued governed training jobs
//   --job <id>            run one specific job
//   --verify-audit        repository audit-chain verification
//   --db-status           repository/schema status
//   --migrate             ensure the training repository schema
//
// All output is JSON on stdout (same contract as the Python lane); exit
// code is 0 unless the top-level result carries ok=false.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class Program
{
    private static int Main(string[] args)
    {
        var opts = new Dictionary<string, string>(StringComparer.Ordinal);
        var flags = new HashSet<string>(StringComparer.Ordinal);
        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            if (!arg.StartsWith("--", StringComparison.Ordinal))
                return Usage();
            string key = arg[2..];
            if (i + 1 < args.Length && !args[i + 1].StartsWith("--"))
                opts[key] = args[++i];
            else
                flags.Add(key);
        }

        string toolRoot = opts.TryGetValue("tool-root", out string? tr) &&
                          tr.Length > 0
            ? Path.GetFullPath(tr)
            : InferToolRoot();

        try
        {
            if (flags.Contains("status") && !flags.Contains("retention"))
                return Emit(Status(toolRoot));
            if (flags.Contains("run-once"))
                return Emit(SelfLearning.RunCycle(
                    toolRoot, force: flags.Contains("force")));
            if (flags.Contains("enable") || flags.Contains("disable"))
                return Emit(SetEnabled(toolRoot, flags.Contains("enable")));
            if (flags.Contains("retention"))
                return Emit(flags.Contains("status")
                    ? RetentionStatus(toolRoot)
                    : Retention.ApplyRetention(
                        toolRoot, dryRun: !flags.Contains("apply")));
            if (flags.Contains("run-jobs"))
                return Emit(RunJobs(toolRoot,
                    opts.TryGetValue("run-jobs", out string? n) &&
                    int.TryParse(n, out int limit) ? limit : 16));
            if (opts.TryGetValue("job", out string? jobId))
                return Emit(RunJob(toolRoot, jobId));
            if (flags.Contains("self-test"))
                return Emit(SelfTest(toolRoot));
            if (flags.Contains("verify-audit"))
                return Emit(new TransformerTrainingRepository(toolRoot)
                    .VerifyAuditChain());
            if (flags.Contains("db-status"))
                return Emit(new TransformerTrainingRepository(toolRoot)
                    .DatabaseStatus());
            if (flags.Contains("migrate"))
                return Emit(new Dictionary<string, object?>
                {
                    ["ok"] = true,
                    ["migrated"] =
                        new TransformerTrainingRepository(toolRoot).Maintain(),
                });
            return Usage();
        }
        catch (Exception exc)
        {
            return Emit(new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = exc.Message.Length > 500
                    ? exc.Message[..500] : exc.Message,
                ["error_type"] = exc.GetType().Name,
            });
        }
    }

    private static int Usage()
    {
        Console.Error.WriteLine(
            "GPTBridge.XingchengLearning [--tool-root <dir>] " +
            "(--status | --run-once [--force] | --enable | --disable | " +
            "--retention [--apply|--status] | --run-jobs [n] | " +
            "--job <id> | --self-test | --verify-audit | --db-status | " +
            "--migrate)");
        return 2;
    }

    private static string InferToolRoot()
    {
        // Walk ancestors looking for the local-model marker
        // (runtime/settings/self-learning.json or xingcheng/).
        var dir = new DirectoryInfo(Directory.GetCurrentDirectory());
        while (dir != null)
        {
            if (Directory.Exists(Path.Combine(dir.FullName, "xingcheng")) &&
                Directory.Exists(Path.Combine(dir.FullName, "runtime")))
                return dir.FullName;
            dir = dir.Parent;
        }
        return Directory.GetCurrentDirectory();
    }

    private static int Emit(Dictionary<string, object?> payload)
    {
        Console.WriteLine(CanonicalJson.PrettyDict(payload));
        return payload.TryGetValue("ok", out object? ok) &&
               ok is bool b && !b ? 1 : 0;
    }

    private static Dictionary<string, object?> Status(string toolRoot)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        var state = SelfLearningState.Load(toolRoot);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["policy"] = policy.ToDict(),
            ["state"] = state,
            ["training_window"] = policy.TrainingWindowStatus(),
            ["runtime_checkpoint"] = EngineSettings.PinnedCheckpoint(toolRoot),
            ["retention_policy"] = RetentionPolicy.Load(toolRoot).ToDict(),
            ["checked_at"] = XcPaths.IsoNow(),
        };
    }

    private static Dictionary<string, object?> SetEnabled(string toolRoot, bool on)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        policy.Enabled = on;
        policy.Save(toolRoot);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["action"] = on ? "enabled" : "disabled",
            ["policy"] = policy.ToDict(),
        };
    }

    private static Dictionary<string, object?> RetentionStatus(string toolRoot)
        => new()
        {
            ["ok"] = true,
            ["policy"] = RetentionPolicy.Load(toolRoot).ToDict(),
            ["checked_at"] = XcPaths.IsoNow(),
        };

    private static Dictionary<string, object?> RunJobs(string toolRoot, int limit)
    {
        var repo = new TransformerTrainingRepository(toolRoot);
        var executor = new TrainingJobExecutor(repo, toolRoot);
        var results = new List<object?>();
        foreach (var row in repo.QueuedJobs(limit))
        {
            results.Add(executor.RunJob((string)row["job_id"]!));
            // sequential: the retired executor drained the queue one job at
            // a time to keep resource supervision deterministic.
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["processed"] = results.Count,
            ["results"] = results,
        };
    }

    private static Dictionary<string, object?> RunJob(string toolRoot, string jobId)
    {
        var repo = new TransformerTrainingRepository(toolRoot);
        return new TrainingJobExecutor(repo, toolRoot).RunJob(jobId);
    }

    /// <summary>Governed end-to-end smoke of the native lane: synthetic
    /// snapshot -> dataset -> queued job -> tokenize -> trainer subprocess
    /// -> export-bundle -> completed. Uses a tiny scratch model so the
    /// 2.4 GB production bundle is never touched; every mutation goes
    /// through the repository's audited paths.</summary>
    private static Dictionary<string, object?> SelfTest(string toolRoot)
    {
        var steps = new List<object?>();
        var repo = new TransformerTrainingRepository(toolRoot);

        // Synthetic examples with a guaranteed train+validation split:
        // iterate suffixes until the deterministic hash split yields both.
        var examples = new List<Dictionary<string, object?>>();
        bool hasVal = false;
        for (int i = 0; i < 400 && (examples.Count < 6 || !hasVal); i++)
        {
            string prompt = $"xc-selftest prompt {i}";
            string completion = $"xc-selftest completion {i}";
            string hash = TransformerTrainingRepository.Sha256Text(
                $"{prompt}\n\n{completion}");
            bool val = Convert.ToInt32(hash[..8], 16) % 1000 < 500;
            if (!val || !hasVal)
            {
                examples.Add(new Dictionary<string, object?>
                {
                    ["revision"] = i + 1,
                    ["example_id"] = $"selftest-{i}",
                    ["intent"] = "selftest",
                    ["input_text"] = prompt,
                    ["target_text"] = completion,
                    ["source_type"] = "selftest",
                    ["quality_score"] = 0.9,
                });
                if (val) hasVal = true;
            }
        }
        string snapshotPath = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"selftest-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        var snapshot = SftDataset.BuildSftDataset(
            snapshotPath,
            new Dictionary<string, List<Dictionary<string, object?>>>
            {
                ["main"] = examples,
            },
            valPermille: 500);
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "snapshot",
            ["examples"] = snapshot["manifest"] is Dictionary<string, object?> m
                ? m.GetValueOrDefault("example_count") : null,
        });

        var dataset = repo.CreateDataset(
            contentSha256: (string)snapshot["content_sha256"]!,
            snapshotPath: (string)snapshot["snapshot_path"]!,
            snapshotSha256: (string)snapshot["snapshot_sha256"]!,
            examples: (List<Dictionary<string, object?>>)snapshot["examples"]!,
            sourceManifest: new Dictionary<string, object?>
            {
                ["format"] = SftDataset.SftFormatVersion,
                ["origin"] = "xc-learning-selftest",
            },
            createdBy: "xc-learning-selftest");
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "dataset",
            ["dataset_id"] = dataset["dataset_id"],
            ["inserted"] = dataset.GetValueOrDefault("inserted"),
        });

        // Tiny scratch model — the point is the governed chain, not
        // capacity. No init_checkpoint => trainer inits from ``model``.
        var job = repo.CreateTrainingJob(
            datasetId: (string)dataset["dataset_id"]!,
            configuration: new Dictionary<string, object?>
            {
                ["training_kind"] = "sft",
                ["tokenizer_dir"] =
                    "runtime/tokenizers/xingcheng-bpe-8k-20260919-120054",
                ["model_id"] = "xingcheng-selftest",
                ["model"] = new Dictionary<string, object?>
                {
                    ["vocab_size"] = 16384,
                    ["hidden_size"] = 64,
                    ["intermediate_size"] = 128,
                    ["num_hidden_layers"] = 2,
                    ["num_attention_heads"] = 4,
                    ["num_key_value_heads"] = 4,
                    ["max_position_embeddings"] = 256,
                    ["moe_num_experts"] = 0,
                },
                ["max_length"] = 128,
                ["max_steps"] = 4,
                ["warmup_steps"] = 1,
                ["lr"] = 1e-3,
                ["checkpoint_every"] = 0,
                ["eval_every"] = 0,
                ["log_every"] = 1,
                ["device"] = "cpu",
                ["max_train_seconds"] = 600,
                ["seed"] = 7,
            },
            requestedBy: "xc-learning-selftest");
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "job-queued", ["job_id"] = job["job_id"],
        });

        var report = new TrainingJobExecutor(repo, toolRoot)
            .RunJob((string)job["job_id"]!);
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "execute",
            ["ok"] = report["ok"],
            ["error_code"] = report.GetValueOrDefault("error_code"),
            ["output_path"] = report.GetValueOrDefault("output_path"),
        });
        if (!TransformerTrainingRepository.Truthy(report["ok"]))
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["steps"] = steps,
                ["job"] = report.GetValueOrDefault("job"),
            };

        // Lifecycle smoke against a scratch directory — production
        // lifecycle roots are never touched.
        string bundleDir = Path.GetDirectoryName(
            report["output_path"]!.ToString()!)!;
        string jobDir = Directory.GetParent(bundleDir)!.FullName;
        string lcDir = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"selftest-lifecycle-{DateTime.UtcNow:yyyyMMdd-HHmmss}");
        Directory.CreateDirectory(lcDir);
        // Adapter registration + native eval gate: candidate bundle vs
        // itself as baseline (regression delta = 0). The gate outcome is
        // reported; the step asserts the eval ran and was recorded.
        var adapter = repo.RegisterAdapterCandidate(
            (string)job["job_id"]!,
            report["output_path"]!.ToString()!,
            new Dictionary<string, object?>
            {
                ["origin"] = "xc-learning-selftest",
            });
        string suitePath = Path.Combine(
            toolRoot, "xingcheng", "eval",
            "star-native-eval-dialogue-20260921-125054.json");
        var eval = Evaluation.RunEvaluation(
            repo, (string)adapter["adapter_id"]!,
            report["output_path"]!.ToString()!, suitePath,
            baselineArtifact: report["output_path"]!.ToString()!,
            evaluatedBy: "xc-learning-selftest");
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "evaluate",
            ["adapter_id"] = adapter["adapter_id"],
            ["suite"] = eval.GetValueOrDefault("suite"),
            ["passed"] = eval.GetValueOrDefault("passed"),
            ["eval_ok"] = eval.GetValueOrDefault("ok"),
        });

        // DPO preference-pair bridge: build a governed snapshot and
        // register it (dataset row + audit), without queueing training.
        string pairsPath = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"selftest-pairs-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        var pairsManifest = SftDataset.BuildPairsSnapshot(
            Enumerable.Range(0, 4).Select(i =>
                new Dictionary<string, object?>
                {
                    ["pair_id"] = $"selftest-pair-{i}",
                    ["prompt_text"] = $"xc-selftest dpo prompt {i}",
                    ["chosen_text"] = $"chosen answer {i}",
                    ["rejected_text"] = $"rejected answer {i}",
                }).ToList(),
            pairsPath);
        var pairsDataset = SftDataset.RegisterPairsSnapshot(
            repo, pairsManifest, createdBy: "xc-learning-selftest");
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "preference-pairs",
            ["pairs"] = pairsManifest["pairs"],
            ["dataset_id"] = pairsDataset["dataset_id"],
        });

        var lc = ModelLifecycle.LoadOrCreate(lcDir, "xingcheng-selftest");
        lc.Transition("INITIALIZED");
        lc.Transition("SFT_TRAINING");
        lc.Transition("INSTRUCT_READY");
        var meta = new Dictionary<string, object?>
        {
            ["config_sha256"] = "selftest-fp",
            ["maturity_level"] = 7,
        };
        lc.RegisterArtifact("weights",
            Path.Combine(jobDir, "final.xcn"), meta, activate: true);
        lc.RegisterArtifact("weights",
            Path.Combine(bundleDir, "weights.bin"), meta, activate: true);
        lc.Save(lcDir);
        var reloaded = ModelLifecycle.Load(lcDir);
        if (reloaded.ActiveWeightsVersion != 2)
            throw new InvalidOperationException("lifecycle reload mismatch");
        reloaded.GovernedRollbackWeights(
            1, "selftest-fp", excludeVersions: new[] { 2 });
        if (reloaded.ActiveWeightsVersion != 1)
            throw new InvalidOperationException("governed rollback mismatch");
        bool denied = false;
        try
        {
            reloaded.GovernedRollbackWeights(2, "wrong-fp");
        }
        catch (ArgumentException) { denied = true; }
        var retired = reloaded.RetireWeights(keepLatest: 1);
        reloaded.Save(lcDir);
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "lifecycle",
            ["active_version"] = reloaded.ActiveWeightsVersion,
            ["rollback_denied_on_bad_fingerprint"] = denied,
            ["retired_versions"] = retired
                .Select(e => (object?)e["version"]).ToList(),
        });
        return new Dictionary<string, object?>
        {
            ["ok"] = denied && reloaded.ActiveWeightsVersion == 1 &&
                     TransformerTrainingRepository.Truthy(
                         eval.GetValueOrDefault("ok")),
            ["steps"] = steps,
            ["job"] = report.GetValueOrDefault("job"),
        };
    }
}
