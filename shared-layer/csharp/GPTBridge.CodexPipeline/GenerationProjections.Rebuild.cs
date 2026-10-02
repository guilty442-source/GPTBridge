using System.Text;

namespace GPTBridge.CodexPipeline;

internal static partial class GenerationProjections
{

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

    /// <summary>codex_version_authority_registry is a derived
    /// current-state registry (D108): exactly one
    /// CURRENT_AUTHORITATIVE row equal to the live generation, every
    /// earlier publication HISTORICAL_SUPERSEDED with its successor
    /// link. Rebuilt whole from the revision ledger plus the staged
    /// version — drift here is a projection defect, never a rewrite
    /// of history (D73: LATEST-PUBLISHED-ONLY).</summary>
    private static int RebuildVersionAuthorityRegistry(
        StageConnection connection, string version)
    {
        if (!HasTable(connection, "codex_version_authority_registry"))
            return 0;
        var versions = new SortedSet<string>(StringComparer.Ordinal);
        if (HasTable(connection, "revision_history"))
            foreach (var row in connection.Execute(
                "SELECT DISTINCT version FROM revision_history").Rows)
            {
                var v = row[0]?.ToString() ?? "";
                if (v.Length > 0) versions.Add(v);
            }
        foreach (var row in connection.Execute(
            "SELECT version_identity "
            + "FROM codex_version_authority_registry").Rows)
        {
            var v = row[0]?.ToString() ?? "";
            if (v.Length > 0) versions.Add(v);
        }
        versions.Add(version);
        var ordered = versions.ToList();
        connection.Execute(
            "DELETE FROM codex_version_authority_registry");
        var staged = new List<object?[]>();
        for (int i = 0; i < ordered.Count; i++)
        {
            var v = ordered[i];
            var isCurrent = v == version;
            var next = i + 1 < ordered.Count ? ordered[i + 1] : null;
            staged.Add(new object?[]
            {
                v,
                isCurrent ? "CURRENT_AUTHORITATIVE"
                    : "HISTORICAL_SUPERSEDED",
                isCurrent ? null : next,
                isCurrent ? null : version,
                isCurrent ? "latest-published-version"
                    : "latest-successor-published",
                isCurrent ? 0L : 1L,
                version,
            });
        }
        connection.Executemany(
            "INSERT INTO codex_version_authority_registry "
            + "(version_identity, authority_status, successor_version, "
            + "retired_at, reason, immutable, current_binding_version) "
            + "VALUES (?,?,?,?,?,?,?)",
            staged.Select(r => (IReadOnlyList<object?>)r).ToList());
        return ordered.Count;
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
        const string certification = "sealed-governed-authorization";
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
        AppendCertificationRows(connection, version, epoch);
    }

    /// <summary>D113 continuity: every seal generation carries
    /// same-version ``sealed_components`` coverage,
    /// ``trust_anchor_requirements`` and ``certification_evidence``
    /// rows.  External Ed25519 signatures stay pending by design —
    /// the evidence row records that state honestly instead of
    /// claiming a certified release that never completed.</summary>
    private static readonly string[] SealComponents =
    {
        "certification-policy", "history", "identities", "lineage",
        "metadata", "provisions", "schema", "sovereigns",
        "supersessions", "version-rules",
    };

    private static void AppendCertificationRows(
        StageConnection connection, string version, long epoch)
    {
        if (HasTable(connection, "sealed_components"))
            foreach (var component in SealComponents)
                connection.Execute(
                    "INSERT INTO sealed_components (version, component,"
                    + " included_in_full_root) VALUES (?,?,?)",
                    new object?[] { version, component, 1L });
        if (HasTable(connection, "trust_anchor_requirements"))
            connection.Execute(
                "INSERT INTO trust_anchor_requirements (version, "
                + "algorithm, threshold, key_count, "
                + "detached_signatures_required, private_key_storage, "
                + "version_epoch) VALUES (?,?,?,?,?,?,?)",
                new object?[]
                {
                    version, "Ed25519", 2L, 3L, 1L,
                    "external-offline-never-in-codex", epoch,
                });
        if (HasTable(connection, "certification_evidence"))
            connection.Execute(
                "INSERT INTO certification_evidence (version, "
                + "signed_payload_fields, evidence_location, "
                + "validation_rule, status, version_epoch) "
                + "VALUES (?,?,?,?,?,?)",
                new object?[]
                {
                    version,
                    "version|content-root|history-head|identity-root",
                    "external-offline-signature-bundle",
                    "human-governor-seal+Ed25519-2-of-3-verification",
                    "external-signatures-required", epoch,
                });
    }

