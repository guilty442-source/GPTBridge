using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Conflict-safe worktree synchronization — parity with
/// git_tiers.workspace_sync.synchronize:
/// commit → merge queue → precheck → contract check → recovery ref →
/// merge → audit → checkpoint → fast-forward → optional governed push →
/// convergence evidence.
/// </summary>
internal static class Sync
{
    public const string Actor = "governance/workspace-sync";

    internal sealed record Worktree(string Path, string Head, string Branch);

    public static List<Worktree> ListWorktrees(string root)
    {
        var result = Git.Run(root,
            new[] { "worktree", "list", "--porcelain" });
        var worktrees = new List<Worktree>();
        string? path = null;
        var head = "";
        var branch = "";
        void Flush()
        {
            if (path is not null)
                worktrees.Add(new Worktree(
                    Path.GetFullPath(path), head, branch));
            path = null;
            head = branch = "";
        }
        if (result.Code != 0)
            return worktrees;
        foreach (var line in result.Stdout.Split('\n'))
        {
            if (line.StartsWith("worktree "))
            {
                Flush();
                path = line["worktree ".Length..].Trim();
            }
            else if (line.StartsWith("HEAD "))
                head = line["HEAD ".Length..].Trim();
            else if (line.StartsWith("branch "))
                branch = line["branch ".Length..].Trim();
            else if (line.StartsWith("detached"))
                branch = "HEAD";
        }
        Flush();
        return worktrees;
    }

    private static string CreateRecoveryRef(
        string projectRoot, string worktree, string queueId)
    {
        var head = Git.RevParse(worktree, "refs/heads/main");
        if (head.Length == 0)
            return "";
        var stamp = DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ");
        var safeId = string.Concat(queueId.Select(
            c => char.IsLetterOrDigit(c) || c is '-' or '_' ? c : '_'));
        var reference = $"refs/gptbridge/recovery/{stamp}/{safeId}";
        var gate = TierGate.Execute(projectRoot, worktree,
            new[] { "update-ref", reference, head }, Actor);
        return gate.Allowed && gate.Result!.Code == 0 ? reference : "";
    }

    private static bool AuditPasses(string projectRoot, string mainPath)
    {
        var result = AuditGate.Run(mainPath);
        if (result.Status is "fail" or "timeout")
        {
            Console.Error.WriteLine(
                $"[workspace-sync] integrated-main audit failed: " +
                result.Summary());
            return false;
        }
        if (result.Status == "delegated")
        {
            // Python oracle is retired — delegated is unverifiable.
            Console.Error.WriteLine(
                $"[workspace-sync] audit engine delegated with no " +
                $"coverage: {result.Note}");
            return false;
        }
        return true;
    }

