// Policy.cs — self-learning / retention policy and state persistence.
//
// Ports of self_learning.py (policy + state + training window) and
// retention.py (policy). Fail-closed loading: unreadable/invalid files
// resolve to defaults; ``enabled=false`` disables everything.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class XcPaths
{
    public const string SelfLearningPolicyRel = "xingcheng/runtime/settings/self-learning.json";
    public const string SelfLearningStateRel = "xingcheng/runtime/state/self-learning.json";
    public const string SelfLearningSnapshotRel = "xingcheng/runtime/state/self-learning";
    public const string LogsRel = "xingcheng/runtime/logs";
    public const string RetentionPolicyRel = "xingcheng/runtime/settings/retention.json";
    public const string EngineSettingsRel = "xingcheng/runtime/settings/native-engine.json";
    public const string RetentionAuditRel = "xingcheng/runtime/logs/retention.jsonl";
    public const string JobsRel = "xingcheng/runtime/models/jobs";
    public const string LifecycleGlobRel = "xingcheng/runtime/models/lifecycle";
    public const string LifecycleRel = "xingcheng/runtime/models/lifecycle/xingcheng-native";
    public const string MaturityStateRel = "xingcheng/runtime/state/model-maturity.json";
    public const string ModelServiceRel = "xingcheng/runtime/ipc/model-service.json";
    public const string ModelId = "xingcheng-native";
    public const double MinQuality = 0.8;
    public static readonly string[] Scopes =
        { "coding", "investment", "main", "mathematical" };
    public static readonly string[] EvidencePathKeys =
        { "checkpoint", "checkpoint_path", "weights", "weights_path", "artifact_path" };
    public static readonly string[] ProbeValues =
        { "星火測試", "QZ-88", "13 + 29", "6 × 7" };

    // Xingcheng-owned settings live under the institution root
    // (XINGCHENG_INSTITUTION_ROOT/runtime/settings). A pre-migration copy
    // under the legacy local-model settings dir is still honored
    // read-only so a missing canonical file can never silently unlock a
    // policy default; writes always target the canonical path.
    public const string LegacySettingsDirRel = "runtime/settings";

    public static string SettingsReadPath(string toolRoot, string canonicalRel)
    {
        string canonical = Path.Combine(toolRoot, canonicalRel);
        if (File.Exists(canonical)) return canonical;
        string legacy = Path.Combine(
            toolRoot, LegacySettingsDirRel, Path.GetFileName(canonicalRel));
        return File.Exists(legacy) ? legacy : canonical;
    }

    public static string IsoNow()
        => DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'");
}

internal sealed class SelfLearningPolicy
{
    public const string Format = "star-self-learning-policy/v1";

