// AgentRuntime.cs — the agent-layer absorptions (§5, §10, §11, §12,
// §22):
//
//   LongHorizonTaskCoordinator   checkpoint -> compact -> resume ->
//                                revalidate task machine (never grows
//                                prompt context without bound)
//   StarCodeAgentRuntime         star-code-task/v1 governed coding
//                                proposal pipeline (inspect -> plan ->
//                                propose -> static validate -> compile/
//                                test only when authorized -> verify)
//   star-fim/v1                  fill-in-the-middle contract + runtime
//                                envelope (no tokenizer change; when no
//                                dedicated FIM tokens exist the envelope
//                                text markers carry the structure)
//   RepoTaskHarness              per-task evidence record (§22)
//   ModalityProvenance           per-request multimodal ledger (§12)
//   TeacherLineage               schema-only distillation lineage (§12)
//
// Every record is JSON under xingcheng/runtime/state/ — the coordinator
// persists, never the prompt window.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

/// <summary>§5 task states — closed vocabulary.</summary>
internal enum LongHorizonState
{
    CREATED, PLANNING, EXECUTING, VERIFYING,
    RECOVERING, COMPLETED, FAILED, CANCELLED,
}

/// <summary>§5 a persisted long-horizon task. The record owns the goal,
/// plan, step ledger, evidence and resource accounting; the prompt
/// window only ever sees the compacted view.</summary>
internal sealed class LongHorizonTask
{
    public const string Format = "star-long-horizon-task/v1";

    public string TaskId = "";
    public string Goal = "";
    public List<string> Constraints = new();
    public List<string> Plan = new();
    public List<string> CompletedSteps = new();
    public List<string> PendingSteps = new();
    public List<Dictionary<string, object?>> ToolResults = new();
    public List<Dictionary<string, object?>> Evidence = new();
    public List<string> ArtifactRefs = new();
    public Dictionary<string, object?> Verification = new();
    public List<Dictionary<string, object?>> FailureHistory = new();
    public Dictionary<string, object?> ResourceUsage = new()
    {
        ["forwards"] = 0, ["tool_calls"] = 0, ["compactions"] = 0,
    };
    public LongHorizonState State = LongHorizonState.CREATED;
    public string CompactSummary = "";      // bounded working memory
    public string Generation = "";          // bound to a model gen
    public int CheckpointSeq = 0;

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = Format,
        ["task_id"] = TaskId,
        ["goal"] = Goal,
        ["constraints"] = Constraints.Cast<object?>().ToList(),
        ["plan"] = Plan.Cast<object?>().ToList(),
        ["completed_steps"] = CompletedSteps.Cast<object?>().ToList(),
        ["pending_steps"] = PendingSteps.Cast<object?>().ToList(),
        ["tool_results"] = ToolResults.Cast<object?>().ToList(),
        ["evidence"] = Evidence.Cast<object?>().ToList(),
        ["artifact_refs"] = ArtifactRefs.Cast<object?>().ToList(),
        ["verification"] = Verification,
        ["failure_history"] = FailureHistory.Cast<object?>().ToList(),
        ["resource_usage"] = ResourceUsage,
        ["state"] = State.ToString(),
        ["compact_summary"] = CompactSummary,
        ["generation"] = Generation,
        ["checkpoint_seq"] = CheckpointSeq,
    };

    public static LongHorizonTask FromDict(
        Dictionary<string, object?> d)
    {
        var t = new LongHorizonTask();
        string Str(string k) => d.TryGetValue(k, out var v)
            ? v?.ToString() ?? "" : "";
        List<string> Strs(string k)
            => d.TryGetValue(k, out var v) && v is List<object?> l
                ? l.Select(x => x?.ToString() ?? "").ToList()
                : new List<string>();
        List<Dictionary<string, object?>> Objs(string k)
            => d.TryGetValue(k, out var v) && v is List<object?> l
                ? l.OfType<Dictionary<string, object?>>().ToList()
                : new List<Dictionary<string, object?>>();

        t.TaskId = Str("task_id");
        t.Goal = Str("goal");
        t.Constraints = Strs("constraints");
        t.Plan = Strs("plan");
        t.CompletedSteps = Strs("completed_steps");
        t.PendingSteps = Strs("pending_steps");
        t.ToolResults = Objs("tool_results");
        t.Evidence = Objs("evidence");
        t.ArtifactRefs = Strs("artifact_refs");
        t.FailureHistory = Objs("failure_history");
        if (d.TryGetValue("verification", out var ver) &&
            ver is Dictionary<string, object?> vd)
            t.Verification = vd;
        if (d.TryGetValue("resource_usage", out var ru) &&
            ru is Dictionary<string, object?> rd)
            t.ResourceUsage = rd;
        if (Enum.TryParse(Str("state"), out LongHorizonState st))
            t.State = st;
        t.CompactSummary = Str("compact_summary");
        t.Generation = Str("generation");
        if (d.TryGetValue("checkpoint_seq", out var cs))
            t.CheckpointSeq = Convert.ToInt32(cs);
        return t;
    }
}

