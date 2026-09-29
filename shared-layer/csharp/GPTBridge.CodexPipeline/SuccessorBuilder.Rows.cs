using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

internal static partial class SuccessorBuilder
{
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
}
