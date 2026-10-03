using System.Security.Cryptography;
using System.Text.Json;

namespace GPTBridge.CodexPipeline;

/// <summary>A pinned migration snapshot reader. A snapshot is not authority or release approval.</summary>
public sealed class NativeCodexSnapshot
{
    public const string Format = "gptbridge-codex-native-snapshot/v1";
    private readonly Dictionary<string, JsonElement[]> tables;
    public string Generation { get; }
    public long RowCount { get; }
    public int TableCount => tables.Count;

    private NativeCodexSnapshot(string generation, Dictionary<string, JsonElement[]> rows)
    {
        Generation = generation;
        tables = rows;
        RowCount = rows.Values.Sum(v => (long)v.Length);
    }

    public static NativeCodexSnapshot Read(byte[] bytes, string expectedGeneration,
        string expectedSha256, long expectedRows, int expectedTables)
    {
        if (string.IsNullOrWhiteSpace(expectedGeneration) || expectedSha256.Length != 64
            || !expectedSha256.All(Uri.IsHexDigit) || expectedRows < 1 || expectedTables < 1)
            throw new InvalidDataException("NATIVE_CODEX_PIN_REQUIRED");
        var hash = Convert.ToHexString(SHA256.HashData(bytes));
        if (!hash.Equals(expectedSha256, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("NATIVE_CODEX_HASH_MISMATCH");
        using var document = JsonDocument.Parse(bytes);
        var root = document.RootElement;
        var fields = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in root.EnumerateObject())
            if (!fields.Add(property.Name))
                throw new InvalidDataException("NATIVE_CODEX_FIELD_DUPLICATE");
        if (root.GetProperty("artifact").GetString() != Format
            || root.GetProperty("generation").GetString() != expectedGeneration)
            throw new InvalidDataException("BLOCKED_GENERATION_DRIFT");
        var result = new Dictionary<string, JsonElement[]>(StringComparer.Ordinal);
        foreach (var table in root.GetProperty("tables").EnumerateObject())
        {
            if (string.IsNullOrWhiteSpace(table.Name) || table.Value.ValueKind != JsonValueKind.Array
                || !result.TryAdd(table.Name, table.Value.EnumerateArray().Select(row =>
                {
                    if (row.ValueKind != JsonValueKind.Object)
                        throw new InvalidDataException("NATIVE_CODEX_ROW_INVALID");
                    var names = new HashSet<string>(StringComparer.Ordinal);
                    foreach (var column in row.EnumerateObject())
                        if (!names.Add(column.Name))
                            throw new InvalidDataException("NATIVE_CODEX_COLUMN_DUPLICATE");
                    return row.Clone();
                }).ToArray()))
                throw new InvalidDataException("NATIVE_CODEX_TABLE_INVALID");
        }
        var snapshot = new NativeCodexSnapshot(expectedGeneration, result);
        if (snapshot.RowCount != expectedRows || snapshot.TableCount != expectedTables
            || root.GetProperty("row_count").GetInt64() != expectedRows
            || root.GetProperty("table_count").GetInt32() != expectedTables)
            throw new InvalidDataException("NATIVE_CODEX_PARITY_MISMATCH");
        return snapshot;
    }

    public IReadOnlyList<JsonElement> Rows(string table) => tables.TryGetValue(table, out var rows)
        ? Array.AsReadOnly(rows) : throw new InvalidDataException("NATIVE_CODEX_TABLE_MISSING:" + table);
}
