namespace GPTBridge.GitAutomation;

internal static partial class PyLit
{
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
}
