using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    private static IEnumerable<string> IterUiBuildInputs()
    {
        var files = new List<string>();
        foreach (var relative in UiBuildInputRoots)
        {
            var baseDir = Path.Combine(ProjectRoot, relative);
            if (Directory.Exists(baseDir))
            {
                files.AddRange(Directory
                    .EnumerateFiles(baseDir, "*", SearchOption.AllDirectories));
            }
        }
        foreach (var relative in UiBuildInputFiles)
        {
            var candidate = Path.Combine(ProjectRoot, relative);
            if (File.Exists(candidate))
            {
                files.Add(candidate);
            }
        }
        foreach (var pattern in UiBuildToolDirs)
        {
            foreach (var match in GlobDirectories(WorkspaceRoot, pattern))
            {
                files.AddRange(Directory
                    .EnumerateFiles(match, "*", SearchOption.AllDirectories));
            }
        }
        foreach (var pattern in UiBuildToolFiles)
        {
            files.AddRange(GlobFiles(WorkspaceRoot, pattern));
        }
        // Deterministic order — the fingerprint must be stable, not depend
        // on filesystem enumeration order.
        files.Sort(StringComparer.Ordinal);
        return files;
    }

    private static IEnumerable<string> GlobDirectories(string root, string pattern)
    {
        var segments = pattern.Split('/');
        var current = new List<string> { root };
        foreach (var segment in segments)
        {
            var next = new List<string>();
            foreach (var dir in current)
            {
                if (segment == "*")
                {
                    if (Directory.Exists(dir))
                    {
                        next.AddRange(Directory.EnumerateDirectories(dir));
                    }
                }
                else
                {
                    var candidate = Path.Combine(dir, segment);
                    if (Directory.Exists(candidate))
                    {
                        next.Add(candidate);
                    }
                }
            }
            current = next;
        }
        current.Sort(StringComparer.Ordinal);
        return current;
    }

    private static IEnumerable<string> GlobFiles(string root, string pattern)
    {
        var segments = pattern.Split('/');
        var fileSegment = segments[^1];
        var dirs = GlobDirectories(root,
            string.Join('/', segments[..^1]));
        var files = new List<string>();
        foreach (var dir in dirs)
        {
            var candidate = Path.Combine(dir, fileSegment);
            if (File.Exists(candidate))
            {
                files.Add(candidate);
            }
        }
        files.Sort(StringComparer.Ordinal);
        return files;
    }

    private static long MtimeNs(FileSystemInfo info)
    {
        // Python st_mtime_ns equivalent: 100ns FILETIME ticks since
        // 1970-01-01, expressed in nanoseconds.
        return (info.LastWriteTimeUtc.Ticks - 621355968000000000L) * 100L;
    }

    private static string UiBuildFingerprint()
    {
        var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var utf8 = new UTF8Encoding(
            encoderShouldEmitUTF8Identifier: false,
            throwOnInvalidBytes: false);
        foreach (var path in IterUiBuildInputs())
        {
            try
            {
                var info = new FileInfo(path);
                var relative = Path
                    .GetRelativePath(WorkspaceRoot, path)
                    .Replace(Path.DirectorySeparatorChar, '/');
                digest.AppendData(utf8.GetBytes(
                    $"{relative}:{info.Length}:{MtimeNs(info)}\n"));
            }
            catch (IOException)
            {
                continue;
            }
            catch (UnauthorizedAccessException)
            {
                continue;
            }
        }
        return Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant();
    }

    private static bool UiBuildIsCurrent()
    {
        try
        {
            var payload = JsonNode.Parse(
                File.ReadAllText(UiBuildStampPath))?.AsObject();
            if (payload?["fingerprint"]?.GetValue<string>()
                != UiBuildFingerprint())
            {
                return false;
            }
        }
        catch (Exception)
        {
            return false;
        }
        return UiBuildOutputPaths.All(relative =>
            File.Exists(Path.Combine(ProjectRoot,
                relative.Replace('/', Path.DirectorySeparatorChar))));
    }

    private static void EnsureUiBuild(bool force)
    {
        if (!force && UiBuildIsCurrent())
        {
            return;
        }
        var swc = Path.Combine(WorkspaceRoot, ".tools", "swc", "swc.exe");
        var esbuild = Path.Combine(WorkspaceRoot, ".tools", "esbuild", "esbuild.exe");
        if (!File.Exists(swc) || !File.Exists(esbuild))
        {
            throw new InvalidOperationException(
                "vendored swc/esbuild toolchain missing under .tools/; cannot build the frontend from source");
        }
        // Python runtime retired (B167/B38): the swc+esbuild chain is
        // driven by the native RendererBuild port — no interpreter, no
        // venv provisioning on the build path.
        WriteLauncherStatus(
            "Frontend sources changed; rebuilding (swc+esbuild)...");
        WriteStartupJournal("launcher.ui.build.start",
            new JsonObject { ["forced"] = force });
        var stopwatch = Stopwatch.StartNew();
        NativeRendererBuild("--all");
        foreach (var relative in UiBuildOutputPaths)
        {
            var output = Path.Combine(ProjectRoot,
                relative.Replace('/', Path.DirectorySeparatorChar));
            if (!File.Exists(output))
            {
                throw new InvalidOperationException(
                    $"Frontend build output is missing: {output}");
            }
        }
        File.WriteAllText(UiBuildStampPath,
            new JsonObject
            {
                ["fingerprint"] = UiBuildFingerprint(),
                ["built_at"] = DateTime.UtcNow.ToString("o"),
            }.ToJsonString() + "\n",
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        WriteStartupJournal("launcher.ui.build.done", new JsonObject
        {
            ["duration_ms"] = stopwatch.ElapsedMilliseconds,
        });
        WriteLauncherStatus("Frontend rebuilt from source.");
    }

    private static string ArgValue(string[] args, string name)
    {
        for (var index = 0; index < args.Length - 1; index++)
        {
            if (args[index] == name)
            {
                return args[index + 1];
            }
        }
        return "";
    }
}
