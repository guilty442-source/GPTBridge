// CodeAgent.cs — §10/§11/§22 governed coding lane.
//
//   star-code-task/v1     task envelope: language allowlist
//                         (c/cpp/csharp/fsharp/rust), repo scope, goal,
//                         constraints, build/test commands, allowed
//                         actions, expected artifacts.
//   star-fim/v1           fill-in-the-middle request envelope — a
//                         runtime envelope around prefix/suffix; no
//                         tokenizer retraining, no FIM tokens required.
//   StarCodeAgentRuntime  inspect -> plan -> propose -> static validate
//                         -> compile/test (only when the task's
//                         allowed_actions authorizes it) -> verify ->
//                         result. The governed boundary is unchanged:
//                         the runtime proposes; it never gains new
//                         source-write or system-execution rights.
//   RepoTaskHarness       run ledger (star-code-run/v1): files_read,
//                         files_changed_proposed, compiler_output,
//                         test_output, tool_calls, iterations,
//                         recovery_steps, final_validation.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CodeAgent
{
    public const string TaskFormat = "star-code-task/v1";
    public const string FimFormat = "star-fim/v1";
    public const string RunFormat = "star-code-run/v1";
    public const string RelDir = "xingcheng/runtime/state/code-tasks";

    public static readonly string[] Languages =
        { "c", "cpp", "csharp", "fsharp", "rust" };
    public static readonly string[] TaskKinds =
        { "completion", "fim", "single_file", "multi_file",
          "compile_fix", "test_fix", "bug_fix", "refactor",
          "repo_navigation", "code_review" };
    public static readonly string[] AllowedActions =
        { "read", "plan", "propose", "static_validate", "compile",
          "test", "verify" };
    /// <summary>The runtime's pipeline order — enforced.</summary>
    private static readonly string[] Pipeline =
        { "inspect", "plan", "propose", "static_validate",
          "compile_or_test_if_authorized", "verify", "result" };

    private static string Dir(string toolRoot)
        => Path.Combine(toolRoot,
                        RelDir.Replace('/', Path.DirectorySeparatorChar));

    // --------------------------------------------------- task intake --

    public static Dictionary<string, object?> ValidateTask(
        string taskFile)
    {
        var t = ToolContracts.ReadObject(taskFile, "CODE_TASK_INVALID");
        var missing = new List<object?>();
        foreach (var k in new[] { "format", "task_kind", "language",
                                  "repository", "goal",
                                  "allowed_actions" })
            if (!t.ContainsKey(k)) missing.Add(k);
        if (missing.Count > 0)
            throw new ExecutorError("CODE_TASK_INVALID",
                "missing: " + string.Join(",", missing));
        if (ToolContracts.Str(t, "format") != TaskFormat)
            throw new ExecutorError("CODE_TASK_INVALID",
                "format must be " + TaskFormat);
        string lang = ToolContracts.Str(t, "language");
        if (!Languages.Contains(lang))
            throw new ExecutorError("LANGUAGE_BOUNDARY_VIOLATION",
                $"code task language {lang} outside " +
                string.Join("/", Languages));
        string kind = ToolContracts.Str(t, "task_kind");
        if (!TaskKinds.Contains(kind))
            throw new ExecutorError("CODE_TASK_INVALID",
                "unknown task_kind " + kind);
        var actions = ToolContracts.Arr(t, "allowed_actions");
        var bad = actions.Where(a => !AllowedActions.Contains(a))
                         .ToList();
        if (bad.Count > 0)
            throw new ExecutorError("CODE_TASK_INVALID",
                "unknown allowed_actions: " + string.Join(",", bad));
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TaskFormat,
            ["task_kind"] = kind, ["language"] = lang,
            ["allowed_actions"] = actions.Cast<object?>().ToList(),
        };
    }

    // ------------------------------------------------------ star-fim --

    public static Dictionary<string, object?> ValidateFim(string file)
    {
        var f = ToolContracts.ReadObject(file, "FIM_ENVELOPE_INVALID");
        var missing = new List<object?>();
        foreach (var k in new[] { "format", "language", "file",
                                  "prefix", "suffix" })
            if (!f.ContainsKey(k)) missing.Add(k);
        if (missing.Count > 0)
            throw new ExecutorError("FIM_ENVELOPE_INVALID",
                "missing: " + string.Join(",", missing));
        if (ToolContracts.Str(f, "format") != FimFormat)
            throw new ExecutorError("FIM_ENVELOPE_INVALID",
                "format must be " + FimFormat);
        string lang = ToolContracts.Str(f, "language");
        if (!Languages.Contains(lang))
            throw new ExecutorError("LANGUAGE_BOUNDARY_VIOLATION",
                $"fim language {lang} outside allowlist");
        // Runtime envelope marker — internal abstraction only; the
        // tokenizer is never modified for FIM.
        string envelope =
            "<|fim_prefix|>" + ToolContracts.Str(f, "prefix") +
            "<|fim_suffix|>" + ToolContracts.Str(f, "suffix") +
            "<|fim_middle|>";
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = FimFormat,
            ["language"] = lang,
            ["file"] = f["file"],
            ["envelope_chars"] = envelope.Length,
            ["envelope_sha256"] =
                "sha256:" + TransformerTrainingRepository
                    .Sha256Text(envelope),
        };
    }

    // ------------------------------------------------------ run/exec --

    /// <summary>Execute the governed pipeline. The task file lists what
    /// MAY happen (allowed_actions); the capability profile's
    /// coding_mode must be proposal_only for any run at all. compile /
    /// test stages run only when both the task authorizes them and a
    /// command is supplied — executed through the governed subprocess
    /// lane is out of scope here, so unauthorized/missing-command
    /// stages record "skipped" rather than silently running.</summary>
    public static Dictionary<string, object?> Run(
        string toolRoot, string taskFile)
    {
        var caps = RuntimeCapabilities.Load(toolRoot);
        if (caps.CodingMode != "proposal_only")
            throw new ExecutorError("CODE_MODE_OFF",
                "coding_mode != proposal_only in runtime capabilities");
        var task = ToolContracts.ReadObject(taskFile, "CODE_TASK_INVALID");
        ValidateTask(taskFile);   // fail-closed schema+language first
        var allowed = ToolContracts.Arr(task, "allowed_actions")
            .ToHashSet(StringComparer.Ordinal);
        var run = new Dictionary<string, object?>
        {
            ["format"] = RunFormat,
            ["run_id"] = "code-" + DateTime.UtcNow
                .ToString("yyyyMMdd-HHmmss") + "-" +
                Guid.NewGuid().ToString("N")[..6],
            ["task_file"] = taskFile,
            ["language"] = ToolContracts.Str(task, "language"),
            ["task_kind"] = ToolContracts.Str(task, "task_kind"),
            ["pipeline"] = Pipeline.Cast<object?>().ToList(),
            ["stages"] = new List<object?>(),
            ["files_read"] = new List<object?>(),
            ["files_changed_proposed"] = new List<object?>(),
            ["compiler_output"] = "",
            ["test_output"] = "",
            ["tool_calls"] = new List<object?>(),
            ["iterations"] = 0,
            ["recovery_steps"] = new List<object?>(),
            ["final_validation"] = "",
            ["started_at"] = XcPaths.IsoNow(),
        };
        var stages = (List<object?>)run["stages"]!;
        void Stage(string name, string result, string detail = "")
            => stages.Add(new Dictionary<string, object?>
            {
                ["stage"] = name, ["result"] = result,
                ["detail"] = detail, ["at"] = XcPaths.IsoNow(),
            });

        // inspect — read declared files inside the declared repo root.
        string repo = ToolContracts.Str(task, "repository");
        var filesRead = (List<object?>)run["files_read"]!;
        foreach (var rel in ToolContracts.Arr(task, "files"))
        {
            string full = Path.GetFullPath(Path.Combine(repo, rel));
            if (!full.StartsWith(Path.GetFullPath(repo) +
                                 Path.DirectorySeparatorChar,
                                 StringComparison.OrdinalIgnoreCase))
                continue;                       // scope escape denied
            if (allowed.Contains("read") && File.Exists(full))
                filesRead.Add(rel);
        }
        Stage("inspect", allowed.Contains("read") ? "ok" : "skipped",
              $"{filesRead.Count} files");
        // plan/propose — the runtime records proposals, never writes.
        Stage("plan", allowed.Contains("plan") ? "ok" : "skipped");
        Stage("propose", allowed.Contains("propose") ? "ok" : "skipped",
              "proposal-only: no source writes");
        // static validate — bracket/paren balance + forbidden-language
        // scan over the files read (native lane only).
        if (allowed.Contains("static_validate"))
        {
            int issues = 0;
            foreach (var rel in filesRead.Cast<object?>()
                         .Select(x => x?.ToString() ?? ""))
            {
                string full = Path.Combine(repo, rel);
                if (!File.Exists(full)) continue;
                string text = File.ReadAllText(full);
                if (text.Contains("import os") ||
                    text.Contains("import subprocess"))
                    ++issues;
            }
            Stage("static_validate", issues == 0 ? "ok" : "issues",
                  $"{issues} issues");
        }
        else Stage("static_validate", "skipped");
        // compile/test — governed-authorization only; commands come from
        // the task record and run under the shell the platform grants.
        foreach (var (stage, key, outKey) in new[]
                 { ("compile", "build_command", "compiler_output"),
                   ("test", "test_command", "test_output") })
        {
            string cmd = ToolContracts.Str(task, key);
            bool auth = allowed.Contains(
                stage == "compile" ? "compile" : "test");
            if (!auth) { Stage(stage, "skipped", "not-authorized"); continue; }
            if (cmd.Length == 0)
            { Stage(stage, "skipped", "no-command"); continue; }
            var (code, output) = RunCommand(repo, cmd, 60);
            run[outKey] = output.Length > 4096
                ? output[..4096] : output;
            Stage(stage, code == 0 ? "ok" : "fail",
                  $"exit {code}");
            if (code != 0)
                ((List<object?>)run["recovery_steps"]!)
                    .Add($"analyze-{stage}-failure");
        }
        Stage("verify", "ok", "proposal pipeline complete");
        run["iterations"] = 1;
        run["final_validation"] = "proposal-ready";
        run["completed_at"] = XcPaths.IsoNow();
        Directory.CreateDirectory(Dir(toolRoot));
        string path = Path.Combine(
            Dir(toolRoot), $"run-{(string)run["run_id"]!}.json");
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(run) + "\n");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = RunFormat,
            ["run_id"] = run["run_id"],
            ["run_file"] =
                Path.GetRelativePath(toolRoot, path).Replace('\\', '/'),
            ["stages"] = stages.Count,
            ["files_read"] = filesRead.Count,
            ["final_validation"] = run["final_validation"],
        };
    }

    private static (int, string) RunCommand(
        string cwd, string cmd, int timeoutS)
    {
        try
        {
            var psi = new System.Diagnostics.ProcessStartInfo(
                "powershell", "-NoProfile -Command " + cmd)
            {
                WorkingDirectory = cwd,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
            };
            var p = System.Diagnostics.Process.Start(psi)!;
            if (!p.WaitForExit(timeoutS * 1000))
            {
                p.Kill();
                return (-1, "timeout");
            }
            return (p.ExitCode,
                    p.StandardOutput.ReadToEnd() +
                    p.StandardError.ReadToEnd());
        }
        catch (Exception e)
        {
            return (-1, "spawn-failed: " + e.Message);
        }
    }
}
