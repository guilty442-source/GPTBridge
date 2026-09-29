using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Codex amendment pipeline driver — intake → successor build →
/// five-sovereign audit — port of codex_amendment_driver.py.
///
///     staged request file → CodexAmendmentRequestLedger.Begin
///         (lineage lock) → BuildSuccessor (authority export →
///         candidate artifact + manifest) → RunFiveSovereignAudit
///         (unanimous receipts + certificate) → ready-for-governor
///
/// Stop line: this driver never seals, signs or publishes.  The
/// unanimous audit certificate only advances a request to
/// ``ready-for-governor``; publication remains governor-invoked through
/// the executor apply path (A382).
///
/// Authority source: the live PostgreSQL codex schema is authoritative
/// (A173).  When the canonical ``codex/data/governance_codex.sql``
/// artifact is absent the driver exports the PostgreSQL authority into
/// a non-authoritative ``.sql`` artifact — the same contract
/// ``IsolateGeneration`` uses in the update pipeline.
///
/// ``codex.amend`` channel submissions are notifications only: the
/// staged request artifact file is the authoritative intake.
/// </summary>
internal static partial class Driver
{
    public static string CanonicalCodexRoot() =>
        UpdatePipeline.CanonicalCodexRoot();

    public static string CanonicalDatabase() =>
        Path.Combine(CanonicalCodexRoot(), "data",
            UpdatePipeline.DatabaseName);

    public const string IntakeGlob = "codex-amendment-request-*.json";
    public const string CandidatesDirname = "candidates";

    public static string[] IntakeDirs() => new[]
    {
        Repo.StateDir(),
        Path.Combine(Repo.Root(), "governance_rule", "execution",
            "audit", "convergence"),
    };

    public static string ExportDir() =>
        Path.Combine(Repo.Root(), "main-system", "runtime", "temp",
            "codex-amendment-intake");

