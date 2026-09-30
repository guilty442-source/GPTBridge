// GroundedResultV2.cs — star-grounded-result/v2 (Command-R lesson).
//
// Every claim must be traceable to a specific source with full citation
// metadata. evidence_strength classifies the support level. Unsupported
// claims must never be presented as facts.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class GroundedResultV2
{
    public const string Format = "star-grounded-result/v2";
    public const string MetricsRel =
        "xingcheng/runtime/state/grounded-metrics.json";

    public static readonly string[] EvidenceStrengths =
        { "DIRECT", "STRONG", "PARTIAL", "CONFLICTING", "UNSUPPORTED" };
    public static readonly string[] ClaimCategories =
        { "FACT", "INFERENCE", "OPINION", "FICTION", "UNKNOWN" };

    public sealed class Claim
    {
        public string ClaimId = "";
        public string Text = "";
        public string SourceId = "";
        public string ResourceId = "";
        public string DocumentId = "";
        public string DocumentRevision = "";
        public int Page;
        public string Section = "";
        public string ChunkId = "";
        public int SpanBegin;
        public int SpanEnd;
        public double RetrievalScore;
        public double RerankScore;
        public string EvidenceStrength = "UNSUPPORTED";
        public string CitationId = "";
        public string Category = "UNKNOWN";
    }

    public sealed class GroundedResult
    {
        public string ResultId = "";
        public string Query = "";
        public List<Claim> Claims = new();
        public List<string> SourceIds = new();
        public double Confidence;
        public string TraceId = "";
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "GROUNDING_SCHEMA_INVALID", "grounded result must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "GROUNDING_SCHEMA_INVALID", $"expected format {Format}");
        var required = new[]
        {
            "result_id", "query", "claims", "source_ids",
            "confidence", "trace_id",
        };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "GROUNDING_SCHEMA_INVALID", $"missing field {k}");
        if (el.GetProperty("claims").ValueKind != JsonValueKind.Array)
            throw new ExecutorError(
                "GROUNDING_SCHEMA_INVALID", "claims must be an array");
        if (el.GetProperty("source_ids").ValueKind != JsonValueKind.Array)
            throw new ExecutorError(
                "GROUNDING_SCHEMA_INVALID", "source_ids must be an array");
        double confidence = el.GetProperty("confidence").GetDouble();
        if (confidence is < 0.0 or > 1.0)
            throw new ExecutorError(
                "GROUNDING_SCHEMA_INVALID", "confidence out of [0,1]");
        var claimRequired = new[]
        {
            "claim_id", "text", "source_id", "resource_id", "document_id",
            "document_revision", "page", "section", "chunk_id",
            "span_begin", "span_end", "retrieval_score", "rerank_score",
            "evidence_strength", "citation_id", "category",
        };
        foreach (var c in el.GetProperty("claims").EnumerateArray())
        {
            foreach (var k in claimRequired)
                if (!c.TryGetProperty(k, out _))
                    throw new ExecutorError(
                        "GROUNDING_SCHEMA_INVALID",
                        $"claim missing field {k}");
            string strength = c.GetProperty("evidence_strength").GetString() ?? "";
            if (!EvidenceStrengths.Contains(strength))
                throw new ExecutorError(
                    "GROUNDING_SCHEMA_INVALID",
                    $"bad evidence_strength {strength}");
            string category = c.GetProperty("category").GetString() ?? "";
            if (!ClaimCategories.Contains(category))
                throw new ExecutorError(
                    "GROUNDING_SCHEMA_INVALID", $"bad category {category}");
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["claim_count"] = el.GetProperty("claims").GetArrayLength(),
            ["source_count"] = el.GetProperty("source_ids").GetArrayLength(),
        };
    }

    public static Dictionary<string, object?> Metrics(string toolRoot)
    {
        string path = Path.Combine(toolRoot,
            MetricsRel.Replace('/', Path.DirectorySeparatorChar));
        if (!File.Exists(path))
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["ledger_file"] = MetricsRel,
                ["total"] = 0, ["by_strength"] = new Dictionary<string, object?>(),
                ["unsupported_claim_rate"] = (double?)null,
            };
        try
        {
            var root = JsonDocument.Parse(File.ReadAllText(path)).RootElement;
            long total = root.TryGetProperty("total", out var t)
                ? t.GetInt64() : 0;
            var byStrength = new Dictionary<string, object?>();
            if (root.TryGetProperty("by_strength", out var bs) &&
                bs.ValueKind == JsonValueKind.Object)
                foreach (var p in bs.EnumerateObject())
                    byStrength[p.Name] = p.Value.GetInt64();
            long unsupported = byStrength.TryGetValue("UNSUPPORTED", out var u)
                ? (long)u : 0;
            double? rate = total > 0 ? (double)unsupported / total : null;
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["ledger_file"] = MetricsRel,
                ["total"] = total, ["by_strength"] = byStrength,
                ["unsupported_claim_rate"] = rate,
            };
        }
        catch (JsonException)
        {
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["ledger_file"] = MetricsRel,
                ["total"] = 0, ["by_strength"] = new Dictionary<string, object?>(),
                ["unsupported_claim_rate"] = (double?)null,
            };
        }
    }

    public static Dictionary<string, object?> ValidateFile(
        string toolRoot, string file)
    {
        var el = ToolContracts.ReadJson(file, "GROUNDING_SCHEMA_INVALID");
        var r = Validate(el);
        try
        {
            string dir = Path.Combine(toolRoot, XcPaths.LogsRel);
            Directory.CreateDirectory(dir);
            File.AppendAllText(
                Path.Combine(dir, "grounded-results-v2.jsonl"),
                CanonicalJson.Canonical(el) + "\n");
        }
        catch { /* ledger append is best-effort */ }
        return r;
    }
}
