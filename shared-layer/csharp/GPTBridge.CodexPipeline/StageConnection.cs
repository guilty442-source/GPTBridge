using System.Text.RegularExpressions;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// sqlite3-compatible surface bound to one PostgreSQL stage schema —
/// direct port of codex_postgresql_stage.StageConnection.
///
/// Translates the codex pipeline's bounded sqlite dialect (``?``
/// placeholders, ``sqlite_master`` catalog reads, ``PRAGMA
/// table_info``/``integrity_check``/``quick_check``/``foreign_key_check``/
/// ``database_list``, ``INSERT OR REPLACE``/``INSERT OR IGNORE``,
/// ``CREATE VIRTUAL TABLE ... USING fts5`` and ``ORDER BY rowid``) onto
/// psycopg-identical PostgreSQL statements, then onto Npgsql ``@p``
/// binding.
/// </summary>
internal sealed class StageConnection : IDisposable
{
    private static readonly Regex VirtualFts = new(
        @"^\s*CREATE\s+VIRTUAL\s+TABLE\s+([\w$]+)\s+USING\s+fts5\s*\(([^)]*)\)",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex PragmaTableInfo = new(
        @"^\s*PRAGMA\s+table_info\s*\(\s*['""]?([\w$]+)['""]?\s*\)\s*;?\s*$",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex PragmaDatabaseList = new(
        @"^\s*PRAGMA\s+database_list\s*;?\s*$",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex PragmaFkCheck = new(
        @"^\s*PRAGMA\s+foreign_key_check\s*;?\s*$",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex PragmaIntegrity = new(
        @"^\s*PRAGMA\s+(integrity_check|quick_check)\s*;?\s*$",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex PragmaOther = new(
        @"^\s*PRAGMA\b",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex InsertOrReplace = new(
        @"^\s*INSERT\s+OR\s+REPLACE\s+INTO\b",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex InsertOrIgnore = new(
        @"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex OrderByRowid = new(
        @"ORDER\s+BY\s+rowid\b",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex IsNotPlaceholder = new(
        @"\bIS\s+NOT\s+\?",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex IsPlaceholder = new(
        @"\bIS\s+\?",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex InsertTarget = new(
        @"INSERT\s+INTO\s+""?([\w$]+)""?",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    public string Schema { get; }

    private readonly NpgsqlConnection _connection;
    private NpgsqlTransaction? _transaction;

    /// <summary>Truthy marker (sqlite3.Row parity) → rows indexable by
    /// column name.</summary>
    public bool RowFactory { get; set; }

    public StageConnection(string schema, bool @readonly = false)
        : this(schema, PgDsn.AdminDsn(), true)
    {
    }

    /// <summary>``dsn`` override lets the read-only authority reuse the
    /// same dialect facade (``search_path`` pins the live schema);
    /// ``transactional=false`` maps psycopg autocommit.</summary>
    internal StageConnection(string schema, string dsn,
        bool transactional)
    {
        if (!PgDsn.Identifier.IsMatch(schema))
            throw new InvalidOperationException(
                $"STAGE_SCHEMA_NAME_INVALID:{schema}");
        Schema = schema;
        _connection = new NpgsqlConnection(PgDsn.Normalize(dsn));
        _connection.Open();
        _transaction = transactional
            ? _connection.BeginTransaction() : null;
        using var command = new NpgsqlCommand(
            $"SET search_path TO \"{schema}\", pg_catalog",
            _connection, _transaction);
        command.ExecuteNonQuery();
    }

    // -- dialect translation ---------------------------------------------

    internal string Translate(string statement)
    {
        var text = statement;
        var virtualFts = VirtualFts.Match(text);
        if (virtualFts.Success)
        {
            var name = virtualFts.Groups[1].Value;
            var defs = virtualFts.Groups[2].Value.Split(',')
                .Select(c => $"\"{c.Trim()}\" TEXT");
            return $"CREATE TABLE \"{name}\" ({string.Join(", ", defs)})";
        }
        var tableInfo = PragmaTableInfo.Match(text);
        if (tableInfo.Success)
        {
            var table = tableInfo.Groups[1].Value;
            return
                "SELECT ordinal_position - 1 AS cid, column_name AS name, "
                + "data_type AS type, CASE WHEN is_nullable='NO' THEN 1 "
                + "ELSE 0 END AS notnull, column_default AS dflt_value, "
                + "CASE WHEN column_name IN (SELECT a.attname FROM "
                + "pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid "
                + "AND a.attnum=ANY(i.indkey) WHERE "
                + $"i.indrelid=(current_schema()||'.'||'{table}')"
                + "::regclass "
                + "AND i.indisprimary) THEN 1 ELSE 0 END AS pk "
                + "FROM information_schema.columns WHERE "
                + "table_schema=current_schema() AND "
                + $"table_name='{table}' ORDER BY ordinal_position";
        }
        if (PragmaFkCheck.IsMatch(text))
            return "__fk_check__";
        if (PragmaIntegrity.IsMatch(text))
            return "__integrity_ok__";
        if (PragmaDatabaseList.IsMatch(text))
            return $"SELECT 'main', '{Schema}', '{Schema}'";
        if (PragmaOther.IsMatch(text))
            return "SELECT 1 WHERE false";
        if (text.Contains("sqlite_master", StringComparison.Ordinal))
        {
            text = text
                .Replace("sqlite_master", "information_schema.tables")
                .Replace("name", "table_name");
            if (text.Contains("type='table'", StringComparison.Ordinal))
                text = text.Replace("type='table'",
                    "table_type='BASE TABLE' AND "
                    + "table_schema=current_schema()");
        }
        text = OrderByRowid.Replace(text, "ORDER BY 1");
        // sqlite ``IS ?``/``IS NOT ?`` NULL-safe equality → PostgreSQL
        // DISTINCT FROM forms; a bare ``IS $n`` is a syntax error.
        text = IsNotPlaceholder.Replace(text, "IS DISTINCT FROM ?");
        text = IsPlaceholder.Replace(text, "IS NOT DISTINCT FROM ?");
        var orReplace = InsertOrReplace.Match(text);
        var orIgnore = InsertOrIgnore.Match(text);
        if (orReplace.Success || orIgnore.Success)
        {
            text = (orReplace.Success ? InsertOrReplace : InsertOrIgnore)
                .Replace(text, "INSERT INTO", 1);
            var pk = PrimaryKeysForStatement(text);
            if (pk.Count > 0)
                text += " ON CONFLICT ("
                    + string.Join(", ",
                        pk.Select(c => $"\"{c}\""))
                    + ") DO NOTHING";
            return Placeholders(text);
        }
        return Placeholders(text);
    }

    /// <summary>psycopg placeholder pass: ``%`` → ``%%`` unless
    /// %[sbt], then ``?`` → ``%s`` (parity, blind replace).</summary>
    private static string Placeholders(string statement) =>
        Regex.Replace(statement, "%(?![sbt])", "%%")
            .Replace("?", "%s");

    /// <summary>Convert psycopg-style ``%s``/``%b``/``%t`` placeholders
    /// to Npgsql ``@pN`` positionals; ``%%`` unescapes to ``%``.</summary>
    internal static string ToNpgsqlText(string psycopg)
    {
        var builder = new System.Text.StringBuilder(psycopg.Length + 8);
        var index = 0;
        var parameter = 0;
        while (index < psycopg.Length)
        {
            var c = psycopg[index];
            if (c == '%' && index + 1 < psycopg.Length)
            {
                var next = psycopg[index + 1];
                if (next == '%') { builder.Append('%'); index += 2; continue; }
                if (next is 's' or 'b' or 't')
                {
                    builder.Append("@p").Append(parameter++);
                    index += 2;
                    continue;
                }
            }
            builder.Append(c);
            index += 1;
        }
        return builder.ToString();
    }

    private List<string> PrimaryKeysForStatement(string statement)
    {
        var match = InsertTarget.Match(statement);
        if (!match.Success)
            return new List<string>();
        try
        {
            return StageCodec.PrimaryKeyColumns(
                _connection, Schema, match.Groups[1].Value);
        }
        catch (PostgresException)
        {
            return new List<string>();
        }
    }

    private NpgsqlCommand NewCommand(string psycopgStatement,
        IReadOnlyList<object?>? parameters)
    {
        var command = new NpgsqlCommand(ToNpgsqlText(psycopgStatement),
            _connection, _transaction);
        if (parameters is not null)
            for (var i = 0; i < parameters.Count; i++)
                command.Parameters.Add(
                    new NpgsqlParameter($"p{i}", parameters[i] ?? DBNull.Value)
                );
        return command;
    }

    // -- DB-API surface -----------------------------------------------------

    public StageCursor Execute(string statement,
        IReadOnlyList<object?>? parameters = null)
    {
        var translated = Translate(statement);
        if (translated == "__fk_check__")
            return new StageCursor(ForeignKeyViolations()
                .Select(v => (object?[])v).ToList());
        if (translated == "__integrity_ok__")
            return new StageCursor(
                new List<object?[]> { new object?[] { "ok" } });
        using var command = NewCommand(translated, parameters);
        var rowcount = -1;
        var rows = new List<object?[]>();
        var columns = new List<string>();
        try
        {
            using var reader = command.ExecuteReader();
            for (var i = 0; i < reader.FieldCount; i++)
                columns.Add(reader.GetName(i));
            while (reader.Read())
            {
                var row = new object?[reader.FieldCount];
                for (var i = 0; i < row.Length; i++)
                    row[i] = reader.IsDBNull(i) ? null : reader.GetValue(i);
                rows.Add(row);
            }
            rowcount = reader.RecordsAffected >= 0
                ? (int)reader.RecordsAffected : rows.Count;
        }
        catch (PostgresException) when (!command.CommandText.TrimStart()
            .StartsWith("SELECT", StringComparison.OrdinalIgnoreCase))
        {
            throw;
        }
        if (columns.Count == 0)
            rowcount = Math.Max(rowcount, 0);
        return new StageCursor(rows, columns, rowcount);
    }

    public StageCursor Executemany(string statement,
        IReadOnlyList<IReadOnlyList<object?>> rows)
    {
        var translated = ToNpgsqlText(Translate(statement));
        var affected = 0;
        // Projection rebuilds stage tens of thousands of rows; batch
        // parameter sets into one round-trip per chunk instead of one
        // command per row.
        const int batchRows = 256;
        for (var offset = 0; offset < rows.Count; offset += batchRows)
        {
            using var batch = new NpgsqlBatch(_connection, _transaction);
            var limit = Math.Min(batchRows, rows.Count - offset);
            for (var i = 0; i < limit; i++)
            {
                var row = rows[offset + i];
                var command = new NpgsqlBatchCommand(translated);
                for (var p = 0; p < row.Count; p++)
                    command.Parameters.Add(new NpgsqlParameter(
                        $"p{p}", row[p] ?? DBNull.Value));
                batch.BatchCommands.Add(command);
            }
            affected += Math.Max(batch.ExecuteNonQuery(), 0);
        }
        return new StageCursor(new List<object?[]>(), new(), affected);
    }

    /// <summary>Parity with Python ``executescript`` — naive ``;`` split,
    /// raw chunks (no translation).</summary>
    public void Executescript(string script)
    {
        foreach (var chunk in script.Split(';'))
        {
            if (chunk.Trim().Length == 0)
                continue;
            // sql-ok: caller-composed DDL batch
            using var command = new NpgsqlCommand(chunk, _connection,
                _transaction);
            command.ExecuteNonQuery();
        }
    }

    public void Commit()
    {
        // sqlite parity: commit ends the transaction; further
        // statements run in autocommit until the next commit.
        var transaction = _transaction;
        _transaction = null;
        transaction?.Commit();
    }

    public void Rollback()
    {
        var transaction = _transaction;
        _transaction = null;
        transaction?.Rollback();
    }
    public void Dispose() { _transaction?.Dispose(); _connection.Dispose(); }
    public void Close() => Dispose();

    // -- integrity probes ---------------------------------------------------

    private List<string[]> ForeignKeyViolations()
    {
        var constraints = new List<(string Name, string Child,
            string Parent, int[] Conkey, int[] Confkey)>();
        using (var command = new NpgsqlCommand(
            "SELECT con.conname, con.conrelid::regclass::text, "
            + "con.confrelid::regclass::text, con.conkey, con.confkey "
            + "FROM pg_constraint con JOIN pg_namespace n "
            + "ON n.oid=con.connamespace WHERE con.contype='f' "
            + "AND n.nspname=current_schema()",
            _connection, _transaction))
        using (var reader = command.ExecuteReader())
            while (reader.Read())
                constraints.Add((reader.GetString(0), reader.GetString(1),
                    reader.GetString(2),
                    (int[])reader.GetValue(3), (int[])reader.GetValue(4)));
        var violations = new List<string[]>();
        foreach (var (name, child, parent, conkey, confkey) in constraints)
        {
            var childCols = Attnames(child, conkey);
            var parentCols = Attnames(parent, confkey);
            if (childCols.Count == 0 || parentCols.Count == 0)
                continue;
            var join = string.Join(" AND ", childCols.Zip(parentCols)
                .Select(pair =>
                    $"c.\"{pair.First}\" IS NOT DISTINCT FROM p.\"{pair.Second}\""));
            var keyRepr = string.Join("|| '|' ||", childCols
                .Select(c => $"COALESCE(c.\"{c}\"::text,'')"));
            var childTable = child.Split('.')[^1];
            var parentTable = parent.Split('.')[^1];
            // sql-ok: identifiers from pg_constraint catalog
            using var command = new NpgsqlCommand(
                $"SELECT '{child}', {keyRepr}, '{name}', '{parent}' "
                + $"FROM \"{childTable}\" c WHERE NOT ("
                + $"SELECT 1 FROM \"{parentTable}\" p WHERE {join})",
                _connection, _transaction);
            using var reader = command.ExecuteReader();
            while (reader.Read())
            {
                var row = new string[reader.FieldCount];
                for (var i = 0; i < row.Length; i++)
                    row[i] = reader.IsDBNull(i) ? ""
                        : reader.GetValue(i).ToString() ?? "";
                violations.Add(row);
            }
        }
        return violations;
    }

    private List<string> Attnames(string relation, int[] attnums)
    {
        using var command = new NpgsqlCommand(
            "SELECT attname FROM pg_attribute WHERE attrelid=@r::regclass "
            + "AND attnum=ANY(@n) ORDER BY attnum",
            _connection, _transaction);
        command.Parameters.AddWithValue("r", relation);
        command.Parameters.AddWithValue("n", attnums);
        var names = new List<string>();
        using var reader = command.ExecuteReader();
        while (reader.Read())
            names.Add(reader.GetString(0));
        return names;
    }
}

/// <summary>sqlite3 Cursor equivalent — materialized row list.</summary>
internal sealed class StageCursor
{
    private readonly List<object?[]> _rows;
    private readonly List<string> _columns;
    private int _index;

    public int RowCount { get; }

    public StageCursor(List<object?[]> rows,
        List<string>? columns = null, int rowcount = -1)
    {
        _rows = rows;
        _columns = columns ?? new List<string>();
        RowCount = rowcount < 0 ? rows.Count : rowcount;
    }

    public IReadOnlyList<string> Columns => _columns;

    public object?[]? FetchOne() =>
        _index < _rows.Count ? _rows[_index++] : null;

    public List<object?[]> FetchAll() => new(_rows);

    public int ColumnIndex(string name) =>
        _columns.FindIndex(c =>
            string.Equals(c, name, StringComparison.OrdinalIgnoreCase));

    public IEnumerable<object?[]> Rows => _rows;
}
