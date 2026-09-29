namespace GPTBridge.GitAutomation;

internal static partial class PyLit
{
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
}
