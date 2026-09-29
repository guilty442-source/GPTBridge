namespace GPTBridge.Permission;

/// <summary>
/// Automated permission lifecycle sweep — direct port of
/// core_system/permission_automation_lifecycle.py (A436/A10/A11/A22).
///
/// Duties carried over:
///   1. grant expiry handling and delegated auto-renewal,
///   2. pre-expiry warning state (EXPIRING),
///   3. periodic cleanup of revoked/expired grants older than 30 days.
///
/// The sweep is externally driven via <see cref="RunOnceAsync"/> — the
/// automation core owns cadence (§1.1); this type never spawns its own
/// loop.  Renewal is a permission transaction delegated to the
/// sovereign's permission.renew adjudication; a refused or faulting
/// adjudication fails closed to EXPIRED.
/// </summary>
public sealed class PermissionLifecycleAutomation
{
    public static readonly TimeSpan DefaultTtl = TimeSpan.FromDays(30);
    public static readonly TimeSpan DefaultWarning = TimeSpan.FromDays(7);
    public static readonly TimeSpan TerminalRetention = TimeSpan.FromDays(30);
    public const int DefaultMaxRenewals = 10;

    private readonly Dictionary<string, PermissionGrant> _grants = new(
        StringComparer.Ordinal);
    private readonly object _lock = new();
    private readonly Func<DateTimeOffset> _clock;
    private readonly Func<PermissionGrant, CancellationToken, Task<bool>>?
        _renewAdjudicator;

    public TimeSpan CheckInterval { get; } = TimeSpan.FromSeconds(3600);
    public TimeSpan ExpiryWarning { get; }
    public bool AutoRenewEnabled { get; }
    public int MaxRenewals { get; }

    /// <param name="renewAdjudicator">
    /// Delegates renewal to the permission sovereign's permission.renew
    /// adjudication.  Returns true when the renewal was accepted.
    /// Null = renewal adjudication unavailable → every expiry fails
    /// closed to EXPIRED.
    /// </param>
    public PermissionLifecycleAutomation(
        Func<PermissionGrant, CancellationToken, Task<bool>>?
            renewAdjudicator = null,
        TimeSpan? expiryWarning = null,
        bool autoRenewEnabled = true,
        int maxRenewals = DefaultMaxRenewals,
        Func<DateTimeOffset>? clock = null)
    {
        _renewAdjudicator = renewAdjudicator;
        ExpiryWarning = expiryWarning ?? DefaultWarning;
        AutoRenewEnabled = autoRenewEnabled;
        MaxRenewals = maxRenewals;
        _clock = clock ?? (() => DateTimeOffset.UtcNow);
    }

    /// <summary>Register a new governed permission grant.</summary>
    public PermissionGrant RegisterGrant(
        string grantId, string actor, string capability, string action,
        string target, string? dataScope = null, TimeSpan? ttl = null,
        bool autoRenew = true)
    {
        var now = _clock();
        var grant = new PermissionGrant
        {
            GrantId = grantId,
            Actor = actor,
            Capability = capability,
            Action = action,
            Target = target,
            DataScope = dataScope,
            IssuedAt = now,
            ExpiresAt = now + (ttl ?? DefaultTtl),
            AutoRenew = autoRenew,
        };
        lock (_lock)
            _grants[grantId] = grant;
        return grant;
    }

    public PermissionGrant? GetGrant(string grantId)
    {
        lock (_lock)
            return _grants.TryGetValue(grantId, out var g) ? g : null;
    }

    /// <summary>Revoke a grant （收權）.  False when the id is unknown.</summary>
    public bool RevokeGrant(string grantId, string reason = "")
    {
        lock (_lock)
        {
            if (!_grants.TryGetValue(grantId, out var grant))
                return false;
            grant.State = PermissionGrantState.Revoked;
            grant.RevokedAt = _clock();
            grant.RevokedReason = reason;
            return true;
        }
    }

    /// <summary>Suspend a grant （停權）.  False when the id is unknown.</summary>
    public bool SuspendGrant(string grantId, string reason = "")
    {
        lock (_lock)
        {
            if (!_grants.TryGetValue(grantId, out var grant))
                return false;
            grant.State = PermissionGrantState.Suspended;
            return true;
        }
    }

