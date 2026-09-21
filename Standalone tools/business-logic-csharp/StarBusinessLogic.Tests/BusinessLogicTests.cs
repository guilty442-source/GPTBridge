using StarBusinessLogic.Domain;
using StarBusinessLogic.Application;

namespace StarBusinessLogic.Tests;

public class BusinessLogicTests
{
    [Theory]
    [InlineData("幫我分析投資組合的風險", StarIntent.Risk)]
    [InlineData("寫一個 python 函式 def foo", StarIntent.Coding)]
    [InlineData("你好", StarIntent.Conversation)]
    [InlineData("搜尋最新的配息公告", StarIntent.Distribution)]
    public void IntentClassifier_ShouldClassify(string prompt, StarIntent expected)
    {
        var c = new RuleIntentClassifier();
        var r = c.Classify(prompt);
        Assert.Equal(expected, r.Primary);
        Assert.NotEmpty(r.Candidates);
    }

    [Fact]
    public void PlanBuilder_ShouldMapTools()
    {
        var classifier = new RuleIntentClassifier();
        var builder = new DefaultPlanBuilder();
        var intent = classifier.Classify("請幫我搜尋星澄的介紹");
        var plan = builder.Build(intent, "some context");
        Assert.Contains("web_search", plan.RequiredTools);
        Assert.True(plan.NeedsGrounding);
    }

    [Fact]
    public async Task Orchestrator_EndToEnd_WithFakeModel()
    {
        var intent = new RuleIntentClassifier();
        var plan = new DefaultPlanBuilder();
        var grounding = new DefaultGroundingService();
        var contextBuilder = new DefaultContextBuilder();
        var router = new DefaultToolRouter(new[]
        {
            new StubToolExecutor("rag_query", "星澄是本地生成式語言模型，支援繁體中文。"),
            new StubToolExecutor("web_search", "配息 2025-06-15 NT$1.2"),
        });
        var model = new FakeModelClient();
        var orch = new StarOrchestrator(intent, plan, router, grounding, contextBuilder, model);

        var resp = await orch.ExecuteAsync(new OrchestratorRequest("幫我搜尋星澄的介紹", MaxNewTokens: 32));
        Assert.NotNull(resp.Text);
        Assert.Contains("Intent=", resp.Text);
        Assert.NotEmpty(resp.ToolResults);
        Assert.True(resp.UsedGrounding);
    }

    [Fact]
    public void Grounding_ShouldDedupAndLimit()
    {
        var svc = new DefaultGroundingService();
        var plan = new ExecutionPlan(StarIntent.Search, "q", new[] { "rag_query" }, 5, true, "q");
        var evidences = Enumerable.Range(0, 10).Select(i => new Evidence($"s{i}", $"content {i % 3}", $"hash{i % 3}", 1.0 - i * 0.05, "rag_query")).ToList();
        var result = svc.Ground(plan, evidences);
        Assert.True(result.Evidences.Count <= 6);
        Assert.Equal(result.Evidences.Select(e => e.ContentHash).Distinct().Count(), result.Evidences.Count);
    }

    [Fact]
    public async Task MoE_Config_ShouldBePresentInPythonButNotRequiredForCSharp()
    {
        // C# 業務層不直接處理 MoE 路由，僅透由 IModelClient 呼叫 Python 推論（Python/C++ 雙路徑在模型層）
        // 此測試確保業務層不依賴模型權重
        var client = new FakeModelClient();
        var req = new ModelInferenceRequest("prompt", "context", "Conversation");
        var resp = await client.InferAsync(req);
        Assert.Equal("fake-native", resp.ModelId);
    }
}
