namespace StarBusinessLogic.Domain;

// 對應 Python StarNativeIntentMixin / StarModelRegistry 的意圖分類
// 模型周邊業務由 C# 負責，模型推論仍經 Python
public enum StarIntent
{
    Conversation,
    Capabilities,
    Status,
    Search,
    Distribution,
    Quote,
    Risk,
    Analysis,
    Calculation,
    Reasoning,
    Statistics,
    DataOrganization,
    Coding,
    SelfUpgrade,
    Reading,
    Unknown
}

public sealed record IntentResult(
    StarIntent Primary,
    IReadOnlyList<StarIntent> Candidates,
    double Confidence,
    string NormalizedPrompt
);

public interface IIntentClassifier
{
    IntentResult Classify(string prompt);
}

public sealed class RuleIntentClassifier : IIntentClassifier
{
    // 速度：編譯正則 + 快取（對應 Python COMMAND_UNDERSTANDING_CACHE）
    private static readonly System.Text.RegularExpressions.Regex CalcRegex =
        new(@"\d+\s*[\+\-\*\/]", System.Text.RegularExpressions.RegexOptions.Compiled);
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, (IntentResult Result, long Ticks)> _cache = new();
    private const int MaxCache = 128;
    private const int MaxPromptLen = 2000;
    private static readonly TimeSpan Ttl = TimeSpan.FromMinutes(5);

    public IntentResult Classify(string prompt)
    {
        // 正確性：空值/超長截斷，fail-closed 不拋例外
        var text = (prompt ?? string.Empty).Trim();
        if (text.Length > MaxPromptLen) text = text[..MaxPromptLen];
        if (text.Length == 0) return new IntentResult(StarIntent.Conversation, new[] { StarIntent.Conversation }, 0.5, string.Empty);

        // 速度：快取命中直接回傳
        if (_cache.TryGetValue(text, out var cached) && (DateTime.UtcNow.Ticks - cached.Ticks) < Ttl.Ticks)
            return cached.Result;

        var lower = text.ToLowerInvariant();
        StarIntent intent = StarIntent.Conversation;
        double conf = 0.6;

        // 正確性：優先順序與 Python 保持一致（配息/報價優先於一般搜尋），避免誤判
        if (lower.Contains("配息") || lower.Contains("distribution") || lower.Contains("除息"))
        { intent = StarIntent.Distribution; conf = 0.9; }
        else if (lower.Contains("報價") || lower.Contains("淨值") || lower.Contains("quote") || lower.Contains("price"))
        { intent = StarIntent.Quote; conf = 0.9; }
        else if (lower.Contains("風險") || lower.Contains("risk"))
        { intent = StarIntent.Risk; conf = 0.85; }
        else if (lower.Contains("分析") || lower.Contains("analysis"))
        { intent = StarIntent.Analysis; conf = 0.85; }
        else if (lower.Contains("計算") || lower.Contains("calculation") || CalcRegex.IsMatch(lower))
        { intent = StarIntent.Calculation; conf = 0.8; }
        else if (lower.Contains("程式") || lower.Contains("code") || lower.Contains("def ") || lower.Contains("```"))
        { intent = StarIntent.Coding; conf = 0.85; }
        else if (lower.Contains("閱讀") || lower.Contains("reading") || lower.Contains("摘要"))
        { intent = StarIntent.Reading; conf = 0.8; }
        else if (lower.Contains("搜尋") || lower.Contains("search"))
        { intent = StarIntent.Search; conf = 0.8; }

        var candidates = new[] { intent, StarIntent.Conversation }.Distinct().ToList();
        var result = new IntentResult(intent, candidates, Math.Clamp(conf, 0.0, 1.0), text);

        // 速度：LRU 式淘汰（超過 MaxCache 隨機移除一個，避免無限增長）
        if (_cache.Count >= MaxCache)
        {
            var first = _cache.Keys.FirstOrDefault();
            if (first != null) _cache.TryRemove(first, out _);
        }
        _cache[text] = (result, DateTime.UtcNow.Ticks);
        return result;
    }
}
