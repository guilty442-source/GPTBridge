using System.Security.Cryptography;
using System.Text;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Export/verify paths between the PostgreSQL authority and ``.sql``
/// artifacts + the atomic schema-replace import — ports of
/// codex_postgresql_export.py / codex_postgresql_import.py /
/// codex_postgresql_pool.authority_state.
/// </summary>
internal static class PgExport
{
    private static List<string> AuthorityTables(NpgsqlConnection source)
    {
        using var command = new NpgsqlCommand(
            "SELECT table_name FROM information_schema.tables "
            + $"WHERE table_schema='{PgDsn.CodexSchema}' "
            + "AND table_type='BASE TABLE' "
            + "AND table_name<>'codex_authority_state' "
            + "ORDER BY table_name",
            source);
        var tables = new List<string>();
        using var reader = command.ExecuteReader();
        while (reader.Read())
            tables.Add(reader.GetString(0));
        return tables;
    }

    /// <summary>Export the live authority to a non-authoritative
    /// ``.sql`` artifact (A173 staged snapshot).</summary>
    public static string ExportPostgresqlCodex(string target)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(target) ?? ".");
        var token = Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(
                $"export:{Path.GetFullPath(target)}")))
            .ToLowerInvariant()[..16];
        var stage = StageCodec.StageName($"export{token}");
        try
        {
            StageCodec.CloneAuthority(stage);
            using (var admin = PgDsn.Admin())
                StageCodec.Exec(admin,
                    $"DROP TABLE IF EXISTS \"{stage}\""
                    + ".codex_authority_state");
            StageCodec.DumpSchema(stage, target);
        }
        finally
        {
            StageCodec.DropSchema(stage);
        }
        return target;
    }

    /// <summary>``authority_state`` — the codex_authority_state row via
    /// the governed read-only connection.</summary>
    public static Dictionary<string, object?> AuthorityState()
    {
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT * FROM \"{PgDsn.CodexSchema}\""
            + ".codex_authority_state", connection);
        using var reader = command.ExecuteReader();
        if (!reader.Read())
            throw new InvalidOperationException(
                "POSTGRESQL_CODEX_AUTHORITY_NOT_INITIALIZED");
        var row = new Dictionary<string, object?>(StringComparer.Ordinal);
        for (var i = 0; i < reader.FieldCount; i++)
            row[reader.GetName(i)] = reader.IsDBNull(i)
                ? null : reader.GetValue(i);
        return row;
    }

    private static List<string> TableColumns(NpgsqlConnection connection,
        string schema, string table)
    {
        using var command = new NpgsqlCommand(
            "SELECT column_name FROM information_schema.columns "
            + "WHERE table_schema=@s AND table_name=@t "
            + "ORDER BY ordinal_position", connection);
        command.Parameters.AddWithValue("s", schema);
        command.Parameters.AddWithValue("t", table);
        var columns = new List<string>();
        using var reader = command.ExecuteReader();
        while (reader.Read())
            columns.Add(reader.GetString(0));
        return columns;
    }

    private static string Norm(object? value) => value switch
    {
        null or DBNull => "\x00NULL",
        byte[] bytes => "B" + Convert.ToHexString(bytes),
        _ => "S" + (value.ToString() ?? ""),
    };

    private static (int Checked, bool Equal) CompareTable(
        NpgsqlConnection stageConnection, string stageSchema,
        NpgsqlConnection target, string table)
    {
        var columns = TableColumns(stageConnection, stageSchema, table);
        var colList = string.Join(", ",
            columns.Select(c => $"\"{c}\""));
        List<object?[]> Read(NpgsqlConnection conn, string schema)
        {
            // sql-ok: catalog-introspected identifiers
            using var command = new NpgsqlCommand(
                $"SELECT {colList} FROM \"{schema}\".\"{table}\"", conn);
            var rows = new List<object?[]>();
            using var reader = command.ExecuteReader();
            while (reader.Read())
            {
                var row = new object?[reader.FieldCount];
                for (var i = 0; i < row.Length; i++)
                    row[i] = reader.IsDBNull(i) ? null
                        : reader.GetValue(i);
                rows.Add(row);
            }
            return rows;
        }
        var stageRows = Read(stageConnection, stageSchema)
            .Select(r => string.Join("\x1f", r.Select(Norm)))
            .OrderBy(s => s, StringComparer.Ordinal).ToList();
        var pgRows = Read(target, PgDsn.CodexSchema)
            .Select(r => string.Join("\x1f", r.Select(Norm)))
            .OrderBy(s => s, StringComparer.Ordinal).ToList();
        return (stageRows.Count,
            stageRows.SequenceEqual(pgRows));
    }

    /// <summary>Compare every artifact table and cell with the live
    /// authority.</summary>
    public static Dictionary<string, object?> VerifySqlParity(
        string source)
    {
        var resolved = Path.GetFullPath(source);
        var stage = StageCodec.StageName("parity"
            + Convert.ToHexString(SHA256.HashData(
                Encoding.UTF8.GetBytes(resolved)))
                .ToLowerInvariant()[..12]);
        StageCodec.MaterializeArtifact(resolved, stage);
        var mismatches = new List<string>();
        var checkedRows = 0;
        var stageTables = new List<string>();
        try
        {
            using var stageConnection = PgDsn.Admin();
            using var target = PgDsn.Readonly();
            stageTables = StageCodec.SchemaTables(
                stageConnection, stage);
            var pgTables = AuthorityTables(target);
            if (!stageTables.SequenceEqual(pgTables))
                mismatches.Add("table-set");
            foreach (var table in stageTables)
            {
                var (count, equal) = CompareTable(
                    stageConnection, stage, target, table);
                checkedRows += count;
                if (!equal)
                    mismatches.Add(table);
            }
        }
        finally
        {
            StageCodec.DropSchema(stage);
        }
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["result"] = mismatches.Count == 0 ? "PASS" : "FAIL",
            ["table_count"] = (long)stageTables.Count,
            ["row_count"] = (long)checkedRows,
            ["mismatches"] = mismatches.Cast<object?>().ToList(),
        };
    }
}

