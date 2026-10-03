using System.Data;
using System.Security.Cryptography;
using System.Text.Json;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>One-time read-only source bridge. Never changes PostgreSQL or selects runtime authority.</summary>
internal static class NativeCodexMigration
{
    public static byte[] Capture(string expectedGeneration)
    {
        using var source = PgDsn.Readonly();
        using var transaction = source.BeginTransaction(IsolationLevel.RepeatableRead);
        using (var readOnly = new NpgsqlCommand("SET TRANSACTION READ ONLY", source, transaction))
            readOnly.ExecuteNonQuery();
        var schema = "\"" + PgDsn.CodexSchema.Replace("\"", "\"\"") + "\"";
        using var stateCommand = new NpgsqlCommand($"SELECT codex_version FROM {schema}.codex_authority_state", source, transaction);
        var generation = Convert.ToString(stateCommand.ExecuteScalar());
        if (string.IsNullOrWhiteSpace(expectedGeneration) || generation != expectedGeneration)
            throw new InvalidDataException("BLOCKED_GENERATION_DRIFT");
        using var catalog = new NpgsqlCommand("SELECT table_name FROM information_schema.tables WHERE table_schema=@schema AND table_type='BASE TABLE' ORDER BY table_name", source, transaction);
        catalog.Parameters.AddWithValue("schema", PgDsn.CodexSchema);
        var names = new List<string>();
        using (var reader = catalog.ExecuteReader())
            while (reader.Read()) names.Add(reader.GetString(0));
        var tables = new SortedDictionary<string, JsonElement[]>(StringComparer.Ordinal);
        foreach (var name in names)
        {
            var quoted = "\"" + name.Replace("\"", "\"\"") + "\"";
            using var command = new NpgsqlCommand($"SELECT row_to_json(t)::text FROM {schema}.{quoted} t ORDER BY row_to_json(t)::text COLLATE \"C\"", source, transaction);
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
        var bytes = JsonSerializer.SerializeToUtf8Bytes(new { artifact = NativeCodexSnapshot.Format,
            generation, row_count = count, table_count = tables.Count, tables });
        NativeCodexSnapshot.Read(bytes, expectedGeneration,
            Convert.ToHexString(SHA256.HashData(bytes)), count, tables.Count);
        transaction.Commit();
        return bytes;
    }
}