/// <summary>§5 coordinator — the only owner of task records. State
/// transitions are guarded; checkpoint writes are atomic; compaction
/// produces a bounded summary + step ledger, never a longer prompt.</summary>
internal static class LongHorizonTaskCoordinator
{
    public const string TasksRel = "xingcheng/runtime/state/tasks";
    public const int MaxSummaryChars = 8192;
    public const int MaxLedgerEntries = 256;

    private static string TaskPath(string toolRoot, string id)
        => Path.Combine(toolRoot, TasksRel, $"task-{id}.json");

    public static Dictionary<string, object?> Create(
        string toolRoot, string goal,
        List<string> constraints, string generation)
    {
        if (string.IsNullOrWhiteSpace(goal))
            throw new ExecutorError("TASK_GOAL_REQUIRED", "empty goal");
        var t = new LongHorizonTask
        {
            TaskId = $"task-{DateTime.UtcNow:yyyyMMddHHmmss}-" +
                     Guid.NewGuid().ToString("N")[..8],
            Goal = goal,
            Constraints = constraints,
            Generation = generation,
            State = LongHorizonState.CREATED,
        };
        Save(toolRoot, t);
        return t.ToDict();
    }

    public static LongHorizonTask Load(string toolRoot, string taskId)
    {
        string path = TaskPath(toolRoot, taskId);
        if (!File.Exists(path))
            throw new ExecutorError("TASK_NOT_FOUND", taskId);
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var d = (Dictionary<string, object?>)ModelLifecycle.Decode(
            doc.RootElement)!;
        var t = LongHorizonTask.FromDict(d);
        if (t.TaskId != taskId)
            throw new ExecutorError("TASK_ID_MISMATCH", taskId);
        return t;
    }

    /// <summary>Guarded transition — the illegal moves are typed
    /// failures, not silent no-ops.</summary>
    public static Dictionary<string, object?> Transition(
        string toolRoot, string taskId, LongHorizonState next,
        string note = "")
    {
        var t = Load(toolRoot, taskId);
        if (!Legal(t.State, next))
            throw new ExecutorError(
                "TASK_TRANSITION_INVALID",
                $"{t.State} -> {next}");
        t.State = next;
        if (note.Length > 0)
            t.Evidence.Add(new Dictionary<string, object?>
            {
                ["at"] = XcPaths.IsoNow(), ["kind"] = "transition",
                ["to"] = next.ToString(), ["note"] = note,
            });
        Save(toolRoot, t);
        return t.ToDict();
    }

    private static bool Legal(LongHorizonState from, LongHorizonState to)
        => (from, to) switch
        {
            (LongHorizonState.CREATED, LongHorizonState.PLANNING) => true,
            (LongHorizonState.PLANNING, LongHorizonState.EXECUTING) => true,
            (LongHorizonState.EXECUTING, LongHorizonState.VERIFYING) => true,
            (LongHorizonState.EXECUTING, LongHorizonState.RECOVERING) => true,
            (LongHorizonState.RECOVERING, LongHorizonState.EXECUTING) => true,
            (LongHorizonState.VERIFYING, LongHorizonState.COMPLETED) => true,
            (LongHorizonState.VERIFYING, LongHorizonState.RECOVERING) => true,
            (_, LongHorizonState.FAILED) =>
                from is not LongHorizonState.COMPLETED
                     and not LongHorizonState.CANCELLED,
            (_, LongHorizonState.CANCELLED) =>
                from is not LongHorizonState.COMPLETED,
            _ => false,
        };