    private static void RecordAuditedHead(string mainPath, string head)
    {
        var path = Path.Combine(mainPath, "main-system", "runtime",
            "state", "workspace-sync-audit.json");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var payload = new JsonObject
            {
                ["head"] = head,
                ["audited_at"] = Canon.EpochSeconds(),
                ["actor"] = Actor,
            };
            using var document = JsonDocument.Parse(payload.ToJsonString());
            Canon.WriteJsonAtomic(
                path, Canon.Indented(document.RootElement) + "\n");
        }
        catch (IOException) { /* evidence write failure never fails sync */ }
        catch (UnauthorizedAccessException) { }
    }

    private static string? AuditedHead(string mainPath)
    {
        var path = Path.Combine(mainPath, "main-system", "runtime",
            "state", "workspace-sync-audit.json");
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(path));
            return node?["head"]?.GetValue<string>();
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
    }

    public static string Synchronize(
        string projectRoot, bool commitDirty = true, bool push = false)
        => Synchronize(projectRoot, commitDirty, push,
            withSql: true, sqlOutcome: out _);

    /// <summary>Full workspace sync with the SQL half integrated: when
    /// <paramref name="withSql"/> holds, SQL-derived git projections are
    /// refreshed on measured version drift BEFORE the commit sweep, so
    /// the merge→audit→fast-forward flow certifies the current
    /// generation. The refresh never blocks git work: failures report
    /// into <paramref name="sqlOutcome"/> and retry next cycle.</summary>
    public static string Synchronize(
        string projectRoot, bool commitDirty, bool push, bool withSql,
        out JsonObject? sqlOutcome)
    {
        sqlOutcome = null;
        var blocked = GovManifest.WriteBlockReason(
            projectRoot, "workspace-sync.synchronize");
        if (blocked is not null)
            return $"error:{blocked}";
        var worktrees = ListWorktrees(projectRoot);
        var main = worktrees.FirstOrDefault(
            w => BranchPolicy.IsMain(w.Branch));
        if (main is null)
            return "error:main-worktree-not-found";
        var commonDir = Git.CommonDir(projectRoot);
        var lockPath = Path.Combine(
            commonDir, "gptbridge-workspace-sync.lock");
        try
        {
            using var coordinator = ProcessFileLock.Acquire(lockPath);
            return SynchronizeLocked(
                projectRoot, worktrees, main, commonDir,
                commitDirty, push, withSql, out sqlOutcome);
        }
        catch (LockBusyException)
        {
            throw;
        }
    }

    private static string SynchronizeLocked(
        string projectRoot, List<Worktree> worktrees, Worktree main,
        string commonDir, bool commitDirty, bool push, bool withSql,
        out JsonObject? sqlOutcome)
    {
        sqlOutcome = null;
        if (withSql)
        {
            // SQL half first: refresh stale projections BEFORE the commit
            // sweep, so the sweep commits them and audit certifies the
            // current generation. Refresh failures report and retry next
            // cycle — they never block git work.
            try
            {
                var sql = SqlSync.RefreshIfStale(projectRoot);
                sqlOutcome = sql;
                if (sql["error"] is not null)
                    Console.Error.WriteLine(
                        "[workspace-sync] sql refresh deferred: " +
                        sql["error"]?.GetValue<string>());
            }
            catch (Exception error)
            {
                Console.Error.WriteLine(
                    "[workspace-sync] sql refresh deferred: " +
                    $"{error.GetType().Name}: {error.Message}");
            }
        }
        List<string> dirty = new();
        for (var attempt = 0; attempt < 5; attempt++)
        {
            if (commitDirty)
            {
                foreach (var item in worktrees)
                {
                    var status = SelfCommit.RunOnce(
                        projectRoot, item.Path, actor: Actor);
                    if (status.StartsWith("error:")
                        || status == "in-progress")
                        return $"error:self-commit:{item.Path}:{status}";
                }
            }
            worktrees = ListWorktrees(projectRoot);
            var probeDeadline =
                DateTime.UtcNow.AddSeconds(30);
            while (true)
            {
                dirty = worktrees
                    .Where(w => Status.Capture(w.Path).Entries.Count > 0)
                    .Select(w => w.Path).ToList();
                if (dirty.Count == 0)
                    break;
                if (attempt == 4 || DateTime.UtcNow >= probeDeadline)
                    break;
                Thread.Sleep(3000);
            }
            if (dirty.Count == 0)
                break;
        }
        if (dirty.Count > 0)
            return "error:dirty-worktree:" + string.Join('|', dirty);

        var invalidDiffs = worktrees
            .Where(w => BranchPolicy.IsWorker(w.Branch))
            .Select(w => BranchPolicy.Normalize(w.Branch))
            .Where(branch => Git.Run(projectRoot,
                new[] { "diff", "--check", $"main...{branch}" }).Code != 0)
            .ToList();
        if (invalidDiffs.Count > 0)
            return "error:worker-diff-check:" + string.Join('|', invalidDiffs);

        var queue = new MergeQueue(projectRoot, commonDir);
        var mergedAny = false;
        foreach (var item in worktrees)
        {
            var branch = BranchPolicy.Normalize(item.Branch);
            if (!BranchPolicy.IsWorker(item.Branch))
                continue;
            var ancestor = Git.Run(main.Path,
                new[] { "merge-base", "--is-ancestor", branch, "main" });
            if (ancestor.Code == 0)
                continue;
            var workerId = Path.GetFileName(
                item.Path.TrimEnd(Path.DirectorySeparatorChar));
            var sourceSha = Git.RevParse(main.Path, branch);
            var baseSha = Git.RevParse(main.Path, "main");
            var existing = queue.FindEntry(branch, sourceSha);
            if (existing?["status"]?.GetValue<string>()
                    is "conflicted" or "blocked" or "failed")
                return $"conflict:{branch}:retry-blocked";
            var entry = existing ?? queue.Enqueue(
                workerId, branch, sourceSha,
                baseMainCommit: baseSha, escalatedBy: Actor);
            if (entry["status"]?.GetValue<string>() == "error")
                return $"error:merge-queue:" +
                       $"{entry["detail"]?.GetValue<string>() ?? "enqueue-failed"}";
            var queueId = entry["queue_id"]!.GetValue<string>();
            queue.MarkRunning(queueId);

            var mergeBase = Git.Run(main.Path,
                new[] { "merge-base", "main", sourceSha });
            if (mergeBase.Code != 0
                || string.IsNullOrWhiteSpace(mergeBase.Stdout))
            {
                queue.MarkBlocked(queueId, "no-common-ancestor");
                return $"conflict:{branch}:no-merge-base";
            }
            var whitespace = Git.Run(main.Path,
                new[] { "diff", "--check", $"main...{sourceSha}" });
            if (whitespace.Code != 0)
            {
                queue.MarkConflicted(queueId, "diff-check-failed");
                return $"conflict:{branch}:diff-check";
            }
            var contract = ContractCheck.Check(
                projectRoot, main.Path, sourceSha, "main");
            if (contract["ok"]?.GetValue<bool>() == false)
            {
                var rules = ((JsonArray)contract["violations"]!)
                    .OfType<JsonObject>()
                    .Select(v => v["rule"]?.GetValue<string>() ?? "?");
                queue.MarkBlocked(queueId,
                    "contract-check:" + string.Join(';', rules));
                return $"error:{branch}:contract-check";
            }
            var recoveryRef = CreateRecoveryRef(
                projectRoot, main.Path, queueId);
            var merged = TierGate.Execute(projectRoot, main.Path,
                new[] { "merge", "--no-edit", sourceSha }, Actor);
            if (!merged.Allowed || merged.Result!.Code != 0)
            {
                TierGate.Execute(projectRoot, main.Path,
                    new[] { "merge", "--abort" }, Actor);
                queue.MarkConflicted(queueId,
                    $"merge-failed; recovery={recoveryRef}");
                return $"conflict:{branch}";
            }
            mergedAny = true;
            queue.MarkMerged(queueId,
                $"merged {sourceSha} into main; recovery={recoveryRef}");
        }

        if (mergedAny)
        {
            try { ProtectedAttrs.Restore(main.Path); }
            catch (Exception) { }
        }
        var headNow = Git.RevParse(main.Path, "main");
        if (mergedAny || headNow != AuditedHead(main.Path))
        {
            if (!AuditPasses(projectRoot, main.Path))
                return "error:integrated-main-governance-audit";
            RecordAuditedHead(main.Path, headNow);
        }
        if (mergedAny)
            ReleaseCheckpoint.Record(projectRoot, main.Path,
                auditResult: "pass", actor: Actor);

        foreach (var item in worktrees)
        {
            if (!BranchPolicy.IsWorker(item.Branch))
                continue;
            var branch = BranchPolicy.Normalize(item.Branch);
            var advanced = TierGate.Execute(projectRoot, item.Path,
                new[] { "merge", "--ff-only", "main" }, Actor);
            if (!advanced.Allowed || advanced.Result!.Code != 0)
            {
                var detail = !advanced.Allowed
                    ? advanced.Detail
                    : (advanced.Result!.Stderr.Length > 0
                        ? advanced.Result!.Stderr
                        : advanced.Result!.Stdout).Trim();
                return $"error:fast-forward:{branch}:" +
                       detail[..Math.Min(160, detail.Length)];
            }
            try { ProtectedAttrs.Restore(item.Path); }
            catch (Exception) { }
        }

        if (push)
        {
            var pushResult = PushGate.Execute(
                projectRoot, main.Path);
            if (pushResult is not null)
                return pushResult;
        }
        try
        {
            PushGate.RecordConvergence(projectRoot, pushed: push);
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(
                $"[workspace-sync] convergence evidence failed: {error.Message}");
        }
        return push ? "synchronized-and-pushed" : "synchronized";
    }
}
