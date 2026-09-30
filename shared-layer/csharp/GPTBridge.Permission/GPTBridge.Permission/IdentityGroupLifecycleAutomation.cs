using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Permission;

/// <summary>A deletion proposal handed to the permission sovereign —
/// the A297 decide-vs-execute seam for group removal.</summary>
public sealed class GroupDeletionProposal
{
    public required string GroupId { get; init; }
    public required string Actor { get; init; }
    public required string ToolId { get; init; }
    public required string Reason { get; init; }
}

/// <summary>
/// Identity-group registration/deletion automation — extends the
/// retired ``IdentityGroupManager`` coordination duty into the
/// automated lifecycle the permission automation orchestrator exposed
/// through ``register_identity_group`` and the reconciliation status.
///
/// Each <see cref="RunOnceAsync"/> tick:
///  1. auto-registers sealed-directory identity groups that are not
///     yet locally registered (coordinated-layer bookkeeping — the
///     sealed registry is hard-coded immutable, nothing writes it),
///  2. proposes deletion for active local groups absent from the
///     sealed directory whose bound tool is retired or gone — the
///     injected sovereign decider approves, a governed deactivation
///     follows; no decider means fail-closed pending, never a silent
///     delete (A297),
///  3. resolves duplicate-actor conflicts automatically
///     (keep-newest), and
///  4. appends every mutation to the metadata-only lifecycle ledger.
/// </summary>
public sealed class IdentityGroupLifecycleAutomation
{
    public static readonly TimeSpan DefaultInterval =
        TimeSpan.FromSeconds(300);

    private readonly IdentityGroupAutomation _groups;
    private readonly string _projectRoot;
    private readonly Func<string, string?> _toolStatus;
    private readonly Func<GroupDeletionProposal, bool>? _decider;
    private readonly string _ledgerPath;
    private readonly object _ledgerLock = new();
    private int _sequence = -1;

    /// <summary>The coordinated group registry this automation drives.</summary>
    public IdentityGroupAutomation Groups => _groups;

    /// <param name="projectRoot">Managed-registry root.</param>
    /// <param name="toolStatus">tool_id → inventory status
    /// (``"active"``/``"retired"``/null when absent).</param>
    /// <param name="decider">Sovereign deletion decision — true
    /// approves; absent or false means fail-closed pending.</param>
    /// <param name="groups">Existing coordinated registry to drive
    /// (a fresh one is created when omitted).</param>
    /// <param name="ledgerPath">Metadata-only lifecycle ledger;
    /// defaults to runtime/state/identity-group-lifecycle.jsonl.</param>
    public IdentityGroupLifecycleAutomation(
        string projectRoot,
        Func<string, string?>? toolStatus = null,
        Func<GroupDeletionProposal, bool>? decider = null,
        IdentityGroupAutomation? groups = null,
        string? ledgerPath = null)
    {
        _projectRoot = projectRoot;
        _groups = groups ?? new IdentityGroupAutomation(projectRoot);
        _toolStatus = toolStatus ?? (_ => null);
        _decider = decider;
        _ledgerPath = ledgerPath ?? Path.Combine(
            projectRoot, "runtime", "state",
            "identity-group-lifecycle.jsonl");
    }

    /// <summary>Pending deletion proposals awaiting sovereign
    /// adjudication (refusals and no-decider fallbacks).</summary>
    public List<GroupDeletionProposal> PendingDeletions { get; } = new();