    public bool Enabled = true;
    public int MinNewExamples = 24;
    public bool AutoActivate = true;
    public string[] Suites = { "star-native-eval-dialogue-20260921-125054" };
    public int MaxSteps = 400;
    public int BatchSize = 8;
    public int GradAccum = 2;
    public int MaxLength = 256;
    public double Lr = 5e-5;
    public int WarmupSteps = 20;
    public string Device = "cuda";
    public int GpuRequiredMb = 0;
    public int ValPermille = 100;
    public string TrainingTimezone = "Asia/Taipei";
    public string QuietHoursStart = "22:00";
    public string QuietHoursEnd = "07:00";
    public int MinIntervalS = 0;
    public int MaxCyclesPerDay = 0;
    public int MaxConsecutiveFailures = 0;
    public bool InferenceExclusion = true;
    public double MaxSyntheticRatio = 0.0;
    public string[] SyntheticSourcePrefixes = { "synthetic", "self-distillation" };
    public double MinPoolAvgQuality = 0.0;
    public bool CurriculumEnabled = false;
    public Dictionary<string, List<string>> CurriculumIntentMap = new();
    public bool PostUpgradeMaturityRecheck = false;
    public string MaturityRecheckDevice = "cpu";
    public bool DegradationProbeEnabled = false;
    public string DegradationProbeSuite = "star-native-eval-dialogue-20260921-125054";
    public int DegradationMinExamples = 1;
    public bool DedupEnabled = true;
    public bool ExcludeProbeValues = true;
    public int MaxDatasetExamples = 0;
    public int TrainTimeBudgetS = 0;
    public int TrainVramBudgetMb = 0;
    public int TrainGpuBudgetS = 0;
    public int GpuBusyBackoffS = 0;
    public int GpuBusyBackoffCapS = 0;
    public bool DpoEnabled = false;
    public int DpoMinNewPairs = 8;
    public double DpoBeta = 0.1;
    // Capability-training freeze (architecture-convergence phase): the
    // cycle still collects / sanitizes / deduplicates / registers and
    // runs evaluation gates, but the weight-changing stages (SFT/DPO/
    // pretrain jobs and candidate activation) never fire. Defaults true
    // — fail-closed; unfreezing requires an explicit policy edit.
    // SINGLE_CAPABILITY_RECOVERY lane (star-single-capability-recovery/v1):
    // "FROZEN" seals every weight-mutating job; "SINGLE_CAPABILITY_RECOVERY"
    // admits exactly one SFT lane whose capability == ActiveCapability.
    public bool CapabilityTrainingFrozen = true;
    public string CapabilityTrainingMode = "FROZEN";
    public string ActiveCapability = "";
    // Canonical-architecture pretrain lane (star-canonical-pretrain/v1):
    // while the capability freeze holds, a policy may declare
    // architecture_pretrain_mode = "XC_FUSED_1" to admit jobs with
    // training_kind=pretrain whose model.generation is the canonical
    // architecture contract. The lane exists so a genuine canonical
    // weight bootstrap can run under governance; it never widens the
    // capability freeze (SFT/DPO stay governed by CapabilityTrainingMode
    // and the recovery lane). Default DISABLED — fail-closed.
    public string ArchitecturePretrainMode = "DISABLED";
    // AutonomousCapabilityRecoveryLoop §3: OFF / COLLECT_ONLY /
    // DATASET_BUILD / PILOT_ONLY / GOVERNED_AUTONOMOUS. Production
    // starts COLLECT_ONLY; promotion to a higher lane requires every
    // §2 validation gate to have passed (handled by the loop gate,
    // never by editing this field mid-run).
    public string SelfTrainingMode = "COLLECT_ONLY";

