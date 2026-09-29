using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    private static string LauncherBuildFingerprint()
    {
        var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var relative in LauncherBuildSources)
        {
            var path = Path.Combine(LauncherRoot,
                relative.Replace('/', Path.DirectorySeparatorChar));
            try
            {
                var info = new FileInfo(path);
                digest.AppendData(Encoding.UTF8.GetBytes(
                    $"{relative}:{MtimeNs(info)}:{info.Length}\n"));
            }
            catch (IOException)
            {
                digest.AppendData(Encoding.UTF8.GetBytes(
                    $"{relative}:missing\n"));
            }
            catch (UnauthorizedAccessException)
            {
                digest.AppendData(Encoding.UTF8.GetBytes(
                    $"{relative}:missing\n"));
            }
        }
        return Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant();
    }

    private static bool DesktopLauncherNeedsRefresh()
    {
        if (!File.Exists(
            Path.Combine(InstallRoot, "config", "root.txt")))
        {
            return false; // desktop bootstrap not installed
        }
        try
        {
            var payload = JsonNode.Parse(
                File.ReadAllText(LauncherStampPath))?.AsObject();
            return payload?["fingerprint"]?.GetValue<string>()
                != LauncherBuildFingerprint();
        }
        catch (Exception)
        {
            return true;
        }
    }

    private static bool RefreshLockIsActive()
    {
        try
        {
            var age = DateTime.UtcNow
                - File.GetLastWriteTimeUtc(RefreshLockPath);
            return age.TotalSeconds < RefreshLockStaleSeconds;
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static void RefreshDesktopLauncher()
    {
        try
        {
            if (!DesktopLauncherNeedsRefresh() || RefreshLockIsActive())
            {
                return;
            }
            // Native self-install (B167/B38): spawn our own exe with
            // --install-desktop — same detached-single-flight semantics
            // install.py had. The child skips the launch mutex (the
            // refresh lock single-flights installs), so it is never
            // blocked by this process holding it.
            var self = Environment.ProcessPath;
            if (self is null || !File.Exists(self))
            {
                return;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(RefreshLockPath)!);
            File.WriteAllText(RefreshLockPath,
                Environment.ProcessId.ToString(), Encoding.ASCII);
            var startInfo = new ProcessStartInfo
            {
                FileName = self,
                WorkingDirectory = ProjectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            };
            startInfo.ArgumentList.Add("--install-desktop");
            startInfo.ArgumentList.Add("--project-root");
            startInfo.ArgumentList.Add(ProjectRoot);
            Process.Start(startInfo);
            WriteStartupJournal("launcher.refresh.spawned", new JsonObject
            {
                ["sources"] = new JsonArray(
                    LauncherBuildSources
                        .Select(s => JsonValue.Create(s)).ToArray()),
            });
        }
        catch (Exception error)
        {
            WriteStartupJournal("launcher.refresh.failed",
                new JsonObject { ["message"] = error.Message });
        }
    }

    // ------------------------------------------------------------------
    // runtimes + frontend freshness
    // ------------------------------------------------------------------

}
