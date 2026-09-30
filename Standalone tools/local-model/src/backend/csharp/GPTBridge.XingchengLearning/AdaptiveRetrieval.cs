// AdaptiveRetrieval.cs — AdaptiveRetrievalRepresentation
// (efficiency/scale directive §41-§48, §55). Arctic-Embed-2.0-style
// adaptive representation: not every vector needs the maximum
// dimension or precision. Documents tier HOT/WARM/COLD; retrieval runs
// cheap compact recall + expensive high-quality rerank — grounding
// correctness is the gate, never negotiable.
//
//   Embedding      star-adaptive-embedding/v1: FULL/MEDIUM/COMPACT
//                  dims derived from the LIVE embedding model (§42 —
//                  Snowflake's numbers are never transplanted);
//                  matryoshka truncation only when supported (§43).
//   TierPolicy     §44-§45 doc-tier -> dim + precision mapping.
//   TwoStage       §46 cheap-recall -> expensive-precision plan.
//   Gate           §47 promotion gate: compact retrieval may only
//                  promote when recall@K/NDCG/MRR/citation/grounding
//                  hold against the full-precision baseline.
//   Metrics        §55 retrieval-efficiency record.
//
// Emits: star-adaptive-embedding/v1, star-vector-tier-policy/v1,
// star-retrieval-compression-gate/v1, star-retrieval-efficiency/v1.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class AdaptiveRetrieval
{
    public const string EmbeddingFormat = "star-adaptive-embedding/v1";
    public const string TierFormat = "star-vector-tier-policy/v1";
    public const string GateFormat =
        "star-retrieval-compression-gate/v1";
    public const string MetricsFormat = "star-retrieval-efficiency/v1";

    public static readonly string[] DimTiers = { "FULL", "MEDIUM", "COMPACT" };
    public static readonly string[] DocTiers = { "HOT", "WARM", "COLD" };
    public static readonly string[] PrecisionTiers =
        { "FP16", "BF16", "INT8", "INT4", "PQ_CANDIDATE" };

    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) && v.ValueKind ==
            JsonValueKind.Number && v.TryGetInt64(out long n) ? n : d;
    private static double DNum(JsonElement r, string k, double d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;
    private static string Str(JsonElement r, string k, string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;

    // ------------------------------------------------- embedding ----

    /// <summary>§42-§43 adaptive-embedding contract. full_dim comes
    /// from the live embedding model — the request supplies
    /// medium_dim/compact_dim (must satisfy 0 < compact <= medium <=
    /// full). Truncated dims are only legal when the model declares
    /// matryoshka_supported (head dims carry retrieval information).</summary>
    public static Dictionary<string, object?> Embedding(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("EMBEDDING_COMPRESSION_REGRESSION",
                "embedding record must be an object");
        long full = Num(el, "full_dim", 0);
        long med = Num(el, "medium_dim", 0);
        long compact = Num(el, "compact_dim", 0);
        if (full <= 0)
            throw new ExecutorError("EMBEDDING_COMPRESSION_REGRESSION",
                "full_dim required — dims derive from the live " +
                "embedding model, never copied from Snowflake (§42)");
        bool matryoshka =
            el.TryGetProperty("matryoshka_supported", out var ms) &&
            ms.ValueKind == JsonValueKind.True;
        var violations = new List<object?>();
        if (med == 0 && compact == 0)
            violations.Add("declare medium_dim and/or compact_dim");
        if (med < 0 || compact < 0)
            violations.Add("negative dimension");
        if (compact > 0 && med > 0 && compact > med)
            violations.Add("compact_dim must be <= medium_dim");
        if (med > full || compact > full)
            violations.Add("truncated dims may not exceed full_dim");
        if ((med > 0 && med < full || compact > 0 && compact < full) &&
            !matryoshka)
            violations.Add("truncated dims require " +
                           "matryoshka_supported=true (§43)");
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = EmbeddingFormat,
            ["full_dim"] = full,
            ["medium_dim"] = med,
            ["compact_dim"] = compact,
            ["matryoshka_supported"] = matryoshka,
            ["tiers"] = DimTiers,
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "EMBEDDING_TIERS_VALID"
                : "EMBEDDING_COMPRESSION_REGRESSION",
        };
    }

    // -------------------------------------------------- tier map ----

    /// <summary>§44-§45 the fixed doc-tier policy: HOT keeps full dim +
    /// high precision; WARM drops to compact dim + INT8; COLD adds
    /// INT4/PQ. FP32-forever for every vector is forbidden.</summary>
    public static Dictionary<string, object?> TierPolicy()
        => new()
        {
            ["ok"] = true, ["format"] = TierFormat,
            ["tiers"] = new Dictionary<string, object?>
            {
                ["HOT"] = new Dictionary<string, object?>
                {
                    ["dim_tier"] = "FULL",
                    ["precision"] = new[] { "FP16", "BF16" },
                    ["rerank_eligible"] = true,
                },
                ["WARM"] = new Dictionary<string, object?>
                {
                    ["dim_tier"] = "COMPACT",
                    ["precision"] = new[] { "INT8" },
                },
                ["COLD"] = new Dictionary<string, object?>
                {
                    ["dim_tier"] = "COMPACT",
                    ["precision"] = new[] { "INT4", "PQ_CANDIDATE" },
                },
            },
            ["rule"] = "no vector stays FP32 permanently (§45)",
        };

    // -------------------------------------------------- two-stage ---

    /// <summary>§46 two-stage retrieval plan: stage-1 compact recall
    /// over tier-compressed vectors, stage-2 high-quality rerank on the
    /// surviving candidate set.</summary>
    public static Dictionary<string, object?> TwoStage(JsonElement el)
    {
        long recallK = Num(el, "compact_recall_k", 200);
        long rerankK = Num(el, "rerank_k", 20);
        if (rerankK <= 0 || recallK < rerankK)
            throw new ExecutorError("RETRIEVAL_RECALL_REGRESSION",
                "compact_recall_k must be >= rerank_k > 0");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-two-stage-retrieval/v1",
            ["stage1_compact_recall"] = new Dictionary<string, object?>
            {
                ["vector"] = "COMPACT dim, tier precision",
                ["recall_k"] = recallK,
                ["purpose"] = "cheap recall — RAM/CPU/bandwidth bound",
            },
            ["stage2_rerank"] = new Dictionary<string, object?>
            {
                ["vector"] = "FULL dim + reranker",
                ["rerank_k"] = rerankK,
                ["purpose"] = "expensive precision on survivors only",
            },
        };
    }

    // ------------------------------------------------------- gate ---

    /// <summary>§47 promotion gate. Candidate compact representation
    /// may only promote if grounding metrics hold vs the full-precision
    /// baseline — recall@K/NDCG/MRR within tolerance, citation
    /// correctness and answer grounding at-or-above baseline.</summary>
    public static Dictionary<string, object?> Gate(JsonElement el)
    {
        if (!el.TryGetProperty("baseline", out var b) ||
            !el.TryGetProperty("candidate", out var c) ||
            b.ValueKind != JsonValueKind.Object ||
            c.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("RETRIEVAL_RECALL_REGRESSION",
                "gate requires baseline{} and candidate{} metrics");
        double tol = DNum(el, "recall_tolerance", 0.01);
        var failures = new List<object?>();
        var rows = new Dictionary<string, object?>();
        foreach (var (m, hard) in new[]
                 {
                     ("recall_at_k", false), ("ndcg", false),
                     ("mrr", false),
                     ("citation_correctness", true),
                     ("answer_grounding", true),
                 })
        {
            double vb = DNum(b, m, double.NaN),
                   vc = DNum(c, m, double.NaN);
            double delta = double.IsNaN(vb) || double.IsNaN(vc)
                ? double.NaN : vc - vb;
            bool pass = double.IsNaN(delta) ? false
                : hard ? delta >= 0 : delta >= -tol;
            rows[m] = new Dictionary<string, object?>
            {
                ["baseline"] = double.IsNaN(vb) ? null : vb,
                ["candidate"] = double.IsNaN(vc) ? null : vc,
                ["delta"] = double.IsNaN(delta) ? null : delta,
                ["pass"] = pass,
            };
            if (!pass)
                failures.Add($"{m} " +
                    (double.IsNaN(delta) ? "missing" :
                     $"regressed by {-delta:F4}"));
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = GateFormat,
            ["metrics"] = rows,
            ["recall_tolerance"] = tol,
            ["failures"] = failures,
            ["verdict"] = failures.Count == 0
                ? "PROMOTE" : "RETRIEVAL_RECALL_REGRESSION",
            ["rule"] = "cheap recall may never cost grounding " +
                       "correctness (§47)",
        };
    }

    // ---------------------------------------------------- metrics ---

    /// <summary>§55 retrieval-efficiency record.</summary>
    public static Dictionary<string, object?> Metrics(JsonElement el)
    {
        long docs = Math.Max(1, Num(el, "doc_count", 1));
        long bytes = Num(el, "vector_store_bytes", 0);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = MetricsFormat,
            ["vector_bytes_per_doc"] = Math.Round(
                (double)bytes / docs, 1),
            ["embedding_dim"] = Num(el, "embedding_dim"),
            ["vector_precision"] = Str(el, "vector_precision"),
            ["recall_at_k"] = DNum(el, "recall_at_k"),
            ["ndcg"] = DNum(el, "ndcg"),
            ["search_latency_ms"] = DNum(el, "search_latency_ms"),
            ["rerank_latency_ms"] = DNum(el, "rerank_latency_ms"),
        };
    }
}
