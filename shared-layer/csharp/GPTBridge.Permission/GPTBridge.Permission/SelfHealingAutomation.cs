namespace GPTBridge.Permission;

/// <summary>Health-check outcomes — the four checks of the retired
/// ``SelfHealingManager._health_check``.</summary>
public enum HealingIssue
{
    DirectoryAccess,
    GovernanceConnection,
    SovereignState,
    DirectoryPermissions,
}

/// <summary>
/// Native successor of the retired
/// ``permission_automation_healing.SelfHealingManager``.
///
/// Each <see cref="RunOnceAsync"/> tick runs the four checks
/// (registry accessibility, governance connection, permission-
/// sovereign state, directory-permission authority version) and fires
/// the registered repair delegate for every failed check; any issue
/// marks the component degraded until a clean tick restores it.
///
/// Repair effects are injected delegates — the retired module called
/// private snapshot reloads and ``sovereign.re_certify``; in the native
/// core the host registers governed repairs instead, preserving the
/// A297 decide-vs-execute boundary (this component detects, the host
/// acts).
/// </summary>
public sealed class SelfHealingAutomation
{
    public static readonly TimeSpan DefaultInterval =
        TimeSpan.FromSeconds(300);

    private readonly string _projectRoot;
    private readonly Func<bool>? _governanceConnected;
    private readonly Func<bool>? _sovereignHealthy;
    private readonly
        Dictionary<HealingIssue, Func<CancellationToken, Task>>
        _repairs = new();
    private bool _degraded;

    /// <param name="governanceConnected">Replacement for the retired
    /// governance-connection check (sovereign started + governance ref
    /// resolvable).</param>
    /// <param name="sovereignHealthy">Replacement for the retired
    /// sovereign-state check
    /// (``sovereign_id == "permission-sovereign"``).</param>
    public SelfHealingAutomation(
        string projectRoot,
        Func<bool>? governanceConnected = null,
        Func<bool>? sovereignHealthy = null)
    {
        _projectRoot = projectRoot;
        _governanceConnected = governanceConnected;
        _sovereignHealthy = sovereignHealthy;
    }

    /// <summary>Register the governed repair action for an issue —
    /// replaces the retired ad-hoc reloads.</summary>
    public void RegisterRepair(
        HealingIssue issue, Func<CancellationToken, Task> repair)
        => _repairs[issue] = repair;

    /// <summary>Issues detected on the latest tick.</summary>
    public List<HealingIssue> LatestIssues { get; private set; } =
        new();

    /// <summary>Single health-check tick — returns detected issues.</summary>
    public async Task<IReadOnlyList<HealingIssue>> RunOnceAsync(
        CancellationToken ct = default)
    {
        var issues = new List<HealingIssue>();

        if (!CheckDirectoryAccess())
            issues.Add(HealingIssue.DirectoryAccess);
        if (!(_governanceConnected?.Invoke() ?? false))
            issues.Add(HealingIssue.GovernanceConnection);
        if (!(_sovereignHealthy?.Invoke() ?? false))
            issues.Add(HealingIssue.SovereignState);
        if (!CheckDirectoryPermissions())
            issues.Add(HealingIssue.DirectoryPermissions);

        foreach (var issue in issues)
        {
            if (_repairs.TryGetValue(issue, out var repair))
            {
                try { await repair(ct); }
                catch (OperationCanceledException) { throw; }
                catch { /* repair failure logged upstream; stays degraded */ }
            }
        }
        _degraded = issues.Count > 0;
        LatestIssues = issues;
        return issues;
    }

    /// <summary>Managed registries all loadable.</summary>
    public bool CheckDirectoryAccess() =>
        RegistrySnapshot.LoadAll(_projectRoot).Complete;

    /// <summary>Authority current_version resolvable and non-null —
    /// the retired directory-permissions check.</summary>
    public bool CheckDirectoryPermissions()
    {
        var snap = RegistrySnapshot.LoadAll(_projectRoot);
        return snap.DirectoryAuthority is not null
            && snap.AuthorityCurrentVersion is not null;
    }

    /// <summary>True while the latest tick found issues.</summary>
    public bool IsDegraded() => _degraded;
}
