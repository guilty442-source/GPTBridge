using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Python-compatible canonical JSON (parity with json.dumps used by the
/// git governance plane): sorted keys, configurable separators, and
/// ensure_ascii=False escaping (non-ASCII emitted as raw UTF-8).
/// </summary>
internal static class Canon
{
    public static string Escape(string value)
    {
        var sb = new StringBuilder(value.Length + 8);
        sb.Append('"');
        foreach (var ch in value)
        {
            switch (ch)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                default:
                    if (ch < ' ')
                        sb.Append("\\u").Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                    else
                        sb.Append(ch);
                    break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }

    /// <summary>Canonical compact form: sort_keys=True, separators=(",",":").</summary>
    public static string Compact(JsonElement element)
    {
        var sb = new StringBuilder();
        WriteElement(element, sb, compact: true, indent: -1, depth: 0);
        return sb.ToString();
    }

    /// <summary>Python json.dumps default separators: (", ", ": ").</summary>
    public static string Spaced(JsonElement element)
    {
        var sb = new StringBuilder();
        WriteElement(element, sb, compact: false, indent: -1, depth: 0);
        return sb.ToString();
    }

    /// <summary>json.dumps(indent=2, sort_keys=True).</summary>
    public static string Indented(JsonElement element)
    {
        var sb = new StringBuilder();
        WriteElement(element, sb, compact: false, indent: 2, depth: 0);
        return sb.ToString();
    }

    public static string CompactNode(JsonNode? node) =>
        Compact(node is null ? default : ToElement(node));

    private static JsonElement ToElement(JsonNode node)
    {
        using var document = JsonDocument.Parse(node.ToJsonString());
        return document.RootElement.Clone();
    }

    private static void WriteElement(
        JsonElement element, StringBuilder sb, bool compact, int indent, int depth)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
            {
                var properties = element.EnumerateObject()
                    .OrderBy(p => p.Name, StringComparer.Ordinal).ToList();
                if (properties.Count == 0)
                {
                    sb.Append("{}");
                    return;
                }
                sb.Append('{');
                for (var i = 0; i < properties.Count; i++)
                {
                    if (i > 0) sb.Append(compact ? "," : ",");
                    if (indent >= 0)
                    {
                        sb.Append('\n');
                        sb.Append(' ', indent * (depth + 1));
                    }
                    else if (!compact)
                    {
                        sb.Append(' ');
                    }
                    sb.Append(Escape(properties[i].Name));
                    sb.Append(compact ? ":" : ": ");
                    WriteElement(properties[i].Value, sb, compact, indent, depth + 1);
                }
                if (indent >= 0)
                {
                    sb.Append('\n');
                    sb.Append(' ', indent * depth);
                }
                sb.Append('}');
                return;
            }
            case JsonValueKind.Array:
            {
                var items = element.EnumerateArray().ToList();
                if (items.Count == 0)
                {
                    sb.Append("[]");
                    return;
                }
                sb.Append('[');
                for (var i = 0; i < items.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    if (indent >= 0)
                    {
                        sb.Append('\n');
                        sb.Append(' ', indent * (depth + 1));
                    }
                    else if (!compact)
                    {
                        sb.Append(' ');
                    }
                    WriteElement(items[i], sb, compact, indent, depth + 1);
                }
                if (indent >= 0)
                {
                    sb.Append('\n');
                    sb.Append(' ', indent * depth);
                }
                sb.Append(']');
                return;
            }
            case JsonValueKind.String:
                sb.Append(Escape(element.GetString() ?? ""));
                return;
            case JsonValueKind.Number:
                sb.Append(element.GetRawText());
                return;
            case JsonValueKind.True:
                sb.Append("true");
                return;
            case JsonValueKind.False:
                sb.Append("false");
                return;
            default:
                sb.Append("null");
                return;
        }
    }

    public static string Sha256Hex(ReadOnlySpan<byte> data) =>
        Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();

    public static string Sha256Hex(string text) =>
        Sha256Hex(Encoding.UTF8.GetBytes(text));

    public static string Sha256File(string path) =>
        Sha256Hex(File.ReadAllBytes(path));

    public static string UtcNow() =>
        DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);

    /// <summary>Python time.strftime("%Y-%m-%dT%H:%M:%S%z") — local, no colon.</summary>
    public static string LocalStamp()
    {
        var now = DateTimeOffset.Now;
        var offset = now.Offset.ToString("hh\\mm");
        var sign = now.Offset >= TimeSpan.Zero ? "+" : "-";
        return now.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture)
            + sign + offset;
    }

    public static double EpochSeconds() =>
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    /// <summary>Atomic JSON write matching the Python *.tmp + os.replace flow.</summary>
    public static void WriteJsonAtomic(string path, string content)
    {
        var tmp = path + "." + Environment.ProcessId + ".tmp";
        try
        {
            File.WriteAllText(tmp, content, new UTF8Encoding(false));
            File.Move(tmp, path, overwrite: true);
        }
        catch (IOException)
        {
            try { File.Delete(tmp); } catch (IOException) { }
            throw;
        }
    }
}
