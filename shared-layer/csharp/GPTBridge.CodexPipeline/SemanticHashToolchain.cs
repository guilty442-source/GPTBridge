using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Machine-schema semantic-hash toolchain — OBL_MACHINE_SCHEMA_PARITY.
///
/// Faithful C# port of the retired Python lane module
/// ``governance_rule/execution/semantic_hash_toolchain.py``
/// (added in ef0707606, 2026-09-24; deleted with the Python lane in
/// 675fa045b, 2026-09-29 — recovered from git history at 675fa045b^).
///
/// This class is <b>pure</b>: no database access, no writes, no
/// parity-state mutation.  Callers supply a registry row as a plain
/// string-keyed dictionary (e.g. columns of one
/// ``machine_schema_registry`` row read by a governed reader) and get
/// the canonical descriptor, its SEAL_CANONICAL_V1 serialization and
/// the SHA-256 semantic hash back.
///
/// Serialization recipe: SEAL_CANONICAL_V1 (seal_canonicalization_spec)
/// — UTF-8, NFC, lexicographic key order, compact JSON separators,
/// SHA-256.  This is the same recipe family as
/// ``codex_amendment_contract.content_hash``, except that the Python
/// oracle used ``json.dumps`` defaults: ``ensure_ascii=True`` (every
/// non-ASCII character emitted as ``\uXXXX``, surrogate pairs above the
/// BMP) — reproduced byte-exactly here.  ``CanonJson`` must NOT be used
/// for this hash: it serializes with ``ensure_ascii=False``.
///
/// Staged restamp proposal
/// (governance_rule/execution/audit/convergence/
///  codex-amendment-proposal-machine-schema-parity-restamp-20260924.staged.json)
/// leaves two resolutions open — both stay extractable from this class:
///
///   OPTION A — the governor publishes the canonical descriptor
///              projection; the toolchain re-verifies persisted
///              ``canonical_semantic_hash`` values against it.
///              Surface: <see cref="BuildDescriptor"/> /
///              <see cref="CanonicalJson"/> expose the descriptor and
///              its canonical text for comparison against the published
///              projection; <see cref="EvaluateRow"/> compares the
///              producer hash against a supplied canonical value.
///
///   OPTION B — the governor authorizes re-stamp:
///              ``canonical_semantic_hash := producer_semantic_hash``
///              under this documented DESCRIPTOR_FIELDS projection for
///              all rows in a single governed transaction.
///              Surface: <see cref="ComputeProducerHash"/> is exactly
///              the value a governed restamp would persist.
///
/// The governor has not picked a recipe; nothing here is wired into
/// ``GenerationProjections.RebuildSchemaParityStatus``.
/// </summary>
public static class SemanticHashToolchain
{
    /// <summary>Serialization recipe identifier (emitted in evidence).</summary>
    public const string Recipe = "SEAL_CANONICAL_V1";

    /// <summary>Staged proposal option A — verify persisted canonical
    /// hashes against a governor-published descriptor projection.</summary>
    public const string RecipeOptionA = "verify-published-projection";

    /// <summary>Staged proposal option B — re-stamp canonical_semantic_hash
    /// to the producer hash under this descriptor projection.</summary>
    public const string RecipeOptionB = "restamp-to-producer-hash";

    /// <summary>Semantic contract fields — the complete set that defines
    /// what a schema IS.  Registry bookkeeping (version_identity, status,
    /// parity_status, content_hash itself) is excluded.  Order is the
    /// projection order (serialization sorts keys independently).</summary>
    public static readonly string[] DescriptorFields =
    {
        "schema_code",
        "schema_kind",
        "semantic_owner",
        "required_fields",
        "optional_fields",
        "compatibility_rule",
        "persistence_class",
        "redaction_rule",
        "field_types",
        "nullability",
        "schema_version",
        "unknown_fields_policy",
        "enum_constraints",
        "range_constraints",
        "successor_schema_code",
    };

    /// <summary>Descriptor fields whose registry column may carry the
    /// value embedded as JSON text (or ``|``-joined fallback text).</summary>
    public static readonly string[] EmbeddedJsonFields =
    {
        "required_fields", "optional_fields", "field_types",
        "nullability", "enum_constraints", "range_constraints",
    };

    private static readonly HashSet<string> EmbeddedJson = new(
        EmbeddedJsonFields, StringComparer.Ordinal);

