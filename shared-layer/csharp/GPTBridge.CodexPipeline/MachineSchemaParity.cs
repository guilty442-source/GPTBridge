using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Machine-schema parity probe — C# reimplementation of the retired
/// ``governance_rule/execution/semantic_hash_toolchain.py`` (retired
/// with the Python lane; recovered from git history).
///
/// Recipe: SEAL_CANONICAL_V1 (same serialization family as
/// codex_amendment_contract.content_hash) — UTF-8, NFC, lexicographic
/// key order, compact separators, Python ``json.dumps``-compatible
/// string escaping (ensure_ascii), SHA-256.
///
/// Three layers per registered machine schema:
///   producer    — semantic hash of the canonical descriptor
///                 projection (DESCRIPTOR_FIELDS)
///   validator   — independent re-derivation of the same row
///                 (must equal the producer bit-for-bit)
///   persistence — comparison against the persisted
///                 ``canonical_semantic_hash`` / ``content_hash``
///
/// CANONICAL_MISMATCH is evidence, not a pass — the stored canonical
/// values were stamped by governor-side tooling whose descriptor
/// projection is not derivable.  Re-stamping is a governor action;
/// this probe never writes parity state.
/// </summary>
internal static class MachineSchemaParity
{
    public const string Recipe = "SEAL_CANONICAL_V1";

    /// <summary>Semantic contract fields — the complete set defining
    /// what a schema IS.  Registry bookkeeping (version_identity,
    /// status, parity_status, content_hash) is excluded.</summary>
    internal static readonly string[] DescriptorFields =
    {
        "schema_code", "schema_kind", "semantic_owner",
        "required_fields", "optional_fields", "compatibility_rule",
        "persistence_class", "redaction_rule", "field_types",
        "nullability", "schema_version", "unknown_fields_policy",
        "enum_constraints", "range_constraints",
        "successor_schema_code",
    };

    private static readonly HashSet<string> EmbeddedJson = new(
        StringComparer.Ordinal)
    {
        "required_fields", "optional_fields", "field_types",
        "nullability", "enum_constraints", "range_constraints",
    };

    // ------------------------------------------------------------------
    // canonical JSON writer — byte-identical to Python
    // ``json.dumps(obj, sort_keys=True, separators=(",",":"))``
    // (ensure_ascii default: non-ASCII escaped as \uXXXX lowercase hex,
    // surrogate pairs for non-BMP).
    // ------------------------------------------------------------------

    private static void WriteJsonString(StringBuilder sb, string value)
    {
        sb.Append('"');
        foreach (var c in value.Normalize(NormalizationForm.FormC))
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    // Python ensure_ascii escapes c < 0x20 as \u00xx and
                    // every non-ASCII char as \uXXXX (surrogate pairs
                    // above the BMP — foreach already yields them as
                    // separate chars, each below 0x10000).
                    if (c < ' ' || c > 0x7E)
                        sb.Append("\\u")
                            .Append(((int)c).ToString("x4",
                                CultureInfo.InvariantCulture));
                    else
                        sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }

