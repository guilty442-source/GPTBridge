using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Governed git hooks — parity with the retired Python hook scripts.
/// The same exe serves pre-commit, pre-merge-commit, pre-push and
/// pre-receive; hooks call it through thin sh shims.
/// </summary>
internal static class Hooks
{
    private static string Actor()
    {
        var actor = Environment.GetEnvironmentVariable("GIT_AUTHOR_NAME")
                    ?? Environment.GetEnvironmentVariable("USERNAME")
                    ?? "unknown";
        return actor.Length > 0 ? actor : "unknown";
    }

    private static bool Zero(string oid) =>
        oid.Length > 0 && oid.Trim('0').Length == 0;

    /// <summary>pre-commit / pre-merge-commit gate.</summary>
    public static int Commit(string projectRoot, string worktree)
    {
        var actor = Actor();
        var check = Git.Run(worktree, new[] { "diff", "--cached", "--check" });
        if (check.Code != 0)
        {
            Ledger.Audit(projectRoot, 2, "commit", actor, false,
                "pre-commit staged patch check failed",
                operation: "commit", worktree: worktree);
            Console.Error.Write(check.Stdout);
            Console.Error.Write(check.Stderr);
            return check.Code;
        }
        var native = AuditGate.Run(worktree);
        if (native.Status is "fail" or "timeout")
        {
            Ledger.Audit(projectRoot, 2, "commit", actor, false,
                $"native audit engine gate failed ({native.Summary()})",
                operation: "commit", worktree: worktree);
            foreach (var error in native.Errors)
                Console.Error.WriteLine($"[FAIL] {error}");
            return 1;
        }
        if (native.Status == "delegated")
        {
            // The Python oracle fallback is retired: an engine that
            // cannot run produces unverifiable evidence — fail closed.
            Ledger.Audit(projectRoot, 2, "commit", actor, false,
                $"native audit engine unavailable ({native.Note})",
                operation: "commit", worktree: worktree);
            Console.Error.WriteLine(
                $"[FAIL] native audit engine unavailable: {native.Note}");
            return 1;
        }
        Ledger.Audit(projectRoot, 2, "commit", actor, true,
            $"pre-commit governance audit passed ({native.Summary()})",
            operation: "commit", worktree: worktree);
        return 0;
    }

    /// <summary>pre-push gate: delete / tag-rewrite / non-fast-forward
    /// need GOVERNANCE_AUTHORITY_APPROVAL.</summary>
    public static int Push(string projectRoot, string worktree,
                           string? remoteArg = null)
    {
        var actor = Actor();
        var remote = remoteArg
            ?? Environment.GetCommandLineArgs().SkipWhile(
                a => a != "--remote").Skip(1).FirstOrDefault()
            ?? "unknown";
        var highRisk = new List<string>();
        string? line;
        while ((line = Console.In.ReadLine()) is not null)
        {
            var fields = line.Split(' ',
                StringSplitOptions.RemoveEmptyEntries);
            if (fields.Length != 4)
            {
                Ledger.Audit(projectRoot, 3, $"push {remote}", actor, false,
                    "invalid pre-push input", operation: "push",
                    worktree: worktree);
                return 1;
            }
            var (localRef, localOid, remoteRef, remoteOid) =
                (fields[0], fields[1], fields[2], fields[3]);
            if (Zero(localOid))
            {
                highRisk.Add($"delete:{remoteRef}");
            }
            else if (!Zero(remoteOid)
                     && (remoteRef.StartsWith("refs/tags/")
                         || Git.Run(worktree, new[]
                             {
                                 "merge-base", "--is-ancestor",
                                 remoteOid, localOid,
                             }).Code != 0))
            {
                highRisk.Add($"rewrite:{remoteRef}");
            }
        }
        var command = $"push {remote}";
        if (highRisk.Count == 0)
        {
            Ledger.Audit(projectRoot, 2, command, actor, true,
                "pre-push explicit invocation confirmed",
                operation: "push", worktree: worktree);
            return 0;
        }
        var approved = Environment.GetEnvironmentVariable(
            "GOVERNANCE_AUTHORITY_APPROVAL")?.ToLowerInvariant()
            is "1" or "true" or "yes";
        Ledger.Audit(projectRoot, 3, command, actor, approved,
            string.Join(',', highRisk), operation: "push",
            worktree: worktree);
        if (approved)
            return 0;
        Console.Error.WriteLine(
            "[pre-push] BLOCKED: ref deletion, tag rewrite, or " +
            "non-fast-forward push requires " +
            "GOVERNANCE_AUTHORITY_APPROVAL=1");
        return 1;
    }

