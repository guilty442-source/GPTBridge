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
        CapabilityFreeze.GuardJob(kind, (string)cfg["capability"]!,
                                  SelfLearningPolicy.Load(_toolRoot));

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
    /// config dict (XCN2 MoE widths, XCN3 hybrid-attention geometry,
    /// XCN4 vision early-fusion block, XCN5 Gemma A4B axis, XCN6
    /// fused-router flag, XCN7 DeepSeek V4-Pro axis, XCN8 YaRN block,
    /// XCN9 Gemma4 marker block, XCN10 MTP-stack — field names match
    /// ``xct_util.parse_model``).</summary>
    private static Dictionary<string, object?> XcnConfig(string ckptPath)
    {
        using var f = new FileStream(ckptPath, FileMode.Open, FileAccess.Read);
        using var r = new BinaryReader(f);
        byte[] magic = r.ReadBytes(4);
        if (magic.Length != 4 ||
            magic[0] != 'X' || magic[1] != 'C' || magic[2] != 'N' || magic[3] != '1')
            throw new ExecutorError("EXECUTOR_CKPT_BAD_MAGIC", ckptPath);
        uint ver = r.ReadUInt32();
        if (ver < 1 || ver > 10)
            throw new ExecutorError("EXECUTOR_CKPT_VERSION", $"v{ver}");
        uint vocab = r.ReadUInt32();
        uint hidden = r.ReadUInt32();
        uint inter = r.ReadUInt32();
        uint layers = r.ReadUInt32();
        uint heads = r.ReadUInt32();
        uint kvHeads = r.ReadUInt32();
        uint maxPos = r.ReadUInt32();
        uint moeExperts = r.ReadUInt32();
        uint moeTopK = r.ReadUInt32();
        uint moeInterval = r.ReadUInt32();
        double ropeTheta = r.ReadSingle();
        double rmsEps = r.ReadSingle();
        double moeAux = r.ReadSingle();
        var cfg = new Dictionary<string, object?>
        {
            ["vocab_size"] = (long)vocab,
            ["hidden_size"] = (long)hidden,
            ["intermediate_size"] = (long)inter,
            ["num_hidden_layers"] = (long)layers,
            ["num_attention_heads"] = (long)heads,
            ["num_key_value_heads"] = (long)kvHeads,
            ["max_position_embeddings"] = (long)maxPos,
            ["moe_num_experts"] = (long)moeExperts,
            ["moe_top_k"] = (long)moeTopK,
            ["moe_layer_interval"] = (long)moeInterval,
            ["rope_theta"] = (double)(float)ropeTheta,
            ["rms_norm_eps"] = (double)(float)rmsEps,
            ["moe_aux_loss_weight"] = (double)(float)moeAux,
        };
        if (ver >= 2)
        {
            cfg["moe_expert_intermediate_size"] = (long)r.ReadUInt32();
            cfg["moe_num_shared_experts"] = (long)r.ReadUInt32();
            cfg["moe_shared_intermediate_size"] = (long)r.ReadUInt32();
        }
        if (ver >= 3)
        {
            cfg["full_attention_interval"] = (long)r.ReadUInt32();
            uint flags = r.ReadUInt32();
            cfg["attn_output_gate"] = (flags & 1u) != 0;
            cfg["qk_norm"] = (flags & 2u) != 0;
            cfg["shared_expert_gate"] = (flags & 4u) != 0;
            cfg["partial_rotary_factor"] = (double)r.ReadSingle();
            cfg["linear_num_key_heads"] = (long)r.ReadUInt32();
            cfg["linear_key_head_dim"] = (long)r.ReadUInt32();
            cfg["linear_num_value_heads"] = (long)r.ReadUInt32();
            cfg["linear_value_head_dim"] = (long)r.ReadUInt32();
            cfg["linear_conv_kernel_dim"] = (long)r.ReadUInt32();
        }
        if (ver >= 4)
        {
            cfg["use_vision"] = r.ReadUInt32() != 0u;
            cfg["vision_patch_dim"] = (long)r.ReadUInt32();
            cfg["vision_max_patches"] = (long)r.ReadUInt32();
        }
        if (ver >= 5)
        {
            // XCN5 Gemma A4B block (see xct_ckpt.h write order):
            // global_attention_interval, sliding_window, num_global_kv_heads,
            // flag bits, rope proportions/base frequencies, softcap.
            cfg["global_attention_interval"] = (long)r.ReadUInt32();
            cfg["sliding_window_size"] = (long)r.ReadUInt32();
            cfg["num_global_kv_heads"] = (long)r.ReadUInt32();
            uint gflags = r.ReadUInt32();
            cfg["k_eq_v_global"] = (gflags & 1u) != 0;
            cfg["use_post_attn_norm"] = (gflags & 2u) != 0;
            cfg["use_post_ffw_norm"] = (gflags & 4u) != 0;
            if ((gflags & 8u) != 0) cfg["ffn_activation"] = "gelu_tanh";
            cfg["local_rope_proportion"] = (double)r.ReadSingle();
            cfg["global_rope_proportion"] = (double)r.ReadSingle();
            cfg["local_base_frequency"] = (double)r.ReadSingle();
            cfg["global_base_frequency"] = (double)r.ReadSingle();
            cfg["final_logit_softcap"] = (double)r.ReadSingle();
        }
        if (ver >= 6)
        {
            // XCN6 fused router: Qwen3-A3B softmax | Qwen3.5 sigmoid
            // scoring flag (see xct_ckpt.h).
            cfg["moe_router_sigmoid"] = r.ReadUInt32() != 0u;
        }
        if (ver >= 7)
        {
            // XCN7 DeepSeek V4-Pro block (see xct_ckpt.h write order):
            // MLA dims, aux-free balance flag + bias rate, MTP depth +
            // loss weight.
            cfg["kv_lora_rank"] = (long)r.ReadUInt32();
            cfg["q_lora_rank"] = (long)r.ReadUInt32();
            cfg["qk_nope_head_dim"] = (long)r.ReadUInt32();
            cfg["qk_rope_head_dim"] = (long)r.ReadUInt32();
            cfg["moe_auxfree_balance"] = r.ReadUInt32() != 0u;
            cfg["moe_lb_bias_rate"] = (double)r.ReadSingle();
            cfg["num_nextn_predict_layers"] = (long)r.ReadUInt32();
            cfg["mtp_loss_weight"] = (double)r.ReadSingle();
        }
        if (ver >= 8)
        {
            // XCN8 Qwen3-Coder YaRN block (see xct_ckpt.h write order):
            // extension factor, original context length, beta band
            // bounds, attention factor (mscale).
            cfg["yarn_factor"] = (double)r.ReadSingle();
            cfg["yarn_original_max_position_embeddings"] =
                (long)r.ReadUInt32();
            cfg["yarn_beta_fast"] = (double)r.ReadSingle();
            cfg["yarn_beta_slow"] = (double)r.ReadSingle();
            cfg["yarn_attention_factor"] = (double)r.ReadSingle();
        }
        if (ver >= 9)
        {
            // XCN9 Gemma4 block (see xct_ckpt.h write order): the marker
            // u32 is always present at ver >= 9 — 1 = g4 fields follow,
            // 0 = non-gemma4 (canonical fused generation checkpoints
            // land here).
            uint g4m = r.ReadUInt32();
            if (g4m == 1u)
            {
                cfg["model_type"] = "gemma4_text";
                cfg["head_dim"] = (long)r.ReadUInt32();
                cfg["global_head_dim"] = (long)r.ReadUInt32();
                cfg["sliding_window"] = (long)r.ReadUInt32();
                cfg["num_kv_shared_layers"] = (long)r.ReadUInt32();
                cfg["hidden_size_per_layer_input"] = (long)r.ReadUInt32();
                cfg["vocab_size_per_layer_input"] = (long)r.ReadUInt32();
                uint g4flags = r.ReadUInt32();
                cfg["use_double_wide_mlp"] = (g4flags & 1u) != 0;
                cfg["tie_word_embeddings"] = (g4flags & 2u) != 0;
                cfg["rope_theta_full"] = (double)r.ReadSingle();
                cfg["rope_partial_rotary_factor"] = (double)r.ReadSingle();
                cfg["final_logit_softcapping"] = (double)r.ReadSingle();
                cfg["attention_scale"] = (double)r.ReadSingle();
                uint nt = r.ReadUInt32();
                var types = new List<object?>();
                for (uint i = 0; i < nt; ++i)
                {
                    uint nl = r.ReadUInt32();
                    types.Add(System.Text.Encoding.UTF8.GetString(
                        r.ReadBytes((int)nl)));
                }
                cfg["layer_types"] = types;
                uint al = r.ReadUInt32();
                cfg["hidden_activation"] =
                    System.Text.Encoding.UTF8.GetString(
                        r.ReadBytes((int)al));
            }
            else if (g4m != 0u)
            {
                throw new ExecutorError("EXECUTOR_CKPT_VERSION",
                    "bad gemma4 marker");
            }
        }
        if (ver >= 10)
        {
            // XCN10 v29 MTP-stack block (see xct_ckpt.h write order):
            // mtp_stack_depth u32 + mtp_stack_loss_weight float.
            cfg["mtp_stack_depth"] = (long)r.ReadUInt32();
            cfg["mtp_stack_loss_weight"] = (double)r.ReadSingle();
        }
        return cfg;
    }

    private void RunModelTool(string toolRoot, string stderrLog,
                              params string[] args)
    {
        var result = NativeTools.Run(
            NativeTools.ModelToolExe(toolRoot), args, toolRoot, stderrLog,
            timeoutS: 7200, rssBudgetMb: 0);
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
            timeoutS: 7200, rssBudgetMb: 0);
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

    private Dictionary<string, object?> InvokeTrainerNative(
        List<Dictionary<string, object?>> trainDocs,
        List<Dictionary<string, object?>> valDocs,
        Dictionary<string, object?> configuration,
        string outputDir)
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

        // -- trainer job spec.
        double deadline = TransformerTrainingRepository.Num(
            configuration, "max_train_seconds");
        string emitCkpt = Path.Combine(outputDir, "final.xcn");
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
            toolRoot, stderrLog, timeoutS, rssBudget, sampleInterval);
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

        // -- export the trained weights as a native bundle (the runnable +
        //    registerable artifact).
        string bundleDir = Path.Combine(outputDir, "bundle");
        string configFrom = bundleManifestForExport
            ?? WriteScratchManifest(outputDir, modelCfg);
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
            ["export"] = export,
        };
        if (report.TryGetValue("deadline_hit", out object? dh) &&
            dh is bool hit && hit)
            summary["stopped_reason"] = "deadline-exceeded";
        if (report.TryGetValue("checkpoint_emitted", out object? ce) &&
            ce is bool emitted && !emitted)
            summary["stopped_reason"] = "checkpoint-not-emitted";
        return summary;
    }

    private static string WriteScratchManifest(
        string outputDir, Dictionary<string, object?> modelCfg)
    {
        // Minimal manifest wrapper so export-bundle can copy config verbatim.
        string path = Path.Combine(outputDir, "model-config.json");
        var wrapper = new Dictionary<string, object?> { ["config"] = modelCfg };
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
        // weights, so the whole stage is sealed while frozen. Queueing /
        // dataset registration stay open — only execution is gated.
        var freezePolicy = SelfLearningPolicy.Load(_toolRoot);
        if (freezePolicy.CapabilityTrainingFrozen)
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
            var (dataset, trainDocs, valDocs) =
                LoadSplitDocuments((string)row["dataset_id"]!);
            // §1 recovery lane defense-in-depth: under
            // SINGLE_CAPABILITY_RECOVERY every document carrying a
            // capability tag must name the active capability — a mixed
            // dataset is a multi-capability job and is denied.
            {
                var pol = SelfLearningPolicy.Load(_toolRoot);
                if (string.Equals(pol.CapabilityTrainingMode,
                                  "SINGLE_CAPABILITY_RECOVERY",
                                  StringComparison.OrdinalIgnoreCase))
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
            Dictionary<string, object?> summary;
            try
            {
                summary = InvokeTrainerNative(
                    trainDocs, valDocs, configuration, outputDir);
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
