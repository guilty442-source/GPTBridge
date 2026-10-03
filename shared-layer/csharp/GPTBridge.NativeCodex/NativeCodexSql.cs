using System.Text.Json;
using System.Text.RegularExpressions;

namespace GPTBridge.CodexPipeline;

/// <summary>Bounded, process-local SELECT execution over a generation-pinned snapshot.</summary>
public sealed class NativeCodexSql
{
    private readonly NativeCodexSnapshot snapshot;
    public NativeCodexSql(NativeCodexSnapshot snapshot) => this.snapshot = snapshot;

    public IReadOnlyList<IReadOnlyDictionary<string, JsonElement>> Query(string sql,
        IReadOnlyDictionary<string, object?>? parameters = null, int maxScannedRows = 100000)
    {
        if (sql.Length > 16384 || maxScannedRows is < 1 or > 1000000)
            throw Error("BOUNDS");
        var parser = new Parser(sql);
        parser.Expect("SELECT");
        var columns = parser.List();
        parser.Expect("FROM");
        var table = parser.Identifier();
        var filters = new List<(string Column, string Op, object? Value)>();
        if (parser.Take("WHERE"))
        {
            do
            {
                var column = parser.Identifier();
                var op = parser.Next();
                if (op is not ("=" or "<>" or "!=" or "<" or ">" or "<=" or ">=")) throw Error("OPERATOR");
                filters.Add((column, op, parser.Value(parameters)));
                if (filters.Count > 32) throw Error("BOUNDS");
            } while (parser.Take("AND"));
        }
        var ordering = new List<(string Column, bool Descending)>();
        if (parser.Take("ORDER"))
        {
            parser.Expect("BY");
            do
            {
                var column = parser.Identifier();
                var descending = parser.Take("DESC");
                if (!descending) parser.Take("ASC");
                ordering.Add((column, descending));
                if (ordering.Count > 16) throw Error("BOUNDS");
            } while (parser.Take(","));
        }
        var limit = 1000;
        if (parser.Take("LIMIT"))
        {
            var value = parser.Value(parameters);
            if (value is not decimal number || number != decimal.Truncate(number) || number is < 0 or > 10000)
                throw Error("LIMIT");
            limit = (int)number;
        }
        parser.Take(";");
        if (!parser.End) throw Error("UNSUPPORTED_SYNTAX");
        var rows = snapshot.Rows(table);
        if (rows.Count > maxScannedRows) throw Error("SCAN_LIMIT");
        var matched = new List<JsonElement>();
        foreach (var row in rows)
        {
            foreach (var column in columns.Where(c => c != "*").Concat(filters.Select(f => f.Column)).Concat(ordering.Select(o => o.Column)))
                _ = Column(row, column);
            if (filters.All(filter => Matches(Column(row, filter.Column), filter.Op, filter.Value)))
                matched.Add(row);
        }
        if (ordering.Count > 0)
            matched = matched.Select((row, index) => (row, index)).OrderBy(item => item,
                Comparer<(JsonElement row, int index)>.Create((left, right) =>
                {
                    foreach (var order in ordering)
                    {
                        var comparison = Compare(Scalar(Column(left.row, order.Column)), Scalar(Column(right.row, order.Column)));
                        if (comparison != 0) return order.Descending ? -comparison : comparison;
                    }
                    return left.index.CompareTo(right.index);
                })).Select(item => item.row).ToList();
        return matched.Take(limit).Select(row => (IReadOnlyDictionary<string, JsonElement>)
            new System.Collections.ObjectModel.ReadOnlyDictionary<string, JsonElement>(columns[0] == "*"
                ? row.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.Clone(), StringComparer.Ordinal)
                : columns.ToDictionary(column => column, column => Column(row, column).Clone(), StringComparer.Ordinal))).ToArray();
    }

    private static JsonElement Column(JsonElement row, string column) => row.TryGetProperty(column, out var value)
        ? value : throw Error("COLUMN_MISSING:" + column);
    private static object? Scalar(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Null => null,
        JsonValueKind.String => value.GetString(),
        JsonValueKind.Number when value.TryGetDecimal(out var number) => number,
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        _ => throw Error("VALUE_UNSUPPORTED")
    };
    private static int Compare(object? left, object? right)
    {
        if (left is null) return right is null ? 0 : 1; // NULLS LAST for ASC.
        if (right is null) return -1;
        if (left is decimal a && right is decimal b) return a.CompareTo(b);
        if (left is string x && right is string y) return StringComparer.Ordinal.Compare(x, y);
        if (left is bool p && right is bool q) return p.CompareTo(q);
        throw Error("TYPE_MISMATCH");
    }
    private static bool Matches(JsonElement field, string op, object? value)
    {
        var actual = Scalar(field);
        if (actual is null || value is null) return false;
        var comparison = Compare(actual, value);
        return op switch { "=" => comparison == 0, "<>" or "!=" => comparison != 0,
            "<" => comparison < 0, ">" => comparison > 0, "<=" => comparison <= 0, ">=" => comparison >= 0,
            _ => throw Error("OPERATOR") };
    }
    private static InvalidDataException Error(string reason) => new("NATIVE_SQL_" + reason);

    private sealed class Parser
    {
        private readonly List<string> tokens = new();
        private int position;
        public bool End => position == tokens.Count;
        public Parser(string sql)
        {
            const string pattern = "\\s+|\"(?:[^\"]|\"\")+\"|'(?:[^']|'')*'|@[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*|-?[0-9]+(?:\\.[0-9]+)?|<=|>=|<>|!=|[*,;=<>]";
            var consumed = 0;
            foreach (Match match in Regex.Matches(sql, pattern, RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)))
            {
                if (match.Index != consumed) throw Error("UNSUPPORTED_SYNTAX");
                consumed += match.Length;
                if (!string.IsNullOrWhiteSpace(match.Value)) tokens.Add(match.Value);
            }
            if (consumed != sql.Length || tokens.Count > 256) throw Error("UNSUPPORTED_SYNTAX");
        }
        public string Next() => End ? throw Error("INCOMPLETE_SYNTAX") : tokens[position++];
        public bool Take(string value)
        {
            if (End || !tokens[position].Equals(value, StringComparison.OrdinalIgnoreCase)) return false;
            position++; return true;
        }
        public void Expect(string value) { if (!Take(value)) throw Error("EXPECTED:" + value); }
        public string Identifier()
        {
            var token = Next();
            if (token.StartsWith('"')) return token[1..^1].Replace("\"\"", "\"");
            if (!Regex.IsMatch(token, "^[A-Za-z_][A-Za-z0-9_]*$", RegexOptions.CultureInvariant)) throw Error("IDENTIFIER");
            return token.ToLowerInvariant();
        }
        public List<string> List()
        {
            if (Take("*")) return new() { "*" };
            var columns = new List<string>();
            do { columns.Add(Identifier()); } while (Take(",") && columns.Count <= 128);
            if (columns.Count > 128 || columns.Distinct(StringComparer.Ordinal).Count() != columns.Count) throw Error("PROJECTION");
            return columns;
        }
        public object? Value(IReadOnlyDictionary<string, object?>? parameters)
        {
            var token = Next();
            if (token.StartsWith('@'))
            {
                if (parameters is null || !parameters.TryGetValue(token[1..], out var value)) throw Error("PARAMETER_MISSING");
                return Scalar(JsonSerializer.SerializeToElement(value));
            }
            if (token.StartsWith('\'')) return token[1..^1].Replace("''", "'");
            if (token.Equals("NULL", StringComparison.OrdinalIgnoreCase)) return null;
            if (token.Equals("TRUE", StringComparison.OrdinalIgnoreCase)) return true;
            if (token.Equals("FALSE", StringComparison.OrdinalIgnoreCase)) return false;
            if (decimal.TryParse(token, System.Globalization.NumberStyles.AllowLeadingSign | System.Globalization.NumberStyles.AllowDecimalPoint,
                System.Globalization.CultureInfo.InvariantCulture, out var number)) return number;
            throw Error("VALUE_UNSUPPORTED");
        }
    }
}