    /// <summary>pre-receive gate: protected refs, deletion, non-ff;
    /// main accepts fast-forward only.</summary>
    public static int Receive(string projectRoot, string worktree)
    {
        var actor = Actor();
        var highRisk = new List<string>();
        var mainViolations = new List<string>();
        var updates = new List<string>();
        string? line;
        while ((line = Console.In.ReadLine()) is not null)
        {
            var fields = line.Split(' ',
                StringSplitOptions.RemoveEmptyEntries);
            if (fields.Length != 3)
            {
                Ledger.ChainedAudit(projectRoot, 3, "receive", actor, false,
                    "invalid pre-receive input", operation: "receive",
                    worktree: worktree);
                return 1;
            }
            var (oldOid, newOid, reference) =
                (fields[0], fields[1], fields[2]);
            updates.Add(reference);
            var deleting = Zero(newOid);
            var creating = Zero(oldOid);
            if (reference.StartsWith("refs/heads/"))
            {
                var branch = BranchPolicy.Normalize(reference);
                if (deleting)
                {
                    highRisk.Add(BranchPolicy.IsProtected(branch)
                        ? $"protected-branch-delete:{reference}"
                        : $"delete:{reference}");
                }
                else if (!creating
                         && Git.Run(worktree, new[]
                             {
                                 "merge-base", "--is-ancestor",
                                 oldOid, newOid,
                             }).Code != 0)
                {
                    if (BranchPolicy.IsMain(branch))
                        mainViolations.Add(
                            $"non-fast-forward:{reference}");
                    else
                        highRisk.Add($"rewrite:{reference}");
                }
            }
            else if (reference.StartsWith("refs/tags/"))
            {
                if (deleting || !creating)
                {
                    highRisk.Add(BranchPolicy.IsProtectedTag(reference)
                        ? $"protected-tag-rewrite:{reference}"
                        : $"tag-rewrite:{reference}");
                }
            }
            else if (deleting)
            {
                highRisk.Add($"delete:{reference}");
            }
        }
        var approved = Environment.GetEnvironmentVariable(
            "GOVERNANCE_AUTHORITY_APPROVAL")?.ToLowerInvariant()
            is "1" or "true" or "yes";
        if (mainViolations.Count > 0)
        {
            Ledger.ChainedAudit(projectRoot, 3, "receive", actor, false,
                string.Join(',', mainViolations), operation: "receive",
                worktree: worktree, result: "denied");
            Console.Error.WriteLine(
                "[pre-receive] BLOCKED: main accepts fast-forward only");
            return 1;
        }
        if (highRisk.Count > 0 && !approved)
        {
            Ledger.ChainedAudit(projectRoot, 3, "receive", actor, false,
                string.Join(',', highRisk), operation: "receive",
                worktree: worktree, result: "denied");
            Console.Error.WriteLine(
                "[pre-receive] BLOCKED: deletion or history rewrite " +
                "requires governance approval");
            return 1;
        }
        Ledger.ChainedAudit(projectRoot, highRisk.Count > 0 ? 3 : 2,
            "receive", actor, true,
            string.Join(',', highRisk.Count > 0 ? highRisk : updates),
            operation: "receive", worktree: worktree,
            result: "authorized");
        return 0;
    }

