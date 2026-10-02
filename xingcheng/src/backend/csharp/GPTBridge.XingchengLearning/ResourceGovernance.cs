// ResourceGovernance.cs — 星澄 ↔ 主系統資源治理契約層。
//
// 法源：A590/A593/A598/A610（main-system resource-governor 為全專案唯一
// 資源權威；FORBID:second-resource-governor）；星澄 native-only 架構規格
// §4–§18、§54、§80、§91。
//
// 本檔只放契約（資料＋驗證）；I/O 在 ResourceGovernorClient.cs。
// 星澄只持有 ResourceGovernorClient——不得有 GlobalResourceGovernor /
// MachineQuotaAuthority / HardwareOwner（A610）。
//
// 契約擁有權（規格 §79）：
//   star-resource-request/v1         Xingcheng → Main System
//   star-resource-grant/v1           Main System → Xingcheng
//   star-learning-intent/v1          Xingcheng（內部決策記錄）
//   star-acceleration-plan/v1        Xingcheng（grant-bounded 執行計畫）
//   star-resource-usage-receipt/v1   Xingcheng → Main System（xstore 層）

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

public static class ResourceContracts
{
    public const string RequestFormat = "star-resource-request/v1";
    public const string GrantFormat = "star-resource-grant/v1";
    public const string IntentFormat = "star-learning-intent/v1";
    public const string PlanFormat = "star-acceleration-plan/v1";
    public const string UsageReceiptFormat =
        "star-resource-usage-receipt/v1";
    public const string AuditFormat = "star-resource-audit/v1";
}

/// <summary>規格 §80 型別化錯誤碼。</summary>
public static class ResourceErrors
{
    public const string GovernorUnavailable = "RESOURCE_GOVERNOR_UNAVAILABLE";
    public const string RequestDenied = "RESOURCE_REQUEST_DENIED";
    public const string RequestDeferred = "RESOURCE_REQUEST_DEFERRED";
    /// <summary>AC §10: a production execution path with no bound
    /// resource_grant_id fails closed — the only escape is the
    /// explicit dev/test StaticLocalGrant (§11).</summary>
    public const string GrantRequired = "RESOURCE_GRANT_REQUIRED";
    public const string GrantExpired = "RESOURCE_GRANT_EXPIRED";
    public const string GrantRevoked = "RESOURCE_GRANT_REVOKED";
    public const string BudgetExceeded = "RESOURCE_BUDGET_EXCEEDED";
    public const string PlanOverGrant = "ACCELERATION_PLAN_OVER_GRANT";
    public const string InsufficientGranted =
        "INSUFFICIENT_GRANTED_RESOURCES";
    public const string PausedByPressure =
        "TRAINING_PAUSED_BY_RESOURCE_PRESSURE";
    /// <summary>Authority-convergence §6 hard rule: the main-system
    /// resource governor is the only whole-machine authority. Any
    /// xingcheng code path that tries to set a CPU/RAM/VRAM quota,
    /// change machine-wide priority or touch another tool's
    /// allocation throws this — xingcheng only requests, receives and
    /// reports grants.</summary>
    public const string AuthorityMainSystemOnly =
        "RESOURCE_AUTHORITY_MAIN_SYSTEM_ONLY";
}

/// <summary>規格 §7：主系統只能回這五種回覆。</summary>
public enum GrantResponseKind { Denied, Deferred, Partial, Granted, Revoked }

/// <summary>規格 §19 標準壓力狀態。</summary>
public enum ResourcePressure { Normal, PrePressure, ActivePressure, Emergency }

public static class GrantResponseParse
{
    public static GrantResponseKind? Of(string? text) => text switch
    {
        "DENIED" => GrantResponseKind.Denied,
        "DEFERRED" => GrantResponseKind.Deferred,
        "PARTIAL" => GrantResponseKind.Partial,
        "GRANTED" => GrantResponseKind.Granted,
        "REVOKED" => GrantResponseKind.Revoked,
        _ => null,
    };

    public static ResourcePressure PressureOf(string? text) => text switch
    {
        "PRE_PRESSURE" => ResourcePressure.PrePressure,
        "ACTIVE_PRESSURE" => ResourcePressure.ActivePressure,
        "EMERGENCY" => ResourcePressure.Emergency,
        _ => ResourcePressure.Normal,
    };
}

