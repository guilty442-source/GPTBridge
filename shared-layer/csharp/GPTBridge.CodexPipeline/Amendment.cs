using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Fail-closed denial of an unauthorized codex amendment request —
/// parity with codex_amendment.CodexAmendmentDenied.
/// </summary>
internal class CodexAmendmentDenied : Exception
{
    public const string FailureCode = "PERMISSION_DENIED";

    public CodexAmendmentDenied(string message) : base(message) { }
}

/// <summary>Repo-root discovery + amendment constants + JSON helpers
/// shared by the whole pipeline.</summary>
internal static class Repo
{
    private static string? _root;

    /// <summary>Explicit override (CLI ``--root``).</summary>
    public static void SetRoot(string root) =>
        _root = Path.GetFullPath(root);

    /// <summary>Repo root: explicit override, ``GPTBRIDGE_ROOT``, or the
    /// nearest ancestor of the working directory that carries the
    /// ``governance_rule/execution`` marker.  Fail-closed.</summary>
    public static string Root()
    {
        if (_root is not null)
            return _root;
        var env = Environment.GetEnvironmentVariable("GPTBRIDGE_ROOT");
        if (!string.IsNullOrWhiteSpace(env))
        {
            _root = Path.GetFullPath(env);
            return _root;
        }
        var dir = new DirectoryInfo(Directory.GetCurrentDirectory());
        while (dir is not null)
        {
            if (Directory.Exists(Path.Combine(
                    dir.FullName, "governance_rule", "execution"))
                && Directory.Exists(
                    Path.Combine(dir.FullName, "main-system")))
            {
                _root = dir.FullName;
                return _root;
            }
            dir = dir.Parent;
        }
        throw new InvalidOperationException("REPO_ROOT_UNRESOLVED");
    }

    public static string StateDir() =>
        Path.Combine(Root(), "main-system", "runtime", "state");

    /// <summary>``%Y-%m-%dT%H:%M:%SZ`` UTC timestamp (time.gmtime
    /// parity).</summary>
    public static string UtcNow() =>
        DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'",
            System.Globalization.CultureInfo.InvariantCulture);

    // -- minimal JSON-object traversal helpers ----------------------------

    public static JsonObject? Obj(JsonNode? node) =>
        node as JsonObject;

    public static string Str(JsonNode? node, string key)
    {
        if (node is JsonObject obj
            && obj.TryGetPropertyValue(key, out var value)
            && value is JsonValue v
            && v.TryGetValue<string>(out var s))
            return s;
        return "";
    }

    public static JsonNode? Get(JsonNode? node, string key) =>
        node is JsonObject obj
            && obj.TryGetPropertyValue(key, out var value) ? value : null;

    public static bool Truthy(JsonNode? node)
    {
        if (node is null)
            return false;
        if (node is JsonValue v)
        {
            if (v.TryGetValue<bool>(out var b)) return b;
            if (v.TryGetValue<string>(out var s)) return s.Length > 0;
            if (v.TryGetValue<long>(out var l)) return l != 0;
            if (v.TryGetValue<double>(out var d)) return d != 0;
        }
        if (node is JsonArray a) return a.Count > 0;
        if (node is JsonObject o) return o.Count > 0;
        return true;
    }

    /// <summary>Convert a JsonNode tree into the plain CLR object model
    /// the canonical-JSON writer consumes (dict/list/scalars).</summary>
    public static object? ToPlain(JsonNode? node)
    {
        switch (node)
        {
            case null:
                return null;
            case JsonObject obj:
                var map = new Dictionary<string, object?>(
                    StringComparer.Ordinal);
                foreach (var pair in obj)
                    map[pair.Key] = ToPlain(pair.Value);
                return map;
            case JsonArray arr:
                var list = new List<object?>(arr.Count);
                foreach (var item in arr)
                    list.Add(ToPlain(item));
                return list;
            case JsonValue value:
                // Python parity: a JSON number with '.' or 'e' parses as
                // float, otherwise int (arbitrary precision — fall back
                // to decimal for bigint).  Inspecting the raw token is
                // required: .NET happily fits -3e20 into decimal where
                // Python would keep it a float.
                if (value.TryGetValue<bool>(out var b)) return b;
                if (value.TryGetValue<string>(out var s)) return s;
                if (value.TryGetValue<System.Text.Json.JsonElement>(
                        out var el)
                    && el.ValueKind == System.Text.Json.JsonValueKind
                        .Number)
                {
                    var raw = el.GetRawText();
                    if (raw.IndexOfAny(new[] { '.', 'e', 'E' }) >= 0)
                        return el.GetDouble();
                    if (el.TryGetInt64(out var li)) return li;
                    if (el.TryGetDecimal(out var dm)) return dm;
                    return el.GetDouble();
                }
                if (value.TryGetValue<long>(out var l)) return l;
                if (value.TryGetValue<double>(out var d)) return d;
                if (value.TryGetValue<decimal>(out var m)) return m;
                return value.ToString();
            default:
                return node.ToString();
        }
    }

    /// <summary>``_atomic_json`` — indent-2, sort-keys, LF-terminated
    /// atomic replace.  Emits through canonical key ordering to match
    /// Python ``json.dumps(sort_keys=True, indent=2)``.</summary>
    public static void AtomicJson(string path, object? payload)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path) ?? ".");
        var temporary = path + ".tmp";
        File.WriteAllText(temporary,
            PrettyJson.Serialize(payload) + "\n",
            new System.Text.UTF8Encoding(false));
        File.Move(temporary, path, overwrite: true);
    }
}

