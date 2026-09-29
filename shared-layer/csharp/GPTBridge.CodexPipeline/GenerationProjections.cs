using System.Text;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Per-generation projection and bookkeeping rebuild for the amendment
/// pipeline — direct port of codex_generation_projections.py.
///
/// Every published codex generation rebinds its derived projections to
/// the generation's own version identity: ``current_version``,
/// ``current_version_identity``, ``active_provision_binding_version``,
/// ``governance_closure_current_version``, ``revision_history``,
/// ``seal_manifest``, ``epoch_seal_manifest``,
/// ``codex_search_index_manifest``, ``codex_internal_module_manifest``,
/// ``codex_search_document``, ``codex_search_fts*`` and
/// ``current_normative_surface``.
///
/// Seal convention: seal rows record the roots of the generation as
/// published *before* the seal rows are appended — a seal cannot digest
/// its own row.
/// </summary>
internal static class GenerationProjections
{
    private static readonly string[] ProjectVersionKeys =
    {
        "current_version",
        "active_provision_binding_version",
        "governance_closure_current_version",
    };

    private static readonly Dictionary<string, string> SurfaceLayers =
        new(StringComparer.Ordinal)
        {
            ["article"] = "UNKNOWN",
            ["principle"] = "SPECIAL_LAW_DOMAIN_RULE",
            ["edict"] = "SPECIAL_LAW_DOMAIN_RULE",
            ["sovereign"] = "SPECIAL_LAW_DOMAIN_RULE",
            ["closure-definition"] = "CLOSURE_AGGREGATION",
            ["registry-rule"] = "REGISTRY_FACT",
            ["formal-rule"] = "FORMAL-RULE",
        };

    private static bool HasTable(StageConnection connection,
        string table) => Columns(connection, table).Count > 0;

    private static List<string> Columns(StageConnection connection,
        string table)
    {
        var cursor = connection.Execute($"PRAGMA table_info(\"{table}\")");
        return cursor.Rows
            .Select(row => row[1]?.ToString() ?? "").ToList();
    }

    private static List<object?[]> Rows(StageConnection connection,
        string table, string orderBy = "")
    {
        if (!HasTable(connection, table))
            return new List<object?[]>();
        // sql-ok: identifier from the fixed projection-table set
        var sql = $"SELECT * FROM \"{table}\""
            + (orderBy.Length > 0 ? $" ORDER BY {orderBy}" : "");
        return connection.Execute(sql).Rows.ToList();
    }

    private static int Count(StageConnection connection, string table)
    {
        if (!HasTable(connection, table))
            return 0;
        return Convert.ToInt32(connection.Execute(
            $"SELECT COUNT(*) FROM \"{table}\"").FetchOne()?[0] ?? 0);
    }

    private static string ReadVersion(StageConnection connection)
    {
        var row = connection.Execute(
            "SELECT value FROM metadata WHERE key='codex_version'")
            .FetchOne();
        var version = row?[0]?.ToString()?.Trim() ?? "";
        if (version.Length == 0)
            throw new InvalidOperationException(
                "generation bookkeeping requires metadata.codex_version");
        return version;
    }

    private static long Epoch(StageConnection connection)
    {
        var row = connection.Execute(
            "SELECT value FROM metadata WHERE key='current_version_epoch'")
            .FetchOne();
        return row is not null && row[0] is not null
            ? Convert.ToInt64(row[0]) : 2;
    }

    private static void RestampMetadata(StageConnection connection,
        string version, long epoch)
    {
        connection.Executemany(
            "UPDATE metadata SET value=? WHERE key=?",
            ProjectVersionKeys
                .Select(key => (IReadOnlyList<object?>)
                    new object?[] { version, key })
                .ToList());
        connection.Execute(
            "UPDATE metadata SET value=? "
            + "WHERE key='current_version_identity'",
            new object?[] { $"E{epoch}:{version}" });
    }

    private static void RestampBindingVersions(
        StageConnection connection, string version)
    {
        foreach (var table in new[] { "provision_lifecycle_status",
            "effective_provisions" })
            if (Columns(connection, table)
                .Contains("current_binding_version"))
                connection.Execute( // sql-ok: fixed allowlist identifier
                    $"UPDATE \"{table}\" SET current_binding_version=?",
                    new object?[] { version });
    }