    /// <summary>C102 atomic-publication: every ``status='active'`` row
    /// in ``architecture_diagram_artifact_registry`` rebinds to the
    /// staged version inside the same successor transaction —
    /// ``content_hash`` is recomputed from the artifact file itself and
    /// ``validated_at_utc`` restamped, so a registered diagram can never
    /// lag the generation that seals it (FORBID:diagram-lag|
    /// mixed-version-generation).  The single
    /// ``ARCHITECTURE_DIAGRAM_SYNC_CURRENT`` evidence row is rebuilt
    /// from the measured post-rebuild state, never pre-repair counts.</summary>
    private static void RebuildDiagramSync(StageConnection connection,
        string version, bool failClosed = true)
    {
        if (!HasTable(connection,
                "architecture_diagram_artifact_registry"))
            return;
        var rows = connection.Execute(
            "SELECT diagram_code, artifact_path, content_hash, "
            + "source_codex_version, read_only_required "
            + "FROM architecture_diagram_artifact_registry "
            + "WHERE status='active' ORDER BY diagram_code").Rows
            .ToList();
        var present = 0;
        var hashMatch = 0;
        var readOnly = 0;
        var readOnlyViolations = 0;
        var stale = 0;
        var missing = 0;
        var rebind = new List<object?[]>();
        foreach (var row in rows)
        {
            var code = row[0]?.ToString() ?? "";
            var relative = (row[1]?.ToString() ?? "")
                .Replace('/', Path.DirectorySeparatorChar);
            var file = Path.GetFullPath(Path.Combine(Repo.Root(),
                relative));
            if (!File.Exists(file))
            {
                missing++;
                continue;
            }
            present++;
            if (new FileInfo(file).IsReadOnly) readOnly++;
            else if (Convert.ToInt64(row[4]) != 0) readOnlyViolations++;
            var hash = SuccessorBuilder.FileSha256(file);
            rebind.Add(new object?[] { version, hash, version, code });
        }
        connection.Executemany(
            "UPDATE architecture_diagram_artifact_registry SET "
            + "source_codex_version=?, content_hash=?, "
            + "validated_at_utc=?, sync_status='SYNCED_CURRENT' "
            + "WHERE diagram_code=? AND status='active'",
            rebind.Select(r => (IReadOnlyList<object?>)r).ToList());
        // Validate the rows actually persisted, rather than assuming that
        // the old registry hashes or successful writes prove current parity.
        foreach (var row in connection.Execute(
            "SELECT artifact_path, content_hash, source_codex_version, sync_status "
            + "FROM architecture_diagram_artifact_registry WHERE status='active'").Rows)
        {
            var file = Path.GetFullPath(Path.Combine(Repo.Root(),
                (row[0]?.ToString() ?? "").Replace('/', Path.DirectorySeparatorChar)));
            if ((row[2]?.ToString() ?? "") != version
                || (row[3]?.ToString() ?? "") != "SYNCED_CURRENT") stale++;
            if (File.Exists(file)
                && SuccessorBuilder.FileSha256(file) == (row[1]?.ToString() ?? "")) hashMatch++;
        }
        if (!HasTable(connection, "architecture_diagram_sync_evidence"))
            return;
        var passed = DiagramSyncPassed(rows.Count, present, hashMatch, stale, missing, readOnlyViolations);
        connection.Execute(
            "UPDATE architecture_diagram_sync_evidence SET "
            + "codex_version=?, required_count=?, registered_count=?, "
            + "present_count=?, hash_match_count=?, read_only_count=?, "
            + "stale_count=?, missing_count=?, result=?, "
            + "verified_at_utc=?, "
            + "verifier='deterministic-file-registry-validator', "
            + "status='current' "
            + "WHERE evidence_id='ARCHITECTURE_DIAGRAM_SYNC_CURRENT'",
            new object?[]
            {
                version, (long)rows.Count, (long)rows.Count,
                (long)present, (long)hashMatch, (long)readOnly,
                (long)stale, (long)missing,
                passed ? "PASS" : "FAIL", version,
            });
        if (!passed && failClosed)
            throw new InvalidOperationException("ARCHITECTURE_DIAGRAM_SYNC_INCOMPLETE");
    }

