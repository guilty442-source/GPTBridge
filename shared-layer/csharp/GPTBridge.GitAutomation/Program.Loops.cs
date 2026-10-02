using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal static partial class Program
{

    private static async Task<int> Watch(string root, Options options)
    {
        var service = new Service(root, options);
        return await service.Run();
    }

    // -- unified-host entries (GPTBridge.Automation in-process planes) --

    /// Resident sweep+sync service for the unified automation host:
    /// runs the same loop as ``--watch`` under the caller's process
    /// supervision.  Exit contract: 0 graceful (flow disabled /
    /// stop-file), 1 single-instance lock held — the caller defers and
    /// adopts the plane when the external holder releases.
    internal static Task<int> WatchService(string root)
    {
        var options = new Options { Mode = "watch" };
        return new Service(Path.GetFullPath(root), options).Run();
    }

    /// Unified-host ``--once`` plane: exactly one sweep+sync cycle,
    /// then exit.  Takes no instance lock (bounded one-shot parity).
    internal static Task<int> RunOnce(string root)
    {
        var options = new Options { Mode = "once" };
        return new Service(Path.GetFullPath(root), options).Run();
    }

    // -- sweep / sync primitives (shared with --once / --sweep / --sync) --

    private static JsonObject Sweep(
        string root, Options options,
        Dictionary<string, (string, double)> dirtySince,
        IReadOnlySet<string>? only = null)
    {
        var results = new JsonObject();
        var scopes = new JsonObject();
        var now = Environment.TickCount64 / 1000.0;
        var worktrees = Sync.ListWorktrees(root).Select(w => w.Path)
            .Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        if (!worktrees.Contains(root, StringComparer.OrdinalIgnoreCase))
            worktrees.Insert(0, Path.GetFullPath(root));
        // Event-scoped sweeps visit only the worktrees that signalled a
        // change, plus every worktree still holding a pending debounce
        // marker — a quiet worktree mid-debounce must not starve.
        if (only is not null)
            worktrees = worktrees.Where(w =>
                only.Contains(w) || dirtySince.ContainsKey(w)).ToList();
        foreach (var worktree in worktrees)
        {
            Status.Snapshot snapshot;
            try
            {
                snapshot = Status.CaptureSnapshot(worktree);
            }
            catch (Exception)
            {
                dirtySince.Remove(worktree);
                continue;
            }
            if (!snapshot.Dirty)
            {
                dirtySince.Remove(worktree);
                continue;
            }
            scopes[worktree] = new JsonArray(
                snapshot.AffectedScopes
                    .Select(s => (JsonNode?)JsonValue.Create(s)).ToArray());
            if (!dirtySince.TryGetValue(worktree, out var marker)
                || marker.Item1 != snapshot.Fingerprint)
            {
                dirtySince[worktree] = (snapshot.Fingerprint, now);
                results[worktree] = "debounce";
                continue;
            }
            if (now - marker.Item2 < options.Debounce)
            {
                results[worktree] = "debounce";
                continue;
            }
            var status = SelfCommit.RunOnce(
                root, worktree, snapshot: snapshot);
            results[worktree] = status;
            if (status is "committed" or "clean")
                dirtySince.Remove(worktree);
        }
        return new JsonObject
        {
            ["at"] = Canon.EpochSeconds(),
            ["results"] = results,
            ["affected_scope"] = scopes,
        };
    }

    private static (string Status, JsonObject? Sql) SyncCycle(
        string root, Options options, bool push = false)
    {
        var effectivePush = push || options.Push;
        try
        {
            var status = Sync.Synchronize(root,
                commitDirty: options.CommitDirty, push: effectivePush,
                withSql: options.SqlSync, sqlOutcome: out var sql);
            return (status, sql);
        }
        catch (LockBusyException)
        {
            return ("skipped:lock-busy", null);
        }
    }
}
