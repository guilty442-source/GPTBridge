namespace GPTBridge.CodexPipeline;

/// <summary>
/// Five-sovereign audit runner for staged Codex amendment requests
/// (G69) — port of codex_amendment_audit_runner.py.
///
/// Executable adapter between a lifecycle-locked request and the
/// ``CodexAmendmentAuditGate``: supplies no audit evidence itself, each
/// sovereign must provide an independent check callable.  Never
/// fabricates receipts, never writes the Codex, never seals, signs or
/// publishes.  A missing callable, an unsuccessful audit, an
/// unrecordable audit ledger or an unavailable lifecycle record is a
/// denial.
/// </summary>
internal class AuditRunnerError : Exception
{
    public string Code { get; }

    public AuditRunnerError(string code, string detail = "")
        : base(detail.Length > 0 ? $"{code}:{detail}" : code)
        => Code = code;
}

internal sealed record AuditRun(
    bool Ok, string RequestId, string State,
    SovereignAudit.AuditResult? Result, string Error = "")
{
    public Dictionary<string, object?> AsDict()
    {
        var record = Result?.ToRecord()
            ?? new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["amendment_id"] = RequestId,
                ["ok"] = false,
                ["reason"] = Error.Length > 0
                    ? Error : "AUDIT_RUN_DENIED",
            };
        record["runner"] = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["request_id"] = RequestId,
            ["state"] = State,
            ["error"] = Error,
        };
        return record;
    }
}

internal static class AuditRunner
{
    /// <summary>Run the existing five-sovereign gate for one built
    /// successor request.</summary>
    public static async Task<AuditRun> RunFiveSovereignAudit(
        string requestPath,
        IReadOnlyDictionary<string,
            Func<Dictionary<string, object?>>> checks,
        CodexAmendmentRequestLedger ledger,
        SovereignAudit.Gate? gate = null,
        Dictionary<string, object?>? predecessor = null,
        string? requester = null)
    {
        var requestId = "";
        try
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            requestId = request.RequestId;
            var record = ledger.LoadRecord(requestId)
                ?? throw new AuditRunnerError("REQUEST_RECORD_REQUIRED");
            var recordHash = record.TryGetValue("request_hash",
                out var rh) ? rh?.ToString() ?? "" : "";
            if (recordHash != request.RequestHash)
                throw new AuditRunnerError("REQUEST_HASH_MISMATCH");
            var state = record.TryGetValue("state", out var s)
                ? s?.ToString() ?? "" : "";
            if (state != Lifecycle.StateSuccessorBuilt)
                throw new AuditRunnerError("SUCCESSOR_NOT_BUILT",
                    state.Length > 0 ? state : "missing");
            if (checks.Count == 0)
                throw new AuditRunnerError("SOVEREIGN_CHECKS_REQUIRED");
            var auditGate = gate ?? new SovereignAudit.Gate();
            ledger.Transition(requestId, Lifecycle.StateAuditing);
            var result = await auditGate.AuditAsync(requestId, checks,
                predecessor: predecessor ?? request.Predecessor,
                requester: requester ?? "");
            if (!result.Ok)
            {
                ledger.Transition(requestId, Lifecycle.StateRejected,
                    new Dictionary<string, object?>
                    {
                        ["audit_reason"] = result.Reason,
                        ["failed"] = result.Failed
                            .Cast<object?>().ToList(),
                        ["missing"] = result.Missing
                            .Cast<object?>().ToList(),
                    });
                return new AuditRun(false, requestId,
                    Lifecycle.StateRejected, result, result.Reason);
            }
            ledger.Transition(requestId, Lifecycle.StateAuditPassed,
                new Dictionary<string, object?>
                {
                    ["audit_reason"] = result.Reason,
                    ["certificate_hash"] =
                        result.Certificate?.TryGetValue(
                            "certificate_hash", out var ch) == true
                            ? ch?.ToString() ?? "" : "",
                });
            ledger.Transition(requestId,
                Lifecycle.StateReadyForGovernor,
                new Dictionary<string, object?>
                {
                    ["division_plan_released"] =
                        result.DivisionPlan is not null,
                });
            return new AuditRun(true, requestId,
                Lifecycle.StateReadyForGovernor, result);
        }
        catch (Exception error) when (error is AmendmentLifecycleError
            or AuditRunnerError)
        {
            if (requestId.Length > 0)
            {
                try
                {
                    var record = ledger.LoadRecord(requestId);
                    var state = record?.TryGetValue("state", out var s)
                        == true ? s?.ToString() ?? "" : "";
                    if (record is not null
                        && !Lifecycle.TerminalStates.Contains(state))
                        ledger.Transition(requestId,
                            Lifecycle.StateRejected,
                            new Dictionary<string, object?>
                            { ["error"] = error.Message });
                }
                catch (AmendmentLifecycleError) { }
            }
            return new AuditRun(false, requestId,
                Lifecycle.StateRejected, null, error.Message);
        }
    }
}
