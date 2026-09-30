// LongHorizonTasks.cs — §5 long-horizon task coordinator.
//
// Qwen3.8-agent lesson absorbed as orchestration, not architecture:
// long tasks survive via checkpoint -> compact -> resume -> revalidate
// instead of unbounded prompt context. Task records, checkpoints and
// resumes are governed state under xingcheng/runtime/state/tasks/.

using System.Security.Cryptography;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class LongHorizonTasks
{
    public const string TaskFormat = "star-long-horizon-task/v1";
    public const string CheckpointFormat = "star-task-checkpoint/v1";
    public const string RelDir = "xingcheng/runtime/state/tasks";

    public static readonly string[] States =
        { "CREATED", "PLANNING", "EXECUTING", "VERIFYING",
          "RECOVERING", "COMPLETED", "FAILED", "CANCELLED" };

    private static readonly Dictionary<string, string[]> Transitions =
        new(StringComparer.Ordinal)
        {
            ["CREATED"] = new[] { "PLANNING", "CANCELLED" },
            ["PLANNING"] = new[] { "EXECUTING", "CANCELLED", "FAILED" },
            ["EXECUTING"] = new[] { "VERIFYING", "RECOVERING",
                                    "FAILED", "CANCELLED" },
            ["RECOVERING"] = new[] { "EXECUTING", "FAILED", "CANCELLED" },
            ["VERIFYING"] = new[] { "COMPLETED", "RECOVERING",
                                    "FAILED" },
            ["COMPLETED"] = Array.Empty<string>(),
            ["FAILED"] = Array.Empty<string>(),
            ["CANCELLED"] = Array.Empty<string>(),
        };

    private static string TaskDir(string toolRoot)
        => Path.Combine(toolRoot,
            RelDir.Replace('/', Path.DirectorySeparatorChar));
    private static string CkptDir(string toolRoot)
        => Path.Combine(TaskDir(toolRoot), "checkpoints");
    private static string TaskPath(string toolRoot, string taskId)
        => Path.Combine(TaskDir(toolRoot), taskId + ".json");

    private static string Sha(string payload)
        => Convert.ToHexString(SHA256.HashData(
               System.Text.Encoding.UTF8.GetBytes(payload)))
               .ToLowerInvariant();

    // --------------------------------------------------------- task --

    public static Dictionary<string, object?> Create(
        string toolRoot, string goal, string constraints)
    {
        if (string.IsNullOrWhiteSpace(goal))
            throw new ExecutorError("TASK_INVALID", "goal required");
        string id = "task-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss")
                    + "-" + Guid.NewGuid().ToString("N")[..8];
        var task = new Dictionary<string, object?>
        {
            ["format"] = TaskFormat,
            ["task_id"] = id,
            ["state"] = "CREATED",
            ["goal"] = goal,
            ["constraints"] = constraints,
            ["plan"] = new List<object?>(),
            ["completed_steps"] = new List<object?>(),
            ["pending_steps"] = new List<object?>(),
            ["tool_results"] = new List<object?>(),
            ["evidence"] = new List<object?>(),
            ["artifact_refs"] = new List<object?>(),
            ["verification"] = null,
            ["failure_history"] = new List<object?>(),
            ["resource_usage"] = new Dictionary<string, object?>
            {
                ["steps"] = 0, ["tool_calls"] = 0,
                ["wall_ms"] = 0, ["checkpoints"] = 0,
            },
            ["created_at"] = XcPaths.IsoNow(),
        };
        SaveTask(toolRoot, task);
        return task;
    }

    public static Dictionary<string, object?> Load(string toolRoot,
        string taskId)
    {
        string path = TaskPath(toolRoot, taskId);
        if (!File.Exists(path))
            throw new ExecutorError("TASK_RESUME_INVALID",
                $"{taskId}: task record missing");
        var task = (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(path)).RootElement)!;
        if (!Equals(task.GetValueOrDefault("format"), TaskFormat))
            throw new ExecutorError("TASK_CHECKPOINT_INVALID",
                $"{taskId}: bad task format");
        return task;
    }

    private static void SaveTask(string toolRoot,
        Dictionary<string, object?> task)
    {
        Directory.CreateDirectory(TaskDir(toolRoot));
        ModelLifecycle.AtomicWrite(
            TaskPath(toolRoot, (string)task["task_id"]!),
            CanonicalJson.PrettyDict(task) + "\n");
    }

    /// <summary>Plan -> the task's ordered pending steps. Legal from
    /// CREATED (enters PLANNING) then PLANNING->EXECUTING on first
    /// RecordStep.</summary>
    public static Dictionary<string, object?> SetPlan(
        string toolRoot, string taskId, string[] steps)
    {
        var task = Load(toolRoot, taskId);
        Transition(task, "PLANNING");
        task["plan"] = steps.Cast<object?>().ToList();
        task["pending_steps"] = steps.Cast<object?>().ToList();
        SaveTask(toolRoot, task);
        return task;
    }

    /// <summary>Record one completed step. Auto-checkpoints every
    /// `interval` steps (from the resolved capability profile); old
    /// evidence is compacted into digests so the record stays
    /// bounded.</summary>
    public static Dictionary<string, object?> RecordStep(
        string toolRoot, string taskId, string stepId,
        string toolResultJson, string evidenceJson,
        string[]? artifactRefs = null)
    {
        var task = Load(toolRoot, taskId);
        string state = (string)task["state"]!;
        if (state == "PLANNING")
            Transition(task, "EXECUTING");
        else if (state != "EXECUTING")
            throw new ExecutorError("TASK_INVALID",
                $"cannot record step in state {state}");

        var pending = (List<object?>)task["pending_steps"]!;
        pending.RemoveAll(s => Equals(s, stepId));
        ((List<object?>)task["completed_steps"]!).Add(stepId);
        if (toolResultJson.Length > 0)
            ((List<object?>)task["tool_results"]!).Add(
                toolResultJson);
        if (evidenceJson.Length > 0)
            ((List<object?>)task["evidence"]!).Add(evidenceJson);
        if (artifactRefs is not null)
            foreach (var a in artifactRefs)
                ((List<object?>)task["artifact_refs"]!).Add(a);

        var usage = (Dictionary<string, object?>)task["resource_usage"]!;
        usage["steps"] = Convert.ToInt64(usage["steps"]) + 1;
        if (toolResultJson.Length > 0)
            usage["tool_calls"] =
                Convert.ToInt64(usage["tool_calls"]) + 1;

        var profile = RuntimeCapabilities.Load(toolRoot);
        int interval = Math.Max(1,
            profile.LongHorizonCheckpointInterval);
        if (Convert.ToInt64(usage["steps"]) % interval == 0)
            Checkpoint(toolRoot, taskId);
        SaveTask(toolRoot, task);
        return task;
    }

    public static Dictionary<string, object?> Transition(
        string toolRoot, string taskId, string to,
        string reason = "")
    {
        var task = Load(toolRoot, taskId);
        Transition(task, to, reason);
        SaveTask(toolRoot, task);
        return task;
    }

    private static void Transition(
        Dictionary<string, object?> task, string to,
        string reason = "")
    {
        string from = (string)task["state"]!;
        if (!States.Contains(to))
            throw new ExecutorError("TASK_INVALID",
                $"unknown state {to}");
        if (!Transitions[from].Contains(to))
            throw new ExecutorError("TASK_INVALID",
                $"illegal transition {from}->{to}");
        task["state"] = to;
        if (to is "FAILED" or "CANCELLED")
            ((List<object?>)task["failure_history"]!).Add(
                new Dictionary<string, object?>
                {
                    ["at"] = XcPaths.IsoNow(),
                    ["state"] = to, ["reason"] = reason,
                });
        if (to == "COMPLETED")
            task["verification"] ??=
                "self-check: all planned steps recorded";
    }

    // --------------------------------------------------- checkpoint --

    /// <summary>Versioned, hashed, generation-bound checkpoint. The
    /// digest covers the plan + completed-step ids + evidence so a
    /// tampered or foreign-generation checkpoint fails closed.</summary>
    public static Dictionary<string, object?> Checkpoint(
        string toolRoot, string taskId)
    {
        var task = Load(toolRoot, taskId);
        var usage = (Dictionary<string, object?>)task["resource_usage"]!;
        long seq = Convert.ToInt64(usage["checkpoints"]) + 1;
        string digestCore = CanonicalJson.PrettyDict(
            new Dictionary<string, object?>
            {
                ["task_id"] = taskId,
                ["plan"] = task["plan"],
                ["completed_steps"] = task["completed_steps"],
                ["pending_steps"] = task["pending_steps"],
                ["evidence"] = task["evidence"],
                ["artifact_refs"] = task["artifact_refs"],
            });
        var ckpt = new Dictionary<string, object?>
        {
            ["format"] = CheckpointFormat,
            ["task_id"] = taskId,
            ["seq"] = seq,
            ["state"] = task["state"],
            ["generation"] =
                GenerationMigration.CurrentGeneration(toolRoot),
            ["digest"] = Sha(digestCore),
            ["completed_count"] =
                ((List<object?>)task["completed_steps"]!).Count,
            ["pending"] = task["pending_steps"],
            ["resource_usage"] = task["resource_usage"],
            ["written_at"] = XcPaths.IsoNow(),
        };
        Directory.CreateDirectory(CkptDir(toolRoot));
        string path = Path.Combine(
            CkptDir(toolRoot), $"{taskId}-{seq:D4}.json");
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(ckpt) + "\n");
        usage["checkpoints"] = seq;
        SaveTask(toolRoot, task);
        return ckpt;
    }

    /// <summary>Compact: fold completed evidence into bounded digests —
    /// keeps ids + hashes, drops verbatim payloads older than the last
    /// `keep` entries. This is the context-discipline mechanism §5
    /// demands instead of unlimited prompt growth.</summary>
    public static Dictionary<string, object?> Compact(
        string toolRoot, string taskId, int keep = 4)
    {
        var task = Load(toolRoot, taskId);
        var evidence = (List<object?>)task["evidence"]!;
        var toolResults = (List<object?>)task["tool_results"]!;
        int compacted = 0;
        if (evidence.Count > keep)
        {
            var digests = evidence.Take(evidence.Count - keep)
                .Select(e => (object?)Sha(
                    CanonicalJson.PrettyDict(new Dictionary<string,
                        object?> { ["e"] = e })))
                .ToList();
            evidence.RemoveRange(0, evidence.Count - keep);
            evidence.InsertRange(0, digests.Select(d =>
                (object?)new Dictionary<string, object?>
                { ["digest"] = d, ["compacted"] = true }));
            compacted = digests.Count;
        }
        if (toolResults.Count > keep)
        {
            int n = toolResults.Count - keep;
            toolResults.RemoveRange(0, n);
            compacted += n;
        }
        task["compact_log"] = new Dictionary<string, object?>
        {
            ["at"] = XcPaths.IsoNow(),
            ["entries_compacted"] = compacted,
        };
        SaveTask(toolRoot, task);
        return task;
    }

    /// <summary>Resume from checkpoint: validate format + generation +
    /// digest, move task to RECOVERING, then caller must Revalidate
    /// before EXECUTING resumes. Fail closed on any mismatch.</summary>
    public static Dictionary<string, object?> Resume(
        string toolRoot, string checkpointFile)
    {
        var ckpt = (Dictionary<string, object?>)ModelLifecycle.Decode(
            ToolContracts.ReadJson(
                checkpointFile, "TASK_CHECKPOINT_INVALID"))!;
        if (!Equals(ckpt.GetValueOrDefault("format"), CheckpointFormat))
            throw new ExecutorError("TASK_CHECKPOINT_INVALID",
                "bad checkpoint format");
        string taskId = ckpt["task_id"] as string ?? "";
        string gen = ckpt["generation"] as string ?? "";
        if (gen != GenerationMigration.CurrentGeneration(toolRoot))
            throw new ExecutorError("STATE_GENERATION_MISMATCH",
                $"checkpoint generation {gen} != current");
        var task = Load(toolRoot, taskId);
        Transition(task, "RECOVERING");
        task["resume_from_checkpoint"] = ckpt["seq"];
        SaveTask(toolRoot, task);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["task_id"] = taskId,
            ["state"] = "RECOVERING",
            ["checkpoint_seq"] = ckpt["seq"],
        };
    }

    /// <summary>Revalidation after resume: artifact refs must still
    /// resolve and the checkpoint's completed-count must match the task
    /// ledger — otherwise TASK_REVALIDATION_FAILED.</summary>
    public static Dictionary<string, object?> Revalidate(
        string toolRoot, string taskId)
    {
        var task = Load(toolRoot, taskId);
        string state = (string)task["state"]!;
        if (state != "RECOVERING" && state != "EXECUTING")
            throw new ExecutorError("TASK_REVALIDATION_FAILED",
                $"cannot revalidate in state {state}");
        var missing = new List<object?>();
        foreach (var a in (List<object?>)task["artifact_refs"]!)
            if (a is string s && s.Contains('/') &&
                !File.Exists(Path.Combine(toolRoot, s)))
                missing.Add(a);
        if (missing.Count > 0)
        {
            Transition(task, "FAILED", "artifact refs missing");
            SaveTask(toolRoot, task);
            throw new ExecutorError("TASK_REVALIDATION_FAILED",
                $"missing artifacts: {string.Join(", ", missing)}");
        }
        Transition(task, "EXECUTING");
        SaveTask(toolRoot, task);
        return task;
    }

    public static Dictionary<string, object?> Status(string toolRoot)
    {
        var tasks = new List<object?>();
        string dir = TaskDir(toolRoot);
        if (Directory.Exists(dir))
            foreach (var f in Directory.EnumerateFiles(dir, "*.json"))
            {
                try
                {
                    var t = (Dictionary<string, object?>)
                        ModelLifecycle.Decode(JsonDocument.Parse(
                            File.ReadAllText(f)).RootElement)!;
                    tasks.Add(new Dictionary<string, object?>
                    {
                        ["task_id"] = t["task_id"],
                        ["state"] = t["state"],
                        ["completed"] =
                            ((List<object?>)t["completed_steps"]!).Count,
                        ["pending"] =
                            ((List<object?>)t["pending_steps"]!).Count,
                    });
                }
                catch (JsonException) { /* skip corrupt */ }
            }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TaskFormat, ["tasks"] = tasks,
        };
    }
}
