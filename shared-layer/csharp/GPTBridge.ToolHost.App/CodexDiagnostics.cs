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
using System.Text;
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
        if (verb is not ("--arch-docs" or "--mirror-check"))
            return Error("CODEX_DIAGNOSTIC_VERB_DENIED");
        var exe = ResolvePipelineExe(env.ProjectRoot);
        if (!File.Exists(exe))
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "CODEX_PIPELINE_UNAVAILABLE",
                ["message"] = exe,
            };
        var start = new ProcessStartInfo(exe)
        {
            WorkingDirectory = env.ProjectRoot,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        start.ArgumentList.Add(verb);
        start.ArgumentList.Add("--root");
        start.ArgumentList.Add(env.ProjectRoot);
        return await RunProcessAsync(start, DiagnosticTimeout, ct)
            .ConfigureAwait(false);
    }

    private static JsonObject Error(string code) => new()
    {
        ["ok"] = false, ["error_code"] = code,
    };

    internal static async Task<JsonObject> RunProcessAsync(
        ProcessStartInfo start, TimeSpan budget, CancellationToken ct)
    {
        Process? process = null;
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(budget);
        try
        {
            timeout.Token.ThrowIfCancellationRequested();
            process = Process.Start(start)
                ?? throw new InvalidOperationException("spawn failed");
            // Drain both pipes concurrently: a full stderr pipe can block
            // the native entry before it writes or closes stdout.
            var stdout = process.StandardOutput.ReadToEndAsync(timeout.Token);
            var stderr = process.StandardError.ReadToEndAsync(timeout.Token);
            await Task.WhenAll(stdout, stderr, process.WaitForExitAsync(timeout.Token))
                .ConfigureAwait(false);
            JsonObject? parsed;
            try { parsed = JsonNode.Parse(await stdout.ConfigureAwait(false)) as JsonObject; }
            catch (JsonException) { parsed = null; }
            if (parsed is null)
                return Error(process.ExitCode == 0
                    ? "CODEX_PIPELINE_BAD_OUTPUT" : "CODEX_PIPELINE_FAILED");
            if (process.ExitCode != 0)
            {
                parsed["ok"] = false;
                parsed["error_code"] ??= "CODEX_PIPELINE_FAILED";
            }
            return parsed;
        }
        catch (OperationCanceledException)
        {
            return Error(ct.IsCancellationRequested
                ? "CODEX_PIPELINE_CANCELLED" : "CODEX_PIPELINE_TIMEOUT");
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
        finally
        {
            if (process is not null)
            {
                try
                {
                    if (!process.HasExited)
                    {
                        process.Kill(entireProcessTree: true);
                        using var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                        await process.WaitForExitAsync(cleanup.Token).ConfigureAwait(false);
                    }
                }
                catch (InvalidOperationException) { /* process already exited */ }
                finally { process.Dispose(); }
            }
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

        if (result["ok"] is not JsonValue status
            || !status.TryGetValue<bool>(out var success) || !success)
        {
            Check(prefix, false,
                result["error_code"]?.ToString()
                    ?? "CODEX_PIPELINE_FAILED");
            return;
        }
        // --mirror-check emits errors[]; --arch-docs emits errors[],
        // gaps[], unreferenced_canonical[] and per-document
        // stale_identifiers — fold each into a named row.
        var rows = 0;
        void ErrorsRow(string name, JsonNode? node)
        {
            if (node is null) return;
            ++rows;
            if (node is not JsonArray errors)
            {
                Check(name, false, "CODEX_PIPELINE_BAD_OUTPUT");
                return;
            }
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
            Check(prefix, false, "CODEX_DIAGNOSTIC_EVIDENCE_MISSING");
    }
}
