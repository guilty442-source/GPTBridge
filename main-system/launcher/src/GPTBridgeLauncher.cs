// GPTBridgeLauncher.cs — C# port of GPTBridgeLauncher.cpp.
//
// Compiled by install.ps1 with csc.exe (.NET Framework) when MSVC is
// unavailable.  Behaviour must mirror the C++ launcher exactly:
//   * GUI subsystem (no console window ever).
//   * Reads %LOCALAPPDATA%\GPTBridgeLauncher\config\root.txt.
//   * Launches <root>\launcher\scripts\start.ps1 via powershell.exe with
//     CreateNoWindow so no console window flashes or lingers.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class GPTBridgeLauncher
{
    private const string AppDisplayName = "程式庫";

    private const string MsgNotInstalled =
        "程式庫啟動器尚未安裝，請執行 launcher\\scripts\\install.ps1。";

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
        MessageBox.Show(message, AppDisplayName,
            MessageBoxButtons.OK, MessageBoxIcon.Error,
            MessageBoxDefaultButton.Button1,
            MessageBoxOptions.DefaultDesktopOnly);
    }

    private static int LaunchHost(string projectRoot, string launchScript)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments =
                "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden" +
                " -File " + Quote(launchScript) +
                " -ProjectRoot " + Quote(projectRoot),
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

        var launchScript = Path.Combine(
            projectRoot, "launcher", "scripts", "start.ps1");
        if (!Directory.Exists(projectRoot) || !File.Exists(launchScript))
        {
            ShowError(MsgMissing);
            return 1;
        }

        return LaunchHost(projectRoot, launchScript);
    }
}
