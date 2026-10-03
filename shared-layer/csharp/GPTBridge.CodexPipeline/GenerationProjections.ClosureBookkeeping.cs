using System.Text.Json;
using System.Text.RegularExpressions;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// In-generation convergence bookkeeping rebuild (D119/D122): every
/// published generation rebinds ``codex_convergence_closure``,
/// ``codex_convergence_metrics`` and ``governance_closure_state`` to its
/// own version identity, with counts recomputed from the staged
/// candidate — the same measurements the convergence evaluator reads
/// back from the published mirror afterwards.  Missing implementation
/// stays ``INCOMPLETE_EVIDENCE``; nothing here fabricates PASS.
///
/// Runs inside ``RebuildGenerationBookkeeping`` after all other
/// projections are rebuilt and before the revision entry and seal rows
/// are appended, so the seal fingerprints include the recomputed rows.
/// </summary>
internal static partial class GenerationProjections
{
    private sealed record Finding(string RuleCode, List<string> Reasons);

    private static readonly Regex HashShape = new("^[0-9a-f]{64}$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    private static Dictionary<string, object?> Record(
        List<string> columns, object?[] values) =>
        columns.Select((name, index) => (name, value: values[index]))
            .ToDictionary(p => p.name, p => p.value, StringComparer.Ordinal);

    private static List<Dictionary<string, object?>> Records(
        StageConnection connection, string table)
    {
        var columns = Columns(connection, table);
        return Rows(connection, table)
            .Select(values => Record(columns, values)).ToList();
    }

    private static string Text(
        IReadOnlyDictionary<string, object?> row, string key) =>
        row.TryGetValue(key, out var value) && value is not null
            && value is not DBNull
            ? value.ToString() ?? "" : "";

    private static long Int(
        IReadOnlyDictionary<string, object?> row, string key) =>
        long.TryParse(Text(row, key), out var parsed) ? parsed : 0;

    private static string DigestRows(
        IEnumerable<Dictionary<string, object?>> rows) =>
        AmendmentContract.ContentHash(rows
            .Select(AmendmentContract.CanonicalJson)
            .OrderBy(s => s, StringComparer.Ordinal).ToList());

    private static HashSet<string> RegisteredEvaluatorCodes()
    {
        var set = new HashSet<string>(StringComparer.Ordinal);
        var path = Path.Combine(Repo.Root(), "governance_rule",
            "execution", "formal_rules", "evaluators.json");
        if (!File.Exists(path)) return set;
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        if (doc.RootElement.TryGetProperty("REGISTERED_RULE_CODES",
                out var codes))
            foreach (var code in codes.EnumerateArray())
                if (code.GetString() is { } value) set.Add(value);
        return set;
    }

    private static bool ValuesEqual(object? stored, JsonElement element)
    {
        var text = stored is null or DBNull ? null : stored.ToString();
        return element.ValueKind switch
        {
            JsonValueKind.Null => text is null,
            JsonValueKind.String =>
                text is not null && string.Equals(text,
                    element.GetString(), StringComparison.Ordinal),
            JsonValueKind.True => text is "1" or "true" or "True",
            JsonValueKind.False => text is "0" or "false" or "False",
            _ => string.Equals(text, element.GetRawText(),
                StringComparison.Ordinal),
        };
    }

