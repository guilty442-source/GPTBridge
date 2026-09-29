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
internal static class SuccessorBuilder
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

    private static Dictionary<string, object?> UpdateRows(
        StageConnection connection, string table,
        Dictionary<string, object?> key, JsonObject fields,
        string? successorVersion)
    {
        var columns = RequireTable(connection, table);
        var normalized = NormalizedRow(columns, fields, table,
            requirePrimary: false);
        foreach (var name in normalized.Keys.ToList())
            normalized[name] = SubstituteSuccessor(normalized[name],
                successorVersion, $"{table}.{name}");
        var count = ExistingCount(connection, table, key);
        if (count != 1)
            throw new SuccessorBuildError("CANDIDATE_ROW_NOT_UNIQUE",
                $"{table}:{count}");
        var sortedFields = normalized.Keys
            .OrderBy(n => n, StringComparer.Ordinal).ToList();
        var sortedKeys = key.Keys
            .OrderBy(n => n, StringComparer.Ordinal).ToList();
        var assignments = string.Join(", ",
            sortedFields.Select(name => $"{Quote(name)} = ?"));
        var where = string.Join(" AND ",
            sortedKeys.Select(name => $"{Quote(name)} IS ?"));
        connection.Execute( // sql-ok: identifiers composed via Quote
            $"UPDATE {Quote(table)} SET {assignments} WHERE {where}",
            sortedFields.Select(n => normalized[n])
                .Concat(sortedKeys.Select(n => key[n])).ToList());
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["action"] = "update", ["table"] = table,
            ["key"] = key, ["fields"] = normalized,
        };
    }

    private static (List<Dictionary<string, object?>> Applied,
        List<Dictionary<string, object?>> Deferred) ApplyChanges(
            StageConnection connection, JsonObject payload,
            string? successorVersion)
    {
        var applied = new List<Dictionary<string, object?>>();
        var deferred = new List<Dictionary<string, object?>>();

        var changesNode = Repo.Get(payload, "changes");
        if (changesNode is JsonObject changesMap
            && changesMap.Count > 0)
            // Python iterates a Mapping's keys → first item is a str →
            // CHANGE_NOT_AN_OBJECT
            throw new SuccessorBuildError("CHANGE_NOT_AN_OBJECT", "0");
        if (changesNode is JsonArray changes)
        {
            var index = 0;
            foreach (var item in changes)
            {
                if (item is not JsonObject map)
                    throw new SuccessorBuildError(
                        "CHANGE_NOT_AN_OBJECT", index.ToString());
                var table = Repo.Str(map, "table").Trim();
                var key = Repo.Get(map, "key") as JsonObject;
                var field = Repo.Str(map, "field").Trim();
                if (table.Length == 0 || key is null
                    || field.Length == 0)
                    throw new SuccessorBuildError(
                        "CHANGE_CONTRACT_INVALID", index.ToString());
                var fields = new JsonObject
                {
                    [field] = Repo.Get(map, "proposed")?.DeepClone(),
                };
                if (Repo.Get(map, "also") is JsonObject also)
                    foreach (var pair in also.ToList())
                        fields[pair.Key] = pair.Value?.DeepClone();
                var keyDict = (Dictionary<string, object?>)
                    Repo.ToPlain(key)!;
                if (table == FormalRuleRegistry)
                    ValidateFormalRuleTransition(connection, keyDict,
                        NormalizedRow(RequireTable(connection, table),
                            fields, table, requirePrimary: false));
                applied.Add(UpdateRows(connection, table, keyDict,
                    fields, successorVersion));
                index += 1;
            }
        }

        if (Repo.Get(payload, "proposed_change") is JsonObject proposed)
        {
            var table = Repo.Str(proposed, "table").Trim();
            var operation = Repo.Str(proposed, "operation").Trim()
                .ToLowerInvariant();
            var action = Repo.Str(proposed, "action").Trim()
                .ToLowerInvariant();
            var rows = Repo.Get(proposed, "rows") as JsonArray;
            if (table.Length > 0 && !table.Contains('/')
                && (action == "insert" || operation.Contains("insert"))
                && rows is not null)
            {
                foreach (var row in rows)
                {
                    if (row is not JsonObject map)
                        throw new SuccessorBuildError(
                            "SUCCESSOR_ROW_INVALID", table);
                    applied.Add(InsertRow(connection, table, map,
                        successorVersion));
                }
            }
            else
            {
                deferred.Add(new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["action"] = "deferred",
                    ["reason"] = "non-canonical proposed_change requires "
                        + "governor normalization",
                    ["payload"] = Repo.ToPlain(proposed),
                });
            }
        }

        foreach (var proposalKey in new[] { "proposed_repair",
            "proposed_resolution" })
            if (Repo.Get(payload, proposalKey) is JsonObject proposal)
                deferred.Add(new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["action"] = "deferred",
                    ["reason"] = $"{proposalKey} requires governor "
                        + "normalization",
                    ["payload"] = Repo.ToPlain(proposal),
                });

        IEnumerable<JsonNode?> successors =
            Repo.Get(payload, "proposed_successors") switch
            {
                JsonObject single => new[] { single },
                JsonArray array => array,
                _ => Enumerable.Empty<JsonNode?>(),
            };
        var index2 = 0;
        foreach (var item in successors)
        {
            if (item is not JsonObject map)
                throw new SuccessorBuildError(
                    "SUCCESSOR_NOT_AN_OBJECT", index2.ToString());
            var registry = (Repo.Str(map, "registry").Trim().Length > 0
                ? Repo.Str(map, "registry")
                : Repo.Str(map, "table")).Trim();
            var action = Repo.Str(map, "action").Trim().ToLowerInvariant();
            if (registry.Length == 0)
            {
                deferred.Add(new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["action"] = "deferred",
                    ["index"] = index2,
                    ["reason"] =
                        "governor-provision-or-artifact-assignment",
                    ["payload"] = Repo.ToPlain(map),
                });
                index2 += 1;
                continue;
            }
            if (action == "insert")
            {
                if (Repo.Get(map, "rows") is not JsonArray rows)
                    throw new SuccessorBuildError(
                        "SUCCESSOR_ROWS_REQUIRED", registry);
                foreach (var row in rows)
                {
                    if (row is not JsonObject rowMap)
                        throw new SuccessorBuildError(
                            "SUCCESSOR_ROW_INVALID", registry);
                    applied.Add(InsertRow(connection, registry, rowMap,
                        successorVersion));
                }
                index2 += 1;
                continue;
            }
            if (action == "update")
            {
                var key = Repo.Get(map, "key") as JsonObject;
                var fields = (Repo.Get(map, "set")
                    ?? Repo.Get(map, "fields")) as JsonObject;
                if (key is null || fields is null)
                    throw new SuccessorBuildError(
                        "SUCCESSOR_UPDATE_INVALID", registry);
                var keyDict = (Dictionary<string, object?>)
                    Repo.ToPlain(key)!;
                if (registry == FormalRuleRegistry)
                    ValidateFormalRuleTransition(connection, keyDict,
                        NormalizedRow(RequireTable(connection, registry),
                            fields, registry, requirePrimary: false));
                applied.Add(UpdateRows(connection, registry, keyDict,
                    fields, successorVersion));
                index2 += 1;
                continue;
            }
            deferred.Add(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["action"] = "deferred",
                ["index"] = index2,
                ["registry"] = registry,
                ["requested_action"] =
                    action.Length > 0 ? action : "unspecified",
                ["reason"] = "artifact rebind, derived-registry rebuild "
                    + "or governor-side row set",
                ["payload"] = Repo.ToPlain(map),
            });
            index2 += 1;
        }

        if (Repo.Get(payload, "proposed_successor") is JsonObject
            singular)
            deferred.Add(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["action"] = "deferred",
                ["reason"] = "governor-provision-id-and-normative-text-"
                    + "assignment",
                ["payload"] = Repo.ToPlain(singular),
            });
        return (applied, deferred);
    }

    private static void SetCandidateVersion(StageConnection connection,
        string? successorVersion)
    {
        if (string.IsNullOrEmpty(successorVersion))
            return;
        try
        {
            CodexVersion.Units(successorVersion);
        }
        catch (ArgumentException)
        {
            throw new SuccessorBuildError("SUCCESSOR_VERSION_INVALID",
                successorVersion);
        }
        var columns = RequireTable(connection, "metadata");
        var names = new HashSet<string>(columns.Select(c => c.Name),
            StringComparer.Ordinal);
        if (!names.Contains("key") || !names.Contains("value"))
            throw new SuccessorBuildError("METADATA_CONTRACT_INCOMPLETE");
        var cursor = connection.Execute(
            "UPDATE metadata SET value=? WHERE key='codex_version'",
            new object?[] { successorVersion });
        if (cursor.RowCount == 0)
            connection.Execute(
                "INSERT INTO metadata (key, value) "
                + "VALUES ('codex_version', ?)",
                new object?[] { successorVersion });
    }

    private static string[] FormalRuleErrors(StageConnection connection)
    {
        var columns = TableColumns(connection, FormalRuleRegistry);
        if (columns.Count == 0)
            return Array.Empty<string>();
        var names = new HashSet<string>(columns.Select(c => c.Name),
            StringComparer.Ordinal);
        var statusColumn = names.Contains("status") ? "status" : "";
        var codeColumn = new[] { "rule_code", "rule_id", "code" }
            .FirstOrDefault(names.Contains) ?? "";
        if (statusColumn.Length == 0 || codeColumn.Length == 0)
            return new[] { "FORMAL_RULE_REGISTRY_CONTRACT_INCOMPLETE" };
        HashSet<string> evaluatorCodes;
        try
        {
            evaluatorCodes = EvaluatorRegistry.RegisteredRuleCodes();
        }
        catch (Exception)
        {
            return new[]
            { "FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE" };
        }
        var parityColumns = new[] { "parity_evidence_id", "parity_status" }
            .Where(names.Contains).ToList();
        var selected = string.Join(", ",
            new[] { Quote(codeColumn), Quote(statusColumn) }
                .Concat(parityColumns.Select(Quote)));
        var errors = new List<string>();
        var cursor = connection.Execute( // sql-ok: quoted identifiers
            $"SELECT {selected} FROM {Quote(FormalRuleRegistry)}");
        foreach (var row in cursor.Rows)
        {
            var code = row[0]?.ToString() ?? "";
            var status = row[1];
            var parityEvidence = false;
            for (var i = 0; i < parityColumns.Count; i++)
            {
                var value = row[2 + i];
                if (parityColumns[i] == "parity_evidence_id"
                    && RepoTruthy.TruthyText(value))
                    parityEvidence = true;
                if (parityColumns[i] == "parity_status"
                    && string.Equals((value?.ToString() ?? "").Trim(),
                        "VERIFIED", StringComparison.OrdinalIgnoreCase))
                    parityEvidence = true;
            }
            foreach (var error in AmendmentContract.ValidateRuleState(
                status, evaluatorCodes.Contains(code), parityEvidence))
                errors.Add($"{FormalRuleRegistry}:{code}:{error}");
        }
        return errors.ToArray();
    }

    /// <summary>Build one validated candidate and record its lineage
    /// evidence.</summary>
    public static SuccessorBuildResult BuildSuccessor(
        string requestPath,
        string sourceDatabase,
        string outputDatabase,
        CodexAmendmentRequestLedger ledger,
        string? successorVersion = null,
        string? expectedCurrentVersion = null,
        long? expectedRevisionSequence = null)
    {
        var requestId = "";
        var outputCreated = false;
        var manifestPath = Path.ChangeExtension(
            Path.GetFullPath(outputDatabase),
            ".candidate-manifest.json");
        try
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            requestId = request.RequestId;
            var source = Path.GetFullPath(sourceDatabase);
            var output = Path.GetFullPath(outputDatabase);
            if (!File.Exists(source))
                throw new SuccessorBuildError(
                    "SOURCE_DATABASE_MISSING", source);
            if (string.Equals(source, output, StringComparison.Ordinal))
                throw new SuccessorBuildError(
                    "CANDIDATE_MUST_NOT_OVERWRITE_SOURCE");
            if (File.Exists(output))
                throw new SuccessorBuildError(
                    "CANDIDATE_OUTPUT_EXISTS", output);
            if (File.Exists(manifestPath))
                throw new SuccessorBuildError(
                    "CANDIDATE_MANIFEST_EXISTS", manifestPath);
            Directory.CreateDirectory(Path.GetDirectoryName(output)!);
            List<string[]> baselineViolations;
            using (var store =
                AmendmentContract.OpenCodexStore(source))
                baselineViolations =
                    UpdateValidation.ForeignKeyViolations(store.Connection);
            var record = ledger.Begin(requestPath,
                currentVersion: expectedCurrentVersion,
                expectedRevisionSequence: expectedRevisionSequence);
            if (record.State == Lifecycle.StateSubmitted)
                ledger.Transition(requestId, Lifecycle.StateUnderReview);
            else if (record.State != Lifecycle.StateUnderReview)
                throw new SuccessorBuildError("REQUEST_NOT_UNDER_REVIEW",
                    $"{requestId}:{record.State}");
            File.Copy(source, output, overwrite: false);
            outputCreated = true;
            var errors = new List<string>();
            List<Dictionary<string, object?>> applied;
            List<Dictionary<string, object?>> deferred;
            // Parity: open_artifact(write_back=True) always dumps the
            // mutated schema back over the candidate artifact on exit;
            // a rejected candidate is then removed by unlink below.
            using (var store = AmendmentContract.OpenCodexStore(output,
                writeBack: true))
            {
                var connection = store.Connection;
                SetCandidateVersion(connection, successorVersion);
                (applied, deferred) = ApplyChanges(connection,
                    request.Payload, successorVersion);
                connection.Commit();
                errors.AddRange(FormalRuleErrors(connection));
            }
            errors.AddRange(UpdateValidation.StagedGenerationErrors(
                output, version: successorVersion,
                baselineViolations: baselineViolations));
            if (errors.Count > 0)
            {
                try { File.Delete(output); outputCreated = false; }
                catch (IOException) { }
                ledger.Transition(requestId, Lifecycle.StateRejected,
                    new Dictionary<string, object?>
                    { ["errors"] = errors.Cast<object?>().ToList() });
                return new SuccessorBuildResult(false, requestId,
                    output, manifestPath, Errors: errors);
            }
            var sealPreview =
                AmendmentContract.ComputeSealPreview(output);
            var candidateSha256 = FileSha256(output);
            var manifest = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["schema"] = CandidateManifestSchema,
                ["request_id"] = requestId,
                ["request_hash"] = request.RequestHash,
                ["lineage_key"] = request.LineageKey,
                ["source_database_sha256"] = FileSha256(source),
                ["candidate_sha256"] = candidateSha256,
                ["successor_version"] = successorVersion ?? "",
                ["predecessor"] = request.Predecessor,
                ["scope"] = request.Scope.ToList(),
                ["applied"] = applied,
                ["deferred"] = deferred,
                ["seal_preview_schema"] =
                    AmendmentContract.SealPreviewSchema,
                ["seal_preview"] = sealPreview,
                ["governor_only"] = new List<object?>
                {
                    "seal_manifest", "epoch_seal_manifest",
                    "revision_history", "atomic-publication",
                    "authority-reanchor",
                },
                ["created_at"] = Repo.UtcNow(),
                ["manifest_hash_algorithm"] =
                    AmendmentContract.ContentHashAlgorithm,
                ["manifest_hash_excludes"] = new List<object?>
                { "manifest_hash" },
            };
            manifest["manifest_hash"] = AmendmentContract.ContentHash(
                manifest.Where(pair => pair.Key != "manifest_hash")
                    .ToDictionary(pair => pair.Key, pair => pair.Value,
                        StringComparer.Ordinal));
            Repo.AtomicJson(manifestPath, manifest);
            ledger.Transition(requestId, Lifecycle.StateSuccessorBuilt,
                new Dictionary<string, object?>
                {
                    ["candidate_sha256"] = candidateSha256,
                    ["manifest_path"] = manifestPath,
                    ["seal_preview"] = sealPreview,
                });
            return new SuccessorBuildResult(true, requestId, output,
                manifestPath, candidateSha256, applied, deferred,
                SealPreview: sealPreview);
        }
        catch (Exception error) when (error is AmendmentLifecycleError
            or SuccessorBuildError or IOException)
        {
            if (outputCreated)
            {
                try { File.Delete(Path.GetFullPath(outputDatabase)); }
                catch (IOException) { }
            }
            if (requestId.Length > 0)
            {
                try
                {
                    var record = ledger.LoadRecord(requestId);
                    var state = record?.TryGetValue("state", out var s)
                        == true ? s?.ToString() ?? "" : "";
                    if (record is not null
                        && !new HashSet<string>(StringComparer.Ordinal)
                        {
                            Lifecycle.StateRejected,
                            Lifecycle.StateExecuted,
                            "withdrawn",
                        }.Contains(state))
                        ledger.Transition(requestId,
                            Lifecycle.StateRejected,
                            new Dictionary<string, object?>
                            { ["error"] = error.Message });
                }
                catch (AmendmentLifecycleError) { }
            }
            return new SuccessorBuildResult(false, requestId,
                Path.GetFullPath(outputDatabase), manifestPath,
                Errors: new List<string> { error.Message });
        }
    }

    internal static string FileSha256(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(stream))
            .ToLowerInvariant();
    }
}

internal static class RepoTruthy
{
    /// <summary>Python truthiness for scalar cell values used in
    /// parity-evidence checks.</summary>
    public static bool TruthyText(object? value) => value switch
    {
        null => false,
        bool b => b,
        string s => s.Length > 0,
        _ => true,
    };
}
