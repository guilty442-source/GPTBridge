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
