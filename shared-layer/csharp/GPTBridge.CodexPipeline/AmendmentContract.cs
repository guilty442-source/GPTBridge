using System.Security.Cryptography;
using System.Text;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Codex amendment hash and formal-rule state contract (G69/G70) —
/// direct port of codex_amendment_contract.py.
///
/// ``ComputeSealPreview`` produces a *candidate preview* only: seal
/// closure (unanimous five-sovereign certificate) and version-axis
/// publication remain governed authority.
/// </summary>
internal static class AmendmentContract
{
    public const string ContentHashAlgorithm = "sha256-canonical-json-utf8-v1";
    public const string SearchDocumentHashAlgorithm = "sha256-utf8-content-v1";
    public const string RevisionEntryHashAlgorithm =
        "sha256-canonical-utf8-pipe-v1";
    public const string SealPreviewSchema = "gptbridge-codex-seal-preview/v1";

    public static readonly string[] ContentTables =
    {
        "preamble", "principles", "sections", "articles", "edicts",
        "sovereigns", "savings",
    };

    public const string RuleStateProposed = "proposed";
    public const string RuleStateDeclaredPendingParity =
        "declared-pending-evaluator-parity";
    public const string RuleStateParityVerified = "evaluator-parity-verified";
    public const string RuleStateActive = "active";

    public static readonly HashSet<string> TerminalRuleStates = new(
        StringComparer.Ordinal)
    { "retired", "superseded", "withdrawn", "inactive" };

    public static readonly HashSet<string> NonterminalRuleStates = new(
        StringComparer.Ordinal)
    {
        RuleStateProposed, RuleStateDeclaredPendingParity,
        RuleStateParityVerified, RuleStateActive,
    };

    public static readonly Dictionary<string, HashSet<string>>
        RuleStateTransitions = new(StringComparer.Ordinal)
        {
            [RuleStateProposed] = new(StringComparer.Ordinal)
            { RuleStateDeclaredPendingParity, "withdrawn" },
            [RuleStateDeclaredPendingParity] = new(StringComparer.Ordinal)
            { RuleStateParityVerified, "withdrawn" },
            [RuleStateParityVerified] = new(StringComparer.Ordinal)
            { RuleStateActive, "retired", "superseded", "withdrawn" },
            [RuleStateActive] = new(StringComparer.Ordinal)
            { "retired", "superseded", "inactive" },
        };

    public static string CanonicalJson(object? payload) =>
        CanonJson.Serialize(payload);

    private static string Sha256Hex(byte[] data) =>
        Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();

    /// <summary>Reproducible lineage/content hash for structured
    /// amendment payloads — sha256 over NFC(canonical_json).utf8.</summary>
    public static string ContentHash(object? payload) =>
        Sha256Hex(CanonJson.CanonicalUtf8(CanonicalJson(payload)));

    /// <summary>Hash one search/document payload exactly as stored.</summary>
    public static string SearchDocumentHash(object content) =>
        Sha256Hex(content is byte[] bytes ? bytes
            : Encoding.UTF8.GetBytes(content.ToString() ?? ""));

    /// <summary>``sha256-canonical-utf8-pipe-v1`` revision-chain hash:
    /// pipe-joined canonical JSON of named fields, sorted, hash field
    /// excluded.</summary>
    public static string RevisionEntryHash(
        IReadOnlyDictionary<string, object?> fields,
        IEnumerable<string>? exclude = null)
    {
        var excluded = new HashSet<string>(
            exclude ?? new[] { "entry_hash" }, StringComparer.Ordinal);
        var parts = fields
            .Where(pair => !excluded.Contains(pair.Key))
            .OrderBy(pair => pair.Key, StringComparer.Ordinal)
            .Select(pair => CanonicalJson(pair.Value));
        return Sha256Hex(
            Encoding.UTF8.GetBytes(string.Join("|", parts)));
    }

    // -- seal preview ----------------------------------------------------

