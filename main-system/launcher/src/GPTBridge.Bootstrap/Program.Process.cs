using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    private static Process StartHiddenProcess(
        string filePath, IReadOnlyList<string> arguments,
        string workingDirectory)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = filePath,
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        foreach (var arg in arguments)
        {
            startInfo.ArgumentList.Add(arg);
        }
        // Cmd scripts (npm.cmd etc.) resolve through CreateProcess itself —
        // no manual cmd.exe /c quoting layer needed.
        var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException(
                $"Failed to start process: {filePath}");
        return process;
    }

    private static void InvokeLauncherCommand(
        string filePath, IReadOnlyList<string> arguments,
        string? workingDirectory = null)
    {
        var isScript =
            filePath.EndsWith(".cmd", StringComparison.OrdinalIgnoreCase)
            || filePath.EndsWith(".bat", StringComparison.OrdinalIgnoreCase);
        var startInfo = new ProcessStartInfo
        {
            WorkingDirectory = workingDirectory ?? ProjectRoot,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        if (isScript)
        {
            // Batch files cannot run through CreateProcess directly; route
            // through cmd.exe with a single quoted command line (/s keeps
            // the inner quoting literal).
            var inner = "\"" + filePath + "\""
                + (arguments.Count > 0
                    ? " " + string.Join(" ", arguments.Select(QuoteArg))
                    : "");
            startInfo.FileName = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System),
                "cmd.exe");
            startInfo.Arguments = "/d /s /c \"" + inner + "\"";
        }
        else
        {
            startInfo.FileName = filePath;
            foreach (var arg in arguments)
            {
                startInfo.ArgumentList.Add(arg);
            }
        }
        WriteLauncherStatus(
            $"Run: {filePath} {string.Join(" ", arguments)}");
        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException(
                $"Failed to start process: {filePath}");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        process.WaitForExit();
        if (process.ExitCode != 0)
        {
            foreach (var line in Tail(stdout.Result + stderr.Result, 30))
            {
                WriteLauncherStatus("  | " + line);
            }
            throw new InvalidOperationException(
                $"Command failed with exit code {process.ExitCode}: "
                + filePath);
        }
    }

    private static string QuoteArg(string arg) =>
        arg.Contains(' ') || arg.Contains('"')
            ? "\"" + arg.Replace("\"", "\\\"") + "\""
            : arg;

    private static IEnumerable<string> Tail(string text, int count)
    {
        var lines = text
            .Replace("\r\n", "\n")
            .Split('\n', StringSplitOptions.RemoveEmptyEntries);
        return lines.Skip(Math.Max(0, lines.Length - count));
    }

    private static string? Which(params string[] names)
    {
        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var name in names)
        {
            foreach (var directory in path.Split(Path.PathSeparator))
            {
                if (string.IsNullOrWhiteSpace(directory))
                {
                    continue;
                }
                var candidate = Path.Combine(directory.Trim(), name);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }
        return null;
    }

    // ------------------------------------------------------------------
    // migration autostart (delegated to the governed Python module)
    // ------------------------------------------------------------------

}
