using System.Text;
using System.Text.RegularExpressions;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// PostgreSQL staging-schema lifecycle and ``.sql`` artifact codec —
/// byte-identical port of codex_postgresql_stage.py.
///
/// The reviewable/digestable artifact is a deterministic ``.sql`` dump:
/// one statement per line, ``E''``-escaped literals, pk-ordered rows.
/// Fail-closed: no DSN, no schema, no mutation of the live authority —
/// writes only ever land inside ``*_stage_*`` schemas.
/// </summary>
internal static partial class StageCodec
{
    public const string StagePrefix = "codex_stage_";
    public const string ArtifactHeader = "-- gptbridge-codex-artifact/v1";

    private static string Q(string identifier) =>
        "\"" + identifier.Replace("\"", "\"\"") + "\"";

    public static string StageName(object token)
    {
        var text = Regex.Replace(token.ToString() ?? "", "[^A-Za-z0-9_]", "_");
        if (text.Length > 48) text = text[..48];
        text = text.Trim('_');
        return $"{PgDsn.CodexSchema}_{StagePrefix}{(text.Length > 0 ? text : "stage")}";
    }

    private static void CheckName(string schema)
    {
        if (!PgDsn.Identifier.IsMatch(schema))
            throw new InvalidOperationException(
                $"STAGE_SCHEMA_NAME_INVALID:{schema}");
    }

    public static bool SchemaExists(string schema)
    {
        using var connection = PgDsn.Admin();
        using var command = new NpgsqlCommand(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name=@s",
            connection);
        command.Parameters.AddWithValue("s", schema);
        return command.ExecuteScalar() is not null;
    }

    public static void CreateEmptySchema(string schema)
    {
        CheckName(schema);
        using var connection = PgDsn.Admin();
        Exec(connection, $"DROP SCHEMA IF EXISTS {Q(schema)} CASCADE");
        Exec(connection, $"CREATE SCHEMA {Q(schema)}");
    }

    public static void DropSchema(string schema)
    {
        if (!PgDsn.Identifier.IsMatch(schema)
            || !schema.Contains(StagePrefix, StringComparison.Ordinal))
            throw new InvalidOperationException(
                $"STAGE_SCHEMA_NAME_INVALID:{schema}");
        using var connection = PgDsn.Admin();
        Exec(connection, $"DROP SCHEMA IF EXISTS {Q(schema)} CASCADE");
    }

    internal static List<string> SchemaTables(NpgsqlConnection connection,
        string schema)
    {
        using var command = new NpgsqlCommand(
            "SELECT table_name FROM information_schema.tables "
            + "WHERE table_schema=@s AND table_type='BASE TABLE' "
            + "ORDER BY table_name",
            connection);
        command.Parameters.AddWithValue("s", schema);
        var tables = new List<string>();
        using var reader = command.ExecuteReader();
        while (reader.Read())
            tables.Add(reader.GetString(0));
        return tables;
    }

    /// <summary>Clone every base table (structure + rows) into a fresh
    /// stage schema.</summary>
    public static List<string> CloneSchema(string sourceSchema,
        string targetSchema)
    {
        CheckName(sourceSchema);
        CheckName(targetSchema);
        using var connection = PgDsn.Admin();
        var tables = SchemaTables(connection, sourceSchema);
        if (tables.Count == 0)
            throw new InvalidOperationException(
                $"STAGE_SOURCE_EMPTY:{sourceSchema}");
        Exec(connection,
            $"DROP SCHEMA IF EXISTS {Q(targetSchema)} CASCADE");
        Exec(connection, $"CREATE SCHEMA {Q(targetSchema)}");
        foreach (var table in tables)
        {
            // sql-ok: governed schema clone — bounded catalog set
            Exec(connection,
                $"CREATE TABLE {Q(targetSchema)}.{Q(table)} "
                + $"(LIKE {Q(sourceSchema)}.{Q(table)} INCLUDING ALL)");
            Exec(connection,
                $"INSERT INTO {Q(targetSchema)}.{Q(table)} "
                + $"SELECT * FROM {Q(sourceSchema)}.{Q(table)}");
        }
        return tables;
    }

    public static List<string> CloneAuthority(string targetSchema) =>
        CloneSchema(PgDsn.CodexSchema, targetSchema);

    public static HashSet<string> StageTableNames(string schema)
    {
        using var connection = PgDsn.Admin();
        return new HashSet<string>(SchemaTables(connection, schema),
            StringComparer.Ordinal);
    }

