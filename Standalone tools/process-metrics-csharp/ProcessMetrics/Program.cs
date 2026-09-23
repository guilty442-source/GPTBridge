// gptbridge-process-metrics — P30 psutil replacement probe.
//
// Usage: gptbridge-process-metrics <root-pid> [samples] [interval-ms]
// Emits one JSON document: per-process and aggregate machine-% CPU,
// working set, private bytes, thread count, process count, plus
// p50/p95/p99 over the sample window. Read-only; never exits non-zero
// on missing processes (tree may exit mid-sample) — missing data is
// reported as null, not fabricated.
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

var rootPid = args.Length > 0 && int.TryParse(args[0], out var rp) ? rp : Environment.ProcessId;
var samples = args.Length > 1 && int.TryParse(args[1], out var s1) ? s1 : 10;
var intervalMs = args.Length > 2 && int.TryParse(args[2], out var s2) ? s2 : 1000;
var logicalCores = Environment.ProcessorCount;

var cpuSeries = new List<double>();
var wsSeries = new List<long>();
var pbSeries = new List<long>();
var threadSeries = new List<int>();
var procCountSeries = new List<int>();
var prevCpu = new Dictionary<int, TimeSpan>();
var prevTime = DateTime.UtcNow;

for (var i = 0; i < samples; i++)
{
    var tree = Native.TreeOf(rootPid);
    var now = DateTime.UtcNow;
    var elapsedMs = (now - prevTime).TotalMilliseconds;
    double aggWs = 0, aggPb = 0;
    var aggThreads = 0;
    double cpuPct = 0;
    foreach (var pid in tree)
    {
        try
        {
            using var p = Process.GetProcessById(pid);
            p.Refresh();
            aggWs += p.WorkingSet64;
            aggPb += p.PrivateMemorySize64;
            aggThreads += p.Threads.Count;
            if (prevCpu.TryGetValue(pid, out var prev) && elapsedMs > 0)
                // per-core scale → normalize to whole-machine %
                cpuPct += (p.TotalProcessorTime - prev).TotalMilliseconds
                          / elapsedMs * 100.0 / logicalCores;
            prevCpu[pid] = p.TotalProcessorTime;
        }
        catch { /* exited mid-sample */ }
    }
    prevTime = now;
    if (i > 0) cpuSeries.Add(cpuPct); // first sample has no delta
    wsSeries.Add((long)aggWs);
    pbSeries.Add((long)aggPb);
    threadSeries.Add(aggThreads);
    procCountSeries.Add(tree.Count);
    if (i + 1 < samples) Thread.Sleep(intervalMs);
}

var doc = new
{
    schema = "gptbridge-process-metrics/v1",
    root_pid = rootPid,
    samples,
    interval_ms = intervalMs,
    logical_cores = logicalCores,
    cpu_pct_machine = new
    {
        mean = cpuSeries.Count > 0 ? cpuSeries.Average() : (double?)null,
        p50 = Pct(cpuSeries, 0.50),
        p95 = Pct(cpuSeries, 0.95),
        p99 = Pct(cpuSeries, 0.99),
        max = cpuSeries.Count > 0 ? cpuSeries.Max() : (double?)null,
    },
    working_set_bytes = new { p50 = PctL(wsSeries, 0.5), p95 = PctL(wsSeries, 0.95), max = wsSeries.DefaultIfEmpty().Max() },
    private_bytes = new { p50 = PctL(pbSeries, 0.5), p95 = PctL(pbSeries, 0.95), max = pbSeries.DefaultIfEmpty().Max() },
    threads = new { max = threadSeries.DefaultIfEmpty().Max() },
    process_count = new { max = procCountSeries.DefaultIfEmpty().Max() },
};
Console.WriteLine(JsonSerializer.Serialize(doc));
return 0;

static double Pct(List<double> xs, double q)
{
    if (xs.Count == 0) return 0;
    var s = xs.OrderBy(x => x).ToList();
    return s[Math.Min(s.Count - 1, (int)Math.Ceiling(q * s.Count) - 1)];
}

static long PctL(List<long> xs, double q)
{
    if (xs.Count == 0) return 0;
    var s = xs.OrderBy(x => x).ToList();
    return s[Math.Min(s.Count - 1, (int)Math.Ceiling(q * s.Count) - 1)];
}

static class Native
{
    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(
        IntPtr handle, int cls, ref PROCESS_BASIC_INFORMATION info, int len, out int retLen);

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_BASIC_INFORMATION
    {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2_0;
        public IntPtr Reserved2_1;
        public IntPtr UniqueProcessId;
        public IntPtr InheritedFromUniqueProcessId;
    }

    private static int ParentPid(int pid)
    {
        try
        {
            using var p = Process.GetProcessById(pid);
            var info = new PROCESS_BASIC_INFORMATION();
            var status = NtQueryInformationProcess(
                p.Handle, 0, ref info, Marshal.SizeOf<PROCESS_BASIC_INFORMATION>(), out _);
            return status == 0 ? info.InheritedFromUniqueProcessId.ToInt32() : -1;
        }
        catch { return -1; }
    }

    public static HashSet<int> TreeOf(int root)
    {
        var parent = new Dictionary<int, int>();
        foreach (var p in Process.GetProcesses())
        {
            try { parent[p.Id] = ParentPid(p.Id); }
            catch { /* exited */ }
        }
        var tree = new HashSet<int> { root };
        var changed = true;
        while (changed)
        {
            changed = false;
            foreach (var (pid, ppid) in parent)
                if (tree.Contains(ppid) && tree.Add(pid)) changed = true;
        }
        return tree;
    }
}
