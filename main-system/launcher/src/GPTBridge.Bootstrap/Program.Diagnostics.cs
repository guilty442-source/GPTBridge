using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    private static void WriteLauncherStatus(string message)
    {
        var timestamp = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff");
        Console.WriteLine($"[{timestamp}] {message}");
    }

    private static void ShowLauncherError(string message)
    {
        try
        {
            var logRoot = Path.Combine(InstallRoot, "logs");
            Directory.CreateDirectory(logRoot);
            var line = string.Format(
                "[{0:yyyy-MM-dd HH:mm:ss.fff}] {1} ERROR: {2}",
                DateTime.Now, AppDisplayName, message);
            File.AppendAllText(
                Path.Combine(logRoot, "launcher.log"),
                line + Environment.NewLine,
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
        catch (Exception error)
        {
            Console.WriteLine(
                $"Warning: Unable to write launcher log: {error.Message}");
        }
    }

    private static void WriteStartupJournal(string evt, JsonObject? payload)
    {
        try
        {
            var record = new JsonObject
            {
                ["event"] = evt,
                ["timestamp"] = DateTime.UtcNow.ToString("o"),
            };
            if (payload is not null)
            {
                foreach (var pair in payload)
                {
                    record[pair.Key] = pair.Value?.DeepClone();
                }
            }
            File.AppendAllText(JournalPath,
                record.ToJsonString() + "\n",
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
        catch (Exception)
        {
            // Observability only — never block the launch.
        }
    }

    // ------------------------------------------------------------------
    // process helpers
    // ------------------------------------------------------------------

}
