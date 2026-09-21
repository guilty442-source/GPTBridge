namespace StarBusinessLogic.Domain;

// 對應 Python StarNativeGroundingMixin：把工具回傳轉為模型可理解的證據格式
public sealed record Evidence(
    string SourceId,
    string Content,
    string ContentHash,
    double Relevance,
    string SourceType // rag / web_search / market_data / calculation
);

public sealed record GroundingResult(
    IReadOnlyList<Evidence> Evidences,
    string ConsolidatedContext,
    bool HasSufficientEvidence
);

public interface IGroundingService
{
    GroundingResult Ground(ExecutionPlan plan, IReadOnlyList<Evidence> raw);
}

public sealed class DefaultGroundingService : IGroundingService
{
    public GroundingResult Ground(ExecutionPlan plan, IReadOnlyList<Evidence> raw)
    {
        // 正確性：空/無效輸入 fail-closed，不拋例外
        if (raw == null || raw.Count == 0)
            return new GroundingResult(Array.Empty<Evidence>(), string.Empty, !plan.NeedsGrounding);

        // 速度：單次遍歷 + 預分配，避免多次 Where/OrderBy 造成多次枚舉
        var filtered = new List<Evidence>(raw.Count);
        foreach (var e in raw)
        {
            if (e == null || string.IsNullOrWhiteSpace(e.Content) || string.IsNullOrWhiteSpace(e.ContentHash))
                continue;
            filtered.Add(e);
        }
        if (filtered.Count == 0)
            return new GroundingResult(Array.Empty<Evidence>(), string.Empty, !plan.NeedsGrounding);

        // 穩定排序：Relevance 降序，相同 Relevance 保持原序以確保可重現
        filtered.Sort((a, b) => b.Relevance.CompareTo(a.Relevance));

        var seen = new HashSet<string>(StringComparer.Ordinal);
        var selected = new List<Evidence>(6);
        foreach (var e in filtered)
        {
            if (selected.Count >= 6) break;
            if (seen.Add(e.ContentHash))
                selected.Add(e);
        }

        // 正確性：若需 grounding 但無有效證據，標記不充分，業務層可決定是否回退或要求補充輸入
        bool sufficient = !plan.NeedsGrounding || selected.Count > 0;

        // 速度：StringBuilder 避免 Join 多次分配
        var sb = new System.Text.StringBuilder(selected.Count * 128);
        for (int i = 0; i < selected.Count; i++)
        {
            if (i > 0) sb.Append("\n\n---\n\n");
            var e = selected[i];
            sb.Append($"[Evidence {i + 1} | {e.SourceType} | {e.SourceId}]\n");
            sb.Append(e.Content);
        }

        return new GroundingResult(selected, sb.ToString(), sufficient);
    }
}
