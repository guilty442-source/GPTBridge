using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Durable merge queue — parity with git_tiers.merge_queue: queue.json in
/// the repo-common gptbridge-automation/merge-queue dir, transitions
/// audited through the chain ledger.
/// </summary>
internal sealed class MergeQueue
{
    private readonly string _dir;
    private readonly string _file;
    private readonly string _lockPath;
    private readonly string _projectRoot;

    public MergeQueue(string projectRoot, string commonDir)
    {
        _projectRoot = projectRoot;
        _dir = Path.Combine(commonDir, "gptbridge-automation",
                            "merge-queue");
        Directory.CreateDirectory(_dir);
        _file = Path.Combine(_dir, "queue.json");
        _lockPath = Path.Combine(_dir, "merge-queue.lock");
    }

    private JsonObject Load()
    {
        JsonObject payload;
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(_file));
            payload = node as JsonObject ?? new JsonObject();
        }
        catch (IOException) { payload = new JsonObject(); }
        catch (JsonException) { payload = new JsonObject(); }
        payload["next_sequence"] ??= 1;
        payload["entries"] ??= new JsonArray();
        return payload;
    }

    private void Store(JsonObject payload)
    {
        using var document = JsonDocument.Parse(payload.ToJsonString());
        Canon.WriteJsonAtomic(
            _file, Canon.Indented(document.RootElement));
    }

    private JsonArray Entries(JsonObject payload) =>
        (JsonArray)payload["entries"]!;

    public JsonObject? FindEntry(string branch, string sourceCommit)
    {
        var entries = Entries(Load());
        for (var i = entries.Count - 1; i >= 0; i--)
        {
            var item = entries[i] as JsonObject;
            if (item?["source_branch"]?.GetValue<string>() == branch
                && item["source_commit"]?.GetValue<string>() == sourceCommit)
                return (JsonObject)item.DeepClone();
        }
        return null;
    }

    public JsonObject Enqueue(
        string workerId, string branch, string sourceCommit,
        string baseMainCommit = "", string taskId = "", int priority = 0,
        string escalatedBy = "", double notBefore = 0.0)
    {
        var blocked = GovManifest.WriteBlockReason(
            _projectRoot, "merge-queue.enqueue");
        if (blocked is not null)
            return new JsonObject
            {
                ["status"] = "error", ["detail"] = blocked,
            };
        var escalationActors = new HashSet<string>(StringComparer.Ordinal)
        {
            "human-governor", "governance/automation-supervisor",
            "governance/git-coordinator", "governance/workspace-sync",
            "governance/decision-layer",
        };
        if (priority > 0 && !escalationActors.Contains(escalatedBy))
            priority = 0;
        var auditId = Guid.NewGuid().ToString("N")[..16];
        JsonObject entry;
        try
        {
            using var _ = ProcessFileLock.Acquire(_lockPath);
            var payload = Load();
            var sequence = payload["next_sequence"]!.GetValue<int>();
            entry = new JsonObject
            {
                ["queue_id"] = $"mq-{sequence:D6}-{auditId[..6]}",
                ["enqueue_sequence"] = sequence,
                ["worker_id"] = workerId,
                ["task_id"] = taskId,
                ["source_branch"] = branch,
                ["source_commit"] = sourceCommit,
                ["base_main_commit"] = baseMainCommit,
                ["enqueue_time"] = Canon.EpochSeconds(),
                ["priority"] = priority,
                ["escalated_by"] =
                    priority > 0 ? escalatedBy : "",
                ["attempt_count"] = 0,
                ["not_before"] = notBefore,
                ["blocked_reason"] = "",
                ["status"] = "pending",
                ["audit_id"] = auditId,
                ["policy_hash"] = BranchPolicy.PolicyDigest(),
                ["updated_at"] = Canon.EpochSeconds(),
            };
            payload["next_sequence"] = sequence + 1;
            Entries(payload).Add(entry);
            Store(payload);
        }
        catch (LockBusyException)
        {
            return new JsonObject
            {
                ["status"] = "error", ["detail"] = "queue-busy",
            };
        }
        Ledger.ChainedAudit(_projectRoot, 2, "merge-queue enqueue",
            "governance/merge-queue", true,
            $"{entry["queue_id"]} {branch}@{sourceCommit} priority={priority}",
            operation: "merge-queue", phase: "result", result: "enqueued");
        return entry;
    }

    private static readonly HashSet<string> Statuses = new(StringComparer.Ordinal)
    {
        "pending", "running", "merged", "conflicted", "blocked",
        "failed", "cancelled",
    };

    private JsonObject? Transition(
        string queueId, string status, string detail = "",
        bool incrementAttempt = false)
    {
        if (!Statuses.Contains(status))
            return null;
        if (GovManifest.WriteBlockReason(
                _projectRoot, "merge-queue.transition") is not null)
            return null;
        JsonObject? entry = null;
        using (var _ = ProcessFileLock.Acquire(_lockPath))
        {
            var payload = Load();
            entry = Entries(payload)
                .OfType<JsonObject>()
                .FirstOrDefault(e =>
                    e["queue_id"]?.GetValue<string>() == queueId);
            if (entry is null)
                return null;
            entry["status"] = status;
            entry["updated_at"] = Canon.EpochSeconds();
            if (detail.Length > 0)
                entry[status is "blocked" or "conflicted" or "failed"
                    ? "blocked_reason" : "detail"] = detail;
            if (incrementAttempt)
                entry["attempt_count"] =
                    (entry["attempt_count"]?.GetValue<int>() ?? 0) + 1;
            Store(payload);
            entry = (JsonObject)entry.DeepClone();
        }
        Ledger.ChainedAudit(_projectRoot, 2, "merge-queue transition",
            "governance/merge-queue", true,
            $"{queueId} -> {status} {detail}"[..Math.Min(400,
                $"{queueId} -> {status} {detail}".Length)],
            operation: "merge-queue", phase: "result", result: status);
        return entry;
    }

    public JsonObject? MarkRunning(string queueId) =>
        Transition(queueId, "running", incrementAttempt: true);
    public JsonObject? MarkMerged(string queueId, string detail = "") =>
        Transition(queueId, "merged", detail);
    public JsonObject? MarkConflicted(string queueId, string reason) =>
        Transition(queueId, "conflicted", reason);
    public JsonObject? MarkBlocked(string queueId, string reason) =>
        Transition(queueId, "blocked", reason);

    public bool HasPending()
    {
        var entries = Entries(Load());
        return entries.OfType<JsonObject>().Any(
            e => e["status"]?.GetValue<string>() == "pending");
    }
}

