using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal static partial class PushGate
{
    /// <summary>The push branch of synchronize(): fetch → ancestor →
    /// mandatory test gate → push → evidence. Returns the failure string
    /// or null on success.</summary>
    public static string? Execute(string projectRoot, string mainPath)
    {
        var fetched = TierGate.Execute(projectRoot, mainPath,
            new[] { "fetch", "origin", "main" }, Sync.Actor);
        if (!fetched.Allowed || fetched.Result!.Code != 0)
            return "error:fetch-origin-main";
        var remoteIsAncestor = Git.Run(mainPath, new[]
        {
            "merge-base", "--is-ancestor", "origin/main", "main",
        });
        if (remoteIsAncestor.Code != 0)
            return "error:remote-main-diverged";
        var testGate = MandatoryTestGate(projectRoot);
        if (testGate["passed"]?.GetValue<bool>() != true)
        {
            RecordPushEvidence(projectRoot, testGate, pushed: false,
                detail: testGate["detail"]?.GetValue<string>() ?? "");
            return "error:push-test-gate";
        }
        var pushed = TierGate.Execute(projectRoot, mainPath,
            new[] { "push", "origin", "main" }, Sync.Actor);
        if (!pushed.Allowed || pushed.Result!.Code != 0)
        {
            var detail = !pushed.Allowed
                ? pushed.Detail
                : (pushed.Result!.Stderr.Length > 0
                    ? pushed.Result!.Stderr
                    : pushed.Result!.Stdout).Trim();
            RecordPushEvidence(projectRoot, testGate, pushed: false,
                detail: detail[..Math.Min(160, detail.Length)]);
            return $"error:push:{detail[..Math.Min(160, detail.Length)]}";
        }
        RecordPushEvidence(projectRoot, testGate, pushed: true);
        return null;
    }

    private static (int? Ahead, int? Behind) AheadBehind(string worktree)
    {
        var ahead = Git.Run(worktree,
            new[] { "rev-list", "--count", "origin/main..main" });
        var behind = Git.Run(worktree,
            new[] { "rev-list", "--count", "main..origin/main" });
        return (
            ahead.Code == 0 && int.TryParse(ahead.Stdout.Trim(), out var a)
                ? a : null,
            behind.Code == 0 && int.TryParse(behind.Stdout.Trim(), out var b)
                ? b : null);
    }

    private static JsonObject SyncState(string worktree)
    {
        var local = Git.RevParse(worktree, "refs/heads/main");
        var origin = Git.RevParse(worktree, "refs/remotes/origin/main");
        string Pairwise()
        {
            if (local.Length == 0 || origin.Length == 0)
                return "missing";
            if (local == origin)
                return "equal";
            if (Git.Run(worktree, new[]
                    { "merge-base", "--is-ancestor", origin, local })
                    .Code == 0)
                return "ahead";
            if (Git.Run(worktree, new[]
                    { "merge-base", "--is-ancestor", local, origin })
                    .Code == 0)
                return "behind";
            return "diverged";
        }
        var relation = Pairwise();
        var state = (local.Length == 0 || origin.Length == 0)
            ? "MISSING_REF"
            : relation switch
            {
                "equal" => "IN_SYNC",
                "ahead" => "LOCAL_AHEAD",
                "behind" => "ORIGIN_AHEAD",
                _ => "DIVERGED",
            };
        return new JsonObject
        {
            ["state"] = state,
            ["local_main_sha"] = local,
            ["origin_main_sha"] = origin,
        };
    }

    /// <summary>record_convergence_evidence parity (§10.69-F①/D④).</summary>
    public static void RecordConvergence(
        string projectRoot, bool pushed = false)
    {
        var mainPath = Sync.ListWorktrees(projectRoot)
            .FirstOrDefault(w => BranchPolicy.IsMain(w.Branch))?.Path
            ?? projectRoot;
        var state = SyncState(mainPath);
        var (ahead, behind) = AheadBehind(mainPath);
        var inSync = state["state"]!.GetValue<string>() == "IN_SYNC"
                     && ahead == 0;
        var path = StatePath(projectRoot);
        var prior = ReadState(path);
        var streak = prior["convergence"]?["consecutive_in_sync"]
            ?.GetValue<int>() ?? 0;
        streak = inSync ? streak + 1 : 0;
        var now = Canon.UtcNow();
        var convergence = new JsonObject
        {
            ["state"] = state["state"]!.DeepClone(),
            ["ahead"] = ahead.HasValue ? ahead.Value : null,
            ["behind"] = behind.HasValue ? behind.Value : null,
            ["ahead_zero"] = inSync,
            ["consecutive_in_sync"] = streak,
            ["last_checked"] = now,
            ["local_main_sha"] = state["local_main_sha"]!.DeepClone(),
            ["origin_main_sha"] = state["origin_main_sha"]!.DeepClone(),
        };
        var record = ReadState(path);
        record["schema"] = "gptbridge-push-gate/v1";
        record["updated_at"] = now;
        record["convergence"] = convergence;
        WriteState(path, record);
        Ledger.Audit(projectRoot, 1, "sync-state origin/main", Sync.Actor,
            true,
            $"sync_state={state["state"]} ahead={ahead} behind={behind} " +
            $"streak={streak} pushed={pushed}",
            operation: "convergence-check", worktree: mainPath,
            phase: "result",
            result: inSync ? "in-sync"
                : state["state"]!.GetValue<string>().ToLowerInvariant(),
            returncode: inSync ? 0 : 1);
    }

    /// <summary>record_push_evidence parity (C④/F④).</summary>
    public static void RecordPushEvidence(
        string projectRoot, JsonObject? testGate, bool pushed,
        string detail = "")
    {
        var mainPath = Sync.ListWorktrees(projectRoot)
            .FirstOrDefault(w => BranchPolicy.IsMain(w.Branch))?.Path
            ?? projectRoot;
        var localSha = Git.RevParse(mainPath, "main");
        var originSha = Git.RevParse(mainPath, "origin/main");
        var gateSummary = "none";
        if (testGate is not null)
        {
            var totals = testGate["totals"];
            gateSummary =
                $"tests={(testGate["passed"]?.GetValue<bool>() == true ? "pass" : "fail")}" +
                $" skipped={testGate["skipped"]?.GetValue<bool>() ?? false}" +
                $" suites={(testGate["suites"] as JsonArray)?.Count ?? 0}" +
                $" pass={totals?["pass"]?.GetValue<int>() ?? 0}" +
                $" fail={totals?["fail"]?.GetValue<int>() ?? 0}" +
                $" blocked={totals?["blocked"]?.GetValue<int>() ?? 0}" +
                $" rebuilt={testGate["rebuilt"]?.GetValue<bool>() ?? false}" +
                $" ms={testGate["duration_ms"]?.GetValue<int>()}";
        }
        var now = Canon.UtcNow();
        var path = StatePath(projectRoot);
        var record = ReadState(path);
        record["schema"] = "gptbridge-push-gate/v1";
        record["updated_at"] = now;
        record["last_test_gate"] = testGate?.DeepClone();
        record["last_push"] = new JsonObject
        {
            ["at"] = now,
            ["result"] = pushed ? "pushed" : "denied",
            ["local_main_sha"] = localSha,
            ["origin_main_sha"] = originSha,
            ["test_gate"] = gateSummary,
            ["detail"] = detail[..Math.Min(300, detail.Length)],
        };
        WriteState(path, record);
        Ledger.Audit(projectRoot, 2, "push origin main", Sync.Actor,
            pushed,
            $"mandatory-test-gate[{gateSummary}] {detail}".Trim()
                [..Math.Min(500,
                    $"mandatory-test-gate[{gateSummary}] {detail}".Trim().Length)],
            operation: "push", worktree: mainPath, phase: "result",
            result: pushed ? "pushed" : "denied",
            returncode: pushed ? 0 : 1);
    }
}
