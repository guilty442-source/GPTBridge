using System.Globalization;
using System.Text;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Canonical JSON identical to the Python oracle contract
/// (codex_amendment_contract.canonical_json):
///
///   json.dumps(_json_safe(v), ensure_ascii=False, sort_keys=True,
///              separators=(",", ":"), default=str)
///
/// then SHA-256 over the NFC-normalized UTF-8 text.  Every hash in the
/// amendment pipeline (content / identity / full roots, revision entries,
/// certificates) derives from this serialization — byte parity is the
/// contract, so the writer reproduces CPython's formatting exactly:
/// shortest-round-trip floats, integral floats keep ".0", exponents as
/// "e+NN"/"e-NN", raw (unescaped) non-ASCII, hex for byte payloads.
/// </summary>
internal static class CanonJson
{
    /// <summary>Serialize like Python canonical_json().</summary>
    public static string Serialize(object? payload) =>
        Write(ToJsonSafe(payload));

    /// <summary>Python ``_json_safe``: bytes→hex, tuples→lists, dict
    /// keys→str, everything else passes through to ``default=str``
    /// semantics at write time.</summary>
    public static object? ToJsonSafe(object? value)
    {
        switch (value)
        {
            case null:
            case string:
            case bool:
                return value;
            case byte or sbyte or short or ushort or int or uint
                or long or ulong:
                return value;
            case float or double or decimal:
                return value;
            case byte[] bytes:
                return Convert.ToHexString(bytes).ToLowerInvariant();
            case System.Collections.IDictionary map:
                {
                    var result = new SortedDictionary<string, object?>(
                        StringComparer.Ordinal);
                    foreach (System.Collections.DictionaryEntry entry in map)
                        result[PyStr(entry.Key)] = ToJsonSafe(entry.Value);
                    return result;
                }
            case System.Collections.IEnumerable items:
                {
                    var list = new List<object?>();
                    foreach (var item in items)
                        list.Add(ToJsonSafe(item));
                    return list;
                }
            default:
                return PyStr(value);
        }
    }

    /// <summary>Python ``str(value)`` for the default=str fallback and
    /// non-string mapping keys.</summary>
    public static string PyStr(object? value) => value switch
    {
        null => "None",
        bool b => b ? "True" : "False",
        DateTime dt => PyDateTime(dt),
        DateTimeOffset dto => PyDateTime(dto.UtcDateTime),
        byte[] bytes => Convert.ToHexString(bytes).ToLowerInvariant(),
        IFormattable f => f.ToString(null, CultureInfo.InvariantCulture),
        _ => value.ToString() ?? "",
    };

    /// <summary>Python str(datetime): "YYYY-MM-DD HH:MM:SS[.ffffff][+HH:MM]".</summary>
    private static string PyDateTime(DateTime dt)
    {
        var text = dt.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
        var micros = (dt.Ticks % TimeSpan.TicksPerSecond) / 10;
        if (micros != 0)
            text += $".{micros:D6}";
        return text;
    }

    // -- writer ------------------------------------------------------------

    public static string Write(object? safe) => Write(safe, spaced: false);

    /// <summary>``spaced=true`` reproduces json.dumps default separators
    /// (``", "`` / ``": "``) — the audit gate's ``_canonical_hash``
    /// deliberately uses spaced separators, distinct from the compact
    /// ``canonical_json`` convention.</summary>
    public static string Write(object? safe, bool spaced)
    {
        var builder = new StringBuilder(256);
        WriteValue(builder, safe, spaced);
        return builder.ToString();
    }

    /// <summary>json.dumps(spaced separators) over the plain object
    /// model — for payloads already in CLR form.</summary>
    public static string SerializeSpaced(object? payload) =>
        Write(ToJsonSafe(payload), spaced: true);

