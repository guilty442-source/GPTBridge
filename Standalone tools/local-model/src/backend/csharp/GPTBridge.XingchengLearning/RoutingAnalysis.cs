// RoutingAnalysis.cs — §29 two-level routing trace +
// star-routing-trace/v1 evidence records.
//
// Service-level routing (intent -> service expert) and neural-level
// routing (per-layer MoE router trace from the C++ engine) merge into
// one trace. Aggregation metrics: expert_affinity, specialization,
// overlap, stability, hotspot, shared_expert_dependency. Sensitive
// prompt text is never stored — only digests and ids.

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class RoutingAnalysis
{
    public const string Format = "star-routing-trace/v1";
    public const string RelDir =
        "xingcheng/runtime/state/routing-traces";

    private static readonly string[] Required =
        { "request", "intent", "service_expert", "model_generation",
          "architecture", "layer", "router_logits_summary",
          "selected_experts", "shared_expert", "tool", "rag",
          "result", "evaluation" };

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "MOE_TRACE_INVALID", "routing trace must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "MOE_TRACE_INVALID", $"expected format {Format}");
        foreach (var k in Required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "MOE_TRACE_INVALID", $"missing field {k}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
        };
    }

    /// <summary>Record one trace row (prompt digested, never stored
    /// verbatim when sensitive).</summary>
    public static Dictionary<string, object?> Record(
        string toolRoot, JsonElement trace)
    {
        var check = Validate(trace);
        var rec = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["recorded_at"] = XcPaths.IsoNow(),
        };
        foreach (var p in trace.EnumerateObject())
            rec[p.Name] = ModelLifecycle.Decode(p.Value);
        rec["prompt_hash"] = Sha(
            (trace.TryGetProperty("request", out var r)
                ? r.ToString() : ""));
        Directory.CreateDirectory(Path.Combine(toolRoot,
            RelDir.Replace('/', Path.DirectorySeparatorChar)));
        string id = "rt-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss")
            + "-" + Guid.NewGuid().ToString("N")[..8];
        rec["trace_id"] = id;
        ModelLifecycle.AtomicWrite(
            Path.Combine(toolRoot,
                RelDir.Replace('/', Path.DirectorySeparatorChar),
                id + ".json"),
            CanonicalJson.PrettyDict(rec) + "\n");
        rec["ok"] = true;
        return rec;
    }

    /// <summary>Aggregate affinity/specialization/overlap/stability/
    /// hotspot/shared_expert_dependency over stored traces.</summary>
    public static Dictionary<string, object?> Aggregate(string toolRoot)
    {
        string dir = Path.Combine(toolRoot,
            RelDir.Replace('/', Path.DirectorySeparatorChar));
        var expertCounts = new Dictionary<string, long>(
            StringComparer.Ordinal);
        var intentExperts = new Dictionary<string, HashSet<string>>(
            StringComparer.Ordinal);
        long total = 0; long sharedUsed = 0;
        if (Directory.Exists(dir))
            foreach (var f in Directory.EnumerateFiles(dir, "*.json"))
            {
                Dictionary<string, object?> t;
                try
                {
                    t = (Dictionary<string, object?>)
                        ModelLifecycle.Decode(JsonDocument.Parse(
                            File.ReadAllText(f)).RootElement)!;
                }
                catch (JsonException) { continue; }
                ++total;
                string intent = t.GetValueOrDefault("intent")
                    as string ?? "";
                if (!intentExperts.TryGetValue(intent, out var set))
                    intentExperts[intent] = set =
                        new HashSet<string>(StringComparer.Ordinal);
                if (t.GetValueOrDefault("selected_experts") is
                    List<object?> sel)
                    foreach (var e in sel)
                    {
                        string es = e?.ToString() ?? "";
                        expertCounts[es] =
                            expertCounts.GetValueOrDefault(es) + 1;
                        set.Add(es);
                    }
                if (t.GetValueOrDefault("shared_expert") is bool sb &&
                    sb) ++sharedUsed;
            }
        long max = expertCounts.Count > 0
            ? expertCounts.Values.Max() : 0;
        var hotspots = expertCounts
            .Where(kv => total > 0 &&
                         kv.Value > Math.Max(4, total / 2))
            .Select(kv => (object?)kv.Key).ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["traces"] = total,
            ["expert_affinity"] = intentExperts.ToDictionary(
                kv => kv.Key,
                kv => (object?)kv.Value.OrderBy(x => x).ToList()),
            ["expert_specialization"] =
                intentExperts.Count(kv => kv.Value.Count <= 2),
            ["expert_overlap"] = intentExperts.Count > 1
                ? intentExperts.Values.Aggregate(
                    Enumerable.Empty<string>(),
                    (a, s) => a.Union(s)).Count()
                : 0,
            ["expert_hotspot"] = hotspots,
            ["expert_stability"] = max,
            ["shared_expert_dependency"] =
                total > 0 ? (double)sharedUsed / total : 0.0,
        };
    }

    private static string Sha(string s)
        => Convert.ToHexString(SHA256.HashData(
               Encoding.UTF8.GetBytes(s))).ToLowerInvariant();
}
