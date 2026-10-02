using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal sealed record GitResult(int Code, string Stdout, string Stderr, bool TimedOut = false);

/// <summary>Bounded git subprocess execution (parity with GitRepository.run).</summary>
internal static class Git
{
    public const int DefaultTimeoutMs = 120_000;

    public static GitResult Run(
        string? workDirectory, IReadOnlyList<string> args,
        int timeoutMs = DefaultTimeoutMs) =>
        Exec(ResolveExecutable(), workDirectory, args, timeoutMs);

    // Git for Windows cmd/git.exe is a launcher. Start the installed native
    // binary directly so a stalled launcher cannot hold the automation lane.
    internal static string ResolveExecutable()
    {
        if (!OperatingSystem.IsWindows()) return "git";
        foreach (var entry in (Environment.GetEnvironmentVariable("PATH") ?? "")
                     .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var directory = entry.Trim('"');
            if (!File.Exists(Path.Combine(directory, "git.exe"))) continue;
            var name = Path.GetFileName(Path.TrimEndingDirectorySeparator(directory));
            var root = name.Equals("cmd", StringComparison.OrdinalIgnoreCase)
                       || name.Equals("bin", StringComparison.OrdinalIgnoreCase)
                ? Path.GetDirectoryName(directory) : directory;
            foreach (var lane in new[] { "ucrt64", "mingw64", "mingw32" })
            {
                var native = Path.Combine(root!, lane, "bin", "git.exe");
                if (File.Exists(native)) return native;
            }
            return Path.Combine(directory, "git.exe");
        }
        return "git";
    }

    /// <summary>Bounded arbitrary subprocess (dotnet/powershell/native
    /// tools) — same timeout/kill semantics as git runs.</summary>
    public static GitResult Exec(
        string executable, string? workDirectory,
        IReadOnlyList<string> args, int timeoutMs = DefaultTimeoutMs)
    {
        var startInfo = new ProcessStartInfo(executable)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        if (!string.IsNullOrEmpty(workDirectory))
            startInfo.WorkingDirectory = workDirectory;
        foreach (var arg in args)
            startInfo.ArgumentList.Add(arg);
        try
        {
            using var process = Process.Start(startInfo)
                ?? throw new InvalidOperationException("git spawn failed");
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            var deadline = Stopwatch.StartNew();
            if (!process.WaitForExit(timeoutMs)
                || !Task.WaitAll(new Task[] { stdout, stderr },
                    Math.Max(0, timeoutMs - (int)deadline.ElapsedMilliseconds)))
            {
                try { process.Kill(entireProcessTree: true); }
                catch (InvalidOperationException) { }
                return new GitResult(-1, "", $"timeout:{timeoutMs}ms", TimedOut: true);
            }
            return new GitResult(
                process.ExitCode, stdout.Result, stderr.Result);
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception
            or InvalidOperationException)
        {
            return new GitResult(-1, "", $"spawn:{error.Message}");
        }
    }

    /// <summary>(gitDir, commonDir) resolved purely from the filesystem —
    /// same contract as generation_snapshot._resolve_git_dirs.</summary>
    public static (string GitDir, string CommonDir)? ResolveGitDirs(string worktree)
    {
        var dotgit = Path.Combine(worktree, ".git");
        string gitDir;
        try
        {
            if (Directory.Exists(dotgit))
            {
                gitDir = dotgit;
            }
            else if (File.Exists(dotgit))
            {
                var line = File.ReadAllLines(dotgit)
                    .FirstOrDefault(l => l.StartsWith("gitdir:", StringComparison.Ordinal));
                if (line is null)
                    return null;
                gitDir = line["gitdir:".Length..].Trim();
                if (!Path.IsPathRooted(gitDir))
                    gitDir = Path.GetFullPath(Path.Combine(worktree, gitDir));
            }
            else
            {
                return null;
            }
            var commondirFile = Path.Combine(gitDir, "commondir");
            string commonDir;
            if (File.Exists(commondirFile))
            {
                var rel = File.ReadAllText(commondirFile).Trim();
                commonDir = Path.GetFullPath(Path.Combine(gitDir, rel));
            }
            else
            {
                commonDir = Path.GetFullPath(gitDir);
            }
            return (Path.GetFullPath(gitDir), commonDir);
        }
        catch (IOException)
        {
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            return null;
        }
    }

    public static string RevParse(string worktree, string argument)
    {
        var result = Run(worktree, new[] { "rev-parse", argument });
        return result.Code == 0 ? result.Stdout.Trim() : "";
    }

    public static string CommonDir(string worktree) =>
        ResolveGitDirs(worktree)?.CommonDir
        ?? RevParse(worktree, "--git-common-dir");

    public static string CurrentBranch(string worktree)
    {
        var result = Run(worktree,
            new[] { "rev-parse", "--abbrev-ref", "HEAD" });
        return result.Code == 0 ? result.Stdout.Trim() : "";
    }
}

