using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Machine-schema parity probe — C# reimplementation of the retired
/// ``governance_rule/execution/semantic_hash_toolchain.py`` (retired
/// with the Python lane; recovered from git history).
///
/// Recipe: SEAL_CANONICAL_V1 (same serialization family as
/// codex_amendment_contract.content_hash) — UTF-8, NFC, lexicographic
/// key order, compact separators, Python ``json.dumps``-compatible
/// string escaping (ensure_ascii), SHA-256.
///
/// Three layers per registered machine schema:
///   producer    — semantic hash of the canonical descriptor
///                 projection (DESCRIPTOR_FIELDS)
///   validator   — independent re-derivation of the same row
///                 (must equal the producer bit-for-bit)
///   persistence — comparison against the persisted
///                 ``canonical_semantic_hash`` / ``content_hash``
///
/// CANONICAL_MISMATCH is evidence, not a pass — the stored canonical
/// values were stamped by governor-side tooling whose descriptor
/// projection is not derivable.  Re-stamping is a governor action;
/// this probe never writes parity state.
/// </summary>
internal static class MachineSchemaParity
{
    public const string Recipe = SemanticHashToolchain.Recipe;
    internal static readonly string[] DescriptorFields = SemanticHashToolchain.DescriptorFields;

    public static string SealHash(SortedDictionary<string, object?> descriptor) =>
        SemanticHashToolchain.SealHash(descriptor);

    internal static string CanonicalDescriptorJson(Dictionary<string, object?> row) =>
        SemanticHashToolchain.CanonicalDescriptorJson(row);

    private static SortedDictionary<string, object?> BuildDescriptor(Dictionary<string, object?> row) =>
        new(SemanticHashToolchain.BuildDescriptor(row), StringComparer.Ordinal);

    /// <summary>Single-row descriptor + hash — used by
    /// ``--parity-descriptor &lt;code&gt;`` for recipe debugging.</summary>
    public static Dictionary<string, object?> Describe(string code)
    {
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT r.*, p.canonical_semantic_hash "
            + $"FROM {PgDsn.CodexSchema}.machine_schema_registry r "
            + $"LEFT JOIN {PgDsn.CodexSchema}.machine_schema_parity_evidence p"
            + "  ON p.schema_code = r.schema_code "
            + "WHERE r.schema_code = @c", connection);
        command.Parameters.AddWithValue("c", code);
        using var reader = command.ExecuteReader();
        if (!reader.Read())
            return new Dictionary<string, object?>
            {
                ["error"] = "SCHEMA_CODE_UNKNOWN", ["code"] = code,
            };
        var row = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        for (var i = 0; i < reader.FieldCount; i++)
            row[reader.GetName(i)] =
                reader.IsDBNull(i) ? null : reader.GetValue(i);
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["schema_code"] = code,
            ["recipe"] = Recipe,
            ["descriptor"] = CanonicalDescriptorJson(row),
            ["producer_semantic_hash"] =
                SealHash(BuildDescriptor(row)),
            ["canonical_semantic_hash"] =
                row["canonical_semantic_hash"],
        };
    }

    public static Dictionary<string, object?> Probe()
    {
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT r.*, p.canonical_semantic_hash "
            + $"FROM {PgDsn.CodexSchema}.machine_schema_registry r "
            + $"LEFT JOIN {PgDsn.CodexSchema}.machine_schema_parity_evidence p"
            + "  ON p.schema_code = r.schema_code ORDER BY r.schema_code",
            connection);
        var rows = new List<Dictionary<string, object?>>();
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read())
            {
                var row = new Dictionary<string, object?>(
                    StringComparer.Ordinal);
                for (var i = 0; i < reader.FieldCount; i++)
                    row[reader.GetName(i)] =
                        reader.IsDBNull(i) ? null : reader.GetValue(i);
                rows.Add(row);
            }
        }

        var results = new List<Dictionary<string, object?>>();
        foreach (var row in rows)
        {
            var canonical =
                row["canonical_semantic_hash"] as string;
            var storedContent = row["content_hash"] as string;
            results.Add(SemanticHashToolchain.EvaluateRow(
                row, canonical, storedContent));
        }

        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["artifact"] = "machine-schema-parity-probe/v1",
            ["recipe"] = Recipe,
            ["descriptor_fields"] = DescriptorFields,
            ["rows"] = results.Count,
            ["producer_validator_pass"] = results.Count(r =>
                (string)r["producer_validator_layer"]! == "PASS"),
            ["canonical_match"] = results.Count(r =>
                (string)r["persistence_layer"]! == "MATCH"),
            ["note"] = "CANONICAL_MISMATCH is evidence, not a pass; "
                + "restamp or descriptor-projection publication is a "
                + "governor action. This probe never writes parity "
                + "state.",
            ["results"] = results,
        };
    }
}