    private static void WriteCanonical(StringBuilder sb, object? value)
    {
        switch (value)
        {
            case null:
                sb.Append("null"); break;
            case string s:
                WriteJsonString(sb, s); break;
            case bool b:
                sb.Append(b ? "true" : "false"); break;
            case List<object?> list:
                sb.Append('[');
                for (var i = 0; i < list.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    WriteCanonical(sb, list[i]);
                }
                sb.Append(']'); break;
            case SortedDictionary<string, object?> dict:
                sb.Append('{');
                var first = true;
                foreach (var (k, v) in dict)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    WriteJsonString(sb, k);
                    sb.Append(':');
                    WriteCanonical(sb, v);
                }
                sb.Append('}'); break;
            case long or int or short or byte:
                sb.Append(Convert.ToString(value,
                    CultureInfo.InvariantCulture)); break;
            case double d:
                // Python json.dumps float: integral floats keep ".0",
                // others use repr() shortest round-trip.
                sb.Append(double.IsInteger(d)
                    ? d.ToString("0.0", CultureInfo.InvariantCulture)
                    : d.ToString("G17",
                        CultureInfo.InvariantCulture));
                break;
            default:
                // Python default=str fallback
                WriteJsonString(sb,
                    Convert.ToString(value,
                        CultureInfo.InvariantCulture) ?? "");
                break;
        }
    }

    public static string SealHash(SortedDictionary<string, object?> d)
    {
        var sb = new StringBuilder();
        WriteCanonical(sb, d);
        return Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(sb.ToString())))
            .ToLowerInvariant();
    }

    /// <summary>Canonical serialization of one descriptor — diagnostic
    /// surface for recipe verification (``--parity-descriptor``).</summary>
    internal static string CanonicalDescriptorJson(
        Dictionary<string, object?> row)
    {
        var sb = new StringBuilder();
        WriteCanonical(sb, BuildDescriptor(row));
        return sb.ToString();
    }

    /// <summary>Parse an embedded field: JSON when the text is valid
    /// JSON, else ``|``-split when it contains a bar, else the raw
    /// string (mirrors the retired toolchain's ``_parse_embedded``).</summary>
    private static object? ParseEmbedded(string? value)
    {
        if (value is null) return null;
        try
        {
            return FromJsonElement(
                JsonDocument.Parse(value).RootElement);
        }
        catch (JsonException)
        {
            return value.Contains('|')
                ? value.Split('|').Cast<object?>().ToList()
                : (object?)value;
        }
    }

    private static object? FromJsonElement(JsonElement e) =>
        e.ValueKind switch
        {
            JsonValueKind.Object => ToSorted(e),
            JsonValueKind.Array => e.EnumerateArray()
                .Select(FromJsonElement).Cast<object?>().ToList(),
            JsonValueKind.String => e.GetString(),
            JsonValueKind.Number =>
                e.TryGetInt64(out long i)
                    ? i
                    : (object)e.GetDouble(),
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => null,
        };

    private static SortedDictionary<string, object?> ToSorted(
        JsonElement e)
    {
        var dict = new SortedDictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var p in e.EnumerateObject())
            dict[p.Name] = FromJsonElement(p.Value);
        return dict;
    }

    /// <summary>Project a registry row to its canonical descriptor —
    /// the single definition both layers use so the field set cannot
    /// drift.</summary>
    private static SortedDictionary<string, object?> BuildDescriptor(
        Dictionary<string, object?> row)
    {
        var d = new SortedDictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var field in DescriptorFields)
        {
            row.TryGetValue(field, out var raw);
            var s = raw as string;
            d[field] = EmbeddedJson.Contains(field)
                ? ParseEmbedded(s)
                : s;
        }
        return d;
    }

    /// <summary>Single-row descriptor + hash — used by
    /// ``--parity-descriptor &lt;code&gt;`` for recipe debugging.</summary>
    public static Dictionary<string, object?> Describe(string code)
    {
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT r.*, p.canonical_semantic_hash "
            + $"FROM {PgDsn.CodexSchema}.machine_schema_registry r "
            + $"JOIN {PgDsn.CodexSchema}.machine_schema_parity_evidence p"
            + "  ON p.schema_code = r.schema_code "
            + "WHERE r.schema_code = @c", connection);
        command.Parameters.AddWithValue("c", code);
        using var reader = command.ExecuteReader();
        if (!reader.Read())
            return new Dictionary<string, object?>
            {
                ["error"] = "SCHEMA_CODE_UNKNOWN", ["code"] = code,
            };
        var row = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        for (var i = 0; i < reader.FieldCount; i++)
            row[reader.GetName(i)] =
                reader.IsDBNull(i) ? null : reader.GetValue(i);
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["schema_code"] = code,
            ["recipe"] = Recipe,
            ["descriptor"] = CanonicalDescriptorJson(row),
            ["producer_semantic_hash"] =
                SealHash(BuildDescriptor(row)),
            ["canonical_semantic_hash"] =
                row["canonical_semantic_hash"],
        };
    }

    public static Dictionary<string, object?> Probe()
    {
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT r.*, p.canonical_semantic_hash "
            + $"FROM {PgDsn.CodexSchema}.machine_schema_registry r "
            + $"JOIN {PgDsn.CodexSchema}.machine_schema_parity_evidence p"
            + "  ON p.schema_code = r.schema_code ORDER BY r.schema_code",
            connection);
        var rows = new List<Dictionary<string, object?>>();
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read())
            {
                var row = new Dictionary<string, object?>(
                    StringComparer.Ordinal);
                for (var i = 0; i < reader.FieldCount; i++)
                    row[reader.GetName(i)] =
                        reader.IsDBNull(i) ? null : reader.GetValue(i);
                rows.Add(row);
            }
        }

        var results = new List<Dictionary<string, object?>>();
        foreach (var row in rows)
        {
            var canonical =
                row["canonical_semantic_hash"] as string;
            var storedContent = row["content_hash"] as string;
            var producer = SealHash(BuildDescriptor(row));
            var validator = SealHash(BuildDescriptor(
                new Dictionary<string, object?>(row,
                    StringComparer.Ordinal)));
            results.Add(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["schema_code"] = row["schema_code"],
                ["semantic_owner"] = row.GetValueOrDefault(
                    "semantic_owner"),
                ["parity_status"] = row.GetValueOrDefault(
                    "parity_status"),
                ["producer_semantic_hash"] = producer,
                ["validator_semantic_hash"] = validator,
                ["canonical_semantic_hash"] = canonical,
                ["stored_content_hash"] = storedContent,
                ["producer_validator_layer"] =
                    producer == validator ? "PASS" : "FAIL",
                ["persistence_layer"] =
                    producer == canonical || producer == storedContent
                        ? "MATCH" : "CANONICAL_MISMATCH",
            });
        }

        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["artifact"] = "machine-schema-parity-probe/v1",
            ["recipe"] = Recipe,
            ["descriptor_fields"] = DescriptorFields,
            ["rows"] = results.Count,
            ["producer_validator_pass"] = results.Count(r =>
                (string)r["producer_validator_layer"]! == "PASS"),
            ["canonical_match"] = results.Count(r =>
                (string)r["persistence_layer"]! == "MATCH"),
            ["note"] = "CANONICAL_MISMATCH is evidence, not a pass; "
                + "restamp or descriptor-projection publication is a "
                + "governor action. This probe never writes parity "
                + "state.",
            ["results"] = results,
        };
    }
}