/// <summary>``codex_postgresql_import.import_codex_artifact`` —
/// atomic schema-replace with advisory lock, monotonic-version guard
/// and authority-state bookkeeping.</summary>
internal static class PgImport
{
    private static string SourceHash(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream))
            .ToLowerInvariant();
    }

    private static long? VersionUnits(string value)
    {
        try { return CodexVersion.Units(value); }
        catch (ArgumentException) { return null; }
    }

    private static bool VersionRegresses(string source, string current)
    {
        var sourceUnits = VersionUnits(source);
        var currentUnits = VersionUnits(current);
        if (sourceUnits is null || currentUnits is null)
            return true;
        return sourceUnits < currentUnits;
    }

    /// <summary>Serialize concurrent imports and refuse version
    /// regressions — xact-scoped advisory lock.  Parity with psycopg
    /// implicit-transaction behaviour: a failed authority-state probe
    /// rolls back (fresh init has nothing to regress against) and the
    /// import proceeds in a fresh transaction.</summary>
    private static NpgsqlTransaction GuardImport(NpgsqlConnection target,
        string sourceVersion)
    {
        var transaction = target.BeginTransaction();
        Exec(target, transaction,
            "SELECT pg_advisory_xact_lock("
            + "hashtext('gptbridge.codex.import'))");
        object? row;
        try
        {
            row = Scalar(target, transaction,
                $"SELECT codex_version FROM \"{PgDsn.CodexSchema}\""
                + ".codex_authority_state");
        }
        catch (PostgresException)
        {
            // Authority state unreadable (fresh init, missing
            // schema/table): nothing to regress against.
            transaction.Rollback();
            transaction = target.BeginTransaction();
            row = null;
        }
        if (row is not null)
        {
            var currentVersion = row.ToString() ?? "";
            if (VersionRegresses(sourceVersion, currentVersion))
            {
                var message = "CODEX_VERSION_REGRESSION:"
                    + $"{sourceVersion}<{currentVersion}";
                transaction.Rollback();
                transaction.Dispose();
                throw new InvalidOperationException(message);
            }
        }
        return transaction;
    }

    private static void SealAuthority(NpgsqlConnection target,
        NpgsqlTransaction transaction, string codexVersion,
        string sourceDigest, int tableCount, int totalRows)
    {
        var schema = PgDsn.CodexSchema;
        Exec(target, transaction,
            $"CREATE TABLE \"{schema}\".codex_authority_state ("
            + "authority_uri TEXT PRIMARY KEY, "
            + "codex_version TEXT NOT NULL, "
            + "source_sha256 TEXT NOT NULL, "
            + "table_count BIGINT NOT NULL, "
            + "row_count BIGINT NOT NULL, "
            + "imported_at TIMESTAMPTZ NOT NULL "
            + "DEFAULT CURRENT_TIMESTAMP)");
        using (var command = new NpgsqlCommand(
            $"INSERT INTO \"{schema}\".codex_authority_state "
            + "(authority_uri,codex_version,source_sha256,"
            + "table_count,row_count) VALUES (@u,@v,@s,@t,@r)",
            target, transaction))
        {
            command.Parameters.AddWithValue("u",
                PgDsn.AuthorityUri);
            command.Parameters.AddWithValue("v", codexVersion);
            command.Parameters.AddWithValue("s", sourceDigest);
            command.Parameters.AddWithValue("t", (long)tableCount);
            command.Parameters.AddWithValue("r", (long)totalRows);
            command.ExecuteNonQuery();
        }
        Exec(target, transaction,
            $"GRANT USAGE ON SCHEMA \"{schema}\" TO gptbridge_runtime");
        Exec(target, transaction,
            $"GRANT SELECT ON ALL TABLES IN SCHEMA \"{schema}\" "
            + "TO gptbridge_runtime");
        Exec(target, transaction,
            $"ALTER DEFAULT PRIVILEGES IN SCHEMA \"{schema}\" "
            + "GRANT SELECT ON TABLES TO gptbridge_runtime");
    }

    private static List<string> ArtifactStatements(string artifact)
    {
        var rawLines = File.ReadAllLines(artifact, Encoding.UTF8);
        if (rawLines.Length == 0 || rawLines[0].Trim()
            != StageCodec.ArtifactHeader)
            throw new InvalidOperationException(
                "CODEX_SQL_ARTIFACT_HEADER_INVALID");
        return rawLines
            .Where(l => l.Trim().Length > 0
                && !l.TrimStart().StartsWith("--",
                    StringComparison.Ordinal))
            .Select(l => l.Trim()).ToList();
    }

    /// <summary>Atomically replace the PostgreSQL Codex schema from the
    /// ``.sql`` artifact.</summary>
    public static Dictionary<string, object?> ImportCodexArtifact(
        string source)
    {
        var resolved = Path.GetFullPath(source);
        var tables = StageCodec.ArtifactTableNames(resolved);
        if (tables is null || !tables.Contains("metadata"))
            throw new InvalidOperationException(
                "CODEX_SQL_ARTIFACT_INVALID");
        var sortedTables = tables
            .OrderBy(t => t, StringComparer.Ordinal).ToList();
        var codexVersion = StageCodec.ArtifactVersion(resolved);
        var statements = ArtifactStatements(resolved);
        var sourceDigest = SourceHash(resolved);
        var totalRows = 0;
        using var target = PgDsn.Admin();
        // psycopg autocommit=False parity: one transaction wraps the
        // whole schema replace.
        using var transaction = GuardImport(target, codexVersion);
        Exec(target, transaction,
            $"DROP SCHEMA IF EXISTS \"{PgDsn.CodexSchema}\" CASCADE");
        Exec(target, transaction,
            $"CREATE SCHEMA \"{PgDsn.CodexSchema}\"");
        Exec(target, transaction,
            $"REVOKE CREATE ON SCHEMA \"{PgDsn.CodexSchema}\" FROM PUBLIC");
        Exec(target, transaction,
            $"SET LOCAL search_path TO \"{PgDsn.CodexSchema}\", "
            + "pg_catalog");
        foreach (var statement in statements)
            if (statement.StartsWith("INSERT INTO",
                StringComparison.Ordinal))
                totalRows += 1;
        // sql-ok: governed artifact codec, line-per-statement — chunked
        // into multi-statement commands inside the guarded transaction.
        StageCodec.ExecStatements(target, statements, transaction);
        SealAuthority(target, transaction, codexVersion, sourceDigest,
            sortedTables.Count, totalRows);
        transaction.Commit();
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["codex_version"] = codexVersion,
            ["source_sha256"] = sourceDigest,
            ["table_count"] = (long)sortedTables.Count,
            ["row_count"] = (long)totalRows,
        };
    }

    private static void Exec(NpgsqlConnection connection,
        NpgsqlTransaction transaction, string sql)
    {
        using var command = new NpgsqlCommand(sql, connection,
            transaction);
        command.ExecuteNonQuery();
    }

    private static object? Scalar(NpgsqlConnection connection,
        NpgsqlTransaction transaction, string sql)
    {
        using var command = new NpgsqlCommand(sql, connection,
            transaction);
        return command.ExecuteScalar();
    }
}
