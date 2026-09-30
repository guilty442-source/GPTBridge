// ToolContracts.cs — the Command R7B / Hermes absorption (§16-§18):
//
//   ToolDecisionGate            tool-need classification + metrics
//   star-tool-call/v2           structured call + result schemas (the
//                               single tool-call format — models never
//                               emit free-form calls a parser must guess)
//   star-grounded-result/v1     claim-level citation contract
//   StructuredOutputValidator   parse -> schema -> repair-once ->
//                               revalidate; a JSON parse failure can
//                               never report success (§18)
//
// All decisions are typed and fail-closed; "a tool exists" is never
// proof that a call is required.

using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.XingchengLearning;

/// <summary>§16 tool-call need classification.</summary>
internal enum ToolDecision
{
    TOOL_REQUIRED,
    TOOL_OPTIONAL,
    TOOL_NOT_REQUIRED,
    TOOL_DENIED,
}

/// <summary>§16 gate — decides whether the request needs a tool before
/// any call is shaped. Inputs are the request's declared need signals
/// (never the model's free text): a claimed requirement must match the
/// registered tool surface, and denied/duplicate calls are refused.</summary>
internal sealed class ToolDecisionGate
{
    public sealed class Metrics
    {
        public long Total;
        public long ToolCalls;
        public long UnnecessaryCalls;
        public long MissedCalls;
        public long InvalidCalls;
        public long SuccessfulCalls;

        public Dictionary<string, object?> ToDict() => new()
        {
            ["total_requests"] = Total,
            ["tool_calls"] = ToolCalls,
            ["unnecessary_tool_call_rate"] =
                Rate(UnnecessaryCalls, ToolCalls),
            ["missed_tool_call_rate"] =
                Rate(MissedCalls, Total - ToolCalls),
            ["invalid_tool_call_rate"] =
                Rate(InvalidCalls, ToolCalls),
            ["tool_success_rate"] =
                Rate(SuccessfulCalls, ToolCalls),
        };

        private static double Rate(long n, long d)
            => d > 0 ? Math.Round((double)n / d, 6) : 0.0;
    }

    /// <param name="requestNeedsTool">the request's declared need
    /// (retrieval, computation, action) — set by the caller, never by
    /// model text.</param>
    /// <param name="toolRegistered">the named tool exists in the
    /// governed registry.</param>
    /// <param name="toolPermitted">the governed permission layer allows
    /// this tool for this caller.</param>
    /// <param name="capabilityResolvable">the request's need can be
    /// answered by the model alone (no external fact/action).</param>
    public static ToolDecision Decide(
        bool requestNeedsTool, bool toolRegistered, bool toolPermitted,
        bool capabilityResolvable)
    {
        if (requestNeedsTool && !toolPermitted)
            return ToolDecision.TOOL_DENIED;
        if (!requestNeedsTool && capabilityResolvable)
            return ToolDecision.TOOL_NOT_REQUIRED;
        if (requestNeedsTool && toolRegistered && toolPermitted)
            return capabilityResolvable
                ? ToolDecision.TOOL_OPTIONAL
                : ToolDecision.TOOL_REQUIRED;
        if (requestNeedsTool && (!toolRegistered || !toolPermitted))
            return ToolDecision.TOOL_DENIED;
        // No declared need but the model could use one as enrichment.
        return toolRegistered && toolPermitted
            ? ToolDecision.TOOL_OPTIONAL
            : ToolDecision.TOOL_NOT_REQUIRED;
    }

    /// <summary>Fold one observed outcome into the rolling metrics:
    /// called=true when a call was emitted, valid/successful reflect the
    /// tool result, needed is the post-hoc label from evaluation.</summary>
    public static void Observe(
        Metrics m, bool called, bool valid, bool successful, bool needed)
    {
        ++m.Total;
        if (called)
        {
            ++m.ToolCalls;
            if (!valid) ++m.InvalidCalls;
            if (valid && successful) ++m.SuccessfulCalls;
            if (!needed) ++m.UnnecessaryCalls;
        }
        else if (needed)
        {
            ++m.MissedCalls;
        }
    }
}

/// <summary>§17 star-tool-call/v2 — the only tool-call envelope. A
/// call that fails validation is never passed to a parser that guesses
/// (TOOL_SCHEMA_INVALID, fail-closed).</summary>
internal static class ToolCallV2
{
    public const string Format = "star-tool-call/v2";

