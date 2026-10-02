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
using System.Runtime.InteropServices;
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

    /// <summary>Rust data/safety lane exe (xstore/xcorpus) under
    /// src/backend/rust/&lt;name&gt;/target/release. These are optional
    /// deployments — the resolver returns "" when the crate is not
    /// built so the caller decides whether absence is fatal.</summary>
    public static string RustExe(string toolRoot, string name)
    {
        string path = Path.Combine(toolRoot, "src", "backend", "rust",
            name, "target", "release", $"{name}.exe");
        return File.Exists(path) ? path : "";
    }

    /// <summary>Content-addressed artifact store dir owned by the Rust
    /// lane (xstore objects + receipts + audit chain).</summary>
    public static string ArtifactStoreDir(string toolRoot)
        => Path.Combine(toolRoot, "xingcheng", "runtime", "store");

    public sealed class RunResult
    {
        public int ExitCode;
        public double ElapsedS;
        public double? PeakRssMb;
        public int RssSamples;
        public double? MinVramFreeMb;
        public int VramSamples;
        public string StdoutTail = "";
    }

    /// <summary>Run a native tool with timeout + RSS accounting; on
    /// non-zero exit the stderr tail becomes the error message.
    /// <paramref name="lowPriority"/> drops the child to BelowNormal so
    /// batch training yields CPU to interactive work and live inference
    /// (governor A598: training sheds first). <paramref name="env"/>
    /// injects per-invocation environment variables (e.g. the trainer's
    /// XINGCHENG_TRAINER_CUDA_OPT device-opt gate) — the child inherits
    /// the parent environment plus these overrides.
    /// <paramref name="vramProbeMb"/> is an optional sampler invoked on
    /// each RSS sample tick; it should return system-wide free VRAM in MB
    /// (or null when the probe fails) so summaries can record the minimum
    /// headroom the run observed.</summary>
    public static RunResult Run(
        string exe,
        IEnumerable<string> args,
        string workingDir,
        string stderrLogPath,
        double timeoutS = 14400,
        double rssBudgetMb = 0,
        double sampleIntervalS = 5,
        bool lowPriority = false,
        IReadOnlyDictionary<string, string>? env = null,
        Func<double?>? vramProbeMb = null)
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
        if (env != null)
            foreach (var kv in env)
                psi.Environment[kv.Key] = kv.Value;

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
        if (lowPriority)
        {
            try { proc.PriorityClass = ProcessPriorityClass.BelowNormal; }
            catch { /* lowering priority must never abort governed work */ }
        }
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

        double deadline = timeoutS > 0 ? timeoutS : double.MaxValue;
        double peakRss = 0;
        int samples = 0;
        double minVramFree = double.MaxValue;
        int vramSamples = 0;
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
                if (vramProbeMb != null)
                {
                    double? free = null;
                    try { free = vramProbeMb(); }
                    catch { /* sampler failure must not kill supervision */ }
                    if (free.HasValue)
                    {
                        vramSamples++;
                        minVramFree = Math.Min(minVramFree, free.Value);
                    }
                }
            }
        }
        finally
        {
            // Drain async output handlers — bounded. The parameterless
            // WaitForExit waits for the redirected pipes to EOF, but a
            // detached grandchild can inherit those handles and hold
            // them open past the child's exit (the retired external
            // service lane spawned a persistent serve process, which
            // kept the collect lane deadlocked until killed). The
            // handlers have already
            // appended everything the child wrote; 15 s of grace keeps
            // the drain guarantee without waiting on pipes the child
            // no longer owns.
            try
            {
                using var cts = new CancellationTokenSource(
                    TimeSpan.FromSeconds(15));
                proc.WaitForExitAsync(cts.Token).GetAwaiter().GetResult();
            }
            catch { /* already dead / drain abandoned */ }
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
            MinVramFreeMb = vramSamples > 0
                ? Math.Round(minVramFree, 1) : null,
            VramSamples = vramSamples,
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
        // Toolhelp32 snapshot, no WMI/System.Management package — the
        // native-only gate flags third-party nuget in production scope.
        IntPtr snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snap == IntPtr.Zero || snap == INVALID_HANDLE_VALUE)
            return children;
        try
        {
            var e = new PROCESSENTRY32
                { dwSize = (uint)Marshal.SizeOf<PROCESSENTRY32>() };
            if (!Process32First(snap, ref e)) return children;
            do
            {
                if (e.th32ParentProcessID == (uint)parentPid)
                    children.Add((int)e.th32ProcessID);
            }
            while (Process32Next(snap, ref e));
        }
        finally { CloseHandle(snap); }
        return children;
    }

    private const uint TH32CS_SNAPPROCESS = 0x00000002;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new(-1);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PROCESSENTRY32
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public UIntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(
        uint dwFlags, uint th32ProcessID);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern bool Process32First(IntPtr hSnapshot,
        ref PROCESSENTRY32 lppe);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode,
        SetLastError = true)]
    private static extern bool Process32Next(IntPtr hSnapshot,
        ref PROCESSENTRY32 lppe);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);
}