    internal static void Exec(NpgsqlConnection connection, string sql)
    {
        using var command = new NpgsqlCommand(sql, connection);
        try
        {
            command.ExecuteNonQuery();
        }
        catch (PostgresException error)
        {
            throw new PostgresException(error.MessageText
                + $" [statement: {(sql.Length > 160
                    ? sql[..160] + "…" : sql)}]",
                error.Severity, error.InvariantSeverity,
                error.SqlState);
        }
    }

    // ------------------------------------------------------------------
    // Artifact codec: deterministic .sql dump
    // ------------------------------------------------------------------

    internal static string Literal(object? value)
    {
        switch (value)
        {
            case null or DBNull:
                return "NULL";
            case bool b:
                return b ? "true" : "false";
            case byte or sbyte or short or ushort or int or uint or long
                or ulong:
                return Convert.ToString(value,
                    System.Globalization.CultureInfo.InvariantCulture) ?? "0";
            case float f:
                return PyFloatRepr(f);
            case double d:
                return PyFloatRepr(d);
            case decimal m:
                return PyFloatRepr((double)m);
            case byte[] bytes:
                return "decode('" + Convert.ToHexString(bytes)
                    .ToLowerInvariant() + "','hex')";
            default:
                var text = value.ToString() ?? "";
                var escaped = text
                    .Replace("\\", "\\\\")
                    .Replace("'", "\\'")
                    .Replace("\r", "\\r")
                    .Replace("\n", "\\n")
                    .Replace("\t", "\\t");
                return "E'" + escaped + "'";
        }
    }

    /// <summary>Python repr(float) — shared by the codec and canonical
    /// JSON writer.</summary>
    internal static string PyFloatRepr(double value)
    {
        if (double.IsNaN(value)) return "nan";
        if (double.IsPositiveInfinity(value)) return "inf";
        if (double.IsNegativeInfinity(value)) return "-inf";
        var culture = System.Globalization.CultureInfo.InvariantCulture;
        var text = value.ToString("R", culture);
        if (text.Contains('E'))
        {
            var parts = text.Split('E');
            var exp = int.Parse(parts[1], culture);
            return parts[0] + "e" + (exp < 0 ? "-" : "+")
                + Math.Abs(exp).ToString("D2", culture);
        }
        if (!text.Contains('.'))
            text += ".0";
        return text;
    }

    internal static List<string> PrimaryKeyColumns(
        NpgsqlConnection connection, string schema, string table)
    {
        using var command = new NpgsqlCommand(
            "SELECT a.attname FROM pg_index i "
            + "JOIN pg_attribute a ON a.attrelid=i.indrelid "
            + "AND a.attnum=ANY(i.indkey) "
            + "WHERE i.indrelid=@r::regclass AND i.indisprimary "
            + "ORDER BY a.attnum",
            connection);
        command.Parameters.AddWithValue("r", $"{schema}.{table}");
        var columns = new List<string>();
        using var reader = command.ExecuteReader();
        while (reader.Read())
            columns.Add(reader.GetString(0));
        return columns;
    }

    /// <summary>Write a deterministic ``.sql`` artifact for one schema —
    /// byte-identical to the Python ``dump_schema`` output.</summary>
    public static string DumpSchema(string schema, string target,
        string? version = null)
    {
        CheckName(schema);
        Directory.CreateDirectory(Path.GetDirectoryName(target) ?? ".");
        using var connection = PgDsn.Admin();
        var tables = SchemaTables(connection, schema);
        var lines = new List<string>
        {
            ArtifactHeader,
            $"-- source_schema: {schema}",
        };
        foreach (var table in tables)
        {
            var columns = new List<(string Name, string PgType, bool NotNull)>();
            using (var command = new NpgsqlCommand(
                "SELECT column_name, data_type, is_nullable "
                + "FROM information_schema.columns WHERE table_schema=@s "
                + "AND table_name=@t ORDER BY ordinal_position",
                connection))
            {
                command.Parameters.AddWithValue("s", schema);
                command.Parameters.AddWithValue("t", table);
                using var reader = command.ExecuteReader();
                while (reader.Read())
                    columns.Add((reader.GetString(0), reader.GetString(1),
                        reader.GetString(2) == "NO"));
            }
            var pk = PrimaryKeyColumns(connection, schema, table);
            var columnDefs = columns
                .Select(c => $"\"{c.Name}\" {ArtifactType(c.PgType)}"
                    + (c.NotNull ? " NOT NULL" : ""))
                .ToList();
            if (pk.Count > 0)
                columnDefs.Add("PRIMARY KEY ("
                    + string.Join(", ", pk.Select(Q)) + ")");
            lines.Add($"CREATE TABLE \"{table}\" "
                + $"({string.Join(", ", columnDefs)});");
            var names = columns.Select(c => c.Name).ToList();
            var order = pk.Count > 0
                ? string.Join(", ", pk.Select(Q))
                : string.Join(", ", names.Select(Q));
            using (var command = new NpgsqlCommand(
                $"SELECT {string.Join(", ", names.Select(Q))} "
                + $"FROM {Q(schema)}.{Q(table)} ORDER BY {order}",
                connection))
            {
                using var reader = command.ExecuteReader();
                while (reader.Read())
                {
                    var values = new object?[names.Count];
                    for (var i = 0; i < names.Count; i++)
                        values[i] = reader.IsDBNull(i) ? null
                            : reader.GetValue(i);
                    lines.Add(
                        $"INSERT INTO \"{table}\" "
                        + $"({string.Join(", ", names.Select(n => $"\"{n}\""))}) "
                        + $"VALUES ({string.Join(", ",
                            values.Select(Literal))});");
                }
            }
        }
        lines.Add("-- end-artifact");
        // Parity: Python Path.write_text() in text mode translates \n
        // to os.linesep (CRLF on Windows) — use Environment.NewLine.
        File.WriteAllText(target,
            string.Join(Environment.NewLine, lines)
            + Environment.NewLine, new UTF8Encoding(false));
        return target;
    }

    private static string ArtifactType(string pgType)
    {
        var value = pgType.ToUpperInvariant();
        if (value.Contains("INT") || value.Contains("SERIAL"))
            return "BIGINT";
        if (value.Contains("DOUBLE") || value.Contains("REAL")
            || value.Contains("FLOA") || value.Contains("NUMERIC"))
            return "DOUBLE PRECISION";
        if (value.Contains("BYTEA") || value.Contains("BLOB"))
            return "BYTEA";
        if (value.StartsWith("TIMESTAMP", StringComparison.Ordinal))
            return "TIMESTAMPTZ";
        if (value.Contains("BOOL"))
            return "BOOLEAN";
        return "TEXT";
    }

    /// <summary>Create ``schema`` and execute one ``.sql`` artifact into
    /// it.</summary>
    public static List<string> MaterializeArtifact(string artifact,
        string schema)
    {
        CheckName(schema);
        var rawLines = File.ReadAllLines(artifact, Encoding.UTF8);
        var statements = rawLines
            .Where(line => line.Trim().Length > 0
                && !line.TrimStart().StartsWith("--",
                    StringComparison.Ordinal))
            .Select(line => line.Trim())
            .ToList();
        using var connection = PgDsn.Admin();
        Exec(connection, $"DROP SCHEMA IF EXISTS {Q(schema)} CASCADE");
        Exec(connection, $"CREATE SCHEMA {Q(schema)}");
        Exec(connection, $"SET search_path TO {Q(schema)}, pg_catalog");
        foreach (var statement in statements)
            Exec(connection, statement);
        return statements
            .Where(s => s.StartsWith("CREATE TABLE \"",
                StringComparison.Ordinal))
            .Select(s => s.Split('"')[1])
            .ToList();
    }

    /// <summary>Table names of a readable ``.sql`` artifact; null when
    /// unreadable or not an artifact.</summary>
    public static HashSet<string>? ArtifactTableNames(string artifact)
    {
        string text;
        try
        {
            text = File.ReadAllText(artifact, Encoding.UTF8);
        }
        catch (IOException)
        {
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            return null;
        }
        var head = text.Split('\n').Take(2).ToList();
        if (!head.Any(line => line.Contains(ArtifactHeader,
            StringComparison.Ordinal)))
            return null;
        return new HashSet<string>(
            Regex.Matches(text, "^CREATE TABLE \"([^\"]+)\"",
                RegexOptions.Multiline)
            .Cast<Match>().Select(m => m.Groups[1].Value),
            StringComparer.Ordinal);
    }

    /// <summary>``metadata.codex_version`` parsed from the artifact
    /// without a database.</summary>
    public static string ArtifactVersion(string artifact)
    {
        string text;
        try
        {
            text = File.ReadAllText(artifact, Encoding.UTF8);
        }
        catch (IOException)
        {
            return "";
        }
        catch (UnauthorizedAccessException)
        {
            return "";
        }
        var match = Regex.Match(text,
            "INSERT INTO \"metadata\" \\([^)]*\\) VALUES "
            + "\\(E?'codex_version', E?'([^']*)'\\)");
        if (!match.Success)
            return "";
        return match.Groups[1].Value
            .Replace("\\'", "'").Replace("\\\\", "\\");
    }
}