    private static Dictionary<(string, string), (string Subject,
        string Content)> ProvisionTextMaps(StageConnection connection)
    {
        var maps = new Dictionary<(string, string),
            (string Subject, string Content)>();
        if (HasTable(connection, "articles"))
            foreach (var row in connection.Execute(
                "SELECT provision_id, subject, rule, prohibition, "
                + "exception FROM articles").Rows)
                maps[("article", row[0]?.ToString() ?? "")] = (
                    row[1]?.ToString() ?? "",
                    $"{row[1]}\n{row[2]}\n{row[3]}\n{row[4]}");
        if (HasTable(connection, "principles"))
            foreach (var row in connection.Execute(
                "SELECT provision_id, statement, binding FROM principles")
                .Rows)
                maps[("principle", row[0]?.ToString() ?? "")] = (
                    row[0]?.ToString() ?? "", $"{row[1]} {row[2]}");
        if (HasTable(connection, "edicts"))
            foreach (var row in connection.Execute(
                "SELECT provision_id, area, edict, immutability "
                + "FROM edicts").Rows)
                maps[("edict", row[0]?.ToString() ?? "")] = (
                    row[1]?.ToString() ?? "", $"{row[1]} {row[2]} {row[3]}");
        if (HasTable(connection, "sovereigns"))
            foreach (var row in connection.Execute(
                "SELECT sovereign_id, area, rank, basis FROM sovereigns")
                .Rows)
                maps[("sovereign", row[0]?.ToString() ?? "")] = (
                    row[0]?.ToString() ?? "",
                    $"{row[0]} {row[1]} {row[2]} {row[3]}");
        return maps;
    }

    private static int RebuildSearchDocuments(
        StageConnection connection, string version)
    {
        if (!(HasTable(connection, "provision_lifecycle_status")
            && HasTable(connection, "codex_search_document")))
            return 0;
        var textMaps = ProvisionTextMaps(connection);
        var modules = HasTable(connection,
            "codex_internal_module_membership")
            ? connection.Execute(
                "SELECT provision_type, provision_id, module_code "
                + "FROM codex_internal_module_membership").Rows
                .ToDictionary(r => (r[0]?.ToString() ?? "",
                    r[1]?.ToString() ?? ""), r => r[2]?.ToString() ?? "")
            : new Dictionary<(string, string), string>();
        var laws = HasTable(connection, "provision_law_classification")
            ? connection.Execute(
                "SELECT provision_type, provision_id, law_code "
                + "FROM provision_law_classification").Rows
                .ToDictionary(r => (r[0]?.ToString() ?? "",
                    r[1]?.ToString() ?? ""), r => r[2]?.ToString() ?? "")
            : new Dictionary<(string, string), string>();
        var priorLaw = connection.Execute(
            "SELECT provision_type, provision_id, law_code "
            + "FROM codex_search_document").Rows
            .ToDictionary(r => (r[0]?.ToString() ?? "",
                r[1]?.ToString() ?? ""), r => r[2]?.ToString() ?? "");
        var priorModule = connection.Execute(
            "SELECT provision_type, provision_id, module_code "
            + "FROM codex_search_document").Rows
            .ToDictionary(r => (r[0]?.ToString() ?? "",
                r[1]?.ToString() ?? ""), r => r[2]?.ToString() ?? "");
        var lifecycle = connection.Execute(
            "SELECT provision_type, provision_id, lifecycle_state "
            + "FROM provision_lifecycle_status").Rows.ToList();
        connection.Execute("DELETE FROM codex_search_document");
        var staged = new List<object?[]>();
        foreach (var row in lifecycle)
        {
            var ptype = row[0]?.ToString() ?? "";
            var pid = row[1]?.ToString() ?? "";
            string subject, content;
            if (new[] { "article", "principle", "edict", "sovereign" }
                .Contains(ptype))
            {
                if (!textMaps.TryGetValue((ptype, pid), out var mapped))
                    continue;
                subject = mapped.Subject;
                content = mapped.Content;
            }
            else
            {
                subject = pid;
                content = $"{ptype} {pid} resolves normative detail "
                    + "through its registered owner";
            }
            var moduleCode =
                modules.TryGetValue((ptype, pid), out var mc) ? mc
                : priorModule.TryGetValue((ptype, pid), out var pm) ? pm
                : "CODEX_MODULE_DIRECTORY";
            var lawCode =
                laws.TryGetValue((ptype, pid), out var lc) ? lc
                : priorLaw.TryGetValue((ptype, pid), out var pl) ? pl
                : "CODEX_MAIN";
            staged.Add(new object?[]
            {
                ptype, pid, moduleCode, lawCode, subject, content,
                AmendmentContract.SearchDocumentHash(content),
                row[2]?.ToString() ?? "", version,
            });
        }
        connection.Executemany(
            "INSERT INTO codex_search_document (provision_type, "
            + "provision_id, module_code, law_code, subject, content, "
            + "content_hash, lifecycle_state, version_identity) "
            + "VALUES (?,?,?,?,?,?,?,?,?)",
            staged.Select(r => (IReadOnlyList<object?>)r).ToList());
        return staged.Count;
    }

