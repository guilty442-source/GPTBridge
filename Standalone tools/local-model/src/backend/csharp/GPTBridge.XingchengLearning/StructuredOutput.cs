// StructuredOutput.cs — §18 star-structured-output/v1.
//
// Contract: model output -> parse -> schema validate -> (optional)
// repair once -> validate -> success/fail. A parse failure must never
// be reported as success; every failure path emits an explicit §36
// error and the failure record itself is valid evidence.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class StructuredOutput
{
    public const string Format = "star-structured-output/v1";

    /// <summary>Minimal schema subset: {"type":"object",
    /// "required":[...], "properties":{"k":{"type":"string|number|
    /// boolean|object|array"}}}. Unknown schema fields are ignored;
    /// type checking is exact, never coerced.</summary>
    public static Dictionary<string, object?> Validate(
        string rawOutput, JsonElement schema, bool repairOnce)
    {
        var record = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["repair_attempted"] = false,
        };

        JsonElement? parsed = TryParse(rawOutput);
        if (parsed is null && repairOnce)
        {
            // §18 SchemaRepair: one controlled repair, full evidence.
            record["repair_attempted"] = true;
            record["original_invalid_output"] =
                rawOutput.Length > 512
                    ? rawOutput[..512] + "…" : rawOutput;
            record["validation_error"] = "json_parse";
            record["repair_action"] = "extract_balanced_json";
            string? repaired = ExtractJson(rawOutput);
            if (repaired is not null)
                parsed = TryParse(repaired);
            else
            {
                record["ok"] = false;
                record["error"] = "STRUCTURED_REPAIR_FAILED";
                record["final_validation"] = "failed";
                return record;
            }
        }
        if (parsed is null)
        {
            record["ok"] = false;
            record["error"] = "STRUCTURED_PARSE_FAILED";
            return record;
        }

        var failures = CheckSchema(parsed.Value, schema, "");
        if (failures.Count > 0 && repairOnce &&
            !Convert.ToBoolean(record["repair_attempted"]))
        {
            // schema-level repair: fill missing required scalars once
            record["repair_attempted"] = true;
            record["original_invalid_output"] =
                rawOutput.Length > 512
                    ? rawOutput[..512] + "…" : rawOutput;
            record["validation_error"] =
                "schema:" + string.Join(",", failures);
            record["repair_action"] = "fill_required_scalars";
            parsed = RepairRequired(parsed.Value, schema);
            if (parsed is null)
            {
                record["ok"] = false;
                record["error"] = "STRUCTURED_REPAIR_FAILED";
                record["schema_failures"] = failures;
                record["final_validation"] = "failed";
                return record;
            }
            failures = CheckSchema(parsed.Value, schema, "");
        }
        if (failures.Count > 0)
        {
            record["ok"] = false;
            record["error"] = "STRUCTURED_SCHEMA_FAILED";
            record["schema_failures"] = failures;
            if (Convert.ToBoolean(record["repair_attempted"]))
                record["final_validation"] = "failed";
            return record;
        }
        if (Convert.ToBoolean(record["repair_attempted"]))
            record["final_validation"] = "passed";
        record["ok"] = true;
        record["value"] =
            ModelLifecycle.Decode(parsed.Value);
        return record;
    }

    private static JsonElement? TryParse(string text)
    {
        try
        {
            return JsonDocument.Parse(text).RootElement.Clone();
        }
        catch (JsonException) { return null; }
    }

    /// <summary>Repair-once: locate the outermost balanced JSON object
    /// or array inside noisy model text. Returns null when no balanced
    /// span exists.</summary>
    private static string? ExtractJson(string text)
    {
        int start = -1;
        char open = '{', close = '}';
        for (int i = 0; i < text.Length; i++)
            if (text[i] is '{' or '[')
            {
                start = i; open = text[i];
                close = open == '{' ? '}' : ']';
                break;
            }
        if (start < 0) return null;
        int depth = 0; bool inStr = false; bool esc = false;
        for (int i = start; i < text.Length; i++)
        {
            char c = text[i];
            if (inStr)
            {
                if (esc) esc = false;
                else if (c == '\\') esc = true;
                else if (c == '"') inStr = false;
                continue;
            }
            if (c == '"') inStr = true;
            else if (c == '{' || c == '[') ++depth;
            else if (c == '}' || c == ']')
            {
                --depth;
                if (depth == 0)
                    return text[start..(i + 1)];
            }
        }
        return null;
    }

    private static List<object?> CheckSchema(
        JsonElement v, JsonElement schema, string path)
    {
        var failures = new List<object?>();
        if (schema.ValueKind != JsonValueKind.Object) return failures;
        if (schema.TryGetProperty("type", out var t) &&
            t.ValueKind == JsonValueKind.String)
        {
            string want = t.GetString()!;
            bool okType = want switch
            {
                "object" => v.ValueKind == JsonValueKind.Object,
                "array" => v.ValueKind == JsonValueKind.Array,
                "string" => v.ValueKind == JsonValueKind.String,
                "number" => v.ValueKind == JsonValueKind.Number,
                "integer" => v.ValueKind == JsonValueKind.Number &&
                             v.TryGetInt64(out _),
                "boolean" => v.ValueKind is JsonValueKind.True or
                             JsonValueKind.False,
                "null" => v.ValueKind == JsonValueKind.Null,
                _ => true,
            };
            if (!okType)
            {
                failures.Add(new Dictionary<string, object?>
                {
                    ["path"] = path.Length > 0 ? path : "$",
                    ["error"] = $"expected type {want}, " +
                                $"got {v.ValueKind}",
                });
                return failures; // deeper checks meaningless
            }
        }
        if (v.ValueKind == JsonValueKind.Object)
        {
            if (schema.TryGetProperty("required", out var req) &&
                req.ValueKind == JsonValueKind.Array)
                foreach (var r in req.EnumerateArray())
                {
                    string name = r.GetString() ?? "";
                    if (!v.TryGetProperty(name, out _))
                        failures.Add(new Dictionary<string, object?>
                        {
                            ["path"] = path + "/" + name,
                            ["error"] = "missing required field",
                        });
                }
            if (schema.TryGetProperty("properties", out var props) &&
                props.ValueKind == JsonValueKind.Object)
                foreach (var p in props.EnumerateObject())
                    if (v.TryGetProperty(p.Name, out var sub))
                        failures.AddRange(CheckSchema(
                            sub, p.Value, path + "/" + p.Name));
        }
        return failures;
    }

    /// <summary>Insert null placeholders for missing required fields —
    /// a schema can then accept or still reject (null fails the type
    /// check unless the field is nullable, which is honest).</summary>
    /// <summary>CLI-facing: validate an output file against a schema
    /// file — parse -> schema -> single repair -> validate.</summary>
    public static Dictionary<string, object?> Validate(
        string toolRoot, string outputFile, string schemaFile)
    {
        if (string.IsNullOrEmpty(outputFile) ||
            !File.Exists(outputFile))
            throw new ExecutorError("STRUCTURED_PARSE_FAILED",
                $"{outputFile}: output file missing");
        var schema = ToolContracts.ReadJson(
            schemaFile, "STRUCTURED_SCHEMA_FAILED");
        var r = Validate(File.ReadAllText(outputFile), schema,
                         repairOnce: true);
        r["file"] = outputFile;
        return r;
    }

    private static JsonElement? RepairRequired(
        JsonElement v, JsonElement schema)
    {
        if (v.ValueKind != JsonValueKind.Object ||
            schema.ValueKind != JsonValueKind.Object ||
            !schema.TryGetProperty("required", out var req) ||
            req.ValueKind != JsonValueKind.Array)
            return null;
        var rebuilt = new Dictionary<string, object?>();
        foreach (var p in v.EnumerateObject())
            rebuilt[p.Name] = ModelLifecycle.Decode(p.Value);
        bool changed = false;
        foreach (var r in req.EnumerateArray())
        {
            string name = r.GetString() ?? "";
            if (name.Length > 0 && !rebuilt.ContainsKey(name))
            {
                rebuilt[name] = null;
                changed = true;
            }
        }
        if (!changed) return null;
        try
        {
            return JsonDocument.Parse(
                CanonicalJson.PrettyDict(rebuilt)).RootElement.Clone();
        }
        catch (JsonException) { return null; }
    }
}