    private static List<(string Name, bool Primary)> TableColumns(
        StageConnection connection, string table)
    {
        var cursor = connection.Execute($"PRAGMA table_info({table})");
        var columns = new List<(string, bool)>();
        foreach (var row in cursor.Rows)
            columns.Add((row[1]?.ToString() ?? "",
                Convert.ToInt32(row[5]) != 0));
        return columns;
    }

    private static List<string> TableNames(StageConnection connection)
    {
        var cursor = connection.Execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            + "AND name NOT LIKE 'sqlite_%' ORDER BY name");
        return cursor.Rows
            .Select(row => row[0]?.ToString() ?? "").ToList();
    }

    private static List<Dictionary<string, object?>> TableRows(
        StageConnection connection, string table)
    {
        var columns = TableColumns(connection, table);
        if (columns.Count == 0)
            return new List<Dictionary<string, object?>>();
        var names = columns.Select(c => c.Name).ToList();
        var primary = columns.Where(c => c.Primary)
            .Select(c => c.Name).ToList();
        var order = primary.Count > 0
            ? string.Join(", ", primary) : "rowid";
        var cursor = connection.Execute( // sql-ok: introspected names
            $"SELECT {string.Join(", ", names)} FROM {table} "
            + $"ORDER BY {order}");
        var rows = new List<Dictionary<string, object?>>();
        foreach (var row in cursor.Rows)
        {
            var entry = new Dictionary<string, object?>(
                StringComparer.Ordinal);
            for (var i = 0; i < names.Count; i++)
                entry[names[i]] = row[i];
            rows.Add(entry);
        }
        rows.Sort((a, b) => string.CompareOrdinal(
            CanonicalJson(a), CanonicalJson(b)));
        return rows;
    }

    private static Dictionary<string, object?> TableFingerprints(
        StageConnection connection, IEnumerable<string> tables)
    {
        var fingerprints = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var table in tables)
            fingerprints[table] = ContentHash(TableRows(connection, table));
        return fingerprints;
    }

    /// <summary>Fingerprint a live codex store connection into
    /// seal-preview roots — identical to ``_seal_preview_from``.</summary>
    public static Dictionary<string, object?> SealPreviewFrom(
        StageConnection connection)
    {
        var allTables = TableNames(connection);
        var contentTables = ContentTables
            .Where(allTables.Contains).ToList();
        var identityTables = allTables
            .Where(t => t.EndsWith("_directory", StringComparison.Ordinal)
                || t.EndsWith("_registry", StringComparison.Ordinal))
            .ToList();
        var fingerprints = TableFingerprints(connection, allTables);
        var contentMap = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var table in contentTables)
            contentMap[table] = fingerprints[table];
        var identityMap = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var table in identityTables)
            identityMap[table] = fingerprints[table];
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["schema"] = SealPreviewSchema,
            ["algorithm"] = ContentHashAlgorithm,
            ["content_root"] = ContentHash(contentMap),
            ["identity_root"] = ContentHash(identityMap),
            ["full_root"] = ContentHash(fingerprints),
            ["table_count"] = allTables.Count,
            ["content_tables"] = contentTables,
            ["identity_tables"] = identityTables,
            ["table_fingerprints"] = fingerprints,
        };
    }

    // -- open_codex_store ---------------------------------------------------

    /// <summary>Parity with ``open_codex_store``: a readable ``.sql``
    /// artifact materializes into a throwaway stage schema (dropped on
    /// dispose); anything else reads the live authority through the
    /// governed read-only connection.</summary>
    public static CodexStore OpenCodexStore(string? database,
        bool writeBack = false)
    {
        if (database is not null
            && database.EndsWith(".sql", StringComparison.Ordinal)
            && File.Exists(database))
            return CodexStore.ForArtifact(database, writeBack);
        return CodexStore.ForAuthority();
    }

    /// <summary>``compute_seal_preview`` — candidate roots for review,
    /// never authoritative sealing.</summary>
    public static Dictionary<string, object?> ComputeSealPreview(
        string database)
    {
        using var store = OpenCodexStore(database);
        return SealPreviewFrom(store.Connection);
    }

    // -- rule-state validation --------------------------------------------

    public static List<string> ValidateRuleState(object? status,
        bool? evaluatorRegistered, bool parityEvidence = false)
    {
        var state = (status?.ToString() ?? "").Trim().ToLowerInvariant();
        if (state.Length == 0)
            return new List<string> { "RULE_STATE_REQUIRED" };
        if (TerminalRuleStates.Contains(state))
            return new List<string>();
        if (!NonterminalRuleStates.Contains(state))
            return new List<string> { $"RULE_STATE_UNKNOWN:{state}" };
        var errors = new List<string>();
        if (evaluatorRegistered is not true)
            errors.Add("RULE_EVALUATOR_REQUIRED");
        if (state == RuleStateParityVerified && !parityEvidence)
            errors.Add("RULE_PARITY_EVIDENCE_REQUIRED");
        return errors;
    }

    public static List<string> ValidateRuleTransition(object? current,
        object? target, bool? evaluatorRegistered,
        bool parityEvidence = false)
    {
        var currentState = (current?.ToString() ?? "")
            .Trim().ToLowerInvariant();
        var targetState = (target?.ToString() ?? "")
            .Trim().ToLowerInvariant();
        var errors = ValidateRuleState(currentState,
            evaluatorRegistered, parityEvidence);
        if (errors.Count > 0)
            return errors;
        if (TerminalRuleStates.Contains(currentState))
            return new List<string>
            { $"RULE_STATE_TERMINAL:{currentState}" };
        if (!RuleStateTransitions.TryGetValue(currentState,
                out var allowed)
            || !allowed.Contains(targetState))
            return new List<string>
            {
                $"RULE_STATE_TRANSITION_DENIED:"
                + $"{currentState}->{targetState}"
            };
        if (targetState == RuleStateActive && !parityEvidence)
            return new List<string> { "RULE_PARITY_EVIDENCE_REQUIRED" };
        return ValidateRuleState(targetState,
            evaluatorRegistered, parityEvidence);
    }
}