    /// <summary>
    /// A10 automatic-stop: map a stop trigger to the lifecycle operation
    /// permission-core must apply.  Fail-closed ordering — destructive
    /// triggers map to the strongest operation.
    /// </summary>
    public static LifecycleOperation MapStopTrigger(
        PermissionStopTrigger trigger) => trigger switch
    {
        PermissionStopTrigger.Expiry => LifecycleOperation.Terminate,
        PermissionStopTrigger.IdentityOrRoleRevocation =>
            LifecycleOperation.Revoke,
        PermissionStopTrigger.ScopeLoss => LifecycleOperation.Restrict,
        PermissionStopTrigger.GenerationRaise => LifecycleOperation.Suspend,
        PermissionStopTrigger.SecurityEvent => LifecycleOperation.Revoke,
        PermissionStopTrigger.ContractInvalidation =>
            LifecycleOperation.Suspend,
        PermissionStopTrigger.ControllingBasisRetirement =>
            LifecycleOperation.Terminate,
        _ => LifecycleOperation.Terminate,
    };

    /// <summary>
    /// One grant-check sweep — driven by the automation core
    /// (permission-automation-lifecycle flow, interval 3600s).
    /// </summary>
    public async Task RunOnceAsync(CancellationToken ct = default)
    {
        var now = _clock();
        List<PermissionGrant> snapshot;
        lock (_lock)
            snapshot = _grants.Values.ToList();

        foreach (var grant in snapshot)
        {
            if (grant.State == PermissionGrantState.Active
                && grant.IsExpired(now))
            {
                if (AutoRenewEnabled && grant.AutoRenew
                    && grant.RenewalCount < MaxRenewals
                    && _renewAdjudicator is not null)
                    await RenewGrantAsync(grant, ct);
                else
                    grant.State = PermissionGrantState.Expired;
            }
            else if (grant.State == PermissionGrantState.Active
                     && grant.IsExpiringSoon(now, ExpiryWarning))
            {
                grant.State = PermissionGrantState.Expiring;
            }

            grant.LastChecked = now;

            // Terminal cleanup: revoked grants age from revoked_at;
            // expired grants age from expires_at (they have no
            // revoked_at — expires_at is the terminal timestamp).
            if (grant.State is PermissionGrantState.Revoked
                    or PermissionGrantState.Expired)
            {
                var terminalAt = grant.RevokedAt ?? grant.ExpiresAt;
                if (terminalAt is { } t && now - t > TerminalRetention)
                    lock (_lock)
                        _grants.Remove(grant.GrantId);
            }
        }
    }

    /// <summary>
    /// Auto-renew — a permission transaction delegated to the sovereign
    /// adjudicator (A10/A11 gate).  Refusal or fault fails closed to
    /// EXPIRED; success extends expiry by the default TTL.
    /// </summary>
    private async Task RenewGrantAsync(
        PermissionGrant grant, CancellationToken ct)
    {
        var now = _clock();
        bool accepted;
        try
        {
            accepted = await _renewAdjudicator!(grant, ct);
        }
        catch
        {
            accepted = false;
        }
        if (!accepted)
        {
            grant.State = PermissionGrantState.Expired;
            return;
        }
        grant.ExpiresAt = now + DefaultTtl;
        grant.RenewedAt = now;
        grant.RenewalCount++;
        grant.State = PermissionGrantState.Active;
    }

    /// <summary>Aggregate statistics (parity with get_stats).</summary>
    public JsonStats GetStats()
    {
        lock (_lock)
        {
            var byState = new Dictionary<string, int>(
                StringComparer.Ordinal);
            foreach (var g in _grants.Values)
            {
                var key = g.State.ToString().ToLowerInvariant();
                byState[key] = byState.GetValueOrDefault(key) + 1;
            }
            return new JsonStats
            {
                TotalGrants = _grants.Count,
                ByState = byState,
                AutoRenewEnabled = AutoRenewEnabled,
                MaxRenewals = MaxRenewals,
            };
        }
    }

    public sealed record JsonStats
    {
        public required int TotalGrants { get; init; }
        public required Dictionary<string, int> ByState { get; init; }
        public required bool AutoRenewEnabled { get; init; }
        public required int MaxRenewals { get; init; }
    }
}