    // ------------------------------------------------------------------
    // descriptor projection (Python: build_descriptor)
    // ------------------------------------------------------------------

    /// <summary>Project a ``machine_schema_registry`` row to its
    /// canonical descriptor — the single definition both producer and
    /// validator layers use so the field set cannot drift.
    /// Equivalent to Python ``_nfc(_parse_embedded(row))`` restricted to
    /// <see cref="DescriptorFields"/>; absent fields project to null.</summary>
    public static Dictionary<string, object?> BuildDescriptor(
        IReadOnlyDictionary<string, object?> row)
    {
        var descriptor = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var field in DescriptorFields)
        {
            row.TryGetValue(field, out var raw);
            descriptor[field] = Nfc(
                EmbeddedJson.Contains(field)
                    ? ParseEmbedded(raw)
                    : RawValue(raw));
        }
        return descriptor;
    }

    /// <summary>Python ``_parse_embedded`` + ``isinstance(v, str)`` gate:
    /// string values are JSON-decoded (``json.loads``); on decode failure
    /// they split on ``|`` when a bar is present, else stay raw strings.
    /// Non-string values (driver-delivered jsonb objects, numbers, bools,
    /// null) pass through untouched.</summary>
    private static object? ParseEmbedded(object? raw)
    {
        if (raw is not string text)
            return RawValue(raw);
        try
        {
            return FromJsonElement(JsonDocument.Parse(text).RootElement);
        }
        catch (JsonException)
        {
            // Python json.loads also accepts the non-strict literals
            // NaN/Infinity/-Infinity, which System.Text.Json rejects.
            var trimmed = text.Trim();
            if (trimmed == "NaN") return double.NaN;
            if (trimmed == "Infinity") return double.PositiveInfinity;
            if (trimmed == "-Infinity") return double.NegativeInfinity;
            return text.Contains('|')
                ? text.Split('|').Cast<object?>().ToList()
                : text;
        }
    }

    /// <summary>Normalize a raw column value: ``JsonElement`` (driver
    /// materialized jsonb) becomes the plain CLR object model; anything
    /// else passes through — matching psycopg2 delivering already-parsed
    /// JSON objects for jsonb columns.</summary>
    private static object? RawValue(object? raw) =>
        raw is JsonElement element ? FromJsonElement(element) : raw;

    private static object? FromJsonElement(JsonElement e) =>
        e.ValueKind switch
        {
            JsonValueKind.Object => e.EnumerateObject()
                .ToDictionary(p => p.Name,
                    p => FromJsonElement(p.Value), StringComparer.Ordinal),
            JsonValueKind.Array => e.EnumerateArray()
                .Select(FromJsonElement).Cast<object?>().ToList(),
            JsonValueKind.String => e.GetString(),
            JsonValueKind.Number =>
                // NB: without the cast the ternary's common type is
                // double and integral values would serialize as "0.0"
                // — Python json.loads keeps them ints ("0").
                e.TryGetInt64(out var i) ? i : (object)e.GetDouble(),
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => null,
        };

    /// <summary>Python ``_nfc``: strings NFC-normalize; the transform
    /// recurses through list items and mapping <b>values</b> — mapping
    /// keys are intentionally left unnormalized, exactly as the retired
    /// oracle did (``{k: _nfc(v) for k, v in value.items()}``).</summary>
    private static object? Nfc(object? value) => value switch
    {
        string s => s.Normalize(NormalizationForm.FormC),
        List<object?> list => list.Select(Nfc).ToList(),
        IDictionary<string, object?> map => map.ToDictionary(
            kv => kv.Key, kv => Nfc(kv.Value), StringComparer.Ordinal),
        _ => value,
    };

    // ------------------------------------------------------------------
    // canonical serialization + hash (Python: seal_hash)
    // ------------------------------------------------------------------

    /// <summary>SEAL_CANONICAL_V1 canonical text of an arbitrary plain
    /// object model — byte-identical to Python
    /// ``json.dumps(obj, sort_keys=True, separators=(",", ":"),
    /// default=str)`` including ``ensure_ascii`` escaping.</summary>
    public static string CanonicalJson(object? value)
    {
        var builder = new StringBuilder(256);
        WriteCanonical(builder, value);
        return builder.ToString();
    }

    /// <summary>Canonical descriptor text — diagnostic surface for
    /// recipe verification against a governor-published projection
    /// (option A).</summary>
    public static string CanonicalDescriptorJson(
        IReadOnlyDictionary<string, object?> row) =>
        CanonicalJson(BuildDescriptor(row));

    /// <summary>Python ``seal_hash``: canonical serialization, UTF-8,
    /// SHA-256, lowercase hex digest.</summary>
    public static string SealHash(object? descriptor) =>
        Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(CanonicalJson(descriptor))))
            .ToLowerInvariant();

    /// <summary>Producer semantic hash for one registry row —
    /// ``SealHash(BuildDescriptor(row))``.  Under restamp option B this
    /// is the value a governed transaction would persist to
    /// ``canonical_semantic_hash``.</summary>
    public static string ComputeProducerHash(
        IReadOnlyDictionary<string, object?> row) =>
        SealHash(BuildDescriptor(row));

    /// <summary>Three-layer pure evaluation of one registry row,
    /// mirroring the retired toolchain's per-row evidence record:
    ///   producer    — semantic hash of the canonical descriptor
    ///   validator   — independent re-derivation from a fresh copy of
    ///                 the same row (must equal producer bit-for-bit)
    ///   persistence — producer hash vs the supplied persisted
    ///                 ``canonical_semantic_hash`` / ``content_hash``
    /// CANONICAL_MISMATCH is evidence, not a pass.</summary>
    public static Dictionary<string, object?> EvaluateRow(
        IReadOnlyDictionary<string, object?> row,
        string? canonicalSemanticHash = null,
        string? storedContentHash = null)
    {
        var producer = ComputeProducerHash(row);
        var validator = ComputeProducerHash(
            new Dictionary<string, object?>(row, StringComparer.Ordinal));
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["schema_code"] = row.TryGetValue("schema_code", out var sc)
                ? sc : null,
            ["semantic_owner"] = row.TryGetValue("semantic_owner",
                out var so) ? so : null,
            ["parity_status"] = row.TryGetValue("parity_status",
                out var ps) ? ps : null,
            ["producer_semantic_hash"] = producer,
            ["validator_semantic_hash"] = validator,
            ["canonical_semantic_hash"] = canonicalSemanticHash,
            ["stored_content_hash"] = storedContentHash,
            ["producer_validator_layer"] =
                producer == validator ? "PASS" : "FAIL",
            ["persistence_layer"] =
                producer == (canonicalSemanticHash ?? storedContentHash)
                    ? "MATCH" : "CANONICAL_MISMATCH",
        };
    }

    // ------------------------------------------------------------------
    // canonical JSON writer — byte-identical to Python json.dumps
    // (ensure_ascii=True, sort_keys, compact separators, default=str)
    // ------------------------------------------------------------------

    private static void WriteCanonical(StringBuilder sb, object? value)
    {
        switch (value)
        {
            case null:
                sb.Append("null");
                break;
            case string text:
                WriteJsonString(sb, text);
                break;
            case bool b:
                sb.Append(b ? "true" : "false");
                break;
            case byte or sbyte or short or ushort or int or uint or long:
                sb.Append(Convert.ToInt64(value,
                    CultureInfo.InvariantCulture));
                break;
            case ulong big:
                sb.Append(big.ToString(CultureInfo.InvariantCulture));
                break;
            case float f:
                WritePythonFloat(sb, f);
                break;
            case double d:
                WritePythonFloat(sb, d);
                break;
            case IDictionary<string, object?> map:
                WriteObject(sb, map.Select(
                    kv => new KeyValuePair<string, object?>(
                        kv.Key, kv.Value)));
                break;
            case System.Collections.IDictionary map:
                // Python ``default=str`` also applies to non-string
                // mapping keys (str(key)); sort happens at dump time.
                WriteObject(sb, map.Cast<System.Collections.DictionaryEntry>()
                    .Select(e => new KeyValuePair<string, object?>(
                        CanonJson.PyStr(e.Key), e.Value)));
                break;
            case System.Collections.IEnumerable items:
                sb.Append('[');
                var firstItem = true;
                foreach (var item in items)
                {
                    if (!firstItem) sb.Append(',');
                    firstItem = false;
                    WriteCanonical(sb, item);
                }
                sb.Append(']');
                break;
            default:
                // Python default=str: any non-JSON-native type (decimal,
                // DateTime, …) serializes as the quoted str() of it.
                WriteJsonString(sb, CanonJson.PyStr(value));
                break;
        }
    }

    private static void WriteObject(StringBuilder sb,
        IEnumerable<KeyValuePair<string, object?>> pairs)
    {
        // Python sort_keys orders by Unicode code point; ordinal UTF-16
        // ordering would misrank non-BMP keys against U+E000–U+FFFF keys.
        var sorted = pairs.OrderBy(kv => kv.Key,
            PythonKeyOrder.Instance);
        sb.Append('{');
        var first = true;
        foreach (var (key, val) in sorted)
        {
            if (!first) sb.Append(',');
            first = false;
            WriteJsonString(sb, key);
            sb.Append(':');
            WriteCanonical(sb, val);
        }
        sb.Append('}');
    }

    /// <summary>Python ``ensure_ascii`` string escaping: the C0 controls
    /// use their short escapes where defined, every other character
    /// outside U+0020–U+007E becomes ``\uXXXX`` (lowercase hex), and
    /// non-BMP characters emit as UTF-16 surrogate-pair escapes exactly
    /// like CPython.</summary>
    private static void WriteJsonString(StringBuilder sb, string value)
    {
        sb.Append('"');
        foreach (var c in value)
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

    /// <summary>Python ``repr(float)`` as emitted by json.dumps:
    /// shortest round-trip, integral floats keep ".0", exponents render
    /// "e+NN"/"e-NN", non-finite values as Python literals.</summary>
    private static void WritePythonFloat(StringBuilder sb, double value)
    {
        if (double.IsNaN(value)) { sb.Append("NaN"); return; }
        if (double.IsPositiveInfinity(value))
        {
            sb.Append("Infinity");
            return;
        }
        if (double.IsNegativeInfinity(value))
        {
            sb.Append("-Infinity");
            return;
        }
        var text = value.ToString("R", CultureInfo.InvariantCulture);
        var negative = text.StartsWith('-');
        if (negative) text = text[1..];
        var parts = text.Split('E');
        var shift = parts.Length == 2
            ? int.Parse(parts[1], CultureInfo.InvariantCulture) : 0;
        var dot = parts[0].IndexOf('.');
        var decimalPoint = (dot < 0 ? parts[0].Length : dot) + shift;
        var digits = parts[0].Replace(".", "");
        var leading = digits.Length - digits.TrimStart('0').Length;
        decimalPoint -= leading;
        digits = digits.TrimStart('0').TrimEnd('0');
        if (negative) sb.Append('-');
        if (digits.Length == 0) { sb.Append("0.0"); return; }
        var exponent = decimalPoint - 1;
        // CPython repr switches notation at 1e-4 and 1e16; .NET R
        // has different thresholds even when the significant digits agree.
        if (exponent < -4 || exponent >= 16)
        {
            sb.Append(digits[0]);
            if (digits.Length > 1) sb.Append('.').Append(digits[1..]);
            sb.Append('e').Append(exponent < 0 ? "-" : "+")
                .Append(Math.Abs(exponent).ToString("D2", CultureInfo.InvariantCulture));
        }
        else if (decimalPoint <= 0)
            sb.Append("0.").Append('0', -decimalPoint).Append(digits);
        else if (decimalPoint >= digits.Length)
            sb.Append(digits).Append('0', decimalPoint - digits.Length).Append(".0");
        else
            sb.Append(digits[..decimalPoint]).Append('.').Append(digits[decimalPoint..]);
    }

    /// <summary>Python ``str`` comparison ordering for ``sort_keys`` —
    /// code-point order, not UTF-16 code-unit order.</summary>
    private sealed class PythonKeyOrder : IComparer<string>
    {
        public static readonly PythonKeyOrder Instance = new();

        public int Compare(string? x, string? y)
        {
            var a = x!.EnumerateRunes();
            var b = y!.EnumerateRunes();
            while (true)
            {
                var hasA = a.MoveNext();
                var hasB = b.MoveNext();
                if (!hasA) return hasB ? -1 : 0;
                if (!hasB) return 1;
                var diff = a.Current.Value - b.Current.Value;
                if (diff != 0) return diff;
            }
        }
    }
}
