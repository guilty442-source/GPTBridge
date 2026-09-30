// LongContextRuntime.cs — unified long-context runtime (§20) built on
// the MiniMax-M3-style block index (§8, §7.1 field set).
//
//   LongContextRuntime =
//       ContextBudgetManager + LongContextBlockIndex +
//       ContextCacheManager + InferenceMemoryPlanner
//
// §20 honesty contract: the probe ladder (2K..64K) proves runtime
// mechanics only. LongContextRuntime never claims a model context
// capability — a runtime that accepts a longer input does not mean the
// model supports that length (§20 last rule). Max claimable context
// stays whatever the generation certifies (today: 128K for
// gen-2-consolidated).
//
// §8 decoupling rule: the block index is runtime metadata — it serves
// sparse attention, context compression, RAG, prefix cache and agent
// resume, and is never bound into the model architecture.

namespace GPTBridge.XingchengLearning;

/// <summary>§7.1/§8 block record — both specs merged into one schema.
/// ``memory_tier``/``resident``/``last_access`` drive residency
/// decisions; ``importance``/``recency``/``retrieval_score`` drive
/// selection; hashes + source provenance serve RAG and resume.</summary>
internal sealed class ContextBlock
{
    public long BlockId;
    public long TokenBegin;
    public long TokenEnd;
    public string MemoryTier = "hot";      // hot | warm | cold
    public double Importance;              // 0..1
    public string RetrievalKey = "";       // embedding/key hash for RAG
    public bool Resident = true;           // currently materialized
    public long LastAccessUnix;
    public string SourceSpan = "";         // source doc/span id
    public string SemanticHash = "";       // content signature
    public string Modality = "TEXT";       // TEXT | IMAGE | ...
    public double Recency;                 // 0..1 computed at insert
    public double RetrievalScore;          // set per-query
}

/// <summary>§7.1 context-selection policy — full, sparse (block-index
/// selected), or recurrent_summary (DeltaNet carry + recent window).</summary>
internal enum ContextSelectionPolicy { FULL, SPARSE, RECURRENT_SUMMARY }

/// <summary>§8 LongContextBlockIndex — model-agnostic block metadata
/// store with importance/recency-aware selection.</summary>
internal sealed class LongContextBlockIndex
{
    private readonly List<ContextBlock> _blocks = new();

    public long BlockTokens = 512;         // index granularity

    /// <summary>Index a span; importance in [0,1], retrieval_key is the
    /// content address. Returns the assigned block id.</summary>
    public ContextBlock Add(
        long tokenBegin, long tokenEnd, double importance,
        string retrievalKey, string semanticHash,
        string modality = "TEXT", string sourceSpan = "")
    {
        if (tokenEnd <= tokenBegin)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                $"invalid span [{tokenBegin},{tokenEnd}]");
        if (importance < 0 || importance > 1)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                $"importance out of range: {importance}");
        var b = new ContextBlock
        {
            BlockId = _blocks.Count, TokenBegin = tokenBegin,
            TokenEnd = tokenEnd, Importance = importance,
            RetrievalKey = retrievalKey, SemanticHash = semanticHash,
            Modality = modality, SourceSpan = sourceSpan,
            LastAccessUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            Recency = 1.0,   // newest insert is most recent
        };
        _blocks.Add(b);
        return b;
    }

    /// <summary>Select blocks under a token budget for a query.
    /// Scoring: importance + recency + per-query retrieval score
    /// (caller computes retrieval_score via RetrievalKey match or a
    /// probe scorer). Sparse selection = top-scored blocks until the
    /// budget is exhausted; coverage_ratio reports what fraction of the
    /// context was served.</summary>
    public Dictionary<string, object?> Select(
        ContextSelectionPolicy policy, long tokenBudget,
        Func<ContextBlock, double>? scorer = null)
    {
        if (policy == ContextSelectionPolicy.FULL ||
            policy == ContextSelectionPolicy.RECURRENT_SUMMARY)
        {
            // FULL: serve everything if it fits, else fail-closed —
            // truncation must be an explicit caller decision.
            // RECURRENT_SUMMARY: runtime contract only returns the
            // newest-resident tail; the recurrent carry is the model
            // plane's DeltaNet state, surfaced separately.
            var all = policy == ContextSelectionPolicy.FULL
                ? _blocks.ToList()
                : _blocks.Where(b => b.Resident)
                    .OrderByDescending(b => b.LastAccessUnix)
                    .ToList();
            long need = all.Sum(b => b.TokenEnd - b.TokenBegin);
            if (policy == ContextSelectionPolicy.FULL && need > tokenBudget)
                throw new ExecutorError(
                    ConvErr.ToolDecisionInvalid,
                    $"full context {need} exceeds budget {tokenBudget}");
            return Report(policy, all.TakeWhile(
                MakeBudgetTake(tokenBudget)).ToList());
        }
        // SPARSE: score, rank, budget-fill.
        var ranked = _blocks
            .Select(b => (b, s: (scorer?.Invoke(b) ?? 0)
                              + b.Importance + b.Recency))
            .OrderByDescending(x => x.s).ToList();
        var picked = new List<ContextBlock>();
        long used = 0;
        foreach (var (b, _) in ranked)
        {
            long n = b.TokenEnd - b.TokenBegin;
            if (used + n > tokenBudget) break;
            used += n; picked.Add(b);
            b.LastAccessUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        }
        picked.Sort((a, b) => a.TokenBegin.CompareTo(b.TokenBegin));
        return Report(policy, picked);
    }

    private static Func<ContextBlock, bool> MakeBudgetTake(long budget)
    {
        long used = 0;
        return b =>
        {
            long n = b.TokenEnd - b.TokenBegin;
            if (used + n > budget) return false;
            used += n; return true;
        };
    }

    private Dictionary<string, object?> Report(
        ContextSelectionPolicy policy, List<ContextBlock> picked)
    {
        long total = _blocks.Sum(b => b.TokenEnd - b.TokenBegin);
        long served = picked.Sum(b => b.TokenEnd - b.TokenBegin);
        return new Dictionary<string, object?>
        {
            ["format"] = "star-long-context-select/v1",
            ["policy"] = policy.ToString(),
            ["selected_blocks"] = picked.Select(
                b => (object?)b.BlockId).ToList(),
            ["context_blocks_selected"] = picked.Count,
            ["tokens_served"] = served,
            ["coverage_ratio"] =
                total > 0 ? (double)served / total : 1.0,
        };
    }

    public int Count => _blocks.Count;
    public long TotalTokens => _blocks.Sum(b => b.TokenEnd - b.TokenBegin);
}

/// <summary>§20 unified LongContextRuntime — composition point for
/// budget, index, cache and memory planning. Read-only composition:
/// it never mutates model state.</summary>
internal sealed class LongContextRuntime
{
    public const string Format = "star-long-context-runtime/v1";

    /// <summary>§20 probe ladder — the only context sizes the runtime
    /// may claim to *test*. Capability claims remain generation-bound.</summary>
    public static readonly long[] ProbeLadder =
        { 2048, 4096, 8192, 16384, 32768, 65536 };

    public LongContextBlockIndex Index { get; } = new();
    public ContextCacheManager Cache { get; } = new();

    /// <summary>Fail-closed capability check: a requested context beyond
    /// the certified maximum is an orchestration decision the caller
    /// must justify — the runtime reports PROBE_ONLY, never ACTIVE.</summary>
    public static string ContextStatus(
        long requestedTokens, long certifiedMax)
        => requestedTokens <= certifiedMax ? "ACTIVE" : "PROBE_ONLY";
}
