namespace GPTBridge.Permission;

/// <summary>
/// Permission grant lifecycle states — direct port of
/// core_system/permission_automation_types.py PermissionGrantState.
/// </summary>
public enum PermissionGrantState
{
    Active,
    Expiring,
    Expired,
    Revoked,
    Suspended,
    PendingRenewal,
}

/// <summary>
/// A10 automatic-stop trigger → mapped lifecycle operation.
/// permission-core automatically chooses SUSPEND|REVOKE|TERMINATE when a
/// stop trigger fires; the mapping is contract-fixed (A10).
/// </summary>
public enum PermissionStopTrigger
{
    Expiry,
    IdentityOrRoleRevocation,
    ScopeLoss,
    GenerationRaise,
    SecurityEvent,
    ContractInvalidation,
    ControllingBasisRetirement,
}

public enum LifecycleOperation
{
    Issue,
    Terminate,
    Renew,
    Restrict,
    Suspend,
    Revoke,
}

/// <summary>
/// One governed permission grant — port of PermissionGrant dataclass.
/// </summary>
public sealed class PermissionGrant
{
    public string GrantId { get; init; } = "";
    public string Actor { get; init; } = "";
    public string Capability { get; init; } = "";
    public string Action { get; init; } = "";
    public string Target { get; init; } = "";
    public string? DataScope { get; init; }
    public DateTimeOffset IssuedAt { get; init; }
    public DateTimeOffset? ExpiresAt { get; set; }
    public PermissionGrantState State { get; set; } = PermissionGrantState.Active;
    public DateTimeOffset? RenewedAt { get; set; }
    public DateTimeOffset? RevokedAt { get; set; }
    public string? RevokedReason { get; set; }
    public bool AutoRenew { get; init; } = true;
    public int RenewalCount { get; set; }
    public DateTimeOffset? LastChecked { get; set; }

    public bool IsExpired(DateTimeOffset now) =>
        ExpiresAt is { } exp && now >= exp;

    public bool IsExpiringSoon(DateTimeOffset now, TimeSpan warning) =>
        ExpiresAt is { } exp && now >= exp - warning;

    public TimeSpan? TimeUntilExpiry(DateTimeOffset now) =>
        ExpiresAt is { } exp ? exp - now : null;
}