    public static readonly string[] RequiredRequest =
        { "request_id", "tool", "arguments", "schema_version",
          "reason_code", "requires_confirmation", "expected_result" };
    public static readonly string[] RequiredResult =
        { "request_id", "tool", "status", "payload", "evidence",
          "error" };

    /// <summary>Validate a call request object; throws
    /// TOOL_SCHEMA_INVALID on any contract breach.</summary>
    public static Dictionary<string, object?> ValidateRequest(
        Dictionary<string, object?> call)
    {
        foreach (string key in RequiredRequest)
            if (!call.ContainsKey(key) || call[key] is null)
                throw new ExecutorError(
                    ConvErr.ToolSchemaInvalid, $"missing field: {key}");
        string Str(string k)
            => call[k]?.ToString() ?? "";
        if (Str("request_id").Length == 0 || Str("tool").Length == 0)
            throw new ExecutorError(
                ConvErr.ToolSchemaInvalid, "empty request_id/tool");
        if (Str("schema_version") != Format)
            throw new ExecutorError(
                ConvErr.ToolSchemaInvalid,
                $"schema_version must be {Format}");
        if (call["arguments"] is not Dictionary<string, object?>)
            throw new ExecutorError(
                ConvErr.ToolSchemaInvalid, "arguments must be object");
        if (call["requires_confirmation"] is not bool)
            throw new ExecutorError(
                ConvErr.ToolSchemaInvalid,
                "requires_confirmation must be bool");
        if (call["expected_result"] is not Dictionary<string, object?>)
            throw new ExecutorError(
                ConvErr.ToolSchemaInvalid,
                "expected_result must be an object shape hint");
        var norm = new Dictionary<string, object?>
        {
            ["schema_version"] = Format,
            ["request_id"] = Str("request_id"),
            ["tool"] = Str("tool"),
            ["arguments"] = call["arguments"],
            ["reason_code"] = Str("reason_code"),
            ["requires_confirmation"] = call["requires_confirmation"],
            ["expected_result"] = call["expected_result"],
        };
        return norm;
    }

    /// <summary>Validate a tool result; status is closed:
    /// OK|DENIED|ERROR|TIMEOUT|INVALID_RESULT.</summary>
    public static Dictionary<string, object?> ValidateResult(
        Dictionary<string, object?> result)
    {
        foreach (string key in RequiredResult)
            if (!result.ContainsKey(key))
                throw new ExecutorError(
                    ConvErr.ToolSchemaInvalid, $"result missing: {key}");
        string status = result["status"]?.ToString() ?? "";
        if (status is not ("OK" or "DENIED" or "ERROR" or "TIMEOUT"
                           or "INVALID_RESULT"))
            throw new ExecutorError(
                ConvErr.ToolSchemaInvalid,
                $"unknown tool status '{status}'");
        return new Dictionary<string, object?>
        {
            ["request_id"] = result["request_id"]?.ToString() ?? "",
            ["tool"] = result["tool"]?.ToString() ?? "",
            ["status"] = status,
            ["payload"] = result["payload"],
            ["evidence"] = result["evidence"],
            ["error"] = result["error"],
        };
    }
}

/// <summary>§16 star-grounded-result/v1 — every claim carries its
/// evidence chain and a closed support state.</summary>
internal static class GroundedResult
{
    public const string Format = "star-grounded-result/v1";
    public static readonly string[] SupportStates =
        { "SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONFLICT" };

    public static readonly string[] RequiredClaim =
        { "claim_id", "source_id", "document_id", "chunk_id",
          "revision", "retrieval_score", "rerank_score", "citation",
          "support_state" };

    public static Dictionary<string, object?> ValidateClaim(
        Dictionary<string, object?> claim)
    {
        foreach (string key in RequiredClaim)
            if (!claim.ContainsKey(key) || claim[key] is null)
                throw new ExecutorError(
                    ConvErr.GroundingUnsupportedClaim,
                    $"claim missing: {key}");
        string state = claim["support_state"]?.ToString() ?? "";
        if (!SupportStates.Contains(state))
            throw new ExecutorError(
                ConvErr.GroundingUnsupportedClaim,
                $"support_state '{state}'");
        var norm = new Dictionary<string, object?>(claim)
        {
            ["format"] = Format,
        };
        return norm;
    }