    public Dictionary<string, object?> ToDict()
    {
        var d = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["enabled"] = Enabled,
            ["min_new_examples"] = MinNewExamples,
            ["auto_activate"] = AutoActivate,
            ["suites"] = Suites.Cast<object?>().ToList(),
            ["max_steps"] = MaxSteps,
            ["batch_size"] = BatchSize,
            ["grad_accum"] = GradAccum,
            ["max_length"] = MaxLength,
            ["lr"] = Lr,
            ["warmup_steps"] = WarmupSteps,
            ["device"] = Device,
            ["gpu_required_mb"] = GpuRequiredMb,
            ["val_permille"] = ValPermille,
            ["training_timezone"] = TrainingTimezone,
            ["quiet_hours_start"] = QuietHoursStart,
            ["quiet_hours_end"] = QuietHoursEnd,
            ["min_interval_s"] = MinIntervalS,
            ["max_cycles_per_day"] = MaxCyclesPerDay,
            ["max_consecutive_failures"] = MaxConsecutiveFailures,
            ["inference_exclusion"] = InferenceExclusion,
            ["max_synthetic_ratio"] = MaxSyntheticRatio,
            ["synthetic_source_prefixes"] =
                SyntheticSourcePrefixes.Cast<object?>().ToList(),
            ["min_pool_avg_quality"] = MinPoolAvgQuality,
            ["curriculum_enabled"] = CurriculumEnabled,
            ["curriculum_intent_map"] = CurriculumIntentMap,
            ["post_upgrade_maturity_recheck"] = PostUpgradeMaturityRecheck,
            ["maturity_recheck_device"] = MaturityRecheckDevice,
            ["degradation_probe_enabled"] = DegradationProbeEnabled,
            ["degradation_probe_suite"] = DegradationProbeSuite,
            ["degradation_min_examples"] = DegradationMinExamples,
            ["dedup_enabled"] = DedupEnabled,
            ["exclude_probe_values"] = ExcludeProbeValues,
            ["max_dataset_examples"] = MaxDatasetExamples,
            ["train_time_budget_s"] = TrainTimeBudgetS,
            ["train_vram_budget_mb"] = TrainVramBudgetMb,
            ["train_gpu_budget_s"] = TrainGpuBudgetS,
            ["gpu_busy_backoff_s"] = GpuBusyBackoffS,
            ["gpu_busy_backoff_cap_s"] = GpuBusyBackoffCapS,
            ["dpo_enabled"] = DpoEnabled,
            ["dpo_min_new_pairs"] = DpoMinNewPairs,
            ["dpo_beta"] = DpoBeta,
            ["capability_training_frozen"] = CapabilityTrainingFrozen,
            ["capability_training_mode"] = CapabilityTrainingMode,
            ["active_capability"] = ActiveCapability,
            ["architecture_pretrain_mode"] = ArchitecturePretrainMode,
            ["self_training_mode"] = SelfTrainingMode,
        };
        return d;
    }

    public static SelfLearningPolicy Load(string toolRoot)
    {
        string path = XcPaths.SettingsReadPath(
            toolRoot, XcPaths.SelfLearningPolicyRel);
        var policy = new SelfLearningPolicy();
        if (!File.Exists(path))
            return policy;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            policy.Enabled = Get(root, "enabled", policy.Enabled);
            policy.MinNewExamples = Get(root, "min_new_examples", policy.MinNewExamples);
            policy.AutoActivate = Get(root, "auto_activate", policy.AutoActivate);
            policy.Suites = GetArr(root, "suites", policy.Suites);
            policy.MaxSteps = Get(root, "max_steps", policy.MaxSteps);
            policy.BatchSize = Get(root, "batch_size", policy.BatchSize);
            policy.GradAccum = Get(root, "grad_accum", policy.GradAccum);
            policy.MaxLength = Get(root, "max_length", policy.MaxLength);
            policy.Lr = Get(root, "lr", policy.Lr);
            policy.WarmupSteps = Get(root, "warmup_steps", policy.WarmupSteps);
            policy.Device = Get(root, "device", policy.Device);
            policy.GpuRequiredMb = Get(root, "gpu_required_mb", policy.GpuRequiredMb);
            policy.ValPermille = Get(root, "val_permille", policy.ValPermille);
            policy.TrainingTimezone = Get(root, "training_timezone", policy.TrainingTimezone);
            policy.QuietHoursStart = Get(root, "quiet_hours_start", policy.QuietHoursStart);
            policy.QuietHoursEnd = Get(root, "quiet_hours_end", policy.QuietHoursEnd);
            policy.MinIntervalS = Get(root, "min_interval_s", policy.MinIntervalS);
            policy.MaxCyclesPerDay = Get(root, "max_cycles_per_day", policy.MaxCyclesPerDay);
            policy.MaxConsecutiveFailures = Get(root, "max_consecutive_failures",
                                                policy.MaxConsecutiveFailures);
            policy.InferenceExclusion = Get(root, "inference_exclusion",
                                            policy.InferenceExclusion);
            policy.MaxSyntheticRatio = Get(root, "max_synthetic_ratio",
                                           policy.MaxSyntheticRatio);
            policy.SyntheticSourcePrefixes = GetArr(root, "synthetic_source_prefixes",
                                                    policy.SyntheticSourcePrefixes);
            policy.MinPoolAvgQuality = Get(root, "min_pool_avg_quality",
                                           policy.MinPoolAvgQuality);
            policy.CurriculumEnabled = Get(root, "curriculum_enabled",
                                           policy.CurriculumEnabled);
            if (root.TryGetProperty("curriculum_intent_map", out var cim) &&
                cim.ValueKind == JsonValueKind.Object)
            {
                policy.CurriculumIntentMap = new Dictionary<string, List<string>>();
                foreach (var p in cim.EnumerateObject())
                    if (p.Value.ValueKind == JsonValueKind.Array)
                        policy.CurriculumIntentMap[p.Name] = p.Value.EnumerateArray()
                            .Select(e => e.GetString() ?? "").ToList();
            }
            policy.PostUpgradeMaturityRecheck = Get(root, "post_upgrade_maturity_recheck",
                                                    policy.PostUpgradeMaturityRecheck);
            policy.MaturityRecheckDevice = Get(root, "maturity_recheck_device",
                                               policy.MaturityRecheckDevice);
            policy.DegradationProbeEnabled = Get(root, "degradation_probe_enabled",
                                                 policy.DegradationProbeEnabled);
            policy.DegradationProbeSuite = Get(root, "degradation_probe_suite",
                                               policy.DegradationProbeSuite);
            policy.DegradationMinExamples = Get(root, "degradation_min_examples",
                                                policy.DegradationMinExamples);
            policy.DedupEnabled = Get(root, "dedup_enabled", policy.DedupEnabled);
            policy.ExcludeProbeValues = Get(root, "exclude_probe_values",
                                            policy.ExcludeProbeValues);
            policy.MaxDatasetExamples = Get(root, "max_dataset_examples",
                                            policy.MaxDatasetExamples);
            policy.TrainTimeBudgetS = Get(root, "train_time_budget_s",
                                          policy.TrainTimeBudgetS);
            policy.TrainVramBudgetMb = Get(root, "train_vram_budget_mb",
                                           policy.TrainVramBudgetMb);
            policy.TrainGpuBudgetS = Get(root, "train_gpu_budget_s",
                                         policy.TrainGpuBudgetS);
            policy.GpuBusyBackoffS = Get(root, "gpu_busy_backoff_s",
                                         policy.GpuBusyBackoffS);
            policy.GpuBusyBackoffCapS = Get(root, "gpu_busy_backoff_cap_s",
                                            policy.GpuBusyBackoffCapS);
            policy.DpoEnabled = Get(root, "dpo_enabled", policy.DpoEnabled);
            policy.DpoMinNewPairs = Get(root, "dpo_min_new_pairs", policy.DpoMinNewPairs);
            policy.DpoBeta = Get(root, "dpo_beta", policy.DpoBeta);
            policy.CapabilityTrainingFrozen = Get(root, "capability_training_frozen",
                                                  policy.CapabilityTrainingFrozen);
            policy.CapabilityTrainingMode = Get(root, "capability_training_mode",
                                                policy.CapabilityTrainingMode);
            policy.ActiveCapability = Get(root, "active_capability",
                                          policy.ActiveCapability);
            policy.ArchitecturePretrainMode = Get(root,
                "architecture_pretrain_mode", policy.ArchitecturePretrainMode);
            policy.SelfTrainingMode = Get(root, "self_training_mode",
                                          policy.SelfTrainingMode);
        }
        catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException)
        {
            return new SelfLearningPolicy();
        }
        return policy;
    }

    public void Save(string toolRoot)
    {
        string path = Path.Combine(toolRoot, XcPaths.SelfLearningPolicyRel);
        ModelLifecycle.AtomicWrite(path, CanonicalJson.PrettyDict(ToDict()) + "\n");
    }

    /// <summary>Fail-closed Taipei quiet-hours window check.</summary>
    public Dictionary<string, object?> TrainingWindowStatus(DateTimeOffset? now = null)
    {
        TimeZoneInfo zone;
        try
        {
            // Windows TZ name differs from IANA; map the configured Taipei zone.
            string tz = TrainingTimezone == "Asia/Taipei"
                ? "Taipei Standard Time" : TrainingTimezone;
            zone = TimeZoneInfo.FindSystemTimeZoneById(tz);
        }
        catch (Exception)
        {
            try { zone = TimeZoneInfo.FindSystemTimeZoneById(TrainingTimezone); }
            catch (Exception)
            {
                return new Dictionary<string, object?>
                {
                    ["allowed"] = false,
                    ["reason"] = "invalid-training-window",
                    ["timezone"] = TrainingTimezone,
                };
            }
        }
        if (!TryParseHm(QuietHoursStart, out var start) ||
            !TryParseHm(QuietHoursEnd, out var end))
        {
            return new Dictionary<string, object?>
            {
                ["allowed"] = false,
                ["reason"] = "invalid-training-window",
                ["timezone"] = TrainingTimezone,
            };
        }
        var local = TimeZoneInfo.ConvertTime(
            now ?? DateTimeOffset.UtcNow, zone).TimeOfDay;
        bool quiet = start > end
            ? local >= start || local < end
            : local >= start && local < end;
        return new Dictionary<string, object?>
        {
            ["allowed"] = !quiet,
            ["reason"] = quiet ? "quiet-hours" : "allowed",
            ["timezone"] = TrainingTimezone,
            ["local_time"] = local.ToString(@"hh\:mm"),
            ["quiet_hours"] = $"{QuietHoursStart}-{QuietHoursEnd}",
        };
    }

    private static bool TryParseHm(string value, out TimeSpan result)
    {
        result = default;
        var parts = (value ?? "").Split(':', 2);
        if (parts.Length != 2 ||
            !int.TryParse(parts[0], out int h) ||
            !int.TryParse(parts[1], out int m) ||
            h is < 0 or > 23 || m is < 0 or > 59)
            return false;
        result = new TimeSpan(h, m, 0);
        return true;
    }

    // ------------------------------------------------------ json helpers --

    private static bool Get(JsonElement root, string key, bool fallback)
        => root.TryGetProperty(key, out var v) &&
           v.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? v.GetBoolean() : fallback;

    private static int Get(JsonElement root, string key, int fallback)
        => root.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out int i)
            ? i : fallback;

    private static double Get(JsonElement root, string key, double fallback)
        => root.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.Number && v.TryGetDouble(out double d)
            ? d : fallback;

    private static string Get(JsonElement root, string key, string fallback)
        => root.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? fallback : fallback;

    private static string[] GetArr(JsonElement root, string key, string[] fallback)
        => root.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Array
            ? v.EnumerateArray().Select(e => e.GetString() ?? "").ToArray()
            : fallback;
}