    private static bool DiagramSyncPassed(int required, int present, int hashMatch,
        int stale, int missing, int readOnlyViolations) =>
        required > 0 && present == required && hashMatch == required
            && stale == 0 && missing == 0 && readOnlyViolations == 0;

    private static bool HistoricalSubSovereignClause(string clause) =>
        System.Text.RegularExpressions.Regex.IsMatch(clause,
            "historical|abolished|abolition|retired|lineage|supersede|removed|no sub-sovereign|not a sovereign|without restoring|never a sovereign|not revive|revival|forbid|prohibit|non-active|no active|must not|shall not|do not|does not|cannot|banned|sub\\s*=\\s*none",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);

    private static void RebuildProjectDirectoryProjection(StageConnection connection, string version)
    {
        if (!HasTable(connection, "project_architecture_directory")
            || !HasTable(connection, "a233_normalized_directory_entry")) return;
        var columns = Columns(connection, "project_architecture_directory");
        foreach (var row in Rows(connection, "project_architecture_directory"))
        {
            var payload = columns.Select((name, index) => (name, value: row[index]))
                .ToDictionary(p => p.name, p => p.value);
            var code = payload["architecture_code"]?.ToString() ?? "";
            var count = connection.Execute(
                "SELECT COUNT(*) FROM a233_normalized_directory_entry "
                + "WHERE source_table='project_architecture_directory' AND source_key=?",
                new object?[] { code }).Rows.First()[0];
            if (Convert.ToInt64(count) != 1)
                throw new InvalidOperationException($"A233_PROJECT_PROJECTION_COVERAGE:{code}");
            connection.Execute(
                "UPDATE a233_normalized_directory_entry SET domain_payload=?,content_hash=?,revision=? "
                + "WHERE source_table='project_architecture_directory' AND source_key=?",
                new object?[] { AmendmentContract.CanonicalJson(payload),
                    AmendmentContract.ContentHash(payload), version, code });
        }
        var extra = connection.Execute(
            "SELECT COUNT(*) FROM a233_normalized_directory_entry n "
            + "WHERE n.source_table='project_architecture_directory' AND NOT EXISTS "
            + "(SELECT 1 FROM project_architecture_directory d WHERE d.architecture_code=n.source_key)").Rows.First()[0];
        if (Convert.ToInt64(extra) != 0)
            throw new InvalidOperationException("A233_PROJECT_PROJECTION_EXTRA_ROWS");
    }

    private static void RebuildStaleReferenceEvidence(StageConnection connection, string version)
    {
        if (!HasTable(connection, "architecture_stale_reference_evidence")) return;
        var current = 0L;
        var historical = 0L;
        // Restrictive mentions do not grant a role. Explicitly historical
        // clauses are counted separately; every other active mention fails closed.
        // Clauses are split at sentence boundaries (';' and '.') so a sentence
        // carrying active sub-sovereign semantics is never shielded by a
        // historical marker elsewhere in the same clause (e.g. B112).  All
        // three normative fields are scanned — exception text can grant
        // current scope just as rule text can.
        foreach (var row in connection.Execute(
            "SELECT a.rule, a.prohibition, a.exception FROM articles a JOIN provision_lifecycle_status l "
            + "ON l.provision_id=a.provision_id AND l.provision_type='article' "
            + "WHERE l.lifecycle_state='active'").Rows)
            for (var field = 0; field < 3; field++)
                foreach (var clause in (row[field]?.ToString() ?? "")
                    .Split(';', '.'))
                {
                    if (!clause.Contains("sub-sovereign", StringComparison.OrdinalIgnoreCase)
                        && !clause.Contains("sub_sovereign", StringComparison.OrdinalIgnoreCase)) continue;
                    if (HistoricalSubSovereignClause(clause)) historical++;
                    else current++;
                }
        connection.Execute(
            "UPDATE architecture_stale_reference_evidence SET current_reference_count=?, "
            + "historical_reference_count=?,result=?,version_identity=?,status='current' "
            + "WHERE evidence_id='CURRENT_ARCHITECTURE_STALE_REFERENCE_SCAN_CURRENT'",
            new object?[] { current, historical, current == 0 ? "PASS" : "FAIL", version });
    }