/// <summary>
/// §10.8 paired-path contract rules — parity with
/// git_tiers.contract_check (contract_rules.json + fnmatch globs).
/// </summary>
internal static class ContractCheck
{
    private static bool Fnmatch(string name, string pattern)
    {
        // Python fnmatch: * crosses directories, ? is single char,
        // [seq]/[!seq] character classes.
        var regex = "^" + Regex.Escape(pattern)
            .Replace("\\*", ".*").Replace("\\?", ".") + "$";
        // Handle character classes left escaped by Regex.Escape.
        regex = Regex.Replace(regex, @"\\\[([^\]]+)\\\]",
            m => "[" + m.Groups[1].Value + "]");
        return Regex.IsMatch(name, regex);
    }

    public static JsonObject Check(
        string projectRoot, string worktree, string source, string target)
    {
        var diff = Git.Run(worktree,
            new[] { "diff", "--name-only", $"{target}...{source}" });
        var touched = diff.Code == 0
            ? diff.Stdout.Split('\n')
                .Select(l => l.Trim())
                .Where(l => l.Length > 0).Order().ToList()
            : new List<string>();
        var rulesPath = Path.Combine(projectRoot, "governance_rule",
            "execution", "git_tiers", "contracts", "contract_rules.json");
        var rules = new List<JsonObject>();
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(rulesPath));
            if (node?["rules"] is JsonArray list)
                rules = list.OfType<JsonObject>().ToList();
        }
        catch (IOException) { }
        catch (JsonException) { }

        var violations = new JsonArray();
        var warnings = new JsonArray();
        foreach (var rule in rules)
        {
            var triggers = rule["trigger_globs"]?.AsArray()
                .Select(n => n?.GetValue<string>() ?? "").ToList() ?? [];
            var requires = rule["require_globs"]?.AsArray()
                .Select(n => n?.GetValue<string>() ?? "").ToList() ?? [];
            if (triggers.Count == 0 || requires.Count == 0)
                continue;
            if (!touched.Any(t => triggers.Any(p => Fnmatch(t, p))))
                continue;
            if (touched.Any(t => requires.Any(p => Fnmatch(t, p))))
                continue;
            var entry = new JsonObject
            {
                ["rule"] = rule["name"]?.GetValue<string>() ?? "unnamed",
                ["severity"] =
                    rule["severity"]?.GetValue<string>() ?? "warn",
                ["detail"] = rule["detail"]?.GetValue<string>() ?? "",
            };
            if (entry["severity"]!.GetValue<string>() == "error")
                violations.Add(entry);
            else
                warnings.Add(entry);
        }
        return new JsonObject
        {
            ["ok"] = violations.Count == 0,
            ["violations"] = violations,
            ["warnings"] = warnings,
            ["checked_rules"] = rules.Count,
            ["touched"] = new JsonArray(
                touched.Select(t => (JsonNode?)JsonValue.Create(t))
                    .ToArray()),
        };
    }
}
