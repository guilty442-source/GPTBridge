// ResourceGovernorClient.cs — 星澄對主系統唯一資源權威的客戶端。
//
// 法源：A610 FORBID:second-resource-governor（星澄不得有第二個全域
// 資源權威——本類是唯一的資源出入口）；規格 §4（只負責 Request/
// Renew/Report/Release/PauseResponse/ResumeResponse/PressureResponse，
// 不得改主系統 policy、quota、他人 allocation）、§75–§77（governor
// 不可用 → autonomous training FAIL-CLOSED；StaticLocalGrant 僅
// dev/test）、§34–§35（XingchengLocalResourceAllocator 只分配已獲
/// 授權的 quota，不是全機 governor）。
//
// 通道：檔案契約（與 governor state 同目錄 main-system/runtime/state/）。
//   resource-requests/<request_id>.request.json  本端寫
//   resource-requests/<request_id>.renew.json    本端寫
//   resource-requests/<request_id>.release.json  本端寫
//   resource-grants/<request_id>.json            governor 回覆
//   resource-reports/<grant_id>.jsonl            週期遙測（§50）
//   resource-receipts/<workload>.jsonl           使用收據（§54/§91）

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

/// <summary>一次 Request 的完整回覆。Unavailable 與 DENIED 不同：
/// governor 不存在/心跳過期 → Unavailable（→ RESOURCE_GOVERNOR_
/// UNAVAILABLE）；governor 明確拒絕 → Response=Denied。</summary>
public sealed class GovernorReply
{
    public bool Unavailable;
    public GrantResponseKind? Response;
    public ResourceGrant? Grant;
    public string Reason = "";
    public Dictionary<string, object?> Raw = new();

    public bool Admitted =>
        Response is GrantResponseKind.Granted or GrantResponseKind.Partial &&
        Grant != null;
}

public sealed class ResourceGovernorClient
{
    private const string DevGrantEnv = "XC_DEV_STATIC_GRANT";
    private readonly string _toolRoot;

    public ResourceGovernorClient(string toolRoot) { _toolRoot = toolRoot; }

    /// <summary>定位 main-system/runtime/state/（祖先走訪，同
    /// JobExecutor.GovernorStatePath 的發現語義）。</summary>
    public string? StateDir()
    {
        try
        {
            string? dir = Path.GetFullPath(_toolRoot);
            for (int i = 0; i < 6 && dir != null; i++)
            {
                string candidate = Path.Combine(dir, "main-system",
                    "runtime", "state", "resource-governor.json");
                if (File.Exists(candidate))
                    return Path.GetDirectoryName(candidate);
                dir = Path.GetDirectoryName(dir);
            }
            return null;
        }
        catch { return null; }
    }

