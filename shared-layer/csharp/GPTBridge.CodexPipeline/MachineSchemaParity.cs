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
internal static partial class MachineSchemaParity
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
        var authority = PgExport.AuthorityState();
        AssertGeneration(authority, authority);
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT r.*, {EvidenceColumns} "
            + $"FROM {PgDsn.CodexSchema}.machine_schema_registry r "
            + $"LEFT JOIN {PgDsn.CodexSchema}.machine_schema_parity_evidence p"
            + "  ON p.schema_code = r.schema_code "
            + "WHERE r.schema_code = @c", connection);
        command.Parameters.AddWithValue("c", code);
        var row = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        using (var reader = command.ExecuteReader())
        {
            if (!reader.Read())
            {
                AssertGeneration(authority, PgExport.AuthorityState());
                return new Dictionary<string, object?>
                {
                    ["error"] = "SCHEMA_CODE_UNKNOWN", ["code"] = code,
                    ["generation"] = authority["codex_version"],
                    ["source_sha256"] = authority["source_sha256"],
                };
            }
            for (var i = 0; i < reader.FieldCount; i++)
                row[reader.GetName(i)] =
                    reader.IsDBNull(i) ? null : reader.GetValue(i);
        }
        AssertGeneration(authority, PgExport.AuthorityState());
        var result = EvaluateEvidence(row, (string)authority["codex_version"]!);
        result["recipe"] = Recipe;
        result["descriptor"] = CanonicalDescriptorJson(row);
        result["generation"] = authority["codex_version"];
        result["source_sha256"] = authority["source_sha256"];
        return result;
    }

    public static Dictionary<string, object?> Probe()
    {
        var authority = PgExport.AuthorityState();
        AssertGeneration(authority, authority);
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT r.*, {EvidenceColumns} "
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
            results.Add(EvaluateEvidence(row, (string)authority["codex_version"]!));
        }
        AssertGeneration(authority, PgExport.AuthorityState());
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["artifact"] = "machine-schema-parity-probe/v1",
            ["recipe"] = Recipe,
            ["generation"] = authority["codex_version"],
            ["source_sha256"] = authority["source_sha256"],
            ["descriptor_fields"] = DescriptorFields,
            ["rows"] = results.Count,
            ["producer_validator_pass"] = results.Count(r =>
                (string)r["producer_validator_layer"]! == "PASS"),
            ["canonical_match"] = results.Count(r =>
                (string)r["persistence_layer"]! == "MATCH"),
            ["current_evidence_pass"] = results.Count(r =>
                (string)r["current_evidence_status"]! == "PASS"),
            ["current_evidence_open"] = results.Count(r =>
                (string)r["current_evidence_status"]! != "PASS"),
            ["note"] = "CANONICAL_MISMATCH is evidence, not a pass; "
                + "restamp or descriptor-projection publication is a "
                + "governor action. This probe never writes parity "
                + "state. Registry labels and descriptor hash matches do not "
                + "certify current-generation evidence.",
            ["results"] = results,
        };
    }
}