/// <summary>規格 §68 星澄內部 VRAM 分類（grant 只授 TRAINING_GRANTED
/// ＋允許時的 TRAINING_OPTIONAL；INFERENCE_REQUIRED 永不被訓練佔用）。</summary>
public enum VramClass
{
    InferenceRequired, RuntimeState, TrainingGranted,
    TrainingOptional, SafetyReserve,
}

/// <summary>規格 §6：星澄提交給主系統的資源申請。不能直接說「我要
/// 16 threads」——必須透過本契約（§5）。</summary>
public sealed class ResourceRequest
{
    public string RequestId = "";
    public string WorkloadId = "";
    public string CandidateId = "";
    public string Capability = "";
    /// <summary>對應 governor 的 8 類 workload（training/batch/
    /// verification/maintenance/…）或 "benchmark"（§62 自動調優）。</summary>
    public string WorkloadClass = "training";
    public long Priority;
    public int MinimumCpuThreads = 1;
    public int PreferredCpuThreads;
    public long MinimumRamBytes;
    public long PreferredRamBytes;
    public bool GpuOptional;
    public bool GpuRequired;
    public long MinimumVramBytes;
    public long PreferredVramBytes;
    public long IoReadBudget;
    public long IoWriteBudget;
    public double ExpectedDurationS;
    public bool Checkpointable = true;
    public bool Preemptible = true;

    /// <summary>§6 必要欄位驗證；空清單 = 合法可提交。</summary>
    public List<string> Validate()
    {
        var problems = new List<string>();
        if (RequestId.Length == 0) problems.Add("request_id");
        if (WorkloadId.Length == 0) problems.Add("workload_id");
        if (WorkloadClass.Length == 0) problems.Add("workload_class");
        if (MinimumCpuThreads < 0 || PreferredCpuThreads < 0)
            problems.Add("cpu_threads-negative");
        if (MinimumCpuThreads > PreferredCpuThreads &&
            PreferredCpuThreads > 0)
            problems.Add("minimum-cpu-over-preferred");
        if (MinimumRamBytes > PreferredRamBytes && PreferredRamBytes > 0)
            problems.Add("minimum-ram-over-preferred");
        if (MinimumVramBytes > PreferredVramBytes && PreferredVramBytes > 0)
            problems.Add("minimum-vram-over-preferred");
        if (GpuRequired && GpuOptional)
            problems.Add("gpu-flags-conflict");
        return problems;
    }

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = ResourceContracts.RequestFormat,
        ["request_id"] = RequestId,
        ["workload_id"] = WorkloadId,
        ["candidate_id"] = CandidateId,
        ["capability"] = Capability,
        ["workload_class"] = WorkloadClass,
        ["priority"] = Priority,
        ["minimum_cpu_threads"] = MinimumCpuThreads,
        ["preferred_cpu_threads"] = PreferredCpuThreads,
        ["minimum_ram_bytes"] = MinimumRamBytes,
        ["preferred_ram_bytes"] = PreferredRamBytes,
        ["gpu_optional"] = GpuOptional,
        ["gpu_required"] = GpuRequired,
        ["minimum_vram_bytes"] = MinimumVramBytes,
        ["preferred_vram_bytes"] = PreferredVramBytes,
        ["io_read_budget"] = IoReadBudget,
        ["io_write_budget"] = IoWriteBudget,
        ["expected_duration_s"] = ExpectedDurationS,
        ["checkpointable"] = Checkpointable,
        ["preemptible"] = Preemptible,
    };
}

/// <summary>規格 §8：主系統核發的資源授權。AccelerationPlane 只能讀它，
/// 不能擴張它（§9/§11）。</summary>
public sealed class ResourceGrant
{
    public string GrantId = "";
    public string RequestId = "";
    public string WorkloadClass = "";
    public int CpuThreadsMax;
    public long RamBytesMax;
    public long PinnedRamBytesMax;
    public bool GpuAllowed;
    /// <summary>絕對上限；-1 表示主系統以 vram_budget_percent 授權，
    /// 客戶端必須以 percent × probe total 解析後再取 min（§41）。</summary>
    public long VramBytesMax;
    public int VramBudgetPercent;
    public double GpuComputeShare;
    public long IoReadLimit;
    public long IoWriteLimit;
    public int BackgroundThreadsMax;
    public double ValidUntilS;
    public string PressureState = "NORMAL";

    public bool IsExpired(double nowUnixS) =>
        ValidUntilS > 0 && nowUnixS > ValidUntilS;

