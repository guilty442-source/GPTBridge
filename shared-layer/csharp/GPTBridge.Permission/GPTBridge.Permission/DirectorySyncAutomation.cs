using System.Security.Cryptography;
using System.Text;

namespace GPTBridge.Permission;

/// <summary>
/// Native successor of the retired
/// ``permission_automation_directory.DirectorySyncManager``.
///
/// Computes the sync hash over the managed directory snapshots
/// (sha256 of code-rules initial version + authority current version +
/// sealed identity count) and records hash changes as the sync event.
/// Directory/identity-group child identities are retired (A592/A604) —
/// the hash update itself is the sync record; there is no child to
/// notify.
///
/// Driven externally by AutomationCore via <see cref="RunOnceAsync"/>;
/// the retired private-loop fallback is not reproduced — scheduling
/// belongs to the automation flow registry.
/// </summary>
public sealed class DirectorySyncAutomation
{
    /// <summary>Default tick interval recorded for the automation flow
    /// registration (5 minutes, same as the retired manager).</summary>
    public static readonly TimeSpan DefaultInterval =
        TimeSpan.FromSeconds(300);

    private readonly string _projectRoot;
    private string? _lastSyncHash;

    public DirectorySyncAutomation(string projectRoot)
    {
        _projectRoot = projectRoot;
    }

    /// <summary>Single directory sync tick — detects drift vs the last
    /// recorded hash.</summary>
    /// <returns>True when the directory hash changed (or was first
    /// observed) on this tick.</returns>
    public Task<bool> RunOnceAsync()
    {
        var snap = RegistrySnapshot.LoadAll(_projectRoot);
        var data =
            $"{snap.InitialCodeVersion}" +
            $"{snap.AuthorityCurrentVersion}" +
            $"{snap.IdentityGroupIds.Count}";
        var hash = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(data)))
            .ToLowerInvariant();

        if (_lastSyncHash is null)
        {
            _lastSyncHash = hash;
            return Task.FromResult(false);
        }
        if (hash != _lastSyncHash)
        {
            _lastSyncHash = hash;
            return Task.FromResult(true);
        }
        return Task.FromResult(false);
    }

    /// <summary>Current sync state (last hash + registered interval).</summary>
    public IReadOnlyDictionary<string, object?> GetSyncStatus() =>
        new Dictionary<string, object?>
        {
            ["last_sync_hash"] = _lastSyncHash,
            ["sync_interval"] = DefaultInterval.TotalSeconds,
        };
}