    /// <summary>Synchronise ``machine_schema_registry.parity_status`` from
    /// the parity evidence rows.  A schema is VERIFIED only when its
    /// evidence row carries four equal, non-null semantic hashes with
    /// status PASS — the status can never be asserted without matching
    /// producer/validator/persistence/canonical evidence.  Rows whose
    /// evidence is missing, PENDING, or internally inconsistent fall back
    /// to PENDING (fail-closed).</summary>
    private static int RebuildSchemaParityStatus(
        StageConnection connection)
    {
        if (!HasTable(connection, "machine_schema_registry")
            || !HasTable(connection, "machine_schema_parity_evidence"))
            return 0;
        const string evidenceOk =
            "EXISTS (SELECT 1 FROM machine_schema_parity_evidence e "
            + "WHERE e.schema_code=r.schema_code AND e.status='PASS' "
            + "AND e.producer_semantic_hash IS NOT NULL "
            + "AND e.validator_semantic_hash IS NOT NULL "
            + "AND e.persistence_semantic_hash IS NOT NULL "
            + "AND e.canonical_semantic_hash IS NOT NULL "
            + "AND e.producer_semantic_hash=e.validator_semantic_hash "
            + "AND e.validator_semantic_hash=e.persistence_semantic_hash "
            + "AND e.persistence_semantic_hash=e.canonical_semantic_hash)";
        var cursor = connection.Execute(
            "UPDATE machine_schema_registry r SET parity_status='VERIFIED' "
            + $"WHERE {evidenceOk} AND r.parity_status<>'VERIFIED'");
        var verified = cursor.RowCount;
        cursor = connection.Execute(
            "UPDATE machine_schema_registry r SET parity_status='PENDING' "
            + $"WHERE NOT ({evidenceOk}) AND r.parity_status='VERIFIED'");
        return verified + cursor.RowCount;
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
            RebuildVersionAuthorityRegistry(connection, version);
            SyncNormativeSurface(connection, version);
            RebuildProjectDirectoryProjection(connection, version);
            RebuildStaleReferenceEvidence(connection, version);
            RebuildSchemaParityStatus(connection);
            RebuildDiagramSync(connection, version);
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

    /// <summary>Live-authority projection repair (A173): rebuilds the
    /// derived search/index/manifest surfaces inside the live
    /// ``gptbridge_codex`` schema from the authoritative content tables
    /// when stored projection rows carry transport loss (e.g. ``??``
    /// replacement damage). Touches only deterministic derived
    /// projections — no version restamp, revision append, or seal
    /// bookkeeping; the generation identity is unchanged.</summary>
    public static Dictionary<string, object?> RepairLiveProjections()
    {
        using var connection = new StageConnection(PgDsn.CodexSchema);
        try
        {
            var version = ReadVersion(connection);
            var docCount = RebuildSearchDocuments(connection, version);
            var ftsCount = RebuildFts(connection);
            RebuildModuleManifest(connection, version);
            RebuildSearchManifest(connection, version, ftsCount,
                docCount);
            var authorityRows = RebuildVersionAuthorityRegistry(
                connection, version);
            RebuildProjectDirectoryProjection(connection, version);
            RebuildStaleReferenceEvidence(connection, version);
            var parityUpdates = RebuildSchemaParityStatus(connection);
            RebuildDiagramSync(connection, version, failClosed: false);
            connection.Commit();
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = true,
                ["schema"] = PgDsn.CodexSchema,
                ["authority_registry_rows"] = authorityRows,
                ["version"] = version,
                ["documents"] = (long)docCount,
                ["fts_rows"] = (long)ftsCount,
                ["schema_parity_updates"] = parityUpdates,
            };
        }
        catch
        {
            connection.Rollback();
            throw;
        }
    }
}
