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
}
