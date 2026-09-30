using System.Text.Json.Nodes;

namespace GPTBridge.Permission;

/// <summary>
/// Permission lifecycle adjudicator — direct port of
/// governance/sovereigns/permission/permission_lifecycle.py (A436/A10/A22).
///
/// The sovereign is decision-only (A297): it never executes lifecycle
/// mutations directly.  Each adjudication:
///   1. verifies the permission_id was previously issued via the
///      append-only ledger — fail-closed PERMISSION_NOT_ISSUED,
///   2. passes the A319 two-key review gate (a current xingcheng
///      permission-review finding is mandatory — injected delegate),
///   3. appends a new lifecycle entry to the ledger,
///   4. returns a decision outcome; execution is delegated to the
///      governed executor.
/// </summary>
public sealed class PermissionLifecycleAdjudicator
{
    private static readonly string[] DecisionBasis =
        { "A436", "A10", "A22" };

    private readonly PermissionGrantLedger _ledger;

    /// <summary>
    /// A319 two-key review gate: (capability, target, purpose,
    /// requester) → null when the review finding is current and the
    /// mutation may proceed; otherwise a refusal code string.
    /// </summary>
    private readonly Func<string, string, string, string,
        CancellationToken, Task<string?>> _reviewGate;

    public PermissionLifecycleAdjudicator(
        PermissionGrantLedger ledger,
        Func<string, string, string, string, CancellationToken,
            Task<string?>> reviewGate)
    {
        _ledger = ledger;
        _reviewGate = reviewGate;
    }

    public sealed record Outcome(
        bool Accepted, string? RefusalCode, JsonObject? Payload);

    private static Outcome Refuse(string code) =>
        new(false, code, null);

    private static Outcome Accept(string action, string permissionId) =>
        new(true, null, new JsonObject
        {
            ["action"] = action,
            ["permission_id"] = permissionId,
            ["execution"] = "delegated-to-governed-executor",
        });

    private async Task<Outcome> AdjudicateAsync(
        string operation, string permissionId, string requester,
        JsonObject? detail, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(permissionId))
            return Refuse("MISSING_PERMISSION_ID");
        if (!_ledger.WasIssued(permissionId))
            return Refuse("PERMISSION_NOT_ISSUED");
        if (operation == "terminate")
        {
            var status = _ledger.CurrentStatus(permissionId);
            if (status?["status"]?.GetValue<string>() == "terminated")
                return Refuse("PERMISSION_ALREADY_TERMINATED");
        }
        var refusal = await _reviewGate(
            $"permission.{operation}", permissionId, operation,
            requester, ct);
        if (refusal is not null)
            return Refuse(refusal);
        _ledger.RecordLifecycle(
            operation, permissionId, requester, DecisionBasis, detail);
        return Accept($"permission.{operation}", permissionId);
    }

    public Task<Outcome> TerminateAsync(
        string permissionId, string requester,
        CancellationToken ct = default) =>
        AdjudicateAsync("terminate", permissionId, requester, null, ct);

    public Task<Outcome> RenewAsync(
        string permissionId, string requester,
        CancellationToken ct = default) =>
        AdjudicateAsync("renew", permissionId, requester, null, ct);

    public Task<Outcome> RestrictAsync(
        string permissionId, string requester, JsonObject restrictions,
        CancellationToken ct = default) =>
        AdjudicateAsync(
            "restrict", permissionId, requester,
            new JsonObject { ["restrictions"] = restrictions.DeepClone() },
            ct);

    public Task<Outcome> SuspendAsync(
        string permissionId, string requester,
        CancellationToken ct = default) =>
        AdjudicateAsync("suspend", permissionId, requester, null, ct);

    public Task<Outcome> RevokeAsync(
        string permissionId, string requester,
        CancellationToken ct = default) =>
        AdjudicateAsync("revoke", permissionId, requester, null, ct);
}
