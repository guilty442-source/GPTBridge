// JobExecutor.cs — governed training job executor, native C++ lane.
//
// Port of training_job_executor.run_job: the same state machine
// (queued → preflight → training → validating → completed|failed), the
// same EXECUTOR_* error codes, the same lifecycle bookkeeping and audit
// events — but the training body is the native chain instead of the
// retired Python/JAX/torch trainers:
//
//   snapshot docs -> xc_modeltool tokenize -> init bundle -> import-bundle
//   -> xingcheng_trainer --job -> report.json -> export-bundle
//   -> adapter artifact = <job>/bundle/manifest.json
//
// ``.pt`` init/resume is retired lineage: fail-closed
// (EXECUTOR_INIT_CHECKPOINT_RETIRED), mirroring JAX_INIT_TORCH_CHECKPOINT.

using System.Diagnostics;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal sealed class TrainingJobExecutor
{
    private readonly TransformerTrainingRepository _repo;
    private readonly string _toolRoot;   // <local-model> (repo.ToolRoot is +/xingcheng)
    private readonly string _jobsRoot;   // <tool>/xingcheng/runtime/models/jobs

    private const string JobsRelative = "xingcheng/runtime/models/jobs";
    private const string LifecycleRelative =
        "xingcheng/runtime/models/lifecycle";
    private const string ResourceActionLedger =
        "xingcheng/runtime/logs/resource-actions.jsonl";
    private static readonly HashSet<string> ResourceActionTrainingStates =
        new(StringComparer.Ordinal) { "PRETRAINING", "SFT_TRAINING" };

    public TrainingJobExecutor(TransformerTrainingRepository repository, string toolRoot)
    {
        _repo = repository;
        _toolRoot = Path.GetFullPath(toolRoot);
        _jobsRoot = Path.GetFullPath(
            Path.Combine(_toolRoot, JobsRelative));
    }

    // ------------------------------------------------------ normalization --

    private string ResolveUnderRoot(string value, string code)
    {
        string candidate = Path.IsPathRooted(value)
            ? value
            : Path.Combine(_repo.ToolRoot, value);
        string resolved = Path.GetFullPath(candidate);
        if (!resolved.StartsWith(_repo.ToolRoot + Path.DirectorySeparatorChar,
                                 StringComparison.Ordinal) &&
            !string.Equals(resolved, _repo.ToolRoot, StringComparison.Ordinal))
            throw new ExecutorError(code,
                $"path escapes tool root: {value}");
        return resolved;
    }

    private Dictionary<string, object?> NormalizeConfiguration(
        IReadOnlyDictionary<string, object?> row)
    {
        string raw = (string)(row["configuration_json"] ?? "");
        Dictionary<string, object?> cfg;
        try
        {
            using var doc = JsonDocument.Parse(raw);
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
                throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                    "configuration_json must be an object");
            cfg = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                cfg[p.Name] = ModelLifecycle.Decode(p.Value);
        }
        catch (JsonException)
        {
            throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                "configuration_json is not valid JSON");
        }

        foreach (string key in new[]
                 {
                     "max_length", "batch_size", "grad_accum", "max_steps",
                     "warmup_steps", "checkpoint_every", "eval_every",
                     "log_every", "gpu_required_mb", "max_train_seconds",
                     "max_train_vram_mb", "max_train_gpu_seconds",
                 })
        {
            if (!cfg.ContainsKey(key)) continue;
            object? v = cfg[key];
            if (v is int or long) continue;
            if (v is string s && int.TryParse(s, out int parsed))
            {
                cfg[key] = parsed;
                continue;
            }
            throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                $"{key} must be an integer");
        }
        if (cfg.TryGetValue("lr", out object? lrRaw))
        {
            double lr = Convert.ToDouble(lrRaw);
            if (!(lr > 0 && lr <= 0.1))
                throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                    "lr outside bounded range (0, 0.1]");
            cfg["lr"] = lr;
        }
        if (cfg.TryGetValue("beta", out object? betaRaw))
        {
            double beta = Convert.ToDouble(betaRaw);
            if (!(beta >= 0.001 && beta <= 1.0))
                throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                    "beta outside bounded range [0.001, 1.0]");
            cfg["beta"] = beta;
        }

        string kind = TransformerTrainingRepository.Str(cfg, "training_kind")
                      ?? "pretrain";
        if (kind is not ("pretrain" or "sft" or "dpo"))
            throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                $"unknown training_kind: {kind}");
        cfg["training_kind"] = kind;
        // §34 capability-training freeze — a formal training kind is a
        // frozen operation while the freeze holds; probes/benchmarks
        // never flow through this executor. SINGLE_CAPABILITY_RECOVERY
        // narrows the freeze to exactly one declared-capability SFT job.
        cfg["capability"] =
            TransformerTrainingRepository.Str(cfg, "capability") ?? "";
        var freezePol = SelfLearningPolicy.Load(_toolRoot);
        CapabilityFreeze.GuardJob(kind, (string)cfg["capability"]!,
                                  freezePol);
        if (kind == "pretrain" && CapabilityFreeze.CAPABILITY_TRAINING_FROZEN)
        {
            // star-canonical-pretrain/v1: while the freeze holds, the
            // lane admits pretrain only when the policy declares the
            // canonical-pretrain mode AND the job's model block pins the
            // canonical generation — a generic or non-canonical pretrain
            // stays frozen.
            if (!CapabilityFreeze.CanonicalPretrainLaneOpen(freezePol))
                throw new ExecutorError("CANONICAL_PRETRAIN_DENIED",
                    "pretrain requires architecture_pretrain_mode " +
                    "XC_FUSED_1 while capability training is frozen");
            string? gen = cfg.TryGetValue("model", out object? mdl) &&
                          mdl is Dictionary<string, object?> mdict
                ? TransformerTrainingRepository.Str(mdict, "generation")
                : null;
            if (!string.Equals(gen, ArchitectureTaxonomy.CanonicalArchitecture,
                               StringComparison.Ordinal))
                throw new ExecutorError("CANONICAL_PRETRAIN_DENIED",
                    "canonical pretrain lane requires model.generation " +
                    $"== {ArchitectureTaxonomy.CanonicalArchitecture}");
        }
        // §4/§50 maturation order: even when the freeze lane admits the
        // job, the declared capability must be the current sequence head
        // (instruction_following first); out-of-order capabilities are
        // denied before any weight work is scheduled.
        if (kind == "sft")
            Maturation300M.GuardSequence(_toolRoot,
                                         (string)cfg["capability"]!);


        object? initRaw = cfg.GetValueOrDefault("init_checkpoint");
        if (initRaw != null && initRaw.ToString() is { Length: > 0 } initStr)
        {
            string resolved = ResolveUnderRoot(
                initStr, "EXECUTOR_INIT_CHECKPOINT_SCOPE_DENIED");
            if (resolved.EndsWith(".pt", StringComparison.OrdinalIgnoreCase))
                throw new ExecutorError(
                    "EXECUTOR_INIT_CHECKPOINT_RETIRED",
                    "torch .pt init is retired lineage; provide a native " +
                    "bundle directory or .xcn checkpoint");
            if (!Directory.Exists(resolved) && !File.Exists(resolved))
                throw new ExecutorError("EXECUTOR_INIT_CHECKPOINT_MISSING",
                    $"init checkpoint missing: {resolved}");
            cfg["init_checkpoint"] = resolved;
        }
        else
        {
            cfg["init_checkpoint"] = null;
        }
        if (kind == "dpo" && cfg["init_checkpoint"] == null)
            throw new ExecutorError("EXECUTOR_DPO_REQUIRES_INIT",
                "DPO requires init_checkpoint as policy/reference anchor");

        object? resumeRaw = cfg.GetValueOrDefault("resume_checkpoint");
        if (resumeRaw != null && resumeRaw.ToString() is { Length: > 0 } resumeStr)
        {
            string resolved = ResolveUnderRoot(
                resumeStr, "EXECUTOR_RESUME_SCOPE_DENIED");
            if (!File.Exists(resolved))
                throw new ExecutorError("EXECUTOR_RESUME_MISSING",
                    $"resume checkpoint missing: {resolved}");
            cfg["resume_checkpoint"] = resolved;
        }
        else
        {
            cfg["resume_checkpoint"] = null;
        }
        cfg["model_id"] = TransformerTrainingRepository.Str(cfg, "model_id")
                          ?? XcPaths.ModelId;
        return cfg;
    }

    /// <summary>§41 freeze patterns from the job config: accepts a JSON
    /// array of strings (bounded 64, each ≤256 chars) — anything else is
    /// a typed rejection, never a silent drop.</summary>
    private static List<object?>? FreezePatterns(
        Dictionary<string, object?> configuration)
    {
        if (!configuration.TryGetValue("freeze", out object? raw) ||
            raw is null)
            return null;
        if (raw is not System.Collections.IEnumerable list ||
            raw is string)
            throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                "freeze must be an array of pattern strings");
        var patterns = new List<object?>();
        foreach (object? item in list)
        {
            if (item is not string s || s.Length == 0 || s.Length > 256)
                throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                    "freeze pattern must be a non-empty string ≤256 chars");
            patterns.Add(s);
            if (patterns.Count > 64)
                throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                    "freeze pattern count exceeds 64");
        }
        return patterns;
    }

    private (Dictionary<string, object?> dataset,
             List<Dictionary<string, object?>> trainDocs,
             List<Dictionary<string, object?>> valDocs) LoadSplitDocuments(
        string datasetId)
    {
        var (dataset, splits) = _repo.DatasetAndSplits(datasetId);
        if (dataset.Count == 0)
            throw new ExecutorError("EXECUTOR_DATASET_MISSING",
                "training dataset does not exist");
        if ((string?)dataset["state"] != "prepared")
            throw new ExecutorError("EXECUTOR_DATASET_NOT_PREPARED",
                $"training dataset state is {dataset["state"]}");
        string snapshotPath = (string)dataset["snapshot_path"]!;
        if (!File.Exists(snapshotPath))
            throw new ExecutorError("EXECUTOR_SNAPSHOT_MISSING",
                "dataset snapshot file is missing");
        if (TransformerTrainingRepository.Sha256File(snapshotPath) !=
            (string?)dataset["snapshot_sha256"])
            throw new ExecutorError("EXECUTOR_SNAPSHOT_DRIFT",
                "snapshot SHA-256 drifted from registered digest");
        var documents = ReadSnapshotDocuments(snapshotPath);
        var snapshotHashes = documents
            .Select(d => (string)d["sha256"]!).ToHashSet(StringComparer.Ordinal);
        if (!snapshotHashes.SetEquals(splits.Keys))
            throw new ExecutorError("EXECUTOR_SNAPSHOT_MISMATCH",
                "snapshot records do not match registered example hashes");
        var trainDocs = documents
            .Where(d => splits[(string)d["sha256"]!] == "train").ToList();
        var valDocs = documents
            .Where(d => splits[(string)d["sha256"]!] == "validation").ToList();
        if (trainDocs.Count == 0 || valDocs.Count == 0)
            throw new ExecutorError("EXECUTOR_EMPTY_SPLIT",
                "registered splits produced no train/validation documents");
        return (dataset, trainDocs, valDocs);
    }

    private static List<Dictionary<string, object?>> ReadSnapshotDocuments(string path)
    {
        var docs = new List<Dictionary<string, object?>>();
        try
        {
            foreach (string line in File.ReadLines(path))
            {
                string trimmed = line.Trim();
                if (trimmed.Length == 0) continue;
                using var doc = JsonDocument.Parse(trimmed);
                var record = new Dictionary<string, object?>();
                foreach (var p in doc.RootElement.EnumerateObject())
                    record[p.Name] = ModelLifecycle.Decode(p.Value);
                docs.Add(record);
            }
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            throw new ExecutorError("EXECUTOR_SNAPSHOT_UNREADABLE",
                $"snapshot unreadable: {ex.Message}");
        }
        return docs;
    }

    // ----------------------------------------------------------- lifecycle --

    private (ModelLifecycle lifecycle, string dir) LifecycleOf(string modelId)
    {
        string directory = Path.GetFullPath(
            Path.Combine(_toolRoot, LifecycleRelative, modelId));
        if (!directory.StartsWith(_toolRoot + Path.DirectorySeparatorChar,
                                  StringComparison.Ordinal))
            throw new ExecutorError("EXECUTOR_LIFECYCLE_SCOPE_DENIED",
                "lifecycle path escapes tool root");
        return (ModelLifecycle.LoadOrCreate(directory, modelId), directory);
    }

    private void LifecycleAdvance(ModelLifecycle lifecycle, string target, string reason)
    {
        if (lifecycle.State == target) return;
        try
        {
            if (lifecycle.State == "FAILED")
                lifecycle.Transition("INITIALIZED", "recovery after failure");
            if (lifecycle.State == "UNINITIALIZED")
                lifecycle.Transition("INITIALIZED", "bootstrap");
            lifecycle.Transition(target, reason);
        }
        catch (ArgumentException)
        {
            return; // bookkeeping denial must not abort governed training
        }
        lifecycle.Save(Path.Combine(_toolRoot, LifecycleRelative, lifecycle.ModelId));
    }

    private void LifecycleFail(Dictionary<string, object?> cfg, string reason)
    {
        try
        {
            string modelId = TransformerTrainingRepository.Str(cfg, "model_id")
                             ?? XcPaths.ModelId;
            var (lifecycle, dir) = LifecycleOf(modelId);
            lifecycle.Fail(reason.Length > 200 ? reason[..200] : reason);
            lifecycle.Save(dir);
        }
        catch { /* bookkeeping */ }
    }

    private Dictionary<string, object?> FailJob(string jobId, string code, string message)
    {
        try
        {
            return _repo.TransitionTrainingJob(
                jobId, "failed", errorCode: code, errorMessage: message);
        }
        catch (ArgumentException)
        {
            return _repo.JobRow(jobId) ?? new Dictionary<string, object?>();
        }
    }

    // ------------------------------------------------------ native chain --

    /// <summary>Read the model config object out of a bundle manifest.</summary>
    private static Dictionary<string, object?> BundleConfig(string bundleDir)
    {
        string manifestPath = Path.Combine(bundleDir, "manifest.json");
        if (!File.Exists(manifestPath))
            throw new ExecutorError("EXECUTOR_BUNDLE_MANIFEST_MISSING",
                $"bundle manifest missing: {manifestPath}");
        using var doc = JsonDocument.Parse(File.ReadAllText(manifestPath));
        if (!doc.RootElement.TryGetProperty("config", out var cfg) ||
            cfg.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("EXECUTOR_BUNDLE_MANIFEST_MISSING",
                "bundle manifest has no config object");
        var map = new Dictionary<string, object?>();
        foreach (var p in cfg.EnumerateObject())
            map[p.Name] = ModelLifecycle.Decode(p.Value);
        return map;
    }

    /// <summary>Read the XCN1..XCN10 header into a trainer ``model``
    /// config dict — sole implementation lives in <see cref="XcnHeader"/>
    /// (field order must match ``xct_ckpt.h``).</summary>
    private static Dictionary<string, object?> XcnConfig(string ckptPath)
        => XcnHeader.ReadConfig(ckptPath);

    private void RunModelTool(string toolRoot, string stderrLog,
                              params string[] args)
    {
        var result = NativeTools.Run(
            NativeTools.ModelToolExe(toolRoot), args, toolRoot, stderrLog,
            timeoutS: 7200, rssBudgetMb: 0, lowPriority: true);
        if (result.ExitCode != 0)
            throw new ExecutorError("EXECUTOR_MODELTOOL_FAILED",
                $"xc_modeltool {args[0]} exited {result.ExitCode}: " +
                result.StdoutTail.Trim());
    }

    private Dictionary<string, object?> RunModelToolJson(
        string toolRoot, string stderrLog, params string[] args)
    {
        var result = NativeTools.Run(
            NativeTools.ModelToolExe(toolRoot), args, toolRoot, stderrLog,
            timeoutS: 7200, rssBudgetMb: 0, lowPriority: true);
        string tail = result.StdoutTail.Trim();
        try
        {
            using var doc = JsonDocument.Parse(
                tail[(tail.LastIndexOf('{'))..]);
            if (doc.RootElement.TryGetProperty("ok", out var ok) &&
                ok.ValueKind == JsonValueKind.False)
                throw new ExecutorError("EXECUTOR_MODELTOOL_FAILED",
                    tail.Length > 300 ? tail[..300] : tail);
            var map = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                map[p.Name] = ModelLifecycle.Decode(p.Value);
            return map;
        }
        catch (JsonException)
        {
            throw new ExecutorError("EXECUTOR_MODELTOOL_FAILED",
                $"xc_modeltool {args[0]} produced no JSON (exit " +
                $"{result.ExitCode}): {tail[..Math.Min(300, tail.Length)]}");
        }
    }

    // --------------------------------------------- resource preflight --

    /// <summary>Preflight resource gate. Every execution entry (--job,
    /// --run-jobs, the self-learning cycle) funnels through
    /// <see cref="RunJob"/>, so this one gate covers all of them. Two
    /// checks, both fail-closed with EXECUTOR_GPU_BUSY so the
    /// self-learning gpu-busy backoff (GpuBusyRecord) actually fires —
    /// previously that error code was never emitted and the backoff was
    /// dead code:
    ///   1. inference exclusion — a live model-service session owns the
    ///      machine; training must not contend with serving.
    ///   2. resource-governor concurrency budget — when the governor
    ///      pauses the training class (pressure active), the job stays
    ///      queued instead of fighting interactive/model work (A598:
    ///      training sheds first).
    /// A missing/unreadable governor state file is fail-open (trainer
    /// auto threads), per the budget contract: no state, expired or
    /// kill-switch falls back to static limits. Returns the trainer
    /// thread count derived from the training quota (0 = auto).</summary>
    private int PreflightResourceGate()
    {
        var status = PreflightStatus();
        if (status.TryGetValue("would_block", out object? wb) &&
            wb is bool blocked && blocked)
            throw new ExecutorError(
                (string)status["error_code"]!,
                (string)status["reason"]!);
        return (int)status["trainer_threads"]!;
    }

    /// <summary>Read-only view of the same gate for operators
    /// (--preflight): inference liveness, governor budget, and the
    /// derived trainer thread count. Never throws ExecutorError —
    /// a would-be refusal is reported as would_block + reason.</summary>
    public Dictionary<string, object?> PreflightStatus()
    {
        bool? inferenceActive = Collectors.InferenceActive(_toolRoot);
        var status = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-training-preflight/v1",
            ["inference_active"] = inferenceActive,
            ["governor_state"] = GovernorStatePath(),
            ["training_quota"] = null,
            ["training_state"] = null,
            ["pressure"] = null,
            ["trainer_threads"] = 0,
            ["would_block"] = false,
        };
        if (inferenceActive != false)
        {
            status["would_block"] = true;
            status["error_code"] = "EXECUTOR_GPU_BUSY";
            status["reason"] = inferenceActive == true
                ? "inference session active — training deferred"
                : "inference state unavailable — training deferred " +
                  "(fail-closed)";
            return status;
        }
        var (quota, trainingState, pressure) = GovernorTrainingBudget();
        status["training_quota"] = quota;
        status["training_state"] = trainingState;
        status["pressure"] = pressure;
        if (quota == 0 || trainingState == "paused")
        {
            status["would_block"] = true;
            status["error_code"] = "EXECUTOR_GPU_BUSY";
            status["reason"] = "resource governor paused training " +
                $"(pressure {pressure}) — job stays queued with " +
                "gpu-busy backoff";
            return status;
        }
        if (quota > 0)
            status["trainer_threads"] = Math.Clamp(quota, 1, 16);
        return status;
    }

    /// <summary>Read the governor's concurrency budget for the training
    /// class. Unknown/missing/corrupt state yields quota -1 (fail-open:
    /// trainer auto threads).</summary>
    private (int quota, string state, string pressure) GovernorTrainingBudget()
    {
        string? statePath = GovernorStatePath();
        if (statePath == null) return 0;
        string raw;
        try
        {
            raw = File.ReadAllText(statePath);
        }
        catch (Exception ex) when (ex is IOException or
            UnauthorizedAccessException)
        {
            return 0;
        }
        try
        {
            using var doc = JsonDocument.Parse(raw);
            var root = doc.RootElement;
            if (!root.TryGetProperty("concurrency_budget", out var budget) ||
                budget.ValueKind != JsonValueKind.Object)
                return 0;
            if (!budget.TryGetProperty("classes", out var classes) ||
                classes.ValueKind != JsonValueKind.Object)
                return 0;
            if (!classes.TryGetProperty("training", out var training) ||
                training.ValueKind != JsonValueKind.Object)
                return 0;
            int quota = training.TryGetProperty("quota", out var q) &&
                        q.ValueKind == JsonValueKind.Number &&
                        q.TryGetInt32(out int n) ? n : -1;
            string state = training.TryGetProperty("state", out var s) &&
                           s.ValueKind == JsonValueKind.String
                ? s.GetString() ?? "" : "";
            string pressure = budget.TryGetProperty("pressure", out var p) &&
                              p.ValueKind == JsonValueKind.String
                ? p.GetString() ?? "" : "";
            if (quota == 0 || state == "paused")
                throw new ExecutorError("EXECUTOR_GPU_BUSY",
                    "resource governor paused training " +
                    $"(pressure {pressure}) — job stays queued with " +
                    "gpu-busy backoff");
            if (quota > 0)
                return Math.Clamp(quota, 1, 16);
        }
        catch (JsonException)
        {
            return 0;
        }
        return 0;
    }

    private string? GovernorStatePath()
    {
        try
        {
            string repoRoot = Path.GetFullPath(
                Path.Combine(_toolRoot, "..", ".."));
            string candidate = Path.Combine(repoRoot, "main-system",
                "runtime", "state", "resource-governor.json");
            return File.Exists(candidate) ? candidate : null;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>Append one line to the resource-action ledger
    /// (previously declared but never written). Best-effort: ledger
    /// failure must never break governed training.</summary>
    private void AppendResourceAction(
        string jobId, string state,
        IReadOnlyDictionary<string, object?>? detail = null)
    {
        if (!ResourceActionTrainingStates.Contains(state)) return;
        try
        {
            string path = Path.Combine(_toolRoot, ResourceActionLedger);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var entry = new Dictionary<string, object?>
            {
                ["at"] = DateTime.UtcNow.ToString(
                    "yyyy-MM-dd'T'HH:mm:ss'Z'"),
                ["job_id"] = jobId,
                ["state"] = state,
            };
            if (detail != null)
                foreach (var kv in detail) entry[kv.Key] = kv.Value;
            File.AppendAllText(path,
                CanonicalJson.PlainDict(entry) + "\n",
                new System.Text.UTF8Encoding(false));
        }
        catch { /* ledger must never break governed training */ }
    }

    /// <summary>Advisory batch plan (§7-§10): bucket + free VRAM ->
    /// microbatch + grad-accum. Advisory only — the planner's
    /// activation estimate is crude, and the native trainer runs
    /// per-example with packing (batch_size/grad_accum are config
    /// intent, not trainer inputs), so the plan is attached for
    /// operators instead of enforced. Null when no VRAM budget or no
    /// measurable scale is configured.</summary>
    private static Dictionary<string, object?>? AdvisoryBatchPlan(
        Dictionary<string, object?> configuration,
        string? initBundleDir,
        int maxLen)
    {
        int vramMb = TransformerTrainingRepository.Int(
            configuration, "max_train_vram_mb");
        if (vramMb <= 0 || string.IsNullOrEmpty(initBundleDir)) return null;
        long? total = CapacityPlane.BundleTotalParams(initBundleDir);
        if (!total.HasValue || total.Value <= 0) return null;
        int bucket = TrainingAcceleration.SeqBuckets
            .FirstOrDefault(b => b >= maxLen,
                TrainingAcceleration.SeqBuckets[^1]);
        int batchSize = TransformerTrainingRepository.Int(
            configuration, "batch_size");
        int gradAccum = TransformerTrainingRepository.Int(
            configuration, "grad_accum");
        if (batchSize <= 0) batchSize = 1;
        if (gradAccum <= 0) gradAccum = 1;
        try
        {
            string payload = System.Text.Json.JsonSerializer.Serialize(
                new Dictionary<string, object?>
                {
                    ["sequence_bucket"] = bucket,
                    ["free_vram_bytes"] = (long)vramMb * 1024L * 1024L,
                    ["trainable_params"] = total.Value,
                    ["target_batch_tokens"] =
                        (long)batchSize * gradAccum * bucket,
                });
            using var doc = JsonDocument.Parse(payload);
            var plan = TrainingAcceleration.BatchPlan(doc.RootElement);
            plan["advisory_only"] = true;
            plan["note"] = "native trainer runs per-example with " +
                "packing — batch_size/grad_accum express config " +
                "intent; size the VRAM budget from microbatch_tokens";
            return plan;
        }
        catch (ExecutorError)
        {
            return null;
        }
    }

    private Dictionary<string, object?> InvokeTrainerNative(
        List<Dictionary<string, object?>> trainDocs,
        List<Dictionary<string, object?>> valDocs,
        Dictionary<string, object?> configuration,
        string outputDir,
        int trainerThreads)
    {
        string kind = (string)configuration["training_kind"]!;
        string toolRoot = _toolRoot;
        string stderrLog = Path.Combine(outputDir, "train-stderr.log");

        // -- init weights: bundle dir -> import to XCN; .xcn passthrough.
        string? initXcn = null;
        Dictionary<string, object?>? modelCfg = null;
        string? bundleManifestForExport = null;
        string? tokenizerPath = null;
        object? initObj = configuration["init_checkpoint"];
        if (initObj is string initPath)
        {
            if (Directory.Exists(initPath))
            {
                // bundle directory: manifest config + tokenizer + import.
                modelCfg = BundleConfig(initPath);
                bundleManifestForExport = Path.Combine(initPath, "manifest.json");
                string tk = Path.Combine(initPath, "tokenizer.json");
                if (File.Exists(tk)) tokenizerPath = tk;
                initXcn = Path.Combine(outputDir, "init.xcn");
                RunModelTool(toolRoot, stderrLog,
                    "import-bundle", "--bundle", initPath, "--out", initXcn);
            }
            else
            {
                initXcn = initPath; // .xcn file
                modelCfg = XcnConfig(initPath);
            }
        }
        modelCfg ??= configuration.TryGetValue("model", out object? m) &&
                     m is Dictionary<string, object?> mc
                ? mc
                : throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                    "scratch training requires configuration.model");

        // -- tokenizer: bundle first, then configured tokenizer_dir.
        if (tokenizerPath == null)
        {
            object? tkRaw = configuration.GetValueOrDefault("tokenizer_dir");
            if (tkRaw == null || tkRaw.ToString() is not { Length: > 0 })
                throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                    "tokenizer_dir is required");
            string tkDir = ResolveUnderRoot(
                tkRaw.ToString()!, "EXECUTOR_TOKENIZER_SCOPE_DENIED");
            tokenizerPath = Directory.Exists(tkDir)
                ? Path.Combine(tkDir, "tokenizer.json")
                : tkDir;
            if (!File.Exists(tokenizerPath))
                throw new ExecutorError("EXECUTOR_TOKENIZER_UNAVAILABLE",
                    $"tokenizer unavailable: {tkDir}");
        }

        // -- source rows -> tokenized ids.
        bool chat = !configuration.TryGetValue("chat_wrap", out object? cw) ||
                    cw is not bool b || b; // default true
        int maxLen = TransformerTrainingRepository.Int(configuration, "max_length");
        if (maxLen <= 0) maxLen = 256;
        string trainSrc = Path.Combine(outputDir, "train-src.jsonl");
        string trainIds = Path.Combine(outputDir, "train-ids.jsonl");
        WriteSourceRows(trainSrc, trainDocs, kind);
        var tkArgs = new List<string>
        {
            "tokenize", "--tokenizer", tokenizerPath,
            "--in", trainSrc, "--out", trainIds,
            "--max-length", maxLen.ToString(System.Globalization.CultureInfo.InvariantCulture),
        };
        if (chat && kind != "pretrain") tkArgs.Add("--chat");
        var tkOut = RunModelToolJson(toolRoot, stderrLog, tkArgs.ToArray());
        if (TransformerTrainingRepository.Int(tkOut, "rows_out") < 2)
            throw new ExecutorError("EXECUTOR_EMPTY_SPLIT",
                "tokenized train split produced no usable rows");

        // -- validation split: previously loaded for snapshot integrity
        //    and then silently discarded. Tokenize it too — fail-fast on
        //    val data that cannot tokenize — and carry the artifact into
        //    the summary for the eval tier (the native trainer consumes
        //    the train path only).
        string valSrc = Path.Combine(outputDir, "val-src.jsonl");
        string valIds = Path.Combine(outputDir, "val-ids.jsonl");
        WriteSourceRows(valSrc, valDocs, kind);
        var valTkArgs = new List<string>
        {
            "tokenize", "--tokenizer", tokenizerPath,
            "--in", valSrc, "--out", valIds,
            "--max-length", maxLen.ToString(System.Globalization.CultureInfo.InvariantCulture),
        };
        if (chat && kind != "pretrain") valTkArgs.Add("--chat");
        var valTkOut = RunModelToolJson(toolRoot, stderrLog, valTkArgs.ToArray());
        int valRows = TransformerTrainingRepository.Int(valTkOut, "rows_out");

        // -- trainer job spec.
        double deadline = TransformerTrainingRepository.Num(
            configuration, "max_train_seconds");
        string emitCkpt = Path.Combine(outputDir, "final.xcn");
        string? initBundleDir = initObj is string ip && Directory.Exists(ip)
            ? ip : null;
        var batchPlan = AdvisoryBatchPlan(configuration, initBundleDir, maxLen);
        var jobSpec = new Dictionary<string, object?>
        {
            ["task"] = kind,
            ["model"] = modelCfg,
            ["train"] = new Dictionary<string, object?>
            {
                ["lr"] = TransformerTrainingRepository.Num(configuration, "lr"),
                ["weight_decay"] = TransformerTrainingRepository.Num(
                    configuration, "weight_decay") is double wd && wd > 0 ? wd : 0.01,
                ["grad_clip"] = TransformerTrainingRepository.Num(
                    configuration, "grad_clip") is double gc && gc > 0 ? gc : 1.0,
                ["beta"] = TransformerTrainingRepository.Num(
                    configuration, "beta") is double be && be > 0 ? be : 0.1,
                ["warmup_steps"] = TransformerTrainingRepository.Int(
                    configuration, "warmup_steps"),
                ["max_steps"] = TransformerTrainingRepository.Int(
                    configuration, "max_steps"),
                ["log_every"] = Math.Max(1, TransformerTrainingRepository.Int(
                    configuration, "log_every")),
                ["checkpoint_every"] = TransformerTrainingRepository.Int(
                    configuration, "checkpoint_every"),
                ["seed"] = TransformerTrainingRepository.Int(
                    configuration, "seed") is int sd && sd > 0 ? sd : 42,
                ["deadline_s"] = deadline,
                ["lr_decay"] = TransformerTrainingRepository.Str(
                    configuration, "lr_decay") ?? "cosine",
                ["init_checkpoint"] = initXcn,
                ["emit_checkpoint"] = emitCkpt,
                ["overwrite"] = true,
                // Trainer CPU lanes follow the governor's training quota
                // (0 = trainer auto: min(8, hw), cap 16).
                ["threads"] = trainerThreads,
                // §41-§45 ParameterFreezeMap passthrough: config
                // "freeze" is a bounded list of wildcard patterns; the
                // trainer resolves it before init_params so frozen
                // params never allocate Adam moments (sparse optimizer).
                ["freeze"] = FreezePatterns(configuration),
            },
            ["data"] = new Dictionary<string, object?>
            {
                ["path"] = trainIds,
                ["format"] = kind,
                ["max_rows"] = 100000000,
                ["max_len"] = maxLen,
            },
        };
        string jobSpecPath = Path.Combine(outputDir, "job.json");
        File.WriteAllText(jobSpecPath,
            CanonicalJson.PrettyDict(jobSpec) + "\n",
            new System.Text.UTF8Encoding(false));
        string reportPath = Path.Combine(outputDir, "report.json");

        double timeoutS = TransformerTrainingRepository.Num(
            configuration, "train_process_timeout_s");
        if (timeoutS <= 0) timeoutS = 14400;
        double rssBudget = TransformerTrainingRepository.Num(
            configuration, "train_max_rss_mb");
        double sampleInterval = TransformerTrainingRepository.Num(
            configuration, "resource_sample_interval_s");
        if (sampleInterval <= 0) sampleInterval = 5;

        var started = Stopwatch.StartNew();
        var run = NativeTools.Run(
            NativeTools.TrainerExe(toolRoot),
            new[] { "--job", jobSpecPath, "--report", reportPath },
            toolRoot, stderrLog, timeoutS, rssBudget, sampleInterval,
            lowPriority: true);
        if (run.ExitCode != 0)
            throw new ExecutorError("EXECUTOR_TRAINING_FAILED",
                $"trainer exited {run.ExitCode} " +
                $"(see {stderrLog})");
        if (!File.Exists(reportPath))
            throw new ExecutorError("EXECUTOR_TRAINING_FAILED",
                "training subprocess produced no summary");

        Dictionary<string, object?> report;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(reportPath));
            report = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                report[p.Name] = ModelLifecycle.Decode(p.Value);
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            throw new ExecutorError("EXECUTOR_TRAINING_FAILED",
                $"summary unreadable: {ex.Message}");
        }
        if (report.TryGetValue("params_finite", out object? pf) &&
            pf is bool finite && !finite)
            throw new ExecutorError("EXECUTOR_TRAINING_FAILED",
                "trainer produced non-finite parameters");
        if (report.TryGetValue("nonfinite_abort", out object? nfa) &&
            nfa is bool aborted && aborted)
            throw new ExecutorError("EXECUTOR_TRAINING_FAILED",
                "trainer aborted on non-finite loss/gradients");

        // -- export the trained weights as a native bundle (the runnable +
        //    registerable artifact).
        if (!File.Exists(emitCkpt))
            throw new ExecutorError("EXECUTOR_TRAINING_FAILED",
                $"final checkpoint not emitted: {emitCkpt}");
        string bundleDir = Path.Combine(outputDir, "bundle");
        string configFrom = bundleManifestForExport
            ?? WriteScratchManifest(outputDir, modelCfg, emitCkpt);
        string weightQuant = (
                TransformerTrainingRepository.Str(
                    configuration, "weight_quant") ?? "none").Trim();
        if (weightQuant != "none" && weightQuant != "int8" &&
            weightQuant != "int4_packed")
            throw new ExecutorError("EXECUTOR_CONFIG_INVALID",
                $"unsupported weight_quant: {weightQuant}");
        var exportArgs = new List<string>
        {
            "export-bundle", "--ckpt", emitCkpt, "--out", bundleDir,
            "--config-from", configFrom, "--tokenizer", tokenizerPath,
        };
        if (weightQuant != "none") exportArgs.AddRange(new[] { "--quant", weightQuant });
        var export = RunModelToolJson(toolRoot, stderrLog, exportArgs.ToArray());

        var summary = new Dictionary<string, object?>(report)
        {
            ["final_checkpoint"] = emitCkpt,
            ["bundle_dir"] = bundleDir,
            ["bundle_manifest"] = Path.Combine(bundleDir, "manifest.json"),
            ["elapsed_seconds"] = run.ElapsedS,
            ["resource"] = new Dictionary<string, object?>
            {
                ["peak_rss_mb"] = run.PeakRssMb,
                ["rss_budget_mb"] = rssBudget > 0 ? rssBudget : null,
                ["rss_samples"] = run.RssSamples,
                ["elapsed_s"] = Math.Round(run.ElapsedS, 1),
            },
            ["requested_device"] =
                TransformerTrainingRepository.Str(configuration, "device") ?? "",
            ["executed_device"] = "cpu-native",
            ["trainer_threads"] = trainerThreads,
            ["train_ids"] = trainIds,
            ["val_ids"] = valIds,
            ["val_rows_tokenized"] = valRows,
            ["batch_plan"] = batchPlan,
            ["export"] = export,
        };
        if (report.TryGetValue("deadline_hit", out object? dh) &&
            dh is bool hit && hit)
            summary["stopped_reason"] = "deadline-exceeded";
        if (report.TryGetValue("nonfinite_abort", out object? na) &&
            na is bool nab && nab)
            summary["stopped_reason"] = "nonfinite-abort";
        if (report.TryGetValue("checkpoint_emitted", out object? ce) &&
            ce is bool emitted && !emitted)
            summary["stopped_reason"] = "checkpoint-not-emitted";
        return summary;
    }

    private static string WriteScratchManifest(
        string outputDir, Dictionary<string, object?> modelCfg,
        string emitCkpt)
    {
        // Minimal manifest wrapper so export-bundle can copy config verbatim.
        // The trainer canonicalises the job config (vision/MTP/FAI pins) before
        // serialising it into the checkpoint; the ckpt header is therefore the
        // ground truth. Overlay every serialized field so the manifest's
        // parity check compares what was actually trained, not the raw spec.
        // Non-ckpt keys (e.g. "generation") survive from the job config.
        var effective = new Dictionary<string, object?>(modelCfg);
        foreach (var kv in XcnConfig(emitCkpt)) effective[kv.Key] = kv.Value;
        string path = Path.Combine(outputDir, "model-config.json");
        var wrapper = new Dictionary<string, object?> { ["config"] = effective };
        File.WriteAllText(path, CanonicalJson.PrettyDict(wrapper) + "\n",
                          new System.Text.UTF8Encoding(false));
        return path;
    }

    private static void WriteSourceRows(
        string path, List<Dictionary<string, object?>> docs, string kind)
    {
        using var writer = new StreamWriter(path, append: false,
                                            new System.Text.UTF8Encoding(false));
        foreach (var doc in docs)
        {
            Dictionary<string, object?> row;
            if (kind == "dpo")
            {
                string prompt = (TransformerTrainingRepository.Str(doc, "prompt") ?? "").Trim();
                string chosen = (TransformerTrainingRepository.Str(doc, "chosen") ?? "").Trim();
                string rejected = (TransformerTrainingRepository.Str(doc, "rejected") ?? "").Trim();
                if (prompt.Length == 0 || chosen.Length == 0 || rejected.Length == 0)
                    continue;
                row = new Dictionary<string, object?>
                {
                    ["prompt"] = prompt, ["chosen"] = chosen, ["rejected"] = rejected,
                };
            }
            else if (kind == "pretrain")
            {
                string text = (TransformerTrainingRepository.Str(doc, "text") ?? "").Trim();
                if (text.Length == 0) continue;
                row = new Dictionary<string, object?> { ["text"] = text };
            }
            else
            {
                string prompt = (TransformerTrainingRepository.Str(doc, "prompt") ?? "").Trim();
                string completion = (TransformerTrainingRepository.Str(doc, "completion") ?? "").Trim();
                if (prompt.Length == 0 || completion.Length == 0) continue;
                row = new Dictionary<string, object?>
                {
                    ["prompt"] = prompt, ["completion"] = completion,
                };
            }
            writer.WriteLine(CanonicalJson.PlainDict(row));
        }
    }

    // ------------------------------------------------------------- run_job --

    public Dictionary<string, object?> RunJob(string jobId)
    {
        // Capability-training freeze: every queued job here mutates model
        // weights, so the stage is sealed while frozen. Queueing /
        // dataset registration stay open — only execution is gated.
        // SINGLE_CAPABILITY_RECOVERY opens the queue for jobs whose
        // declared capability matches active_capability; GuardJob in
        // NormalizeConfiguration denies everything else.
        var freezePolicy = SelfLearningPolicy.Load(_toolRoot);
        if (freezePolicy.CapabilityTrainingFrozen &&
            !CapabilityFreeze.RecoveryLaneOpen(freezePolicy) &&
            !CapabilityFreeze.CanonicalPretrainLaneOpen(freezePolicy))
            throw new ExecutorError("EXECUTOR_TRAINING_FROZEN",
                $"capability training is frozen; job {jobId} stays queued");
        var row = _repo.JobRow(jobId)
            ?? throw new ExecutorError("EXECUTOR_JOB_MISSING",
                $"job {jobId} does not exist");
        if ((string?)row["status"] != "queued")
            throw new ExecutorError("EXECUTOR_JOB_NOT_QUEUED",
                $"job is {row["status"]}, not queued");
        _repo.TransitionTrainingJob(jobId, "preflight");
        ModelLifecycle? lifecycle = null;
        Dictionary<string, object?>? configuration = null;
        try
        {
            configuration = NormalizeConfiguration(row);
            // Resource preflight before any snapshot IO: inference
            // exclusion + governor training quota. EXECUTOR_GPU_BUSY keeps
            // the job queued and arms the self-learning gpu-busy backoff.
            int trainerThreads = PreflightResourceGate();
            var (dataset, trainDocs, valDocs) =
                LoadSplitDocuments((string)row["dataset_id"]!);
            // §1 recovery lane defense-in-depth: under
            // SINGLE_CAPABILITY_RECOVERY every document carrying a
            // capability tag must name the active capability — a mixed
            // dataset is a multi-capability job and is denied.
            {
                var pol = SelfLearningPolicy.Load(_toolRoot);
                if (CapabilityFreeze.RecoveryLaneOpen(pol))
                {
                    foreach (var d in trainDocs.Concat(valDocs))
                    {
                        string? dc = TransformerTrainingRepository
                            .Str(d, "capability");
                        if (dc != null && dc.Length > 0 &&
                            !string.Equals(dc, pol.ActiveCapability,
                                           StringComparison.OrdinalIgnoreCase))
                            throw new ExecutorError(
                                "MULTI_CAPABILITY_TRAINING_DENIED",
                                $"dataset document carries capability " +
                                $"'{dc}', not '{pol.ActiveCapability}'");
                    }
                }
            }

            var (lc, lcDir) = LifecycleOf((string)configuration["model_id"]!);
            lifecycle = lc;
            LifecycleAdvance(lifecycle,
                (string)configuration["training_kind"]! == "pretrain"
                    ? "PRETRAINING" : "SFT_TRAINING",
                $"job {jobId} started");
            _repo.TransitionTrainingJob(jobId, "training");

            string outputDir = Path.GetFullPath(
                Path.Combine(_jobsRoot, jobId));
            if (!outputDir.StartsWith(_jobsRoot + Path.DirectorySeparatorChar,
                                      StringComparison.Ordinal))
                throw new ExecutorError("EXECUTOR_OUTPUT_SCOPE_DENIED",
                    "job output dir escapes jobs root");
            Directory.CreateDirectory(outputDir);
            AppendResourceAction(jobId,
                (string)configuration["training_kind"]! == "pretrain"
                    ? "PRETRAINING" : "SFT_TRAINING",
                new Dictionary<string, object?>
                {
                    ["trainer_threads"] = trainerThreads,
                    ["device"] = TransformerTrainingRepository.Str(
                        configuration, "device") ?? "",
                    ["max_train_seconds"] =
                        TransformerTrainingRepository.Num(
                            configuration, "max_train_seconds"),
                    ["max_train_vram_mb"] =
                        TransformerTrainingRepository.Int(
                            configuration, "max_train_vram_mb"),
                });
            Dictionary<string, object?> summary;
            try
            {
                summary = InvokeTrainerNative(
                    trainDocs, valDocs, configuration, outputDir,
                    trainerThreads);
            }
            catch (ExecutorError)
            {
                throw;
            }
            catch (Exception exc)
            {
                throw new ExecutorError("EXECUTOR_TRAINING_FAILED",
                    exc.Message.Length > 500 ? exc.Message[..500] : exc.Message);
            }

            _repo.TransitionTrainingJob(jobId, "validating");
            string finalCheckpoint = Path.GetFullPath(
                TransformerTrainingRepository.Str(summary, "final_checkpoint")
                ?? Path.Combine(outputDir, "final.xcn"));
            string artifactPath = Path.GetFullPath(
                TransformerTrainingRepository.Str(summary, "bundle_manifest")
                ?? Path.Combine(outputDir, "bundle", "manifest.json"));
            if (!finalCheckpoint.StartsWith(
                    _repo.ToolRoot + Path.DirectorySeparatorChar,
                    StringComparison.Ordinal) ||
                !File.Exists(finalCheckpoint))
                throw new ExecutorError("EXECUTOR_VALIDATION_FAILED",
                    "final checkpoint escapes tool root or is missing");
            if (!File.Exists(artifactPath))
                throw new ExecutorError("EXECUTOR_VALIDATION_FAILED",
                    "bundle manifest missing after export");
            VerifyBundleIntegrity(artifactPath);

            var completed = _repo.TransitionTrainingJob(
                jobId, "completed", outputPath: artifactPath);
            RecordExecutionAudit(completed, summary);
            try
            {
                lifecycle!.RegisterArtifact("weights", artifactPath,
                    metadata: new Dictionary<string, object?>
                    {
                        ["job_id"] = jobId,
                        ["dataset_id"] = row["dataset_id"],
                        ["steps"] = summary.GetValueOrDefault("steps"),
                        ["bundle_dir"] = Path.GetDirectoryName(artifactPath),
                    },
                    activate: false);
                LifecycleAdvance(lifecycle,
                    (string)configuration["training_kind"]! == "pretrain"
                        ? "PRETRAINED" : "INSTRUCT_READY",
                    $"job {jobId} completed");
            }
            catch { /* lifecycle bookkeeping failure must not void training */ }
            return new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["job"] = completed,
                ["output_path"] = artifactPath,
                ["summary"] = summary,
                ["automatic_weight_replacement"] = Convert.ToInt32(
                    _repo.RuntimeModelState()
                        .GetValueOrDefault("automatic_weight_replacement") ?? 0),
            };
        }
        catch (ExecutorError exc)
        {
            if (lifecycle != null && configuration != null)
                LifecycleFail(configuration, exc.ErrorCode);
            var failed = FailJob(jobId, exc.ErrorCode, exc.Message);
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["job"] = failed,
                ["error_code"] = exc.ErrorCode,
                ["error_message"] = exc.Message,
            };
        }
        catch (Exception exc)
        {
            var failed = FailJob(jobId, "EXECUTOR_INTERNAL_ERROR",
                exc.Message.Length > 500 ? exc.Message[..500] : exc.Message);
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["job"] = failed,
                ["error_code"] = "EXECUTOR_INTERNAL_ERROR",
                ["error_message"] = exc.Message.Length > 500
                    ? exc.Message[..500] : exc.Message,
            };
        }
    }

    /// <summary>Verify manifest weights_sha256 == sha256(weights.bin).</summary>
    private static void VerifyBundleIntegrity(string manifestPath)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(manifestPath));
        if (!doc.RootElement.TryGetProperty("weights_sha256", out var w))
            throw new ExecutorError("EXECUTOR_VALIDATION_FAILED",
                "bundle manifest lacks weights_sha256");
        string declared = w.GetString() ?? "";
        string weightsPath = Path.Combine(
            Path.GetDirectoryName(manifestPath)!, "weights.bin");
        if (!File.Exists(weightsPath))
            throw new ExecutorError("EXECUTOR_VALIDATION_FAILED",
                "bundle weights.bin missing");
        string actual = TransformerTrainingRepository.Sha256File(weightsPath);
        if (!string.Equals(actual, declared, StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("EXECUTOR_VALIDATION_FAILED",
                "bundle weights SHA-256 mismatch");
    }

    private void RecordExecutionAudit(
        IReadOnlyDictionary<string, object?> job,
        IReadOnlyDictionary<string, object?> summary)
    {
        using var db = Pg.Connect(_repo.Schema, autocommit: false);
        try
        {
            var runtime = _repo.RuntimeModelState();
            _repo.AppendAudit(db,
                eventType: "training-job-executed",
                entityType: "training-job",
                entityId: (string)job["job_id"]!,
                payload: new Dictionary<string, object?>
                {
                    ["dataset_id"] = job["dataset_id"],
                    ["configuration_sha256"] = job["configuration_sha256"],
                    ["steps"] = summary.GetValueOrDefault("steps"),
                    ["tokens_seen"] = summary.GetValueOrDefault("tokens_seen"),
                    ["final_loss"] = summary.GetValueOrDefault("loss_last"),
                    ["eval"] = summary.GetValueOrDefault("eval")
                               ?? new Dictionary<string, object?>(),
                    ["resource"] = summary.GetValueOrDefault("resource")
                                   ?? new Dictionary<string, object?>(),
                    ["output_path"] = job["output_path"]?.ToString() ?? "",
                    ["automatic_weight_replacement"] = Convert.ToInt32(
                        runtime.GetValueOrDefault("automatic_weight_replacement") ?? 0),
                });
            db.Commit();
        }
        catch
        {
            db.Rollback();
            throw;
        }
    }

    public Dictionary<string, object?>? RunNext()
    {
        var queued = _repo.QueuedJobs(1);
        return queued.Count == 0 ? null : RunJob((string)queued[0]["job_id"]!);
    }
}