    private static void WriteValue(StringBuilder builder, object? value,
        bool spaced = false)
    {
        switch (value)
        {
            case null:
                builder.Append("null");
                return;
            case bool b:
                builder.Append(b ? "true" : "false");
                return;
            case string text:
                WriteString(builder, text);
                return;
            case byte or sbyte or short or ushort or int or uint or long:
                builder.Append(Convert.ToInt64(value,
                    CultureInfo.InvariantCulture));
                return;
            case ulong big:
                builder.Append(big.ToString(CultureInfo.InvariantCulture));
                return;
            case float f:
                WriteFloat(builder, f);
                return;
            case double d:
                WriteFloat(builder, d);
                return;
            case decimal m:
                // JSON big integers land here (decimal, not long):
                // integral decimals print without a fraction, matching
                // Python int rendering; fractional decimals render
                // shortest-form like Python str(float).
                builder.Append(m.ToString(CultureInfo.InvariantCulture));
                return;
            case IDictionary<string, object?> map:
                {
                    builder.Append('{');
                    var first = true;
                    foreach (var pair in map)
                    {
                        if (!first)
                            builder.Append(spaced ? ", " : ",");
                        first = false;
                        WriteString(builder, pair.Key);
                        builder.Append(spaced ? ": " : ":");
                        WriteValue(builder, pair.Value, spaced);
                    }
                    builder.Append('}');
                    return;
                }
            case System.Collections.Generic.IEnumerable<object?> list:
                {
                    builder.Append('[');
                    var first = true;
                    foreach (var item in list)
                    {
                        if (!first)
                            builder.Append(spaced ? ", " : ",");
                        first = false;
                        WriteValue(builder, item, spaced);
                    }
                    builder.Append(']');
                    return;
                }
            default:
                WriteValue(builder, ToJsonSafe(value), spaced);
                return;
        }
    }

    private static void WriteString(StringBuilder builder, string text)
    {
        builder.Append('"');
        foreach (var c in text)
        {
            switch (c)
            {
                case '"': builder.Append("\\\""); break;
                case '\\': builder.Append("\\\\"); break;
                case '\b': builder.Append("\\b"); break;
                case '\f': builder.Append("\\f"); break;
                case '\n': builder.Append("\\n"); break;
                case '\r': builder.Append("\\r"); break;
                case '\t': builder.Append("\\t"); break;
                default:
                    if (c < 0x20)
                        builder.Append("\\u")
                            .Append(((int)c).ToString("x4",
                                CultureInfo.InvariantCulture));
                    else
                        builder.Append(c);
                    break;
            }
        }
        builder.Append('"');
    }

    /// <summary>Python repr(float) — shortest round-trip via R format,
    /// integral floats keep a trailing ".0", exponents render as
    /// "e+NN"/"e-NN", non-finite as Python literals.</summary>
    private static void WriteFloat(StringBuilder builder, double value)
    {
        if (double.IsNaN(value)) { builder.Append("NaN"); return; }
        if (double.IsPositiveInfinity(value)) { builder.Append("Infinity"); return; }
        if (double.IsNegativeInfinity(value)) { builder.Append("-Infinity"); return; }
        var text = value.ToString("R", CultureInfo.InvariantCulture);
        if (text.Contains('E'))
        {
            // .NET "1E+20" / "1E-05" → Python "1e+20" / "1e-05"
            var parts = text.Split('E');
            var mantissa = parts[0];
            var exp = int.Parse(parts[1], CultureInfo.InvariantCulture);
            builder.Append(mantissa)
                .Append('e')
                .Append(exp < 0 ? "-" : "+")
                .Append(Math.Abs(exp).ToString("D2", CultureInfo.InvariantCulture));
            return;
        }
        if (!text.Contains('.'))
            text += ".0";
        builder.Append(text);
    }

    /// <summary>NFC normalize then UTF-8 — the exact input the Python
    /// oracle hashes.</summary>
    public static byte[] CanonicalUtf8(string serialized) =>
        Encoding.UTF8.GetBytes(serialized.Normalize(NormalizationForm.FormC));
}