    /// <summary>§5 checkpoint -> compact: fold completed work into the
    /// bounded summary, keep the ledger tail, and bump the checkpoint
    /// sequence. The full step detail stays in the persisted record —
    /// resume re-reads the record, not the prompt.</summary>
    public static Dictionary<string, object?> Checkpoint(
        string toolRoot, string taskId, string summary)
    {
        var t = Load(toolRoot, taskId);
        ++t.CheckpointSeq;
        t.CompactSummary = summary.Length > MaxSummaryChars
            ? summary[..MaxSummaryChars] : summary;
        // Bound the ledger: keep head + tail so the record stays a
        // fixed-size document even on very long tasks.
        if (t.CompletedSteps.Count > MaxLedgerEntries)
            t.CompletedSteps = t.CompletedSteps
                .Take(MaxLedgerEntries / 2)
                .Concat(t.CompletedSteps.TakeLast(MaxLedgerEntries / 2))
                .ToList();
        t.Evidence.Add(new Dictionary<string, object?>
        {
            ["at"] = XcPaths.IsoNow(), ["kind"] = "checkpoint",
            ["seq"] = t.CheckpointSeq,
        });
        Save(toolRoot, t);
        return t.ToDict();
    }

    /// <summary>resume -> revalidate: load the persisted record and
    /// verify the generation binding before any continuation — a task
    /// pinned to a different model generation can never carry its state
    /// forward silently (STATE_GENERATION_MISMATCH).</summary>
    public static Dictionary<string, object?> Resume(
        string toolRoot, string taskId, string generation)
    {
        var t = Load(toolRoot, taskId);
        if (t.Generation.Length > 0 && t.Generation != generation)
            throw new ExecutorError(
                ConvErr.StateGenerationMismatch,
                $"task {taskId} bound to {t.Generation}, " +
                $"resume requested on {generation}");
        if (t.State is LongHorizonState.COMPLETED or
            LongHorizonState.CANCELLED)
            throw new ExecutorError(
                "TASK_TERMINAL", $"{taskId} is {t.State}");
        var d = t.ToDict();
        d["resumed"] = true;
        d["revalidated"] = true;
        d["context_rehydration"] = "record_only";  // never prompt-grown
        return d;
    }

    public static Dictionary<string, object?> RecordStep(
        string toolRoot, string taskId, string step, bool done,
        Dictionary<string, object?>? evidence = null)
    {
        var t = Load(toolRoot, taskId);
        if (done)
        {
            t.PendingSteps.Remove(step);
            t.CompletedSteps.Add(step);
        }
        else if (!t.PendingSteps.Contains(step))
        {
            t.PendingSteps.Add(step);
        }
        if (evidence != null) t.Evidence.Add(evidence);
        Save(toolRoot, t);
        return t.ToDict();
    }

    public static Dictionary<string, object?> RecordFailure(
        string toolRoot, string taskId, string failure)
    {
        var t = Load(toolRoot, taskId);
        t.FailureHistory.Add(new Dictionary<string, object?>
        {
            ["at"] = XcPaths.IsoNow(), ["failure"] = failure,
        });
        Save(toolRoot, t);
        return t.ToDict();
    }

    private static void Save(string toolRoot, LongHorizonTask t)
    {
        string path = TaskPath(toolRoot, t.TaskId);
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(t.ToDict()) + "\n");
    }
}

/// <summary>§10 star-code-task/v1 — the governed coding task contract.
/// Languages are closed to the formal set; task kinds are closed; the
/// pipeline is inspect -> plan -> propose -> static validate ->
/// compile/test (only when authorized) -> verify -> result. The runtime
/// proposes — it never gains new write or execution authority.</summary>
internal static class StarCodeAgentRuntime
{
    public const string Format = "star-code-task/v1";
    public const string ReportFormat = "star-repo-task-report/v1";

    public static readonly string[] Languages =
        { "c", "cpp", "csharp", "fsharp", "rust" };
    public static readonly string[] TaskKinds =
    {
        "completion", "fim", "single_file", "multi_file",
        "compile_fix", "test_fix", "bug_fix", "refactor",
        "repo_navigation", "code_review",
    };

