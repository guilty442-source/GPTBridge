using System.Text.Json;
using StarDomain;

namespace StarBusinessLogic.Application;

// 對應 Python Tool Router：根據 Plan 決定需要的自有域工具，執行器實際執行
// C# 負責決策與協調，執行權仍受治理邊界約束
public sealed record ToolCallResult(
    string ToolId,
    bool Success,
    string Content,
    string ContentHash,
    double LatencyMs,
    string? Error = null
);

public interface IToolRouter
{
    Task<IReadOnlyList<ToolCallResult>> RouteAsync(ExecutionPlan plan, CancellationToken cancellationToken = default);
}

public interface IToolExecutor
{
    string ToolId { get; }
    Task<ToolCallResult> ExecuteAsync(ExecutionPlan plan, CancellationToken cancellationToken = default);
}

// 預設路由器：依 Plan.RequiredTools 委派給已註冊的 Executor
public sealed class DefaultToolRouter : IToolRouter
{
    private readonly IReadOnlyDictionary<string, IToolExecutor> _executors;
    // 唯一工具集合由 F# PlanRules.supportedTools 擁有；C# 不得另行手寫
    // （SAME_RUNTIME_TYPED_CALL：同 .NET runtime typed 直連）。
    private static readonly HashSet<string> Allowed = new(PlanRules.supportedTools,
        StringComparer.OrdinalIgnoreCase);

    // bounded-concurrency/v1: shared admission gate — a plan's tool
    // fan-out acquires slots instead of spawning one unbounded task per
    // tool call.  Effective width = governor "network" class quota
    // (concurrency-budget/v1) clamped into the declared envelope;
    // unreadable state fails open to the envelope max.
    private const int MinParallel = 2;
    private const int MaxParallel = 8;
    private static readonly SemaphoreSlim AdmissionGate =
        new(ResolveParallelWidth(), MaxParallel);

    private static int ResolveParallelWidth()
    {
        try
        {
            var path = Environment.GetEnvironmentVariable(
                "GPTBRIDGE_GOVERNOR_STATE");
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                return MaxParallel;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.TryGetProperty("disabled", out var d)
                && d.ValueKind == JsonValueKind.True)
                return MaxParallel;
            if (!root.TryGetProperty("concurrency_budget", out var budget)
                || budget.GetProperty("contract").GetString()
                    != "concurrency-budget/v1")
                return MaxParallel;
            var quota = budget.GetProperty("classes")
                .GetProperty("network").GetProperty("quota").GetInt32();
            return Math.Clamp(quota > 0 ? quota : MinParallel,
                              MinParallel, MaxParallel);
        }
        catch
        {
            return MaxParallel;
        }
    }

    public DefaultToolRouter(IEnumerable<IToolExecutor> executors)
    {
        _executors = executors?.ToDictionary(e => e.ToolId, StringComparer.OrdinalIgnoreCase)
                     ?? new Dictionary<string, IToolExecutor>(StringComparer.OrdinalIgnoreCase);
    }

    public async Task<IReadOnlyList<ToolCallResult>> RouteAsync(ExecutionPlan plan, CancellationToken cancellationToken = default)
    {
        // 正確性：空/無效輸入不拋例外
        if (plan?.RequiredTools == null || plan.RequiredTools.Count == 0) return Array.Empty<ToolCallResult>();

        // 速度：預分配，避免多次字典查詢
        var toRun = new List<IToolExecutor>(plan.RequiredTools.Count);
        foreach (var tool in plan.RequiredTools)
        {
            if (string.IsNullOrWhiteSpace(tool) || !Allowed.Contains(tool)) continue;
            if (_executors.TryGetValue(tool, out var exec))
                toRun.Add(exec);
        }
        if (toRun.Count == 0) return Array.Empty<ToolCallResult>();

        // 正確性：個別工具失敗不影響其他工具（fail-closed 單點，整體仍可部分成功）
        // 速度：並行執行，單工具超時 5s 避免拖慢整體（對應 Python 的 transport priority/deadline）
        var tasks = toRun.Select(async exec =>
        {
            // admission: wait on the bounded gate — the plan's fan-out
            // shares the governor-sized budget with every other plan.
            await AdmissionGate.WaitAsync(cancellationToken)
                .ConfigureAwait(false);
            try
            {
                using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                cts.CancelAfter(TimeSpan.FromSeconds(5));
                try
                {
                    return await exec.ExecuteAsync(plan, cts.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    throw; // 上層取消則向外傳遞
                }
                catch (Exception ex)
                {
                    // 單工具失敗回傳錯誤結果，不拋整體例外
                    return new ToolCallResult(exec.ToolId, false, string.Empty, string.Empty, 0, ex.Message);
                }
            }
            finally
            {
                AdmissionGate.Release();
            }
        }).ToList();

        var results = await Task.WhenAll(tasks).ConfigureAwait(false);
        // 速度：已依 RequiredTools 原序，無需額外排序
        return results;
    }
}

// 佔位 Executor（測試用），真實實作需注入 RAG / MarketData 等
public sealed class StubToolExecutor : IToolExecutor
{
    public string ToolId { get; }
    private readonly string _content;
    public StubToolExecutor(string toolId, string content) { ToolId = toolId; _content = content; }

    public Task<ToolCallResult> ExecuteAsync(ExecutionPlan plan, CancellationToken cancellationToken = default)
    {
        var hash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(_content)))[..16];
        return Task.FromResult(new ToolCallResult(ToolId, true, _content, hash, 1.0));
    }
}