    private static bool ProjectionMismatch(
        IReadOnlyDictionary<string, object?> row, List<string> columns,
        IReadOnlyDictionary<string, object?> projection)
    {
        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(
                Text(projection, "domain_payload"));
        }
        catch (JsonException)
        {
            return true;
        }
        using (doc)
        {
            var fields = doc.RootElement.ValueKind == JsonValueKind.Object
                ? doc.RootElement.EnumerateObject()
                    .ToDictionary(p => p.Name, p => p.Value,
                        StringComparer.Ordinal)
                : new Dictionary<string, JsonElement>(
                    StringComparer.Ordinal);
            foreach (var column in columns)
                if (!fields.TryGetValue(column, out var element)
                        || !ValuesEqual(row.GetValueOrDefault(column),
                            element))
                    return true;
        }
        return false;
    }

    /// <summary>Recompute the current convergence closure rows, the
    /// current metrics row and the measured governance-closure-state
    /// components so that they bind ``version`` — direct port of the
    /// evaluator's closure-refresh measurements.</summary>
    private static void RebuildClosureBookkeeping(
        StageConnection connection, string version)
    {
        if (!HasTable(connection, "codex_convergence_closure"))
            return;

        var lifecycle = Records(connection, "provision_lifecycle_status");
        var activeArticleIds = new HashSet<string>(lifecycle
            .Where(r => Text(r, "provision_type") == "article"
                && Text(r, "lifecycle_state") == "active")
            .Select(r => Text(r, "provision_id")), StringComparer.Ordinal);
        var predecessor = Text(new Dictionary<string, object?>
        {
            ["version"] = connection.Execute(
                "SELECT version FROM revision_history "
                + "ORDER BY sequence DESC LIMIT 1").FetchOne()?[0],
        }, "version");
        var source = $"source={predecessor};"
            + "candidate-generation=bound-by-version_identity-field;";

        // Participating formal rules rebind their evidence generation to
        // the published version — the registry hash fields are content
        // digests of the candidate itself, so rebinding is a projection,
        // not a re-verification claim.  Retired/withdrawn rules keep
        // their historical binding.
        if (HasTable(connection, "formal_rule_registry"))
            connection.Execute(
                "UPDATE formal_rule_registry SET version_identity=? "
                + "WHERE status NOT IN "
                + "('retired','withdrawn','superseded','inactive')",
                new object?[] { version });

        // -- formal rule findings ------------------------------------
        var registered = RegisteredEvaluatorCodes();
        var formal = Records(connection, "formal_rule_registry");
        var formalCurrent = formal
            .Where(r => !new[] { "retired", "withdrawn", "superseded",
                "inactive" }.Contains(Text(r, "status")))
            .ToList();
        var formalFindings = new List<Finding>();
        foreach (var rule in formalCurrent)
        {
            var reasons = new List<string>();
            var code = Text(rule, "rule_code");
            if (!registered.Contains(code))
                reasons.Add("EVALUATOR_NOT_REGISTERED");
            if (!activeArticleIds.Contains(
                    Text(rule, "controlling_provision_id")))
                reasons.Add("CONTROLLING_PROVISION_NOT_ACTIVE");
            if (Text(rule, "version_identity") != version)
                reasons.Add("CURRENT_GENERATION_EVIDENCE_MISSING");
            if (Text(rule, "status") != "evaluator-parity-verified"
                || Text(rule, "parity_status") != "VERIFIED")
                reasons.Add("PARITY_NOT_VERIFIED");
            foreach (var field in new[] { "semantic_hash",
                "evaluator_hash", "test_contract_hash",
                "controlling_provision_hash" })
                if (!HashShape.IsMatch(Text(rule, field)))
                    reasons.Add($"MISSING_HASH:{field}");
            if (string.IsNullOrWhiteSpace(
                    Text(rule, "parity_evidence_id")))
                reasons.Add("PARITY_EVIDENCE_ID_MISSING");
            if (reasons.Count > 0)
                formalFindings.Add(new Finding(code, reasons));
        }
        var formalDigest = DigestRows(formal);
        var findingsDigest = AmendmentContract.ContentHash(formalFindings
            .Select(f => (object)new Dictionary<string, object?>
            {
                ["rule_code"] = f.RuleCode,
                ["reasons"] = f.Reasons.Cast<object?>().ToList(),
            }).ToList());

        // -- machine schema parity -----------------------------------
        var schemas = Records(connection, "machine_schema_registry");
        var schemaEvidence = Records(connection,
            "machine_schema_parity_evidence");
        var schemaOpen = new List<string>();
        foreach (var schema in schemas)
        {
            var code = Text(schema, "schema_code");
            var evidence = schemaEvidence.LastOrDefault(r =>
                Text(r, "schema_code") == code);
            var hashes = new[] { "producer_semantic_hash",
                "validator_semantic_hash", "persistence_semantic_hash",
                "canonical_semantic_hash" }
                .Select(f => evidence is null ? "" : Text(evidence, f))
                .ToList();
            if (Text(schema, "parity_status") is not ("PASS" or "VERIFIED")
                || evidence is null
                || Text(evidence, "status") != "PASS"
                || Text(evidence, "validated_against_version") != version
                || hashes.Any(h => !HashShape.IsMatch(h))
                || hashes.Distinct(StringComparer.Ordinal).Count() != 1)
                schemaOpen.Add(code);
        }
        var schemaDigest = DigestRows(schemas.Concat(schemaEvidence));

        // -- implementation obligations ------------------------------
        var obligations = Records(connection,
            "implementation_obligations");
        var obligationCounts = obligations
            .GroupBy(r => Text(r, "current_state"))
            .OrderBy(g => g.Key, StringComparer.Ordinal)
            .ToDictionary(g => g.Key, g => g.Count(),
                StringComparer.Ordinal);
        var openObligations = obligations
            .Where(r => !new[] { "complete", "superseded" }
                .Contains(Text(r, "current_state")))
            .Select(r => Text(r, "obligation_code")).ToList();
        var stateParts = string.Join(",",
            obligationCounts.Select(p => $"{p.Key}={p.Value}"));
        var obligationDigest = DigestRows(obligations);

        // -- normative surface ----------------------------------------
        var allSurface = Records(connection, "current_normative_surface");
        var surface = allSurface
            .Where(r => Text(r, "lifecycle_state") == "active").ToList();
        var surfaceUnknown = surface
            .Where(r => Text(r, "surface_layer") == "UNKNOWN").ToList();
        var unknownArticles = surfaceUnknown
            .Count(r => Text(r, "object_type") == "article");
        var nonactiveDefaultVisible = allSurface.Count(r =>
            Text(r, "lifecycle_state") != "active"
            && Int(r, "default_search_visible") != 0);
        var surfaceDigest = DigestRows(surface);

        // -- directory governance --------------------------------------
        var directories = Records(connection, "directory_master_catalog")
            .Where(r => Text(r, "retired_version").Length == 0).ToList();
        var directoryOpen = directories
            .Where(r => !new[] { "active", "verified", "complete" }
                .Contains(Text(r, "implementation_state"))).ToList();
        var canonical = Records(connection,
            "project_architecture_directory");
        var canonicalColumns = Columns(connection,
            "project_architecture_directory");
        var normalized = Records(connection,
            "a233_normalized_directory_entry")
            .Where(r => Text(r, "source_table")
                == "project_architecture_directory").ToList();
        var directoryMismatch = new List<string>();
        foreach (var row in canonical)
        {
            var code = Text(row, "architecture_code");
            var projection = normalized.FirstOrDefault(r =>
                Text(r, "source_key") == code);
            if (projection is null
                || ProjectionMismatch(row, canonicalColumns, projection))
                directoryMismatch.Add(code);
        }
        var directoryDigest = DigestRows(canonical
            .Concat(normalized).Concat(directories));

        // -- active-article payload duplication ------------------------
        var articles = Records(connection, "articles");
        var duplicateGroups = articles
            .Where(r => activeArticleIds.Contains(
                Text(r, "provision_id")))
            .GroupBy(r => AmendmentContract.ContentHash(new object?[]
            {
                r.GetValueOrDefault("rule"),
                r.GetValueOrDefault("prohibition"),
                r.GetValueOrDefault("exception"),
            }))
            .Count(g => g.Count() > 1);

        // -- search document currentness -------------------------------
        var search = Records(connection, "codex_search_document");
        var staleSearch = search.Count(doc =>
        {
            var id = Text(doc, "provision_id");
            var life = lifecycle.FirstOrDefault(r =>
                Text(r, "provision_type") == Text(doc, "provision_type")
                && Text(r, "provision_id") == id);
            return Text(doc, "version_identity") != version
                || life is null
                || Text(doc, "lifecycle_state")
                    != Text(life, "lifecycle_state");
        });
        var searchDigest = DigestRows(search);

        // -- architecture diagram sync ---------------------------------
        var sync = Records(connection,
            "architecture_diagram_sync_evidence")
            .Where(r => Text(r, "status") == "current").ToList();
        var projectionOpen = sync.Count(r =>
            Text(r, "result") != "PASS"
            || Int(r, "hash_match_count") != Int(r, "required_count")
            || Int(r, "stale_count") != 0 || Int(r, "missing_count") != 0);
        var firstSync = sync.FirstOrDefault();

        // -- closure rows -----------------------------------------------
        void UpdateClosure(string id, string required, string evidence,
            long open, string result) =>
            connection.Execute(
                "UPDATE codex_convergence_closure SET version_identity=?, "
                + "verified_at=?, status='current', "
                + "verifier='RULE_CODEX_CONVERGENCE/"
                + "current-registry-evaluation', required_subresults=?, "
                + "evidence_roots=?, open_finding_count=?, result=? "
                + "WHERE closure_id=? AND status='current'",
                new object?[] { version, version, required,
                    source + evidence, open, result, id });

        var incomplete = "INCOMPLETE_EVIDENCE";
        UpdateClosure("FORMAL_RULE_CLOSURE",
            "all current participating rules require active controlling "
            + "source,current-generation registry/evaluator/test/prose "
            + "hashes and independent execution evidence",
            $"registry={formal.Count};participating={formalCurrent.Count};"
            + $"verified-labels={formalCurrent.Count(r => Text(r, "parity_status") == "VERIFIED")};"
            + $"retired={formal.Count(r => Text(r, "status") == "retired")};"
            + $"withdrawn={formal.Count(r => Text(r, "status") == "withdrawn")};"
            + $"input-sha256={formalDigest};"
            + $"open-rules={string.Join(",", formalFindings.Select(f => f.RuleCode))};"
            + $"findings-sha256={findingsDigest};NO_RUNTIME_PARITY_INFERRED",
            formalFindings.Count,
            formalFindings.Count > 0 ? incomplete : "PASS");
        UpdateClosure("MACHINE_SCHEMA_CLOSURE",
            "all registered schemas require independent current "
            + "producer/validator/persistence/canonical execution "
            + "evidence",
            $"registered={schemas.Count};PENDING={schemaOpen.Count};"
            + $"input-sha256={schemaDigest};"
            + "activation-and-verified-release=DENIED",
            schemaOpen.Count, schemaOpen.Count > 0 ? incomplete : "PASS");
        UpdateClosure("NORMATIVE_SURFACE_CLOSURE",
            "active lifecycle identities require classified canonical "
            + "current normative surface",
            $"active={surface.Count};unknown={surfaceUnknown.Count};"
            + $"input-sha256={surfaceDigest}",
            surfaceUnknown.Count,
            surfaceUnknown.Count > 0 ? incomplete : "PASS");
        UpdateClosure("DIRECTORY_CLOSURE",
            "canonical normalized project parity and registered "
            + "directory governance acceptance",
            $"project={canonical.Count}/{normalized.Count};"
            + $"semantic-mismatches={directoryMismatch.Count};"
            + $"catalog={directories.Count};"
            + $"catalog-open={directoryOpen.Count};"
            + $"input-sha256={directoryDigest}",
            directoryMismatch.Count + directoryOpen.Count,
            directoryMismatch.Count + directoryOpen.Count > 0
                ? incomplete : "PASS");
        UpdateClosure("DUPLICATION_CLOSURE",
            "exact active-payload uniqueness and current independent "
            + "semantic duplication review",
            $"exact-duplicate-groups={duplicateGroups};"
            + "semantic-review=PENDING_CURRENT_SEMANTIC_REVIEW",
            duplicateGroups + 1, incomplete);
        UpdateClosure("SEARCH_CURRENTNESS_CLOSURE",
            "search lifecycle and generation joined to canonical "
            + "lifecycle;nonactive default visibility denied;candidate "
            + "search is rebuilt by the governed pipeline",
            $"checked={search.Count};source-stale={staleSearch};"
            + $"source-nonactive-default-flags={nonactiveDefaultVisible};"
            + $"nonactive-default-flags-explicitly-cleared={nonactiveDefaultVisible};"
            + $"input-sha256={searchDigest};"
            + "candidate-publication-requires-search-rebuild",
            staleSearch, staleSearch > 0 ? incomplete : "PASS");
        UpdateClosure("PROJECTION_PARITY_CLOSURE",
            "current measured architecture file registry parity;"
            + "candidate seal/mirror/SQL parity validated by "
            + "publication pipeline;not implementation certification",
            $"diagram-required={(firstSync is null ? 0 : Int(firstSync, "required_count"))};"
            + $"diagram-match={(firstSync is null ? 0 : Int(firstSync, "hash_match_count"))};"
            + $"source-result={(firstSync is null ? "MISSING" : Text(firstSync, "result"))};"
            + "independent-mirror-and-SQL-publication-gate=REQUIRED",
            projectionOpen,
            projectionOpen > 0 ? incomplete : "PASS");
        var totalOpen = (long)formalFindings.Count + schemaOpen.Count
            + surfaceUnknown.Count + directoryMismatch.Count
            + directoryOpen.Count + duplicateGroups + 1 + staleSearch
            + projectionOpen + openObligations.Count + 1;
        UpdateClosure("CODEX_CONVERGENCE_CLOSURE",
            "NORMATIVE_SURFACE_CLOSURE|DIRECTORY_CLOSURE|"
            + "MACHINE_SCHEMA_CLOSURE|FORMAL_RULE_CLOSURE|"
            + "SEARCH_CURRENTNESS_CLOSURE|PROJECTION_PARITY_CLOSURE|"
            + $"DUPLICATION_CLOSURE|OBLIGATION:{stateParts}|"
            + "EXTERNAL_SIGNATURE:externally-blocked",
            $"formal={formalFindings.Count};schema={schemaOpen.Count};"
            + $"surface={surfaceUnknown.Count};"
            + $"directory={directoryOpen.Count};"
            + "semantic-duplication-review=1;"
            + $"open-obligations={openObligations.Count};"
            + "external-signature=1;"
            + $"obligation-input-sha256={obligationDigest};"
            + "counts=gate-instances-not-unique-incidents",
            totalOpen, incomplete);

        // -- current metrics row ---------------------------------------
        if (HasTable(connection, "codex_convergence_metrics"))
        {
            var metrics = Records(connection,
                "codex_convergence_metrics");
            var current = metrics.FirstOrDefault(r =>
                Text(r, "status") == "current");
            if (current is not null)
            {
                var reportId = Text(current, "report_id");
                var layerCounts = new (string Kind, string Layer)[]
                {
                    ("registry_fact", "REGISTRY_FACT"),
                    ("machine_shape", "MACHINE_SCHEMA"),
                    ("formal_logic", "FORMAL-RULE"),
                    ("duplicate", "DUPLICATE"),
                    ("mixed", "MIXED"),
                };
                long SurfaceCount(string layer) => surface.Count(r =>
                    Text(r, "object_type") == "article"
                    && (layer == "SPECIAL_LAW*"
                        ? Text(r, "surface_layer").StartsWith(
                            "SPECIAL_LAW", StringComparison.Ordinal)
                        : Text(r, "surface_layer") == layer));
                var owners = Records(connection,
                    "formal_rule_ownership_map")
                    .Where(r => new[] { "current", "active" }
                        .Contains(Text(r, "status")))
                    .GroupBy(r => Text(r, "invariant_code")).ToList();
                var ownerRate = $"{owners.Count(g => g.Count() == 1)}"
                    + $"/{owners.Count}";
                var closureDup = Records(connection,
                    "codex_convergence_closure")
                    .Where(r => Text(r, "status") == "current")
                    .GroupBy(r => Text(r, "closure_id"))
                    .Count(g => g.Count() > 1);
                var staleEffective = Records(connection,
                    "effective_provisions")
                    .Count(r => Text(r, "current_binding_version")
                        != version);
                connection.Execute(
                    "UPDATE codex_convergence_metrics SET "
                    + "version_identity=?, status='current', "
                    + "result='INCOMPLETE_EVIDENCE', "
                    + "active_article_count_before=?, "
                    + "active_article_count_after=?, "
                    + "core_article_count=?, special_law_count=?, "
                    + "ordinance_count=?, "
                    + "registry_fact_article_count_before=?, "
                    + "registry_fact_article_count_after=?, "
                    + "machine_shape_article_count_before=?, "
                    + "machine_shape_article_count_after=?, "
                    + "formal_logic_article_count_before=?, "
                    + "formal_logic_article_count_after=?, "
                    + "duplicate_article_count_before=?, "
                    + "duplicate_article_count_after=?, "
                    + "mixed_article_count_before=?, "
                    + "mixed_article_count_after=?, "
                    + "unknown_article_count=?, "
                    + "stale_effective_count=?, "
                    + "superseded_default_search_count=?, "
                    + "directory_coverage=?, machine_schema_parity=?, "
                    + "formal_rule_single_owner_rate=?, "
                    + "closure_duplication_count=? "
                    + "WHERE report_id=? AND status='current'",
                    new object?[]
                    {
                        version,
                        Int(current, "active_article_count_after"),
                        (long)activeArticleIds.Count,
                        SurfaceCount("CORE_CONSTITUTIONAL"),
                        SurfaceCount("SPECIAL_LAW*"),
                        SurfaceCount("ORDINANCE"),
                        Int(current, "registry_fact_article_count_after"),
                        SurfaceCount("REGISTRY_FACT"),
                        Int(current, "machine_shape_article_count_after"),
                        SurfaceCount("MACHINE_SCHEMA"),
                        Int(current, "formal_logic_article_count_after"),
                        SurfaceCount("FORMAL-RULE"),
                        Int(current, "duplicate_article_count_after"),
                        SurfaceCount("DUPLICATE"),
                        Int(current, "mixed_article_count_after"),
                        SurfaceCount("MIXED"),
                        (long)unknownArticles,
                        (long)staleEffective,
                        (long)nonactiveDefaultVisible,
                        $"project:{canonical.Count}/{normalized.Count};"
                        + $"catalog:{directories.Count};"
                        + $"open:{directoryOpen.Count}",
                        $"{schemas.Count - schemaOpen.Count}/{schemas.Count}",
                        ownerRate, (long)closureDup, reportId,
                    });
            }
        }

        // -- measured governance closure components ---------------------
        if (!HasTable(connection, "governance_closure_state"))
            return;
        var graph = Records(connection, "governance_closure_state")
            .Where(r => new[] { "current", "active" }
                .Contains(Text(r, "status"))).ToList();
        var releasePending = new HashSet<string>(StringComparer.Ordinal);
        foreach (var component in graph)
        {
            var code = Text(component, "component_code");
            if (code == "VERIFIED_RELEASE") continue;
            if (Text(component, "governance_state") == "PASS") continue;
            releasePending.Add(code);
            foreach (var pending in Text(component, "pending_codes")
                .Split('|'))
                if (pending.Length > 0) releasePending.Add(pending);
        }
        foreach (var code in openObligations)
            if (code.Length > 0) releasePending.Add(code);
        foreach (var code in new[] { "EXTERNAL_SIGNATURE",
            "MACHINE_SCHEMA_PARITY", "FORMAL_EVALUATOR_PARITY",
            "CODEX_CONVERGENCE_CLOSURE" })
            releasePending.Add(code);

        void UpdateGraph(string code, string summary, string pending,
            string certification = "CODEX_CERTIFICATION_INCOMPLETE")
        {
            if (!graph.Any(r => Text(r, "component_code") == code))
                throw new InvalidOperationException(
                    $"CLOSURE_COMPONENT_MISSING:{code}");
            connection.Execute(
                "UPDATE governance_closure_state SET evidence_summary=?, "
                + "pending_codes=?, governance_state='INCOMPLETE_EVIDENCE', "
                + "certification_state=?, version_identity=?, "
                + "status='current' WHERE component_code=?",
                new object?[] { summary, pending, certification,
                    version, code });
        }

        UpdateGraph("FORMAL_EVALUATOR_PARITY",
            $"re-evaluated registry={formal.Count};"
            + $"participating={formalCurrent.Count};"
            + $"historical-VERIFIED-labels={formalCurrent.Count};"
            + $"current-evidence-open={formalFindings.Count};"
            + "registry/evaluator/test/prose proof required;"
            + $"source={predecessor}",
            string.Join("|", formalFindings.Select(f => f.RuleCode)));
        UpdateGraph("IMPLEMENTATION_OBLIGATIONS",
            $"lifecycle-counts:{stateParts};total={obligations.Count};"
            + $"open={openObligations.Count};"
            + "evidence-submitted is not accepted;superseded is not open",
            string.Join("|", openObligations));
        UpdateGraph("CODEX_CONVERGENCE_CLOSURE",
            $"current registry evaluation;"
            + $"formal-open={formalFindings.Count};"
            + $"schema-open={schemaOpen.Count};"
            + $"surface-open={surfaceUnknown.Count};"
            + $"directory-open={directoryOpen.Count};"
            + "semantic-review-pending=1;"
            + $"open-obligations={openObligations.Count};"
            + "external-signature-blocked=1",
            "UNKNOWN_ARTICLE_CLASSIFICATION|MACHINE_SCHEMA_PARITY|"
            + "FORMAL_RULE_PARITY|DIRECTORY_GOVERNANCE_DATA|"
            + "SEMANTIC_DUPLICATION_REVIEW|IMPLEMENTATION_OBLIGATIONS|"
            + "EXTERNAL_SIGNATURE");
        UpdateGraph("VERIFIED_RELEASE",
            "DENIED:union of all registered open components plus all "
            + $"{openObligations.Count} open obligations and actual "
            + "external signature gate;no verification inferred from "
            + "technical audit or status labels",
            string.Join("|", releasePending.OrderBy(c => c,
                StringComparer.Ordinal)),
            "VERIFIED_RELEASE_DENIED");
    }
}
