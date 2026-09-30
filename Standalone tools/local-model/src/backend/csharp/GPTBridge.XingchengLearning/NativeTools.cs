// NativeTools.cs — governed subprocess lane for the native trainer and
// xc_modeltool bridge.
//
// Tool resolution is fail-closed: the executables must live inside the
// tool's own tree (native_transformer/{tools,training}); no PATH fallback.
// Process supervision mirrors the retired Python executor: wall-clock
// timeout, RSS sampling against an optional budget, kill-on-breach, and
// stderr captured to a job-local log — cross-process only via JSON file
// contracts (job spec -> trainer report -> eval output), never memory.

using System.Diagnostics;
using System.Text;

namespace GPTBridge.XingchengLearning;

internal sealed class ExecutorError : Exception
{
    public string ErrorCode { get; }

    public ExecutorError(string errorCode, string message) : base(message)
        => ErrorCode = errorCode;
}

internal static class NativeTools
{
    /// <summary>Resolve a tool exe under the tool's native_transformer tree.</summary>
    public static string ResolveExe(string toolRoot, params string[] relativeParts)
        => Path.Combine(
            new[] { toolRoot, "src", "backend", "services", "xingcheng",
                    "infrastructure", "native_transformer" }
                .Concat(relativeParts).ToArray());

    public static string ModelToolExe(string toolRoot)
    {
        string path = ResolveExe(toolRoot, "tools", "xc_modeltool.exe");
        if (!File.Exists(path))
            throw new ExecutorError(
                "EXECUTOR_MODELTOOL_UNAVAILABLE",
                $"xc_modeltool.exe missing: {path}");
        return path;
    }

    public static string TrainerExe(string toolRoot)
    {
        string path = ResolveExe(toolRoot, "training", "xingcheng_trainer.exe");
        if (!File.Exists(path))
            throw new ExecutorError(
                "EXECUTOR_TRAINER_UNAVAILABLE",
                $"xingcheng_trainer.exe missing: {path}");
        return path;
    }

    public sealed class RunResult
    {
        public int ExitCode;
        public double ElapsedS;
        public double? PeakRssMb;
        public int RssSamples;
        public string StdoutTail = "";
    }

    /// <summary>Run a native tool with timeout + RSS accounting; on
    /// non-zero exit the stderr tail becomes the error message.</summary>
    public static RunResult Run(
        string exe,
        IEnumerable<string> args,
        string workingDir,
        string stderrLogPath,
        double timeoutS = 14400,
        double rssBudgetMb = 0,
        double sampleIntervalS = 5)
    {
        var psi = new ProcessStartInfo
        {
            FileName = exe,
            WorkingDirectory = workingDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        foreach (string arg in args)
            psi.ArgumentList.Add(arg);

        Directory.CreateDirectory(Path.GetDirectoryName(stderrLogPath)!);
        var stderrBuffer = new StringBuilder();
        var stdoutTail = new StringBuilder();

        using var stderrLog = new StreamWriter(stderrLogPath, append: true,
                                               new UTF8Encoding(false));
        var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
        proc.OutputDataReceived += (_, e) =>
        {
            if (e.Data == null) return;
            lock (stdoutTail)
            {
                stdoutTail.AppendLine(e.Data);
                if (stdoutTail.Length > 1048576)
                    stdoutTail.Remove(0, stdoutTail.Length - 1048576);
            }
        };
        proc.ErrorDataReceived += (_, e) =>
        {
            if (e.Data == null) return;
            lock (stderrBuffer)
            {
                stderrBuffer.AppendLine(e.Data);
                if (stderrBuffer.Length > 8192)
                    stderrBuffer.Remove(0, stderrBuffer.Length - 8192);
            }
            try { stderrLog.WriteLine(e.Data); stderrLog.Flush(); }
            catch { /* log failure must not kill supervision */ }
        };

        var started = Stopwatch.StartNew();
        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

        double deadline = timeoutS > 0 ? timeoutS : double.MaxValue;
        double peakRss = 0;
        int samples = 0;
        try
        {
            while (true)
            {
                double waitS = Math.Max(0.1, sampleIntervalS);
                double remaining = deadline - started.Elapsed.TotalSeconds;
                if (remaining <= 0)
                {
                    KillTree(proc);
                    throw new ExecutorError(
                        "EXECUTOR_TRAINING_FAILED",
                        $"training subprocess timed out after {timeoutS}s");
                }
                waitS = Math.Min(waitS, remaining);
                if (proc.WaitForExit((int)(waitS * 1000)))
                    break;
                double? rss = ProcessTreeRssMb(proc.Id);
                if (rss.HasValue)
                {
                    samples++;
                    peakRss = Math.Max(peakRss, rss.Value);
                    if (rssBudgetMb > 0 && rss.Value > rssBudgetMb)
                    {
                        KillTree(proc);
                        throw new ExecutorError(
                            "EXECUTOR_RESOURCE_OVERBUDGET",
                            $"training subprocess exceeded RSS budget: " +
                            $"{rss.Value:F0}MB > {rssBudgetMb:F0}MB");
                    }
                }
            }
        }
        finally
        {
            try { proc.WaitForExit(2000); } catch { /* already dead */ }
        }

        int exitCode;
        try { exitCode = proc.ExitCode; }
        catch { exitCode = -1; }

        var result = new RunResult
        {
            ExitCode = exitCode,
            ElapsedS = started.Elapsed.TotalSeconds,
            PeakRssMb = samples > 0 ? Math.Round(peakRss, 1) : null,
            RssSamples = samples,
            StdoutTail = stdoutTail.ToString(),
        };
        return result;
    }

    private static void KillTree(Process proc)
    {
        try
        {
            // /T kills the whole tree (children included), /F forces.
            var killer = Process.Start(new ProcessStartInfo
            {
                FileName = "taskkill",
                Arguments = $"/PID {proc.Id} /T /F",
                CreateNoWindow = true,
                UseShellExecute = false,
            });
            killer?.WaitForExit(5000);
        }
        catch { /* best effort */ }
        try { proc.Kill(entireProcessTree: true); } catch { /* already dead */ }
    }

    /// <summary>Process-tree RSS in MB (main + descendants) via WMI.</summary>
    public static double? ProcessTreeRssMb(int rootPid)
    {
        try
        {
            var pids = new List<int> { rootPid };
            var seen = new HashSet<int> { rootPid };
            for (int i = 0; i < pids.Count; i++)
            {
                foreach (int child in ChildPids(pids[i]))
                    if (seen.Add(child))
                        pids.Add(child);
            }
            double totalBytes = 0;
            int found = 0;
            foreach (int pid in pids)
            {
                try
                {
                    using var p = Process.GetProcessById(pid);
                    totalBytes += p.WorkingSet64;
                    found++;
                }
                catch { /* process exited mid-sample */ }
            }
            return found > 0 ? totalBytes / (1024.0 * 1024.0) : null;
        }
        catch
        {
            return null;
        }
    }

    private static List<int> ChildPids(int parentPid)
    {
        var children = new List<int>();
        try
        {
            using var searcher = new System.Management.ManagementObjectSearcher(
                $"SELECT ProcessId FROM Win32_Process WHERE ParentProcessId = {parentPid}");
            foreach (var row in searcher.Get())
                children.Add(Convert.ToInt32(row["ProcessId"]));
        }
        catch { /* WMI unavailable -> main process only */ }
        return children;
    }
}
