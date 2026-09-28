using System.Text;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Minimal Python-literal evaluator for the frozen governance sources.
/// Supports the constructs the registry/policy modules actually use:
/// string/int/bool/None literals, implicit string concat, tuples, lists,
/// dicts, frozenset()/set()/tuple() wrappers, ``re.compile("pat")`` and
/// ``Name(kw=value, ...)`` dataclass records (nested calls preserved as
/// opaque records).  Unevaluable expressions yield null rather than a
/// wrong value — the caller then emits a fail row (never silently
/// weakens a check).
/// </summary>
internal static class PyLit
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
    private sealed class Lexer
    {
        private readonly string _src;
        private int _pos;
        public Tk Kind;
        public string Text = "";
        public Lexer(string src) { _src = src; Next(); }
        private int _tokStart;
        /// <summary>Position of the current token — restore re-lexes
        /// the current token itself (kw-arg look-ahead).</summary>
        public int Mark() => _tokStart;
        public void Restore(int mark) { _pos = mark; Next(); }
        public string Source => _src;
        public int TokenStart => _tokStart;
        public void Next()
        {
            for (;;)
            {
                if (_pos >= _src.Length) { Kind = Tk.End; return; }
                var c = _src[_pos];
                if (c == '#')
                {
                    while (_pos < _src.Length && _src[_pos] != '\n') _pos++;
                    continue;
                }
                if (char.IsWhiteSpace(c)) { _pos++; continue; }
                if (c == '\\' && _pos + 1 < _src.Length && _src[_pos + 1] == '\n')
                { _pos += 2; continue; }
                break;
            }
            _tokStart = _pos;
            var ch = _src[_pos];
            if (ch == '"' || ch == '\''
                || (ch is 'r' or 'b' or 'f' or 'u' or 'R' or 'B' or 'F' or 'U'
                    && _pos + 1 < _src.Length
                    && (_src[_pos + 1] == '"' || _src[_pos + 1] == '\'')))
            {
                var raw = ch is 'r' or 'R';
                var fstring = ch is 'f' or 'F';
                if (raw || fstring || ch is 'b' or 'B' or 'u' or 'U')
                    _pos++;
                var quote = _src[_pos];
                var triple = _pos + 2 < _src.Length
                    && _src[_pos + 1] == quote && _src[_pos + 2] == quote;
                var delim = triple ? new string(quote, 3) : quote.ToString();
                _pos += delim.Length;
                var end = _src.IndexOf(delim, _pos, StringComparison.Ordinal);
                if (end < 0) { Kind = Tk.End; return; }
                var body = _src[_pos..end];
                _pos = end + delim.Length;
                Kind = Tk.Str;
                Text = raw || fstring
                    ? body
                    : body.Replace("\\n", "\n").Replace("\\t", "\t")
                        .Replace("\\r", "\r").Replace("\\\"", "\"")
                        .Replace("\\'", "'").Replace("\\\\", "\\")
                        .Replace("\\u", "");
                if (fstring) Text = "\0fstring\0" + Text;
                return;
            }
            if (char.IsDigit(ch))
            {
                var start = _pos;
                while (_pos < _src.Length
                       && (char.IsLetterOrDigit(_src[_pos])
                           || _src[_pos] is '.' or '_'))
                    _pos++;
                Kind = Tk.Num;
                Text = _src[start.._pos];
                return;
            }
            if (char.IsLetter(ch) || ch == '_')
            {
                var start = _pos;
                while (_pos < _src.Length
                       && (char.IsLetterOrDigit(_src[_pos])
                           || _src[_pos] == '_'))
                    _pos++;
                Kind = Tk.Name;
                Text = _src[start.._pos];
                return;
            }
            if (ch == '=' && _pos + 1 < _src.Length && _src[_pos + 1] == '=')
            { Kind = Tk.Punct; Text = "=="; _pos += 2; return; }
            if (ch == '-' && _pos + 1 < _src.Length && _src[_pos + 1] == '>')
            { Kind = Tk.Punct; Text = "->"; _pos += 2; return; }
            if (ch == '.' && _pos + 2 < _src.Length
                && _src[_pos + 1] == '.' && _src[_pos + 2] == '.')
            { Kind = Tk.Punct; Text = "..."; _pos += 3; return; }
            Kind = Tk.Punct;
            Text = ch.ToString();
            _pos++;
        }
    }

    private sealed class Parser
    {
        private readonly Lexer _lx;
        private readonly IReadOnlyDictionary<string, Value> _env;
        public int FailPos = -1;
        public Parser(string src, IReadOnlyDictionary<string, Value> env)
        { _lx = new Lexer(src); _env = env; }
        public string Src => _lx.Source;
        private void MarkFail()
        {
            if (_lx.TokenStart > FailPos) FailPos = _lx.TokenStart;
        }

        private bool Accept(string p)
        {
            if (_lx.Kind == Tk.Punct && _lx.Text == p)
            { _lx.Next(); return true; }
            return false;
        }
        private void Expect(string p)
        {
            if (!Accept(p))
            { MarkFail(); throw new FormatException($"expected {p}"); }
        }

        public Value? Expr()
        {
            var value = Term();
            if (value is null) return null;
            // implicit string concatenation / + concat
            for (;;)
            {
                if (_lx.Kind == Tk.Str)
                {
                    var next = Term();
                    if (next is Str s && value is Str l)
                    { value = new Str(l.Text + s.Text); continue; }
                    return null;
                }
                if (_lx.Kind == Tk.Punct && _lx.Text == "+")
                {
                    _lx.Next();
                    var rhs = Term();
                    if (rhs is null) return null;
                    if (value is Str l && rhs is Str r)
                    { value = new Str(l.Text + r.Text); continue; }
                    if (value is Num ln && rhs is Num rn)
                    { value = new Num(ln.N + rn.N); continue; }
                    return null;
                }
                if (_lx.Kind == Tk.Punct && _lx.Text == "|")
                {
                    _lx.Next();
                    var rhs = Term();
                    var leftSet = SetItems(value);
                    var rightSet = SetItems(rhs);
                    if (leftSet is not null && rightSet is not null)
                    {
                        leftSet.AddRange(rightSet);
                        value = new Seq(leftSet);
                        continue;
                    }
                    return null;
                }
                break;
            }
            return value;
        }

        private Value? Term()
        {
            var value = Primary();
            if (value is null) return null;
            // attribute access:  EXPR.attr
            while (_lx.Kind == Tk.Punct && _lx.Text == ".")
            {
                _lx.Next();
                if (_lx.Kind != Tk.Name)
                { MarkFail(); return null; }
                var attr = _lx.Text;
                _lx.Next();
                value = Attribute(value, attr);
                if (value is null) { MarkFail(); return null; }
            }
            return value;
        }

        /// <summary>``value.attr`` — dataclass kw, dict key,
        /// ``re.compile(...).pattern``, or symbolic ``Ref.attr``.</summary>
        private Value? Attribute(Value value, string attr) =>
            value switch
            {
                Call c when c.Func == "re.compile" && attr == "pattern"
                    && c.Args.Count > 0 => c.Args[0],
                Call c => c.Kw.FirstOrDefault(k => k.Name == attr).Val
                          ?? new Ref($"{c.Func}.{attr}"),
                Dict d => d.Entries
                    .Where(e2 => e2.Key is Str ks && ks.Text == attr)
                    .Select(e2 => e2.Val).FirstOrDefault(),
                Ref r => new Ref($"{r.Name}.{attr}"),
                _ => null,
            };

        private Value? Primary()
        {
            switch (_lx.Kind)
            {
                case Tk.Str:
                    var s = new Str(_lx.Text);
                    _lx.Next();
                    return s.Text.StartsWith("\0fstring\0")
                        ? null : s with { Text = s.Text };
                case Tk.Num:
                    var text = _lx.Text.Replace("_", "");
                    _lx.Next();
                    return double.TryParse(text,
                        System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture,
                        out var n) ? new Num(n) : null;
                case Tk.Name:
                    var name = _lx.Text;
                    _lx.Next();
                    if (_lx.Kind == Tk.Punct && _lx.Text == "(")
                        return CallExpr(name);
                    return name switch
                    {
                        "True" => new Bool(true),
                        "False" => new Bool(false),
                        "None" => new NoneV(),
                        _ => _env.TryGetValue(name, out var bound)
                            ? bound : new Ref(name),
                    };
                case Tk.Punct when _lx.Text == "(":
                    {
                        _lx.Next();
                        var items = new List<Value>();
                        if (Accept(")")) return new Seq(items);
                        for (;;)
                        {
                            var item = Expr();
                            if (item is null) return null;
                            // bare generator:  (EXPR for VAR in ITER)
                            if (_lx.Kind == Tk.Name
                                && _lx.Text == "for")
                            {
                                var gen = Generator(item);
                                if (gen is null) return null;
                                Expect(")");
                                return gen;
                            }
                            items.Add(item);
                            if (Accept(","))
                            {
                                if (Accept(")"))
                                    return new Seq(items);
                                continue;
                            }
                            Expect(")");
                            return new Seq(items);
                        }
                    }
                case Tk.Punct when _lx.Text == "[":
                    {
                        _lx.Next();
                        var items = new List<Value>();
                        if (Accept("]")) return new Seq(items);
                        for (;;)
                        {
                            var item = Expr();
                            if (item is null) return null;
                            if (_lx.Kind == Tk.Name
                                && _lx.Text == "for")
                            {
                                var gen = Generator(item);
                                if (gen is null) return null;
                                Expect("]");
                                return gen;
                            }
                            items.Add(item);
                            if (Accept(","))
                            {
                                if (Accept("]"))
                                    return new Seq(items);
                                continue;
                            }
                            Expect("]");
                            return new Seq(items);
                        }
                    }
                case Tk.Punct when _lx.Text == "{":
                    {
                        _lx.Next();
                        var dictEntries = new List<(Value, Value)>();
                        var setItems = new List<Value>();
                        var isDict = false;
                        var first = true;
                        if (Accept("}"))
                            return new Seq(setItems);
                        for (;;)
                        {
                            if (Accept("*"))
                            {
                                var starred = Expr();
                                if (starred is Seq seq)
                                {
                                    setItems.AddRange(seq.Items);
                                    first = false;
                                    if (Accept(","))
                                    {
                                        if (Accept("}"))
                                            return new Seq(setItems);
                                        continue;
                                    }
                                    Expect("}");
                                    return new Seq(setItems);
                                }
                                return null;
                            }
                            var key = Expr();
                            if (key is null) return null;
                            if (_lx.Kind == Tk.Name && _lx.Text == "for")
                            {
                                var gen = Generator(key);
                                if (gen is null) return null;
                                Expect("}");
                                return gen;
                            }
                            if (first && _lx.Kind == Tk.Punct
                                && _lx.Text == ":")
                                isDict = true;
                            if (isDict)
                            {
                                Expect(":");
                                var val = Expr();
                                if (val is null) return null;
                                dictEntries.Add((key, val));
                            }
                            else
                            {
                                setItems.Add(key);
                            }
                            first = false;
                            if (Accept(","))
                            {
                                if (Accept("}"))
                                    return isDict
                                        ? new Dict(dictEntries)
                                        : new Seq(setItems);
                                continue;
                            }
                            Expect("}");
                            return isDict ? new Dict(dictEntries)
                                          : new Seq(setItems);
                        }
                    }
                case Tk.Punct when _lx.Text == "-":
                    {
                        _lx.Next();
                        var operand = Term();
                        return operand is Num neg
                            ? new Num(-neg.N) : null;
                    }
                default:
                    MarkFail();
                    return null;
            }
        }

        private Value? CallExpr(string func)
        {
            Expect("(");
            var args = new List<Value>();
            var kw = new List<(string, Value)>();
            while (!Accept(")"))
            {
                if (_lx.Kind == Tk.Punct && _lx.Text == "*")
                {
                    _lx.Next();
                    if (_lx.Kind == Tk.Punct && _lx.Text == "*")
                    { _lx.Next(); }
                    var spread = Expr();
                    if (spread is Seq seq) args.AddRange(seq.Items);
                    else if (spread is null) return null;
                }
                else
                {
                    // look-ahead: NAME followed by ``=`` is a kw-arg
                    if (_lx.Kind == Tk.Name)
                    {
                        var mark = _lx.Mark();
                        var name = _lx.Text;
                        _lx.Next();
                        if (_lx.Kind == Tk.Punct && _lx.Text == "=")
                        {
                            _lx.Next();
                            var val = Expr();
                            if (val is null) return null;
                            kw.Add((name, val));
                            if (Accept(",")) continue;
                            if (_lx.Kind == Tk.Punct
                                && _lx.Text == ")")
                            { _lx.Next(); break; }
                            break;
                        }
                        _lx.Restore(mark);
                    }
                    var arg = Expr();
                    if (arg is null) return null;
                    // generator:  ``tuple(EXPR for VAR in ITER)``
                    if (_lx.Kind == Tk.Name && _lx.Text == "for")
                    {
                        arg = Generator(arg);
                        if (arg is null) return null;
                    }
                    args.Add(arg);
                }
                if (Accept(",")) continue;
                if (_lx.Kind == Tk.Punct && _lx.Text == ")")
                { _lx.Next(); break; }
                break;
            }
            return new Call(func, args, kw);
        }

        /// <summary>``EXPR for VAR in ITER`` — evaluate the body once
        /// per iterable item with the loop variable substituted.</summary>
        private Value? Generator(Value body)
        {
            _lx.Next(); // consume "for"
            if (_lx.Kind != Tk.Name) return null;
            var varName = _lx.Text;
            _lx.Next();
            if (!(_lx.Kind == Tk.Name && _lx.Text == "in"))
                return null;
            _lx.Next();
            var iter = Expr();
            if (iter is not Seq seq) return null;
            var output = new List<Value>();
            foreach (var item in seq.Items)
                output.Add(Substitute(body, varName, item));
            return new Seq(output);
        }

        private static Value Substitute(Value value, string name,
            Value bound) =>
            value switch
            {
                Ref r when r.Name == name => bound,
                Seq s => new Seq(s.Items
                    .Select(i => Substitute(i, name, bound)).ToList()),
                Dict d => new Dict(d.Entries
                    .Select(e2 => (Substitute(e2.Key, name, bound),
                        Substitute(e2.Val, name, bound))).ToList()),
                Call c => new Call(c.Func,
                    c.Args.Select(a => Substitute(a, name, bound))
                        .ToList(),
                    c.Kw.Select(k => (k.Name,
                        Substitute(k.Val, name, bound))).ToList()),
                _ => value,
            };
    }

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

    private static readonly bool DebugEnabled =
        Environment.GetEnvironmentVariable(
            "GPTBRIDGE_MANIFEST_DEBUG") == "1";

    /// <summary>Evaluate a Python literal expression source.</summary>
    public static Value? Eval(string src,
        IReadOnlyDictionary<string, Value>? env = null)
    {
        var parser = new Parser(src,
            env ?? new Dictionary<string, Value>());
        try
        {
            var value = parser.Expr();
            if (value is null && DebugEnabled)
                Console.Error.WriteLine(
                    $"[pylit] eval failed near pos {parser.FailPos}: " +
                    $"{src[Math.Max(0, parser.FailPos - 30)
                        ..Math.Min(src.Length, parser.FailPos + 50)]
                        .Replace('\n', ' ')}");
            return value;
        }
        catch (FormatException)
        {
            if (DebugEnabled)
            {
                var p = Math.Max(0, parser.FailPos);
                var from = Math.Max(0, p - 80);
                Console.Error.WriteLine(
                    $"[pylit] eval exception near pos " +
                    $"{parser.FailPos}: ..." +
                    $"{src[from..Math.Min(src.Length, p + 60)]
                        .Replace('\n', ' ')}");
            }
            return null;
        }
    }

    /// <summary>Top-level ``NAME = expr`` / ``NAME: T = expr`` values
    /// from a module source (skips every other statement form and
    /// indented bodies).</summary>
    public static Dictionary<string, Value> ModuleConstants(string src)
    {
        var values = new Dictionary<string, Value>(StringComparer.Ordinal);
        var index = 0;
        var length = src.Length;
        while (index < length)
        {
            // scan to next line start
            var lineEnd = src.IndexOf('\n', index);
            if (lineEnd < 0) lineEnd = length;
            var line = src[index..lineEnd];
            var next = lineEnd + 1;
            if (line.Length == 0 || char.IsWhiteSpace(line[0])
                || line.StartsWith('#'))
            { index = next; continue; }
            var match = System.Text.RegularExpressions.Regex.Match(
                line, @"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=\s*");
            if (!match.Success)
            {
                // Multi-line annotation:  ``NAME: Final[\n ...\n] = (``
                if (System.Text.RegularExpressions.Regex.IsMatch(
                        line, @"^[A-Za-z_][A-Za-z0-9_]*\s*:"))
                {
                    var joined = line;
                    var probe = next;
                    for (var extra = 0; extra < 20 && probe < length;
                         extra++)
                    {
                        var je = src.IndexOf('\n', probe);
                        if (je < 0) je = length;
                        var more = src[probe..je];
                        joined += "\n" + more;
                        if (more.Contains('=')) break;
                        probe = je + 1;
                    }
                    match = System.Text.RegularExpressions.Regex.Match(
                        joined,
                        @"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=\s*");
                }
            }
            if (!match.Success || match.Groups[1].Value is "if" or "for"
                or "while" or "def" or "class" or "return" or "import"
                or "from" or "with" or "del" or "try")
            { index = next; continue; }
            var name = match.Groups[1].Value;
            // value may span multiple lines — read until parentheses
            // balance zero at a top-level line start
            var valueStart = index + match.Length;
            var depth = 0;
            var end = valueStart;
            var inStr = false; var strCh = '\0'; var triple = false;
            while (end < length)
            {
                var c = src[end];
                if (inStr)
                {
                    if (triple)
                    {
                        if (c == strCh && end + 2 < length
                            && src[end + 1] == strCh
                            && src[end + 2] == strCh)
                        { inStr = false; end += 3; continue; }
                    }
                    else if (c == strCh
                             && (end == 0 || src[end - 1] != '\\'))
                    { inStr = false; }
                    end++;
                    continue;
                }
                if (c == '#')
                {
                    while (end < length && src[end] != '\n') end++;
                    continue;
                }
                if (c is '"' or '\'')
                {
                    inStr = true; strCh = c;
                    triple = end + 2 < length
                        && src[end + 1] == c && src[end + 2] == c;
                    if (triple) end += 3; else end++;
                    continue;
                }
                if (c is '(' or '[' or '{') depth++;
                else if (c is ')' or ']' or '}') depth--;
                else if (c == '\n' && depth <= 0) break;
                end++;
            }
            var expr = src[valueStart..end].TrimEnd(',', ' ', '\t', '\r');
            var value = Eval(expr, values);
            if (value is not null)
                values[name] = value;
            else if (Environment.GetEnvironmentVariable(
                         "GPTBRIDGE_MANIFEST_DEBUG") == "1")
            {
                Console.Error.WriteLine(
                    $"[pylit] unevaluable constant: {name} " +
                    $"expr={expr[..Math.Min(80, expr.Length)]}...");
                // bisect: report which top-level items fail
                var parts2 = TopLevelItems(expr);
                foreach (var part in parts2)
                {
                    if (Eval(part, values) is null)
                        Console.Error.WriteLine(
                            $"[pylit]   bad item in {name}: " +
                            $"{part[..Math.Min(120, part.Length)]}");
                }
                for (var pi = 1; pi <= parts2.Count; pi++)
                {
                    var prefix = "("
                        + string.Join(",", parts2.Take(pi)) + ",)";
                    if (Eval(prefix, values) is null)
                    {
                        var bad = parts2[pi - 1];
                        var where = Eval(bad, values) is null
                            ? "item" : "tail";
                        Console.Error.WriteLine(
                            $"[pylit]   first failing prefix in " +
                            $"{name} at item {pi - 1} ({where}): " +
                            $"{bad[..Math.Min(200, bad.Length)]}");
                        break;
                    }
                }
            }
            index = end + 1;
        }
        return values;
    }

    /// <summary>Split ``( a, b, c )`` wrapper contents on depth-1
    /// commas — debug bisect helper.</summary>
    private static List<string> TopLevelItems(string expr)
    {
        var result = new List<string>();
        var inner = expr.Trim();
        if (inner.Length > 1 && inner[0] is '(' or '[' or '{')
            inner = inner[1..^1];
        var depth = 0;
        var inStr = false; var strCh = '\0'; var triple = false;
        var start = 0;
        for (var i = 0; i <= inner.Length; i++)
        {
            if (i == inner.Length
                || (depth == 0 && inner[i] == ',' && !inStr))
            {
                var item = inner[start..i].Trim();
                if (item.Length > 0) result.Add(item);
                start = i + 1;
                continue;
            }
            var c = inner[i];
            if (inStr)
            {
                if (triple)
                {
                    if (c == strCh && i + 2 < inner.Length
                        && inner[i + 1] == strCh
                        && inner[i + 2] == strCh)
                    { inStr = false; i += 2; }
                }
                else if (c == strCh && inner[i - 1] != '\\')
                    inStr = false;
                continue;
            }
            if (c is '"' or '\'')
            {
                inStr = true; strCh = c;
                triple = i + 2 < inner.Length
                    && inner[i + 1] == c && inner[i + 2] == c;
                if (triple) i += 2;
            }
            else if (c is '(' or '[' or '{') depth++;
            else if (c is ')' or ']' or '}') depth--;
        }
        return result;
    }

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
