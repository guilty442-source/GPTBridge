using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    // ------------------------------------------------------------------
    // desktop host selection (A618/A621/A625: Electron retired)
    // ------------------------------------------------------------------

    /// <summary>
    /// Resolve the desktop host for this launch.  Precedence:
    /// <c>--ui-host</c> arg → <c>GPTBRIDGE_UI_HOST</c> env →
    /// <c>launcher/state/ui-host.json</c>.  Tauri is the only remaining
    /// host — a stale "electron" marker or any other value resolves to
    /// "tauri" and is journaled as a coercion.
    /// </summary>
    private static string ResolveUiHost(string uiHostArg)
    {
        var raw = uiHostArg;
        if (string.IsNullOrWhiteSpace(raw))
        {
            raw = Environment.GetEnvironmentVariable("GPTBRIDGE_UI_HOST");
        }
        if (string.IsNullOrWhiteSpace(raw))
        {
            try
            {
                var payload = JsonNode.Parse(
                    File.ReadAllText(
                        Path.Combine(StateRoot, "ui-host.json")))?.AsObject();
                raw = payload?["host"]?.GetValue<string>();
            }
            catch (Exception)
            {
                // Missing/invalid marker — the Tauri default applies.
            }
        }
        var host = (raw ?? "").Trim().ToLowerInvariant();
        if (host != "tauri")
        {
            if (!string.IsNullOrEmpty(host))
            {
                WriteStartupJournal("launcher.ui-host.coerced",
                    new JsonObject { ["requested"] = host });
            }
            host = "tauri";
        }
        return host;
    }

    private static int LaunchTauriHost()
    {
        var shellExe = Path.Combine(
            ProjectRoot, "src-tauri", "target", "release",
            "gptbridge-shell.exe");
        if (!File.Exists(shellExe))
        {
            // Electron is retired — there is no fallback host.  Fail
            // honestly: the shell binary must be rebuilt
            // (cargo build --release in src-tauri).
            WriteLauncherStatus(
                "gptbridge-shell.exe not found; no fallback host remains "
                + "(Electron retired). Rebuild src-tauri first.");
            WriteStartupJournal("launcher.tauri.missing",
                new JsonObject { ["missing"] = shellExe });
            return 1;
        }

        Environment.SetEnvironmentVariable("GPTBRIDGE_SOURCE_PRODUCTION", "1");
        Environment.SetEnvironmentVariable("GPTBRIDGE_MANAGE_BACKEND", "1");
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_WORKSPACE_ROOT", WorkspaceRoot);
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_PROJECT_ROOT", WorkspaceRoot);

        WriteLauncherStatus("Launching Tauri desktop host (gptbridge-shell).");
        WriteStartupJournal("launcher.phase.tauri.start", null);

        var process = StartHiddenProcess(
            shellExe, Array.Empty<string>(), ProjectRoot);
        WaitForEarlyExit(process);

        if (process.HasExited)
        {
            if (process.ExitCode != 0)
            {
                WriteStartupJournal("launcher.tauri.exited",
                    new JsonObject { ["code"] = process.ExitCode });
                // A dead shell means no UI at all — there is no fallback
                // host (Electron retired); report the failure honestly.
                WriteLauncherStatus(
                    $"gptbridge-shell exited ({process.ExitCode}); "
                    + "no fallback host remains (Electron retired).");
                return process.ExitCode != 0 ? process.ExitCode : 1;
            }
            WriteLauncherStatus(
                "Tauri shell handed off to the running instance.");
            WriteStartupJournal("launcher.tauri.handoff", null);
        }
        else
        {
            WriteLauncherStatus(
                $"Tauri shell startup accepted. PID={process.Id}");
            WriteStartupJournal("launcher.tauri.accepted",
                new JsonObject { ["pid"] = process.Id });
        }

        WriteLauncherStatus(
            "Main system UI launched; gptbridge-shell is supervising "
            + "the governed backend.");
        return 0;
    }

    /// Poll for an early exit so a host that crashes during startup is
    /// caught truthfully — returns as soon as the process exits; a healthy
    /// long-running host waits the full deadline (3 s vs the original
    /// fixed 800 ms, which missed crashes past that window).
    private static void WaitForEarlyExit(Process process)
    {
        var deadline = DateTime.UtcNow.AddSeconds(3);
        while (DateTime.UtcNow < deadline)
        {
            if (process.WaitForExit(250))
            {
                return;
            }
        }
    }

    // ------------------------------------------------------------------
    // environment
    // ------------------------------------------------------------------

}
