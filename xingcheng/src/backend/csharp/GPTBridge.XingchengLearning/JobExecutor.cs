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
        // denied before any weight work is scheduled. Capability
        // unification §16/§82: the id is validated against the canonical
        // CapabilityRegistry first (unknown ids fail closed), then the
        // progression policy enforces sequence order.
        if (kind == "sft")
            CapabilityProgressionPolicy.GuardAdmission(
                _toolRoot, (string)cfg["capability"]!);


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

    /// <summary>Outcome of the governed resource preflight: the trainer
    /// thread cap, the GPU admission record, and the live ResourceGrant
    /// the caller must release when the workload ends. The grant is the
    /// authority — every ceiling downstream reads it, never hardware.</summary>
    internal sealed class PreflightDecision
    {
        public int Threads;
        public GpuPlan Gpu = null!;
        public ResourceGrant? Grant;
        public ResourceGovernorClient? Client;
        public ResourceRequest? Request;
        public XingchengLocalResourceAllocator.LaneAllocation? Lane;
        public string RequestTimeoutNote = "";

        /// <summary>End-of-workload lifecycle: append the
        /// star-resource-usage-receipt/v1 and release the grant (§54/§58).
        /// Best-effort — ledger failure must never break the lane.</summary>
        public void Finish(string result,
                           IReadOnlyDictionary<string, object?>? summary)
        {
            if (Client == null || Grant == null) return;
            try
            {
                var receipt = new ResourceUsageReceipt
                {
                    GrantId = Grant.GrantId,
                    WorkloadId = Request?.WorkloadId ?? "",
                    Result = result,
                };
                if (summary != null)
                {
                    if (summary.TryGetValue("elapsed_seconds",
                            out var es) && es is double eds)
                        receipt.WallTimeS = eds;
                    if (summary.TryGetValue("resource", out var res) &&
                        res is IReadOnlyDictionary<string, object?> r &&
                        r.TryGetValue("peak_rss_mb", out var pm) &&
                        pm is double pmb)
                        receipt.PeakRamBytes = (long)(pmb * 1048576.0);
                }
                Client.AppendUsageReceipt(receipt);
            }
            catch { /* receipt is best-effort audit */ }
            try { Client.Release(Grant); }
            catch { /* release is best-effort */ }
        }
    }

    /// <summary>Preflight resource gate. Every execution entry (--job,
    /// --run-jobs, the self-learning cycle) funnels through
    /// <see cref="RunJob"/>, so this one gate covers all of them.
    /// Chain (spec §44 admission):
    ///   1. inference exclusion — a live model-service session owns the
    ///      machine; training must not contend with serving;
    ///   2. governor fast-path — paused/zero-quota stays queued without
    ///      paying the request round-trip (still fail-closed);
    ///   3. star-resource-request/v1 → main-system governor →
    ///      DENIED/DEFERRED/PARTIAL/GRANTED/REVOKED (spec §7); governor
    ///      unavailable → fail-closed unless XC_DEV_STATIC_GRANT=1
    ///      (spec §75–§77: StaticLocalGrant is dev/test only).
    /// Denied/deferred/unavailable carry typed resource codes (§80); the
    /// self-learning backoff treats them exactly like EXECUTOR_GPU_BUSY —
    /// the job stays queued, never runs ungranted.</summary>
    private PreflightDecision PreflightResourceGate(
        string jobId, Dictionary<string, object?> configuration)
    {
        var status = PreflightStatus();
        if (status.TryGetValue("would_block", out object? wb) &&
            wb is bool blocked && blocked)
            throw new ExecutorError(
                (string)status["error_code"]!,
                (string)status["reason"]!);
        var decision = AcquireResourceGrant(jobId, configuration);
        // AC §10: production without a bound grant fails closed — a
        // null grant here means a future code path returned a decision
        // without binding; name it RESOURCE_GRANT_REQUIRED, not a
        // null-deref.
        if (decision.Grant == null)
            throw new ExecutorError(ResourceErrors.GrantRequired,
                "resource preflight produced no grant — execution " +
                "denied (fail-closed)");
        var grant = decision.Grant;
        // §30/§34-§35: lane resources are allocated inside the grant
        // envelope by XingchengLocalResourceAllocator — the lane never
        // sizes itself directly from the grant (and never from the
        // hardware). With the serial lane cap the request list holds a
        // single training lane; multi-lane pilots append more entries.
        var req = decision.Request;
        decision.Lane = XingchengLocalResourceAllocator.Allocate(
            grant,
            new[]
            {
                new XingchengLocalResourceAllocator.LaneRequest
                {
                    LaneId = jobId,
                    Kind = "training",
                    CpuThreads = req?.PreferredCpuThreads
                        ?? grant.CpuThreadsMax,
                    RamBytes = req?.PreferredRamBytes ?? 0,
                    WantsGpu = req?.GpuOptional ?? false,
                    VramBytes = req?.PreferredVramBytes ?? 0,
                    Priority = 100,
                },
            })[0];
        decision.Threads = Math.Clamp(decision.Lane.CpuThreads, 1, 16);
        decision.Gpu = ResolveGpuPlan(configuration, grant,
            decision.Lane);
        return decision;
    }

    /// <summary>Build the job's star-resource-request/v1 and submit it
    /// through the client. Xingcheng never states raw hardware needs to
    /// itself — the governor decides what the machine can give (§5).</summary>
    private PreflightDecision AcquireResourceGrant(
        string jobId, Dictionary<string, object?> configuration)
    {
        var decision = new PreflightDecision();
        string device = (TransformerTrainingRepository.Str(
            configuration, "device") ?? "").Trim().ToLowerInvariant();
        int prefThreads = TransformerTrainingRepository.Int(
            configuration, "trainer_threads");
        int prefVramMb = TransformerTrainingRepository.Int(
            configuration, "max_train_vram_mb");
        int minVramMb = TransformerTrainingRepository.Int(
            configuration, "train_cuda_min_free_mb");
        long prefRssMb = (long)TransformerTrainingRepository.Num(
            configuration, "train_max_rss_mb");
        var request = new ResourceRequest
        {
            RequestId = "rr-" + jobId,
            WorkloadId = jobId,
            CandidateId = TransformerTrainingRepository.Str(
                configuration, "model_id") ?? "",
            // §63: the request carries only a canonical capability_id
            // — a free string can never reach the governor.
            Capability = CapabilityResolver.Require(
                TransformerTrainingRepository.Str(
                    configuration, "capability") ?? ""),
            WorkloadClass = "training",
            Priority = TransformerTrainingRepository.Int(
                configuration, "priority"),
            MinimumCpuThreads = 1,
            PreferredCpuThreads = prefThreads > 0 ? prefThreads : 16,
            MinimumRamBytes = 0,
            PreferredRamBytes = prefRssMb > 0 ? prefRssMb << 20 : 0,
            GpuOptional = device is "cuda" or "gpu" or "auto",
            GpuRequired = false,
            MinimumVramBytes = 0,
            PreferredVramBytes = prefVramMb > 0 ? (long)prefVramMb << 20 : 0,
            IoReadBudget = 0,
            IoWriteBudget = 0,
            ExpectedDurationS = TransformerTrainingRepository.Num(
                configuration, "max_train_seconds"),
            Checkpointable = true,
            Preemptible = true,
        };
        var client = new ResourceGovernorClient(_toolRoot);
        decision.Client = client;
        decision.Request = request;
        int waitS = TransformerTrainingRepository.Int(
            configuration, "grant_wait_s");
        var reply = client.Request(request, waitS > 0 ? waitS : 60);
        if (reply.Unavailable)
        {
            // §75: governor down -> autonomous training denied,
            // fail-closed. The only escape is the explicit dev/test
            // StaticLocalGrant (§76-§77) — never a production fallback.
            decision.Grant = ResourceGovernorClient.StaticLocalGrant();
            if (decision.Grant == null)
                throw new ExecutorError(
                    ResourceErrors.GovernorUnavailable,
                    "resource governor unavailable — training denied " +
                    "(fail-closed; XC_DEV_STATIC_GRANT=1 enables the " +
                    "dev/test static grant)");
            return decision;
        }
        switch (reply.Response)
        {
            case GrantResponseKind.Granted:
            case GrantResponseKind.Partial:
                decision.Grant = reply.Grant;
                if (decision.Grant == null)
                    throw new ExecutorError(ResourceErrors.RequestDenied,
                        "governor replied granted without grant payload");
                return decision;
            case GrantResponseKind.Denied:
                throw new ExecutorError(ResourceErrors.RequestDenied,
                    $"resource request denied: {reply.Reason}");
            case GrantResponseKind.Revoked:
                throw new ExecutorError(ResourceErrors.GrantRevoked,
                    $"resource grant revoked: {reply.Reason}");
            default:
                throw new ExecutorError(ResourceErrors.RequestDeferred,
                    $"resource request deferred: {reply.Reason}");
        }
    }

    /// <summary>Read-only view of the same gate for operators
    /// (--preflight): inference liveness, governor budget, and the
    /// derived trainer thread count. Never throws ExecutorError —
    /// a would-be refusal is reported as would_block + reason.</summary>
    public Dictionary<string, object?> PreflightStatus()
    {
        bool? inferenceActive = Collectors.InferenceActive(_toolRoot);
        var (governorMode, gpuEnabled, vramPct, vramMb) =
            GovernorGpuPolicy();
        var status = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-training-preflight/v1",
            ["inference_active"] = inferenceActive,
            ["governor_state"] = GovernorStatePath(),
            ["governor_mode"] = governorMode,
            ["gpu"] = new Dictionary<string, object?>
            {
                ["gpu_enabled"] = gpuEnabled,
                ["vram_budget_percent"] = vramPct,
                ["vram_budget_mb"] = vramMb,
                ["probe"] = ProbeCuda()?.ToDict(),
            },
            ["training_quota"] = null,
            ["training_state"] = null,
            ["pressure"] = null,
            ["trainer_threads"] = 0,
            ["would_block"] = false,
            // Runtime Host visibility (read-only): whether a single Active
            // Model owner currently holds the machine. Informational only —
            // enforcement lives in the host lease itself.
            ["runtime_host"] = RuntimeHostSnapshot(),
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

    /// <summary>Read-only snapshot of the Runtime Host Active Model
    /// record (star-runtime-host/v1), if one exists. Best-effort: any
    /// read/parse failure yields null — visibility must never break the
    /// preflight gate.</summary>
    private Dictionary<string, object?>? RuntimeHostSnapshot()
    {
        try
        {
            string path = Path.Combine(
                _toolRoot, "xingcheng", "runtime", "ipc", "runtime-host.json");
            if (!File.Exists(path)) return null;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object ||
                !root.TryGetProperty("format", out var fmt) ||
                fmt.GetString() != "star-runtime-host/v1")
                return new Dictionary<string, object?>
                {
                    ["active"] = false,
                    ["reason"] = "descriptor-format-mismatch",
                };
            long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            long heartbeat = root.TryGetProperty("heartbeat_at_s", out var hb) &&
                             hb.ValueKind == JsonValueKind.Number &&
                             hb.TryGetInt64(out long h) ? h : 0;
            long lease = root.TryGetProperty("lease_timeout_s", out var lt) &&
                         lt.ValueKind == JsonValueKind.Number &&
                         lt.TryGetInt64(out long l) ? l : 0;
            var slots = new Dictionary<string, object?>();
            if (root.TryGetProperty("runtimes", out var rs) &&
                rs.ValueKind == JsonValueKind.Array)
                foreach (var s in rs.EnumerateArray())
                {
                    if (!s.TryGetProperty("kind", out var k) ||
                        !s.TryGetProperty("state", out var st))
                        continue;
                    slots[k.GetString() ?? "?"] = st.GetString();
                }
            return new Dictionary<string, object?>
            {
                ["active"] = true,
                ["owner_pid"] = root.TryGetProperty("owner_pid", out var op) &&
                                op.ValueKind == JsonValueKind.Number &&
                                op.TryGetInt32(out int pid) ? pid : null,
                ["owner_exe"] = root.TryGetProperty("owner_exe", out var oe) &&
                                oe.ValueKind == JsonValueKind.String
                    ? oe.GetString() : null,
                ["bundle_dir"] = root.TryGetProperty("bundle_dir", out var bd) &&
                                 bd.ValueKind == JsonValueKind.String
                    ? bd.GetString() : null,
                ["heartbeat_age_s"] = Math.Max(0, now - heartbeat),
                ["lease_expired"] = lease > 0 && now - heartbeat > lease,
                ["slots"] = slots,
            };
        }
        catch
        {
            return null;
        }
    }

    /// <summary>Read the governor's concurrency budget for the training
    /// class. Unknown/missing/corrupt state yields quota -1 (fail-open:
    /// trainer auto threads).</summary>
    private (int quota, string state, string pressure) GovernorTrainingBudget()
    {
        string? statePath = GovernorStatePath();
        if (statePath == null) return (-1, "", "");
        string raw;
        try
        {
            raw = File.ReadAllText(statePath);
        }
        catch (Exception ex) when (ex is IOException or
            UnauthorizedAccessException)
        {
            return (-1, "", "");
        }
        try
        {
            using var doc = JsonDocument.Parse(raw);
            var root = doc.RootElement;
            if (!root.TryGetProperty("concurrency_budget", out var budget) ||
                budget.ValueKind != JsonValueKind.Object)
                return (-1, "", "");
            if (!budget.TryGetProperty("classes", out var classes) ||
                classes.ValueKind != JsonValueKind.Object)
                return (-1, "", "");
            if (!classes.TryGetProperty("training", out var training) ||
                training.ValueKind != JsonValueKind.Object)
                return (-1, "", "");
            int quota = training.TryGetProperty("quota", out var q) &&
                        q.ValueKind == JsonValueKind.Number &&
                        q.TryGetInt32(out int n) ? n : -1;
            string state = training.TryGetProperty("state", out var s) &&
                           s.ValueKind == JsonValueKind.String
                ? s.GetString() ?? "" : "";
            string pressure = budget.TryGetProperty("pressure", out var pr) &&
                              pr.ValueKind == JsonValueKind.String
                ? pr.GetString() ?? "" : "";
            return (quota, state, pressure);
        }
        catch (JsonException)
        {
            return (-1, "", "");
        }
    }

    /// <summary>GPU admission decision for one job. The trainer's CUDA
    /// lane is opt-in (XINGCHENG_TRAINER_CUDA_OPT); this record is the
    /// audit trail of why a requested device did or did not get it.</summary>
    internal sealed class GpuPlan
    {
        public bool Requested;         // configuration["device"] asks cuda
        public string Mode = "";       // governor mode observed
        public bool GpuEnabled;        // rules modes[mode].gpu_enabled
        public int VramBudgetPercent;  // rules modes[mode].vram_budget_percent
        public bool? ProbeAvailable;   // xc_modeltool probe-cuda
        public long ProbeFreeMb;
        public long ProbeTotalMb;
        public bool Admitted;          // -> XINGCHENG_TRAINER_CUDA_OPT=1
        public string Reason = "";
        /// <summary>§41 resolved VRAM cap (min driver-free, grant cap);
        /// 0 when no grant resolved one.</summary>
        public long VramBudgetMb;

        public Dictionary<string, object?> ToDict() => new()
        {
            ["requested"] = Requested,
            ["governor_mode"] = Mode.Length > 0 ? Mode : null,
            ["gpu_enabled"] = GpuEnabled,
            ["vram_budget_percent"] = VramBudgetPercent,
            ["vram_budget_mb_resolved"] =
                VramBudgetMb > 0 ? VramBudgetMb : null,
            ["probe_available"] = ProbeAvailable,
            ["probe_vram_free_mb"] = ProbeAvailable == true ? ProbeFreeMb : null,
            ["probe_vram_total_mb"] = ProbeAvailable == true ? ProbeTotalMb : null,
            ["admitted"] = Admitted,
            ["reason"] = Reason,
        };
    }

    /// <summary>Resolve the GPU admission for a job's requested device.
    /// Chain: device request -> lane.gpu_allowed (the allocator holds
    /// the single GPU lease inside the grant envelope, §31; the grant is
    /// still the authority, governor mode is informational) -> live
    /// probe-cuda -> effective VRAM = min(driver_free, grant vram cap)
    /// (spec §40–§41: free memory is evidence, never permission). A
    /// denied request is never fatal — the trainer simply runs its CPU
    /// lanes; denial is recorded for audit.</summary>
    private GpuPlan ResolveGpuPlan(
        Dictionary<string, object?> configuration, ResourceGrant? grant,
        XingchengLocalResourceAllocator.LaneAllocation? lane = null)
    {
        var plan = new GpuPlan();
        string req = (TransformerTrainingRepository.Str(
            configuration, "device") ?? "").Trim().ToLowerInvariant();
        plan.Requested = req is "cuda" or "gpu" or "auto";
        var (mode, enabled, pct, _) = GovernorGpuPolicy();
        plan.Mode = mode;
        plan.GpuEnabled = enabled;
        plan.VramBudgetPercent = pct;
        if (!plan.Requested)
        {
            plan.Reason = "device-not-requested";
            return plan;
        }
        if (grant != null && !grant.GpuAllowed)
        {
            plan.Reason = "grant-gpu-not-allowed";
            return plan;
        }
        // §31/G: even with grant.GpuAllowed the allocator may have
        // leased the GPU to a higher-priority lane — the lease, not
        // the raw grant bit, decides this lane.
        if (lane != null && !lane.GpuAllowed)
        {
            plan.Reason = lane.Note.Length > 0
                ? $"lane-{lane.Note}"
                : "lane-gpu-lease-not-held";
            return plan;
        }
        if (!enabled)
        {
            plan.Reason = mode.Length > 0
                ? $"governor-mode-{mode}-gpu-disabled"
                : "governor-gpu-policy-unavailable";
            return plan;
        }
        var probe = ProbeCuda();
        if (probe == null || probe.Available != true)
        {
            plan.ProbeAvailable = probe?.Available;
            plan.Reason = "cuda-probe-unavailable";
            return plan;
        }
        plan.ProbeAvailable = true;
        plan.ProbeFreeMb = probe.FreeMb;
        plan.ProbeTotalMb = probe.TotalMb;
        // Headroom: the resident w/m/v AdamW lane needs ~3x params fp32
        // plus context; train_cuda_min_free_mb tunes the floor (default
        // 2048MB covers the ~1.2GB optimizer footprint of a 100M model).
        // §41: effective_vram_budget = min(driver_available, grant cap);
        // with no grant the mode's vram_budget_percent is the cap.
        int requiredMb = TransformerTrainingRepository.Int(
            configuration, "train_cuda_min_free_mb");
        if (requiredMb <= 0) requiredMb = 2048;
        long capMb;
        if (grant != null)
            capMb = grant.EffectiveVramBytes(
                probe.FreeMb << 20, probe.TotalMb << 20) >> 20;
        else
            capMb = pct > 0 && probe.TotalMb > 0
                ? probe.TotalMb * pct / 100 : probe.FreeMb;
        if (requiredMb > capMb) requiredMb = (int)Math.Max(0, capMb);
        if (probe.FreeMb < requiredMb || requiredMb <= 0)
        {
            plan.Reason =
                $"vram-headroom-{probe.FreeMb}mb-below-{requiredMb}mb" +
                (grant != null ? "-grant-capped" : "");
            return plan;
        }
        plan.Admitted = true;
        plan.Reason = "admitted";
        plan.VramBudgetMb = capMb;
        return plan;
    }

    /// <summary>Governor mode -> GPU policy. The mode comes from the
    /// governor state file; gpu_enabled/vram_budget_percent are resolved
    /// from the same rules file the governor consumed (colocated under
    /// main-system/config). Fail-closed: any missing piece disables GPU.</summary>
    private (string mode, bool gpuEnabled, int vramPct, long vramMb)
        GovernorGpuPolicy()
    {
        string? statePath = GovernorStatePath();
        if (statePath == null) return ("", false, 0, 0);
        try
        {
            using var state = JsonDocument.Parse(
                File.ReadAllText(statePath));
            string mode = "";
            if (state.RootElement.TryGetProperty("mode", out var m) &&
                m.ValueKind == JsonValueKind.String)
                mode = m.GetString() ?? "";
            if (mode.Length == 0 &&
                state.RootElement.TryGetProperty("features", out var f) &&
                f.ValueKind == JsonValueKind.Object &&
                f.TryGetProperty("mode", out var fm) &&
                fm.ValueKind == JsonValueKind.String)
                mode = fm.GetString() ?? "";
            string rulesPath = Path.GetFullPath(Path.Combine(
                Path.GetDirectoryName(statePath)!, "..", "..", "config",
                "resource-governor-rules.json"));
            if (!File.Exists(rulesPath) || mode.Length == 0)
                return (mode, false, 0, 0);
            using var rules = JsonDocument.Parse(
                File.ReadAllText(rulesPath));
            if (!rules.RootElement.TryGetProperty("modes", out var modes) ||
                modes.ValueKind != JsonValueKind.Object ||
                !modes.TryGetProperty(mode, out var mo) ||
                mo.ValueKind != JsonValueKind.Object)
                return (mode, false, 0, 0);
            bool en = mo.TryGetProperty("gpu_enabled", out var ge) &&
                      ge.ValueKind == JsonValueKind.True;
            int pct = mo.TryGetProperty("vram_budget_percent", out var vp) &&
                      vp.ValueKind == JsonValueKind.Number &&
                      vp.TryGetInt32(out int n) ? Math.Clamp(n, 0, 100) : 0;
            long mb = 0;
            if (en && pct > 0)
            {
                var probe = ProbeCuda();
                if (probe?.TotalMb > 0)
                    mb = probe.TotalMb * pct / 100;
            }
            return (mode, en, pct, mb);
        }
        catch (Exception ex) when (ex is IOException or JsonException or
            UnauthorizedAccessException)
        {
            return ("", false, 0, 0);
        }
    }

    /// <summary>Live CUDA probe through the governed model tool
    /// (probe-cuda -> xcuda_probe). Null when the tool is missing or the
    /// probe cannot run — callers treat null as unavailable.</summary>
    private GpuProbe? ProbeCuda()
    {
        try
        {
            string tmp = Path.Combine(Path.GetTempPath(),
                $"xc-probe-cuda-{Environment.ProcessId}.log");
            var run = NativeTools.Run(
                NativeTools.ModelToolExe(_toolRoot),
                new[] { "probe-cuda" }, _toolRoot, tmp,
                timeoutS: 60, rssBudgetMb: 0, lowPriority: true);
            try { File.Delete(tmp); } catch { }
            if (run.ExitCode != 0) return null;
            // The probe emits one JSON document as its last stdout line;
            // nested braces make the shared tail-slice helper pick the
            // wrong root, so parse the last non-empty line directly.
            string? line = run.StdoutTail
                .Split('\n', StringSplitOptions.RemoveEmptyEntries |
                              StringSplitOptions.TrimEntries)
                .LastOrDefault(l => l.StartsWith('{'));
            if (line == null) return null;
            using var doc = JsonDocument.Parse(line);
            if (!doc.RootElement.TryGetProperty("cuda", out var c) ||
                c.ValueKind != JsonValueKind.Object)
                return null;
            return new GpuProbe
            {
                Available = c.TryGetProperty("available", out var a) &&
                            a.ValueKind == JsonValueKind.True,
                FreeMb = c.TryGetProperty("vram_free_mb", out var fb) &&
                         fb.TryGetInt64(out long f1) ? f1 : 0,
                TotalMb = c.TryGetProperty("vram_total_mb", out var tb) &&
                          tb.TryGetInt64(out long t1) ? t1 : 0,
                CcMajor = c.TryGetProperty("cc_major", out var cm) &&
                          cm.TryGetInt32(out int cma) ? cma : 0,
                CcMinor = c.TryGetProperty("cc_minor", out var cn) &&
                          cn.TryGetInt32(out int cmi) ? cmi : 0,
            };
        }
        catch (Exception ex) when (ex is ExecutorError or IOException or
            JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private sealed class GpuProbe
    {
        public bool Available;
        public long FreeMb;
        public long TotalMb;
        public int CcMajor;
        public int CcMinor;

        public Dictionary<string, object?> ToDict() => new()
        {
            ["available"] = Available,
            ["vram_free_mb"] = FreeMb,
            ["vram_total_mb"] = TotalMb,
            ["cc_major"] = CcMajor,
            ["cc_minor"] = CcMinor,
        };
    }

    /// <summary>Locate the governor state file by walking ancestors for
    /// main-system/runtime/state/resource-governor.json (the main
    /// checkout resolves at toolRoot/../..; worktree checkouts nest
    /// differently). Null when absent — the gate stays fail-open.</summary>
    private string? GovernorStatePath()
    {
        try
        {
            string? dir = Path.GetFullPath(_toolRoot);
            for (int i = 0; i < 4 && dir != null; i++)
            {
                string candidate = Path.Combine(dir, "main-system",
                    "runtime", "state", "resource-governor.json");
                if (File.Exists(candidate)) return candidate;
                dir = Path.GetDirectoryName(dir);
            }
            return null;
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
        int maxLen,
        ResourceGrant? grant)
    {
        int vramMb = TransformerTrainingRepository.Int(
            configuration, "max_train_vram_mb");
        // §41: the grant caps the advisory budget — config intent may
        // never exceed what the governor allowed.
        if (grant != null && grant.VramBytesMax > 0)
            vramMb = (int)Math.Min(
                vramMb > 0 ? vramMb : long.MaxValue,
                grant.VramBytesMax >> 20);
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

    /// <summary>Route a tokenize call through the resident session when
    /// one is alive; transport failure falls back to one-shot.
    /// A deterministic op failure (ok:false) mirrors the one-shot
    /// EXECUTOR_MODELTOOL_FAILED instead of falling back.</summary>
    private Dictionary<string, object?> TokenizeViaSession(
        ModelToolSession? serve, string toolRoot, string stderrLog,
        string tokenizerPath, string src, string dst, int maxLen, bool chat)
    {
        if (serve != null)
        {
            try
            {
                var (json, _) = serve.Request(new Dictionary<string, object?>
                {
                    ["op"] = "tokenize",
                    ["tokenizer"] = tokenizerPath,
                    ["in"] = src,
                    ["out"] = dst,
                    ["max_length"] = maxLen,
                    ["chat"] = chat,
                });
                if (!TransformerTrainingRepository.Truthy(
                        json.GetValueOrDefault("ok")))
                    throw new ExecutorError("EXECUTOR_MODELTOOL_FAILED",
                        TransformerTrainingRepository.Str(json, "error")
                        ?? "serve tokenize failed");
                return json;
            }
            catch (ExecutorError ex) when (
                ex.ErrorCode == "EXECUTOR_MODELTOOL_FAILED")
            {
                throw;
            }
            catch (ExecutorError)
            {
                // transport failure: fall through to one-shot
            }
        }
        var args = new List<string>
        {
            "tokenize", "--tokenizer", tokenizerPath,
            "--in", src, "--out", dst,
            "--max-length", maxLen.ToString(
                System.Globalization.CultureInfo.InvariantCulture),
        };
        if (chat) args.Add("--chat");
        return RunModelToolJson(toolRoot, stderrLog, args.ToArray());
    }

    private Dictionary<string, object?> InvokeTrainerNative(
        List<Dictionary<string, object?>> trainDocs,
        List<Dictionary<string, object?>> valDocs,
        Dictionary<string, object?> configuration,
        string outputDir,
        int trainerThreads,
        GpuPlan gpu,
        ResourceGrant? grant)
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

        // -- resident serve session: one serve process for the whole job
        //    (train+val tokenize share the cached tokenizer). Any failure
        //    falls back to one-shot per call — the session only accelerates.
        //    Released before the trainer runs: tokenize is done by then and
        //    the trainer owns the machine alone.
        string? serveBundle = initObj is string sbp && Directory.Exists(sbp)
            ? sbp : null;
        using var serve = serveBundle != null
            ? ModelToolSession.TryStart(toolRoot, serveBundle, stderrLog)
            : null;

        // -- source rows -> tokenized ids.
        bool chat = !configuration.TryGetValue("chat_wrap", out object? cw) ||
                    cw is not bool b || b; // default true
        int maxLen = TransformerTrainingRepository.Int(configuration, "max_length");
        if (maxLen <= 0) maxLen = 256;
        string trainSrc = Path.Combine(outputDir, "train-src.jsonl");
        string trainIds = Path.Combine(outputDir, "train-ids.xcb");
        WriteSourceRows(trainSrc, trainDocs, kind);
        var tkOut = TokenizeViaSession(serve, toolRoot, stderrLog,
            tokenizerPath, trainSrc, trainIds, maxLen,
            chat && kind != "pretrain");
        if (TransformerTrainingRepository.Int(tkOut, "rows_out") < 2)
            throw new ExecutorError("EXECUTOR_EMPTY_SPLIT",
                "tokenized train split produced no usable rows");

        // -- validation split: previously loaded for snapshot integrity
        //    and then silently discarded. Tokenize it too — fail-fast on
        //    val data that cannot tokenize — and carry the artifact into
        //    the summary for the eval tier (the native trainer consumes
        //    the train path only).
        string valSrc = Path.Combine(outputDir, "val-src.jsonl");
        string valIds = Path.Combine(outputDir, "val-ids.xcb");
        WriteSourceRows(valSrc, valDocs, kind);
        var valTkOut = TokenizeViaSession(serve, toolRoot, stderrLog,
            tokenizerPath, valSrc, valIds, maxLen,
            chat && kind != "pretrain");
        int valRows = TransformerTrainingRepository.Int(valTkOut, "rows_out");
        serve?.Dispose();

        // -- trainer job spec.
        double deadline = TransformerTrainingRepository.Num(
            configuration, "max_train_seconds");
        string emitCkpt = Path.Combine(outputDir, "final.xcn");
        var batchPlan = AdvisoryBatchPlan(
            configuration, serveBundle, maxLen, grant);
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
            // §8: the effective grant rides the job spec — the trainer's
            // recorded ceiling, and the audit anchor for this run.
            ["resource_grant"] = grant?.ToDict(),
            ["data"] = new Dictionary<string, object?>
            {
                ["path"] = trainIds,
                ["format"] = kind,
                ["max_rows"] = 100000000,
                ["max_len"] = maxLen,
            },
        };
        // Token packing passthrough: pack_tokens > 0 concatenates several
        // examples into one training sequence (fewer optimizer steps,
        // larger GEMM M). opt-in per job — lanes enabling it size
        // max_steps/lr accordingly (governed decision, not a silent
        // default). pack_sep < 0 disables the boundary token.
        int packTokens = TransformerTrainingRepository.Int(
            configuration, "pack_tokens");
        if (packTokens > 0)
        {
            var dataSpec = (Dictionary<string, object?>)jobSpec["data"]!;
            dataSpec["pack"] = packTokens;
            int packSep = TransformerTrainingRepository.Int(
                configuration, "pack_sep");
            if (packSep >= 0) dataSpec["pack_sep"] = packSep;
        }
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
        // NativeCudaTrainingPlane §26: admitted jobs get the resident
        // w/m/v fused-AdamW lane via the trainer's opt-in env flag. The
        // flag alone is harmless — the trainer probes at runtime and
        // falls back per-tensor to scalar AdamW when the device lane is
        // unavailable; forward/backward stay on CPU lanes either way.
        var env = new Dictionary<string, string>();
        if (gpu.Admitted) env["XINGCHENG_TRAINER_CUDA_OPT"] = "1";
        // §9/§11: grant ceilings reach the native process as env caps —
        // the trainer's memory/stream pools must never exceed them.
        if (grant != null)
        {
            env["XCT_RESOURCE_GRANT_ID"] = grant.GrantId;
            env["XCT_RESOURCE_CPU_THREADS_MAX"] =
                grant.CpuThreadsMax.ToString(
                    System.Globalization.CultureInfo.InvariantCulture);
            env["XCT_RESOURCE_BG_THREADS_MAX"] =
                grant.BackgroundThreadsMax.ToString(
                    System.Globalization.CultureInfo.InvariantCulture);
            if (grant.VramBytesMax >= 0)
                env["XCT_RESOURCE_VRAM_BYTES_MAX"] =
                    grant.VramBytesMax.ToString(
                        System.Globalization.CultureInfo
                            .InvariantCulture);
            if (gpu.Admitted && gpu.VramBudgetMb > 0)
                env["XCT_RESOURCE_VRAM_BYTES_MAX"] =
                    (gpu.VramBudgetMb << 20).ToString(
                        System.Globalization.CultureInfo
                            .InvariantCulture);
        }
        var run = NativeTools.Run(
            NativeTools.TrainerExe(toolRoot),
            new[] { "--job", jobSpecPath, "--report", reportPath },
            toolRoot, stderrLog, timeoutS, rssBudget, sampleInterval,
            lowPriority: true,
            env: env.Count > 0 ? env : null,
            // VRAM telemetry rides the existing RSS sample ticks whenever
            // a GPU device was requested — admitted runs measure the
            // device lane's footprint; denied runs record the contention
            // that produced the denial.
            vramProbeMb: gpu.Requested
                ? () => (double?)ProbeCuda()?.FreeMb
                : null);
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
                ["vram_free_min_mb"] = run.MinVramFreeMb,
                ["vram_samples"] = run.VramSamples,
                ["elapsed_s"] = Math.Round(run.ElapsedS, 1),
            },
            ["requested_device"] =
                TransformerTrainingRepository.Str(configuration, "device") ?? "",
            // Forward/backward still execute on the trainer's CPU lanes;
            // an admitted request accelerates only the optimizer via the
            // resident w/m/v fused-AdamW device lane (Phase 0 of the
            // heterogeneous plan). Evidence is admission + env flag —
            // the trainer report does not yet echo which lane ran, so
            // optimizer_lane records what was enabled, not a verified
            // post-hoc measurement.
            ["executed_device"] = "cpu-native",
            ["optimizer_lane"] =
                gpu.Admitted ? "cuda-adamw" : "cpu-native",
            ["optimizer_lane_evidence"] = "admission+env-flag",
            ["cuda"] = gpu.ToDict(),
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
        summary["contract_checks"] = ContractChecks(configuration);
        return summary;
    }

    /// <summary>Advisory training-acceleration contract checks (§37-§43):
    /// pilot ladder + eval tiers evaluated over the job's own cadence
    /// and attached for operators. Advisory only — violations never fail
    /// the job; they surface policy-vs-contract drift (e.g. production
    /// max_steps beyond the 600-step pilot ladder).</summary>
    private static Dictionary<string, object?> ContractChecks(
        Dictionary<string, object?> configuration)
    {
        var checks = new Dictionary<string, object?>();
        try
        {
            int maxSteps = TransformerTrainingRepository.Int(
                configuration, "max_steps");
            int logEvery = TransformerTrainingRepository.Int(
                configuration, "log_every");
            int evalEvery = TransformerTrainingRepository.Int(
                configuration, "eval_every");
            int ckptEvery = TransformerTrainingRepository.Int(
                configuration, "checkpoint_every");
            string pilotPayload = System.Text.Json.JsonSerializer.Serialize(
                new Dictionary<string, object?>
                {
                    ["planned_steps"] = maxSteps,
                });
            using var pilotDoc = JsonDocument.Parse(pilotPayload);
            checks["pilot_ladder"] =
                TrainingAcceleration.PilotLadder(pilotDoc.RootElement);
            string tierPayload = System.Text.Json.JsonSerializer.Serialize(
                new Dictionary<string, object?>
                {
                    ["fast_eval_every"] = logEvery,
                    ["regression_eval_every"] =
                        evalEvery > 0 ? evalEvery : ckptEvery,
                    ["full_eval_every"] = -1,
                    ["full_on_candidates_only"] = true,
                });
            using var tierDoc = JsonDocument.Parse(tierPayload);
            var tiers = TrainingAcceleration.EvalTiers(tierDoc.RootElement);
            tiers["mapping"] = "fast=log_every, " +
                "regression=eval_every||checkpoint_every, " +
                "full=candidates-only (post-training suite gate)";
            checks["eval_tiers"] = tiers;
            checks["advisory_only"] = true;
        }
        catch (Exception exc) when (exc is ExecutorError or JsonException)
        {
            checks["error"] = exc.Message;
        }
        return checks;
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
        // Serial execution contract: at most one governed training job
        // in flight at a time (policy: 同時訓練上限 = 1). The claim is a
        // single advisory-locked transaction — existence, queued state,
        // sibling-in-flight and the preflight transition are atomic, so
        // concurrent executors can never both pass a check-then-act gap.
        // A sibling in flight keeps this job queued — the caller retries
        // it on the next drain instead of racing resource supervision.
        var (claim, claimedRow) = _repo.TryClaimTrainingJob(jobId);
        switch (claim)
        {
            case TransformerTrainingRepository.JobClaimResult.Missing:
                throw new ExecutorError("EXECUTOR_JOB_MISSING",
                    $"job {jobId} does not exist");
            case TransformerTrainingRepository.JobClaimResult.NotQueued:
                throw new ExecutorError("EXECUTOR_JOB_NOT_QUEUED",
                    $"job is {claimedRow?["status"]}, not queued");
            case TransformerTrainingRepository.JobClaimResult.Busy:
                throw new ExecutorError("EXECUTOR_TRAINING_SERIAL",
                    $"another governed training job is in flight; " +
                    $"{jobId} stays queued");
        }
        var row = claimedRow!;
        ModelLifecycle? lifecycle = null;
        Dictionary<string, object?>? configuration = null;
        PreflightDecision? resourceDecision = null;
        Dictionary<string, object?>? jobSummary = null;
        string jobResult = "failed";
        try
        {
            configuration = NormalizeConfiguration(row);
            // Resource preflight before any snapshot IO: inference
            // exclusion + governor grant (star-resource-request/grant).
            // Governor-unavailable fails closed (§75); resource codes
            // arm the self-learning gpu-busy backoff.
            resourceDecision =
                PreflightResourceGate(jobId, configuration);
            int trainerThreads = resourceDecision.Threads;
            var gpuPlan = resourceDecision.Gpu;
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
                    ["cuda_opt_admitted"] = gpuPlan.Admitted,
                    ["resource_grant"] =
                        resourceDecision.Grant?.ToDict(),
                });
            Dictionary<string, object?> summary;
            try
            {
                summary = InvokeTrainerNative(
                    trainDocs, valDocs, configuration, outputDir,
                    trainerThreads, gpuPlan, resourceDecision.Grant);
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
            jobSummary = summary;

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
            jobResult = "completed";
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
        finally
        {
            // §58/§90: every grant lifecycle ends with release + usage
            // receipt (success, denial post-acquire, or crash alike).
            resourceDecision?.Finish(jobResult, jobSummary);
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
        var runtime = _repo.RuntimeModelState();
        // §17-§18: the repository's standalone audit lane commits on the
        // canonical plane (xstore post-flip; PG tx pre-flip) — no direct
        // connection handling here.
        _repo.AuditEvent(
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
                ["automatic_weight_replacement"] =
                    TransformerTrainingRepository.Truthy(
                        runtime.GetValueOrDefault(
                            "automatic_weight_replacement")) ? 1 : 0,
            });
    }

    public Dictionary<string, object?>? RunNext()
    {
        var queued = _repo.QueuedJobs(1);
        return queued.Count == 0 ? null : RunJob((string)queued[0]["job_id"]!);
    }

    // ------------------------------------------------------ lane lease --

    /// <summary>Lease on the single training lane, backed by a real
    /// governed job row: the lane IS the row in 'training' status — one
    /// mechanism for claims, exclusion, audit and orphan reaping. While
    /// held, every governed claim reports Busy (sibling in flight); a
    /// crash leaves an orphan the --reap-stale path collects. Disposing
    /// fails the row if it is still live.</summary>
    public sealed class LaneLease : IDisposable
    {
        public string JobId = "";
        public int Threads;
        public bool CudaOptAdmitted;
        public Dictionary<string, object?> GpuEvidence = new();
        internal TransformerTrainingRepository? Repo;
        internal PreflightDecision? Decision;
        private bool _done;

        /// <summary>Run finished — validating → completed. Releases the
        /// ResourceGrant and emits the usage receipt (§54/§58).</summary>
        public void Complete()
        {
            if (_done || Repo == null) return;
            _done = true;
            Repo.TransitionTrainingJob(JobId, "validating",
                errorMessage: "staged lane finished");
            Repo.TransitionTrainingJob(JobId, "completed");
            Decision?.Finish("completed", null);
        }

        /// <summary>Run failed or aborted — active status → failed;
        /// grant released either way.</summary>
        public void Abort(string code, string message)
        {
            if (_done || Repo == null) return;
            _done = true;
            var row = Repo.JobRow(JobId);
            string status = (string?)row?["status"] ?? "";
            if (status is "preflight" or "training" or "validating")
                Repo.TransitionTrainingJob(JobId, "failed",
                    errorCode: code, errorMessage: message);
            Decision?.Finish("aborted:" + code, null);
        }

        public void Dispose()
        {
            if (_done) return;
            try { Abort("EXECUTOR_LANE_ABORTED",
                        "lane scope disposed while still active"); }
            catch { /* release must never throw */ }
        }
    }

    /// <summary>Acquire the single training lane for a staged run that
    /// keeps its own orchestration (currently only the §33/§34
    /// instruction-recovery lane). The lane is expressed as a governed
    /// job row claimed through
    /// <see cref="TransformerTrainingRepository.TryClaimTrainingJob"/> —
    /// the same atomic mechanism every queued job uses, so exclusion is
    /// race-free in both directions and every step lands in the audit
    /// chain. Admission parity with a governed SFT job is enforced:
    /// maturation-sequence guard + the governor's resource preflight
    /// (inference exclusion, training pause, thread quota, GPU/VRAM
    /// admission). The caller's freeze guard
    /// (CapabilityFreeze.GuardJob) must already have passed. A denied
    /// claim cancels the marker row; a denied admission fails it — no
    /// lane row ever lingers queued to be drained as real training.</summary>
    public LaneLease AcquireTrainingLane(
        string capability, string laneDatasetId,
        Dictionary<string, object?> configuration,
        string requestedBy)
    {
        var laneCfg = new Dictionary<string, object?>(configuration)
        {
            ["training_kind"] = "sft",
            ["capability"] = capability,
            ["lane"] = "staged-run",
        };
        var job = _repo.CreateTrainingJob(
            datasetId: laneDatasetId, configuration: laneCfg,
            requestedBy: requestedBy);
        string jobId = (string)job["job_id"]!;
        var (claim, _) = _repo.TryClaimTrainingJob(jobId);
        if (claim != TransformerTrainingRepository.JobClaimResult.Claimed)
        {
            _repo.TransitionTrainingJob(jobId, "cancelled",
                errorCode: "EXECUTOR_TRAINING_SERIAL",
                errorMessage: "training lane busy — staged run denied");
            throw new ExecutorError("EXECUTOR_TRAINING_SERIAL",
                "another governed training job holds the lane; " +
                "staged run stays sealed");
        }
        try
        {
            CapabilityProgressionPolicy.GuardAdmission(
                _toolRoot, capability);
            var decision = PreflightResourceGate(jobId, configuration);
            _repo.TransitionTrainingJob(jobId, "training",
                errorMessage: $"staged lane held for '{capability}'");
            _repo.AuditEvent("training-lane-leased",
                "training-job", jobId,
                new Dictionary<string, object?>
                {
                    ["capability"] = capability,
                    ["lane"] = requestedBy,
                    ["trainer_threads"] = decision.Threads,
                    ["cuda_opt_admitted"] = decision.Gpu.Admitted,
                    ["gpu"] = decision.Gpu.ToDict(),
                    ["lane_allocation"] = decision.Lane?.ToDict(),
                    ["resource_grant"] = decision.Grant?.ToDict(),
                });
            return new LaneLease
            {
                JobId = jobId,
                Threads = decision.Threads,
                CudaOptAdmitted = decision.Gpu.Admitted,
                GpuEvidence = decision.Gpu.ToDict(),
                Repo = _repo,
                Decision = decision,
            };
        }
        catch (ExecutorError ex)
        {
            _repo.TransitionTrainingJob(jobId, "failed",
                errorCode: ex.ErrorCode, errorMessage: ex.Message);
            throw;
        }
    }

    // ------------------------------------------------------------- reaper --

    /// <summary>Reap orphaned live-state jobs (preflight/training/
    /// validating). A crashed or killed run never leaves these states by
    /// itself, and <see cref="RunNext"/> only drains queued — without a
    /// reaper the row blocks that dataset's lane forever and every
    /// downstream probe (self-learning, queue depth) lies about it.
    /// Only rows older than <paramref name="olderThanS"/> (floored to
    /// one hour so a live run can never be reaped by a typo) are
    /// candidates; unparseable timestamps are skipped, never reaped.
    /// Reaping marks the row failed with EXECUTOR_ORPHANED_REAPED through
    /// the governed transition (audited); requeueing stays an explicit
    /// operator act via --queue-job. Default dry-run lists candidates.</summary>
    public Dictionary<string, object?> ReapStaleJobs(
        long olderThanS, bool dryRun)
    {
        long thresholdS = Math.Max(3600, olderThanS <= 0 ? 86400 : olderThanS);
        var now = DateTimeOffset.UtcNow;
        var candidates = new List<object?>();
        var reaped = new List<object?>();
        var skipped = new List<object?>();
        foreach (var row in _repo.ActiveJobs())
        {
            string jobId = (string)row["job_id"]!;
            string status = (string)row["status"]!;
            string anchor = TransformerTrainingRepository.Str(row, "started_at")!;
            if (string.IsNullOrEmpty(anchor))
                anchor = TransformerTrainingRepository.Str(row, "created_at")!;
            if (!DateTimeOffset.TryParse(anchor, out var since))
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["job_id"] = jobId, ["status"] = status,
                    ["reason"] = "unparseable-timestamp",
                });
                continue;
            }
            long ageS = (long)(now - since).TotalSeconds;
            if (ageS < thresholdS)
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["job_id"] = jobId, ["status"] = status,
                    ["age_s"] = ageS, ["reason"] = "below-threshold",
                });
                continue;
            }
            var candidate = new Dictionary<string, object?>
            {
                ["job_id"] = jobId, ["status"] = status,
                ["age_s"] = ageS, ["since"] = anchor,
            };
            candidates.Add(candidate);
            if (dryRun) continue;
            try
            {
                var failed = _repo.TransitionTrainingJob(
                    jobId, "failed",
                    errorCode: "EXECUTOR_ORPHANED_REAPED",
                    errorMessage: $"orphaned in {status} for {ageS}s; " +
                        "no live run holds it — requeue explicitly via " +
                        "--queue-job after verifying no trainer process " +
                        "is running");
                candidate["reaped_to"] = failed["status"];
                reaped.Add(candidate);
            }
            catch (Exception exc)
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["job_id"] = jobId, ["status"] = status,
                    ["age_s"] = ageS,
                    ["reason"] = $"reap-refused:{exc.Message}",
                });
            }
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-training-reap/v1",
            ["dry_run"] = dryRun,
            ["threshold_s"] = thresholdS,
            ["candidates"] = candidates,
            ["reaped"] = reaped,
            ["skipped"] = skipped,
            ["checked_at"] = XcPaths.IsoNow(),
        };
    }
}
