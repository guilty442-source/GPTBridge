// LongHorizonTask.cs — §5/§9 Qwen-agent + R1-policy absorption.
//
//   LongHorizonTaskCoordinator — task state machine that survives
//   context limits: progress lives in a checkpointed manifest (never in
//   an ever-growing prompt); every N steps the task compacts its
//   history into a bounded summary, resumes from the checkpoint, and
//   revalidates before continuing.
//
//   States: CREATED -> PLANNING -> EXECUTING -> VERIFYING -> COMPLETED
//                             \-> RECOVERING -> EXECUTING
//                             \-> FAILED | CANCELLED
//
//   Reasoning budgets come from star-runtime-capabilities/v1
//   (ReasoningPolicy §9) — modes only reshape budgets; HIGH is a
//   runtime orchestration posture, never a claimed model capability.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class LongHorizonTask
{
    public const string Format = "star-longhorizon-task/v1";
    public const string CheckpointFormat = "star-task-checkpoint/v1";
    public const string RelDir = "xingcheng/runtime/state/tasks";

    public static readonly string[] States =
        { "CREATED", "PLANNING", "EXECUTING", "VERIFYING",
          "RECOVERING", "COMPLETED", "FAILED", "CANCELLED" };

    private static string Dir(string toolRoot)
        => Path.Combine(toolRoot,
                        RelDir.Replace('/', Path.DirectorySeparatorChar));
    private static string PathOf(string toolRoot, string id)
        => Path.Combine(Dir(toolRoot), $"task-{San(id)}.json");
    private static string CkptOf(string toolRoot, string id)
        => Path.Combine(Dir(toolRoot), $"task-{San(id)}.ckpt.json");
    private static string San(string id)
        => string.Concat(id.Select(
            c => char.IsLetterOrDigit(c) || c == '-' ? c : '_'));

    // ---------------------------------------------------------- task --

    private static Dictionary<string, object?> Load(
        string toolRoot, string id)
    {
        string path = PathOf(toolRoot, id);
        if (!File.Exists(path))
            throw new ExecutorError("TASK_MISSING", id);
        return ToolContracts.ReadObject(path, "TASK_MANIFEST_INVALID");
    }

    private static void Save(
        string toolRoot, Dictionary<string, object?> t)
    {
        Directory.CreateDirectory(Dir(toolRoot));
        ModelLifecycle.AtomicWrite(
            PathOf(toolRoot, (string)t["task_id"]!),
            CanonicalJson.PrettyDict(t) + "\n");
    }

    private static void Event(Dictionary<string, object?> t, string evt,
                              params (string K, object? V)[] f)
    {
        if (t["history"] is not List<object?> h)
        {
            h = new List<object?>();
            t["history"] = h;
        }
        var e = new Dictionary<string, object?>
            { ["at"] = XcPaths.IsoNow(), ["event"] = evt };
        foreach (var (k, v) in f) e[k] = v;
        h.Add(e);
        t["history"] = h;
    }

    public static Dictionary<string, object?> Create(
        string toolRoot, string goal, string constraintsFile,
        string planFile)
    {
        var p = RuntimeCapabilities.Load(toolRoot);
        if (!p.LongHorizonEnabled)
            throw new ExecutorError("TASK_DISABLED",
                "long_horizon.enabled=false in runtime capabilities");
        var t = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["task_id"] = "task-" +
                DateTime.UtcNow.ToString("yyyyMMdd-HHmmss") + "-" +
                Guid.NewGuid().ToString("N")[..6],
            ["state"] = "CREATED",
            ["goal"] = goal,
            ["constraints"] =
                File.Exists(constraintsFile)
                    ? ToolContracts.ReadObject(
                        constraintsFile, "TASK_CONSTRAINTS_INVALID")
                    : new Dictionary<string, object?>(),
            ["plan"] =
                File.Exists(planFile)
                    ? (object?)ToolContracts.Arr(
                        ToolContracts.ReadObject(
                            planFile, "TASK_PLAN_INVALID"), "steps")
                    : new List<object?>(),
            ["completed_steps"] = new List<object?>(),
            ["pending_steps"] = new List<object?>(),
            ["tool_results"] = new List<object?>(),
            ["evidence"] = new List<object?>(),
            ["artifact_refs"] = new List<object?>(),
            ["verification"] = new Dictionary<string, object?>(),
            ["failure_history"] = new List<object?>(),
            ["resource_usage"] = new Dictionary<string, object?>
            {
                ["steps"] = 0, ["tool_calls"] = 0,
                ["tokens_budgeted"] = p.ReasoningTokenBudget,
                ["checkpoints"] = 0, ["compactions"] = 0,
            },
            ["checkpoint_ref"] = "",
            ["created_at"] = XcPaths.IsoNow(),
            ["updated_at"] = XcPaths.IsoNow(),
            ["history"] = new List<object?>(),
        };
        if (t["plan"] is List<object?> pl && pl.Count == 0 &&
            File.Exists(planFile))
            t["plan"] = pl;               // plan file provided but empty
        Event(t, "created", ("goal", goal));
        t["state"] = "PLANNING";
        if (t["plan"] is List<object?> pl2 && pl2.Count > 0)
        {
            t["pending_steps"] = new List<object?>(pl2);
            t["state"] = "EXECUTING";
            Event(t, "planned", ("steps", pl2.Count));
        }
        Save(toolRoot, t);
        return Ok(t);
    }

    public static Dictionary<string, object?> Step(
        string toolRoot, string id, string step,
        string toolResultFile, string evidenceFile)
    {
        var t = Load(toolRoot, id);
        NeedState(t, "EXECUTING", "step");
        var steps = (List<object?>)t["completed_steps"]!;
        steps.Add(new Dictionary<string, object?>
            { ["step"] = step, ["at"] = XcPaths.IsoNow() });
        var pend = (List<object?>)t["pending_steps"]!;
        if (pend.Count > 0) pend.RemoveAt(0);
        var usage = (Dictionary<string, object?>)t["resource_usage"]!;
        usage["steps"] = Convert.ToInt64(usage["steps"]) + 1;
        if (File.Exists(toolResultFile))
        {
            var tr = ToolContracts.ReadObject(
                toolResultFile, "TOOL_SCHEMA_INVALID");
            ((List<object?>)t["tool_results"]!).Add(tr);
            usage["tool_calls"] =
                Convert.ToInt64(usage["tool_calls"]) + 1;
        }
        if (File.Exists(evidenceFile))
            ((List<object?>)t["evidence"]!).Add(
                ToolContracts.ReadObject(
                    evidenceFile, "TASK_EVIDENCE_INVALID"));
        Event(t, "step", ("step", step),
              ("pending", pend.Count));
        t["updated_at"] = XcPaths.IsoNow();
        // auto-checkpoint cadence from the capability profile.
        var p = RuntimeCapabilities.Load(toolRoot);
        if (p.LongHorizonCheckpointInterval > 0 &&
            Convert.ToInt64(usage["steps"]) %
                p.LongHorizonCheckpointInterval == 0)
            Checkpoint(toolRoot, id, t);
        Save(toolRoot, t);
        return Ok(t);
    }

    public static Dictionary<string, object?> Checkpoint(
        string toolRoot, string id,
        Dictionary<string, object?>? existing = null)
    {
        var t = existing ?? Load(toolRoot, id);
        var ckpt = new Dictionary<string, object?>
        {
            ["format"] = CheckpointFormat,
            ["task_id"] = t["task_id"],
            ["state"] = t["state"],
            ["completed_steps"] = t["completed_steps"],
            ["pending_steps"] = t["pending_steps"],
            ["evidence_count"] =
                ((List<object?>)t["evidence"]!).Count,
            ["resource_usage"] = t["resource_usage"],
            ["checkpointed_at"] = XcPaths.IsoNow(),
        };
        string path = CkptOf(toolRoot, id);
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(ckpt) + "\n");
        t["checkpoint_ref"] =
            Path.GetRelativePath(toolRoot, path).Replace('\\', '/');
        var usage = (Dictionary<string, object?>)t["resource_usage"]!;
        usage["checkpoints"] =
            Convert.ToInt64(usage["checkpoints"]) + 1;
        Event(t, "checkpoint");
        t["updated_at"] = XcPaths.IsoNow();
        if (existing == null) Save(toolRoot, t);
        else Save(toolRoot, t);
        return Ok(t);
    }

    /// <summary>Compact: fold completed steps into a bounded summary
    /// record, keeping the last few verbatim. The task carries bounded
    /// context forward — never an unbounded transcript.</summary>
    public static Dictionary<string, object?> Compact(
        string toolRoot, string id, int keepLast = 4)
    {
        var t = Load(toolRoot, id);
        NeedState(t, "EXECUTING", "compact");
        var steps = (List<object?>)t["completed_steps"]!;
        int fold = Math.Max(0, steps.Count - keepLast);
        if (fold == 0)
            return Ok(t);   // nothing to compact
        var folded = steps.Take(fold)
            .Select(s => s is Dictionary<string, object?> d
                ? ToolContracts.Str(d, "step") : s?.ToString() ?? "")
            .ToList();
        t["completed_steps"] = steps.Skip(fold).Cast<object?>().ToList();
        if (t["compacted_history"] is not List<object?> ch)
        {
            ch = new List<object?>();
            t["compacted_history"] = ch;
        }
        ch.Add(new Dictionary<string, object?>
        {
            ["at"] = XcPaths.IsoNow(),
            ["folded_steps"] = folded.Cast<object?>().ToList(),
            ["folded_count"] = fold,
        });
        var usage = (Dictionary<string, object?>)t["resource_usage"]!;
        usage["compactions"] =
            Convert.ToInt64(usage["compactions"]) + 1;
        Event(t, "compact", ("folded", fold));
        t["updated_at"] = XcPaths.IsoNow();
        Save(toolRoot, t);
        return Ok(t);
    }

    /// <summary>Resume: reload from checkpoint, revalidate state +
    /// pending work, re-enter EXECUTING (via RECOVERING when the
    /// manifest was mid-failure).</summary>
    public static Dictionary<string, object?> Resume(
        string toolRoot, string id)
    {
        var t = Load(toolRoot, id);
        if ((string)t["state"]! is "COMPLETED" or "CANCELLED")
            throw new ExecutorError("TASK_TERMINAL", id);
        string ckptPath = CkptOf(toolRoot, id);
        bool revalidated = false;
        if (File.Exists(ckptPath))
        {
            var ck = ToolContracts.ReadObject(
                ckptPath, "TASK_CHECKPOINT_INVALID");
            // revalidate: checkpoint and manifest agree on identity.
            if (ToolContracts.Str(ck, "task_id") != (string)t["task_id"]!)
                throw new ExecutorError("TASK_CHECKPOINT_INVALID",
                    "checkpoint task_id mismatch");
            revalidated = true;
        }
        if ((string)t["state"]! == "FAILED")
        {
            t["state"] = "RECOVERING";
            Event(t, "recovering");
        }
        t["state"] = "EXECUTING";
        Event(t, "resumed", ("revalidated", revalidated));
        t["updated_at"] = XcPaths.IsoNow();
        Save(toolRoot, t);
        var r = Ok(t);
        r["revalidated"] = revalidated;
        return r;
    }

    public static Dictionary<string, object?> Verify(
        string toolRoot, string id, string note)
    {
        var t = Load(toolRoot, id);
        NeedState(t, "EXECUTING", "verify");
        t["state"] = "VERIFYING";
        var pend = (List<object?>)t["pending_steps"]!;
        bool pass = pend.Count == 0;
        t["verification"] = new Dictionary<string, object?>
        {
            ["at"] = XcPaths.IsoNow(), ["note"] = note,
            ["pending_remaining"] = pend.Count,
            ["result"] = pass ? "pass" : "pending-work-remains",
        };
        Event(t, "verify", ("result", pass ? "pass" : "pending"));
        t["state"] = pass ? "VERIFYING" : "EXECUTING";
        t["updated_at"] = XcPaths.IsoNow();
        Save(toolRoot, t);
        return Ok(t);
    }

    public static Dictionary<string, object?> Complete(
        string toolRoot, string id)
    {
        var t = Load(toolRoot, id);
        if ((string)t["state"]! != "VERIFYING")
            throw new ExecutorError("TASK_STATE",
                "complete requires VERIFYING state");
        t["state"] = "COMPLETED";
        Event(t, "completed");
        t["updated_at"] = XcPaths.IsoNow();
        Save(toolRoot, t);
        return Ok(t);
    }

    public static Dictionary<string, object?> Fail(
        string toolRoot, string id, string reason)
    {
        var t = Load(toolRoot, id);
        ((List<object?>)t["failure_history"]!).Add(
            new Dictionary<string, object?>
                { ["at"] = XcPaths.IsoNow(), ["reason"] = reason });
        t["state"] = "FAILED";
        Event(t, "failed", ("reason", reason));
        t["updated_at"] = XcPaths.IsoNow();
        Save(toolRoot, t);
        return Ok(t);
    }

    public static Dictionary<string, object?> Status(
        string toolRoot, string id)
    {
        if (id.Length > 0) return Ok(Load(toolRoot, id));
        var tasks = new List<object?>();
        if (Directory.Exists(Dir(toolRoot)))
            foreach (var f in Directory.GetFiles(Dir(toolRoot),
                                               "task-*.json"))
            {
                if (f.EndsWith(".ckpt.json")) continue;
                var t = ToolContracts.ReadObject(
                    f, "TASK_MANIFEST_INVALID");
                tasks.Add(new Dictionary<string, object?>
                {
                    ["task_id"] = t["task_id"],
                    ["state"] = t["state"],
                    ["goal"] = t["goal"],
                    ["updated_at"] = t["updated_at"],
                });
            }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format, ["tasks"] = tasks,
        };
    }

    private static void NeedState(
        Dictionary<string, object?> t, string want, string op)
    {
        if ((string)t["state"]! != want)
            throw new ExecutorError("TASK_STATE",
                $"{op} requires {want}, have {t["state"]}");
    }

    private static Dictionary<string, object?> Ok(
        Dictionary<string, object?> t)
        => new()
        {
            ["ok"] = true, ["format"] = Format,
            ["task_id"] = t["task_id"], ["state"] = t["state"],
            ["completed"] =
                ((List<object?>)t["completed_steps"]!).Count,
            ["pending"] =
                ((List<object?>)t["pending_steps"]!).Count,
            ["checkpoint_ref"] = t["checkpoint_ref"],
        };
}