    /// <summary>Validate a star-code-task/v1 request; fail-closed on an
    /// out-of-contract language, kind or missing goal.</summary>
    public static Dictionary<string, object?> ValidateTask(
        Dictionary<string, object?> task)
    {
        string Str(string k) => task.TryGetValue(k, out var v)
            ? v?.ToString() ?? "" : "";
        string lang = Str("language").ToLowerInvariant();
        if (!Languages.Contains(lang))
            throw new ExecutorError(
                ConvErr.LanguageBoundaryViolation,
                $"code task language '{lang}' not in formal set");
        string kind = Str("task_kind").ToLowerInvariant();
        if (kind.Length == 0) kind = Str("kind").ToLowerInvariant();
        if (!TaskKinds.Contains(kind))
            throw new ExecutorError(
                "CODE_TASK_KIND_INVALID",
                $"unknown code task kind '{kind}'");
        if (Str("goal").Length == 0)
            throw new ExecutorError(
                "CODE_TASK_GOAL_REQUIRED", "missing goal");
        var norm = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["language"] = lang,
            ["task_kind"] = kind,
            ["repository"] = Str("repository"),
            ["files"] = task.TryGetValue("files", out var f)
                ? f : new List<object?>(),
            ["goal"] = Str("goal"),
            ["constraints"] = task.TryGetValue("constraints", out var c)
                ? c : new List<object?>(),
            ["build_command"] = Str("build_command"),
            ["test_command"] = Str("test_command"),
            ["allowed_actions"] = task.TryGetValue(
                "allowed_actions", out var aa)
                ? aa : new List<object?> { "propose" },
            ["expected_artifacts"] = task.TryGetValue(
                "expected_artifacts", out var ea)
                ? ea : new List<object?>(),
        };
        return norm;
    }

    /// <summary>Pipeline stage runner: allowed_actions decides whether
    /// compile/test may execute at all. With only "propose" the harness
    /// stops after static validation — the same governed boundary the
    /// existing permission layer already enforces.</summary>
    public static Dictionary<string, object?> RunPipeline(
        Dictionary<string, object?> task,
        Func<string, Dictionary<string, object?>> inspect,
        Func<string, Dictionary<string, object?>> propose)
    {
        var norm = ValidateTask(task);
        var allowed = (norm["allowed_actions"] as List<object?>)
            ?.Select(x => x?.ToString() ?? "").ToList()
            ?? new List<string> { "propose" };
        bool mayExecute = allowed.Contains("compile_test");

        var report = RepoTaskHarness.Begin(norm);
        var steps = new List<object?>
        {
            Stage("inspect", inspect("inspect")),
            Stage("plan", propose("plan")),
            Stage("propose", propose("propose")),
            Stage("static_validate", propose("static_validate")),
        };
        RepoTaskHarness.RecordSteps(report, steps);
        if (mayExecute)
        {
            RepoTaskHarness.RecordSteps(report, new List<object?>
            {
                Stage("compile_test", new Dictionary<string, object?>
                {
                    ["authorized"] = true,
                    ["build_command"] = norm["build_command"],
                    ["test_command"] = norm["test_command"],
                }),
            });
        }
        else
        {
            report["execution"] = "skipped_unauthorized";
        }
        RepoTaskHarness.RecordSteps(report, new List<object?>
        {
            Stage("verify", new Dictionary<string, object?>
            {
                ["validated"] = true, ["authorized_run"] = mayExecute,
            }),
        });
        report["result"] = "proposed";
        return report;
    }

    private static object Stage(string name, object payload)
        => new Dictionary<string, object?>
        {
            ["stage"] = name, ["output"] = payload,
            ["at"] = XcPaths.IsoNow(),
        };
}

/// <summary>§22 RepoTaskHarness — the evidence record every coding task
/// produces; proposal-only by default (no permission widening).</summary>
internal static class RepoTaskHarness
{
    public static Dictionary<string, object?> Begin(
        Dictionary<string, object?> task) => new()
    {
        ["format"] = StarCodeAgentRuntime.ReportFormat,
        ["task"] = task,
        ["files_read"] = new List<object?>(),
        ["files_changed_proposed"] = new List<object?>(),
        ["compiler_output"] = "",
        ["test_output"] = "",
        ["tool_calls"] = new List<object?>(),
        ["iterations"] = 0,
        ["recovery_steps"] = new List<object?>(),
        ["final_validation"] = new Dictionary<string, object?>
        {
            ["validated"] = false,
        },
        ["started_at"] = XcPaths.IsoNow(),
    };

