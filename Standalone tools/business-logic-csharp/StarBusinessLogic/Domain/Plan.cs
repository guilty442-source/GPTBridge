namespace StarBusinessLogic.Domain;

// 對應 Python StarNativePlanMixin：將意圖與正規化需求轉為執行計畫
public sealed record ExecutionPlan(
    StarIntent Intent,
    string NormalizedInstruction,
    IReadOnlyList<string> RequiredTools,
    int TaskIntensity, // 1-10
    bool NeedsGrounding,
    string GenerationPrompt
);

public interface IPlanBuilder
{
    ExecutionPlan Build(IntentResult intent, string context = "");
}

public sealed class DefaultPlanBuilder : IPlanBuilder
{
    private static readonly Dictionary<StarIntent, string[]> ToolMap = new()
    {
        [StarIntent.Search] = new[] { "web_search", "rag_query" },
        [StarIntent.Reading] = new[] { "rag_query", "context_builder" },
        [StarIntent.Analysis] = new[] { "rag_query", "market_data", "calculation" },
        [StarIntent.Calculation] = new[] { "calculation" },
        [StarIntent.Coding] = new[] { "coding_expert" },
        [StarIntent.Reasoning] = new[] { "reasoning" },
        [StarIntent.Distribution] = new[] { "market_data", "search" },
        [StarIntent.Quote] = new[] { "market_data" },
        [StarIntent.Risk] = new[] { "analysis", "calculation" },
        [StarIntent.Conversation] = Array.Empty<string>(),
    };

    public ExecutionPlan Build(IntentResult intent, string context = "")
    {
        var tools = ToolMap.TryGetValue(intent.Primary, out var t) ? t : Array.Empty<string>();
        int intensity = intent.Primary is StarIntent.Analysis or StarIntent.Coding or StarIntent.Risk ? 7 : 3;
        if (!string.IsNullOrWhiteSpace(context) && context.Length > 500) intensity = Math.Min(10, intensity + 2);
        bool grounding = intent.Primary is StarIntent.Search or StarIntent.Reading or StarIntent.Analysis or StarIntent.Distribution or StarIntent.Quote;

        // 模型仍由 Python 負責，此處僅產生提示詞與工具清單
        string prompt = intent.NormalizedPrompt;
        if (!string.IsNullOrWhiteSpace(context))
            prompt = $"Context: {context}\n\nInstruction: {intent.NormalizedPrompt}";

        return new ExecutionPlan(
            intent.Primary,
            intent.NormalizedPrompt,
            tools,
            intensity,
            grounding,
            prompt
        );
    }
}
