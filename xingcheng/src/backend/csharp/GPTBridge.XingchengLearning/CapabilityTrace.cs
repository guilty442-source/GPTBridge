// CapabilityTrace.cs — ``star-capability-trace/v1`` +
// ``star-capability-result/v1`` (convergence §16/§17).
//
// Two-level capability observability, record-and-analyse only — during
// the capability-training freeze nothing here may feed back into a
// trainer job.
//
// Level 2 (request trace, ``capability-trace.jsonl``): one event per
// governed request — outer service-expert routing plus inner MoE
// router evidence per layer:
//
//   request_id, intent, service_expert,
//   model_generation, architecture_generation,
//   router_layers[]: {layer_id, router_type,
//                     selected_neural_experts[], shared_expert_used}
//   tool_used, rag_used, final_result, capability_eval
//
// Level 1 (capability result, ``capability-results.jsonl``):
// ``star-capability-result/v1`` — the unified outer-expert contract:
//
//   capability, status, evidence, confidence, source,
//   failure, fallback, trace_id
//
// Both lanes are append-only JSONL under xingcheng/runtime/logs/ —
// canonical, hashable, and readable without the producing generation.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityTrace
{
    public const string TraceFormat = "star-capability-trace/v1";
    public const string ResultFormat = "star-capability-result/v1";
    public const string TraceRel =
        "xingcheng/runtime/logs/capability-trace.jsonl";
    public const string ResultsRel =
        "xingcheng/runtime/logs/capability-results.jsonl";

    public static readonly string[] ResultStatuses =
        { "pass", "fail", "degraded", "skipped" };

    private static string Path(string toolRoot, string rel)
        => System.IO.Path.Combine(
            toolRoot, rel.Replace('/', System.IO.Path.DirectorySeparatorChar));

    private static void AppendJsonl(
        string path, Dictionary<string, object?> record)
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
        File.AppendAllText(
            path, CanonicalJson.PlainDict(record) + "\n",
            new System.Text.UTF8Encoding(false));
    }

    private static Dictionary<string, object?> LoadJson(string file)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(file));
        if (doc.RootElement.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("TRACE_RECORD_NOT_OBJECT", file);
        var map = new Dictionary<string, object?>();
        foreach (var p in doc.RootElement.EnumerateObject())
            map[p.Name] = ModelLifecycle.Decode(p.Value);
        return map;
    }

    private static string Need(
        IReadOnlyDictionary<string, object?> m, string key, string code)
    {
        string v = TransformerTrainingRepository.Str(m, key) ?? "";
        if (v.Length == 0)
            throw new ExecutorError(code, $"missing {key}");
        return v;
    }

    // --------------------------------------------------- trace record --

    /// <summary>Validate + append a ``star-capability-trace/v1`` event.
    /// Router evidence may be one event per layer (``layer_id`` etc. at
    /// top level) or a ``router_layers`` array — both are normalized to
    /// the array form.</summary>
    public static Dictionary<string, object?> RecordTrace(
        string toolRoot, string file)
    {
        var t = LoadJson(file);
        string requestId = Need(t, "request_id", "TRACE_REQUEST_ID");
        string intent = Need(t, "intent", "TRACE_INTENT");
        var rec = new Dictionary<string, object?>
        {
            ["format"] = TraceFormat,
            ["recorded_at"] = XcPaths.IsoNow(),
            ["request_id"] = requestId,
            ["intent"] = intent,
            ["service_expert"] =
                TransformerTrainingRepository.Str(t, "service_expert") ?? "",
            ["model_generation"] =
                TransformerTrainingRepository.Str(t, "model_generation")
                ?? GenerationMigration
                    .CurrentGeneration(toolRoot),
            ["architecture_generation"] =
                TransformerTrainingRepository.Str(t, "architecture_generation")
                ?? "",
            ["tool_used"] =
                TransformerTrainingRepository.Truthy(
                    t.GetValueOrDefault("tool_used")),
            ["rag_used"] =
                TransformerTrainingRepository.Truthy(
                    t.GetValueOrDefault("rag_used")),
            ["final_result"] =
                t.GetValueOrDefault("final_result")
                ?? new Dictionary<string, object?>(),
            ["capability_eval"] =
                t.GetValueOrDefault("capability_eval")
                ?? new Dictionary<string, object?>(),
        };

        var layers = new List<object?>();
        if (t.GetValueOrDefault("router_layers") is List<object?> rl)
        {
            foreach (object? item in rl)
            {
                if (item is not Dictionary<string, object?> l)
                    throw new ExecutorError(
                        "TRACE_ROUTER_LAYER", "router layer not object");
                layers.Add(NormalizeLayer(l));
            }
        }
        else if (t.ContainsKey("layer_id") ||
                 t.ContainsKey("selected_neural_experts"))
        {
            layers.Add(NormalizeLayer(t));
        }
        rec["router_layers"] = layers;

        string path = Path(toolRoot, TraceRel);
        AppendJsonl(path, rec);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = TraceFormat,
            ["request_id"] = requestId,
            ["router_layers"] = layers.Count,
            ["log"] = "xingcheng/runtime/logs/capability-trace.jsonl",
        };
    }

    private static Dictionary<string, object?> NormalizeLayer(
        IReadOnlyDictionary<string, object?> l)
    {
        object? layerId = l.GetValueOrDefault("layer_id");
        if (layerId == null)
            throw new ExecutorError("TRACE_LAYER_ID", "missing layer_id");
        string routerType =
            TransformerTrainingRepository.Str(l, "router_type") ?? "";
        if (routerType.Length == 0)
            throw new ExecutorError("TRACE_ROUTER_TYPE", "missing router_type");
        var selected = new List<object?>();
        if (l.GetValueOrDefault("selected_neural_experts")
            is List<object?> sel)
            selected.AddRange(sel);
        return new Dictionary<string, object?>
        {
            ["layer_id"] = layerId,
            ["router_type"] = routerType,
            ["selected_neural_experts"] = selected,
            ["shared_expert_used"] =
                TransformerTrainingRepository.Truthy(
                    l.GetValueOrDefault("shared_expert_used")),
        };
    }

    // -------------------------------------------------- result record --

    /// <summary>Validate + append a ``star-capability-result/v1`` record —
    /// the unified outer-expert output contract.</summary>
    public static Dictionary<string, object?> RecordResult(
        string toolRoot, string file)
    {
        var r = LoadJson(file);
        string capability = Need(r, "capability", "RESULT_CAPABILITY");
        string status = Need(r, "status", "RESULT_STATUS");
        if (!ResultStatuses.Contains(status))
            throw new ExecutorError("RESULT_STATUS",
                $"expected one of {string.Join("/", ResultStatuses)}");
        var rec = new Dictionary<string, object?>
        {
            ["format"] = ResultFormat,
            ["recorded_at"] = XcPaths.IsoNow(),
            ["capability"] = capability,
            ["status"] = status,
            ["evidence"] =
                r.GetValueOrDefault("evidence")
                ?? new List<object?>(),
            ["confidence"] =
                r.GetValueOrDefault("confidence") ?? 0.0,
            ["source"] =
                TransformerTrainingRepository.Str(r, "source") ?? "",
            ["failure"] =
                TransformerTrainingRepository.Str(r, "failure") ?? "",
            ["fallback"] =
                TransformerTrainingRepository.Str(r, "fallback") ?? "",
            ["trace_id"] =
                TransformerTrainingRepository.Str(r, "trace_id") ?? "",
        };
        string path = Path(toolRoot, ResultsRel);
        AppendJsonl(path, rec);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = ResultFormat,
            ["capability"] = capability,
            ["status"] = status,
            ["log"] = "xingcheng/runtime/logs/capability-results.jsonl",
        };
    }

    // --------------------------------------------------------- status --

    public static Dictionary<string, object?> Status(
        string toolRoot, int tail = 20)
    {
        var byCap = new Dictionary<string, int>(StringComparer.Ordinal);
        var byStatus = new Dictionary<string, int>(StringComparer.Ordinal);
        int traces = 0, results = 0;
        string tp = Path(toolRoot, TraceRel);
        if (File.Exists(tp))
        {
            foreach (string line in File.ReadLines(tp))
            {
                if (line.Trim().Length == 0) continue;
                ++traces;
            }
        }
        string rp = Path(toolRoot, ResultsRel);
        if (File.Exists(rp))
        {
            foreach (string line in File.ReadLines(rp))
            {
                if (line.Trim().Length == 0) continue;
                ++results;
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    string cap = doc.RootElement
                        .TryGetProperty("capability", out var c)
                        ? c.GetString() ?? "?" : "?";
                    string st = doc.RootElement
                        .TryGetProperty("status", out var s)
                        ? s.GetString() ?? "?" : "?";
                    byCap[cap] = byCap.GetValueOrDefault(cap) + 1;
                    byStatus[st] = byStatus.GetValueOrDefault(st) + 1;
                }
                catch { /* malformed tail line — count only */ }
            }
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["trace_events"] = traces,
            ["capability_results"] = results,
            ["by_capability"] = byCap,
            ["by_status"] = byStatus,
            ["trace_log"] = TraceRel,
            ["results_log"] = ResultsRel,
        };
    }
}
