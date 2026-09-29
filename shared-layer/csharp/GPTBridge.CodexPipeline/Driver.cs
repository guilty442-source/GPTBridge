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
internal static class Driver
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
    private static string? ManifestPathFor(
        CodexAmendmentRequestLedger ledger, string requestId)
    {
        var record = ledger.LoadRecord(requestId)
            ?? new Dictionary<string, object?>();
        if (record.TryGetValue("history", out var hist)
            && hist is List<object?> entries)
            for (var i = entries.Count - 1; i >= 0; i--)
            {
                if (entries[i] is not IDictionary<string, object?> entry
                    || !entry.TryGetValue("evidence", out var ev)
                    || ev is not IDictionary<string, object?> evidence)
                    continue;
                if (evidence.TryGetValue("manifest_path", out var mp)
                    && mp is not null)
                {
                    var path = mp.ToString() ?? "";
                    if (File.Exists(path))
                        return path;
                }
            }
        var fallback = Path.Combine(ledger.Root, CandidatesDirname,
            $"{requestId}.candidate-manifest.json");
        return File.Exists(fallback) ? fallback : null;
    }

    /// <summary>The standard five-sovereign check callables for one
    /// request — deterministic standard-charter verifications of the
    /// staged request/candidate.</summary>
    public static Dictionary<string, Func<Dictionary<string, object?>>>
        DefaultSovereignChecks(
            string requestPath,
            CodexAmendmentRequestLedger ledger,
            string? manifestPath = null,
            string? candidatePath = null,
            string? sourceDatabase = null)
    {
        string? manifest;
        if (manifestPath is null)
        {
            var requestId = "";
            try
            {
                requestId = Lifecycle.LoadAmendmentRequest(requestPath)
                    .RequestId;
            }
            catch (AmendmentLifecycleError) { }
            manifest = requestId.Length > 0
                ? ManifestPathFor(ledger, requestId) : null;
        }
        else
        {
            manifest = manifestPath;
        }
        string? candidate = candidatePath
            ?? (manifest is not null
                ? Path.Combine(Path.GetDirectoryName(manifest)!,
                    Path.GetFileName(manifest)!
                        .Replace(".candidate-manifest.json", ".sql",
                            StringComparison.Ordinal))
                : null);
        if (candidate is not null
            && !candidate.EndsWith(".sql", StringComparison.Ordinal))
            candidate = null;
        string requestIdFinal;
        string[] scope;
        try
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            requestIdFinal = request.RequestId;
            scope = request.Scope;
        }
        catch (AmendmentLifecycleError)
        {
            requestIdFinal = "";
            scope = Array.Empty<string>();
        }
        return new Dictionary<string,
            Func<Dictionary<string, object?>>>(StringComparer.Ordinal)
        {
            ["decision-sovereign"] =
                DecisionCheck(requestPath, ledger),
            ["permission-sovereign"] =
                PermissionCheck(requestPath, sourceDatabase),
            ["system-runtime-sovereign"] =
                RuntimeCheck(requestPath, sourceDatabase),
            ["automation-sovereign"] =
                AutomationCheck(manifest, candidate, ledger),
            ["xingcheng"] =
                SovereignAudit.BuildXingchengAssistantCoreCheck(
                    requestIdFinal, scope),
        };
    }

    /// <summary>Discover staged request artifacts and their ledger
    /// states.</summary>
    public static List<Dictionary<string, object?>> ScanRequests(
        IEnumerable<string>? intakeDirs = null,
        CodexAmendmentRequestLedger? ledger = null)
    {
        ledger ??= new CodexAmendmentRequestLedger();
        var directories = intakeDirs?.ToArray() ?? IntakeDirs();
        var found = new Dictionary<string, Dictionary<string, object?>>(
            StringComparer.Ordinal);
        foreach (var directory in directories)
        {
            if (!Directory.Exists(directory))
                continue;
            foreach (var path in Directory.GetFiles(directory,
                IntakeGlob).OrderBy(p => p, StringComparer.Ordinal))
            {
                AmendmentRequest request;
                try
                {
                    request = Lifecycle.LoadAmendmentRequest(path);
                }
                catch (AmendmentLifecycleError error)
                {
                    found[path] = new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["path"] = path,
                        ["request_id"] =
                            Path.GetFileNameWithoutExtension(path),
                        ["valid"] = false,
                        ["error"] = error.Message,
                        ["state"] = "",
                    };
                    continue;
                }
                var record = ledger.LoadRecord(request.RequestId);
                found[request.RequestId] =
                    new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["path"] = path,
                        ["request_id"] = request.RequestId,
                        ["valid"] = true,
                        ["state"] = record?.TryGetValue("state",
                            out var s) == true
                            ? s?.ToString() ?? "" : "",
                        ["scope"] = request.Scope
                            .Cast<object?>().ToList(),
                    };
            }
        }
        return found.Values
            .OrderBy(item => item["request_id"]?.ToString(),
                StringComparer.Ordinal)
            .ToList();
    }

    /// <summary>Newest recorded audit verdict for ``requestId`` from
    /// the append-only audit ledger — its presence there is itself the
    /// recording evidence the executor revalidates.</summary>
    private static Dictionary<string, object?>? LatestAuditResult(
        string requestId)
    {
        string[] lines;
        try
        {
            lines = File.ReadAllLines(SovereignAudit.AuditLedgerPath());
        }
        catch (IOException) { return null; }
        Dictionary<string, object?>? latest = null;
        foreach (var line in lines)
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0)
                continue;
            JsonNode? entry;
            try { entry = JsonNode.Parse(trimmed); }
            catch (JsonException) { continue; }
            if (entry is JsonObject obj
                && Repo.Str(obj, "amendment_id") == requestId)
                latest = (Dictionary<string, object?>)
                    Repo.ToPlain(obj)!;
        }
        if (latest is null
            || !(latest.TryGetValue("ok", out var ok) && ok is true))
            return null;
        latest["audit_recorded"] = true;
        return latest;
    }

    /// <summary>Apply a certified amendment end to end (A488 publish +
    /// seal).  Every failure is terminal ``rejected`` with evidence.
    /// </summary>
    private static Dictionary<string, object?> ExecuteReadyRequest(
        string requestPath, CodexAmendmentRequestLedger ledger,
        string requestId)
    {
        var auditResult = LatestAuditResult(requestId);
        if (auditResult is null)
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["state"] = Lifecycle.StateReadyForGovernor,
                ["error"] = "AUDIT_RESULT_UNAVAILABLE",
            };
        var candidate = Path.Combine(ledger.Root, CandidatesDirname,
            $"{requestId}.sql");
        if (!File.Exists(candidate))
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["state"] = Lifecycle.StateReadyForGovernor,
                ["error"] = "CANDIDATE_DATABASE_MISSING",
            };
        var certificateHash = "";
        if (auditResult.TryGetValue("certificate", out var cert)
            && cert is IDictionary<string, object?> certMap
            && certMap.TryGetValue("certificate_hash", out var ch))
            certificateHash = ch?.ToString() ?? "";
        AmendmentExecutionResult execution;
        try
        {
            execution = Executor.ExecuteAmendment(
                requestPath: requestPath,
                preparedDatabase: candidate,
                auditResult: auditResult,
                apply: true,
                // Per-request staging so a service tick and a CLI run
                // on the same request never share scratch state
                // mid-flight.
                stagingRoot: Path.Combine(Executor.DefaultStaging(),
                    requestId));
        }
        catch (ExecutorDenied error)
        {
            ledger.Reject(requestId, reason: error.Message,
                evidence: new Dictionary<string, object?>
                { ["stage"] = "execute", ["denied"] = true });
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["state"] = Lifecycle.StateRejected,
                ["error"] = error.Message,
            };
        }
        catch (Exception error)
        {
            ledger.Reject(requestId,
                reason: $"EXECUTE_ERROR:{error.Message}",
                evidence: new Dictionary<string, object?>
                { ["stage"] = "execute" });
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["state"] = Lifecycle.StateRejected,
                ["error"] = "EXECUTE_ERROR:"
                    + $"{error.GetType().Name}:{error.Message}",
            };
        }
        if (!execution.Ok)
        {
            ledger.Reject(requestId,
                reason: execution.Reason.Length > 0
                    ? execution.Reason : "update-pipeline-rejected",
                evidence: new Dictionary<string, object?>
                {
                    ["stage"] = "execute",
                    ["phases"] = execution.Phases
                        .Cast<object?>().ToList(),
                });
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["state"] = Lifecycle.StateRejected,
                ["error"] = execution.Reason.Length > 0
                    ? execution.Reason : "update-pipeline-rejected",
            };
        }
        ledger.Transition(requestId, Lifecycle.StateExecuted,
            new Dictionary<string, object?>
            {
                ["executor"] = "codex_amendment_executor",
                ["auto_execute"] = true,
                ["executed_at"] = Repo.UtcNow(),
                ["version"] = execution.Version,
                ["seal_state"] = execution.SealState,
                ["certificate_hash"] = certificateHash,
            });
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["ok"] = true,
            ["state"] = Lifecycle.StateExecuted,
            ["version"] = execution.Version,
            ["seal_state"] = execution.SealState,
            ["certificate_hash"] = certificateHash,
        };
    }

    /// <summary>Advance one staged request through build and audit.
    /// Without ``autoExecute`` the driver stops at
    /// ``ready-for-governor`` — publication stays governor-invoked.</summary>
    public static async Task<Dictionary<string, object?>>
        AdvanceRequest(
            string requestPath,
            CodexAmendmentRequestLedger? ledger = null,
            string? sourceDatabase = null,
            string? successorVersion = null,
            SovereignAudit.Gate? gate = null,
            bool autoExecute = false)
    {
        ledger ??= new CodexAmendmentRequestLedger();
        var result = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        { ["request_path"] = requestPath };
        AmendmentRequest request;
        try
        {
            request = Lifecycle.LoadAmendmentRequest(requestPath);
        }
        catch (AmendmentLifecycleError error)
        {
            // Close the malformed artifact in the ledger instead of
            // leaving it state-less (zombie reprocessing).
            try
            {
                ledger.RecordInvalidRequest(requestPath, error);
            }
            catch (Exception) { }
            result["ok"] = false;
            result["stage"] = "intake";
            result["state"] = Lifecycle.StateRejected;
            result["error"] = error.Message;
            result["request_id"] =
                Path.GetFileNameWithoutExtension(requestPath);
            return result;
        }
        var requestId = request.RequestId;
        result["request_id"] = requestId;
        var record = ledger.LoadRecord(requestId);
        var state = record?.TryGetValue("state", out var st) == true
            ? st?.ToString() ?? "" : "";
        if (Lifecycle.TerminalStates.Contains(state))
        {
            result["ok"] = state == Lifecycle.StateExecuted;
            result["stage"] = "terminal";
            result["state"] = state;
            return result;
        }
        if (state == Lifecycle.StateAuditing)
        {
            // A certificate can only exist after audit-passed; an
            // ``auditing`` record is a crashed cycle — rewind so the
            // gate can re-run.
            ledger.Transition(requestId, Lifecycle.StateSuccessorBuilt,
                new Dictionary<string, object?>
                { ["recovery"] = "auditing-rewind" });
            state = Lifecycle.StateSuccessorBuilt;
        }
        if (state is "" or Lifecycle.StateSubmitted
            or Lifecycle.StateUnderReview)
        {
            var callerSuppliedSource = sourceDatabase is not null;
            if (sourceDatabase is null)
            {
                try
                {
                    sourceDatabase = AuthoritySourceDatabase();
                }
                catch (Exception error)
                {
                    result["ok"] = false;
                    result["stage"] = "export";
                    result["state"] = state.Length > 0
                        ? state : Lifecycle.StateSubmitted;
                    result["error"] = "AUTHORITY_EXPORT_FAILED:"
                        + error.GetType().Name;
                    return result;
                }
            }
            var source = sourceDatabase;
            var candidate = Path.Combine(ledger.Root, CandidatesDirname,
                $"{requestId}.sql");
            // Staleness is only checked when the driver derived the
            // authority itself; a caller-supplied source is validated
            // as-is.
            string? currentVersion;
            long? revisionSequence;
            if (callerSuppliedSource)
            {
                currentVersion = null;
                revisionSequence = null;
            }
            else
            {
                (currentVersion, revisionSequence) =
                    LiveAuthorityIdentity();
            }
            if (string.IsNullOrEmpty(successorVersion))
            {
                // A candidate must carry a version distinct from its
                // predecessor or the staging-mechanics check denies it.
                successorVersion = Repo.UtcNow();
                var predecessorVersion =
                    request.Predecessor.TryGetValue("codex_version",
                        out var pv) ? pv?.ToString() ?? "" : "";
                if (successorVersion == predecessorVersion)
                    successorVersion = DateTimeOffset.UtcNow
                        .AddSeconds(1).ToString(
                            "yyyy-MM-dd'T'HH:mm:ss'Z'",
                            System.Globalization.CultureInfo
                                .InvariantCulture);
            }
            var build = SuccessorBuilder.BuildSuccessor(
                requestPath, source, candidate, ledger,
                successorVersion: successorVersion,
                expectedCurrentVersion: currentVersion,
                expectedRevisionSequence: revisionSequence);
            result["build"] = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = build.Ok,
                ["candidate_path"] = build.OutputDatabase,
                ["manifest_path"] = build.ManifestPath,
                ["applied"] = (build.Applied ?? new())
                    .Cast<object?>().ToList(),
                ["deferred"] = (long)(build.Deferred?.Count ?? 0),
                ["errors"] = (build.Errors ?? new())
                    .Cast<object?>().ToList(),
            };
            if (!build.Ok)
            {
                result["ok"] = false;
                result["stage"] = "build";
                result["state"] = Lifecycle.StateRejected;
                return result;
            }
            state = Lifecycle.StateSuccessorBuilt;
            sourceDatabase = source;
        }
        if (state == Lifecycle.StateSuccessorBuilt)
        {
            var source = sourceDatabase;
            if (source is null)
            {
                try { source = AuthoritySourceDatabase(); }
                catch (Exception) { source = null; }
            }
            if (source is null)
            {
                // Same deferral contract: a transient authority-export
                // failure must NOT reach the gate as a permanent
                // sovereign denial.
                result["ok"] = false;
                result["stage"] = "audit";
                result["state"] = Lifecycle.StateSuccessorBuilt;
                result["error"] = "AUTHORITY_EXPORT_UNAVAILABLE";
                return result;
            }
            var manifest = ManifestPathFor(ledger, requestId);
            var checks = DefaultSovereignChecks(requestPath,
                ledger: ledger, manifestPath: manifest,
                sourceDatabase: source);
            var run = await AuditRunner.RunFiveSovereignAudit(
                requestPath, checks, ledger, gate: gate,
                requester: Repo.Str(request.Payload, "requested_by"));
            result["audit"] = run.AsDict();
            result["ok"] = run.Ok;
            result["stage"] = "audit";
            result["state"] = run.State;
            if (!run.Ok)
                return result;
            state = run.State;
        }
        if (state == Lifecycle.StateAuditPassed)
        {
            // A crash between audit-passed and ready-for-governor left
            // the certificate recorded but the promotion unwritten.
            ledger.Transition(requestId,
                Lifecycle.StateReadyForGovernor,
                new Dictionary<string, object?>
                { ["recovery"] = "audit-passed-promotion" });
            state = Lifecycle.StateReadyForGovernor;
        }
        if (state == Lifecycle.StateReadyForGovernor && autoExecute)
        {
            var outcome = ExecuteReadyRequest(requestPath, ledger,
                requestId);
            result["execution"] = outcome;
            result["ok"] = outcome.TryGetValue("ok", out var ok)
                && ok is true;
            result["stage"] = "execute";
            result["state"] = outcome.TryGetValue("state", out var os)
                ? os?.ToString() ?? "" : "";
            if (outcome.TryGetValue("error", out var oe)
                && oe is not null)
                result["error"] = oe;
            return result;
        }
        // audit-passed or ready-for-governor: nothing left for the
        // driver.
        result["ok"] = true;
        result["stage"] = "audit";
        result["state"] = state;
        return result;
    }

    /// <summary>Advance every staged non-terminal request once.</summary>
    public static async Task<List<Dictionary<string, object?>>>
        AdvanceAll(
            IEnumerable<string>? intakeDirs = null,
            CodexAmendmentRequestLedger? ledger = null,
            string? sourceDatabase = null,
            string? successorVersion = null,
            SovereignAudit.Gate? gate = null,
            bool autoExecute = false)
    {
        ledger ??= new CodexAmendmentRequestLedger();
        var results = new List<Dictionary<string, object?>>();
        foreach (var item in ScanRequests(intakeDirs, ledger))
        {
            var valid = item.TryGetValue("valid", out var v)
                && v is true;
            var state = item.TryGetValue("state", out var s)
                ? s?.ToString() ?? "" : "";
            if (valid && Lifecycle.TerminalStates.Contains(state))
                continue;
            results.Add(await AdvanceRequest(
                item["path"]?.ToString() ?? "",
                ledger: ledger, sourceDatabase: sourceDatabase,
                successorVersion: successorVersion, gate: gate,
                autoExecute: autoExecute));
        }
        return results;
    }
}