internal static class SelfLearningState
{
    public const string Format = "star-self-learning-state/v1";

    public static Dictionary<string, object?> Load(string toolRoot)
    {
        string path = Path.Combine(toolRoot, XcPaths.SelfLearningStateRel);
        if (!File.Exists(path))
            return new Dictionary<string, object?>
            {
                ["format"] = Format,
                ["trained_example_total"] = 0,
            };
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
                return new Dictionary<string, object?> { ["format"] = Format };
            var state = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                state[p.Name] = ModelLifecycle.Decode(p.Value);
            return state;
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            return new Dictionary<string, object?>
            {
                ["format"] = Format,
                ["trained_example_total"] = 0,
            };
        }
    }

    public static void Save(string toolRoot, IReadOnlyDictionary<string, object?> state)
    {
        string path = Path.Combine(toolRoot, XcPaths.SelfLearningStateRel);
        var payload = new Dictionary<string, object?> { ["format"] = Format };
        foreach (var kv in state)
            payload[kv.Key] = kv.Value;
        payload["updated_at"] = XcPaths.IsoNow();
        ModelLifecycle.AtomicWrite(path, CanonicalJson.PrettyDict(payload) + "\n");
    }
}

internal sealed class RetentionPolicy
{
    public const string Format = "star-retention-policy/v1";