    private static int RebuildFts(StageConnection connection)
    {
        if (!(HasTable(connection, "codex_search_fts")
            && HasTable(connection, "codex_search_document")))
            return 0;
        var docs = connection.Execute(
            "SELECT provision_type, provision_id, module_code, law_code, "
            + "subject, content FROM codex_search_document "
            + "WHERE lifecycle_state='active' "
            + "ORDER BY provision_type, provision_id").Rows.ToList();
        connection.Execute("DELETE FROM codex_search_fts");
        connection.Executemany(
            "INSERT INTO codex_search_fts (provision_type, provision_id, "
            + "module_code, law_code, subject, content) "
            + "VALUES (?,?,?,?,?,?)",
            docs.Select(r => (IReadOnlyList<object?>)r).ToList());
        var shadowTables = connection.Execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            + "AND name LIKE 'codex_search_fts_%'").Rows
            .Select(r => r[0]?.ToString() ?? "")
            .ToHashSet(StringComparer.Ordinal);
        if (shadowTables.Contains("codex_search_fts_content"))
        {
            connection.Execute("DELETE FROM codex_search_fts_content");
            connection.Executemany(
                "INSERT INTO codex_search_fts_content "
                + "(id, c0, c1, c2, c3, c4, c5) VALUES (?,?,?,?,?,?,?)",
                docs.Select((row, index) =>
                    (IReadOnlyList<object?>)new object?[]
                    {
                        (long)(index + 1),
                        row[0]?.ToString() ?? "",
                        row[1]?.ToString() ?? "",
                        row[2]?.ToString() ?? "",
                        row[3]?.ToString() ?? "",
                        row[4]?.ToString() ?? "",
                        row[5]?.ToString() ?? "",
                    }).ToList());
        }
        if (shadowTables.Contains("codex_search_fts_docsize"))
        {
            connection.Execute("DELETE FROM codex_search_fts_docsize");
            connection.Executemany(
                "INSERT INTO codex_search_fts_docsize (id, sz) "
                + "VALUES (?,?)",
                docs.Select((row, index) =>
                    (IReadOnlyList<object?>)new object?[]
                    {
                        (long)(index + 1),
                        (object?)Encoding.UTF8.GetBytes(string.Join(",",
                            row.Select(v =>
                                (v?.ToString() ?? "").Length))),
                    }).ToList());
        }
        foreach (var internal_ in new[] { "codex_search_fts_data",
            "codex_search_fts_idx" })
            if (shadowTables.Contains(internal_))
                connection.Execute( // sql-ok: fixed FTS-shadow allowlist
                    $"DELETE FROM \"{internal_}\"");
        return docs.Count;
    }

    private static void RebuildModuleManifest(
        StageConnection connection, string version)
    {
        if (!(HasTable(connection, "codex_internal_module_manifest")
            && HasTable(connection, "codex_internal_module_membership")))
            return;
        var modules = connection.Execute(
            "SELECT DISTINCT module_code "
            + "FROM codex_internal_module_membership ORDER BY module_code")
            .Rows.Select(r => r[0]?.ToString() ?? "").ToList();
        var lifecycle = HasTable(connection, "provision_lifecycle_status")
            ? connection.Execute(
                "SELECT provision_type, provision_id, lifecycle_state "
                + "FROM provision_lifecycle_status").Rows
                .ToDictionary(r => (r[0]?.ToString() ?? "",
                    r[1]?.ToString() ?? ""), r => r[2]?.ToString() ?? "")
            : new Dictionary<(string, string), string>();
        var allMembership = Rows(connection,
            "codex_internal_module_membership");
        var allDocs = Rows(connection, "codex_search_document");
        var allDeps = Rows(connection,
            "codex_internal_module_dependency");
        var allLifecycle = Rows(connection,
            "provision_lifecycle_status");
        connection.Execute("DELETE FROM codex_internal_module_manifest");
        var staged = new List<object?[]>();
        foreach (var module in modules)
        {
            var membership = allMembership
                .Where(r => (r[2]?.ToString() ?? "") == module).ToList();
            var memberKeys = membership
                .Select(r => (r[0]?.ToString() ?? "",
                    r[1]?.ToString() ?? ""))
                .OrderBy(k => k).ToList();
            var memberIds = memberKeys.Select(k => k.Item2).ToList();
            var states = memberKeys.Select(k =>
                lifecycle.TryGetValue(k, out var s)
                    ? s : "unregistered").ToList();
            var active = states.Count(s => s == "active");
            var superseded = states.Count(s => s == "superseded");
            var retired = states.Count(s => s == "retired");
            var other = states.Count - active - superseded - retired;
            var docs = allDocs
                .Where(r => (r[2]?.ToString() ?? "") == module).ToList();
            var docDigest = AmendmentContract.ContentHash(
                docs.Select(r => (object?)r.ToList()).ToList());
            var deps = allDeps.Where(r => (r[0]?.ToString() ?? "")
                == module || (r[1]?.ToString() ?? "") == module).ToList();
            var memberKeySet = memberKeys.ToHashSet();
            var lifecycleRows = allLifecycle
                .Where(r => memberKeySet.Contains(
                    (r[0]?.ToString() ?? "", r[1]?.ToString() ?? "")))
                .ToList();
            var classificationRows = Rows(connection,
                "provision_law_classification")
                .Where(r => memberKeySet.Contains(
                    (r[0]?.ToString() ?? "", r[1]?.ToString() ?? "")))
                .ToList();
            var resolutionRows = Rows(connection,
                "provision_reference_resolution_v2")
                .Where(r => memberIds.Contains(r[1]?.ToString() ?? ""))
                .ToList();
            staged.Add(new object?[]
            {
                module, version, (long)membership.Count, (long)active,
                (long)superseded,
                AmendmentContract.ContentHash(membership
                    .Select(r => (object?)r.ToList()).ToList()),
                docDigest,
                AmendmentContract.ContentHash(deps
                    .Select(r => (object?)r.ToList()).ToList()),
                docDigest, "sealed", version, (long)retired,
                (long)other,
                AmendmentContract.ContentHash(lifecycleRows
                    .Select(r => (object?)r.ToList()).ToList()),
                AmendmentContract.ContentHash(classificationRows
                    .Select(r => (object?)r.ToList()).ToList()),
                AmendmentContract.ContentHash(resolutionRows
                    .Select(r => (object?)r.ToList()).ToList()),
            });
        }
        connection.Executemany(
            "INSERT INTO codex_internal_module_manifest (module_code, "
            + "version_identity, provision_count, active_count, "
            + "superseded_count, membership_hash, content_hash, "
            + "dependency_hash, search_document_hash, status, "
            + "sealed_at_utc, retired_count, other_state_count, "
            + "lifecycle_hash, classification_hash, "
            + "successor_resolution_hash) "
            + "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            staged.Select(r => (IReadOnlyList<object?>)r).ToList());
    }

    private static void RebuildSearchManifest(
        StageConnection connection, string version, int ftsCount,
        int docCount)
    {
        if (!HasTable(connection, "codex_search_index_manifest"))
            return;
        var docs = Rows(connection, "codex_search_document");
        var fts = Rows(connection, "codex_search_fts");
        var alias = Rows(connection, "codex_search_alias");
        var modules = Rows(connection, "codex_internal_module_manifest");
        var deps = Rows(connection, "codex_internal_module_dependency");
        connection.Execute(
            "UPDATE codex_search_index_manifest SET "
            + "codex_version_identity=?, authoritative_content_root=?, "
            + "source_count=?, indexed_count=?, module_manifest_root=?, "
            + "index_content_hash=?, built_at_utc=?, status='current', "
            + "alias_count=?, alias_hash=?, fts_row_count=?, "
            + "fts_content_hash=?, dependency_graph_hash=?",
            new object?[]
            {
                version,
                AmendmentContract.ContentHash(
                    docs.Select(r => (object?)r.ToList()).ToList()),
                (long)docs.Count, (long)docCount,
                AmendmentContract.ContentHash(
                    modules.Select(r => (object?)r.ToList()).ToList()),
                AmendmentContract.ContentHash(
                    docs.Select(r => (object?)r.ToList()).ToList()),
                version, (long)alias.Count,
                AmendmentContract.ContentHash(
                    alias.Select(r => (object?)r.ToList()).ToList()),
                (long)ftsCount,
                AmendmentContract.ContentHash(
                    fts.Select(r => (object?)r.ToList()).ToList()),
                AmendmentContract.ContentHash(
                    deps.Select(r => (object?)r.ToList()).ToList()),
            });
    }

    private static void SyncNormativeSurface(
        StageConnection connection, string version)
    {
        if (!HasTable(connection, "current_normative_surface"))
            return;
        var lifecycle = HasTable(connection, "provision_lifecycle_status")
            ? connection.Execute(
                "SELECT provision_type, provision_id, lifecycle_state "
                + "FROM provision_lifecycle_status").Rows
                .ToDictionary(r => (r[0]?.ToString() ?? "",
                    r[1]?.ToString() ?? ""), r => r[2]?.ToString() ?? "")
            : new Dictionary<(string, string), string>();
        var surface = connection.Execute(
            "SELECT surface_entry_id, object_type, object_identity "
            + "FROM current_normative_surface").Rows.ToList();
        var existing = surface.Select(r =>
            (r[1]?.ToString() ?? "", r[2]?.ToString() ?? ""))
            .ToHashSet();
        var stateUpdates = surface
            .Where(r => lifecycle.ContainsKey(
                (r[1]?.ToString() ?? "", r[2]?.ToString() ?? "")))
            .Select(r => (IReadOnlyList<object?>)new object?[]
            {
                lifecycle[(r[1]?.ToString() ?? "",
                    r[2]?.ToString() ?? "")],
                r[0],
            }).ToList();
        connection.Executemany(
            "UPDATE current_normative_surface SET lifecycle_state=? "
            + "WHERE surface_entry_id=?", stateUpdates);
        connection.Execute(
            "UPDATE current_normative_surface SET version_identity=?",
            new object?[] { version });
        var toAdd = lifecycle
            .Where(pair => pair.Value == "active"
                && !existing.Contains(pair.Key))
            .Select(pair => (pair.Key.Item1, pair.Key.Item2,
                pair.Value))
            .ToList();
        const string insertSql =
            "INSERT INTO current_normative_surface (surface_entry_id, "
            + "surface_layer, object_type, object_identity, "
            + "lifecycle_state, default_search_visible, "
            + "version_identity, status) VALUES (?,?,?,?,?,?,?,?)";
        connection.Executemany(insertSql,
            toAdd.Select(item => (IReadOnlyList<object?>)new object?[]
            {
                $"{item.Item1}:{item.Item2}",
                SurfaceLayers.TryGetValue(item.Item1, out var layer)
                    ? layer : "UNKNOWN",
                item.Item1, item.Item2, item.Item3,
                1L, version, "current",
            }).ToList());
        var rules = HasTable(connection, "formal_rule_registry")
            ? connection.Execute(
                "SELECT rule_code FROM formal_rule_registry "
                + "WHERE status<>'withdrawn'").Rows
                .Select(r => r[0]?.ToString() ?? "").ToList()
            : new List<string>();
        var ruleKeys = rules
            .Select(r => ("formal-rule", r)).ToHashSet();
        var toAddKeys = toAdd
            .Select(item => (item.Item1, item.Item2)).ToHashSet();
        var extra = ruleKeys
            .Where(k => !existing.Contains(k) && !toAddKeys.Contains(k))
            .OrderBy(k => k)
            .Select(k => (IReadOnlyList<object?>)new object?[]
            {
                $"formal-rule:{k.Item2}", "FORMAL-RULE",
                "formal-rule", k.Item2, "active", 1L, version,
                "current",
            }).ToList();
        connection.Executemany(insertSql, extra);
    }

    private static string AppendRevision(StageConnection connection,
        string version, long epoch, string changeId,
        string changeScope, string summary)
    {
        if (!HasTable(connection, "revision_history"))
            return new string('0', 64);
        var row = connection.Execute(
            "SELECT sequence, entry_hash FROM revision_history "
            + "ORDER BY sequence DESC LIMIT 1").FetchOne();
        var sequence = row is not null
            ? Convert.ToInt64(row[0]) + 1 : 1;
        var previousHash = row?[1]?.ToString()
            ?? new string('0', 64);
        var fields = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["sequence"] = sequence,
            ["change_id"] = changeId,
            ["version"] = version,
            ["recorded_date"] = version.Length >= 10
                ? version[..10] : version,
            ["change_scope"] = changeScope,
            ["summary"] = summary,
            ["previous_hash"] = previousHash,
            ["version_epoch"] = epoch,
            ["recorded_at_utc"] = version,
            ["timestamp_status"] = "verified",
            ["timestamp_migration_evidence"] =
                "amendment-pipeline-generation-bookkeeping",
        };
        fields["entry_hash"] =
            AmendmentContract.RevisionEntryHash(fields);
        connection.Execute(
            "INSERT INTO revision_history (sequence, change_id, "
            + "version, recorded_date, change_scope, summary, "
            + "previous_hash, entry_hash, version_epoch, "
            + "recorded_at_utc, timestamp_status, "
            + "timestamp_migration_evidence) "
            + "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            new[] { "sequence", "change_id", "version",
                "recorded_date", "change_scope", "summary",
                "previous_hash", "entry_hash", "version_epoch",
                "recorded_at_utc", "timestamp_status",
                "timestamp_migration_evidence" }
                .Select(name => fields[name]).ToList());
        return (string)fields["entry_hash"]!;
    }

    private static void AppendSealRows(StageConnection connection,
        string version, long epoch, string historyHead)
    {
        if (!(HasTable(connection, "seal_manifest")
            && HasTable(connection, "epoch_seal_manifest")))
            return;
        var preview = AmendmentContract.SealPreviewFrom(connection);
        var provisionCount =
            Count(connection, "provision_lifecycle_status");
        var identityCount = Count(connection, "provision_identities");
        var lineageCount = Count(connection, "provision_lineage");
        const string certification = "sealed-governed-certification";
        connection.Execute(
            "INSERT INTO seal_manifest (version, history_head, "
            + "provision_count, identity_count, lineage_count, "
            + "certification_state, content_root, identity_root, "
            + "full_root, version_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
            new object?[]
            {
                version, historyHead, (long)provisionCount,
                (long)identityCount, (long)lineageCount,
                certification, preview["content_root"],
                preview["identity_root"], preview["full_root"],
                epoch,
            });
        connection.Execute(
            "INSERT INTO epoch_seal_manifest (version_epoch, version, "
            + "version_identity, history_head, provision_count, "
            + "identity_count, lineage_count, certification_state, "
            + "content_root, identity_root, full_root, "
            + "legacy_history_head) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            new object?[]
            {
                epoch, version, $"E{epoch}:{version}", historyHead,
                (long)provisionCount, (long)identityCount,
                (long)lineageCount, certification,
                preview["content_root"], preview["identity_root"],
                preview["full_root"], null,
            });
    }

    /// <summary>Rebind every derived projection of the staged
    /// generation to its version — runs inside the isolated staging
    /// copy after the prepared successor is applied.</summary>
    public static Dictionary<string, object?>
        RebuildGenerationBookkeeping(string database,
            string changeId = "",
            string changeScope = "amendment-execution",
            string summary = "")
    {
        using var store = AmendmentContract.OpenCodexStore(database,
            writeBack: true);
        var connection = store.Connection;
        try
        {
            var version = ReadVersion(connection);
            var epoch = Epoch(connection);
            RestampMetadata(connection, version, epoch);
            RestampBindingVersions(connection, version);
            var docCount = RebuildSearchDocuments(connection, version);
            var ftsCount = RebuildFts(connection);
            RebuildModuleManifest(connection, version);
            RebuildSearchManifest(connection, version, ftsCount,
                docCount);
            SyncNormativeSurface(connection, version);
            var historyHead = AppendRevision(connection, version,
                epoch,
                changeId.Length > 0 ? changeId
                    : $"amendment-execution-{version}",
                changeScope,
                summary.Length > 0 ? summary
                    : $"Governed amendment execution {version}");
            connection.Commit();
            AppendSealRows(connection, version, epoch, historyHead);
            connection.Commit();
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["version"] = version,
                ["epoch"] = epoch,
                ["documents"] = (long)docCount,
                ["fts_rows"] = (long)ftsCount,
                ["revision_head"] = historyHead,
            };
        }
        catch
        {
            connection.Rollback();
            store.SuppressWriteBack();
            throw;
        }
    }
}
