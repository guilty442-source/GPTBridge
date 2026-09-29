// bootstrap-entry is registered migrate-csharp (A341/A610): the launch
// pipeline is owned by launcher/src/GPTBridge.Bootstrap (net10); this file
// stays the no-MSVC csc.exe fallback build of GPTBridgeLauncher.exe.
//
// History note (B167/B38): the start.py / install.py /
// firewall_whitelist.py lanes are retired with the Python runtime;
// this file is compiled by the native self-install path
// (GPTBridge.Bootstrap.exe --install-desktop, csc.exe fallback).
//
// Per A217: C# ONLY for Windows-specific .NET/CLR/WinRT/COM integration.
// The --install-desktop lane compiles this file with csc.exe as the
// no-MSVC fallback build of the desktop bootstrap.

// GPTBridgeLauncher.cs — C# fallback port of GPTBridgeLauncher.cpp.
//
// Minimal desktop bootstrap (same contract as the C++ launcher):
//   * GUI subsystem (no console window ever).
//   * Reads %LOCALAPPDATA%\GPTBridgeLauncher\config\root.txt.
//   * Launches <root>\launcher\bin\GPTBridge.Bootstrap.exe hidden; the
//     Python start.py fallback is retired (B167/B38) — a missing
//     bootstrap is reported as an install problem.
// All launcher behaviour lives in the bootstrap entry and updates in real
// time; this EXE is reinstalled only when the bootstrap contract itself
// changes, via GPTBridge.Bootstrap.exe --install-desktop (the native
// self-install lane that replaced install.py).
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
internal static class GPTBridgeLauncher
{
    private const string AppDisplayName = "專案程式庫";

    private const string MsgNotInstalled =
        "程式庫啟動器尚未安裝，請執行 launcher\\bin\\GPTBridge.Bootstrap.exe --install-desktop。";

    private const string MsgMissing =
        "找不到程式庫或啟動模組，請重新安裝啟動器。";

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static string ReadRootFile(string rootFile)
    {
        try
        {
            var bytes = File.ReadAllBytes(rootFile);
            if (bytes.Length > 1048576) return "";
            var text = Encoding.UTF8.GetString(bytes);
            if (text.Length > 0 && text[0] == '\ufeff')
            {
                text = text.Substring(1);
            }
            return text.Trim();
        }
        catch (IOException)
        {
            return "";
        }
        catch (UnauthorizedAccessException)
        {
            return "";
        }
    }

    private static void ShowError(string message)
    {
        try
        {
            var localAppData = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            var logDir = Path.Combine(localAppData, "GPTBridgeLauncher", "logs");
            Directory.CreateDirectory(logDir);
            var logPath = Path.Combine(logDir, "launcher.log");
            var line = string.Format(
                "[{0:yyyy-MM-dd HH:mm:ss.fff}] {1} ERROR: {2}",
                DateTime.Now, AppDisplayName, message);
            File.AppendAllText(logPath, line + Environment.NewLine, Encoding.UTF8);
        }
        catch
        {
        }
    }

    private static int LaunchHost(
        string projectRoot, string target, string arguments)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = target,
            Arguments = arguments,
            WorkingDirectory = projectRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        startInfo.EnvironmentVariables.Remove("ELECTRON_RUN_AS_NODE");

        try
        {
            using (Process.Start(startInfo))
            {
            }
            return 0;
        }
        catch (Exception error)
        {
            ShowError(error.Message);
            return 1;
        }
    }

    [STAThread]
    private static int Main()
    {
        var localAppData = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (string.IsNullOrEmpty(localAppData))
        {
            ShowError(MsgNotInstalled);
            return 1;
        }

        var rootFile = Path.Combine(
            localAppData, "GPTBridgeLauncher", "config", "root.txt");
        if (!File.Exists(rootFile))
        {
            ShowError(MsgNotInstalled);
            return 1;
        }

        var projectRoot = ReadRootFile(rootFile);
        if (projectRoot.Length == 0)
        {
            ShowError(MsgNotInstalled);
            return 1;
        }

        // Primary: the C# bootstrap entry (migrate-csharp owner).
        var bootstrap = Path.Combine(
            projectRoot, "launcher", "bin", "GPTBridge.Bootstrap.exe");
        if (File.Exists(bootstrap))
        {
            return LaunchHost(projectRoot, bootstrap, "");
        }

        // No fallback: the Python start.py lane is retired (B167/B38). A
        // missing bootstrap is an install problem — report it honestly.
        ShowError(MsgMissing);
        return 1;
    }
}