    private static string Sha256(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream))
            .ToLowerInvariant();
    }

    /// <summary>Table names of a readable ``.sql`` artifact; null when
    /// unreadable.</summary>
    private static HashSet<string>? ArtifactTableNames(string? artifact)
        => artifact is null ? null
            : StageCodec.ArtifactTableNames(artifact);

    /// <summary>Non-authoritative working copy of the live codex
    /// authority.</summary>
    private static string AuthoritySourceDatabase()
    {
        var canonical = CanonicalDatabase();
        if (File.Exists(canonical))
            return canonical;
        var target = Path.Combine(ExportDir(), "authority-export.sql");
        if (File.Exists(target))
            // A stale export must never masquerade as the live
            // authority, so refresh it.
            File.Delete(target);
        return PgExport.ExportPostgresqlCodex(target);
    }

    /// <summary>Live codex version + revision sequence for lineage
    /// staleness checks.</summary>
    private static (string? Version, long? Sequence)
        LiveAuthorityIdentity()
    {
        Dictionary<string, object?> state;
        try
        {
            state = PgExport.AuthorityState();
        }
        catch (Exception)
        {
            return (null, null);
        }
        var version = (state.TryGetValue("codex_version", out var v)
            ? v?.ToString() ?? ""
            : state.TryGetValue("version", out var v2)
                ? v2?.ToString() ?? "" : "").Trim();
        long? sequence = null;
        if (state.TryGetValue("revision_sequence", out var seq)
            && seq is not null)
        {
            try { sequence = Convert.ToInt64(seq); }
            catch (Exception) { sequence = null; }
        }
        return (version.Length > 0 ? version : null, sequence);
    }

    /// <summary>Other non-terminal requests holding the same
    /// predecessor lineage.</summary>
    private static List<string> LineageConflicts(
        CodexAmendmentRequestLedger ledger, AmendmentRequest request)
    {
        var conflicts = new List<string>();
        if (!Directory.Exists(ledger.RecordsDir))
            return conflicts;
        foreach (var path in Directory.GetFiles(ledger.RecordsDir,
            "*.json").OrderBy(p => p, StringComparer.Ordinal))
        {
            JsonNode? record;
            try { record = JsonNode.Parse(File.ReadAllText(path)); }
            catch (Exception) { continue; }
            if (record is not JsonObject map)
                continue;
            if (Repo.Str(map, "request_id") == request.RequestId)
                continue;
            if (Lifecycle.TerminalStates.Contains(
                    Repo.Str(map, "state")))
                continue;
            if (Repo.Str(map, "lineage_key") == request.LineageKey)
            {
                var id = Repo.Str(map, "request_id");
                conflicts.Add(id.Length > 0
                    ? id : Path.GetFileNameWithoutExtension(path));
            }
        }
        return conflicts;
    }

    private static List<string> ScopeTables(AmendmentRequest request)
    {
        var tables = new List<string>();
        foreach (var entry in request.Scope)
            foreach (var prefix in new[] { "table:", "registry:" })
                if (entry.StartsWith(prefix, StringComparison.Ordinal))
                {
                    var name = entry[(prefix.Length)..].Trim();
                    if (name.Length > 0 && !tables.Contains(name))
                        tables.Add(name);
                }
        return tables;
    }

    private static Func<Dictionary<string, object?>> DecisionCheck(
        string requestPath, CodexAmendmentRequestLedger ledger) =>
        () =>
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            var payload = request.Payload;
            var changeClass = Repo.Str(payload, "change_class").Trim();
            var classOk = Amendment.ChangeClasses.Contains(changeClass);
            var predecessor = request.Predecessor;
            var basis = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["codex_version"] = predecessor
                    .TryGetValue("codex_version", out var v)
                    ? v?.ToString() ?? "" : "",
                ["history_head"] = predecessor
                    .TryGetValue("history_head", out var h)
                    ? h?.ToString() ?? "" : "",
                ["revision_sequence"] = predecessor
                    .TryGetValue("revision_sequence", out var seq)
                    ? seq : null,
            };
            var basisOk = ((string)basis["codex_version"]!).Length > 0
                && ((string)basis["history_head"]!).Length > 0;
            var conflicts = LineageConflicts(ledger, request);
            var ok = classOk && basisOk && conflicts.Count == 0;
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = ok,
                ["method"] = "decision-basis-verification",
                ["findings"] = new List<object?>
                {
                    $"change_class:"
                        + (changeClass.Length > 0
                            ? changeClass : "missing"),
                    $"basis:{(basisOk ? "complete" : "incomplete")}",
                    "lineage:"
                        + (conflicts.Count == 0
                            ? "unique" : "conflict:")
                        + (conflicts.Count == 0
                            ? "" : string.Join(",", conflicts)),
                },
                ["evidence"] = new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["amendment_class"] = changeClass,
                    ["basis_references"] = basis,
                    ["successor_scope_unique"] = conflicts.Count == 0
                        ? "unique-lineage"
                        : "conflict:" + string.Join(",", conflicts),
                },
                ["error"] = ok ? "" : "DECISION_BASIS_FAILED",
            };
        };

    private static Func<Dictionary<string, object?>> PermissionCheck(
        string requestPath, string? sourceDatabase) =>
        () =>
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            var payload = request.Payload;
            var rawRequester = Repo.Str(payload, "requested_by").Trim();
            var requester = Amendment.SovereignAliases
                .TryGetValue(rawRequester, out var alias)
                ? alias : rawRequester;
            var rosterOk = Amendment.ActiveSovereignRequesters
                .Contains(requester);
            var review = Repo.Str(payload, "required_review").Trim();
            var reviewOk = review == Amendment.RequiredGate;
            var scopeTables = ScopeTables(request);
            var missing = new List<string>();
            var unverifiable = false;
            if (scopeTables.Count > 0)
            {
                var names = ArtifactTableNames(sourceDatabase);
                if (names is null)
                    unverifiable = true;
                else
                    missing = scopeTables
                        .Where(name => !names.Contains(name)).ToList();
            }
            var ok = rosterOk && reviewOk && missing.Count == 0
                && !unverifiable;
            var findings = new List<object?>
            {
                $"requester:"
                    + (requester.Length > 0 ? requester : "missing")
                    + $":{(rosterOk ? "roster" : "not-in-roster")}",
                $"required_review:"
                    + (review.Length > 0 ? review : "missing"),
                $"scope_tables:{scopeTables.Count}",
            };
            if (missing.Count > 0)
                findings.Add("unresolvable:" + string.Join(",", missing));
            if (unverifiable)
                findings.Add("scope-tables-unverifiable");
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = ok,
                ["method"] = "directory-parity-diff",
                ["findings"] = findings,
                ["evidence"] = new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["directory_rows"] = new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["requested_by"] = requester,
                        ["scope_tables"] =
                            scopeTables.Cast<object?>().ToList(),
                        ["unresolvable"] =
                            missing.Cast<object?>().ToList(),
                    },
                    ["identity_lifecycle_parity"] = rosterOk
                        ? "requester-active" : "requester-not-in-roster",
                    ["testflow_references"] = review,
                },
                ["error"] = ok ? "" : "DIRECTORY_PARITY_FAILED",
            };
        };

    private static Func<Dictionary<string, object?>> RuntimeCheck(
        string requestPath, string? sourceDatabase) =>
        () =>
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            var payload = request.Payload;
            var flow = Repo.Str(payload, "flow").Trim();
            var flowOk = flow == Amendment.AmendmentFlow;
            var started = System.Diagnostics.Stopwatch.StartNew();
            var names = ArtifactTableNames(sourceDatabase);
            var readMs = (int)started.ElapsedMilliseconds;
            var liveOk = names is not null && names.Contains("metadata");
            var ok = flowOk && liveOk;
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = ok,
                ["method"] = "reader-generation-plan",
                ["findings"] = new List<object?>
                {
                    $"flow:{(flow.Length > 0 ? flow : "missing")}",
                    $"predecessor_readable:{liveOk}",
                    $"predecessor_read_ms:{readMs}",
                },
                ["evidence"] = new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["reader_generation_plan"] = flow,
                    ["channel_continuity"] = liveOk
                        ? "predecessor-readable-during-staging"
                        : "predecessor-unreadable",
                    ["health_window"] = new Dictionary<string, object?>
                    {
                        ["predecessor_read_ms"] = (long)readMs,
                    },
                },
                ["error"] = ok ? "" : "RUNTIME_CONTINUITY_FAILED",
            };
        };

    private static Func<Dictionary<string, object?>> AutomationCheck(
        string? manifestPath, string? candidatePath,
        CodexAmendmentRequestLedger ledger) =>
        () =>
        {
            if (manifestPath is null || candidatePath is null
                || !File.Exists(manifestPath)
                || !File.Exists(candidatePath))
                return new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["ok"] = false,
                    ["method"] = "staging-seal-mirror-mechanics",
                    ["error"] = "STAGED_CANDIDATE_MISSING",
                    ["evidence"] = new Dictionary<string, object?>
                    {
                        ["staging_isolation"] = "",
                        ["seal_roots"] = "",
                        ["mirror_chain"] = "",
                        ["version_identity"] = "",
                        ["rollback_pointer"] = "",
                    },
                };
            var manifest = Lifecycle.LoadJsonObject(manifestPath);
            var candidate = Path.GetFullPath(candidatePath);
            var ledgerRoot = Path.GetFullPath(ledger.Root);
            bool isolationOk;
            try
            {
                isolationOk = candidate.StartsWith(
                    ledgerRoot.TrimEnd(Path.DirectorySeparatorChar)
                    + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase)
                    && !candidate.StartsWith(
                        Path.GetFullPath(CanonicalCodexRoot())
                            .TrimEnd(Path.DirectorySeparatorChar)
                        + Path.DirectorySeparatorChar,
                        StringComparison.OrdinalIgnoreCase);
            }
            catch (Exception) { isolationOk = false; }
            var digest = Sha256(candidate);
            var chainOk = digest == Repo.Str(manifest,
                "candidate_sha256");
            var seal = Repo.Get(manifest, "seal_preview");
            var sealOk = seal is JsonObject { Count: > 0 };
            var version = Repo.Str(manifest, "successor_version").Trim();
            if (version.Length == 0)
                version = StageCodec.ArtifactVersion(candidate);
            var predecessorVersion = (Repo.Get(manifest, "predecessor")
                is JsonObject pred
                    ? Repo.Str(pred, "codex_version") : "").Trim();
            var versionOk = version.Length > 0
                && version != predecessorVersion;
            var rollbackOk = predecessorVersion.Length > 0;
            var ok = isolationOk && chainOk && sealOk && versionOk
                && rollbackOk;
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = ok,
                ["method"] = "staging-seal-mirror-mechanics",
                ["findings"] = new List<object?>
                {
                    $"candidate:{Path.GetFileName(candidate)}",
                    $"sha256:{(chainOk ? "match" : "mismatch")}",
                    $"seal_preview:{(sealOk ? "present" : "missing")}",
                    $"version:{(version.Length > 0 ? version : "missing")}",
                },
                ["evidence"] = new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["staging_isolation"] = isolationOk
                        ? $"{Path.GetFileName(ledgerRoot)}"
                            + $"/{CandidatesDirname}"
                        : "outside-ledger-root",
                    ["seal_roots"] = sealOk ? "seal-preview-present" : "",
                    ["mirror_chain"] = chainOk ? "sha256-verified" : "",
                    ["version_identity"] = version,
                    ["rollback_pointer"] = predecessorVersion,
                },
                ["error"] = ok ? "" : "STAGING_MECHANICS_FAILED",
            };
        };

    /// <summary>Locate the candidate manifest recorded by the build
    /// stage.</summary>
}
