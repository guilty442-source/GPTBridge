namespace GPTBridge.CodexPipeline;

public static class ClosureEvidence
{
    public static bool ProjectionIncomplete(string generation, string evidenceGeneration,
        string result, long required, long registered, long present, long matched,
        long stale, long missing)
        => string.IsNullOrWhiteSpace(generation) || evidenceGeneration != generation
            || result != "PASS" || required <= 0 || registered != required
            || present != required || matched != required || stale != 0 || missing != 0;

    public static int CountOpen<T>(IReadOnlyCollection<T> rows, Func<T, bool> incomplete)
        => rows.Count == 0 ? 1 : rows.Count(incomplete);
}
