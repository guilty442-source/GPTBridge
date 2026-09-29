using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Git governance manifest verification — parity with
/// git_tiers.governance_manifest: fail-closed ACTIVE / READ_ONLY /
/// UPGRADE_REQUIRED plus governed timing reads.
/// </summary>
internal static class GovManifest
{
    public const string Filename = "git_governance_manifest.json";
    public const string DigestFilename = "git_governance_manifest.sha256";
    private const string DirEnv = "GPTBRIDGE_GIT_GOVERNANCE_MANIFEST_DIR";

    private static readonly string[] RequiredFields =
    {
        "manifest_id", "manifest_schema_version", "governance_version",
        "policy_version", "registry_schema_version", "audit_schema_version",
        "queue_schema_version", "worker_schema_version", "hook_version",
        "snapshot_schema_version", "recovery_schema_version",
        "control_plane_version", "minimum_git_version",
        "maximum_tested_git_version",
    };

    public static string ManifestDir(string projectRoot) =>
        Environment.GetEnvironmentVariable(DirEnv) is { Length: > 0 } env
            ? env.Trim()
            : Path.Combine(projectRoot, "governance_rule", "execution",
                           "git_tiers");

    public static string ManifestPath(string projectRoot) =>
        Path.Combine(ManifestDir(projectRoot), Filename);

    public sealed record Status(bool Ok, string Mode, string Reason,
                                JsonObject? Payload = null);

    private static int VersionMajor(JsonNode? node)
    {
        var text = node is JsonValue value
            ? value.ToString() : node?.ToJsonString() ?? "";
        var match = System.Text.RegularExpressions.Regex.Match(
            text, @"\d+");
        return match.Success ? int.Parse(match.Value) : 0;
    }

    public static Status Verify(string projectRoot)
    {
        var manifestPath = ManifestPath(projectRoot);
        var digestPath = Path.Combine(
            Path.GetDirectoryName(manifestPath)!, DigestFilename);
        if (!File.Exists(manifestPath))
            return new Status(false, "UPGRADE_REQUIRED",
                $"manifest-missing:{manifestPath}");
        JsonObject payload;
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(manifestPath));
            if (node is not JsonObject doc)
                return new Status(false, "READ_ONLY",
                    "manifest-not-object");
            payload = doc;
        }
        catch (IOException error)
        {
            return new Status(false, "READ_ONLY",
                $"manifest-read-error:{error.GetType().Name}");
        }
        catch (JsonException error)
        {
            return new Status(false, "READ_ONLY",
                $"manifest-invalid-json:{error.Message}");
        }
        foreach (var field in RequiredFields)
        {
            if (payload[field] is null)
                return new Status(false, "READ_ONLY",
                    $"manifest-format:missing-field:{field}");
        }
        var schemaMajor = VersionMajor(payload["manifest_schema_version"]);
        if (schemaMajor < 1)
            return new Status(false, "UPGRADE_REQUIRED",
                $"manifest-schema-too-old:{payload["manifest_schema_version"]}");
        if (schemaMajor > 1)
            return new Status(false, "READ_ONLY",
                $"manifest-schema-unsupported:{payload["manifest_schema_version"]}");
        var governanceMajor = VersionMajor(payload["governance_version"]);
        if (governanceMajor < 2)
            return new Status(false, "UPGRADE_REQUIRED",
                $"governance-version-too-old:{payload["governance_version"]}");
        if (governanceMajor > 2)
            return new Status(false, "READ_ONLY",
                $"governance-version-too-new:{payload["governance_version"]}");

        var embedded = payload["integrity"]?["digest"]?.GetValue<string>() ?? "";
        var sidecar = "";
        var sidecarPresent = File.Exists(digestPath);
        if (sidecarPresent)
        {
            try
            {
                var raw = File.ReadAllText(digestPath).Trim();
                sidecar = raw.Split((char[]?)null,
                    StringSplitOptions.RemoveEmptyEntries)
                    .FirstOrDefault() ?? "";
                if (sidecar.Length == 0)
                    return new Status(false, "READ_ONLY",
                        "manifest-digest-unreadable");
            }
            catch (IOException)
            {
                return new Status(false, "READ_ONLY",
                    "manifest-digest-unreadable");
            }
        }
        var candidates = new[] { sidecar, embedded }
            .Where(v => v.Length > 0).Select(v => v.ToLowerInvariant())
            .Distinct().ToList();
        if (candidates.Count == 0)
            return new Status(false, "READ_ONLY", "manifest-digest-missing");
        if (candidates.Count > 1)
            return new Status(false, "READ_ONLY", "manifest-digest-conflict");
        // Canonical digest: payload minus integrity + baseline bindings.
        var body = (JsonObject)payload.DeepClone();
        body.Remove("integrity");
        body.Remove("baseline");
        var actual = Canon.Sha256Hex(Canon.CompactNode(body));
        return actual != candidates[0]
            ? new Status(false, "READ_ONLY", "manifest-digest-mismatch")
            : new Status(true, "ACTIVE", "ok", payload);
    }

    /// <summary>assert_write_allowed — fail closed outside ACTIVE mode.</summary>
    public static string? WriteBlockReason(string projectRoot, string operation)
    {
        var status = Verify(projectRoot);
        if (status.Ok)
            return null;
        var reason = $"governance-{status.Mode.ToLowerInvariant()}:{status.Reason}";
        return string.IsNullOrEmpty(operation) ? reason : $"{operation}:{reason}";
    }

    public static double Timing(string projectRoot, string name, double fallback)
    {
        var status = Verify(projectRoot);
        if (!status.Ok || status.Payload?["timings"] is not JsonObject timings)
            return fallback;
        return timings[name]?.GetValue<double>() ?? fallback;
    }
}

