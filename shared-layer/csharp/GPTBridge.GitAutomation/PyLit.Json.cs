using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>JSON codec for the PyLit value model — the native data home
/// for the retired Python-literal registries (B167/B38).  A registry
/// module serialises as one JSON object mapping constant name to value;
/// constructor-call rows keep their shape under ``$call``/``$args``/
/// ``$kw`` so the manifest emitters see identical Value trees.</summary>
internal static partial class PyLit
{
    // -- encode ------------------------------------------------------------

    public static JsonNode? ToJson(Value value) => value switch
    {
        Str s => JsonValue.Create(s.Text),
        Num n => JsonValue.Create(n.N),
        Bool b => JsonValue.Create(b.B),
        NoneV => null,
        Seq seq => new JsonArray(
            seq.Items.Select(i => ToJson(i)).ToArray()),
        Dict d => DictToJson(d),
        Call c => new JsonObject
        {
            ["$call"] = c.Func,
            ["$args"] = new JsonArray(
                c.Args.Select(a => ToJson(a)).ToArray()),
            ["$kw"] = new JsonObject(c.Kw.Select(k =>
                new KeyValuePair<string, JsonNode?>(
                    k.Name, ToJson(k.Val))).ToArray()),
        },
        Ref r => new JsonObject { ["$ref"] = r.Name },
        _ => null,
    };

    private static JsonNode DictToJson(Dict dict)
    {
        var plain = dict.Entries.All(e =>
            e.Key is Str s && !s.Text.StartsWith('$'));
        if (!plain)
            return new JsonObject
            {
                ["$dict"] = new JsonArray(dict.Entries.Select(e =>
                        (JsonNode?)new JsonArray(
                            ToJson(e.Key), ToJson(e.Val)))
                    .ToArray()),
            };
        var obj = new JsonObject();
        foreach (var (key, val) in dict.Entries)
            obj[((Str)key).Text] = ToJson(val);
        return obj;
    }

    /// <summary>Module-level constant table as a JSON object
    /// ``{ "NAME": <value>, ... }``.</summary>
    public static JsonObject ModuleToJson(
        IReadOnlyDictionary<string, Value> constants)
    {
        var obj = new JsonObject();
        foreach (var (name, value) in constants)
            obj[name] = ToJson(value);
        return obj;
    }

    // -- decode ------------------------------------------------------------

    public static Value FromJson(JsonNode? node) => node switch
    {
        null => new NoneV(),
        JsonArray a => new Seq(
            a.Select(i => FromJson(i)).ToList()),
        JsonObject o => ObjectFromJson(o),
        JsonValue v when v.TryGetValue<bool>(out var b) =>
            new Bool(b),
        JsonValue v when v.TryGetValue<double>(out var n) =>
            new Num(n),
        JsonValue v when v.TryGetValue<string>(out var s) =>
            new Str(s),
        _ => new NoneV(),
    };

    private static Value ObjectFromJson(JsonObject obj)
    {
        if (obj.TryGetPropertyValue("$call", out var func))
            return new Call(
                func?.GetValue<string>() ?? "",
                (obj["$args"] as JsonArray ?? new JsonArray())
                    .Select(a => FromJson(a)).ToList(),
                (obj["$kw"] as JsonObject ?? new JsonObject())
                    .Select(kv => (kv.Key, FromJson(kv.Value)))
                    .ToList());
        if (obj.TryGetPropertyValue("$ref", out var name))
            return new Ref(name?.GetValue<string>() ?? "");
        if (obj.TryGetPropertyValue("$dict", out var pairs)
            && pairs is JsonArray arr)
            return new Dict(arr.OfType<JsonArray>().Select(p =>
                ((Value Key, Value Val))(FromJson(p[0]),
                    FromJson(p[1]))).ToList());
        return new Dict(obj.Select(kv =>
            ((Value Key, Value Val))(new Str(kv.Key),
                FromJson(kv.Value))).ToList());
    }

    /// <summary>JSON-encoded module constants — the native replacement
    /// for <c>ModuleConstants(pySource)</c> on retired registries.</summary>
    public static Dictionary<string, Value> ModuleConstantsJson(
        string json)
    {
        var result = new Dictionary<string, Value>(
            StringComparer.Ordinal);
        if (JsonNode.Parse(json) is JsonObject obj)
            foreach (var (name, node) in obj)
                result[name] = FromJson(node);
        return result;
    }
}
