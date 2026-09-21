using StarBusinessLogic.Domain;

namespace StarBusinessLogic.Application;

// 對應 Python Context Builder：把工具回傳轉為模型可理解的標準對話/證據格式
public interface IContextBuilder
{
    string Build(ExecutionPlan plan, GroundingResult grounding);
}

public sealed class DefaultContextBuilder : IContextBuilder
{
    public string Build(ExecutionPlan plan, GroundingResult grounding)
    {
        if (grounding == null || grounding.Evidences.Count == 0)
            return string.Empty;

        // 保留治理要求的來源可追溯性，不把審查結果轉為操作指令
        var header = $"Intent: {plan.Intent}\nTaskIntensity: {plan.TaskIntensity}\nEvidences: {grounding.Evidences.Count}\n";
        return header + grounding.ConsolidatedContext;
    }
}
