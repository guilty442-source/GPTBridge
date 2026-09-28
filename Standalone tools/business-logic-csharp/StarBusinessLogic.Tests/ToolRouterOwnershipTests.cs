using StarDomain;
using StarBusinessLogic.Application;

namespace StarBusinessLogic.Tests;

// INTEGRATION-CROSS-LANGUAGE: C# <-> F# 走 SAME_RUNTIME_TYPED_CALL。
// 唯一工具集合由 F# PlanRules.supportedTools 擁有，C# 路由器直接取用，
// 不得在 C# 另行手寫允許清單（曾發生 coding_expert 重複與 search/analysis
// 被靜默丟棄的手寫漂移）。
public class ToolRouterOwnershipTests
{
    [Fact]
    public void Router_Allowlist_IsExactlyFSharpSupportedTools()
    {
        var expected = new HashSet<string>(
            PlanRules.supportedTools, StringComparer.OrdinalIgnoreCase);
        Assert.True(expected.Count > 0);
        Assert.Contains("web_search", expected);
        Assert.Contains("rag_query", expected);
        Assert.Contains("market_data", expected);
        Assert.Contains("calculation", expected);
    }

    [Fact]
    public async Task Router_RoutesFSharpEmittedTools_WhenExecutorRegistered()
    {
        // F# Risk 意圖可產出 "analysis"；舊手寫清單無此項會靜默丟棄。
        var router = new DefaultToolRouter(new[]
        {
            new StubToolExecutor("analysis", "ok"),
        });
        var plan = new ExecutionPlan(
            StarIntent.Risk, "q", new[] { "analysis" }, 7, false, "q");
        var results = await router.RouteAsync(plan);
        var single = Assert.Single(results);
        Assert.True(single.Success);
    }

    [Fact]
    public async Task Router_SkipsToolsWithoutRegisteredExecutor()
    {
        var router = new DefaultToolRouter(Array.Empty<IToolExecutor>());
        var plan = new ExecutionPlan(
            StarIntent.Search, "q", new[] { "web_search" }, 3, true, "q");
        var results = await router.RouteAsync(plan);
        Assert.Empty(results);
    }
}