    /// <summary>governor 可用性：狀態檔存在且新鮮（mtime 在
    /// max(3×interval, 90s) 內）。不可用 → autonomous training
    /// FAIL-CLOSED（§75）。</summary>
    public bool GovernorAvailable()
    {
        string? dir = StateDir();
        if (dir == null) return false;
        string statePath = Path.Combine(dir, "resource-governor.json");
        try
        {
            double intervalS = 20.0;
            try
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(statePath));
                if (doc.RootElement.TryGetProperty("interval",
                        out var iv) && iv.TryGetDouble(out double v))
                    intervalS = Math.Clamp(v, 1.0, 600.0);
            }
            catch (JsonException) { return false; }
            var age = DateTime.UtcNow -
                File.GetLastWriteTimeUtc(statePath);
            return age.TotalSeconds <= Math.Max(3 * intervalS, 90.0);
        }
        catch (Exception ex) when (ex is IOException or
            UnauthorizedAccessException)
        {
            return false;
        }
    }

    /// <summary>提交 ResourceRequest 並等待 governor 裁決。回覆走
    /// resource-grants/&lt;request_id&gt;.json；逾時仍無回覆 → Deferred
    /// （等下一輪），governor 消失 → Unavailable。</summary>
    public GovernorReply Request(ResourceRequest request,
                                 int timeoutS = 60)
    {
        var reply = new GovernorReply();
        var problems = request.Validate();
        if (problems.Count > 0)
        {
            reply.Response = GrantResponseKind.Denied;
            reply.Reason = "malformed-request:" +
                string.Join(",", problems);
            return reply;
        }
        string? dir = StateDir();
        if (dir == null || !GovernorAvailable())
        {
            reply.Unavailable = true;
            reply.Reason = "resource-governor-unavailable";
            return reply;
        }
        string reqDir = Path.Combine(dir, "resource-requests");
        string grantPath = Path.Combine(dir, "resource-grants",
            request.RequestId + ".json");
        AssertClientWriteScope(dir, Path.Combine(reqDir,
            request.RequestId + ".request.json"));
        AtomicWrite(Path.Combine(reqDir,
                    request.RequestId + ".request.json"),
                    JsonSerializer.Serialize(request.ToDict()));
        var deadline = DateTime.UtcNow.AddSeconds(timeoutS);
        while (DateTime.UtcNow < deadline)
        {
            var parsed = TryReadGrant(grantPath, request.RequestId);
            if (parsed != null) return parsed;
            if (!GovernorAvailable())
            {
                reply.Unavailable = true;
                reply.Reason = "governor-lost-while-waiting";
                return reply;
            }
            Thread.Sleep(500);
        }
        reply.Response = GrantResponseKind.Deferred;
        reply.Reason = "grant-reply-timeout";
        return reply;
    }

    /// <summary>讀取既有 grant 的現況（resize/revoke 偵測：§56–§58
    /// governor 每週期重寫 grant 檔）。不存在或逾期的檔案 → null。</summary>
    public ResourceGrant? CurrentGrant(string requestId)
    {
        string? dir = StateDir();
        if (dir == null) return null;
        var reply = TryReadGrant(Path.Combine(dir, "resource-grants",
            requestId + ".json"), requestId);
        if (reply?.Grant == null) return null;
        if (reply.Response == GrantResponseKind.Revoked) return null;
        return reply.Grant;
    }

    /// <summary>規格 §59：到期前續約。回傳是否成功投遞（不等待結果；
    /// 下次 CurrentGrant 反映新 valid_until）。</summary>
    public bool Renew(ResourceGrant grant)
    {
        string? dir = StateDir();
        if (dir == null) return false;
        string path = Path.Combine(dir, "resource-requests",
            grant.RequestId + ".renew.json");
        AssertClientWriteScope(dir, path);
        return AtomicWrite(path,
                JsonSerializer.Serialize(new Dictionary<string, object?>
                {
                    ["grant_id"] = grant.GrantId,
                }));
    }

    /// <summary>規格 §58：釋放 grant。本端清 request 檔並投遞 release
    /// 標記；governor 下週期清 grant 檔＋稽核。</summary>
    public void Release(ResourceGrant grant)
    {
        string? dir = StateDir();
        if (dir == null) return;
        string reqDir = Path.Combine(dir, "resource-requests");
        string relPath = Path.Combine(reqDir,
            grant.RequestId + ".release.json");
        AssertClientWriteScope(dir, relPath);
        AtomicWrite(relPath, "{}");
        try
        {
            File.Delete(Path.Combine(reqDir,
                grant.RequestId + ".request.json"));
            File.Delete(Path.Combine(reqDir,
                grant.RequestId + ".renew.json"));
        }
        catch { /* best-effort */ }
        AppendAudit(dir, "release", grant.GrantId, grant.RequestId);
    }

    /// <summary>規格 §50：週期性回報實際用量（bounded-interval 彙總，
    /// 非每 kernel——§51 hot path 不跨 IPC）。</summary>
    public void ReportTelemetry(ResourceGrant grant,
                                IReadOnlyDictionary<string, object?> usage)
    {
        string? dir = StateDir();
        if (dir == null) return;
        try
        {
            string repDir = Path.Combine(dir, "resource-reports");
            AssertClientWriteScope(dir,
                Path.Combine(repDir, grant.GrantId + ".jsonl"));
            Directory.CreateDirectory(repDir);
            var line = new Dictionary<string, object?>(usage)
            {
                ["grant_id"] = grant.GrantId,
                ["at"] = DateTime.UtcNow.ToString(
                    "yyyy-MM-dd'T'HH:mm:ss'Z'"),
            };
            File.AppendAllText(
                Path.Combine(repDir, grant.GrantId + ".jsonl"),
                JsonSerializer.Serialize(line) + "\n");
        }
        catch { /* telemetry must never break the lane */ }
    }

    /// <summary>規格 §54/§91：使用收據落於原生檔案層（xstore 資料面
    /// 的 JSONL 約定；PostgreSQL 只用於關係資料，不收原生資源收據）。</summary>
    public void AppendUsageReceipt(ResourceUsageReceipt receipt)
    {
        string? dir = StateDir();
        if (dir == null) return;
        try
        {
            string repDir = Path.Combine(dir, "resource-receipts");
            AssertClientWriteScope(dir, Path.Combine(repDir,
                (receipt.WorkloadId.Length > 0
                    ? receipt.WorkloadId : "unknown") + ".jsonl"));
            Directory.CreateDirectory(repDir);
            File.AppendAllText(
                Path.Combine(repDir,
                    (receipt.WorkloadId.Length > 0
                        ? receipt.WorkloadId : "unknown") + ".jsonl"),
                JsonSerializer.Serialize(receipt.ToDict()) + "\n");
        }
        catch { /* receipt write is best-effort audit */ }
    }

    /// <summary>規格 §21–§23：壓力狀態 → 星澄動作集（純對映；執行在
    /// lane/allocator 層）。</summary>
    public static Dictionary<string, object?> PressureActionPlan(
        ResourcePressure pressure) => pressure switch
    {
        ResourcePressure.PrePressure => new()
        {
            ["no_new_lanes"] = true,
            ["background_concurrency_scale"] = 0.5,
            ["verifier_threads_scale"] = 0.5,
            ["packing_concurrency_scale"] = 0.5,
            ["reduce_prefetch"] = true,
            ["shrink_cold_caches"] = true,
        },
        ResourcePressure.ActivePressure => new()
        {
            ["no_new_lanes"] = true,
            ["shrink_microbatch"] = true,
            ["reduce_streams"] = true,
            ["release_optional_workspace"] = true,
            ["release_cold_training_buffers"] = true,
            ["finish_current_safe_step"] = true,
            ["prepare_pause"] = true,
        },
        ResourcePressure.Emergency => new()
        {
            ["stop_new_compute"] = true,
            ["checkpoint_at_safe_point"] = true,
            ["flush_receipt"] = true,
            ["release_training_vram"] = true,
            ["release_training_ram"] = true,
            ["enter_paused_by_resource_governor"] = true,
        },
        _ => new() { ["normal"] = true },
    };

    /// <summary>規格 §76–§77：StaticLocalGrant 僅 dev/test；必須顯式
    /// 設 XC_DEV_STATIC_GRANT=1，且永遠不得作 production 自動 fallback。</summary>
    public static ResourceGrant? StaticLocalGrant()
    {
        if (Environment.GetEnvironmentVariable(DevGrantEnv) != "1")
            return null;
        return new ResourceGrant
        {
            GrantId = "static-local-dev",
            WorkloadClass = "training",
            CpuThreadsMax = 4,
            RamBytesMax = 8L << 30,
            PinnedRamBytesMax = 0,
            GpuAllowed = false,
            VramBytesMax = 0,
            VramBudgetPercent = 0,
            GpuComputeShare = 0,
            IoReadLimit = 0,
            IoWriteLimit = 0,
            BackgroundThreadsMax = 1,
            ValidUntilS =
                DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600,
            PressureState = "NORMAL",
        };
    }

    // ----------------------------------------------------------- I/O --

    private GovernorReply? TryReadGrant(string grantPath,
                                        string requestId)
    {
        if (!File.Exists(grantPath)) return null;
        try
        {
            using var doc = JsonDocument.Parse(
                File.ReadAllText(grantPath));
            var root = doc.RootElement;
            var map = new Dictionary<string, object?>();
            foreach (var p in root.EnumerateObject())
                map[p.Name] = Decode(p.Value);
            var reply = new GovernorReply { Raw = map };
            reply.Response = GrantResponseParse.Of(
                map.TryGetValue("response", out var r) && r is string s
                    ? s : null);
            reply.Reason = map.TryGetValue("reason", out var rr) &&
                           rr is string rs ? rs : "";
            if (reply.Response == null)
                return null; /* 半寫入檔案：下輪再讀 */
            if (reply.Admitted)
                reply.Grant = ResourceGrant.FromDict(map);
            return reply;
        }
        catch (Exception ex) when (ex is IOException or JsonException or
            UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static object? Decode(JsonElement el) => el.ValueKind switch
    {
        JsonValueKind.Object => el.EnumerateObject().ToDictionary(
            p => p.Name, p => Decode(p.Value)),
        JsonValueKind.Array => el.EnumerateArray()
            .Select(Decode).ToList(),
        JsonValueKind.String => el.GetString(),
        JsonValueKind.Number => el.TryGetInt64(out long l)
            ? l : el.GetDouble(),
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        _ => null,
    };

    /// <summary>Authority-convergence §6 hard rule: the client may only
    /// write into the xingcheng contract dirs (resource-requests /
    /// resource-reports / resource-receipts) — governor state,
    /// resource-grants and every other state file are read-only for
    /// xingcheng. A write outside those dirs is a second-governor
    /// attempt and fails RESOURCE_AUTHORITY_MAIN_SYSTEM_ONLY.</summary>
    private static void AssertClientWriteScope(string stateDir,
                                             string path)
    {
        string dir = Path.GetFileName(
            Path.GetDirectoryName(Path.GetFullPath(path)) ?? "");
        bool allowed = dir is "resource-requests" or "resource-reports"
            or "resource-receipts"
            || (dir == "resource-grants" && Path.GetFileName(path)
                == "grant-client-audit.jsonl");
        if (!allowed)
            throw new ExecutorError(
                ResourceErrors.AuthorityMainSystemOnly,
                $"xingcheng may not write '{path}' — only " +
                "resource-requests/reports/receipts are " +
                "client-writable");
    }

    private void AppendAudit(string dir, string action, string grantId,
                           string requestId)
    {
        try
        {
            string auditDir = Path.Combine(dir, "resource-grants");
            Directory.CreateDirectory(auditDir);
            File.AppendAllText(Path.Combine(auditDir,
                "grant-client-audit.jsonl"),
                JsonSerializer.Serialize(new Dictionary<string, object?>
                {
                    ["format"] = ResourceContracts.AuditFormat,
                    ["side"] = "xingcheng-client",
                    ["action"] = action,
                    ["grant_id"] = grantId,
                    ["request_id"] = requestId,
                    ["at"] = DateTime.UtcNow.ToString(
                        "yyyy-MM-dd'T'HH:mm:ss'Z'"),
                }) + "\n");
        }
        catch { }
    }

    private static bool AtomicWrite(string path, string content)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, content);
            File.Move(tmp, path, true);
            return true;
        }
        catch { return false; }
    }
}