    /// <summary>規格 §41：effective_vram_budget =
    /// min(driver_available, grant.vram_bytes_max)。grant 以 percent
    /// 授權時先換算絕對值。driver free memory 只是 execution
    /// evidence，不是 permission（§40）。</summary>
    public long EffectiveVramBytes(long driverFreeBytes, long driverTotalBytes)
    {
        long cap = VramBytesMax;
        if (cap < 0 && VramBudgetPercent > 0 && driverTotalBytes > 0)
            cap = driverTotalBytes * VramBudgetPercent / 100;
        if (cap < 0) cap = 0;
        return Math.Max(0, Math.Min(driverFreeBytes, cap));
    }

    /// <summary>規格 §11/§103：計畫任一值超過 grant →
    /// ACCELERATION_PLAN_OVER_GRANT，不得執行。回傳違規欄位清單。</summary>
    public List<string> OverGrantViolations(
        int planCpuThreads, long planVramBytes, int planStreams,
        int planBackgroundThreads, long planIoRead, long planIoWrite,
        long planRamBytes)
    {
        var violations = new List<string>();
        if (planCpuThreads > CpuThreadsMax)
            violations.Add($"cpu_threads:{planCpuThreads}>{CpuThreadsMax}");
        if (planRamBytes > 0 && RamBytesMax > 0 && planRamBytes > RamBytesMax)
            violations.Add($"ram:{planRamBytes}>{RamBytesMax}");
        if (!GpuAllowed && planVramBytes > 0)
            violations.Add("vram-requested-without-gpu-grant");
        long vramCap = VramBytesMax >= 0 ? VramBytesMax : long.MaxValue;
        if (planVramBytes > vramCap)
            violations.Add($"vram:{planVramBytes}>{vramCap}");
        if (GpuComputeShare > 0 && planStreams > 0)
        {
            int streamCap = (int)Math.Ceiling(GpuComputeShare * 8.0);
            if (planStreams > streamCap)
                violations.Add($"streams:{planStreams}>{streamCap}");
        }
        if (BackgroundThreadsMax > 0 &&
            planBackgroundThreads > BackgroundThreadsMax)
            violations.Add(
                $"background_threads:{planBackgroundThreads}>" +
                BackgroundThreadsMax);
        if (IoReadLimit > 0 && planIoRead > IoReadLimit)
            violations.Add($"io_read:{planIoRead}>{IoReadLimit}");
        if (IoWriteLimit > 0 && planIoWrite > IoWriteLimit)
            violations.Add($"io_write:{planIoWrite}>{IoWriteLimit}");
        return violations;
    }

    public static ResourceGrant? FromDict(
        IReadOnlyDictionary<string, object?> envelope)
    {
        if (!envelope.TryGetValue("grant", out var g) ||
            g is not IReadOnlyDictionary<string, object?> grant)
            return null;
        return new ResourceGrant
        {
            RequestId = Str(envelope, "request_id"),
            GrantId = Str(grant, "grant_id"),
            WorkloadClass = Str(grant, "workload_class"),
            CpuThreadsMax = Int(grant, "cpu_threads_max"),
            RamBytesMax = Long(grant, "ram_bytes_max"),
            PinnedRamBytesMax = Long(grant, "pinned_ram_bytes_max"),
            GpuAllowed = grant.TryGetValue("gpu_allowed", out var ga) &&
                         ga is bool b && b,
            VramBytesMax = Long(grant, "vram_bytes_max"),
            VramBudgetPercent = Int(grant, "vram_budget_percent"),
            GpuComputeShare = grant.TryGetValue("gpu_compute_share",
                                  out var gs) && gs is double d ? d
                              : grant.TryGetValue("gpu_compute_share",
                                    out var gs2) && gs2 is long l2 ? l2 : 0,
            IoReadLimit = Long(grant, "io_read_limit"),
            IoWriteLimit = Long(grant, "io_write_limit"),
            BackgroundThreadsMax = Int(grant, "background_threads_max"),
            ValidUntilS = Long(grant, "valid_until_s"),
            PressureState = Str(grant, "pressure_state"),
        };
    }