/// <summary>Pretty JSON with sorted keys and two-space indent — the
/// exact ``json.dumps(..., indent=2, ensure_ascii=False, sort_keys=True)``
/// output the Python oracle writes for ledger/manifest files.</summary>
internal static class PrettyJson
{
    public static string Serialize(object? payload)
    {
        var builder = new System.Text.StringBuilder(1024);
        Write(builder, CanonJson.ToJsonSafe(payload), 0);
        return builder.ToString();
    }

    private static void Write(System.Text.StringBuilder builder,
        object? value, int depth)
    {
        var indent = new string(' ', depth * 2);
        var child = new string(' ', (depth + 1) * 2);
        switch (value)
        {
            case null:
                builder.Append("null"); return;
            case bool b:
                builder.Append(b ? "true" : "false"); return;
            case string:
            case byte or sbyte or short or ushort or int or uint or long
                or ulong or float or double:
                builder.Append(CanonJson.Write(value)); return;
            case IDictionary<string, object?> map:
                if (map.Count == 0) { builder.Append("{}"); return; }
                builder.Append("{\n");
                var first = true;
                foreach (var pair in map)
                {
                    if (!first) builder.Append(",\n");
                    first = false;
                    builder.Append(child)
                        .Append(CanonJson.Write(pair.Key))
                        .Append(": ");
                    Write(builder, pair.Value, depth + 1);
                }
                builder.Append('\n').Append(indent).Append('}');
                return;
            case System.Collections.Generic.IEnumerable<object?> list:
                {
                    var items = list.ToList();
                    if (items.Count == 0) { builder.Append("[]"); return; }
                    builder.Append("[\n");
                    for (var i = 0; i < items.Count; i++)
                    {
                        if (i > 0) builder.Append(",\n");
                        builder.Append(child);
                        Write(builder, items[i], depth + 1);
                    }
                    builder.Append('\n').Append(indent).Append(']');
                    return;
                }
            default:
                builder.Append(CanonJson.Write(
                    CanonJson.ToJsonSafe(value)));
                return;
        }
    }
}

/// <summary>Parity with codex_amendment.py — sovereign roster, change
/// classes, requester/class normalization.</summary>
internal static class Amendment
{
    public static readonly string[] ActiveSovereignRequesters =
    {
        "decision-sovereign",
        "permission-sovereign",
        "system-runtime-sovereign",
        "automation-sovereign",
        "xingcheng",
    };

    public static readonly Dictionary<string, string> SovereignAliases =
        new(StringComparer.Ordinal)
        {
            ["星澄"] = "xingcheng",
            // A486: synchronization-sovereign → automation-sovereign
            ["synchronization-sovereign"] = "automation-sovereign",
        };

    public static readonly string[] ChangeClasses =
    {
        "editorial",
        "clarification",
        "provision-scope",
        "authority-duty",
        "architecture-authority",
        "complete-reconstitution",
    };

    public const string DefaultChangeClass = "provision-scope";
    public const string AmendmentFlow =
        "A382/A488-non-disruptive-amendment-flow";
    public const string RequiredGate =
        "five-sovereign-audit-unanimous-pass";

    public static string NormalizeRequester(string requestedBy)
    {
        var raw = (requestedBy ?? "").Trim();
        var value = SovereignAliases.TryGetValue(raw, out var alias)
            ? alias : raw;
        if (!ActiveSovereignRequesters.Contains(value))
            throw new CodexAmendmentDenied(
                $"codex amendment requester is not an active sovereign: "
                + $"'{requestedBy}'");
        return value;
    }

    public static string NormalizeChangeClass(string changeClass)
    {
        var value = (changeClass ?? "").Trim()
            .ToLowerInvariant().Replace('_', '-');
        if (!ChangeClasses.Contains(value))
            throw new CodexAmendmentDenied(
                $"unknown codex amendment change class: '{changeClass}'");
        return value;
    }
}

/// <summary>``codex_repository.codex_version_units`` — legacy
/// ``int.5digits`` or UTC timestamp → integer units.</summary>
internal static class CodexVersion
{
    public const int CodexVersionUnit = 100000;

    public static long Units(object? value)
    {
        var text = (value?.ToString() ?? "").Trim();
        var dot = text.IndexOf('.');
        if (dot >= 0)
        {
            var whole = text[..dot];
            var fraction = text[(dot + 1)..];
            if (whole.All(char.IsDigit) && fraction.Length == 5
                && fraction.All(char.IsDigit))
                return long.Parse(whole) * CodexVersionUnit
                    + long.Parse(fraction);
        }
        var normalized = text.Replace("Z", "+00:00");
        if (DateTimeOffset.TryParse(normalized,
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AssumeUniversal,
                out var parsed))
            return parsed.ToUnixTimeSeconds();
        throw new ArgumentException(
            $"codex version must be integer-dot-five-decimal-digits or a "
            + $"UTC timestamp: '{value}'");
    }
}
