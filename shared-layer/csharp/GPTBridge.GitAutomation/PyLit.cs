using System.Text;

namespace GPTBridge.GitAutomation;

/// <summary>Python-literal subset parser/evaluator used by the governed
/// manifest pipeline (B94 split — Lexer/Parser/Eval in siblings).</summary>
internal static partial class PyLit
{
    public abstract record Value;
    public sealed record Str(string Text) : Value;
    public sealed record Num(double N) : Value;
    public sealed record Bool(bool B) : Value;
    public sealed record NoneV() : Value;
    public sealed record Seq(List<Value> Items) : Value; // tuple/list/set
    public sealed record Dict(List<(Value Key, Value Val)> Entries) : Value;
    public sealed record Call(string Func, List<Value> Args,
        List<(string Name, Value Val)> Kw) : Value;
    public sealed record Ref(string Name) : Value;

    private enum Tk { Name, Str, Num, Punct, End }


    /// <summary>Set-as-items: Seq items, Dict keys, or
    /// set()/frozenset() wrappers (``set(DICT)`` yields keys).</summary>
    private static List<Value>? SetItems(Value? value) =>
        value switch
        {
            Seq s => new List<Value>(s.Items),
            Dict d => d.Entries.Select(e2 => e2.Key).ToList(),
            Call c when c.Args.Count == 1
                        && c.Func is "set" or "frozenset"
                => SetItems(c.Args[0]),
            _ => null,
        };

    /// <summary>Unwrap set()/frozenset()/tuple()/list()/sorted()
    /// single-arg wrappers to the inner sequence.</summary>
    private static Value? Unwrap(Value? value) =>
        value is Call c && c.Args.Count == 1
            && c.Func is "set" or "frozenset" or "tuple" or "list"
                or "sorted"
            ? Unwrap(c.Args[0]) : value;

    /// <summary>``( Call(...) )`` paren-grouped constants evaluate to a
    /// one-item Seq — unwrap to the call record.</summary>
    public static Call? AsCall(Value? value) =>
        value switch
        {
            Call c => c,
            Seq { Items.Count: 1 } s => AsCall(s.Items[0]),
            _ => null,
        };

    /// <summary>Paren-grouped string constants evaluate to a one-item
    /// Seq — unwrap to the text.</summary>
    public static string? AsStr(Value? value) =>
        value switch
        {
            Str s => s.Text,
            Seq { Items.Count: 1 } s => AsStr(s.Items[0]),
            _ => null,
        };

    // -- convenience extractors ------------------------------------------

    public static List<string> Strings(Value? value)
    {
        var result = new List<string>();
        if (Unwrap(value) is Seq seq)
            foreach (var item in seq.Items)
                if (AsStr(item) is { } text)
                    result.Add(text);
        return result;
    }

    public static List<string> StringSet(Value? value) => Strings(value);

    public static Dictionary<string, List<string>> StrListDict(
        Value? value)
    {
        var result = new Dictionary<string, List<string>>(
            StringComparer.Ordinal);
        if (value is Dict dict)
            foreach (var (key, val) in dict.Entries)
                if (key is Str k)
                    result[k.Text] = Strings(val);
        return result;
    }

    public static Dictionary<string, string> StrDict(Value? value)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        if (value is Dict dict)
            foreach (var (key, val) in dict.Entries)
                if (key is Str k && val is Str v)
                    result[k.Text] = v.Text;
        return result;
    }

    /// <summary>Every ``FuncName(...)`` call record in a module —
    /// dataclass rows like ``CapabilityIdentity(...)``.</summary>
    public static List<Call> TopCalls(string src, string funcName)
    {
        var constants = ModuleConstants(src);
        var calls = new List<Call>();
        foreach (var value in constants.Values)
            Collect(value);
        // also call expressions assigned directly
        void Collect(Value v)
        {
            switch (v)
            {
                case Call c:
                    if (c.Func == funcName) calls.Add(c);
                    foreach (var a in c.Args) Collect(a);
                    foreach (var (_, kv) in c.Kw) Collect(kv);
                    break;
                case Seq s:
                    foreach (var i in s.Items) Collect(i);
                    break;
                case Dict d:
                    foreach (var (_, val) in d.Entries) Collect(val);
                    break;
            }
        }
        return calls;
    }

    public static string? KwStr(Call call, string name) =>
        AsStr(call.Kw.FirstOrDefault(k => k.Name == name).Val);

    public static List<string> KwStrings(Call call, string name) =>
        call.Kw.FirstOrDefault(k => k.Name == name).Val is { } v
            ? Strings(v) : new List<string>();
}
