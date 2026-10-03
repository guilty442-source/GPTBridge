using System.Data;
using System.Text.Json;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Read-only PostgreSQL schema capture for the retirement migration
/// (docs/postgresql-retirement-route.md Phase 2+). Same discipline as
/// <see cref="NativeCodexMigration"/>.Capture — one repeatable-read
/// read-only transaction, catalog-driven table list, deterministic row
/// order — generalized from gptbridge_codex to any gptbridge_* domain so
/// frozen and legacy schemas can be sealed into verifiable snapshot
/// artifacts before their consumers are rerouted or retired. Never
/// mutates the source, never stages, never selects runtime authority;
/// the caller pins the artifact by file sha256.
/// </summary>
internal static class PgSchemaSnapshot
{
    public const string Format = "gptbridge-pg-snapshot/v1";

    public static byte[] Capture(string schema)
    {
        if (string.IsNullOrWhiteSpace(schema)
            || !schema.All(c => c is >= 'a' and <= 'z'
                or >= '0' and <= '9' or '_')
            || schema[0] == '_'
            || schema.StartsWith("pg_", StringComparison.Ordinal)
            || schema == "information_schema")
            throw new InvalidDataException("BLOCKED_SCHEMA_NAME");
        using var source = PgDsn.Readonly();
        using var transaction =
            source.BeginTransaction(IsolationLevel.RepeatableRead);
        using (var readOnly = new NpgsqlCommand(
            "SET TRANSACTION READ ONLY", source, transaction))
            readOnly.ExecuteNonQuery();
        using (var check = new NpgsqlCommand(
            "SELECT 1 FROM information_schema.schemata"
            + " WHERE schema_name=@s", source, transaction))
        {
            check.Parameters.AddWithValue("s", schema);
            if (check.ExecuteScalar() is null)
                throw new InvalidDataException("SCHEMA_NOT_FOUND");
        }
        var quotedSchema = "\"" + schema + "\"";
        using var catalog = new NpgsqlCommand(
            "SELECT table_name FROM information_schema.tables"
            + " WHERE table_schema=@schema AND table_type='BASE TABLE'"
            + " ORDER BY table_name", source, transaction);
        catalog.Parameters.AddWithValue("schema", schema);
        var names = new List<string>();
        using (var reader = catalog.ExecuteReader())
            while (reader.Read()) names.Add(reader.GetString(0));
        var tables = new SortedDictionary<string, JsonElement[]>(
            StringComparer.Ordinal);
        foreach (var name in names)
        {
            var quoted = "\"" + name.Replace("\"", "\"\"") + "\"";
            // sql-ok: catalog-introspected identifiers
            using var command = new NpgsqlCommand(
                $"SELECT row_to_json(t)::text FROM {quotedSchema}.{quoted} t"
                + " ORDER BY row_to_json(t)::text COLLATE \"C\"",
                source, transaction);
            using var reader = command.ExecuteReader();
            var rows = new List<JsonElement>();
            while (reader.Read())
            {
                using var row = JsonDocument.Parse(reader.GetString(0));
                rows.Add(row.RootElement.Clone());
            }
            tables.Add(name, rows.ToArray());
        }
        var count = tables.Values.Sum(rows => (long)rows.Length);
        var bytes = JsonSerializer.SerializeToUtf8Bytes(new
        {
            artifact = Format,
            schema,
            row_count = count,
            table_count = tables.Count,
            tables,
        });
        transaction.Commit();
        return bytes;
    }
}