internal sealed class LockBusyException : Exception
{
    public LockBusyException(string message) : base(message) { }
}

/// <summary>
/// Crash-safe process lock — parity with git_tiers.process_lock:
/// O_EXCL create + {pid, token} payload; a lock whose owner pid is dead
/// is stale and reclaimed once.
/// </summary>
internal sealed class ProcessFileLock : IDisposable
{
    private readonly string _path;
    private readonly string _token = Guid.NewGuid().ToString("N");
    private FileStream? _stream;

    private ProcessFileLock(string path) => _path = path;

    public static ProcessFileLock Acquire(string path)
    {
        var instance = new ProcessFileLock(path);
        instance.AcquireInternal();
        return instance;
    }

    private void AcquireInternal()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        for (var attempt = 0; attempt < 2; attempt++)
        {
            try
            {
                _stream = new FileStream(
                    _path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
                var payload = JsonSerializer.Serialize(
                    new { pid = Environment.ProcessId, token = _token });
                var bytes = Encoding.ASCII.GetBytes(payload);
                _stream.Write(bytes);
                _stream.Flush(flushToDisk: true);
                return;
            }
            catch (IOException)
            {
                if (OwnerAlive(_path) || attempt == 1)
                    throw new LockBusyException(
                        $"lock-busy:{Path.GetFileName(_path)}");
                try { File.Delete(_path); }
                catch (IOException) { }
            }
            catch (UnauthorizedAccessException)
            {
                if (OwnerAlive(_path) || attempt == 1)
                    throw new LockBusyException(
                        $"lock-busy:{Path.GetFileName(_path)}");
                try { File.Delete(_path); }
                catch (IOException) { }
            }
        }
        throw new LockBusyException($"lock-busy:{Path.GetFileName(_path)}");
    }

    public static bool PidAlive(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            return !process.HasExited;
        }
        catch (ArgumentException)
        {
            return false;
        }
        catch (InvalidOperationException)
        {
            // Unknown owner — treat as alive (fail-safe, never steal a lock).
            return true;
        }
    }

    public static bool OwnerAlive(string path)
    {
        double age;
        try
        {
            age = Math.Max(0.0,
                (DateTime.UtcNow - File.GetLastWriteTimeUtc(path)).TotalSeconds);
        }
        catch (IOException)
        {
            return false;
        }
        int pid;
        try
        {
            using var document = JsonDocument.Parse(
                File.ReadAllText(path, Encoding.ASCII));
            pid = document.RootElement.GetProperty("pid").GetInt32();
        }
        catch (Exception) when (!File.Exists(path))
        {
            return false;
        }
        catch (Exception)
        {
            return age < 10.0;
        }
        return PidAlive(pid);
    }

    public static bool IsActive(string path) =>
        File.Exists(path) && OwnerAlive(path);

    public void Dispose()
    {
        _stream?.Dispose();
        _stream = null;
        try
        {
            using var document = JsonDocument.Parse(
                File.ReadAllText(_path, Encoding.ASCII));
            if (document.RootElement.GetProperty("token").GetString() == _token)
                File.Delete(_path);
        }
        catch (IOException) { }
        catch (JsonException) { }
        catch (UnauthorizedAccessException) { }
    }
}

/// <summary>
/// Cooperating-writers commit lease inside the worktree git dir
/// (self-commit-lease.json): a fresh lease defers the sweep; the holder
/// renews, then releases only its own claim.
/// </summary>
internal static class CommitLease
{
    public const string Filename = "self-commit-lease.json";

    private static string PathFor(string gitDir) =>
        Path.Combine(gitDir, Filename);

    public static JsonObject? Active(string gitDir)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(PathFor(gitDir)));
            if (node is not JsonObject data)
                return null;
            if (data["until"]?.GetValue<double>() > Canon.EpochSeconds())
                return data;
        }
        catch (IOException) { }
        catch (JsonException) { }
        catch (FormatException) { }
        return null;
    }

    public static string? Claim(
        string gitDir, string owner, double ttlSeconds = 300.0)
    {
        var existing = Active(gitDir);
        if (existing is not null && existing["owner"]?.GetValue<string>() != owner)
            return null;
        var path = PathFor(gitDir);
        var now = Canon.EpochSeconds();
        File.WriteAllText(path, "{" +
            "\"owner\":" + Canon.Escape(owner) + "," +
            "\"claimed_at\":" + now.ToString("R", System.Globalization.CultureInfo.InvariantCulture) + "," +
            "\"until\":" + (now + Math.Max(1.0, ttlSeconds))
                .ToString("R", System.Globalization.CultureInfo.InvariantCulture) +
            "}");
        return path;
    }

    public static void Release(string gitDir, string owner)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(PathFor(gitDir)));
            if (node is JsonObject data
                && data["owner"]?.GetValue<string>() == owner)
                File.Delete(PathFor(gitDir));
        }
        catch (IOException) { }
        catch (JsonException) { }
    }
}
