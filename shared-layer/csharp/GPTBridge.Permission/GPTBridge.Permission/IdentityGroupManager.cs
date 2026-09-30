namespace GPTBridge.Permission;

/// <summary>A local identity-group registration (coordinated-layer
/// record — never silently authoritative, A317).</summary>
public sealed class IdentityGroupRecord
{
    public required string GroupId { get; init; }
    public required string Actor { get; init; }
    public required IReadOnlyList<string> Capabilities { get; init; }
    public required string ToolId { get; init; }
    public IReadOnlyDictionary<string, object?> Metadata { get; init; } =
        new Dictionary<string, object?>();
    public required DateTimeOffset RegisteredAt { get; init; }
    public bool Active { get; internal set; } = true;
    public DateTimeOffset? UnregisteredAt { get; internal set; }

    /// <summary>Present in the sealed identity registry — groups absent
    /// from it are coordinated-only and flag directory drift (A317).</summary>
    public required bool DirectoryRegistered { get; init; }
}

/// <summary>
/// Native successor of the retired
/// ``permission_automation_identity.IdentityGroupManager``.
///
/// Keeps the coordinated-layer group registry (register/unregister,
/// duplicate-actor conflict detection and keep-newest resolution) and
/// reconciles it against the sealed identity-group directory.
/// Reconciliation produces a drift report only — it never writes to
/// either side; decisions belong to the permission sovereign.
/// </summary>
public sealed class IdentityGroupAutomation
{
    private readonly string _projectRoot;
    private readonly Dictionary<string, IdentityGroupRecord> _registry =
        new();

    public IdentityGroupAutomation(string projectRoot)
    {
        _projectRoot = projectRoot;
    }

    private HashSet<string> SealedDirectoryIds()
    {
        var snap = RegistrySnapshot.LoadAll(_projectRoot);
        if (snap.IdentityGroups is null)
            return new HashSet<string>();
        return new HashSet<string>(snap.IdentityGroupIds);
    }

    /// <summary>Register a new identity group — refused on missing
    /// id/actor or id already registered.</summary>
    public bool RegisterGroup(
        string groupId,
        string actor,
        IReadOnlyList<string> capabilities,
        string toolId,
        IReadOnlyDictionary<string, object?>? metadata = null)
    {
        if (string.IsNullOrEmpty(groupId) || string.IsNullOrEmpty(actor)
            || _registry.ContainsKey(groupId))
            return false;
        _registry[groupId] = new IdentityGroupRecord
        {
            GroupId = groupId,
            Actor = actor,
            Capabilities = capabilities,
            ToolId = toolId,
            Metadata = metadata ?? new Dictionary<string, object?>(),
            RegisteredAt = DateTimeOffset.UtcNow,
            DirectoryRegistered = SealedDirectoryIds().Contains(groupId),
        };
        return true;
    }

    /// <summary>Drift report vs the sealed directory — directory ids
    /// absent locally (missing_locally) and local groups absent in the
    /// directory (unregistered_in_directory). Fail-closed error record
    /// when the directory is unavailable.</summary>
    public IReadOnlyDictionary<string, object?> ReconcileWithDirectory()
    {
        if (RegistrySnapshot.LoadAll(_projectRoot).IdentityGroups is null)
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = "directory-unavailable",
            };
        var registered = SealedDirectoryIds();
        var local = _registry.Values
            .Where(r => r.Active)
            .Select(r => r.GroupId)
            .ToHashSet();
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["missing_locally"] =
                registered.Except(local).OrderBy(s => s).ToList(),
            ["unregistered_in_directory"] =
                local.Except(registered).OrderBy(s => s).ToList(),
            ["local_active"] = local.Count,
            ["directory_registered"] = registered.Count,
        };
    }

    /// <summary>Deactivate a registered group.</summary>
    public bool UnregisterGroup(string groupId)
    {
        if (!_registry.TryGetValue(groupId, out var rec))
            return false;
        rec.Active = false;
        rec.UnregisteredAt = DateTimeOffset.UtcNow;
        return true;
    }

    /// <summary>Duplicate-actor conflicts across active groups.</summary>
    public IReadOnlyList<IReadOnlyDictionary<string, object>>
        DetectConflicts() =>
        _registry.Values
            .Where(r => r.Active)
            .GroupBy(r => r.Actor)
            .Where(g => g.Count() > 1)
            .Select(g => (IReadOnlyDictionary<string, object>)
                new Dictionary<string, object>
                {
                    ["actor"] = g.Key,
                    ["groups"] = g.Select(r => r.GroupId).ToList(),
                    ["type"] = "duplicate_actor",
                })
            .ToList();

    /// <summary>Resolve conflicts by keeping the newest registration —
    /// returns the deactivation records.</summary>
    public IReadOnlyList<IReadOnlyDictionary<string, object>>
        ResolveConflicts()
    {
        var resolved = new List<IReadOnlyDictionary<string, object>>();
        foreach (var conflict in DetectConflicts())
        {
            var groups = ((IEnumerable<string>)conflict["groups"])
                .OrderByDescending(g => _registry[g].RegisteredAt)
                .Skip(1);
            foreach (var groupId in groups)
            {
                UnregisterGroup(groupId);
                resolved.Add(new Dictionary<string, object>
                {
                    ["group_id"] = groupId,
                    ["action"] = "deactivated",
                });
            }
        }
        return resolved;
    }

    /// <summary>Record lookup.</summary>
    public IdentityGroupRecord? GetGroupStatus(string groupId) =>
        _registry.TryGetValue(groupId, out var r) ? r : null;

    /// <summary>All active group records.</summary>
    public IReadOnlyList<IdentityGroupRecord> ListActiveGroups() =>
        _registry.Values.Where(r => r.Active).ToList();
}
