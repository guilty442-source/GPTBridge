using System.Text;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Per-worktree automatic self-commit — parity with
/// git_tiers.self_commit.run_once including every guard:
/// manifest write-allowed, coordinator yield, op-in-progress,
/// staged-index-present, commit-lease, identity, index.lock retry,
/// worktree lock, fail-safe unstage on commit failure.
/// </summary>
internal static class SelfCommit
{
    public const string Actor = "governance/self-commit";
    public const string SyncActor = "governance/workspace-sync";
    private const double LeaseTtlSeconds = 120.0;

    private static readonly string[] InProgressMarkers =
        { "MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD" };

    private static bool OperationInProgress(string gitDir) =>
        InProgressMarkers.Any(m => File.Exists(Path.Combine(gitDir, m)))
        || Directory.Exists(Path.Combine(gitDir, "sequencer"));

    private static bool StagedIndexPresent(string worktree) =>
        Git.Run(worktree, new[] { "diff", "--cached", "--quiet" }).Code == 1;

    public static string CommitMessage(
        string branch, Dictionary<string, string> entries)
    {
        var scopes = entries.Keys.Select(WorktreeStatus.ScopeOf)
            .Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal)
            .ToList();
        var scopeTag = scopes.Count > 0
            ? $" [{string.Join(", ", scopes.Take(4))}]" : "";
        var subject =
            $"auto-commit({branch}): {entries.Count} file(s) updated{scopeTag}";
        var body = new StringBuilder()
            .Append("\n\nAutomated self-commit by GPTBridge governance.\n");
        foreach (var pair in entries.OrderBy(
                     p => p.Key, StringComparer.Ordinal))
            body.Append($"- [{pair.Value}] {pair.Key}\n");
        body.Append(
            "\nGenerated with [GPTBridge](https://github.com/guilty442-source/GPTBridge)\n\n" +
            "Co-Authored-By: GPTBridge Self-Commit <governance@gptbridge.local>\n");
        return subject + body;
    }

    private static string WorktreeLockKey(string worktree) =>
        Canon.Sha256Hex(
            Path.GetFullPath(worktree).ToLowerInvariant())[..16];

    /// <summary>run_once parity — returns the Python status vocabulary.</summary>
    public static string RunOnce(
        string projectRoot, string worktree, string actor = Actor,
        Status.Snapshot? snapshot = null)
    {
        var blocked = GovManifest.WriteBlockReason(
            projectRoot, "self-commit.run_once");
        if (blocked is not null)
            return $"error:{blocked}";
        if (!Directory.Exists(worktree))
            return "error:not-a-directory";
        var dirs = Git.ResolveGitDirs(worktree);
        if (dirs is null)
            return "error:not-a-git-worktree";
        var (gitDir, commonDir) = dirs.Value;
        // Yield to the workspace coordinator (unless we ARE the coordinator).
        if (actor != SyncActor
            && ProcessFileLock.IsActive(Path.Combine(
                commonDir, "gptbridge-workspace-sync.lock")))
            return "in-progress";
        var lockPath = Path.Combine(
            commonDir, $"gptbridge-self-commit-{WorktreeLockKey(worktree)}.lock");
        try
        {
            using var _ = ProcessFileLock.Acquire(lockPath);
            return RunOnceUnlocked(projectRoot, worktree, gitDir, commonDir,
                                   actor, snapshot);
        }
        catch (LockBusyException)
        {
            return "in-progress";
        }
    }

    private static string RunOnceUnlocked(
        string projectRoot, string worktree, string gitDir, string commonDir,
        string actor, Status.Snapshot? snapshot)
    {
        if (OperationInProgress(gitDir))
            return "in-progress";
        if (StagedIndexPresent(worktree))
            return "staged-index-present";
        var lease = CommitLease.Active(gitDir);
        if (lease is not null && lease["owner"]?.GetValue<string>() != actor)
            return $"commit-lease-held:{lease["owner"]?.GetValue<string>()}";

        var entries = snapshot?.Entries ?? Status.Capture(worktree).LegacyMap();
        if (entries.Count == 0)
            return "clean";
        var identity = Git.Run(worktree, new[] { "config", "--get", "user.name" });
        if (string.IsNullOrWhiteSpace(identity.Stdout))
            return "error:missing-identity";
        var branch = Git.CurrentBranch(worktree);
        if (branch.Length == 0)
            branch = "HEAD";
        var message = CommitMessage(branch, entries);
        var msgFile = Path.Combine(gitDir, "self-commit-msg.txt");
        try
        {
            File.WriteAllText(msgFile, message, new UTF8Encoding(false));
        }
        catch (IOException error)
        {
            return $"error:write-msg:{error.Message}";
        }
        catch (UnauthorizedAccessException error)
        {
            return $"error:write-msg:{error.Message}";
        }

        var locked = false;
        try
        {
            if (CommitLease.Claim(gitDir, actor, LeaseTtlSeconds) is null)
            {
                var holder = CommitLease.Active(gitDir)?["owner"]
                    ?.GetValue<string>();
                return $"commit-lease-held:{holder}";
            }
            try { ProtectedAttrs.Restore(worktree); }
            catch (Exception) { }
            var isMainWorktree =
                Path.GetFullPath(gitDir).TrimEnd(Path.DirectorySeparatorChar)
                    .Equals(
                        Path.GetFullPath(commonDir)
                            .TrimEnd(Path.DirectorySeparatorChar),
                        StringComparison.OrdinalIgnoreCase);
            if (!isMainWorktree)
            {
                var lockGate = TierGate.Execute(projectRoot, worktree,
                    new[] { "worktree", "lock", worktree }, actor);
                if (!lockGate.Allowed || lockGate.Result!.Code != 0)
                {
                    var detail = !lockGate.Allowed
                        ? lockGate.Detail
                        : lockGate.Result!.Stderr.Trim()[..Math.Min(200, lockGate.Result!.Stderr.Trim().Length)];
                    return $"error:lock:{detail}";
                }
                locked = true;
            }
            var addGate = TierGate.Execute(projectRoot, worktree,
                new[] { "add", "-A" }, actor);
            for (var retry = 0; retry < 3; retry++)
            {
                if (!addGate.Allowed || addGate.Result!.Code == 0
                    || !addGate.Result!.Stderr.Contains("index.lock"))
                    break;
                Thread.Sleep(3000);
                addGate = TierGate.Execute(projectRoot, worktree,
                    new[] { "add", "-A" }, actor);
            }
            if (!addGate.Allowed || addGate.Result is null
                || addGate.Result.Code != 0)
            {
                string detail;
                if (!addGate.Allowed)
                {
                    detail = addGate.Detail;
                }
                else
                {
                    var stderr = addGate.Result!.Stderr.Trim();
                    var fatal = stderr.Split('\n')
                        .Where(l => l.TrimStart().StartsWith("fatal:")
                            || l.TrimStart().StartsWith("error:"))
                        .ToList();
                    detail = (fatal.Count > 0
                        ? string.Join("; ", fatal) : stderr);
                    detail = detail[..Math.Min(200, detail.Length)];
                }
                return $"error:add:{detail}";
            }
            if (Status.Capture(worktree).Clean)
                return "nothing-staged";
            var commitGate = TierGate.Execute(projectRoot, worktree,
                new[] { "commit", "-F", msgFile }, actor);
            if (!commitGate.Allowed || commitGate.Result is null
                || commitGate.Result.Code != 0)
            {
                var detail = !commitGate.Allowed
                    ? commitGate.Detail
                    : commitGate.Result!.Stderr.Trim();
                Ledger.ChainedAudit(projectRoot, 2, "auto-commit fail", actor,
                    true, detail[..Math.Min(500, detail.Length)],
                    operation: "auto-commit", worktree: worktree,
                    phase: "result", result: "failed",
                    returncode: commitGate.Result?.Code ?? -1);
                try
                {
                    TierGate.Execute(projectRoot, worktree,
                        new[] { "restore", "--staged", "." }, actor);
                }
                catch (Exception) { }
                return $"error:commit:{detail[..Math.Min(200, detail.Length)]}";
            }
        }
        finally
        {
            if (locked)
                TierGate.Execute(projectRoot, worktree,
                    new[] { "worktree", "unlock", worktree }, actor);
            try { File.Delete(msgFile); }
            catch (IOException) { }
            CommitLease.Release(gitDir, actor);
        }

        var head = Git.RevParse(worktree, "HEAD");
        Ledger.ChainedAudit(projectRoot, 2, "auto-commit", actor, true,
            $"committed {head} on {branch}: {entries.Count} file(s)",
            operation: "auto-commit", worktree: worktree,
            phase: "result", result: "succeeded");
        Console.Error.WriteLine(
            $"[self-commit] {worktree}: {head} on {branch} " +
            $"({entries.Count} file(s))");
        return "committed";
    }
}
