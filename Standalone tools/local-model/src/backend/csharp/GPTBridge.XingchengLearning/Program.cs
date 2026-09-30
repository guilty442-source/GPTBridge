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
            if (flags.Contains("teacher-collect"))
                return Emit(TeacherCollect.Collect(
                    toolRoot, dryRun: flags.Contains("dry-run")));
            if (flags.Contains("evaluate"))
                return Emit(Evaluate(
                    toolRoot,
                    opts.TryGetValue("job-id", out string? ej) ? ej : "",
                    opts.TryGetValue("bundle", out string? eb) ? eb : "",
                    opts.TryGetValue("suite", out string? es) ? es : "",
                    opts.TryGetValue("baseline", out string? bl) ? bl : null,
                    flags.Contains("chat")));
            if (flags.Contains("gen-begin"))
                return Emit(GenerationMigration.Begin(
                    toolRoot,
                    opts.TryGetValue("target", out string? gt) ? gt : "",
                    opts.TryGetValue("weights", out string? gw) ? gw : "",
                    opts.TryGetValue("weight-method", out string? wm)
                        ? wm : "",
                    source: opts.TryGetValue("source", out string? gs)
                        ? gs : "",
                    tokenizer: opts.TryGetValue("tokenizer", out string? tk)
                        ? tk : "",
                    schemaFrom: opts.TryGetValue("schema-from",
                        out string? sf) ? sf : "",
                    schemaTo: opts.TryGetValue("schema-to", out string? st)
                        ? st : "",
                    expertLineage: opts.TryGetValue("expert-lineage",
                        out string? el) ? el : "",
                    notes: opts.TryGetValue("notes", out string? nt)
                        ? nt : ""));
            if (flags.Contains("gen-record"))
                return Emit(GenerationMigration.Record(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? rm) ? rm : "",
                    opts.TryGetValue("domain", out string? rd) ? rd : "",
                    opts.TryGetValue("status", out string? rs) ? rs : "",
                    opts.TryGetValue("migrated", out string? mig) &&
                        long.TryParse(mig, out long mv) ? mv : 0,
                    opts.TryGetValue("transformed", out string? tr2) &&
                        long.TryParse(tr2, out long tv) ? tv : 0,
                    opts.TryGetValue("rejected", out string? rej) &&
                        long.TryParse(rej, out long rv) ? rv : 0,
                    opts.TryGetValue("note", out string? rn) ? rn : ""));
            if (flags.Contains("gen-certify"))
                return Emit(GenerationMigration.Certify(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? cm) ? cm : "",
                    suitePath: opts.TryGetValue("suite", out string? cs)
                        ? cs : ""));
            if (flags.Contains("gen-promote"))
                return Emit(GenerationMigration.Promote(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? pm) ? pm : ""));
            if (flags.Contains("gen-purge"))
                return Emit(GenerationMigration.Purge(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? pu) ? pu : "",
                    apply: flags.Contains("apply")));
            if (flags.Contains("gen-status"))
                return Emit(GenerationMigration.Status(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? sm) ? sm : ""));
            if (flags.Contains("trace-record"))
                return Emit(CapabilityTrace.RecordTrace(
                    toolRoot,
                    opts.TryGetValue("trace", out string? tf) &&
                        tf.Length > 0 ? tf
                    : opts.TryGetValue("file", out string? tf2)
                        ? tf2 : ""));
            if (flags.Contains("cap-record"))
                return Emit(CapabilityTrace.RecordResult(
                    toolRoot,
                    opts.TryGetValue("result", out string? rf) &&
                        rf.Length > 0 ? rf
                    : opts.TryGetValue("file", out string? rf2)
                        ? rf2 : ""));
            if (flags.Contains("trace-status"))
                return Emit(CapabilityTrace.Status(toolRoot));
            // ---- runtime capability plane (star-runtime-capabilities/v1)
            if (flags.Contains("caps-status"))
                return Emit(RuntimeCapabilities.Status(toolRoot));
            if (flags.Contains("caps-validate"))
                return Emit(RuntimeCapabilities.Validate(
                    opts.TryGetValue("profile", out string? cp)
                        ? cp : ""));
            // ---- feature catalog (star-model-feature-catalog/v1)
            if (flags.Contains("feature-catalog"))
                return Emit(FeatureCatalog.Emit(toolRoot));
            if (flags.Contains("catalog-validate"))
                return Emit(FeatureCatalog.Validate(
                    opts.TryGetValue("file", out string? cf) ? cf : ""));
            // ---- tool contracts (§16/§17/§18)
            if (flags.Contains("tool-decision"))
                return Emit(ToolContracts.Decide(
                    toolRoot,
                    opts.TryGetValue("request", out string? tdr)
                        ? tdr : ""));
            if (flags.Contains("tool-call-validate"))
                return Emit(ToolContracts.ValidateCall(
                    opts.TryGetValue("call", out string? tcc)
                        ? tcc : ""));
            if (flags.Contains("tool-result-validate"))
                return Emit(ToolContracts.ValidateResult(
                    opts.TryGetValue("result", out string? trr)
                        ? trr : ""));
            if (flags.Contains("structured-validate"))
                return Emit(ToolContracts.ValidateStructured(
                    opts.TryGetValue("output", out string? soo)
                        ? soo : "",
                    opts.TryGetValue("schema", out string? sos)
                        ? sos : "",
                    repair: flags.Contains("repair")));
            if (flags.Contains("grounded-record"))
                return Emit(ToolContracts.RecordGrounded(
                    toolRoot,
                    opts.TryGetValue("result", out string? grf)
                        ? grf : ""));
            // ---- long-horizon tasks (§5)
            if (flags.Contains("task-create"))
                return Emit(LongHorizonTask.Create(
                    toolRoot,
                    opts.TryGetValue("goal", out string? tg) ? tg : "",
                    opts.TryGetValue("constraints", out string? tc)
                        ? tc : "",
                    opts.TryGetValue("plan", out string? tpl)
                        ? tpl : ""));
            if (flags.Contains("task-step"))
                return Emit(LongHorizonTask.Step(
                    toolRoot,
                    opts.TryGetValue("task", out string? ts1) ? ts1 : "",
                    opts.TryGetValue("step", out string? ts2) ? ts2 : "",
                    opts.TryGetValue("tool-result", out string? ttr)
                        ? ttr : "",
                    opts.TryGetValue("evidence", out string? tev)
                        ? tev : ""));
            if (flags.Contains("task-checkpoint"))
                return Emit(LongHorizonTask.Checkpoint(
                    toolRoot,
                    opts.TryGetValue("task", out string? tc1)
                        ? tc1 : ""));
            if (flags.Contains("task-compact"))
                return Emit(LongHorizonTask.Compact(
                    toolRoot,
                    opts.TryGetValue("task", out string? tc2)
                        ? tc2 : ""));
            if (flags.Contains("task-resume"))
                return Emit(LongHorizonTask.Resume(
                    toolRoot,
                    opts.TryGetValue("task", out string? tr1)
                        ? tr1 : ""));
            if (flags.Contains("task-verify"))
                return Emit(LongHorizonTask.Verify(
                    toolRoot,
                    opts.TryGetValue("task", out string? tv1)
                        ? tv1 : "",
                    opts.TryGetValue("note", out string? tvn)
                        ? tvn : ""));
            if (flags.Contains("task-complete"))
                return Emit(LongHorizonTask.Complete(
                    toolRoot,
                    opts.TryGetValue("task", out string? tc3)
                        ? tc3 : ""));
            if (flags.Contains("task-fail"))
                return Emit(LongHorizonTask.Fail(
                    toolRoot,
                    opts.TryGetValue("task", out string? tf1)
                        ? tf1 : "",
                    opts.TryGetValue("reason", out string? tfr)
                        ? tfr : ""));
            if (flags.Contains("task-status"))
                return Emit(LongHorizonTask.Status(
                    toolRoot,
                    opts.TryGetValue("task", out string? tst)
                        ? tst : ""));
            // ---- coding lane (§10/§11/§22)
            if (flags.Contains("code-task-validate"))
                return Emit(CodeAgent.ValidateTask(
                    opts.TryGetValue("task-file", out string? ct)
                        ? ct : ""));
            if (flags.Contains("fim-validate"))
                return Emit(CodeAgent.ValidateFim(
                    opts.TryGetValue("file", out string? ff) ? ff : ""));
            if (flags.Contains("code-run"))
                return Emit(CodeAgent.Run(
                    toolRoot,
                    opts.TryGetValue("task-file", out string? cr)
                        ? cr : ""));
            // ---- modality + teacher lineage (§12)
            if (flags.Contains("modality-record"))
                return Emit(ModalityContracts.RecordModality(
                    toolRoot,
                    opts.TryGetValue("file", out string? mf) ? mf : ""));
            if (flags.Contains("teacher-lineage"))
                return Emit(ModalityContracts.RecordTeacher(
                    toolRoot,
                    opts.TryGetValue("file", out string? tlf)
                        ? tlf : ""));
            // ---- dataset quality (§14/§19)
            if (flags.Contains("dataset-quality"))
                return Emit(DatasetQuality.Evaluate(
                    toolRoot,
                    opts.TryGetValue("record", out string? dq)
                        ? dq : ""));
            // ---- evaluation plane (§30) + arch gate (§15) + provenance
            if (flags.Contains("eval-result"))
                return Emit(EvaluationCoordinator.Record(
                    toolRoot,
                    opts.TryGetValue("file", out string? erf)
                        ? erf : ""));
            if (flags.Contains("eval-status"))
                return Emit(EvaluationCoordinator.Status(toolRoot));
            if (flags.Contains("arch-gate"))
                return Emit(ArchitectureGate.Evaluate(
                    opts.TryGetValue("file", out string? agf)
                        ? agf : ""));
            if (flags.Contains("provenance-check"))
                return Emit(BundleProvenance.Check(
                    toolRoot,
                    opts.TryGetValue("bundle", out string? pb)
                        ? pb : ""));
            if (flags.Contains("tool-validate"))
                return Emit(ToolContracts.ValidateCall(
                    opts.TryGetValue("file", out string? tvf) ? tvf : "",
                    opts.TryGetValue("kind", out string? tvk)
                        ? tvk : "request"));
            if (flags.Contains("tool-gate"))
            {
                var decided = ToolContracts.Decide(
                    toolRoot,
                    opts.TryGetValue("tool", out string? tgt) ? tgt : "",
                    opts.TryGetValue("requirement", out string? tgr)
                        ? tgr : "optional",
                    opts.TryGetValue("reason", out string? tre) ? tre : "");
                if (opts.TryGetValue("outcome-status", out string? tos))
                    decided["outcome"] = ToolContracts.RecordOutcome(
                        toolRoot,
                        (string)decided["decision"]!,
                        schemaValid: !flags.Contains("schema-invalid"),
                        status: tos);
                return Emit(decided);
            }
            if (flags.Contains("tool-metrics"))
                return Emit(ToolContracts.MetricsPayload(toolRoot));
            if (flags.Contains("grounded-validate"))
                return Emit(ToolContracts.ValidateGrounded(
                    toolRoot,
                    opts.TryGetValue("file", out string? gvf) ? gvf : ""));
            if (flags.Contains("structured-validate"))
                return Emit(StructuredOutput.Validate(
                    toolRoot,
                    opts.TryGetValue("output", out string? svo) ? svo : "",
                    opts.TryGetValue("schema", out string? svs) ? svs : ""));
            if (flags.Contains("caps-status"))
                return Emit(RuntimeCapabilities.Status(toolRoot));
            if (flags.Contains("caps-validate"))
                return Emit(RuntimeCapabilities.Validate(
                    opts.TryGetValue("file", out string? cv) ? cv : ""));
            if (flags.Contains("catalog-emit"))
                return Emit(FeatureCatalog.Emit(toolRoot));
            if (flags.Contains("catalog-validate"))
                return Emit(FeatureCatalog.Validate(
                    opts.TryGetValue("file", out string? fv) ? fv : ""));
            if (flags.Contains("langcheck"))
                return Emit(LangCheck.Scan(toolRoot));
            if (flags.Contains("queue-job"))
                return Emit(QueueJob(
                    toolRoot,
                    opts.TryGetValue("rows", out string? rows) ? rows : "",
                    opts.TryGetValue("config", out string? cfg) ? cfg : "",
                    opts.TryGetValue("val-permille", out string? vp) &&
                    int.TryParse(vp, out int vpv) ? vpv : 50,
                    flags.Contains("include-collected")));
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
            "--migrate | --teacher-collect [--dry-run] | " +
            "--queue-job --config <cfg.json> [--rows <rows.jsonl>] " +
            "[--include-collected] [--val-permille N] | " +
            "--evaluate --job-id <id> --bundle <dir> --suite <suite.json> " +
            "[--baseline <dir>] [--chat] | " +
            "--gen-begin --target <gen> --weights <path> " +
            "--weight-method <direct|partial|distill> [--source <gen>] " +
            "[--tokenizer <path>] [--schema-from <s>] [--schema-to <s>] " +
            "[--expert-lineage <file.json>] [--notes <text>] | " +
            "--gen-record --manifest <id> --domain <name> " +
            "--status <s> [--migrated N] [--transformed N] " +
            "[--rejected N] [--note <text>] | " +
            "--gen-certify --manifest <id> [--suite <suite.json>] | " +
            "--gen-promote --manifest <id> | " +
            "--gen-purge --manifest <id> [--apply] | " +
            "--gen-status [--manifest <id>] | " +
            "--trace-record --trace <file.json> | " +
            "--cap-record --result <file.json> | --trace-status | " +
            "--caps-status | --caps-validate --file <f.json> | " +
            "--catalog-emit | --catalog-validate --file <f.json> | " +
            "--tool-validate --file <f.json> --kind <request|result> | " +
            "--tool-gate [--tool <name>] [--requirement <req>] " +
            "[--reason <code>] [--outcome-status <s>] [--schema-invalid] | " +
            "--tool-metrics | --grounded-validate --file <f.json> | " +
            "--structured-validate --output <f.json> --schema <f.json> | " +
            "--langcheck)");
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

    /// <summary>Registers a completed job's exported bundle as an adapter
    /// candidate and runs the governed native evaluation (capability or
    /// eval suite) against an optional baseline bundle. Records the full
    /// result row in the repository — the same gate self-learning uses.
    /// --chat measures the deployed chat surface.</summary>
    private static Dictionary<string, object?> Evaluate(
        string toolRoot, string jobId, string bundle, string suitePath,
        string? baseline, bool chat)
    {
        if (string.IsNullOrWhiteSpace(jobId) ||
            string.IsNullOrWhiteSpace(bundle) ||
            string.IsNullOrWhiteSpace(suitePath))
            throw new ArgumentException(
                "evaluate requires --job-id <id> --bundle <dir> " +
                "--suite <suite.json>");
        var repo = new TransformerTrainingRepository(toolRoot);
        var adapter = repo.RegisterAdapterCandidate(
            jobId, bundle,
            new Dictionary<string, object?>
            {
                ["origin"] = "queue-job-evaluate",
                ["prompt_mode"] = chat ? "chat" : "verbatim",
            });
        var eval = Evaluation.RunEvaluation(
            repo, (string)adapter["adapter_id"]!, bundle, suitePath,
            baselineArtifact: baseline,
            evaluatedBy: "queue-job-evaluate", chat: chat);
        return new Dictionary<string, object?>
        {
            ["ok"] = TransformerTrainingRepository.Truthy(
                eval.GetValueOrDefault("ok")),
            ["adapter_id"] = adapter["adapter_id"],
            ["evaluation"] = eval,
        };
    }

    /// <summary>Governed queue entry for externally prepared SFT rows:
    /// rows.jsonl (input_text/target_text/intent/source_type/
    /// quality_score/example_id/revision[/scope]) -> snapshot -> dataset
    /// -> queued job; execution stays behind the same audited job lane as
    /// self-learning. The configuration file carries the training
    /// configuration dictionary verbatim (model arch, init_checkpoint,
    /// weight_quant, budgets).</summary>
    private static Dictionary<string, object?> QueueJob(
        string toolRoot, string rowsPath, string configPath,
        int valPermille, bool includeCollected)
    {
        if (string.IsNullOrWhiteSpace(configPath) ||
            (string.IsNullOrWhiteSpace(rowsPath) && !includeCollected))
            throw new ArgumentException(
                "queue-job requires --config <json> and --rows <jsonl> " +
                "or --include-collected");
        var byScope =
            new Dictionary<string, List<Dictionary<string, object?>>>(
                StringComparer.OrdinalIgnoreCase);
        int lineno = 0;
        if (!string.IsNullOrWhiteSpace(rowsPath))
            foreach (var line in File.ReadLines(rowsPath))
            {
                ++lineno;
                if (string.IsNullOrWhiteSpace(line)) continue;
                Dictionary<string, object?> row;
                try
                {
                    row = (Dictionary<string, object?>)ModelLifecycle.Decode(
                        JsonDocument.Parse(line).RootElement)!;
                }
                catch (Exception parseEx)
                    when (parseEx is JsonException or InvalidCastException)
                {
                    throw new ArgumentException(
                        $"rows.jsonl line {lineno}: invalid JSON");
                }
                string scope =
                    (TransformerTrainingRepository.Str(row, "scope") ?? "main")
                    .Trim();
                if (!byScope.TryGetValue(scope, out var list))
                    byScope[scope] = list =
                        new List<Dictionary<string, object?>>();
                list.Add(row);
            }
        int collected = 0;
        if (includeCollected)
            foreach (var (scope, rows) in
                     Collectors.CollectVerifiedExamples(toolRoot))
            {
                if (!byScope.TryGetValue(scope, out var list))
                    byScope[scope] = list =
                        new List<Dictionary<string, object?>>();
                list.AddRange(rows);
                collected += rows.Count;
            }
        if (byScope.Count == 0)
            throw new ArgumentException("queue-job produced no examples");

        Dictionary<string, object?> configuration;
        try
        {
            configuration = (Dictionary<string, object?>)
                ModelLifecycle.Decode(
                    JsonDocument.Parse(
                        File.ReadAllText(configPath)).RootElement)!;
        }
        catch (Exception cfgEx) when (cfgEx is JsonException or IOException
                                          or InvalidCastException)
        {
            throw new ArgumentException("config file unreadable or invalid");
        }

        string snapshotPath = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"queue-job-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        var snapshot = SftDataset.BuildSftDataset(
            snapshotPath, byScope, valPermille);
        var repo = new TransformerTrainingRepository(toolRoot);
        var dataset = repo.CreateDataset(
            contentSha256: (string)snapshot["content_sha256"]!,
            snapshotPath: (string)snapshot["snapshot_path"]!,
            snapshotSha256: (string)snapshot["snapshot_sha256"]!,
            examples: (List<Dictionary<string, object?>>)snapshot["examples"]!,
            sourceManifest: new Dictionary<string, object?>
            {
                ["format"] = SftDataset.SftFormatVersion,
                ["origin"] = "queue-job",
                ["rows_source"] = string.IsNullOrWhiteSpace(rowsPath)
                    ? null : Path.GetFileName(rowsPath),
                ["include_collected"] = includeCollected,
                ["collected_examples"] = collected,
            },
            createdBy: "queue-job");
        var job = repo.CreateTrainingJob(
            datasetId: (string)dataset["dataset_id"]!,
            configuration: configuration,
            requestedBy: "queue-job");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["dataset_id"] = dataset["dataset_id"],
            ["dataset_inserted"] = dataset.GetValueOrDefault("inserted"),
            ["job_id"] = job["job_id"],
            ["snapshot_path"] = snapshot["snapshot_path"],
            ["examples"] = snapshot["examples"] is
                List<Dictionary<string, object?>> exList ? exList.Count : 0,
        };
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
        // 世代繼任契約：v2 啟用時自動攜入 v1 完整記錄 —— 刪除前代後其
        // 資料仍保留在新代 metadata.succeeded_from 內。
        bool successionRecorded = false;
        if (reloaded.Artifacts.TryGetValue("weights", out var wg) &&
            wg.TryGetValue("versions", out object? wv) &&
            wv is List<object?> wlist)
            foreach (object? item in wlist)
                if (item is Dictionary<string, object?> e &&
                    Convert.ToInt32(e["version"]) == 2 &&
                    e["metadata"] is Dictionary<string, object?> em &&
                    em["succeeded_from"] is Dictionary<string, object?> sf &&
                    Convert.ToInt32(sf["version"]) == 1 &&
                    sf["sha256"] is string sfs && sfs.Length > 0)
                    successionRecorded = true;
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
        // Fail-closed: the active generation is never retired by the
        // supersession path even when a successor nominates it.
        bool retireActiveDenied = reloaded.RetireWeightVersion(1, 2) == null;
        var retired = reloaded.RetireWeights(keepLatest: 1);
        reloaded.Save(lcDir);
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "lifecycle",
            ["active_version"] = reloaded.ActiveWeightsVersion,
            ["rollback_denied_on_bad_fingerprint"] = denied,
            ["succession_recorded"] = successionRecorded,
            ["retire_active_denied"] = retireActiveDenied,
            ["retired_versions"] = retired
                .Select(e => (object?)e["version"]).ToList(),
        });
        return new Dictionary<string, object?>
        {
            ["ok"] = denied && successionRecorded && retireActiveDenied &&
                     reloaded.ActiveWeightsVersion == 1 &&
                     TransformerTrainingRepository.Truthy(
                         eval.GetValueOrDefault("ok")),
            ["steps"] = steps,
            ["job"] = report.GetValueOrDefault("job"),
        };
    }
}
