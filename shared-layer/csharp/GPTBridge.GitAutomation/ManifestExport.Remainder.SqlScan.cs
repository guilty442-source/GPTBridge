using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    // ------------------------------------------------------------------
    //  check_sql_anti_patterns scanner — textual port of
    //  audit_sql_patterns.collect_finding_keys.  Position-tolerant
    //  ``category|relpath`` keys; over-approximation surfaces as visible
    //  fail rows (fail-closed), never silent acceptance.
    // ------------------------------------------------------------------

    private static readonly string[] SqlScanRoots =
    {
        "shared-layer/src", "main-system/src-core",
        "main-system/governance", "governance_rule",
        "Standalone tools",
    };

    private static readonly HashSet<string> SqlSkipDirs =
        new(StringComparer.Ordinal)
    {
        "__pycache__", ".venv", "node_modules", "runtime", "build",
        "dist", ".git", "bin", "obj", ".worktrees", "csharp",
        "site-packages",
    };

    private static readonly string[] SqlExemptPathParts =
    {
        "/tests/", "/test_", "regression_benchmarks.py", "/benchmark",
        "/perf/", "performance/", "native_core_benchmark.py",
        "transformer_benchmark.py", "vector_benchmark.py",
        "parser_benchmark.py", "/e2e/",
    };

    private static readonly HashSet<string> SqlExemptFiles =
        new(StringComparer.Ordinal)
    {
        "analytics_schema.py", "analytics_store_schema.py",
        "collab_repo_schema.py", "local_command_parser.py",
        "runtime_queue.py", "migrations.py", "bootstrap.py",
        "provenance.py", "repair_learning.py", "roles.py",
        "session.py", "transport_notify.py", "maintenance_postgres.py",
        "domain.py", "codex_repository.py", "codex_postgresql.py",
        "codex_update_validation.py", "audit_directories.py",
        "successor_framework.py", "store_async.py",
        "data_layer_contract.py", "lineage.py", "_entity_history.py",
    };

    private static readonly Regex StatementNeutral = new(
        @"^\s*(pragma|set\b|listen|unlisten|notify|begin|commit|rollback|"
        + @"savepoint|release|create|alter|drop|analyze|vacuum|attach|"
        + @"detach|explain|truncate|grant|revoke|reindex|checkpoint|"
        + @"cluster)",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex SelectStar = new(
        @"\bselect\s+\*", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex OffsetRe = new(
        @"\boffset\b", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex ConnReceiver = new(
        @"conn|cur|cursor|connection|session|admin|db\b|store",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex SqlWords = new(
        @"\b(select|insert|update|delete|from|where)\b",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex ExecuteCall = new(
        @"(?:(?<recv>[A-Za-z_][A-Za-z0-9_]*)\.)?"
        + @"(?<fn>execute|executemany)\s*\(",
        RegexOptions.Compiled);
    private static readonly Regex ConnectCall = new(
        @"\b(?:connect|_connect|_get_conn|get_connection|connection)"
        + @"\s*\(", RegexOptions.Compiled);
    private static readonly Regex FStringRe = new(
        @"[fF][""'][^""']*\{(?!\{)", RegexOptions.Compiled);
    private static readonly Regex QuotedStr = new(
        "\"\"\"(?s:.)*?\"\"\"|'''(?s:.)*?'''"
        + "|\"([^\"\\\\]|\\\\.)*\"|'([^'\\\\]|\\\\.)*'",
        RegexOptions.Compiled);
    private static readonly HashSet<string> DmlVerbs =
        new(StringComparer.Ordinal)
    {
        "select", "insert", "update", "delete", "replace", "upsert",
    };
    private static readonly string[] DmlPrefixes =
    {
        "select", "insert", "update", "delete", "replace", "upsert",
        "insert into", "insert or",
    };

    private static List<string> CollectFindingKeys(string root)
    {
        var keys = new List<string>();
        foreach (var scanRoot in SqlScanRoots)
        {
            var baseDir = Rel(root, scanRoot);
            if (!Directory.Exists(baseDir)) continue;
            foreach (var path in Directory
                         .EnumerateFiles(baseDir, "*.py",
                             SearchOption.AllDirectories))
            {
                var parts = path.Split(Path.DirectorySeparatorChar);
                if (parts.Any(SqlSkipDirs.Contains)) continue;
                var rel = Path.GetRelativePath(root, path)
                    .Replace('\\', '/');
                foreach (var finding in ScanSqlFile(path, rel))
                {
                    var space = finding.IndexOf(' ');
                    keys.Add(
                        $"{(space > 0 ? finding[..space] : finding)}" +
                        $"|{rel}");
                }
            }
        }
        keys.Sort(StringComparer.Ordinal);
        return keys;
    }

    private static List<string> ScanSqlFile(string path, string rel)
    {
        var findings = new List<string>();
        string source;
        try { source = File.ReadAllText(path); }
        catch (Exception ex) when (ex is IOException
                                 or UnauthorizedAccessException)
        {
            return new List<string>
                { $"sql-scan unreadable {rel}: {ex.Message}" };
        }
        if (!source.Contains("execute", StringComparison.Ordinal)
            && !source.Contains("connect", StringComparison.Ordinal))
            return findings;
        var lines = source.Replace("\r\n", "\n").Split('\n');

        var constants = new Dictionary<string, string>(
            StringComparer.Ordinal);
        foreach (var raw in lines)
        {
            var m = Regex.Match(raw,
                @"^\s*([A-Z_][A-Za-z0-9_]*)\s*=\s*"
                + @"(?:""([^""\\]*(?:\\.[^""\\]*)*)""|"
                + @"'([^'\\]*(?:\\.[^'\\]*)*)')");
            if (m.Success)
                constants[m.Groups[1].Value] =
                    m.Groups[2].Success ? m.Groups[2].Value
                                        : m.Groups[3].Value;
        }

        var exemptFile =
            SqlExemptPathParts.Any(p =>
                ("/" + rel).Contains(p, StringComparison.Ordinal))
            || SqlExemptFiles.Contains(Path.GetFileName(path));

        // Reconstruct physical line numbers for merged logical lines;
        // Pieces tracks (physicalLine, offsetInText) per appended
        // source line so findings can report the *call* line — Python
        // uses the call node's lineno, not the span's first line.
        var logicalSpans = new List<(int Line, string Text, int Depth,
            List<(int Line, int Off)> Pieces)>();
        {
            var phys = 0;
            var buf = new System.Text.StringBuilder();
            var pieces = new List<(int Line, int Off)>();
            var bracket = 0;
            var startLine = 0;
            var firstIndent = 0;
            foreach (var raw in lines)
            {
                phys++;
                var t = raw.Trim();
                if (buf.Length == 0 && t.Length == 0) continue;
                if (buf.Length == 0)
                {
                    startLine = phys;
                    var fi = 0;
                    while (fi < raw.Length && raw[fi] == ' ') fi++;
                    firstIndent = fi / 4;
                }
                pieces.Add((phys, buf.Length));
                buf.Append(t).Append(' ');
                var plain = StripStrings(raw);
                bracket += plain.Count(c => c is '(' or '[' or '{')
                           - plain.Count(c => c is ')' or ']' or '}');
                if (bracket <= 0)
                {
                    logicalSpans.Add((startLine, buf.ToString(),
                        firstIndent, pieces));
                    buf.Clear();
                    pieces = new List<(int, int)>();
                    bracket = 0;
                }
            }
        }

        bool Suppressed(int lineno) =>
            lineno > 0 && lineno <= lines.Length
            && lines[lineno - 1].Contains("# sql-ok",
                StringComparison.Ordinal);

        // inside-loop tracking via for-header depth stack
        var forDepths = new Stack<int>();
        for (var i = 0; i < logicalSpans.Count; i++)
        {
            var (lineno, text, depth, pieces) = logicalSpans[i];
            // physical line holding the text offset (call-site parity)
            int CallLine(int idx)
            {
                var line = lineno;
                foreach (var (pl, off) in pieces)
                {
                    if (off > idx) break;
                    line = pl;
                }
                return line;
            }
            var trimmed = text.Trim();
            var isFor = Regex.IsMatch(trimmed,
                @"^(async\s+)?for\b.*:$");
            if (isFor)
            {
                while (forDepths.Count > 0 && depth <= forDepths.Peek())
                    forDepths.Pop();
                forDepths.Push(depth);
                continue;
            }
            var isClause = Regex.IsMatch(trimmed,
                @"^(else|elif\b.*|except\b.*|finally)\s*:");
            if (!(isClause && forDepths.Count > 0
                  && depth == forDepths.Peek()))
                while (forDepths.Count > 0 && depth <= forDepths.Peek())
                    forDepths.Pop();
            var insideLoop = forDepths.Count > 0;

            // with ...connect() inside a loop
            if (insideLoop
                && Regex.IsMatch(trimmed, @"^(async\s+)?with\b")
                && ConnectCall.IsMatch(trimmed)
                && !Suppressed(lineno) && !exemptFile)
                findings.Add(
                    $"sql-conn-loop {rel}:{lineno} " +
                    "connect() inside for-loop " +
                    "(per-row commit/persist)");

            foreach (Match call in ExecuteCall.Matches(text))
            {
                var recv = call.Groups["recv"].Value;
                if (call.Groups["recv"].Success
                    && !ConnReceiver.IsMatch(recv))
                    continue;
                var callLine = CallLine(call.Index);
                var argsText = CallArgs(text,
                    call.Index + call.Length - 1);

                // f-string interpolation — Python parity: only args
                // that *are* JoinedStr (a bare f-string literal) are
                // checked; f-strings nested in tuples/calls do not
                // count. Each interpolated arg whose joined constant
                // fragments are not statement-neutral is flagged once.
                foreach (var rawArg in TopLevelArgs(argsText))
                {
                    var arg = rawArg.Trim();
                    if (arg.Length < 2 || arg[0] is not ('f' or 'F')
                        || arg[1] is not ('"' or '\''))
                        continue;
                    if (!Regex.IsMatch(arg, @"\{(?!\{)")) continue;
                    var constText = FStringConstant(arg, 0);
                    if (constText is null
                        || StatementNeutral.IsMatch(constText))
                        continue;
                    if (!Suppressed(callLine) && !exemptFile)
                        findings.Add(
                            $"sql-fstring {rel}:{callLine} " +
                            "execute() argument interpolates variables");
                }

                // literal SQL strings in args — Constant parity:
                // strings carrying an f/F prefix are JoinedStr
                // internals and are not counted by _sql_strings.
                foreach (Match sq in QuotedStr.Matches(argsText))
                {
                    if (sq.Index > 0
                        && argsText[sq.Index - 1] is 'f' or 'F')
                        continue;
                    var body = sq.Value.StartsWith("\"\"\"",
                            StringComparison.Ordinal)
                        || sq.Value.StartsWith("'''",
                            StringComparison.Ordinal)
                        ? sq.Value[3..^3]
                        : sq.Value[1..^1];
                    if (!SqlWords.IsMatch(body)) continue;
                    if (SelectStar.IsMatch(body)
                        && !Suppressed(callLine) && !exemptFile)
                        findings.Add(
                            $"sql-select-star {rel}:{callLine} " +
                            "SELECT * read");
                    if (OffsetRe.IsMatch(body) && !Suppressed(callLine))
                        findings.Add(
                            $"sql-offset {rel}:{callLine} " +
                            "OFFSET pagination");
                }

                // DML inside a for loop — first *positional* arg only;
                // a leading ``name=`` arg means no positional arg
                // exists (Python: node.args empty → check skipped).
                var firstArg = TopLevelArgs(argsText)[0].Trim();
                if (insideLoop && !exemptFile && !Suppressed(callLine)
                    && !Regex.IsMatch(firstArg, @"^[A-Za-z_]\w*\s*="))
                {
                    string? sqlText = null;
                    var qm = QuotedStr.Match(firstArg);
                    if (qm.Success && qm.Index == 0)
                        sqlText = qm.Value.StartsWith("\"\"\"",
                                StringComparison.Ordinal)
                            || qm.Value.StartsWith("'''",
                                StringComparison.Ordinal)
                            ? qm.Value[3..^3]
                            : qm.Value[1..^1];
                    else
                    {
                        var lookup = firstArg.Contains('.')
                            ? firstArg.Split('.').Last()
                            : firstArg;
                        if (constants.TryGetValue(lookup, out var cv))
                            sqlText = cv;
                    }
                    var isDml = sqlText is null
                        || (!StatementNeutral.IsMatch(sqlText)
                            && IsDml(sqlText));
                    if (isDml)
                        findings.Add(
                            $"sql-loop-exec {rel}:{callLine} " +
                            "execute() inside for-loop " +
                            "(N+1 candidate)");
                }
            }
        }
        return findings;
    }

    /// <summary>Split a call-argument span on depth-0 commas
    /// (paren/bracket/brace/quote aware).</summary>
    private static List<string> TopLevelArgs(string argsText)
    {
        var args = new List<string>();
        var depth = 0;
        var start = 0;
        char q = '\0';
        for (var i = 0; i < argsText.Length; i++)
        {
            var c = argsText[i];
            if (q != '\0')
            {
                if (c == '\\') { i++; continue; }
                if (c == q) q = '\0';
                continue;
            }
            if (c is '"' or '\'') { q = c; continue; }
            if (c is '(' or '[' or '{') depth++;
            else if (c is ')' or ']' or '}') depth--;
            else if (c == ',' && depth <= 0)
            {
                args.Add(argsText[start..i]);
                start = i + 1;
            }
        }
        args.Add(argsText[start..]);
        return args;
    }

    /// <summary>Join the constant fragments of the f-string literal
    /// starting at <paramref name="start"/> — Python parity for
    /// ``"".join(v.value for v in arg.values if Constant)``.</summary>
    private static string? FStringConstant(string text, int start)
    {
        var i = start;
        if (i >= text.Length || text[i] is not ('f' or 'F')) return null;
        i++;
        if (i >= text.Length || text[i] is not ('"' or '\''))
            return null;
        var quote = text[i++];
        var sb = new System.Text.StringBuilder();
        while (i < text.Length)
        {
            var c = text[i];
            if (c == '\\' && i + 1 < text.Length)
            { sb.Append(text[i + 1]); i += 2; continue; }
            if (c == quote) return sb.ToString();
            if (c == '{')
            {
                if (i + 1 < text.Length && text[i + 1] == '{')
                { sb.Append('{'); i += 2; continue; }
                var depth = 1;
                i++;
                while (i < text.Length && depth > 0)
                {
                    if (text[i] == '{') depth++;
                    else if (text[i] == '}') depth--;
                    i++;
                }
                continue;
            }
            if (c == '}')
            {
                if (i + 1 < text.Length && text[i + 1] == '}')
                { sb.Append('}'); i += 2; continue; }
                i++; continue;
            }
            sb.Append(c);
            i++;
        }
        return null;
    }

    private static bool IsDml(string sql)
    {
        var head = sql.TrimStart().ToLowerInvariant();
        var first = head.Split(new[] { ' ', '(' }, 2)[0];
        return DmlVerbs.Contains(first)
               || DmlPrefixes.Any(p => head.StartsWith(
                   p, StringComparison.Ordinal));
    }

    private static string StripStrings(string line)
    {
        var sb = new System.Text.StringBuilder(line.Length);
        var inStr = false; var ch = '\0'; var triple = false;
        for (var i = 0; i < line.Length; i++)
        {
            var c = line[i];
            if (inStr)
            {
                if (triple)
                {
                    if (c == ch && i + 2 < line.Length
                        && line[i + 1] == ch && line[i + 2] == ch)
                    { inStr = false; i += 2; }
                }
                else if (c == ch && line[i - 1] != '\\') inStr = false;
                continue;
            }
            if (c is '"' or '\'')
            {
                inStr = true; ch = c;
                triple = i + 2 < line.Length
                    && line[i + 1] == c && line[i + 2] == c;
                if (triple) i += 2;
                continue;
            }
            if (c == '#') break;
            sb.Append(c);
        }
        return sb.ToString();
    }

    /// <summary>Args text of the call whose ``(`` is at index.</summary>
    private static string CallArgs(string text, int openParen)
    {
        var depth = 0;
        for (var i = openParen; i < text.Length; i++)
        {
            var c = text[i];
            if (c == '(') depth++;
            else if (c == ')')
            {
                depth--;
                if (depth == 0)
                    return text[(openParen + 1)..i];
            }
        }
        return text[(openParen + 1)..];
    }
}
