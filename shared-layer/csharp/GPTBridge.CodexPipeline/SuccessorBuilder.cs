using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

/// <summary>Fail-closed candidate construction denial — parity with
/// ``SuccessorBuildError``.</summary>
internal class SuccessorBuildError : Exception
{
    public string Code { get; }
    public string Detail { get; }

    public SuccessorBuildError(string code, string detail = "")
        : base(detail.Length > 0 ? $"{code}:{detail}" : code)
    {
        Code = code;
        Detail = detail;
    }
}

internal sealed record SuccessorBuildResult(
    bool Ok,
    string RequestId,
    string OutputDatabase,
    string ManifestPath,
    string CandidateSha256 = "",
    List<Dictionary<string, object?>>? Applied = null,
    List<Dictionary<string, object?>>? Deferred = null,
    List<string>? Errors = null,
    Dictionary<string, object?>? SealPreview = null)
{
    public Dictionary<string, object?> AsDict() =>
        new(StringComparer.Ordinal)
        {
            ["ok"] = Ok,
            ["request_id"] = RequestId,
            ["output_database"] = OutputDatabase,
            ["manifest_path"] = ManifestPath,
            ["candidate_sha256"] = CandidateSha256,
            ["applied"] = Applied ?? new(),
            ["deferred"] = Deferred ?? new(),
            ["errors"] = (Errors ?? new()).Cast<object?>().ToList(),
            ["seal_preview"] = SealPreview
                ?? new Dictionary<string, object?>(),
        };
}

/// <summary>
/// Governed Codex successor candidate builder (G69) — direct port of
/// codex_successor_builder.py.
///
/// Consumes a request artifact and produces a candidate ``.sql``
/// generation at an explicit output path.  Never mutates the source.
/// Publication, sealing roots, revision/epoch-seal writes and authority
/// re-anchoring remain governor-side; the unanimous five-sovereign audit
/// certificate closes the seal.
/// </summary>
internal static partial class SuccessorBuilder
{
    public const string CandidateManifestSchema =
        "gptbridge-codex-candidate-manifest/v1";
    public const string FormalRuleRegistry = "formal_rule_registry";

    private static readonly HashSet<string> SuccessorSentinels = new(
        StringComparer.Ordinal)
    {
        "successor", "<successor>", "<successor-version>",
        "successor_version", "next-authoritative-utc-second",
    };

    private static string Quote(string identifier) =>
        "\"" + identifier.Replace("\"", "\"\"") + "\"";

    private sealed class ColumnInfo
    {
        public string Name = "";
        public string Type = "";
        public bool NotNull;
        public object? Default;
        public int Pk;
    }

    private static List<ColumnInfo> TableColumns(
        StageConnection connection, string table)
    {
        var cursor = connection.Execute(
            $"PRAGMA table_info({Quote(table)})");
        return cursor.Rows.Select(row => new ColumnInfo
        {
            Name = row[1]?.ToString() ?? "",
            Type = row[2]?.ToString() ?? "",
            NotNull = Convert.ToInt32(row[3]) != 0,
            Default = row[4],
            Pk = Convert.ToInt32(row[5] ?? 0),
        }).ToList();
    }

    private static List<ColumnInfo> RequireTable(
        StageConnection connection, string table)
    {
        var columns = TableColumns(connection, table);
        if (columns.Count == 0)
            throw new SuccessorBuildError("CANDIDATE_TABLE_MISSING", table);
        return columns;
    }

    private static Dictionary<string, object?> NormalizedRow(
        List<ColumnInfo> columns, JsonObject row, string table,
        bool requirePrimary = true)
    {
        var names = new HashSet<string>(columns.Select(c => c.Name),
            StringComparer.Ordinal);
        var normalized = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        foreach (var pair in row)
            normalized[pair.Key] = Repo.ToPlain(pair.Value);
        if (table == FormalRuleRegistry && names.Contains("rule_code")
            && normalized.ContainsKey("rule_id"))
        {
            normalized["rule_code"] = normalized["rule_id"];
            normalized.Remove("rule_id");
        }
        var unknown = normalized.Keys.Where(n => !names.Contains(n))
            .OrderBy(n => n, StringComparer.Ordinal).ToList();
        if (unknown.Count > 0)
            throw new SuccessorBuildError("CANDIDATE_COLUMN_UNKNOWN",
                $"{table}:{string.Join(",", unknown)}");
        var missingPrimary = columns
            .Where(c => c.Pk != 0)
            .Select(c => c.Name)
            .Where(name => requirePrimary
                && (normalized.TryGetValue(name, out var v)
                    ? v?.ToString() ?? "" : "").Trim().Length == 0)
            .ToList();
        if (missingPrimary.Count > 0)
            throw new SuccessorBuildError(
                "CANDIDATE_PRIMARY_KEY_INCOMPLETE",
                $"{table}:{string.Join(",", missingPrimary)}");
        return normalized;
    }

    private static object? SubstituteSuccessor(object? value,
        string? successorVersion, string context)
    {
        if (value is string text
            && SuccessorSentinels.Contains(text.Trim()))
        {
            if (string.IsNullOrEmpty(successorVersion))
                throw new SuccessorBuildError(
                    "SUCCESSOR_VERSION_REQUIRED", context);
            return successorVersion;
        }
        if (value is IDictionary<string, object?> map)
        {
            var result = new Dictionary<string, object?>(
                StringComparer.Ordinal);
            foreach (var pair in map)
                result[pair.Key] = SubstituteSuccessor(pair.Value,
                    successorVersion, $"{context}.{pair.Key}");
            return result;
        }
        if (value is IList<object?> list)
            return list.Select(item => SubstituteSuccessor(item,
                successorVersion, context)).ToList();
        return value;
    }