    /// <summary>Fail-closed aggregation: a result set containing any
    /// UNSUPPORTED/CONFLICT claim is not groundable as-is.</summary>
    public static Dictionary<string, object?> Aggregate(
        List<Dictionary<string, object?>> claims)
    {
        var checkedClaims = claims.Select(ValidateClaim).ToList();
        int unsupported = checkedClaims.Count(
            c => c["support_state"]?.ToString() is "UNSUPPORTED"
                                                  or "CONFLICT");
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["claims"] = checkedClaims.Cast<object?>().ToList(),
            ["claim_count"] = checkedClaims.Count,
            ["unsupported_count"] = unsupported,
            ["fully_grounded"] = unsupported == 0,
        };
    }
}

/// <summary>§18 structured output: parse -> schema validate -> optional
/// single repair -> revalidate. A parse failure can never surface as
/// success — the failure is typed and terminal.</summary>
internal static class StructuredOutputValidator
{
    public const string Format = "star-structured-output/v1";

    public sealed class Outcome
    {
        public bool Ok;
        public string Error = "";
        public JsonNode? Value;
        public bool Repaired;
    }

    /// <summary>Validate text against a JSON-schema-lite contract:
    /// the schema is a JSON object with optional "required" (string[])
    /// and "properties" (object of name -> {"type": ...}). A null schema
    /// accepts any parseable JSON value.</summary>
    public static Outcome Validate(string text, string? schemaJson,
                                   bool allowRepair = true)
    {
        JsonNode? value;
        try { value = JsonNode.Parse(text); }
        catch (JsonException)
        {
            if (!allowRepair)
                return Fail(ConvErr.StructuredParseFailed);
            string repaired = RepairOnce(text);
            if (repaired.Length == 0)
                return new Outcome
                {
                    Error = ConvErr.StructuredParseFailed,
                };
            try { value = JsonNode.Parse(repaired); }
            catch (JsonException)
            {
                return new Outcome
                {
                    Error = ConvErr.StructuredRepairFailed,
                    Repaired = false,
                };
            }
            var o2 = ValidateSchema(value!, schemaJson);
            o2.Repaired = true;
            o2.Value = value;
            return o2;
        }
        var o = ValidateSchema(value!, schemaJson);
        o.Value = value;
        return o;
    }

    private static Outcome ValidateSchema(JsonNode value,
                                          string? schemaJson)
    {
        if (string.IsNullOrWhiteSpace(schemaJson))
            return new Outcome { Ok = true };
        JsonNode schema;
        try { schema = JsonNode.Parse(schemaJson)!; }
        catch (JsonException)
        {
            return new Outcome { Error = ConvErr.StructuredSchemaFailed };
        }
        if (value is not JsonObject obj)
            return new Outcome { Error = ConvErr.StructuredSchemaFailed };
        if (schema["required"] is JsonArray req)
            foreach (var r in req)
            {
                string name = r?.GetValue<string>() ?? "";
                if (name.Length > 0 && !obj.ContainsKey(name))
                    return new Outcome
                    { Error = ConvErr.StructuredSchemaFailed };
            }
        if (schema["properties"] is JsonObject props)
            foreach (var p in props)
            {
                if (!obj.ContainsKey(p.Key)) continue;
                string want =
                    p.Value?["type"]?.GetValue<string>() ?? "";
                if (want.Length == 0) continue;
                if (!TypeMatches(obj[p.Key], want))
                    return new Outcome
                    { Error = ConvErr.StructuredSchemaFailed };
            }
        return new Outcome { Ok = true };
    }

    private static bool TypeMatches(JsonNode? v, string type)
    {
        if (v is null) return type == "null";
        return type switch
        {
            "object" => v is JsonObject,
            "array" => v is JsonArray,
            "string" => v is JsonValue &&
                        v.GetValueKind() == JsonValueKind.String,
            "number" => v is JsonValue &&
                        v.GetValueKind() == JsonValueKind.Number,
            "boolean" => v is JsonValue &&
                         v.GetValueKind() is JsonValueKind.True
                                              or JsonValueKind.False,
            "integer" => v is JsonValue &&
                         v.GetValueKind() == JsonValueKind.Number,
            "null" => v.GetValueKind() == JsonValueKind.Null,
            _ => true,   // unknown type names do not gate
        };
    }

