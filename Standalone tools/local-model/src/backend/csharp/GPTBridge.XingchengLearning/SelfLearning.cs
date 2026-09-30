// SelfLearning.cs — governed self-learning cycle (star-self-learning).
//
// Direct port of self_learning_support.run_cycle_impl /
// _execute_governed_cycle. Gate order is authoritative and fail-closed:
//
//   enabled -> failure breaker -> training window -> min-interval ->
//   daily budget -> gpu-busy backoff -> inference exclusion ->
//   [dpo branch] -> collect -> sanitize (dedup + probe contamination) ->
//   pool stats -> quality-drift -> synthetic-ratio -> new-example
//   threshold / degradation probe -> curriculum -> dataset cap ->
//   SFT/DPO snapshot -> register dataset -> queue job -> executor ->
//   stopped_reason check -> register adapter -> per-suite evaluation ->
//   stage/activate -> lifecycle register -> runtime pin -> prune latest ->
//   [maturity recheck / governed rollback] -> state + report.
//
// Native-lane substitutions (documented, not silent):
//   * _inference_active reads the model-service descriptor pid
//     (Collectors.InferenceActive) instead of the retired engine caches;
//     indeterminate still blocks (fail-closed).
//   * _weights_config_fingerprint hashes the canonical bundle manifest
//     config (or the .xcn header-derived config) instead of torch
//     config_json.
//   * _degradation_probe evaluates via xc_modeltool eval and compares in
//     this file with the tps gates removed (same as the Python lane).
//   * post_upgrade_maturity_recheck: the maturity ladder is not yet
//     ported; when the policy enables it the recheck reports
//     unavailable and the governed rollback path runs — fail-closed.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class SelfLearning
{
    private static readonly Dictionary<int, string> CurriculumCourses = new()
    {
        [5] = "sft-dialogue",
        [6] = "sft-reasoning",
        [7] = "sft-evolution",
    };

    private static string IsoNow() => XcPaths.IsoNow();

    // ------------------------------------------------------------ helpers --

    private static Dictionary<string, object?> Blocked(
        string reason, params (string Key, object? Value)[] extra)
    {
        var result = new Dictionary<string, object?>
        {
            ["ok"] = true, ["action"] = "blocked", ["reason"] = reason,
        };
        foreach (var (k, v) in extra) result[k] = v;
        return result;
    }

    private static string WriteReport(string tool, Dictionary<string, object?> payload)
    {
        string dir = Path.Combine(tool, XcPaths.LogsRel);
        Directory.CreateDirectory(dir);
        string path = Path.Combine(dir,
            $"self-learning-{DateTime.UtcNow:yyyyMMdd-HHmmss}.json");
        File.WriteAllText(path, CanonicalJson.PrettyDict(payload) + "\n",
                          new System.Text.UTF8Encoding(false));
        return path;
    }

    private static string PinRuntimeCheckpoint(string tool, string artifact)
    {
        // The runtime checkpoint is a bundle directory, not a file.
        string pinned = artifact;
        if (Path.GetFileName(artifact) == "manifest.json" &&
            File.Exists(artifact))
            pinned = Path.GetDirectoryName(artifact)!;
        return EngineSettings.PinCheckpoint(tool, pinned);
    }

    private static bool RetirePreviousArtifact(string previous, string keep,
                                               string toolRoot)
    {
        string previousDir = Path.GetFileName(previous) == "manifest.json" &&
                             File.Exists(previous)
            ? Path.GetDirectoryName(previous)!
            : previous;
        if (string.Equals(Path.GetFullPath(previousDir),
                          Path.GetFullPath(keep),
                          StringComparison.OrdinalIgnoreCase))
            return false;
        string root = Path.GetFullPath(toolRoot);
        if (!Path.GetFullPath(previousDir).StartsWith(
                root + Path.DirectorySeparatorChar,
                StringComparison.OrdinalIgnoreCase))
            return false; // never prune outside the module boundary
        try
        {
            if (Directory.Exists(previousDir))
                Directory.Delete(previousDir, recursive: true);
            else if (File.Exists(previous))
                File.Delete(previous);
            else
                return false;
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    // 世代繼任刪除（能力/架構升級後刪除前代）：前代 bundle 只在新代
    // 記錄已攜帶其血統（metadata.succeeded_from，啟用時由
    // ModelLifecycle 自動寫入）且 lifecycle 已先持久化後才實體刪除——
    // 前代資料保留在新代，缺繼任記錄時 fail-closed 保留前代。刪除成功後
    // 把前代條目由 versions 移入 retired 並標記 deleted_at /
    // data_carried_to，活版本表不留死路徑、retired 保留完整資料。
    private static bool PruneSupersededGeneration(
        ModelLifecycle lifecycle, string lifecycleDir, string toolRoot,
        string previous, string keep,
        Dictionary<string, object?>? newEntry)
    {
        if (newEntry == null ||
            !newEntry.TryGetValue("metadata", out object? m) ||
            m is not Dictionary<string, object?> meta ||
            meta["succeeded_from"] is not Dictionary<string, object?> sf)
            return false;   // 前代資料尚未攜入新代 → 禁止刪除
        int prevVersion = -1;
        if (sf.TryGetValue("version", out object? sv) && sv != null)
            prevVersion = Convert.ToInt32(sv);
        lifecycle.Save(lifecycleDir);   // 資料先落地，刪除在後
        if (!RetirePreviousArtifact(previous, keep, toolRoot))
            return false;
        if (prevVersion > 0)
        {
            var moved = lifecycle.RetireWeightVersion(
                prevVersion, Convert.ToInt32(newEntry["version"]));
            if (moved != null)
            {
                moved["deleted_at"] = IsoNow();
                lifecycle.Save(lifecycleDir);
            }
        }
        return true;
    }

    /// <summary>Canonical config fingerprint of a weights artifact —
    /// bundle manifest ``config`` or the .xcn header. Null when
    /// unresolvable (rollback eligibility is deny-by-default).</summary>
    internal static string? WeightsConfigFingerprint(string path)
    {
        try
        {
            Dictionary<string, object?>? config = null;
            if (Directory.Exists(path))
            {
                string manifest = Path.Combine(path, "manifest.json");
                if (!File.Exists(manifest)) return null;
                using var doc = JsonDocument.Parse(File.ReadAllText(manifest));
                if (!doc.RootElement.TryGetProperty("config", out var cfg) ||
                    cfg.ValueKind != JsonValueKind.Object)
                    return null;
                config = new Dictionary<string, object?>();
                foreach (var p in cfg.EnumerateObject())
                    config[p.Name] = ModelLifecycle.Decode(p.Value);
            }
            else if (File.Exists(path) &&
                     Path.GetFileName(path) == "manifest.json")
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                if (!doc.RootElement.TryGetProperty("config", out var cfg) ||
                    cfg.ValueKind != JsonValueKind.Object)
                    return null;
                config = new Dictionary<string, object?>();
                foreach (var p in cfg.EnumerateObject())
                    config[p.Name] = ModelLifecycle.Decode(p.Value);
            }
            else if (File.Exists(path))
            {
                config = ReadXcnHeaderConfig(path);
            }
            if (config == null) return null;
            return TransformerTrainingRepository.Sha256Text(
                CanonicalJson.CanonicalDict(config));
        }
        catch { return null; }
    }

    private static Dictionary<string, object?>? ReadXcnHeaderConfig(string path)
    {
        try
        {
            using var f = new FileStream(path, FileMode.Open, FileAccess.Read);
            using var r = new BinaryReader(f);
            byte[] magic = r.ReadBytes(4);
            if (magic.Length != 4 || magic[0] != 'X' || magic[1] != 'C' ||
                magic[2] != 'N' || magic[3] != '1')
                return null;
            uint ver = r.ReadUInt32();
            if (ver < 1 || ver > 10) return null;
            var cfg = new Dictionary<string, object?>
            {
                ["vocab_size"] = (long)r.ReadUInt32(),
                ["hidden_size"] = (long)r.ReadUInt32(),
                ["intermediate_size"] = (long)r.ReadUInt32(),
                ["num_hidden_layers"] = (long)r.ReadUInt32(),
                ["num_attention_heads"] = (long)r.ReadUInt32(),
                ["num_key_value_heads"] = (long)r.ReadUInt32(),
                ["max_position_embeddings"] = (long)r.ReadUInt32(),
                ["moe_num_experts"] = (long)r.ReadUInt32(),
                ["moe_top_k"] = (long)r.ReadUInt32(),
                ["moe_layer_interval"] = (long)r.ReadUInt32(),
                ["rope_theta"] = (double)(float)r.ReadSingle(),
                ["rms_norm_eps"] = (double)(float)r.ReadSingle(),
                ["moe_aux_loss_weight"] = (double)(float)r.ReadSingle(),
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
                // XCN5 Gemma A4B block (see xct_ckpt.h write order).
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
                // XCN6 fused router flag.
                cfg["moe_router_sigmoid"] = r.ReadUInt32() != 0u;
            }
            if (ver >= 7)
            {
                // XCN7 DeepSeek V4-Pro block (see xct_ckpt.h).
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
                // XCN8 Qwen3-Coder YaRN block (see xct_ckpt.h).
                cfg["yarn_factor"] = (double)r.ReadSingle();
                cfg["yarn_original_max_position_embeddings"] =
                    (long)r.ReadUInt32();
                cfg["yarn_beta_fast"] = (double)r.ReadSingle();
                cfg["yarn_beta_slow"] = (double)r.ReadSingle();
                cfg["yarn_attention_factor"] = (double)r.ReadSingle();
            }
            if (ver >= 9)
            {
                // XCN9 Gemma4 block (see xct_ckpt.h write order): the
                // marker u32 is always present — 1 = g4 fields follow,
                // 0 = non-gemma4 (canonical xc-fused-1 checkpoints).
                uint g4m = r.ReadUInt32();
                if (g4m == 1u)
                {
                    cfg["model_type"] = "gemma4_text";
                    cfg["head_dim"] = (long)r.ReadUInt32();
                    cfg["global_head_dim"] = (long)r.ReadUInt32();
                    cfg["sliding_window"] = (long)r.ReadUInt32();
                    cfg["num_kv_shared_layers"] = (long)r.ReadUInt32();
                    cfg["hidden_size_per_layer_input"] =
                        (long)r.ReadUInt32();
                    cfg["vocab_size_per_layer_input"] =
                        (long)r.ReadUInt32();
                    uint g4flags = r.ReadUInt32();
                    cfg["use_double_wide_mlp"] = (g4flags & 1u) != 0;
                    cfg["tie_word_embeddings"] = (g4flags & 2u) != 0;
                    cfg["rope_theta_full"] = (double)r.ReadSingle();
                    cfg["rope_partial_rotary_factor"] =
                        (double)r.ReadSingle();
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
                    return null;
                }
            }
            if (ver >= 10)
            {
                // XCN10 v29 MTP-stack block (see xct_ckpt.h).
                cfg["mtp_stack_depth"] = (long)r.ReadUInt32();
                cfg["mtp_stack_loss_weight"] = (double)r.ReadSingle();
            }
            return cfg;
        }
        catch { return null; }
    }

    private static Dictionary<string, object?> AttemptGovernedRollback(
        string tool, ModelLifecycle lifecycle, string lifecycleDir,
        string anchorPath, int excludeVersion)
    {
        string? anchor = WeightsConfigFingerprint(anchorPath);
        var excluded = new HashSet<int> { excludeVersion };
        var targets = lifecycle.RollbackTargetVersions(
            compatFingerprint: anchor,
            compatResolver: WeightsConfigFingerprint,
            excludeVersions: excluded);
        if (targets.Count == 0)
            return new Dictionary<string, object?>
            {
                ["rolled_back"] = false,
                ["denied"] = "no-eligible-retained-version",
                ["anchor_config_sha256"] = anchor,
            };
        int target = targets[^1];
        Dictionary<string, object?> entry;
        try
        {
            entry = lifecycle.GovernedRollbackWeights(
                target, compatFingerprint: anchor,
                compatResolver: WeightsConfigFingerprint,
                excludeVersions: excluded);
        }
        catch (ArgumentException exc)
        {
            return new Dictionary<string, object?>
            {
                ["rolled_back"] = false,
                ["denied"] = exc.Message,
                ["anchor_config_sha256"] = anchor,
            };
        }
        lifecycle.Save(lifecycleDir);
        string pinned = PinRuntimeCheckpoint(
            tool, (string)(entry["path"] ?? ""));
        return new Dictionary<string, object?>
        {
            ["rolled_back"] = true,
            ["to_version"] = target,
            ["runtime_checkpoint"] = pinned,
            ["anchor_config_sha256"] = anchor,
        };
    }

    private static int? PreviousMaturityLevel(
        ModelLifecycle lifecycle, string activePath)
    {
        string target = Path.GetFullPath(activePath);
        if (!lifecycle.Artifacts.TryGetValue("weights", out var w) ||
            !w.TryGetValue("versions", out object? v) ||
            v is not IEnumerable<object?> versions)
            return null;
        foreach (object? item in versions)
        {
            if (item is not Dictionary<string, object?> entry) continue;
            string raw = TransformerTrainingRepository.Str(entry, "path") ?? "";
            if (raw.Length == 0) continue;
            string resolved;
            try { resolved = Path.GetFullPath(raw); }
            catch { continue; }
            if (!resolved.Equals(target, StringComparison.OrdinalIgnoreCase))
                continue;
            if (entry.TryGetValue("metadata", out object? m) &&
                m is Dictionary<string, object?> metadata &&
                metadata.TryGetValue("maturity_level", out object? lvl) &&
                lvl != null)
            {
                try { return Convert.ToInt32(lvl); }
                catch { return null; }
            }
            return null;
        }
        return null;
    }

    // ------------------------------------------------------------- gates --

    private static Dictionary<string, object?>? FailureBreakerStatus(
        SelfLearningPolicy policy, IReadOnlyDictionary<string, object?> state)
    {
        int limit = policy.MaxConsecutiveFailures;
        int streak = TransformerTrainingRepository.Int(state, "consecutive_failures");
        if (limit <= 0 || streak < limit) return null;
        return Blocked("failure-breaker",
            ("consecutive_failures", streak),
            ("max_consecutive_failures", limit));
    }

    private static Dictionary<string, object?>? MinIntervalStatus(
        SelfLearningPolicy policy, IReadOnlyDictionary<string, object?> state)
    {
        int gap = policy.MinIntervalS;
        string? lastRun = TransformerTrainingRepository.Str(state, "last_run_at");
        if (gap <= 0 || string.IsNullOrEmpty(lastRun)) return null;
        double elapsed;
        try
        {
            var lastDt = DateTimeOffset.Parse(lastRun.Replace("Z", "+00:00"));
            elapsed = (DateTimeOffset.UtcNow - lastDt).TotalSeconds;
        }
        catch (Exception)
        {
            return Blocked("invalid-last-run-at", ("last_run_at", lastRun));
        }
        if (elapsed < gap)
            return Blocked("min-interval",
                ("elapsed_s", Math.Round(elapsed, 1)),
                ("min_interval_s", gap));
        return null;
    }

    private static Dictionary<string, object?>? GpuBusyBackoffStatus(
        SelfLearningPolicy policy, IReadOnlyDictionary<string, object?> state)
    {
        if (policy.GpuBusyBackoffS <= 0) return null;
        string? until = TransformerTrainingRepository.Str(state, "gpu_busy_until");
        if (string.IsNullOrEmpty(until)) return null;
        double remaining;
        try
        {
            var untilDt = DateTimeOffset.Parse(until.Replace("Z", "+00:00"));
            remaining = (untilDt - DateTimeOffset.UtcNow).TotalSeconds;
        }
        catch (Exception) { return null; }
        if (remaining <= 0) return null;
        return Blocked("gpu-busy-backoff",
            ("remaining_s", Math.Round(remaining, 1)),
            ("gpu_busy_streak",
             TransformerTrainingRepository.Int(state, "gpu_busy_streak")),
            ("gpu_busy_until", until));
    }

    private static Dictionary<string, object?> GpuBusyRecord(
        SelfLearningPolicy policy, IReadOnlyDictionary<string, object?> state)
    {
        int streak = TransformerTrainingRepository
            .Int(state, "gpu_busy_streak") + 1;
        int baseS = policy.GpuBusyBackoffS;
        int cap = policy.GpuBusyBackoffCapS;
        long delay = baseS > 0 ? (long)baseS * (1L << Math.Min(streak - 1, 20)) : 0;
        if (cap > 0) delay = Math.Min(delay, cap);
        string? until = delay > 0
            ? DateTimeOffset.UtcNow.AddSeconds(delay)
                .ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
            : null;
        return new Dictionary<string, object?>
        {
            ["gpu_busy_streak"] = streak,
            ["gpu_busy_until"] = until,
            ["gpu_busy_backoff_s"] = delay,
        };
    }

    private static Dictionary<string, object?>? DailyBudgetStatus(
        SelfLearningPolicy policy, IReadOnlyDictionary<string, object?> state)
    {
        int cap = policy.MaxCyclesPerDay;
        if (cap <= 0) return null;
        string today = DateTime.UtcNow.ToString("yyyy-MM-dd");
        if (state.TryGetValue("cycles_today", out object? c) &&
            c is Dictionary<string, object?> counter &&
            TransformerTrainingRepository.Str(counter, "date") == today &&
            TransformerTrainingRepository.Int(counter, "count") >= cap)
        {
            return Blocked("daily-cycle-budget",
                ("cycles_today", counter), ("max_per_day", cap));
        }
        return null;
    }

    private static Dictionary<string, object?> PoolStats(
        SelfLearningPolicy policy,
        IReadOnlyDictionary<string, List<Dictionary<string, object?>>> examples)
    {
        int total = 0, synthetic = 0;
        double qualitySum = 0.0;
        foreach (var records in examples.Values)
        {
            foreach (var record in records)
            {
                total++;
                qualitySum += Convert.ToDouble(record["quality_score"] ?? 0.0);
                string sourceType = (record["source_type"]?.ToString() ?? "");
                if (policy.SyntheticSourcePrefixes.Any(
                        p => sourceType.StartsWith(p, StringComparison.Ordinal)))
                    synthetic++;
            }
        }
        return new Dictionary<string, object?>
        {
            ["total"] = total,
            ["synthetic_count"] = synthetic,
            ["synthetic_ratio"] = total > 0 ? (double)synthetic / total : 0.0,
            ["pool_avg_quality"] = total > 0 ? qualitySum / total : 0.0,
        };
    }

    private static (Dictionary<string, List<Dictionary<string, object?>>> cleaned,
                    Dictionary<string, object?> stats) SanitizePool(
        SelfLearningPolicy policy,
        Dictionary<string, List<Dictionary<string, object?>>> examples)
    {
        var probeValues = policy.ExcludeProbeValues
            ? XcPaths.ProbeValues : Array.Empty<string>();
        var seen = new HashSet<(string, string)>();
        var cleaned = new Dictionary<string, List<Dictionary<string, object?>>>();
        int deduped = 0, contaminated = 0;
        foreach (var (scope, records) in examples)
        {
            var kept = new List<Dictionary<string, object?>>();
            foreach (var record in records)
            {
                string prompt = record["input_text"]?.ToString() ?? "";
                string completion = record["target_text"]?.ToString() ?? "";
                if (probeValues.Any(v =>
                        prompt.Contains(v, StringComparison.Ordinal) ||
                        completion.Contains(v, StringComparison.Ordinal)))
                {
                    contaminated++;
                    continue;
                }
                if (policy.DedupEnabled)
                {
                    var key = (prompt, completion);
                    if (!seen.Add(key)) { deduped++; continue; }
                }
                kept.Add(record);
            }
            if (kept.Count > 0) cleaned[scope] = kept;
        }
        return (cleaned, new Dictionary<string, object?>
        {
            ["deduped"] = deduped,
            ["contaminated_dropped"] = contaminated,
        });
    }

    private static (Dictionary<string, List<Dictionary<string, object?>>> capped,
                    int truncated) ApplyDatasetCap(
        Dictionary<string, List<Dictionary<string, object?>>> examples, int cap)
    {
        if (cap <= 0) return (examples, 0);
        var flat = new List<(string Scope, Dictionary<string, object?> Record)>();
        foreach (string scope in examples.Keys.OrderBy(s => s, StringComparer.Ordinal))
            foreach (var record in examples[scope])
                flat.Add((scope, record));
        if (flat.Count <= cap) return (examples, 0);
        var output = new Dictionary<string, List<Dictionary<string, object?>>>();
        foreach (var (scope, record) in flat.Take(cap))
        {
            if (!output.TryGetValue(scope, out var list))
                output[scope] = list = new List<Dictionary<string, object?>>();
            list.Add(record);
        }
        return (output, flat.Count - cap);
    }

    private static Dictionary<string, object?>? QualityDriftStatus(
        SelfLearningPolicy policy, IReadOnlyDictionary<string, object?> stats)
    {
        double floor = policy.MinPoolAvgQuality;
        if (floor <= 0) return null;
        double avg = TransformerTrainingRepository.Num(stats, "pool_avg_quality");
        if (avg < floor)
            return Blocked("quality-drift",
                ("pool_avg_quality", Math.Round(avg, 4)),
                ("min_pool_avg_quality", floor));
        return null;
    }

    private static Dictionary<string, object?>? SyntheticRatioStatus(
        SelfLearningPolicy policy, IReadOnlyDictionary<string, object?> stats)
    {
        double cap = policy.MaxSyntheticRatio;
        if (cap <= 0 ||
            TransformerTrainingRepository.Int(stats, "total") == 0)
            return null;
        double ratio = TransformerTrainingRepository.Num(stats, "synthetic_ratio");
        if (ratio > cap)
            return Blocked("synthetic-ratio-exceeded",
                ("synthetic_count",
                 TransformerTrainingRepository.Int(stats, "synthetic_count")),
                ("total_examples",
                 TransformerTrainingRepository.Int(stats, "total")),
                ("synthetic_ratio", Math.Round(ratio, 4)),
                ("max_synthetic_ratio", cap));
        return null;
    }

    private static (Dictionary<string, object?>? course,
                    Dictionary<string, object?>? blocked) SelectCurriculum(
        SelfLearningPolicy policy, string tool)
    {
        if (!policy.CurriculumEnabled)
            return (new Dictionary<string, object?>
            {
                ["course"] = "sft", ["curriculum_enabled"] = false,
            }, null);
        string path = Path.Combine(tool, XcPaths.MaturityStateRel);
        int certified;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            certified = doc.RootElement.GetProperty("certified_level").GetInt32();
        }
        catch (Exception)
        {
            return (null, Blocked("maturity-state-unavailable",
                ("maturity_state", path)));
        }
        if (certified < 4)
            return (null, Blocked("curriculum-foundation-unmet",
                ("certified_level", certified)));
        int? target;
        string course;
        if (certified >= 7) { course = "sft-refresh"; target = null; }
        else { target = certified + 1; course = CurriculumCourses[target.Value]; }
        return (new Dictionary<string, object?>
        {
            ["course"] = course,
            ["target_level"] = target,
            ["certified_level"] = certified,
            ["curriculum_enabled"] = true,
        }, null);
    }

    /// <summary>Baseline-vs-active degradation probe: modeltool eval on the
    /// active bundle, compared against the suite's baseline_metrics with the
    /// tps gates removed (the Python lane popped them before comparing).</summary>
    private static Dictionary<string, object?>? DegradationProbe(
        SelfLearningPolicy policy, string tool,
        ModelLifecycle lifecycle)
    {
        if (!policy.DegradationProbeEnabled) return null;
        string suitePath = Path.Combine(
            tool, "xingcheng", "eval",
            $"{policy.DegradationProbeSuite}.json");
        if (!File.Exists(suitePath))
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["degraded"] = false,
                ["suite"] = policy.DegradationProbeSuite,
                ["error"] = "suite-missing",
            };
        try
        {
            var active = lifecycle.ActiveWeights();
            string? activePath = active == null
                ? null
                : TransformerTrainingRepository.Str(active, "path");
            if (activePath == null || !File.Exists(activePath))
                return new Dictionary<string, object?>
                {
                    ["ok"] = false, ["degraded"] = false,
                    ["suite"] = policy.DegradationProbeSuite,
                    ["error"] = "active-weights-missing",
                };
            string bundle = Evaluation.BundleDirOf(activePath);
            var run = NativeTools.Run(
                NativeTools.ModelToolExe(tool),
                new[] { "eval", "--bundle", bundle, "--suite", suitePath },
                tool,
                Path.Combine(tool, XcPaths.LogsRel, "probe-stderr.log"),
                timeoutS: 7200);
            var output = ParseJson(run.StdoutTail.Trim());
            var metrics = output != null &&
                          output.TryGetValue("candidate", out object? c) &&
                          c is Dictionary<string, object?> cand
                ? cand : new Dictionary<string, object?>();
            using var suiteDoc = JsonDocument.Parse(File.ReadAllText(suitePath));
            var suite = new Dictionary<string, object?>();
            foreach (var p in suiteDoc.RootElement.EnumerateObject())
                suite[p.Name] = ModelLifecycle.Decode(p.Value);
            var gates = suite.TryGetValue("quality_gates", out object? g) &&
                        g is Dictionary<string, object?> gg
                ? new Dictionary<string, object?>(gg)
                : new Dictionary<string, object?>();
            gates.Remove("min_tokens_per_second");
            gates.Remove("min_tps_baseline_ratio");
            var baseline = suite.TryGetValue("baseline_metrics", out object? b) &&
                           b is Dictionary<string, object?> bm
                ? bm : new Dictionary<string, object?>();
            var (comparison, passed) = CompareMetrics(baseline, metrics, gates);
            return new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["degraded"] = !passed,
                ["suite"] = policy.DegradationProbeSuite,
                ["metrics"] = metrics,
                ["comparison"] = comparison,
                ["tps_gate"] = "not-applicable-degradation-probe",
            };
        }
        catch (Exception exc)
        {
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["degraded"] = false,
                ["suite"] = policy.DegradationProbeSuite,
                ["error"] = $"{exc.GetType().Name}: {exc.Message}",
            };
        }
    }

    /// <summary>Port of compare_metrics (star-native-eval-suite gates).</summary>
    internal static (Dictionary<string, object?> comparison, bool passed)
        CompareMetrics(IReadOnlyDictionary<string, object?> baseline,
                       IReadOnlyDictionary<string, object?> candidate,
                       IReadOnlyDictionary<string, object?> gates)
    {
        double maxRegression = TransformerTrainingRepository.Num(
            gates, "max_perplexity_regression_pct") is double m && m > 0 ? m : 5.0;
        bool requireGeneration = !gates.TryGetValue(
            "require_generation", out object? rg) || rg is not bool b || b;
        double minTps = TransformerTrainingRepository.Num(
            gates, "min_tokens_per_second");
        double tpsRatio = TransformerTrainingRepository.Num(
            gates, "min_tps_baseline_ratio");

        bool bp = baseline.TryGetValue("perplexity", out object? bv) &&
                  bv is double or int or long or float;
        bool cp = candidate.TryGetValue("perplexity", out object? cv) &&
                  cv is double or int or long or float;
        double basePpl = bp ? Convert.ToDouble(bv) : 0;
        double candPpl = cp ? Convert.ToDouble(cv) : 0;
        double? pplDelta = null;
        bool pplOk = true;
        if (bp && cp && basePpl > 0)
        {
            pplDelta = (candPpl - basePpl) / basePpl * 100.0;
            pplOk = pplDelta <= maxRegression;
        }
        bool generationOk = !requireGeneration ||
            (candidate.TryGetValue("generation_ok", out object? g) &&
             g is bool ok && ok);
        double candTps = TransformerTrainingRepository.Num(
            candidate, "tokens_per_second");
        double baseTps = TransformerTrainingRepository.Num(
            baseline, "tokens_per_second");
        bool tpsOk = minTps > 0 ? candTps >= minTps : true;
        bool tpsRatioOk = tpsRatio > 0
            ? baseTps > 0 && candTps >= baseTps * tpsRatio
            : true;
        tpsOk = tpsOk && tpsRatioOk;
        bool passed = pplOk && generationOk && tpsOk;
        return (new Dictionary<string, object?>
        {
            ["perplexity_delta_pct"] = pplDelta,
            ["perplexity_ok"] = pplOk,
            ["generation_ok"] = generationOk,
            ["tokens_per_second_ok"] = tpsOk,
            ["tokens_per_second_ratio_ok"] = tpsRatioOk,
            ["baseline_tokens_per_second"] = baseTps > 0 ? baseTps : null,
            ["passed"] = passed,
        }, passed);
    }

    private static Dictionary<string, object?>? ParseJson(string raw)
    {
        int start = raw.IndexOf('{');
        if (start < 0) return null;
        try
        {
            using var doc = JsonDocument.Parse(raw[start..]);
            var map = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                map[p.Name] = ModelLifecycle.Decode(p.Value);
            return map;
        }
        catch { return null; }
    }

    // ------------------------------------------------------------- cycle --

    public static Dictionary<string, object?> RunCycle(
        string toolRoot, SelfLearningPolicy? policy = null, bool force = false)
    {
        var result = RunCycleImpl(toolRoot, policy, force);
        if (result.TryGetValue("action", out object? a) &&
            a?.ToString() == "disabled")
            return result;
        Dictionary<string, object?> retention;
        try
        {
            retention = Retention.ApplyRetention(toolRoot);
        }
        catch (Exception exc)
        {
            retention = new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = $"{exc.GetType().Name}:{exc.Message}",
            };
        }
        result = new Dictionary<string, object?>(result)
        {
            ["retention"] = retention,
        };
        return result;
    }

    public static Dictionary<string, object?> RunCycleImpl(
        string toolRoot, SelfLearningPolicy? policy = null, bool force = false)
    {
        string tool = Path.GetFullPath(toolRoot);
        var resolvedPolicy = policy ?? SelfLearningPolicy.Load(tool);
        var state = SelfLearningState.Load(tool);
        var policyDict = resolvedPolicy.ToDict();

        if (!resolvedPolicy.Enabled)
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "disabled",
                ["policy"] = policyDict, ["checked_at"] = IsoNow(),
            };

        var gate = FailureBreakerStatus(resolvedPolicy, state);
        if (gate != null)
        {
            gate["policy"] = policyDict; gate["checked_at"] = IsoNow();
            return gate;
        }

        var window = resolvedPolicy.TrainingWindowStatus();
        if (!TransformerTrainingRepository.Truthy(window["allowed"]))
        {
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "blocked",
                ["reason"] = window["reason"],
                ["training_window"] = window,
                ["policy"] = policyDict, ["checked_at"] = IsoNow(),
            };
        }

        foreach (var g in new[]
                 {
                     MinIntervalStatus(resolvedPolicy, state),
                     DailyBudgetStatus(resolvedPolicy, state),
                     GpuBusyBackoffStatus(resolvedPolicy, state),
                 })
        {
            if (g == null) continue;
            g["policy"] = policyDict; g["checked_at"] = IsoNow();
            return g;
        }

        if (resolvedPolicy.InferenceExclusion)
        {
            bool? inferenceActive = Collectors.InferenceActive(tool);
            if (inferenceActive != false)
            {
                return new Dictionary<string, object?>
                {
                    ["ok"] = true, ["action"] = "blocked",
                    ["reason"] = inferenceActive == true
                        ? "inference-active" : "inference-state-unavailable",
                    ["policy"] = policyDict, ["checked_at"] = IsoNow(),
                };
            }
        }

        // DPO branch: sufficient new paired preference rows route the cycle
        // to preference optimization instead of SFT.
        if (resolvedPolicy.DpoEnabled)
        {
            var pairs = Collectors.CollectPreferencePairs(tool);
            int newPairs = pairs.Count -
                TransformerTrainingRepository.Int(state, "trained_pair_total");
            int dpoThreshold = force ? 1
                : Math.Max(1, resolvedPolicy.DpoMinNewPairs);
            if (newPairs >= dpoThreshold)
                return RunDpoCycle(tool, resolvedPolicy, state, pairs);
        }

        var examples = Collectors.CollectVerifiedExamples(tool);
        if (examples.Count == 0)
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "idle",
                ["reason"] = "no-verified-examples",
                ["total_examples"] = 0,
            };

        var (cleaned, sanitizeStats) = SanitizePool(resolvedPolicy, examples);
        examples = cleaned;
        if (examples.Count == 0)
        {
            var idle = new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "idle",
                ["reason"] = "pool-empty-after-sanitize",
                ["policy"] = policyDict, ["checked_at"] = IsoNow(),
            };
            foreach (var kv in sanitizeStats) idle[kv.Key] = kv.Value;
            return idle;
        }

        var stats = PoolStats(resolvedPolicy, examples);
        foreach (var kv in sanitizeStats) stats[kv.Key] = kv.Value;
        foreach (var g in new[]
                 {
                     QualityDriftStatus(resolvedPolicy, stats),
                     SyntheticRatioStatus(resolvedPolicy, stats),
                 })
        {
            if (g == null) continue;
            g["policy"] = policyDict; g["checked_at"] = IsoNow();
            return g;
        }

        int total = TransformerTrainingRepository.Int(stats, "total");
        int trainedTotal = TransformerTrainingRepository.Int(
            state, "trained_example_total");
        int newExamples = Math.Max(0, total - trainedTotal);
        Dictionary<string, object?>? degradationTrigger = null;
        if (!force && newExamples < resolvedPolicy.MinNewExamples)
        {
            string lifecycleDirForProbe = Path.Combine(
                tool, XcPaths.LifecycleRel);
            var lifecycleProbe = ModelLifecycle.LoadOrCreate(
                lifecycleDirForProbe, XcPaths.ModelId);
            var probe = DegradationProbe(resolvedPolicy, tool, lifecycleProbe);
            if (probe != null &&
                TransformerTrainingRepository.Truthy(probe["degraded"]))
            {
                if (newExamples < resolvedPolicy.DegradationMinExamples)
                    return new Dictionary<string, object?>
                    {
                        ["ok"] = true, ["action"] = "idle",
                        ["reason"] = "degradation-detected-insufficient-data",
                        ["degradation_probe"] = probe,
                        ["total_examples"] = total,
                        ["new_examples"] = newExamples,
                        ["threshold"] = resolvedPolicy.MinNewExamples,
                        ["policy"] = policyDict, ["checked_at"] = IsoNow(),
                    };
                degradationTrigger = probe;
            }
            else
            {
                var result = new Dictionary<string, object?>
                {
                    ["ok"] = true, ["action"] = "idle",
                    ["reason"] = "below-threshold",
                    ["total_examples"] = total,
                    ["new_examples"] = newExamples,
                    ["threshold"] = resolvedPolicy.MinNewExamples,
                };
                if (probe != null) result["degradation_probe"] = probe;
                return result;
            }
        }

        var (curriculum, curriculumBlocked) = SelectCurriculum(resolvedPolicy, tool);
        if (curriculumBlocked != null)
        {
            curriculumBlocked["policy"] = policyDict;
            curriculumBlocked["checked_at"] = IsoNow();
            return curriculumBlocked;
        }
        var courseIntents = resolvedPolicy.CurriculumIntentMap
            .TryGetValue((string)curriculum!["course"]!, out var intents)
            ? intents : null;
        if (courseIntents != null && courseIntents.Count > 0)
        {
            var wanted = courseIntents.ToHashSet(StringComparer.Ordinal);
            examples = examples
                .Select(kv => (kv.Key, Records: kv.Value
                    .Where(r => wanted.Contains(
                        r["intent"]?.ToString() ?? ""))
                    .ToList()))
                .Where(kv => kv.Records.Count > 0)
                .ToDictionary(kv => kv.Key, kv => kv.Records);
            if (examples.Count == 0)
            {
                var blocked = Blocked("course-dataset-empty",
                    ("course", curriculum["course"]),
                    ("intents", wanted.OrderBy(s => s).ToList()));
                blocked["policy"] = policyDict;
                blocked["checked_at"] = IsoNow();
                return blocked;
            }
            curriculum["intent_filter"] = wanted.OrderBy(s => s).ToList();
            total = examples.Values.Sum(r => r.Count);
            newExamples = Math.Max(0, total - trainedTotal);
        }

        var (capped, datasetCapTruncated) = ApplyDatasetCap(
            examples, resolvedPolicy.MaxDatasetExamples);
        examples = capped;
        int datasetExamples = examples.Values.Sum(r => r.Count);

        string snapshotDir = Path.Combine(tool, XcPaths.SelfLearningSnapshotRel);
        Directory.CreateDirectory(snapshotDir);
        string snapshotPath = Path.Combine(snapshotDir,
            $"sft-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        // §5 provenance: stamp the producing generation + active weights
        // version on every collected record.
        string sftGeneration = GenerationMigration.CurrentGeneration(tool);
        string sftModelVersion =
            $"w{ModelLifecycle.LoadOrCreate(
                Path.Combine(tool, XcPaths.LifecycleRel),
                XcPaths.ModelId).ActiveWeightsVersion}";
        Dictionary<string, object?>? snapshot = null;
        Exception? lastError = null;
        foreach (int permille in new[]
                 {
                     resolvedPolicy.ValPermille, 200, 350, 500,
                 })
        {
            try
            {
                snapshot = SftDataset.BuildSftDataset(
                    outputPath: snapshotPath,
                    examplesByScope: examples,
                    valPermille: permille,
                    generation: sftGeneration,
                    modelVersion: sftModelVersion);
                break;
            }
            catch (Exception ex) when (ex is ArgumentException or InvalidOperationException)
            {
                lastError = ex;
            }
        }
        if (snapshot == null)
        {
            var failure = new Dictionary<string, object?>
            {
                ["ok"] = false, ["action"] = "blocked",
                ["reason"] = $"dataset-split-unavailable:{lastError?.Message}",
                ["total_examples"] = total,
            };
            var failState = new Dictionary<string, object?>(state)
            {
                ["last_run_at"] = IsoNow(),
                ["last_action"] = "training-failed",
                ["last_error"] = failure["reason"],
                ["consecutive_failures"] = TransformerTrainingRepository.Int(
                    state, "consecutive_failures") + 1,
            };
            SelfLearningState.Save(tool, failState);
            return failure;
        }

        var repository = new TransformerTrainingRepository(tool);
        var dataset = repository.CreateDataset(
            contentSha256: (string)snapshot["content_sha256"]!,
            snapshotPath: (string)snapshot["snapshot_path"]!,
            snapshotSha256: (string)snapshot["snapshot_sha256"]!,
            examples: (List<Dictionary<string, object?>>)snapshot["examples"]!,
            sourceManifest: MergeOrigin(
                (Dictionary<string, object?>)snapshot["source_manifest"]!,
                "self-learning"),
            createdBy: "star-self-learning");

        string lifecycleDir = Path.Combine(tool, XcPaths.LifecycleRel);
        var lifecycle = ModelLifecycle.LoadOrCreate(lifecycleDir, XcPaths.ModelId);
        var active = lifecycle.ActiveWeights();
        string? activePathStr = active == null
            ? null
            : TransformerTrainingRepository.Str(active, "path");
        if (activePathStr == null ||
            !(File.Exists(activePathStr) || Directory.Exists(activePathStr)))
        {
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["action"] = "blocked",
                ["reason"] = "active-weights-missing",
                ["total_examples"] = total,
            };
        }
        // init anchor = bundle dir (manifest parent) or the .xcn itself.
        string activePath = Path.GetFileName(activePathStr) == "manifest.json" &&
                            File.Exists(activePathStr)
            ? Path.GetDirectoryName(activePathStr)!
            : activePathStr;
        string repositoryRoot = Path.GetFullPath(repository.ToolRoot);
        string initRelative = Path.GetRelativePath(repositoryRoot, activePath)
            .Replace(Path.DirectorySeparatorChar, '/');

        if (FrozenResult(resolvedPolicy, tool, state, dataset,
                         total, newExamples) is { } frozenSft)
            return frozenSft;

        var job = repository.CreateTrainingJob(
            datasetId: (string)dataset["dataset_id"]!,
            configuration: new Dictionary<string, object?>
            {
                ["training_kind"] = "sft",
                ["tokenizer_dir"] =
                    "runtime/tokenizers/xingcheng-bpe-8k-20260919-120054",
                ["init_checkpoint"] = initRelative,
                ["preset"] = "base",
                ["max_length"] = resolvedPolicy.MaxLength,
                ["batch_size"] = resolvedPolicy.BatchSize,
                ["grad_accum"] = resolvedPolicy.GradAccum,
                ["lr"] = resolvedPolicy.Lr,
                ["max_steps"] = resolvedPolicy.MaxSteps,
                ["warmup_steps"] = resolvedPolicy.WarmupSteps,
                ["checkpoint_every"] = Math.Max(1, resolvedPolicy.MaxSteps / 2),
                ["eval_every"] = Math.Max(1, resolvedPolicy.MaxSteps / 2),
                ["log_every"] = Math.Max(1, resolvedPolicy.MaxSteps / 8),
                ["device"] = resolvedPolicy.Device,
                ["gpu_required_mb"] = resolvedPolicy.GpuRequiredMb,
                ["max_train_seconds"] = resolvedPolicy.TrainTimeBudgetS,
                ["max_train_vram_mb"] = resolvedPolicy.TrainVramBudgetMb,
                ["max_train_gpu_seconds"] = resolvedPolicy.TrainGpuBudgetS,
                ["curriculum_course"] = curriculum["course"],
                ["maturity_target_level"] = curriculum
                    .GetValueOrDefault("target_level"),
            },
            requestedBy: "star-self-learning");

        return ExecuteGovernedCycle(
            tool, resolvedPolicy, state,
            repository, repositoryRoot, lifecycle, lifecycleDir,
            activePath, job, dataset, snapshot, curriculum,
            total, newExamples, datasetExamples, datasetCapTruncated,
            degradationTrigger, stats,
            trainedCounterField: "trained_example_total",
            metricsPhase: "supervised-fine-tuning");
    }

    private static Dictionary<string, object?> MergeOrigin(
        Dictionary<string, object?> manifest, string origin)
    {
        var merged = new Dictionary<string, object?>(manifest)
        {
            ["origin"] = origin,
        };
        return merged;
    }

    /// <summary>Architecture-convergence freeze gate: when
    /// <c>capability_training_frozen</c> is set the cycle keeps its
    /// collect/sanitize/dedup/register work (the dataset snapshot above
    /// is already registered) but must not create a trainer job or
    /// touch active weights. Returns null when not frozen.</summary>
    private static Dictionary<string, object?>? FrozenResult(
        SelfLearningPolicy policy, string tool,
        Dictionary<string, object?> state,
        IReadOnlyDictionary<string, object?> dataset,
        int total, int newCount)
    {
        if (!policy.CapabilityTrainingFrozen) return null;
        SelfLearningState.Save(tool, new Dictionary<string, object?>(state)
        {
            ["last_run_at"] = IsoNow(),
            ["last_action"] = "frozen",
        });
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["action"] = "frozen",
            ["reason"] = "capability-training-frozen",
            ["dataset_id"] = dataset["dataset_id"],
            ["total_examples"] = total,
            ["new_examples"] = newCount,
        };
    }

    private static Dictionary<string, object?> RunDpoCycle(
        string tool, SelfLearningPolicy policy,
        Dictionary<string, object?> state,
        List<Dictionary<string, object?>> pairs)
    {
        var repository = new TransformerTrainingRepository(tool);
        string repositoryRoot = Path.GetFullPath(repository.ToolRoot);
        string lifecycleDir = Path.Combine(tool, XcPaths.LifecycleRel);
        var lifecycle = ModelLifecycle.LoadOrCreate(lifecycleDir, XcPaths.ModelId);
        var active = lifecycle.ActiveWeights();
        string? activePathStr = active == null
            ? null
            : TransformerTrainingRepository.Str(active, "path");
        if (activePathStr == null ||
            !(File.Exists(activePathStr) || Directory.Exists(activePathStr)))
        {
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "blocked",
                ["reason"] = "active-weights-missing",
                ["pairs_total"] = pairs.Count,
                ["checked_at"] = IsoNow(),
            };
        }
        string activePath = Path.GetFileName(activePathStr) == "manifest.json" &&
                            File.Exists(activePathStr)
            ? Path.GetDirectoryName(activePathStr)!
            : activePathStr;

        string snapshotDir = Path.Combine(tool, XcPaths.SelfLearningSnapshotRel);
        Directory.CreateDirectory(snapshotDir);
        string snapshotPath = Path.Combine(snapshotDir,
            $"dpo-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        Dictionary<string, object?> manifest;
        try
        {
            manifest = SftDataset.BuildPairsSnapshot(
                pairs, snapshotPath, valPermille: policy.ValPermille,
                generation: GenerationMigration.CurrentGeneration(tool),
                modelVersion: $"w{lifecycle.ActiveWeightsVersion}");
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException)
        {
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "blocked",
                ["reason"] = $"preference-snapshot-unavailable:{ex.Message}",
                ["pairs_total"] = pairs.Count,
                ["checked_at"] = IsoNow(),
            };
        }
        var dataset = SftDataset.RegisterPairsSnapshot(
            repository, manifest, createdBy: "star-self-learning",
            generation: GenerationMigration.CurrentGeneration(tool),
            modelVersion: $"w{lifecycle.ActiveWeightsVersion}");

        int pairsTotal = pairs.Count;
        int newPairs = Math.Max(0, pairsTotal -
            TransformerTrainingRepository.Int(state, "trained_pair_total"));
        string initRelative = Path.GetRelativePath(repositoryRoot, activePath)
            .Replace(Path.DirectorySeparatorChar, '/');
        if (FrozenResult(policy, tool, state, dataset,
                         pairsTotal, newPairs) is { } frozenDpo)
            return frozenDpo;
        var job = repository.CreateTrainingJob(
            datasetId: (string)dataset["dataset_id"]!,
            configuration: new Dictionary<string, object?>
            {
                ["training_kind"] = "dpo",
                ["tokenizer_dir"] =
                    "runtime/tokenizers/xingcheng-bpe-8k-20260919-120054",
                ["init_checkpoint"] = initRelative,
                ["beta"] = policy.DpoBeta,
                ["max_length"] = policy.MaxLength,
                ["batch_size"] = policy.BatchSize,
                ["lr"] = policy.Lr,
                ["max_steps"] = policy.MaxSteps,
                ["checkpoint_every"] = Math.Max(1, policy.MaxSteps / 2),
                ["log_every"] = Math.Max(1, policy.MaxSteps / 8),
                ["device"] = policy.Device,
                ["gpu_required_mb"] = policy.GpuRequiredMb,
                ["max_train_seconds"] = policy.TrainTimeBudgetS,
                ["max_train_vram_mb"] = policy.TrainVramBudgetMb,
                ["max_train_gpu_seconds"] = policy.TrainGpuBudgetS,
                ["curriculum_course"] = "dpo-alignment",
            },
            requestedBy: "star-self-learning");

        var snapshot = new Dictionary<string, object?>
        {
            ["manifest"] = new Dictionary<string, object?>(manifest)
            {
                ["example_count"] = Convert.ToInt32(manifest["pairs"]),
            },
        };
        return ExecuteGovernedCycle(
            tool, policy, state,
            repository, repositoryRoot, lifecycle, lifecycleDir,
            activePath, job, dataset, snapshot,
            curriculum: new Dictionary<string, object?>
            {
                ["course"] = "dpo-alignment",
                ["curriculum_enabled"] = true,
            },
            total: pairsTotal, newExamples: newPairs,
            datasetExamples: Convert.ToInt32(manifest["pairs"]),
            datasetCapTruncated: 0, degradationTrigger: null,
            stats: new Dictionary<string, object?>
            {
                ["pairs_total"] = pairsTotal,
                ["new_pairs"] = newPairs,
            },
            trainedCounterField: "trained_pair_total",
            metricsPhase: "preference-optimization");
    }

    private static Dictionary<string, object?> ResourceAccount(
        SelfLearningPolicy policy,
        IReadOnlyDictionary<string, object?> trainerSummary,
        IReadOnlyDictionary<string, object?>? snapshot)
    {
        var resource = trainerSummary.TryGetValue("resource", out object? r) &&
                       r is Dictionary<string, object?> res
            ? res : new Dictionary<string, object?>();
        var manifest = snapshot != null &&
                       snapshot.TryGetValue("manifest", out object? m) &&
                       m is Dictionary<string, object?> mm
            ? mm : new Dictionary<string, object?>();
        return new Dictionary<string, object?>
        {
            ["elapsed_seconds"] = trainerSummary.GetValueOrDefault("elapsed_seconds"),
            ["steps"] = trainerSummary.GetValueOrDefault("steps"),
            ["device"] = policy.Device,
            ["dataset_example_count"] = manifest.GetValueOrDefault("example_count"),
            ["train_count"] = manifest.GetValueOrDefault("train_count"),
            ["validation_count"] = manifest.GetValueOrDefault("validation_count"),
            ["peak_rss_mb"] = resource.GetValueOrDefault("peak_rss_mb"),
            ["rss_budget_mb"] = resource.GetValueOrDefault("rss_budget_mb"),
            ["rss_samples"] = resource.GetValueOrDefault("rss_samples"),
            ["gpu_seconds"] = trainerSummary.GetValueOrDefault("gpu_seconds"),
            ["gpu_memory_peak_mb"] =
                trainerSummary.GetValueOrDefault("gpu_memory_peak_mb"),
            ["gpu_time_budget_s"] =
                policy.TrainGpuBudgetS > 0 ? policy.TrainGpuBudgetS : null,
        };
    }

    private static Dictionary<string, object?> ExecuteGovernedCycle(
        string tool, SelfLearningPolicy policy,
        Dictionary<string, object?> state,
        TransformerTrainingRepository repository,
        string repositoryRoot,
        ModelLifecycle lifecycle, string lifecycleDir,
        string activePath,
        IReadOnlyDictionary<string, object?> job,
        IReadOnlyDictionary<string, object?> dataset,
        IReadOnlyDictionary<string, object?> snapshot,
        IReadOnlyDictionary<string, object?> curriculum,
        int total, int newExamples, int datasetExamples,
        int datasetCapTruncated,
        Dictionary<string, object?>? degradationTrigger,
        IReadOnlyDictionary<string, object?> stats,
        string trainedCounterField,
        string metricsPhase)
    {
        string jobId = (string)job["job_id"]!;
        // Count the cycle before training: a crash mid-run must still
        // consume budget (fail-closed, same as Python lane).
        string today = DateTime.UtcNow.ToString("yyyy-MM-dd");
        int count = state.TryGetValue("cycles_today", out object? c) &&
                    c is Dictionary<string, object?> counter &&
                    TransformerTrainingRepository.Str(counter, "date") == today
            ? TransformerTrainingRepository.Int(counter, "count") : 0;
        state = new Dictionary<string, object?>(state)
        {
            ["cycles_today"] = new Dictionary<string, object?>
            {
                ["date"] = today, ["count"] = count + 1,
            },
            ["last_run_at"] = IsoNow(),
        };
        SelfLearningState.Save(tool, state);

        var executor = new TrainingJobExecutor(repository, tool);
        var report = executor.RunJob(jobId);
        if (!TransformerTrainingRepository.Truthy(report["ok"]))
        {
            string errorCode = TransformerTrainingRepository
                .Str(report, "error_code") ?? "";
            var gpuFields = errorCode == "EXECUTOR_GPU_BUSY"
                ? GpuBusyRecord(policy, state)
                : new Dictionary<string, object?>
                {
                    ["gpu_busy_streak"] = 0,
                    ["gpu_busy_until"] = null,
                };
            var failure = new Dictionary<string, object?>
            {
                ["ok"] = false, ["action"] = "training-failed",
                ["job_id"] = jobId,
                ["error_code"] = report.GetValueOrDefault("error_code"),
                ["error_message"] = report.GetValueOrDefault("error_message"),
                ["total_examples"] = total,
            };
            if (gpuFields["gpu_busy_until"] != null)
                failure["gpu_busy_backoff_s"] = gpuFields["gpu_busy_backoff_s"];
            var failState = new Dictionary<string, object?>(state);
            foreach (var kv in gpuFields) failState[kv.Key] = kv.Value;
            failState["last_run_at"] = IsoNow();
            failState["last_action"] = "training-failed";
            failState["last_job_id"] = jobId;
            failState["last_error"] = failure["error_message"];
            failState["consecutive_failures"] = TransformerTrainingRepository
                .Int(state, "consecutive_failures") + 1;
            SelfLearningState.Save(tool, failState);
            failure["report"] = WriteReport(tool, failure);
            return failure;
        }

        var trainerSummary = report.TryGetValue("summary", out object? s) &&
                             s is Dictionary<string, object?> ts
            ? ts : new Dictionary<string, object?>();
        string? stoppedReason = TransformerTrainingRepository
            .Str(trainerSummary, "stopped_reason");
        if (stoppedReason != null)
        {
            var failure = new Dictionary<string, object?>
            {
                ["ok"] = false, ["action"] = "resource-overspend",
                ["job_id"] = jobId,
                ["stopped_reason"] = stoppedReason,
                ["total_examples"] = total,
                ["resource_account"] = ResourceAccount(
                    policy, trainerSummary, snapshot),
            };
            var failState = new Dictionary<string, object?>(state)
            {
                ["gpu_busy_streak"] = 0,
                ["gpu_busy_until"] = null,
                ["last_run_at"] = IsoNow(),
                ["last_action"] = "resource-overspend",
                ["last_job_id"] = jobId,
                ["last_error"] = stoppedReason,
                ["consecutive_failures"] = TransformerTrainingRepository.Int(
                    state, "consecutive_failures") + 1,
            };
            SelfLearningState.Save(tool, failState);
            failure["report"] = WriteReport(tool, failure);
            return failure;
        }

        string artifact = Path.GetFullPath(
            report["output_path"]!.ToString()!);
        string artifactRelative = Path.GetRelativePath(repositoryRoot, artifact)
            .Replace(Path.DirectorySeparatorChar, '/');
        var adapter = repository.RegisterAdapterCandidate(
            jobId: jobId,
            artifactPath: artifactRelative,
            metrics: new Dictionary<string, object?>
            {
                ["phase"] = metricsPhase,
                ["origin"] = "self-learning",
                ["new_examples"] = newExamples,
                ["total_examples"] = total,
            });
        string adapterId = (string)adapter["adapter_id"]!;

        var evaluations = new List<object?>();
        bool allPassed = true;
        foreach (string suiteName in policy.Suites)
        {
            string suitePath = Path.Combine(
                tool, "xingcheng", "eval", $"{suiteName}.json");
            if (!File.Exists(suitePath))
            {
                allPassed = false;
                evaluations.Add(new Dictionary<string, object?>
                {
                    ["suite"] = suiteName,
                    ["passed"] = false,
                    ["error"] = "suite-missing",
                });
                continue;
            }
            var evalResult = Evaluation.RunEvaluation(
                repository,
                adapterId: adapterId,
                candidateArtifact: artifact,
                suitePath: suitePath,
                baselineArtifact: activePath,
                evaluatedBy: "star-self-learning",
                chat: true);
            evaluations.Add(new Dictionary<string, object?>
            {
                ["suite"] = suiteName,
                ["passed"] = TransformerTrainingRepository.Truthy(
                    evalResult["passed"]),
                ["comparison"] = evalResult.GetValueOrDefault("comparison"),
            });
            allPassed = allPassed &&
                TransformerTrainingRepository.Truthy(evalResult["passed"]);
        }

        string action = "rejected";
        var released = new List<object?>();
        string? pinned = null;
        bool pruned = false;
        Dictionary<string, object?>? newEntry = null;
        if (allPassed)
        {
            var staged = repository.ReleaseAdapter(
                adapterId, "stage",
                governedBy: "self-learning-policy",
                reason: $"auto: {newExamples} new verified examples");
            released.Add(staged["status"]);
            action = "staged";
            if (policy.AutoActivate)
            {
                var activated = repository.ReleaseAdapter(
                    adapterId, "activate",
                    governedBy: "self-learning-policy",
                    reason: "auto-activate after all evaluation gates passed");
                released.Add(activated["status"]);
                newEntry = lifecycle.RegisterArtifact(
                    "weights", artifact,
                    metadata: new Dictionary<string, object?>
                    {
                        ["job_id"] = jobId,
                        ["adapter_id"] = adapterId,
                        ["dataset_id"] = dataset["dataset_id"],
                        ["origin"] = "self-learning",
                        ["new_examples"] = newExamples,
                        ["config_sha256"] =
                            WeightsConfigFingerprint(artifact),
                    },
                    activate: true);
                lifecycle.Save(lifecycleDir);
                pinned = PinRuntimeCheckpoint(tool, artifact);
                action = "upgraded";
            }
        }

        var resourceAccount = ResourceAccount(policy, trainerSummary, snapshot);
        var summary = new Dictionary<string, object?>
        {
            ["ok"] = true, ["action"] = action,
            ["job_id"] = jobId,
            ["adapter_id"] = adapterId,
            ["dataset_id"] = dataset["dataset_id"],
            ["curriculum"] = curriculum,
            ["new_examples"] = newExamples,
            ["total_examples"] = total,
            ["dataset_examples"] = datasetExamples,
            ["dataset_cap_truncated"] = datasetCapTruncated,
            ["trigger"] = degradationTrigger != null
                ? "degradation-probe" : "data-threshold",
            ["degradation_probe"] = degradationTrigger,
            ["evaluations"] = evaluations,
            ["released"] = released,
            ["runtime_checkpoint"] = pinned,
            ["previous_weights_pruned"] = pruned,
            ["resource_account"] = resourceAccount,
            ["pool"] = stats,
            ["checked_at"] = IsoNow(),
        };

        bool recheckOk = true;
        if (action == "upgraded" && policy.PostUpgradeMaturityRecheck)
        {
            // The maturity ladder (certify/persist_report) has no native
            // port yet — recheck reports unavailable and the governed
            // rollback gate below treats that as fail-closed, exactly like
            // a failed recheck in the Python lane.
            recheckOk = false;
            summary["maturity_recheck"] = new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = "maturity-recheck-native-lane-unavailable",
            };
            summary["rollback"] = AttemptGovernedRollback(
                tool, lifecycle, lifecycleDir,
                anchorPath: activePath,
                excludeVersion: Convert.ToInt32(
                    newEntry?.GetValueOrDefault("version") ?? -1));
        }

        bool rolledBack = summary.TryGetValue("rollback", out object? rb) &&
                          rb is Dictionary<string, object?> rbm &&
                          TransformerTrainingRepository.Truthy(
                              rbm.GetValueOrDefault("rolled_back"));
        if (action == "upgraded" && recheckOk && !rolledBack)
            pruned = PruneSupersededGeneration(
                lifecycle, lifecycleDir, tool, activePath, artifact,
                newEntry);
        summary["previous_weights_pruned"] = pruned;

        var finalState = new Dictionary<string, object?>(state)
        {
            [trainedCounterField] = total,
            ["last_run_at"] = IsoNow(),
            ["last_action"] = action,
            ["last_job_id"] = jobId,
            ["last_adapter_id"] = adapterId,
            ["last_evaluations"] = evaluations,
            ["last_course"] = curriculum.GetValueOrDefault("course"),
            ["last_degradation_probe"] = summary.GetValueOrDefault("degradation_probe"),
            ["last_maturity_recheck"] = summary.GetValueOrDefault("maturity_recheck"),
            ["last_rollback"] = summary.GetValueOrDefault("rollback"),
            ["active_weights_version"] = lifecycle.ActiveWeightsVersion,
            ["resource_account"] = resourceAccount,
            ["pool"] = stats,
            ["consecutive_failures"] = 0,
            ["gpu_busy_streak"] = 0,
            ["gpu_busy_until"] = null,
            ["last_error"] = null,
        };
        SelfLearningState.Save(tool, finalState);
        summary["report"] = WriteReport(tool, summary);
        return summary;
    }
}