    private static int ExistingCount(StageConnection connection,
        string table, Dictionary<string, object?> key)
    {
        if (key.Count == 0)
            throw new SuccessorBuildError("CANDIDATE_KEY_REQUIRED", table);
        var columns = RequireTable(connection, table);
        var names = new HashSet<string>(columns.Select(c => c.Name),
            StringComparer.Ordinal);
        var unknown = key.Keys.Where(n => !names.Contains(n))
            .OrderBy(n => n, StringComparer.Ordinal).ToList();
        if (unknown.Count > 0)
            throw new SuccessorBuildError("CANDIDATE_KEY_COLUMN_UNKNOWN",
                $"{table}:{string.Join(",", unknown)}");
        var sorted = key.Keys.OrderBy(k => k, StringComparer.Ordinal)
            .ToList();
        var where = string.Join(" AND ",
            sorted.Select(name => $"{Quote(name)} IS ?"));
        // sql-ok: identifiers composed via Quote
        var cursor = connection.Execute(
            $"SELECT COUNT(*) FROM {Quote(table)} WHERE {where}",
            sorted.Select(name => key[name]).ToList());
        return Convert.ToInt32(cursor.FetchOne()?[0] ?? 0);
    }

    private static Dictionary<string, object?>? ExistingRow(
        StageConnection connection, string table,
        Dictionary<string, object?> key)
    {
        var columns = RequireTable(connection, table);
        var names = columns.Select(c => c.Name).ToList();
        var sorted = key.Keys.OrderBy(k => k, StringComparer.Ordinal)
            .ToList();
        var where = string.Join(" AND ",
            sorted.Select(name => $"{Quote(name)} IS ?"));
        var cursor = connection.Execute( // sql-ok: quoted identifiers
            $"SELECT {string.Join(", ", names.Select(Quote))} "
            + $"FROM {Quote(table)} WHERE {where}",
            sorted.Select(name => key[name]).ToList());
        var row = cursor.FetchOne();
        if (row is null)
            return null;
        var result = new Dictionary<string, object?>(
            StringComparer.Ordinal);
        for (var i = 0; i < names.Count; i++)
            result[names[i]] = row[i];
        return result;
    }

    private static HashSet<string> EvaluatorCodes(
        StageConnection connection)
    {
        try
        {
            return EvaluatorRegistry.RegisteredRuleCodes();
        }
        catch (Exception error) when (error
            is InvalidOperationException or IOException)
        {
            throw new SuccessorBuildError(
                "FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE",
                error.Message);
        }
    }

    private static void ValidateFormalRuleTransition(
        StageConnection connection, Dictionary<string, object?> key,
        Dictionary<string, object?> fields)
    {
        if (!fields.ContainsKey("status"))
            return;
        var row = ExistingRow(connection, FormalRuleRegistry, key);
        if (row is null)
            return;
        var ruleCode = key.TryGetValue("rule_code", out var rc)
            ? rc?.ToString() ?? ""
            : key.TryGetValue("rule_id", out var ri)
                ? ri?.ToString() ?? "" : "";
        var parityEvidence =
            fields.TryGetValue("parity_evidence_id", out var pe)
                && RepoTruthy.TruthyText(pe)
            || row.TryGetValue("parity_evidence_id", out var rpe)
                && RepoTruthy.TruthyText(rpe)
            || string.Equals(
                (fields.TryGetValue("parity_status", out var ps)
                    ? ps?.ToString() ?? ""
                    : row.TryGetValue("parity_status", out var rps)
                        ? rps?.ToString() ?? "" : "").Trim(),
                "VERIFIED", StringComparison.OrdinalIgnoreCase);
        var errors = AmendmentContract.ValidateRuleTransition(
            row.TryGetValue("status", out var st) ? st : null,
            fields["status"],
            EvaluatorCodes(connection).Contains(ruleCode),
            parityEvidence);
        if (errors.Count > 0)
            throw new SuccessorBuildError(
                "RULE_STATE_TRANSITION_INVALID",
                $"{(ruleCode.Length > 0 ? ruleCode
                    : AmendmentContract.ContentHash(key))}:"
                + string.Join(",", errors));
    }

    private static Dictionary<string, object?> InsertRow(
        StageConnection connection, string table, JsonObject row,
        string? successorVersion)
    {
        var columns = RequireTable(connection, table);
        var normalized = NormalizedRow(columns, row, table);
        foreach (var name in normalized.Keys.ToList())
            normalized[name] = SubstituteSuccessor(normalized[name],
                successorVersion, $"{table}.{name}");
        var primary = columns.Where(c => c.Pk != 0)
            .Select(c => c.Name).ToList();
        var identity = primary.Count > 0
            ? primary.Where(normalized.ContainsKey)
                .ToDictionary(n => n, n => normalized[n],
                    StringComparer.Ordinal)
            : normalized;
        if (ExistingCount(connection, table, identity) > 0)
            throw new SuccessorBuildError("CANDIDATE_DUPLICATE_ROW",
                $"{table}:{AmendmentContract.ContentHash(identity)}");
        var names = normalized.Keys
            .OrderBy(n => n, StringComparer.Ordinal).ToList();
        connection.Execute( // sql-ok: identifiers composed via Quote
            $"INSERT INTO {Quote(table)} "
            + $"({string.Join(", ", names.Select(Quote))}) "
            + $"VALUES ({string.Join(", ", names.Select(_ => "?"))})",
            names.Select(n => normalized[n]).ToList());
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["action"] = "insert", ["table"] = table,
            ["row"] = normalized,
        };
    }

}