    /// <summary>One bounded repair pass: trim to the outermost braces
    /// and re-quote trailing commas. Anything further is a typed
    /// failure, not a second guess.</summary>
    private static string RepairOnce(string text)
    {
        int open = text.IndexOf('{');
        int close = text.LastIndexOf('}');
        if (open < 0 || close <= open) return "";
        var span = text.AsSpan(open, close - open + 1);
        var sb = new System.Text.StringBuilder(span.Length);
        for (int i = 0; i < span.Length; i++)
        {
            char c = span[i];
            if (c == ',' && i + 1 < span.Length && span[i + 1] == '}')
                continue;               // drop trailing comma
            sb.Append(c);
        }
        return sb.ToString();
    }

    private static Outcome Fail(string code)
        => new() { Error = code };
}


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
                "TOOL_BUDGET_EXHAUSTED",
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
            return JsonDocument.Parse(File.ReadAllText(file))
                               .RootElement.Clone();
        }
        catch (JsonException e)
        {
            throw new ExecutorError(errCode, $"{file}: {e.Message}");
        }
    }

    // ----------------------------------- CLI-facing conveniences -----

    /// <summary>Validate a call or result document by path; ``kind`` is
    /// ``request`` (star-tool-call/v2) or ``result``
    /// (star-tool-result/v2).</summary>
    public static Dictionary<string, object?> ValidateCall(
        string file, string kind)
    {
        var el = ReadJson(file, "TOOL_SCHEMA_INVALID");
        return kind == "result" ? ValidateResult(el) : ValidateCall(el);
    }

    /// <summary>Per-request tool-need decision without a prepared call
    /// document: the allowlist still gates, and a request naming a
    /// non-allowlisted tool is denied rather than guessed.</summary>
    public static Dictionary<string, object?> Decide(
        string toolRoot, string tool, string requirement,
        string reason)
    {
        var profile = RuntimeCapabilities.Load(toolRoot);
        string decision;
        if (tool.Length == 0)
            decision = "TOOL_NOT_REQUIRED";
        else if (profile.ToolsAllowed.Length > 0 &&
                 !profile.ToolsAllowed.Contains(tool))
            decision = "TOOL_DENIED";
        else
            decision = requirement switch
            {
                "required" => "TOOL_REQUIRED",
                "denied" => "TOOL_DENIED",
                _ => "TOOL_OPTIONAL",
            };
        if (!Decisions.Contains(decision))
            throw new ExecutorError("TOOL_DECISION_INVALID", decision);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-tool-decision/v1",
            ["decision"] = decision,
            ["tool"] = tool,
            ["requirement"] = requirement,
            ["reason"] = reason,
            ["requires_confirmation"] =
                profile.ToolRequiresConfirmation &&
                decision == "TOOL_REQUIRED",
        };
    }

    /// <summary>Record the outcome of a gate decision in the metrics
    /// ledger and return the outcome payload.</summary>
    public static Dictionary<string, object?> RecordOutcome(
        string toolRoot, string decision, bool schemaValid,
        string status)
    {
        if (!schemaValid) RecordMetric(toolRoot, "invalid");
        else if (decision == "TOOL_NOT_REQUIRED")
            RecordMetric(toolRoot, "unnecessary");
        if (status.Length > 0 && decision != "TOOL_NOT_REQUIRED")
            RecordMetric(toolRoot,
                status == "ok" ? "succeeded" : "failed");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["decision"] = decision,
            ["schema_valid"] = schemaValid,
            ["status"] = status,
            ["recorded_at"] = XcPaths.IsoNow(),
        };
    }

    /// <summary>Metrics surface for the CLI (alias of Metrics).</summary>
    public static Dictionary<string, object?> MetricsPayload(
        string toolRoot)
        => Metrics(toolRoot);

    /// <summary>Validate a star-grounded-result/v1 file, then append it
    /// to the grounding ledger.</summary>
    public static Dictionary<string, object?> ValidateGrounded(
        string toolRoot, string file)
    {
        var el = ReadJson(file, "GROUNDING_UNSUPPORTED_CLAIM");
        var r = ValidateGrounded(el);
        try
        {
            string dir = Path.Combine(toolRoot, XcPaths.LogsRel);
            Directory.CreateDirectory(dir);
            File.AppendAllText(
                Path.Combine(dir, "grounded-results.jsonl"),
                CanonicalJson.Canonical(el) + "\n");
        }
        catch { /* ledger append is best-effort */ }
        return r;
    }
}
