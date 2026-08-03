using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("\u7A0B\u5F0F\u5EAB")]
[assembly: AssemblyProduct("\u7A0B\u5F0F\u5EAB")]
[assembly: AssemblyDescription("\u7A0B\u5F0F\u5EAB\u6B63\u5F0F\u555F\u52D5\u5668")]

internal static class GPTBridgeLauncher
{
    [STAThread]
    private static int Main()
    {
        try
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string configRoot = Path.Combine(localAppData, "GPTBridgeLauncher", "config");
            string rootFile = Path.Combine(configRoot, "root.txt");

            if (!File.Exists(rootFile))
            {
                ShowError("\u7A0B\u5F0F\u5EAB\u555F\u52D5\u5668\u5C1A\u672A\u5B89\u88DD\uFF0C\u8ACB\u57F7\u884C launcher\\scripts\\install.ps1\u3002");
                return 1;
            }

            string projectRoot = File.ReadAllText(rootFile).Trim();
            string launchScript = Path.Combine(projectRoot, "launcher", "scripts", "start.ps1");
            if (!Directory.Exists(projectRoot) || !File.Exists(launchScript))
            {
                ShowError("\u627E\u4E0D\u5230\u7A0B\u5F0F\u5EAB\u6216\u555F\u52D5\u6A21\u7D44\uFF0C\u8ACB\u91CD\u65B0\u5B89\u88DD\u555F\u52D5\u5668\u3002");
                return 1;
            }

            ProcessStartInfo startInfo = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments =
                    "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " +
                    Quote(launchScript) +
                    " -ProjectRoot " +
                    Quote(projectRoot),
                WorkingDirectory = projectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };

            startInfo.EnvironmentVariables.Remove("ELECTRON_RUN_AS_NODE");
            Process.Start(startInfo);
            return 0;
        }
        catch (Exception error)
        {
            ShowError(error.Message);
            return 1;
        }
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static void ShowError(string message)
    {
        MessageBox.Show(
            message,
            "\u7A0B\u5F0F\u5EAB",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error
        );
    }
}
