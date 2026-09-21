using StarBusinessLogic.Domain;

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
    private static readonly HashSet<string> Allowed = new(new[] { "rag_query", "web_search", "market_data", "calculation", "coding_expert", "reasoning", "context_builder" },
        StringComparer.OrdinalIgnoreCase);

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
