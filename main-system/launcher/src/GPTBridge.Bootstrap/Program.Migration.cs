using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    private static JsonObject RunMigrationAutostart()
    {
        // The governed MigrationRunner owns the report + optional apply;
        // the entry only invokes it. Fail-open: startup never blocks here.
        var result = new JsonObject
        {
            ["history_rows"] = "-",
            ["forward_count"] = "-",
            ["legacy_gap_count"] = "-",
            ["applied_count"] = "-",
            ["error"] = "-",
        };
        try
        {
            var interpreter = ResolvePythonInterpreter(preferWindowed: false);
            if (interpreter is null)
            {
                result["error"] = "PYTHON_UNAVAILABLE";
                return result;
            }
            var scriptsDir = Path.Combine(LauncherRoot, "scripts");
            // The governed runner resolves shared-layer against the
            // WORKSPACE root (the Python launcher passed project_root —
            // main-system — so its autostart never actually ran; that is
            // a latent upstream bug this port corrects).
            var code = "import sys;"
                + "sys.path.insert(0, sys.argv[2]);"
                + "sys.path.insert(0, sys.argv[3]);"
                + "from migration_autostart import run;"
                + "import json;print(json.dumps(run(sys.argv[1])))";
            var startInfo = new ProcessStartInfo
            {
                FileName = interpreter,
                WorkingDirectory = scriptsDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            startInfo.ArgumentList.Add("-c");
            startInfo.ArgumentList.Add(code);
            startInfo.ArgumentList.Add(WorkspaceRoot);
            startInfo.ArgumentList.Add(scriptsDir);
            // workspace root itself — governed packages (governance_rule
            // et al.) import under their workspace-relative names.
            startInfo.ArgumentList.Add(WorkspaceRoot);
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                result["error"] = "SPAWN_FAILED";
                return result;
            }
            var stdout = process.StandardOutput.ReadToEnd();
            process.StandardError.ReadToEnd();
            process.WaitForExit(120_000);

            var line = stdout.Trim().Split('\n')
                .LastOrDefault(l => l.TrimStart().StartsWith('{'));
            if (line is null)
            {
                result["error"] = "NO_REPORT";
                return result;
            }
            var report = JsonNode.Parse(line.Trim())?.AsObject();
            if (report is null)
            {
                result["error"] = "REPORT_UNPARSEABLE";
                return result;
            }
            result["history_rows"] = report["history_rows"]?.GetValue<int>()
                .ToString() ?? "0";
            result["forward_count"] = (report["forward_pending"]
                as JsonArray)?.Count.ToString() ?? "0";
            result["legacy_gap_count"] = report["legacy_gap_count"]
                ?.GetValue<int>().ToString() ?? "0";
            result["applied_count"] = (report["applied"] as JsonArray)
                ?.Count.ToString() ?? "0";
            result["error"] = report["error"]?.GetValue<string>();
            if (string.IsNullOrEmpty(result["error"]?.GetValue<string>()))
            {
                result["error"] = "-";
            }
        }
        catch (Exception error)
        {
            result["error"] = $"{error.GetType().Name}: {error.Message}";
        }
        return result;
    }

    // ------------------------------------------------------------------
    // desktop launcher refresh
    // ------------------------------------------------------------------

}