/// <summary>Owner for ``OpenCodexStore`` — drops materialized stage
/// schemas and optionally dumps mutations back (write_back).</summary>
internal sealed class CodexStore : IDisposable
{
    public StageConnection Connection { get; }

    private readonly string? _artifact;
    private readonly string? _schema;
    private bool _writeBack;

    /// <summary>Parity: on exception the Python contextmanager skips
    /// write-back entirely (``yield`` propagates before dump_schema).
    /// Call inside a catch before Dispose.</summary>
    public void SuppressWriteBack() => _writeBack = false;

    private CodexStore(StageConnection connection, string? artifact,
        string? schema, bool writeBack)
    {
        Connection = connection;
        _artifact = artifact;
        _schema = schema;
        _writeBack = writeBack;
    }

    public static CodexStore ForArtifact(string artifact, bool writeBack)
    {
        var fullPath = Path.GetFullPath(artifact);
        var token = Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(
                Encoding.UTF8.GetBytes(fullPath)))
            .ToLowerInvariant()[..16];
        var schema = StageCodec.StageName(token);
        StageCodec.MaterializeArtifact(artifact, schema);
        return new CodexStore(new StageConnection(schema), artifact,
            schema, writeBack);
    }

    public static CodexStore ForAuthority() =>
        new(new StageConnection(PgDsn.CodexSchema,
            PgDsn.RuntimeDsn(), transactional: false), null, null, false);

    public void Dispose()
    {
        if (_schema is not null)
        {
            try
            {
                if (_writeBack && _artifact is not null)
                {
                    Connection.Commit();
                    StageCodec.DumpSchema(_schema, _artifact);
                }
            }
            finally
            {
                Connection.Dispose();
                StageCodec.DropSchema(_schema);
            }
        }
        else
        {
            Connection.Dispose();
        }
    }
}