/// <summary>規格 §34–§35：只分配已獲授權的 Xingcheng quota。
/// 不是全機 governor；輸入 ResourceGrant、輸出 per-lane allocation；
/// 單 GPU lease：一次至多一條 lane 持有 GPU（§31/驗收場景 G）。</summary>
public sealed class XingchengLocalResourceAllocator
{
    public sealed class LaneRequest
    {
        public string LaneId = "";
        public string Kind = "";       // training|data|eval|background
        public int CpuThreads;
        public long RamBytes;
        public long VramBytes;
        public bool WantsGpu;
        public int Priority;
    }

    public sealed class LaneAllocation
    {
        public string LaneId = "";
        public int CpuThreads;
        public long RamBytes;
        public bool GpuAllowed;
        public long VramBytes;
        public string Note = "";
        public Dictionary<string, object?> ToDict() => new()
        {
            ["lane_id"] = LaneId,
            ["cpu_threads"] = CpuThreads,
            ["ram_bytes"] = RamBytes,
            ["gpu_allowed"] = GpuAllowed,
            ["vram_bytes"] = VramBytes,
            ["note"] = Note,
        };
    }

    /// <summary>依 priority 降序分配；CPU/RAM 總和 &lt;= grant；
    /// GPU 只給第一個 WantsGpu 的 lane（其餘轉 CPU 準備，§32/§103）。</summary>
    public static List<LaneAllocation> Allocate(
        ResourceGrant grant, IReadOnlyList<LaneRequest> lanes)
    {
        var ordered = lanes
            .OrderByDescending(l => l.Priority)
            .ThenBy(l => l.LaneId, StringComparer.Ordinal)
            .ToList();
        int cpuLeft = grant.CpuThreadsMax;
        long ramLeft = grant.RamBytesMax;
        long vramLeft = grant.VramBytesMax < 0
            ? long.MaxValue : grant.VramBytesMax;
        bool gpuTaken = false;
        var allocs = new List<LaneAllocation>();
        foreach (var lane in ordered)
        {
            var alloc = new LaneAllocation { LaneId = lane.LaneId };
            alloc.CpuThreads = Math.Clamp(lane.CpuThreads, 0, cpuLeft);
            cpuLeft -= alloc.CpuThreads;
            alloc.RamBytes = lane.RamBytes <= 0 ? 0
                : Math.Min(lane.RamBytes, Math.Max(0, ramLeft));
            ramLeft -= alloc.RamBytes;
            if (lane.WantsGpu && grant.GpuAllowed && !gpuTaken &&
                lane.VramBytes <= vramLeft)
            {
                alloc.GpuAllowed = true;
                alloc.VramBytes = lane.VramBytes;
                vramLeft -= lane.VramBytes;
                gpuTaken = true;
            }
            else
            {
                alloc.VramBytes = 0;
                alloc.Note = lane.WantsGpu
                    ? "gpu-lease-held-by-higher-priority-lane"
                    : "";
            }
            allocs.Add(alloc);
        }
        return allocs;
    }
}
