// bootstrap-entry is registered migrate-csharp (A341/A610): the launch
// pipeline is owned by launcher/src/GPTBridge.Bootstrap (net10); this file
// stays the no-MSVC csc.exe fallback build of GPTBridgeLauncher.exe.
//
// History note (A217):
// - start.py replaces start.ps1 (called by C++ launcher)
// - install.py replaces install.ps1 (compiles C++ or C# fallback)
// - firewall_whitelist.py replaces firewall_whitelist.ps1
//
// Per A217: C# ONLY for Windows-specific .NET/CLR/WinRT/COM integration
// that cannot be provided by existing Python/TypeScript/C/C++ owner without-loss.
// The Python scripts provide identical functionality without requiring C#.
//
// Original content archived for reference only. Do not compile.
// EXCEPTION: install.py compiles this file with csc.exe as the no-MSVC
// fallback build of the desktop bootstrap.

// GPTBridgeLauncher.cs — C# fallback port of GPTBridgeLauncher.cpp.
//
// Minimal desktop bootstrap (same contract as the C++ launcher):
//   * GUI subsystem (no console window ever).
//   * Reads %LOCALAPPDATA%\GPTBridgeLauncher\config\root.txt.
//   * Launches <root>\launcher\bin\GPTBridge.Bootstrap.exe hidden, falling
//     back to <root>\launcher\scripts\start.py via the project venv
//     pythonw.exe until the C# bootstrap is installed everywhere.
// All launcher behaviour lives in start.py and updates in real time; this
// EXE is reinstalled only when the bootstrap contract itself changes.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
internal static class GPTBridgeLauncher
{
    private const string AppDisplayName = "專案程式庫";

    private const string MsgNotInstalled =
        "程式庫啟動器尚未安裝，請執行 launcher\\scripts\\install.py。";

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

        // Fallback: the retained Python launcher script via the project venv
        // interpreter, so the launcher never depends on machine-wide PATH.
        var launchScript = Path.Combine(
            projectRoot, "launcher", "scripts", "start.py");
        if (!File.Exists(launchScript))
        {
            ShowError(MsgMissing);
            return 1;
        }
        var interpreter = Path.Combine(
            projectRoot, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(interpreter))
        {
            interpreter = "pythonw.exe";
        }

        return LaunchHost(projectRoot, interpreter, Quote(launchScript));
    }
}