    public bool Enabled = true;
    public int KeepJobDirs = 3;
    public double KeepLogsDays = 30.0;
    public double KeepReportDays = 30.0;
    public int KeepMaturityReports = 10;
    public int KeepSelfLearningReports = 10;
    public int KeepSnapshots = 5;
    public int KeepWeightVersions = 1;

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = Format,
        ["enabled"] = Enabled,
        ["keep_job_dirs"] = KeepJobDirs,
        ["keep_logs_days"] = KeepLogsDays,
        ["keep_report_days"] = KeepReportDays,
        ["keep_maturity_reports"] = KeepMaturityReports,
        ["keep_self_learning_reports"] = KeepSelfLearningReports,
        ["keep_snapshots"] = KeepSnapshots,
        ["keep_weight_versions"] = KeepWeightVersions,
    };

    public static RetentionPolicy Load(string toolRoot)
    {
        string path = XcPaths.SettingsReadPath(
            toolRoot, XcPaths.RetentionPolicyRel);
        var policy = new RetentionPolicy();
        if (!File.Exists(path))
            return policy;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.TryGetProperty("enabled", out var e) &&
                e.ValueKind is JsonValueKind.True or JsonValueKind.False)
                policy.Enabled = e.GetBoolean();
            policy.KeepJobDirs = IntProp(root, "keep_job_dirs", policy.KeepJobDirs);
            policy.KeepLogsDays = DblProp(root, "keep_logs_days", policy.KeepLogsDays);
            policy.KeepReportDays = DblProp(root, "keep_report_days", policy.KeepReportDays);
            policy.KeepMaturityReports = IntProp(root, "keep_maturity_reports",
                                                 policy.KeepMaturityReports);
            policy.KeepSelfLearningReports = IntProp(root, "keep_self_learning_reports",
                                                     policy.KeepSelfLearningReports);
            policy.KeepSnapshots = IntProp(root, "keep_snapshots", policy.KeepSnapshots);
            policy.KeepWeightVersions = IntProp(root, "keep_weight_versions",
                                                policy.KeepWeightVersions);
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            return new RetentionPolicy();
        }
        return policy;
    }

    private static int IntProp(JsonElement root, string key, int fallback)
        => root.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out int i)
            ? i : fallback;

    private static double DblProp(JsonElement root, string key, double fallback)
        => root.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.Number && v.TryGetDouble(out double d)
            ? d : fallback;
}