    /// <summary>Single automation tick.</summary>
    public Task<IReadOnlyDictionary<string, object?>> RunOnceAsync(
        CancellationToken ct = default)
    {
        var registered = new List<string>();
        var deactivated = new List<string>();
        PendingDeletions.Clear();

        var reconcile = _groups.ReconcileWithDirectory();
        var report = new Dictionary<string, object?>
        {
            ["reconciled"] = reconcile["ok"],
        };

        if ((bool)reconcile["ok"]!)
        {
            // Auto-register sealed groups missing locally.
            foreach (var groupId in
                (List<string>)reconcile["missing_locally"]!)
            {
                // ``IDENTITY_GROUP_*`` keys are scalar name constants;
                // the actor/bound tool live in the ``*_IDENTITY``
                // record whose ``group_id.$ref`` names the constant.
                var sealedKw =
                    RegistrySnapshot.LoadAll(_projectRoot)
                        .IdentityRecordFor(groupId);
                var actor =
                    sealedKw?["actor"]?.GetValue<string>() ?? groupId;
                var toolId =
                    sealedKw?["bound_tool_id"]?.GetValue<string>()
                    ?? actor;
                if (_groups.RegisterGroup(
                        groupId, actor,
                        Array.Empty<string>(),
                        toolId: toolId))
                {
                    registered.Add(groupId);
                    AppendLedger("register", groupId, actor);
                }
            }

            // Propose deletion for unregistered groups bound to
            // retired/absent tools; keep groups whose tool stays
            // active (drift-flagged, not deleted).
            foreach (var groupId in
                (List<string>)reconcile["unregistered_in_directory"]!)
            {
                var rec = _groups.GetGroupStatus(groupId);
                if (rec is null || !rec.Active) continue;
                var status = _toolStatus(rec.ToolId);
                if (status == "active") continue;
                var proposal = new GroupDeletionProposal
                {
                    GroupId = groupId,
                    Actor = rec.Actor,
                    ToolId = rec.ToolId,
                    Reason = status is null
                        ? "not-in-directory+tool-absent"
                        : $"not-in-directory+tool-{status}",
                };
                if (_decider?.Invoke(proposal) == true
                    && _groups.UnregisterGroup(groupId))
                {
                    deactivated.Add(groupId);
                    AppendLedger(
                        "delete", groupId, rec.Actor, proposal.Reason);
                }
                else
                {
                    PendingDeletions.Add(proposal);
                }
            }
        }

        var resolved = _groups.ResolveConflicts();
        foreach (var r in resolved)
            AppendLedger("conflict-deactivate",
                (string)r["group_id"], "");

        report["registered"] = registered;
        report["deactivated"] = deactivated;
        report["pending_deletions"] = PendingDeletions
            .Select(p => p.GroupId).ToList();
        report["conflicts_resolved"] = resolved.Count;
        return Task.FromResult(
            (IReadOnlyDictionary<string, object?>)report);
    }

    /// <summary>Registration entry point — the native successor of the
    /// orchestrator's ``register_identity_group`` proxy. Validates the
    /// request then delegates to the coordinated registry.</summary>
    public bool RegisterGroup(
        string groupId,
        string actor,
        IReadOnlyList<string> capabilities,
        string toolId,
        IReadOnlyDictionary<string, object?>? metadata = null)
    {
        if (_groups.RegisterGroup(
                groupId, actor, capabilities, toolId, metadata))
        {
            AppendLedger("register", groupId, actor);
            return true;
        }
        return false;
    }

    /// <summary>Explicit deletion request — routed through the same
    /// sovereign decider as automated proposals.</summary>
    public bool DeleteGroup(string groupId, string reason = "manual")
    {
        var rec = _groups.GetGroupStatus(groupId);
        if (rec is null || !rec.Active) return false;
        var proposal = new GroupDeletionProposal
        {
            GroupId = groupId, Actor = rec.Actor,
            ToolId = rec.ToolId, Reason = reason,
        };
        if (_decider?.Invoke(proposal) != true)
        {
            PendingDeletions.Add(proposal);
            return false;
        }
        if (!_groups.UnregisterGroup(groupId)) return false;
        AppendLedger("delete", groupId, rec.Actor, reason);
        return true;
    }

    private void AppendLedger(
        string action, string groupId, string actor, string? detail = null)
    {
        var record = new JsonObject
        {
            ["timestamp"] = DateTimeOffset.UtcNow
                .ToString("yyyy-MM-dd'T'HH:mm:ss.fffffffzzz"),
            ["action"] = action,
            ["group_id"] = groupId,
            ["actor"] = actor,
        };
        if (detail is not null) record["detail"] = detail;
        var line = record.ToJsonString(new JsonSerializerOptions
        {
            WriteIndented = false,
        });
        lock (_ledgerLock)
        {
            if (_sequence < 0)
                _sequence = File.Exists(_ledgerPath)
                    ? File.ReadLines(_ledgerPath).Count()
                    : 0;
            record["sequence"] = _sequence;
            _sequence++;
            Directory.CreateDirectory(
                Path.GetDirectoryName(_ledgerPath)!);
            File.AppendAllText(_ledgerPath, line + "\n");
        }
    }
}