    public Dictionary<string, object?> ToDict() => new()
    {
        ["grant_id"] = GrantId,
        ["request_id"] = RequestId,
        ["workload_class"] = WorkloadClass,
        ["cpu_threads_max"] = CpuThreadsMax,
        ["ram_bytes_max"] = RamBytesMax,
        ["pinned_ram_bytes_max"] = PinnedRamBytesMax,
        ["gpu_allowed"] = GpuAllowed,
        ["vram_bytes_max"] = VramBytesMax,
        ["vram_budget_percent"] = VramBudgetPercent,
        ["gpu_compute_share"] = GpuComputeShare,
        ["io_read_limit"] = IoReadLimit,
        ["io_write_limit"] = IoWriteLimit,
        ["background_threads_max"] = BackgroundThreadsMax,
        ["valid_until_s"] = ValidUntilS,
        ["pressure_state"] = PressureState,
    };

    private static string Str(
        IReadOnlyDictionary<string, object?> d, string key) =>
        d.TryGetValue(key, out var v) && v is string s ? s : "";
    private static int Int(
        IReadOnlyDictionary<string, object?> d, string key) =>
        d.TryGetValue(key, out var v) && v is int i ? i
        : v is long l ? (int)l : v is double db ? (int)db : 0;
    private static long Long(
        IReadOnlyDictionary<string, object?> d, string key) =>
        d.TryGetValue(key, out var v) && v is long l ? l
        : v is int i ? i : v is double db ? (long)db : 0;
}

/// <summary>規格 §13/§14：自主學習的學習意圖。自主學習只決定「學什麼」
/// （§12），建立 intent 前必須先估成本與預期增益（§14）。</summary>
public sealed class LearningIntent
{
    public string Capability = "";
    public string Reason = "";
    public int FailureCount;
    public double ExpectedGain;
    public double ExpectedComputeCost;
    public string SourceCandidate = "";
    public string DatasetSnapshot = "";
    public long MaximumSteps;
    public string Deadline = "";
    public long Priority;

    /// <summary>§14：ExpectedGainPerComputeCost；成本為 0 時回傳 0
    /// （無成本估算的 intent 不得進入 admission）。</summary>
    public double ExpectedGainPerComputeCost =>
        ExpectedComputeCost > 0 ? ExpectedGain / ExpectedComputeCost : 0;

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = ResourceContracts.IntentFormat,
        ["capability"] = Capability,
        ["reason"] = Reason,
        ["failure_count"] = FailureCount,
        ["expected_gain"] = ExpectedGain,
        ["expected_compute_cost"] = ExpectedComputeCost,
        ["expected_gain_per_compute_cost"] = ExpectedGainPerComputeCost,
        ["source_candidate"] = SourceCandidate,
        ["dataset_snapshot"] = DatasetSnapshot,
        ["maximum_steps"] = MaximumSteps,
        ["deadline"] = Deadline,
        ["priority"] = Priority,
    };
}

/// <summary>規格 §54/§55：訓練結束後星澄回報主系統的資源使用收據。
/// 由 xstore/檔案層保存（§91 禁 PostgreSQL 存原生資源收據）。</summary>
public sealed class ResourceUsageReceipt
{
    public string GrantId = "";
    public string WorkloadId = "";
    public double ActualCpuSeconds;
    public double ActualGpuSeconds;
    public long PeakRamBytes;
    public long PeakVramBytes;
    public long IoReadBytes;
    public long IoWriteBytes;
    public double WallTimeS;
    public double CapabilityGain;
    public string Result = "";

    public double CapabilityGainPerCpuSecond =>
        ActualCpuSeconds > 0 ? CapabilityGain / ActualCpuSeconds : 0;
    public double CapabilityGainPerGpuSecond =>
        ActualGpuSeconds > 0 ? CapabilityGain / ActualGpuSeconds : 0;

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = ResourceContracts.UsageReceiptFormat,
        ["grant_id"] = GrantId,
        ["workload_id"] = WorkloadId,
        ["actual_cpu_seconds"] = ActualCpuSeconds,
        ["actual_gpu_seconds"] = ActualGpuSeconds,
        ["peak_ram_bytes"] = PeakRamBytes,
        ["peak_vram_bytes"] = PeakVramBytes,
        ["io_read_bytes"] = IoReadBytes,
        ["io_write_bytes"] = IoWriteBytes,
        ["wall_time_s"] = WallTimeS,
        ["capability_gain"] = CapabilityGain,
        ["capability_gain_per_cpu_second"] = CapabilityGainPerCpuSecond,
        ["capability_gain_per_gpu_second"] = CapabilityGainPerGpuSecond,
        ["result"] = Result,
        ["at"] = DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"),
    };
}
