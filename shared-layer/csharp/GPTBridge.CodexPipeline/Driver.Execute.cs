using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

internal static partial class Driver
{
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
                {
                    ["stage"] = "execute",
                    ["detail"] = error.ToString(),
                });
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["state"] = Lifecycle.StateRejected,
                ["error"] = "EXECUTE_ERROR:"
                    + $"{error.GetType().Name}:{error.Message}",
                ["detail"] = error.ToString(),
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

    /// <summary>Retire one intake file whose ledger record is already
    /// terminal: rename ``….json`` to ``….json.<state>`` so the intake
    /// glob stops rescanning it while the evidence stays on disk.  The
    /// ledger record keeps provenance in ``retired_request_path``.</summary>
    internal static string? RetireTerminalFile(string requestPath,
        CodexAmendmentRequestLedger ledger, string requestId,
        string state)
    {
        if (!Lifecycle.TerminalStates.Contains(state)
            || requestPath.Length == 0)
            return null;
        var retired = requestPath + "." + state;
        if (!File.Exists(requestPath) || File.Exists(retired))
            return null;
        try
        {
            File.Move(requestPath, retired);
        }
        catch (Exception error) when (error is IOException
            or UnauthorizedAccessException)
        {
            return null;
        }
        if (requestId.Length > 0)
            ledger.MarkFileRetired(requestId, retired);
        return retired;
    }

    /// <summary>A live request's ``supersedes`` (v2) or legacy
    /// ``resubmission_of`` withdraws the named request: a target that was
    /// dropped but never scanned is begun first so it still lands in the
    /// ledger, then every non-terminal record transitions to
    /// ``withdrawn``.  Its intake file is retired when the same pass
    /// reaches it.</summary>
    private static void WithdrawSuperseded(
        IReadOnlyList<Dictionary<string, object?>> scan,
        CodexAmendmentRequestLedger ledger)
    {
        var byId = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var item in scan)
        {
            if (item.TryGetValue("valid", out var v) && v is true
                && item.TryGetValue("request_id", out var rid))
            {
                var id = rid?.ToString() ?? "";
                if (id.Length > 0)
                    byId[id] = item["path"]?.ToString() ?? "";
            }
        }
        foreach (var item in scan)
        {
            if (!(item.TryGetValue("valid", out var v) && v is true))
                continue;
            var requestId = item["request_id"]?.ToString() ?? "";
            var state = item["state"]?.ToString() ?? "";
            if (Lifecycle.TerminalStates.Contains(state))
                continue;
            var target = (item.TryGetValue("supersedes", out var sp)
                    ? sp?.ToString() ?? "" : "").Trim();
            if (target.Length == 0)
                target = (item.TryGetValue("resubmission_of",
                        out var ro) ? ro?.ToString() ?? "" : "").Trim();
            if (target.Length == 0 || target == requestId)
                continue;
            var record = ledger.LoadRecord(target);
            if (record is null
                && byId.TryGetValue(target, out var targetPath)
                && targetPath.Length > 0)
            {
                try { ledger.Begin(targetPath); }
                catch (AmendmentLifecycleError) { }
                record = ledger.LoadRecord(target);
            }
            if (record is null)
                continue;
            var targetState = record.TryGetValue("state", out var ts)
                ? ts?.ToString() ?? "" : "";
            if (Lifecycle.TerminalStates.Contains(targetState))
                continue;
            try
            {
                ledger.Transition(target, Lifecycle.StateWithdrawn,
                    new Dictionary<string, object?>
                    { ["superseded_by"] = requestId });
            }
            catch (AmendmentLifecycleError) { }
        }
    }

    /// <summary>Advance every staged non-terminal request once —
    /// superseded requests are withdrawn first and every terminal
    /// intake file is retired out of the scan glob.</summary>
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
        var scan = ScanRequests(intakeDirs, ledger);
        WithdrawSuperseded(scan, ledger);
        foreach (var item in scan)
        {
            var path = item["path"]?.ToString() ?? "";
            var valid = item.TryGetValue("valid", out var v)
                && v is true;
            var state = item.TryGetValue("state", out var s)
                ? s?.ToString() ?? "" : "";
            if (valid && Lifecycle.TerminalStates.Contains(state))
            {
                RetireTerminalFile(path, ledger,
                    item["request_id"]?.ToString() ?? "", state);
                continue;
            }
            var result = await AdvanceRequest(path,
                ledger: ledger, sourceDatabase: sourceDatabase,
                successorVersion: successorVersion, gate: gate,
                autoExecute: autoExecute);
            results.Add(result);
            RetireTerminalFile(path, ledger,
                result.TryGetValue("request_id", out var rid)
                    ? rid?.ToString() ?? "" : "",
                result.TryGetValue("state", out var st)
                    ? st?.ToString() ?? "" : "");
        }
        return results;
    }
}
