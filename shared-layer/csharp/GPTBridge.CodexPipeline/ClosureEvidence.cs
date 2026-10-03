namespace GPTBridge.CodexPipeline;

public static class ClosureEvidence
{
    public static int CountOpen<T>(IReadOnlyCollection<T> rows, Func<T, bool> incomplete)
        => rows.Count == 0 ? 1 : rows.Count(incomplete);
}
