// CodexDiagnostics — governed subprocess bridge to the two read-only
// Codex diagnostic verbs (successor of the retired Python
// codex_diagnostics.py lane). Both verbs are read-only:
//   --arch-docs     architecture registry × architecture-*.md
//                   completeness/stale-identifier report (filesystem)
//   --mirror-check  live zh-TW five-part mirror validation against the
//                   authority (chain/hash/parity/replacement damage)
// The pipeline binary is the governed CodexPipeline; a missing binary
// or non-zero exit reports CODEX_PIPELINE_* honestly, never silently.
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal static class CodexDiagnostics
{
    private static readonly TimeSpan DiagnosticTimeout =
        TimeSpan.FromSeconds(60);

    private static string ResolvePipelineExe(string projectRoot)
    {
        var candidates = new[]
        {
            Path.Combine(projectRoot, "shared-layer", "csharp",
                "GPTBridge.CodexPipeline", "publish",
                "GPTBridge.CodexPipeline.exe"),
            Path.Combine(projectRoot, "shared-layer", "csharp",
                "GPTBridge.CodexPipeline", "bin", "Release", "net10.0",
                "GPTBridge.CodexPipeline.exe"),
        };
        foreach (var path in candidates)
            if (File.Exists(path)) return path;
        return candidates[0];
    }

    /// <summary>Run one read-only CodexPipeline verb; returns the
    /// parsed stdout JSON object, or a fail-closed error payload when
    /// the governed binary is unavailable/unparseable.</summary>
    public static async Task<JsonObject> RunAsync(
        GovernedEnvironment env, string verb, CancellationToken ct)
    {
        var exe = ResolvePipelineExe(env.ProjectRoot);
        if (!File.Exists(exe))
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "CODEX_PIPELINE_UNAVAILABLE",
                ["message"] = exe,
            };
        var start = new ProcessStartInfo(exe, verb)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        try
        {
            using var process = Process.Start(start)
                ?? throw new InvalidOperationException("spawn failed");
            using var timeout = CancellationTokenSource
                .CreateLinkedTokenSource(ct);
            timeout.CancelAfter(DiagnosticTimeout);
            var stdout = await process.StandardOutput
                .ReadToEndAsync(timeout.Token).ConfigureAwait(false);
            await process.WaitForExitAsync(timeout.Token)
                .ConfigureAwait(false);
            var parsed = JsonNode.Parse(stdout) as JsonObject;
            if (parsed is null)
                return new JsonObject
                {
                    ["ok"] = false,
                    ["error_code"] = "CODEX_PIPELINE_BAD_OUTPUT",
                };
            if (process.ExitCode != 0)
            {
                parsed["ok"] = false;
                parsed["error_code"] ??= "CODEX_PIPELINE_FAILED";
            }
            return parsed;
        }
        catch (Exception ex)
        {
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "CODEX_PIPELINE_FAILED",
                ["message"] = ex.Message.Length > 200
                    ? ex.Message[..200] : ex.Message,
            };
        }
    }

    /// <summary>Fold a pipeline verb's report into a checks list —
    /// the individual sub-findings surface as named rows so the
    /// dialogue UI keeps its PASS/FAIL rendering.</summary>
    public static void AppendChecks(
        JsonArray checks, StringBuilder report,
        string prefix, JsonObject result)
    {
        void Check(string name, bool ok, string detail)
        {
            checks.Add(new JsonObject
            {
                ["name"] = name, ["ok"] = ok, ["detail"] = detail,
            });
            report.AppendLine(
                $"{(ok ? "PASS" : "FAIL")} {name}：{detail}");
        }

        if (result["ok"]?.GetValue<bool>() != true
            && result["error_code"] is not null)
        {
            Check(prefix, false,
                result["error_code"]?.GetValue<string>()
                    ?? "CODEX_PIPELINE_FAILED");
            return;
        }
        // --mirror-check emits errors[]; --arch-docs emits errors[],
        // gaps[], unreferenced_canonical[] and per-document
        // stale_identifiers — fold each into a named row.
        var rows = 0;
        void ErrorsRow(string name, JsonNode? node)
        {
            if (node is not JsonArray errors) return;
            ++rows;
            Check(name, errors.Count == 0,
                errors.Count == 0
                    ? "0"
                    : $"{errors.Count}: {errors[0]}");
        }
        ErrorsRow(prefix + ":errors", result["errors"]);
        ErrorsRow(prefix + ":coverage-gaps", result["gaps"]);
        ErrorsRow(prefix + ":unreferenced-canonical",
            result["unreferenced_canonical"]);
        if (result["documents"] is JsonArray docs)
        {
            var stale = 0;
            foreach (var doc in docs)
                if (doc?["stale_identifiers"] is JsonArray si)
                    stale += si.Count;
            ++rows;
            Check(prefix + ":stale-identifiers", stale == 0,
                $"{stale} across {docs.Count} documents");
        }
        if (rows == 0)
            Check(prefix,
                result["ok"]?.GetValue<bool>() == true,
                result["ok"]?.GetValue<bool>() == true
                    ? "ok" : "check failed");
    }
}