/// <summary>xingcheng/runtime/settings/native-engine.json access
/// (checkpoint pin).</summary>
internal static class EngineSettings
{
    public static string? PinnedCheckpoint(string toolRoot)
    {
        string path = XcPaths.SettingsReadPath(
            toolRoot, XcPaths.EngineSettingsRel);
        if (!File.Exists(path))
            return null;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            return doc.RootElement.TryGetProperty("checkpoint", out var c)
                ? c.GetString() : null;
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            return null;
        }
    }

    /// <summary>Pin ``checkpoint`` to a tool-root-relative artifact path.
    /// The pinned serving artifact is xingcheng-owned data — an
    /// out-of-boundary target is refused (XINGCHENG_DATA_BOUNDARY).</summary>
    public static string PinCheckpoint(string toolRoot, string artifactPath)
    {
        DataBoundary.AssertInside(toolRoot, artifactPath);
        string settingsPath = Path.Combine(
            toolRoot, XcPaths.EngineSettingsRel);
        var settings = new Dictionary<string, object?>();
        string existing = XcPaths.SettingsReadPath(
            toolRoot, XcPaths.EngineSettingsRel);
        if (File.Exists(existing))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(existing));
                foreach (var p in doc.RootElement.EnumerateObject())
                    settings[p.Name] = ModelLifecycle.Decode(p.Value);
            }
            catch (Exception ex) when (ex is IOException or JsonException)
            {
                settings = new Dictionary<string, object?>();
            }
        }
        string relative = Path.GetRelativePath(toolRoot, artifactPath)
            .Replace(Path.DirectorySeparatorChar, '/');
        settings["checkpoint"] = relative;
        ModelLifecycle.AtomicWrite(
            settingsPath, CanonicalJson.PrettyDict(settings) + "\n");
        return relative;
    }
}
