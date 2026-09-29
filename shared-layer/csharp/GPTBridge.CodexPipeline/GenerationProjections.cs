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
internal static partial class GenerationProjections
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
}