/// <summary>automation-flows.json — git-automation flow tunables root.</summary>
internal static class FlowsConfig
{
    public static JsonObject? GitAutomationFlow(string projectRoot)
    {
        // Re-read per call: the kill switch must propagate to a resident
        // host within one sweep interval — caching would pin a stale
        // "enabled" decision for the process lifetime.
        try
        {
            var path = Path.Combine(projectRoot, "main-system", "config",
                                    "automation-flows.json");
            var node = JsonNode.Parse(File.ReadAllText(path));
            var flow = node?["flows"]?["git-automation"] as JsonObject;
            return flow?.Count > 0 ? flow : null;
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
    }

    /// <summary>
    /// Runtime kill-switch overrides written by AutomationCore
    /// (runtime/state/automation-flows-state.json → overrides[name]).
    /// Fail-open on unreadable state mirrors AutomationCore._load_overrides:
    /// a corrupt state file must not silently keep a flow killed.
    /// </summary>
    public static JsonObject? Override(string projectRoot)
    {
        try
        {
            var path = Path.Combine(projectRoot, "main-system", "runtime",
                                    "state", "automation-flows-state.json");
            var node = JsonNode.Parse(File.ReadAllText(path));
            return node?["overrides"]?["git-automation"] as JsonObject;
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
    }

    public static bool FlowEnabled(string projectRoot)
    {
        if (GitAutomationFlow(projectRoot)?["enabled"]
                ?.GetValue<bool>() == false)
            return false;
        if (Override(projectRoot)?["enabled"]?.GetValue<bool>() == false)
            return false;
        return true;
    }

    public static bool PushEnabled(string projectRoot) =>
        GitAutomationFlow(projectRoot)?["push"]?.GetValue<bool>() ?? false;

    public static JsonObject? PushGate(string projectRoot) =>
        GitAutomationFlow(projectRoot)?["push_gate"] as JsonObject;
}

/// <summary>
/// A53/E39 tier classification + the SYSTEM_SAFE automation allowlist —
/// the C# governed surface replacing capability_gate.execute_system_safe:
/// the host is itself the governed actor; Tier-3 is structurally refused,
/// Tier-2 is allowlisted, every execution is audited.
/// </summary>
internal static class TierGate
{
    private static readonly string[] Tier1 =
    {
        "--version", "status", "log", "diff", "show", "branch", "remote",
        "blame", "ls-files", "cat-file", "rev-parse", "describe", "tag -l",
        "for-each-ref", "stash list", "config --get", "config --list",
        "worktree list", "worktree list --porcelain", "worktree status",
        "worktree prune --dry-run", "reflog show", "fsck", "count-objects",
        "shortlog", "annotate", "name-rev", "rev-list", "ls-tree",
        "ls-remote", "merge-base",
    };

    private static readonly string[] Tier2 =
    {
        "add", "commit", "stash", "stash push", "stash pop", "stash apply",
        "branch create", "checkout", "switch", "merge", "tag create",
        "tag -a", "tag -m", "fetch", "push", "rebase", "cherry-pick",
        "revert", "worktree add", "worktree create", "worktree lock",
        "worktree unlock", "worktree remove", "worktree move",
        "worktree prune", "mv", "restore", "switch -c", "pull", "clone",
    };

    private static readonly string[] Tier3 =
    {
        "push --force", "push --force-with-lease", "push -f", "push +",
        "commit --amend", "reset --hard", "reset --soft", "branch -D",
        "branch -d", "branch --delete", "filter-branch", "filter-repo",
        "rebase -i", "rebase --interactive", "rebase --root",
        "gc --prune", "gc --prune=now", "gc --aggressive", "reflog expire",
        "reflog expire --expire=now", "update-ref -d", "clean -fd",
        "clean -fdx", "stash drop", "stash clear", "push --delete",
        "tag -d", "replace", "notes remove",
    };

    private static readonly string[] SystemSafeTier2 =
    {
        "add", "commit", "stash", "stash push", "stash pop", "stash apply",
        "branch", "branch create", "switch --create", "switch -c",
        "worktree add", "worktree lock", "worktree unlock",
        "worktree prune", "fetch", "restore", "mv", "tag --annotate",
        "tag create", "commit-graph write", "multi-pack-index write",
        "merge", "merge-tree", "update-ref", "bundle create", "init",
        "remote add", "remote set-url", "gc", "push",
    };

    private static readonly string[] DeniedMarkers =
    {
        "--force", "-f", "-D", "--delete", "--amend", "--hard", "--soft",
        "--prune", "--interactive", "--root", "expire", "filter-branch",
        "filter-repo", "drop", "clear", "clean", "--mirror", "+",
    };

    public static int Classify(IReadOnlyList<string> args)
    {
        var command = string.Join(' ', args).Trim().ToLowerInvariant();
        foreach (var op in Tier3)
            if (command.Contains(op, StringComparison.OrdinalIgnoreCase))
                return 3;
        foreach (var op in Tier1)
            if (command.StartsWith(op, StringComparison.Ordinal)
                || command == op)
                return 1;
        foreach (var op in Tier2)
            if (command.StartsWith(op, StringComparison.Ordinal)
                || command == op)
                return 2;
        return 3;
    }

    private static bool SystemSafeAllowed(IReadOnlyList<string> args)
    {
        if (args.Count == 0)
            return false;
        var head = string.Join(' ', args.Take(2)).ToLowerInvariant();
        var single = args[0].ToLowerInvariant();
        var allowed = SystemSafeTier2.Any(
            op => head == op || head.StartsWith(op + " ")
                  || single == op);
        if (!allowed)
            return false;
        // A denied marker in ANY argument refuses the operation.
        foreach (var arg in args.Skip(1))
        {
            var value = arg.ToLowerInvariant();
            if (DeniedMarkers.Any(marker =>
                    value == marker
                    || (marker.Length > 1 && value.StartsWith(marker))))
                return false;
        }
        return true;
    }

    public sealed record GateResult(
        bool Allowed, string Detail, int Tier, GitResult? Result);

    /// <summary>execute_system_safe parity: Tier-1 direct, allowlisted
    /// Tier-2 executed + audited, everything else refused fail-closed.</summary>
    public static GateResult Execute(
        string projectRoot, string worktree, IReadOnlyList<string> args,
        string actor, int timeoutMs = Git.DefaultTimeoutMs)
    {
        var tier = Classify(args);
        var command = string.Join(' ', args);
        var operation = args.Count > 0 ? args[0] : "unknown";
        if (tier >= 3 || (tier == 2 && !SystemSafeAllowed(args)))
        {
            var detail = tier >= 3
                ? "CAPABILITY_ISSUER_NOT_AUTHORIZED:automation may not issue tier-3 capability"
                : "CAPABILITY_OPERATION_NOT_SYSTEM_SAFE";
            Ledger.ChainedAudit(projectRoot, tier, command, actor, false,
                detail, operation, worktree, phase: "result",
                result: "denied", returncode: 425);
            return new GateResult(false, detail, tier, null);
        }
        var result = Git.Run(worktree, args, timeoutMs);
        Ledger.ChainedAudit(projectRoot, tier, command, actor, true,
            tier == 1 ? "tier-1: direct execution" : "system-safe tier-2",
            operation, worktree, phase: "result",
            result: result.Code == 0 ? "executed" : "failed",
            returncode: result.Code);
        return new GateResult(true, "", tier, result);
    }
}

/// <summary>Branch policy — parity with git_tiers.branch_policy.</summary>
internal static class BranchPolicy
{
    public const string Main = "main";

    private static readonly HashSet<string> Protected = new(StringComparer.Ordinal)
    { "main", "git", "local-model", "rag", "ui" };

    private static readonly string[] EphemeralPrefixes =
    {
        "ai/", "arch-", "bright-", "checker-", "flossy-", "sandy-",
        "permission-", "runtime-", "sync-", "xingcheng-",
    };

    private static readonly string[] ProtectedTagPrefixes =
    { "release/", "codex-" };

    public static string Normalize(string reference)
    {
        var value = (reference ?? "").Trim();
        if (value.StartsWith("refs/heads/", StringComparison.Ordinal))
            return value["refs/heads/".Length..];
        if (value.StartsWith("refs/remotes/", StringComparison.Ordinal))
        {
            var rest = value["refs/remotes/".Length..];
            var slash = rest.IndexOf('/');
            return slash >= 0 ? rest[(slash + 1)..] : rest;
        }
        return value;
    }

    public static bool IsMain(string branch) => Normalize(branch) == Main;

    public static bool IsProtected(string branch) =>
        Protected.Contains(Normalize(branch));

    public static bool IsWorker(string branch)
    {
        var name = Normalize(branch);
        return name.Length > 0 && name != "HEAD" && !IsMain(name);
    }

    public static bool IsProtectedTag(string reference)
    {
        var value = (reference ?? "").Trim();
        if (!value.StartsWith("refs/tags/", StringComparison.Ordinal))
            return false;
        var name = value["refs/tags/".Length..];
        return ProtectedTagPrefixes.Any(name.StartsWith);
    }

    /// <summary>policy_digest parity — sha256 of spaced canonical JSON.</summary>
    public static string PolicyDigest()
    {
        var payload = new JsonObject
        {
            ["protected"] = new JsonArray(
                Protected.Order(StringComparer.Ordinal)
                    .Select(p => JsonValue.Create(p)).ToArray<JsonNode?>()),
            ["ephemeral_prefixes"] = new JsonArray(
                EphemeralPrefixes
                    .Select(p => JsonValue.Create(p)).ToArray<JsonNode?>()),
            ["protected_tag_prefixes"] = new JsonArray(
                ProtectedTagPrefixes
                    .Select(p => JsonValue.Create(p)).ToArray<JsonNode?>()),
            ["main"] = Main,
        };
        using var document = JsonDocument.Parse(payload.ToJsonString());
        return Canon.Sha256Hex(
            Canon.Spaced(document.RootElement))[..16];
    }
}

/// <summary>
/// Re-apply Windows FILE_ATTRIBUTE_READONLY on protected governance
/// sources after checkouts drop it (protected_attrs parity).
/// </summary>
internal static class ProtectedAttrs
{
    public static JsonObject Restore(string root)
    {
        var restored = new JsonArray();
        var missing = new JsonArray();
        var checkedCount = 0;
        var codexDir = Path.Combine(root, "governance_rule", "codex");
        if (Directory.Exists(codexDir))
        {
            foreach (var file in Directory.EnumerateFiles(
                         codexDir, "governance_codex.zh-TW.part-*"))
            {
                checkedCount++;
                var info = new FileInfo(file);
                if (info.IsReadOnly)
                    continue;
                try
                {
                    info.IsReadOnly = true;
                    info.Refresh();
                    if (info.IsReadOnly)
                        restored.Add(
                            Path.GetRelativePath(root, file)
                                .Replace('\\', '/'));
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
        return new JsonObject
        {
            ["checked"] = checkedCount,
            ["restored"] = restored,
            ["missing"] = missing,
        };
    }
}
