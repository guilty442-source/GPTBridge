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
            if (flags.Contains("converge-check"))
                return Emit(ConvergenceChecks.Run(toolRoot));
            if (flags.Contains("maturation-status"))
                return Emit(MaturationStatus(toolRoot));
            if (flags.Contains("maturation-freeze"))
                return Emit(Maturation300M.Freeze(
                    toolRoot,
                    opts.TryGetValue("capability", out string? mc)
                        ? mc : "",
                    opts.TryGetValue("evidence", out string? me)
                        ? me : ""));
            if (flags.Contains("maturation-baseline"))
                return Emit(MaturationBaseline(toolRoot, opts));
            if (flags.Contains("release-gate"))
                return Emit(ConvergenceGate.Run(
                    toolRoot,
                    opts.TryGetValue("bundle", out string? gb)
                        ? gb : null,
                    !flags.Contains("no-builds"),
                    opts.TryGetValue("suite", out string? gs)
                        ? gs : null));
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
            if (flags.Contains("queue-job"))
                return Emit(QueueJob(
                    toolRoot,
                    opts.TryGetValue("rows", out string? rows) ? rows : "",
                    opts.TryGetValue("config", out string? cfg) ? cfg : "",
                    opts.TryGetValue("val-permille", out string? vp) &&
                    int.TryParse(vp, out int vpv) ? vpv : 50,
                    flags.Contains("include-collected")));
            // ── convergence contract (P0-P9) entry points ────────────
            if (flags.Contains("feature-catalog"))
                return Emit(FeatureCatalog.Install(toolRoot));
            if (flags.Contains("capabilities-resolve"))
                return Emit(CapabilitiesResolve(
                    toolRoot,
                    opts.TryGetValue("request", out string? rq)
                        ? rq : "",
                    opts.TryGetValue("bundle", out string? rb)
                        ? rb : ""));
            if (flags.Contains("language-scan"))
                return Emit(LanguageBoundary.Report(
                    opts.TryGetValue("root", out string? lr) &&
                    lr.Length > 0
                        ? Path.GetFullPath(lr) : toolRoot));
            if (flags.Contains("arch-gate"))
                return Emit(ArchGate(
                    opts.TryGetValue("evidence", out string? ev)
                        ? ev : ""));
            if (flags.Contains("tool-call-validate"))
                return Emit(ToolCallV2.ValidateRequest(
                    ParseJsonFile(
                        opts.TryGetValue("call", out string? tc)
                            ? tc : "")));
            if (flags.Contains("tool-result-validate"))
                return Emit(ToolCallV2.ValidateResult(
                    ParseJsonFile(
                        opts.TryGetValue("result", out string? trr)
                            ? trr : "")));
            if (flags.Contains("structured-validate"))
                return Emit(StructuredValidate(
                    opts.TryGetValue("text", out string? sv) ? sv : "",
                    opts.TryGetValue("schema", out string? ss)
                        ? ss : ""));
            if (flags.Contains("fim-validate"))
                return Emit(FimContract.Validate(
                    ParseJsonFile(
                        opts.TryGetValue("fim", out string? fj)
                            ? fj : "")));
            if (flags.Contains("code-task-validate"))
                return Emit(StarCodeAgentRuntime.ValidateTask(
                    ParseJsonFile(
                        opts.TryGetValue("task", out string? ct)
                            ? ct : "")));
            if (flags.Contains("task-create"))
                return Emit(LongHorizonTaskCoordinator.Create(
                    toolRoot,
                    opts.TryGetValue("goal", out string? tg) ? tg : "",
                    opts.TryGetValue("constraints", out string? tcs) &&
                    tcs.Length > 0
                        ? tcs.Split(';').ToList()
                        : new List<string>(),
                    opts.TryGetValue("generation", out string? tgen)
                        ? tgen : "gen-2-consolidated"));
            if (flags.Contains("task-status"))
                return Emit(LongHorizonTaskCoordinator.Load(
                    toolRoot,
                    opts.TryGetValue("task", out string? tid)
                        ? tid : "").ToDict());
            if (flags.Contains("task-transition"))
                return Emit(LongHorizonTaskCoordinator.Transition(
                    toolRoot,
                    opts.TryGetValue("task", out string? tt) ? tt : "",
                    Enum.Parse<LongHorizonState>(
                        opts.TryGetValue("state", out string? ts)
                            ? ts : "", ignoreCase: true),
                    opts.TryGetValue("note", out string? tn)
                        ? tn : ""));
            if (flags.Contains("task-checkpoint"))
                return Emit(LongHorizonTaskCoordinator.Checkpoint(
                    toolRoot,
                    opts.TryGetValue("task", out string? tc2) ? tc2 : "",
                    opts.TryGetValue("summary", out string? sm)
                        ? sm : ""));
            if (flags.Contains("task-resume"))
                return Emit(LongHorizonTaskCoordinator.Resume(
                    toolRoot,
                    opts.TryGetValue("task", out string? trm)
                        ? trm : "",
                    opts.TryGetValue("generation", out string? rg)
                        ? rg : ""));
            if (flags.Contains("task-step"))
                return Emit(LongHorizonTaskCoordinator.RecordStep(
                    toolRoot,
                    opts.TryGetValue("task", out string? st2) ? st2 : "",
                    opts.TryGetValue("step", out string? sp) ? sp : "",
                    flags.Contains("done")));
            if (flags.Contains("provenance-verify"))
                return Emit(BundleProvenance.Verify(
                    opts.TryGetValue("bundle", out string? pb)
                        ? pb : "",
                    ParseJsonFile(
                        opts.TryGetValue("provenance", out string? pp)
                            ? pp : ""),
                    opts.TryGetValue("generation", out string? pg)
                        ? pg : "",
                    opts.TryGetValue("architecture", out string? pa)
                        ? pa : "xc-fused-1"));
            if (flags.Contains("recovery-dataset-build"))
            {
                if (opts.TryGetValue("capability", out string? rcp) &&
                    rcp.Length > 0)
                    InstructionRecovery.Capability = rcp;
                return Emit(InstructionRecovery.BuildDataset(
                    opts.TryGetValue("out", out string? rdo)
                        ? rdo : "",
                    opts.TryGetValue("count", out string? rc) &&
                        int.TryParse(rc, out int rcv) ? rcv : 2800,
                    opts.TryGetValue("seed", out string? rsd) &&
                        int.TryParse(rsd, out int rsv) ? rsv : 42));
            }
            if (flags.Contains("recovery-eval"))
                return Emit(InstructionRecovery.EvalBundle(
                    toolRoot,
                    opts.TryGetValue("bundle", out string? reb)
                        ? reb : "",
                    opts.TryGetValue("suite", out string? res)
                        ? res : "",
                    opts.TryGetValue("out", out string? reo)
                        ? reo : null));
            if (flags.Contains("recovery-run"))
                return Emit(InstructionRecovery.Run(
                    toolRoot,
                    opts.TryGetValue("plan", out string? rpp)
                        ? rpp : ""));
            if (flags.Contains("provenance-compute"))
                return Emit(BundleProvenance.Compute(
                    opts.TryGetValue("bundle", out string? cb)
                        ? cb : "",
                    opts.TryGetValue("generation", out string? cg)
                        ? cg : "gen-2-consolidated",
                    opts.TryGetValue("architecture", out string? ca)
                        ? ca : "xc-fused-1",
                    opts.TryGetValue("xcn", out string? cx)
                        ? cx : "XCN1 v10",
                    opts.TryGetValue("build", out string? cbd)
                        ? cbd : "",
                    opts.TryGetValue("runtime", out string? crt)
                        ? crt : "xc-native-cpp23",
                    opts.TryGetValue("lineage", out string? cl)
                        ? cl : ""));
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
            "--job <id> | --self-test | --converge-check | " +
            "--maturation-status | --maturation-freeze --capability " +
            "<id> --evidence <ref> | --maturation-baseline --weights " +
            "<ref> --weights-sha256 <sha> --model <f> --runtime <f> " +
            "--service <f> | " +
            "--release-gate [--bundle <dir>] [--suite <file>] " +
            "[--no-builds] | " +
            "--verify-audit | --db-status | " +
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
            "--feature-catalog | " +
            "--capabilities-resolve --request <json> [--bundle <dir>] | " +
            "--language-scan [--root <dir>] | " +
            "--arch-gate --evidence <file.json> | " +
            "--tool-call-validate --call <file.json> | " +
            "--tool-result-validate --result <file.json> | " +
            "--structured-validate --text <file> [--schema <file>] | " +
            "--fim-validate --fim <file.json> | " +
            "--code-task-validate --task <file.json> | " +
            "--task-create --goal <text> [--constraints a;b] " +
            "[--generation <gen>] | " +
            "--task-status|--task-transition|--task-checkpoint|" +
            "--task-resume|--task-step --task <id> [...] | " +
            "--provenance-verify --bundle <dir> --provenance <file.json> " +
            "[--generation <gen>] [--architecture <arch>] | " +
            "--provenance-compute --bundle <dir> [...] | " +
            "--recovery-dataset-build --out <dir> [--count N] [--seed N] | " +
            "--recovery-eval --bundle <dir> --suite <file> [--out <file>] | " +
            "--recovery-run --plan <plan.json>)");
        return 2;
    }

    // ── convergence helpers ───────────────────────────────────────────

    private static Dictionary<string, object?> ParseJsonFile(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            throw new ArgumentException("json file missing or unreadable");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        return (Dictionary<string, object?>)ModelLifecycle.Decode(
            doc.RootElement)!;
    }

    private static Dictionary<string, object?> CapabilitiesResolve(
        string toolRoot, string requestPath, string bundle)
    {
        long trainedContext = 0;
        if (bundle.Length > 0)
        {
            // Read the bundle manifest's trained context so the context
            // budget resolves against the real ceiling.
            string mp = Path.Combine(bundle, "manifest.json");
            if (File.Exists(mp))
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(mp));
                if (doc.RootElement.TryGetProperty("config",
                        out var cfg) &&
                    cfg.TryGetProperty("max_position_embeddings",
                        out var mpe) &&
                    mpe.ValueKind == JsonValueKind.Number)
                    trainedContext = mpe.GetInt64();
            }
        }
        var req = string.IsNullOrWhiteSpace(requestPath)
            ? new Dictionary<string, object?>()
            : ParseJsonFile(requestPath);
        return RuntimeCapabilityLayer.Resolve(req, trainedContext);
    }

    private static Dictionary<string, object?> ArchGate(string evidencePath)
    {
        var e = string.IsNullOrWhiteSpace(evidencePath)
            ? new ArchitectureChangeGate.Evidence()
            : ParseEvidence(evidencePath);
        return ArchitectureChangeGate.Evaluate(e);
    }

    private static ArchitectureChangeGate.Evidence ParseEvidence(
        string path)
    {
        var d = ParseJsonFile(path);
        bool B(string k)
            => d.TryGetValue(k, out var v) && v is bool b && b;
        return new ArchitectureChangeGate.Evidence
        {
            ExistingArchCannotSolve = B("existing_arch_cannot_solve"),
            RuntimeOptimizationIneffective =
                B("runtime_optimization_ineffective"),
            DataImprovementIneffective = B("data_improvement_ineffective"),
            PostTrainingIneffective = B("post_training_ineffective"),
            IndependentBenchmark = B("independent_benchmark"),
            Ablation = B("ablation"),
            MemoryImpact = B("memory_impact"),
            LatencyImpact = B("latency_impact"),
        };
    }

    private static Dictionary<string, object?> StructuredValidate(
        string textPath, string schemaPath)
    {
        if (string.IsNullOrWhiteSpace(textPath) ||
            !File.Exists(textPath))
            throw new ArgumentException("structured text file missing");
        string text = File.ReadAllText(textPath);
        string? schema =
            string.IsNullOrWhiteSpace(schemaPath) ||
            !File.Exists(schemaPath)
                ? null : File.ReadAllText(schemaPath);
        var outcome = StructuredOutputValidator.Validate(text, schema);
        return new Dictionary<string, object?>
        {
            ["ok"] = outcome.Ok,
            ["error"] = outcome.Error.Length > 0 ? outcome.Error : null,
            ["repaired"] = outcome.Repaired,
            ["format"] = StructuredOutputValidator.Format,
        };
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
    private static Dictionary<string, object?> MaturationStatus(
        string toolRoot)
    {
        var state = Maturation300M.LoadState(toolRoot);
        var caps = (Dictionary<string, object?>)state["capabilities"]!;
        var head = Maturation300M.Head(state);
        var rows = new List<object?>();
        foreach (var spec in Maturation300M.Sequence)
        {
            var raw = caps.TryGetValue(spec.Id, out object? c)
                ? c as Dictionary<string, object?> : null;
            rows.Add(new Dictionary<string, object?>
            {
                ["capability"] = spec.Id,
                ["status"] = raw?.GetValueOrDefault("status") ?? "pending",
                ["weight_version"] =
                    raw?.GetValueOrDefault("weight_version"),
                ["metrics"] = spec.Metrics.Cast<object?>().ToList(),
            });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["phase"] = Maturation300M.PhaseId,
            ["model_scale"] = Maturation300M.ModelScale,
            ["architecture_generation"] =
                Maturation300M.ArchitectureGeneration,
            ["active_capability"] = head?.Id,
            ["capabilities"] = rows,
            ["ladder"] = Maturation300M.Ladder.Select(
                g => (object?)new Dictionary<string, object?>
                {
                    ["level"] = g.Level,
                    ["code"] = g.Code,
                    ["requirement"] = g.Requirement,
                }).ToList(),
        };
    }

    private static Dictionary<string, object?> MaturationBaseline(
        string toolRoot, Dictionary<string, string> opts)
    {
        string weights = opts.TryGetValue("weights", out string? w)
            ? w : "";
        string sha = opts.TryGetValue("weights-sha256", out string? s)
            ? s : "";
        string Get(string k) =>
            opts.TryGetValue(k, out string? v) ? v : "";
        var model = ParseJsonFile(Get("model"));
        var runtime = ParseJsonFile(Get("runtime"));
        var service = ParseJsonFile(Get("service"));
        var artifact = Maturation300M.WriteBaseline(
            toolRoot, weights, sha, model, runtime, service);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["baseline"] = Maturation300M.BaselineRel,
            ["sections"] =
                Maturation300M.BaselineSections.Cast<object?>().ToList(),
            ["weights_ref"] = artifact["weights_ref"],
        };
    }

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

        // Dedup reuse: a previously registered dataset keeps its
        // original snapshot_path, which retention may have cleaned.
        // Content is deterministic, so restore the file to the
        // recorded path — the registered sha256 still verifies.
        if (dataset["inserted"] is bool ins && !ins &&
            dataset["snapshot_path"] is string recPath &&
            recPath.Length > 0 && !File.Exists(recPath))
        {
            Directory.CreateDirectory(Path.GetDirectoryName(recPath)!);
            File.Copy(snapshotPath, recPath, overwrite: true);
        }

        // Tiny scratch model — the point is the governed chain, not
        // capacity. No init_checkpoint => trainer inits from ``model``.
        var job = repo.CreateTrainingJob(
            datasetId: (string)dataset["dataset_id"]!,
            configuration: new Dictionary<string, object?>
            {
                ["training_kind"] = "sft",
                // §0 recovery lane: under SINGLE_CAPABILITY_RECOVERY a
                // governed-chain job must declare the active capability
                // — an unlabeled sft job is denied by GuardJob.
                ["capability"] = SelfLearningPolicy.Load(toolRoot)
                    .ActiveCapability,
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