    /// <summary>
    /// Install governed hook shims into the shared hooks directory.
    /// Each shim declares HOOK_VERSION (upgrade/verify contract) and
    /// execs this executable — identical layout to the retired Python
    /// wrappers.
    /// </summary>
    public static int Install(string projectRoot, string exePath)
    {
        var hooksDir = ResolveHooksDir(projectRoot);
        Directory.CreateDirectory(hooksDir);
        var forward = exePath.Replace('\\', '/');
        var failures = 0;
        foreach (var hook in new[]
                 { "pre-commit", "pre-merge-commit", "pre-push",
                   "pre-receive" })
        {
            var content = Shim(hook, forward);
            var path = Path.Combine(hooksDir, hook);
            try
            {
                var tmp = path + "." + Environment.ProcessId + ".tmp";
                File.WriteAllText(tmp, content);
                File.Move(tmp, path, overwrite: true);
                Console.WriteLine($"[hooks] installed {hook}");
            }
            catch (IOException error)
            {
                Console.Error.WriteLine(
                    $"[hooks] install failed {hook}: {error.Message}");
                failures++;
            }
        }
        return failures;
    }

    /// <summary>Update the governed templates to the exe-shim form so
    /// verify_governed_templates and hook_generation track the same
    /// contract.</summary>
    public static int UpdateTemplates(string projectRoot, string exePath)
    {
        var templateDir = Path.Combine(projectRoot, "governance_rule",
            "git-hooks");
        var forward = exePath.Replace('\\', '/');
        var failures = 0;
        foreach (var hook in new[]
                 { "pre-commit", "pre-merge-commit", "pre-push",
                   "pre-receive" })
        {
            var content = Shim(hook, forward);
            try
            {
                File.WriteAllText(
                    Path.Combine(templateDir, hook), content);
                Console.WriteLine($"[hooks] template updated {hook}");
            }
            catch (IOException error)
            {
                Console.Error.WriteLine(
                    $"[hooks] template failed {hook}: {error.Message}");
                failures++;
            }
        }
        return failures;
    }

    /// <summary>Hook shim — pre-push carries the governed-marker
    /// comment required by the manifest file-contains checks.</summary>
    private static string Shim(string hook, string forward)
    {
        var contract = hook switch
        {
            "pre-push" =>
                "# Governance contract (enforced by " +
                "GPTBridge.GitAutomation.exe):\n" +
                "# blocks force-push / ref deletion / " +
                "non-fast-forward unless\n" +
                "# GOVERNANCE_AUTHORITY_APPROVAL=1; tag pushes " +
                "verified via merge-base\n" +
                "# ancestry and refs/tags/ rules.\n",
            "pre-receive" =>
                "# Governance contract (enforced by " +
                "GPTBridge.GitAutomation.exe):\n" +
                "# protected refs, deletions and history rewrites " +
                "require governance\n" +
                "# approval; refs/heads/main accepts fast-forward " +
                "updates only.\n",
            _ =>
                "# Governance contract (enforced by " +
                "GPTBridge.GitAutomation.exe):\n" +
                "# staged patch check plus the native audit gate.\n",
        };
        return "#!/bin/sh\n" +
               "HOOK_VERSION=1\n" +
               contract +
               $"exec \"{forward}\" --hook {hook} \"$@\"\n";
    }

    private static string ResolveHooksDir(string root)
    {
        var overrideDir =
            Environment.GetEnvironmentVariable("GPTBRIDGE_HOOKS_DIR");
        if (!string.IsNullOrWhiteSpace(overrideDir))
            return overrideDir.Trim();
        var gitEntry = Path.Combine(root, ".git");
        if (File.Exists(gitEntry))
        {
            var line = File.ReadAllText(gitEntry).Trim();
            if (line.StartsWith("gitdir:",
                    StringComparison.OrdinalIgnoreCase))
            {
                var dir = line["gitdir:".Length..].Trim();
                if (!Path.IsPathRooted(dir))
                    dir = Path.GetFullPath(Path.Combine(root, dir));
                return Path.Combine(dir, "hooks");
            }
        }
        return Path.Combine(gitEntry, "hooks");
    }
}
