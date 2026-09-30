// CodeAgent.cs — §10/§11/§22 coding-agent contracts.
//
// Qwen3-Coder lesson: a formal star-code-task/v1 contract plus a
// staged pipeline (inspect -> plan -> propose -> static validate ->
// compile/test if authorized -> verify -> result). The coding lane
// keeps its existing permission boundary: proposals only, no new
// source-write or system-execution authority. DeepSeek Flash Coding
// lesson: RepoTaskHarness records the full attempt as evidence.
// CodeGemma lesson: star-fim/v1 FIM envelope — runtime wraps
// prefix/suffix with control tokens; the tokenizer is untouched.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CodeAgent
{
    public const string TaskFormat = "star-code-task/v1";
    public const string FimFormat = "star-fim/v1";
    public const string HarnessFormat = "star-repo-harness/v1";

    public static readonly string[] Languages =
        { "c", "cpp", "csharp", "fsharp", "rust" };
    public static readonly string[] TaskTypes =
        { "completion", "FIM", "single_file", "multi_file",
          "compile_fix", "test_fix", "bug_fix", "refactor",
          "repo_navigation", "code_review" };
    public static readonly string[] Stages =
        { "inspect", "plan", "propose", "static_validate",
          "compile_test", "verify", "result" };

    /// <summary>Validate a star-code-task/v1 request. Language outside
    /// the allowlist fails with LANGUAGE_BOUNDARY_VIOLATION.</summary>
    public static Dictionary<string, object?> ValidateTask(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "REPO_TASK_SCOPE_INVALID", "code task must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != TaskFormat)
            throw new ExecutorError(
                "REPO_TASK_SCOPE_INVALID",
                $"expected format {TaskFormat}");
        var required = new[]
            { "language", "repository", "files", "goal", "constraints",
              "build_command", "test_command", "allowed_actions",
              "expected_artifacts" };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "REPO_TASK_SCOPE_INVALID", $"missing field {k}");
        string lang = (el.GetProperty("language").GetString() ?? "")
            .ToLowerInvariant();
        if (!Languages.Contains(lang))
            throw new ExecutorError(
                "LANGUAGE_BOUNDARY_VIOLATION",
                $"language {lang} not in allowlist");
        string type = el.TryGetProperty("task_type", out var tt)
            ? tt.GetString() ?? "bug_fix" : "bug_fix";
        if (!TaskTypes.Contains(type))
            throw new ExecutorError(
                "REPO_TASK_SCOPE_INVALID", $"bad task_type {type}");
        if (el.GetProperty("files").ValueKind != JsonValueKind.Array)
            throw new ExecutorError(
                "REPO_TASK_SCOPE_INVALID", "files must be an array");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TaskFormat,
            ["language"] = lang, ["task_type"] = type,
        };
    }

    /// <summary>Validate a star-fim/v1 envelope. The envelope carries
    /// prefix/suffix/cursor; the C++ runtime materialises it into a
    /// prompt without touching tokenizer assets.</summary>
    public static Dictionary<string, object?> ValidateFim(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "FIM_CONTRACT_INVALID", "fim request must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != FimFormat)
            throw new ExecutorError(
                "FIM_CONTRACT_INVALID",
                $"expected format {FimFormat}");
        var required = new[]
            { "language", "file", "prefix", "suffix", "cursor",
              "imports", "related_files", "constraints" };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "FIM_CONTRACT_INVALID", $"missing field {k}");
        string lang = (el.GetProperty("language").GetString() ?? "")
            .ToLowerInvariant();
        if (!Languages.Contains(lang))
            throw new ExecutorError(
                "LANGUAGE_BOUNDARY_VIOLATION",
                $"language {lang} not in allowlist");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = FimFormat,
            ["language"] = lang,
            ["file"] = el.GetProperty("file").GetString(),
        };
    }

    /// <summary>Validate a RepoTaskHarness evidence record — the audit
    /// trail of one coding attempt (§22).</summary>
    public static Dictionary<string, object?> ValidateHarness(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "REPO_TASK_SCOPE_INVALID", "harness record not object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != HarnessFormat)
            throw new ExecutorError(
                "REPO_TASK_SCOPE_INVALID",
                $"expected format {HarnessFormat}");
        var required = new[]
            { "task_id", "files_read", "files_changed_proposed",
              "compiler_output", "test_output", "tool_calls",
              "iterations", "recovery_steps", "final_validation" };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "REPO_TASK_SCOPE_INVALID", $"missing field {k}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = HarnessFormat,
            ["task_id"] = el.GetProperty("task_id").GetString(),
            ["final_validation"] =
                el.TryGetProperty("final_validation", out var fv)
                    ? fv.GetString() : null,
        };
    }

    // -------------------------------------------------- governed run --

    /// <summary>StarCodeAgentRuntime (§10): the governed pipeline
    /// inspect → plan → propose → static_validate → compile/test (only
    /// when the task's allowed_actions authorizes it) → verify →
    /// result. Proposals only — the lane never writes source and never
    /// runs a command the task did not declare. Every attempt emits a
    /// star-repo-harness/v1 evidence record (§22).</summary>
    public static Dictionary<string, object?> Run(
        string toolRoot, string taskFile)
    {
        var task = ToolContracts.ReadJson(
            taskFile, "REPO_TASK_SCOPE_INVALID");
        var check = ValidateTask(task);
        string repo = task.GetProperty("repository").GetString() ?? "";
        var actions = task.GetProperty("allowed_actions")
            .EnumerateArray().Select(x => x.GetString() ?? "")
            .ToList();
        var files = task.GetProperty("files").EnumerateArray()
            .Select(x => x.GetString() ?? "")
            .Where(s => s.Length > 0).ToList();
        var stages = new List<object?>();
        void Stage(string name, string result, string detail = "")
            => stages.Add(new Dictionary<string, object?>
            {
                ["stage"] = name, ["result"] = result,
                ["detail"] = detail,
            });

        // inspect: bounded read — files stay inside the declared repo.
        var filesRead = new List<object?>();
        var missing = new List<object?>();
        foreach (var rel in files)
        {
            string full = Path.GetFullPath(Path.Combine(repo, rel));
            if (!full.StartsWith(
                    Path.GetFullPath(repo) + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase))
            {
                Stage("inspect", "fail",
                      $"path escapes repository: {rel}");
                missing.Add(rel);
                continue;
            }
            if (File.Exists(full)) filesRead.Add(rel);
            else missing.Add(rel);
        }
        Stage("inspect", missing.Count == 0 ? "pass" : "partial",
              $"{filesRead.Count} read, {missing.Count} missing");
        Stage("plan", "pass", "goal: " +
              (task.GetProperty("goal").GetString() ?? "")[..Math.Min(
                  160, (task.GetProperty("goal").GetString() ?? "")
                      .Length)]);
        Stage("propose", "pass",
              "proposal-only boundary — no source writes");

        // static_validate: only when authorized.
        if (actions.Contains("static_validate"))
        {
            var violations = files
                .Where(f => missing.Contains(f)).ToList();
            Stage("static_validate",
                  violations.Count == 0 ? "pass" : "fail",
                  violations.Count == 0
                      ? "scope clean"
                      : "unreadable: " + string.Join(",", violations));
        }
        else Stage("static_validate", "skipped", "not authorized");

        // compile/test: only the declared commands, only when
        // authorized — executed via the supervised NativeTools lane.
        string compilerOutput = "not authorized";
        string testOutput = "not authorized";
        bool compiled = false, tested = false;
        string logDir = Path.Combine(
            toolRoot, "xingcheng/runtime/logs");
        if (actions.Contains("compile"))
        {
            string cmd = task.GetProperty("build_command")
                .GetString() ?? "";
            if (cmd.Length == 0)
            {
                compilerOutput = "build_command empty";
                Stage("compile_test", "fail", compilerOutput);
            }
            else
            {
                var rr = NativeTools.Run(
                    "cmd.exe",
                    new[] { "/c", cmd }, repo,
                    Path.Combine(logDir, "code-agent-stderr.log"),
                    timeoutS: 900);
                compilerOutput =
                    $"exit={rr.ExitCode} {rr.StdoutTail}";
                compiled = rr.ExitCode == 0;
                Stage("compile_test", compiled ? "pass" : "fail",
                      compilerOutput[..Math.Min(240,
                          compilerOutput.Length)]);
            }
        }
        else Stage("compile_test", "skipped", "compile not authorized");
        if (actions.Contains("test"))
        {
            string cmd = task.GetProperty("test_command")
                .GetString() ?? "";
            if (cmd.Length == 0)
                testOutput = "test_command empty";
            else
            {
                var rr = NativeTools.Run(
                    "cmd.exe",
                    new[] { "/c", cmd }, repo,
                    Path.Combine(logDir, "code-agent-stderr.log"),
                    timeoutS: 900);
                testOutput = $"exit={rr.ExitCode} {rr.StdoutTail}";
                tested = rr.ExitCode == 0;
            }
        }

        bool verified = missing.Count == 0 &&
            (!actions.Contains("compile") || compiled) &&
            (!actions.Contains("test") || tested);
        Stage("verify", verified ? "pass" : "fail");
        Stage("result", verified ? "success" : "failure");

        var harness = new Dictionary<string, object?>
        {
            ["format"] = HarnessFormat,
            ["task_id"] = check["task_type"]?.ToString() ?? "task",
            ["task_file"] = taskFile,
            ["language"] = check["language"],
            ["files_read"] = filesRead,
            ["files_changed_proposed"] = new List<object?>(),
            ["compiler_output"] = compilerOutput,
            ["test_output"] = testOutput,
            ["tool_calls"] = new List<object?>(),
            ["iterations"] = 1,
            ["recovery_steps"] = missing.Cast<object?>().ToList(),
            ["final_validation"] = verified ? "pass" : "fail",
            ["stages"] = stages,
            ["recorded_at"] = XcPaths.IsoNow(),
        };
        harness["ok"] = verified;
        return harness;
    }
}
