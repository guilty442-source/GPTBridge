// ToolContracts.cs — §16/§17 tool decision gate + tool-call v2 +
// grounded-result contracts.
//
// Command-R7B lesson: a tool existing never means it must be called —
// ToolDecisionGate decides per request and records decision/outcome
// metrics (unnecessary/missed/invalid call rates, success rate).
// Hermes lesson: the model must emit a structured star-tool-call/v2
// contract — the runtime never guesses a call out of free text.
// Fail closed: bad schema, denied policy, missing tool, exhausted
// budget or unmet confirmation all produce explicit errors, never a
// guessed call.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ToolContracts
{
    public const string CallFormat = "star-tool-call/v2";
    public const string ResultFormat = "star-tool-result/v2";
    public const string GroundedFormat = "star-grounded-result/v1";
    public const string MetricsRel =
        "xingcheng/runtime/state/tool-metrics.json";

    public static readonly string[] Decisions =
        { "TOOL_REQUIRED", "TOOL_OPTIONAL", "TOOL_NOT_REQUIRED",
          "TOOL_DENIED" };
    public static readonly string[] SupportStates =
        { "SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONFLICT" };
    public static readonly string[] ResultStatuses =
        { "ok", "error", "denied", "timeout" };

    private static string MetricsPath(string toolRoot)
        => Path.Combine(toolRoot,
            MetricsRel.Replace('/', Path.DirectorySeparatorChar));

    // ---------------------------------------------- call validation --

    /// <summary>Validate a star-tool-call/v2 document. Throws
    /// TOOL_SCHEMA_INVALID on any contract breach (fail closed).</summary>
    public static Dictionary<string, object?> ValidateCall(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID", "tool call must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != CallFormat)
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID", $"expected format {CallFormat}");
        var required = new[]
            { "request_id", "tool", "arguments", "schema_version",
              "reason_code", "requires_confirmation",
              "expected_result" };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "TOOL_SCHEMA_INVALID", $"missing field {k}");
        if (el.GetProperty("request_id").GetString() is not { Length: >0 })
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID", "request_id empty");
        if (el.GetProperty("tool").GetString() is not { Length: > 0 })
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID", "tool empty");
        if (el.GetProperty("arguments").ValueKind !=
            JsonValueKind.Object)
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID", "arguments must be an object");
        if (el.GetProperty("requires_confirmation").ValueKind is not
            (JsonValueKind.True or JsonValueKind.False))
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID",
                "requires_confirmation must be boolean");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = CallFormat,
            ["tool"] = el.GetProperty("tool").GetString(),
            ["request_id"] = el.GetProperty("request_id").GetString(),
        };
    }

    /// <summary>Validate a star-tool-result/v2 document.</summary>
    public static Dictionary<string, object?> ValidateResult(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID", "tool result must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != ResultFormat)
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID",
                $"expected format {ResultFormat}");
        foreach (var k in new[] { "request_id", "tool", "status" })
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "TOOL_SCHEMA_INVALID", $"missing field {k}");
        string status = el.GetProperty("status").GetString() ?? "";
        if (!ResultStatuses.Contains(status))
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID", $"bad status {status}");
        if (status == "error" &&
            !el.TryGetProperty("error", out _))
            throw new ExecutorError(
                "TOOL_SCHEMA_INVALID",
                "error result missing error payload");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ResultFormat,
            ["status"] = status,
        };
    }

    // ------------------------------------------------- decision gate --

    /// <summary>Decide whether a validated call may proceed. Inputs:
    /// policy allowlist, risk (high → confirmation), confirmation flag
    /// supplied by caller, tool availability, budget. Returns the gate
    /// verdict; throws the §36 error codes on fail-closed paths.</summary>
    public static Dictionary<string, object?> Gate(
        string toolRoot, JsonElement call, string[] toolsAvailable,
        int toolBudget, bool confirmed, bool requestNeededTool)
    {
        var check = ValidateCall(call);
        string tool = (string)check["tool"]!;
        var profile = RuntimeCapabilities.Load(toolRoot);

        // 1) policy permission
        if (profile.ToolsAllowed.Length > 0 &&
            !profile.ToolsAllowed.Contains(tool))
        {
            RecordMetric(toolRoot, "denied");
            throw new ExecutorError(
                "TOOL_POLICY_DENIED",
                $"{tool} not in runtime allowlist");
        }
        // 2) availability
        if (toolsAvailable.Length > 0 && !toolsAvailable.Contains(tool))
        {
            RecordMetric(toolRoot, "invalid");
            throw new ExecutorError(
                "TOOL_UNAVAILABLE", $"{tool} unavailable");
        }
        // 3) budget
        int budget = toolBudget > 0
            ? toolBudget : profile.ReasoningToolBudget;
        var metrics = LoadMetrics(toolRoot);
        int used = metrics.TryGetValue("calls_made", out var u)
            ? Convert.ToInt32(u) : 0;
        if (budget >= 0 && used >= budget)
        {
            RecordMetric(toolRoot, "budget_blocked");
            throw new ExecutorError(
                "TOOL_BUDGET_EXCEEDED",
                $"tool budget {budget} exhausted");
        }
        // 4) confirmation — high-risk calls must be explicitly confirmed
        bool needsConfirm =
            call.GetProperty("requires_confirmation").GetBoolean() ||
            profile.ToolRequiresConfirmation;
        if (needsConfirm && !confirmed)
        {
            RecordMetric(toolRoot, "awaiting_confirmation");
            throw new ExecutorError(
                "TOOL_CONFIRMATION_REQUIRED",
                $"{tool} requires confirmation");
        }

        string decision = requestNeededTool ? "TOOL_REQUIRED"
            : "TOOL_OPTIONAL";
        RecordMetric(toolRoot, "calls_made");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["decision"] = decision,
            ["tool"] = tool,
            ["requires_confirmation"] = needsConfirm,
            ["budget_remaining"] = budget >= 0
                ? Math.Max(0, budget - used - 1) : -1,
        };
    }

    /// <summary>No-call path: if the orchestrator skipped a tool that
    /// the request actually needed, that is a missed call — counted in
    /// the metrics ledger so the gate stays honest both ways.</summary>
    public static void RecordNoCall(string toolRoot, bool neededTool)
        => RecordMetric(toolRoot,
            neededTool ? "missed" : "unnecessary");

    public static void RecordOutcome(string toolRoot, bool success)
        => RecordMetric(toolRoot, success ? "succeeded" : "failed");

    // ------------------------------------------------------ metrics --

    private static Dictionary<string, object?> LoadMetrics(string toolRoot)
    {
        string path = MetricsPath(toolRoot);
        if (!File.Exists(path))
            return new Dictionary<string, object?>();
        try
        {
            return (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(File.ReadAllText(path))
                            .RootElement)!;
        }
        catch (JsonException) { return new Dictionary<string, object?>(); }
    }

    private static void RecordMetric(string toolRoot, string key)
    {
        var m = LoadMetrics(toolRoot);
        m[key] = (m.TryGetValue(key, out var v) ? Convert.ToInt64(v) : 0)
                 + 1;
        Directory.CreateDirectory(
            Path.GetDirectoryName(MetricsPath(toolRoot))!);
        ModelLifecycle.AtomicWrite(
            MetricsPath(toolRoot),
            CanonicalJson.PrettyDict(m) + "\n");
    }

    /// <summary>§16 metric surface: rates derived from the raw ledger.
    /// A ledger with zero calls reports all rates as null rather than
    /// fabricating 0.0.</summary>
    public static Dictionary<string, object?> Metrics(string toolRoot)
    {
        var m = LoadMetrics(toolRoot);
        long Get(string k) => m.TryGetValue(k, out var v)
            ? Convert.ToInt64(v) : 0;
        long made = Get("calls_made"), decided = made + Get("missed") +
            Get("unnecessary");
        double? Rate(long num, long den) =>
            den > 0 ? (double)num / den : null;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["ledger_file"] = MetricsRel,
            ["counters"] = m,
            ["unnecessary_tool_call_rate"] =
                Rate(Get("unnecessary"), decided),
            ["missed_tool_call_rate"] = Rate(Get("missed"), decided),
            ["invalid_tool_call_rate"] =
                Rate(Get("invalid"), made + Get("invalid")),
            ["tool_success_rate"] =
                Rate(Get("succeeded"), Get("succeeded") + Get("failed")),
        };
    }

    // --------------------------------------------------- grounding ---

    /// <summary>Validate a star-grounded-result/v1 claim record.
    /// When grounding is required and no claims exist the caller must
    /// surface GROUNDING_UNAVAILABLE; an UNSUPPORTED claim surfaces
    /// GROUNDING_UNSUPPORTED_CLAIM.</summary>
    public static Dictionary<string, object?> ValidateGrounded(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "GROUNDING_UNSUPPORTED_CLAIM",
                "grounded record must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != GroundedFormat)
            throw new ExecutorError(
                "GROUNDING_UNSUPPORTED_CLAIM",
                $"expected format {GroundedFormat}");
        var required = new[]
            { "claim_id", "source_id", "document_id", "chunk_id",
              "revision", "retrieval_score", "rerank_score",
              "citation", "support_state" };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "GROUNDING_UNSUPPORTED_CLAIM",
                    $"missing field {k}");
        string state = el.GetProperty("support_state").GetString() ?? "";
        if (!SupportStates.Contains(state))
            throw new ExecutorError(
                "GROUNDING_UNSUPPORTED_CLAIM",
                $"bad support_state {state}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = GroundedFormat,
            ["claim_id"] = el.GetProperty("claim_id").GetString(),
            ["support_state"] = state,
        };
    }

    // ------------------------------------------------------- helpers --

    /// <summary>JSON-file object read as a decoded dictionary — shared
    /// by the contract consumers that work on maps rather than
    /// JsonElement (LongHorizonTask, DatasetQuality).</summary>
    public static Dictionary<string, object?> ReadObject(
        string file, string errCode)
    {
        var root = ReadJson(file, errCode);
        if (root.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(errCode, $"{file}: not object");
        var map = new Dictionary<string, object?>();
        foreach (var p in root.EnumerateObject())
            map[p.Name] = ModelLifecycle.Decode(p.Value);
        return map;
    }

    /// <summary>String field from a decoded map ("" when absent).</summary>
    public static string Str(
        IReadOnlyDictionary<string, object?> m, string key)
        => TransformerTrainingRepository.Str(m, key) ?? "";

    /// <summary>List field from a decoded map (empty when absent or
    /// not a list).</summary>
    public static List<object?> Arr(
        IReadOnlyDictionary<string, object?> m, string key)
        => m.TryGetValue(key, out object? v) && v is List<object?> l
            ? l : new List<object?>();

    public static JsonElement ReadJson(string file, string errCode)
    {
        if (string.IsNullOrEmpty(file) || !File.Exists(file))
            throw new ExecutorError(errCode, $"{file}: file missing");
        try
        {
            return JsonDocument.Parse(File.ReadAllText(file)).RootElement;
        }
        catch (JsonException e)
        {
            throw new ExecutorError(errCode, $"{file}: {e.Message}");
        }
    }
}