    public static void RecordSteps(
        Dictionary<string, object?> report, List<object?> steps)
    {
        if (report["tool_calls"] is List<object?> calls)
            calls.AddRange(steps);
        report["iterations"] =
            Convert.ToInt32(report["iterations"]) + steps.Count;
    }
}

/// <summary>§11 star-fim/v1 — fill-in-the-middle contract. The runtime
/// envelope text markers carry the structure when the tokenizer has no
/// dedicated FIM tokens; tokens are never retrained for this.</summary>
internal static class FimContract
{
    public const string Format = "star-fim/v1";
    // Runtime envelope — plain-text markers the engine treats as normal
    // tokens. Stable and versioned so a future tokenizer MAY upgrade
    // them to real control tokens without a contract change.
    public const string Pre = "<|fim_prefix|>";
    public const string Suf = "<|fim_suffix|>";
    public const string Mid = "<|fim_middle|>";

    public static readonly string[] RequiredFields =
        { "language", "file", "prefix", "suffix" };

    public static Dictionary<string, object?> Validate(
        Dictionary<string, object?> fim)
    {
        foreach (string key in RequiredFields)
            if (!fim.ContainsKey(key) || fim[key] is null)
                throw new ExecutorError(
                    "FIM_SCHEMA_INVALID", $"missing field: {key}");
        string lang = fim["language"]?.ToString() ?? "";
        if (!StarCodeAgentRuntime.Languages.Contains(
                lang.ToLowerInvariant()))
            throw new ExecutorError(
                ConvErr.LanguageBoundaryViolation,
                $"fim language '{lang}' not in formal set");
        var norm = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["language"] = lang.ToLowerInvariant(),
            ["file"] = fim["file"]?.ToString() ?? "",
            ["prefix"] = fim["prefix"]?.ToString() ?? "",
            ["suffix"] = fim["suffix"]?.ToString() ?? "",
            ["cursor"] = fim.TryGetValue("cursor", out var c)
                ? c : 0,
            ["imports"] = fim.TryGetValue("imports", out var i)
                ? i : new List<object?>(),
            ["related_files"] = fim.TryGetValue("related_files",
                out var rf) ? rf : new List<object?>(),
            ["constraints"] = fim.TryGetValue("constraints",
                out var ct) ? ct : new List<object?>(),
        };
        return norm;
    }

    /// <summary>The runtime envelope: what the engine actually sees.
    /// Marker text is deterministic and tokenizer-agnostic.</summary>
    public static string Envelope(Dictionary<string, object?> fim)
    {
        var n = Validate(fim);
        return $"{Pre}\n{n["prefix"]}\n{Suf}\n{n["suffix"]}\n{Mid}\n";
    }
}

/// <summary>§12 ModalityProvenance — per-request multimodal ledger; a
/// request that used vision must record exactly what entered the
/// stream.</summary>
internal static class ModalityProvenance
{
    public const string Format = "star-modality-provenance/v1";

    public static Dictionary<string, object?> Record(
        long textTokens, long visionPatchCount,
        string visionPatchSource, long visionBudget,
        List<long> fusionPositions, bool truncated,
        string modalityHash)
        => new()
        {
            ["format"] = Format,
            ["text_tokens"] = textTokens,
            ["vision_patch_count"] = visionPatchCount,
            ["vision_patch_source"] = visionPatchSource,
            ["vision_budget"] = visionBudget,
            ["fusion_positions"] =
                fusionPositions.Cast<object?>().ToList(),
            ["truncation"] = truncated,
            ["modality_hash"] = modalityHash,
            ["recorded_at"] = XcPaths.IsoNow(),
        };
}

/// <summary>§12 TeacherLineage — schema only this phase; the registry
/// exists so a future distillation can record lineage without a new
/// contract. No distillation executes.</summary>
internal static class TeacherLineage
{
    public const string Format = "star-teacher-lineage/v1";

    public static Dictionary<string, object?> Record(
        string teacherId, string teacherGeneration, string datasetId,
        string sampleHash, string verification, string license,
        string provenance)
        => new()
        {
            ["format"] = Format,
            ["teacher_id"] = teacherId,
            ["teacher_generation"] = teacherGeneration,
            ["dataset_id"] = datasetId,
            ["sample_hash"] = sampleHash,
            ["verification"] = verification,
            ["license"] = license,
            ["provenance"] = provenance,
            ["schema_only"] = true,   // §12: never executes distillation
        };
}
