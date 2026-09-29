// CanonicalJson.cs — Python ``json.dumps`` parity writers.
//
// Two surfaces, one escape/number rule set so hashes and files written by
// the retired Python lane and this port stay byte-compatible:
//
//   Canonical(e)  == json.dumps(e, ensure_ascii=False, sort_keys=True,
//                              separators=(",", ":"))
//   Pretty(e)     == json.dumps(e, ensure_ascii=False, sort_keys=True,
//                              indent=2)  (+ caller appends "\n")
//
// Numbers are re-formatted through a Python repr() port so legacy audit
// payloads hash identically when re-canonicalized by either lane.

using System.Globalization;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CanonicalJson
{
    // ---------------------------------------------------------------------
    // Public surface
    // ---------------------------------------------------------------------

    public static string Canonical(JsonElement element)
    {
        var sb = new StringBuilder();
        Write(element, sb, canonical: true, depth: 0);
        return sb.ToString();
    }

    public static string CanonicalDocument(string json)
    {
        using var doc = JsonDocument.Parse(json);
        return Canonical(doc.RootElement);
    }

    public static string Pretty(JsonElement element)
    {
        var sb = new StringBuilder();
        Write(element, sb, canonical: false, depth: 0);
        return sb.ToString();
    }

    public static JsonElement Parse(string json)
    {
        // Callers must keep the returned element alive only within the doc
        // scope; use ParseDoc when a long-lived element is needed.
        var doc = JsonDocument.Parse(json);
        return doc.RootElement.Clone();
    }

    // ---------------------------------------------------------------------
    // Writer
    // ---------------------------------------------------------------------

    private static void Write(JsonElement el, StringBuilder sb, bool canonical, int depth)
    {
        switch (el.ValueKind)
        {
            case JsonValueKind.Object:
                WriteObject(el, sb, canonical, depth);
                break;
            case JsonValueKind.Array:
                WriteArray(el, sb, canonical, depth);
                break;
            case JsonValueKind.String:
                WriteString(el.GetString() ?? "", sb);
                break;
            case JsonValueKind.Number:
                sb.Append(FormatNumber(el));
                break;
            case JsonValueKind.True:
                sb.Append("true");
                break;
            case JsonValueKind.False:
                sb.Append("false");
                break;
            default:
                sb.Append("null");
                break;
        }
    }

    private static void WriteObject(JsonElement obj, StringBuilder sb, bool canonical, int depth)
    {
        var props = obj.EnumerateObject()
            .OrderBy(p => p.Name, StringComparer.Ordinal)
            .ToList();
        if (props.Count == 0) { sb.Append(canonical ? "{}" : "{}"); return; }
        sb.Append('{');
        for (int i = 0; i < props.Count; i++)
        {
            if (i > 0) sb.Append(',');
            if (!canonical)
            {
                sb.Append('\n');
                sb.Append(' ', (depth + 1) * 2);
            }
            WriteString(props[i].Name, sb);
            sb.Append(canonical ? ":" : ": ");
            Write(props[i].Value, sb, canonical, depth + 1);
        }
        if (!canonical)
        {
            sb.Append('\n');
            sb.Append(' ', depth * 2);
        }
        sb.Append('}');
    }

    private static void WriteArray(JsonElement arr, StringBuilder sb, bool canonical, int depth)
    {
        var items = arr.EnumerateArray().ToList();
        if (items.Count == 0) { sb.Append("[]"); return; }
        sb.Append('[');
        for (int i = 0; i < items.Count; i++)
        {
            if (i > 0) sb.Append(',');
            if (!canonical)
            {
                sb.Append('\n');
                sb.Append(' ', (depth + 1) * 2);
            }
            Write(items[i], sb, canonical, depth + 1);
        }
        if (!canonical)
        {
            sb.Append('\n');
            sb.Append(' ', depth * 2);
        }
        sb.Append(']');
    }

    // Python ensure_ascii=False string escaping: literal UTF-8; escape only
    // '"', '\\' and control chars (\b \f \n \r \t; others \uXXXX lowercase).
    private static void WriteString(string value, StringBuilder sb)
    {
        sb.Append('"');
        foreach (char c in value)
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
                    if (c < ' ')
                        sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                    else
                        sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }

    // Python repr(float) / repr(int) parity.
    //
    // - integral values → "N.0" when the raw JSON has a decimal point or
    //   exponent (float source), plain digits when it does not.
    // - non-integral doubles → shortest round-trip then exponent style
    //   e±NN (sign always present, exponent ≥ 2 digits), matching repr.
    private static string FormatNumber(JsonElement el)
    {
        if (el.TryGetInt64(out long i))
            return i.ToString(CultureInfo.InvariantCulture);
        if (!el.TryGetDouble(out double d) || !double.IsFinite(d))
            return "null"; // json.dumps never emits these; fail to null marker
        string s = d.ToString("R", CultureInfo.InvariantCulture);
        // Normalize .NET "1E+20" -> "1e+20"; pad exponent to >= 2 digits.
        int e = s.IndexOf('E');
        if (e >= 0)
        {
            string mantissa = s[..e];
            string exp = s[(e + 1)..];
            char sign = exp[0] == '-' ? '-' : '+';
            string digits = exp.TrimStart('+', '-').PadLeft(2, '0');
            return mantissa + "e" + sign + digits;
        }
        if (!s.Contains('.') && !s.Contains("Infinity") && !s.Contains("NaN"))
            s += ".0";
        return s;
    }

    // ---------------------------------------------------------------------
    // Dict-builder helpers (canonical text straight from dictionaries)
    // ---------------------------------------------------------------------

    /// <summary>Canonical JSON from a dictionary tree of primitives.</summary>
    public static string CanonicalDict(IReadOnlyDictionary<string, object?> map)
    {
        var sb = new StringBuilder();
        WriteValue(map, sb, true, 0);
        return sb.ToString();
    }

    public static string PrettyDict(IReadOnlyDictionary<string, object?> map)
    {
        var sb = new StringBuilder();
        WriteValue(map, sb, false, 0);
        return sb.ToString();
    }

    /// <summary>Non-sorted insertion-order single-line JSON
    /// (``json.dumps`` default separators ``", "`` / ``": "``).</summary>
    public static string PlainDict(IReadOnlyDictionary<string, object?> map)
    {
        var sb = new StringBuilder();
        WriteValue(map, sb, Mode.Flat, 0, sortKeys: false);
        return sb.ToString();
    }

    internal enum Mode { Canonical, Pretty, Flat }

    public static void WriteValue(object? value, StringBuilder sb, bool canonical, int depth,
                                  bool sortKeys = true)
        => WriteValue(value, sb, canonical ? Mode.Canonical : Mode.Pretty, depth, sortKeys);

    public static void WriteValue(object? value, StringBuilder sb, Mode mode, int depth,
                                  bool sortKeys = true)
    {
        bool canonical = mode == Mode.Canonical;
        bool pretty = mode == Mode.Pretty;

        switch (value)
        {
            case null:
                sb.Append("null");
                break;
            case bool b:
                sb.Append(b ? "true" : "false");
                break;
            case JsonElement el:
                Write(el, sb, canonical, depth);
                break;
            case string s:
                WriteString(s, sb);
                break;
            case double d:
                sb.Append(FormatDouble(d));
                break;
            case float f:
                sb.Append(FormatDouble(f));
                break;
            case long l:
                sb.Append(l.ToString(CultureInfo.InvariantCulture));
                break;
            case int n:
                sb.Append(n.ToString(CultureInfo.InvariantCulture));
                break;
            case uint un:
                sb.Append(un.ToString(CultureInfo.InvariantCulture));
                break;
            case ulong ul:
                sb.Append(ul.ToString(CultureInfo.InvariantCulture));
                break;
            case decimal m:
                sb.Append(m.ToString(CultureInfo.InvariantCulture));
                break;
            case IReadOnlyDictionary<string, object?> map:
            {
                IEnumerable<KeyValuePair<string, object?>> props = map;
                if (sortKeys)
                    props = props.OrderBy(p => p.Key, StringComparer.Ordinal);
                var list = props.ToList();
                if (list.Count == 0) { sb.Append("{}"); return; }
                sb.Append('{');
                for (int i = 0; i < list.Count; i++)
                {
                    if (i > 0) sb.Append(mode == Mode.Flat ? ", " : ",");
                    if (pretty)
                    {
                        sb.Append('\n');
                        sb.Append(' ', (depth + 1) * 2);
                    }
                    WriteString(list[i].Key, sb);
                    sb.Append(canonical ? ":" : ": ");
                    WriteValue(list[i].Value, sb, mode, depth + 1, sortKeys);
                }
                if (pretty)
                {
                    sb.Append('\n');
                    sb.Append(' ', depth * 2);
                }
                sb.Append('}');
                break;
            }
            case System.Collections.IDictionary map:
            {
                // Generic invariance: Dictionary<string, SomeValue> does not
                // match IReadOnlyDictionary<string, object?> — without this
                // branch it silently degrades to an array of KeyValuePair
                // ToString() text. Fail on non-string keys instead.
                var pairs = new List<KeyValuePair<string, object?>>();
                foreach (System.Collections.DictionaryEntry entry in map)
                {
                    if (entry.Key is not string key)
                        throw new ArgumentException(
                            $"CANONICAL_KEY_NONSTRING:{entry.Key?.GetType().Name}");
                    pairs.Add(new KeyValuePair<string, object?>(key, entry.Value));
                }
                IEnumerable<KeyValuePair<string, object?>> props = pairs;
                if (sortKeys)
                    props = props.OrderBy(p => p.Key, StringComparer.Ordinal);
                var list = props.ToList();
                if (list.Count == 0) { sb.Append("{}"); return; }
                sb.Append('{');
                for (int i = 0; i < list.Count; i++)
                {
                    if (i > 0) sb.Append(mode == Mode.Flat ? ", " : ",");
                    if (pretty)
                    {
                        sb.Append('\n');
                        sb.Append(' ', (depth + 1) * 2);
                    }
                    WriteString(list[i].Key, sb);
                    sb.Append(canonical ? ":" : ": ");
                    WriteValue(list[i].Value, sb, mode, depth + 1, sortKeys);
                }
                if (pretty)
                {
                    sb.Append('\n');
                    sb.Append(' ', depth * 2);
                }
                sb.Append('}');
                break;
            }
            case System.Collections.IEnumerable seq:
            {
                var items = seq.Cast<object?>().ToList();
                if (items.Count == 0) { sb.Append("[]"); return; }
                sb.Append('[');
                for (int i = 0; i < items.Count; i++)
                {
                    if (i > 0) sb.Append(mode == Mode.Flat ? ", " : ",");
                    if (pretty)
                    {
                        sb.Append('\n');
                        sb.Append(' ', (depth + 1) * 2);
                    }
                    WriteValue(items[i], sb, mode, depth + 1, sortKeys);
                }
                if (pretty)
                {
                    sb.Append('\n');
                    sb.Append(' ', depth * 2);
                }
                sb.Append(']');
                break;
            }
            default:
                // Fail closed: ToString() of an unknown type would silently
                // corrupt persisted/audited JSON.
                throw new ArgumentException(
                    $"CANONICAL_VALUE_UNSUPPORTED:{value.GetType().Name}");
        }
    }

    private static string FormatDouble(double d)
    {
        if (!double.IsFinite(d)) return "null";
        string s = d.ToString("R", CultureInfo.InvariantCulture);
        int e = s.IndexOf('E');
        if (e >= 0)
        {
            string mantissa = s[..e];
            string exp = s[(e + 1)..];
            char sign = exp[0] == '-' ? '-' : '+';
            string digits = exp.TrimStart('+', '-').PadLeft(2, '0');
            return mantissa + "e" + sign + digits;
        }
        return s.Contains('.') ? s : s + ".0";
    }
}
