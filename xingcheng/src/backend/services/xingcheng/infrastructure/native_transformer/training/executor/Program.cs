// XingchengTrainExecutor — MODEL_TRAINING bounded governed job executor.
//
// Codex: MODEL_TRAINING runtime is on-demand governed jobs; bounded
// concurrency policy (capacity | priority | deadline | backpressure |
// cancellation | drop/reject) applies to every admission. The executor owns
// the queue; xingcheng_trainer.exe (C++23) owns one job per process.
//
// File-queue protocol (transport-proxy compatible naming):
//   submit:  req-<id>.json        atomically written; bounded admission —
//            if pending >= capacity writes rejected-<id>.json and exits 3
//            (backpressure; caller retries or drops per its policy)
//   claim:   claimed-<id>.json    atomic rename by the executor
//   result:  done-<id>.json       trainer report embedded
//            failed-<id>.json     {error}
//            rejected-<id>.json   {reason: queue-full|malformed|deadline}
//            cancelled-<id>.json  {reason: cancel-requested|deadline}
//   cancel:  cancel-<id>.flag     requester drops this file -> worker killed
//   priority: job JSON "priority" (int, default 0); executor picks max first
//   deadline: train.deadline_s enforced as wall-clock kill by the executor
//             (defense in depth — the trainer also self-enforces)
//
// Tunables (executor.json beside the queue dir, else defaults):
//   { "capacity": 16, "workers": 1, "poll_ms": 250,
//     "default_deadline_s": 3600 }
// Worker count/capacity are Resource-Governor-decided on real benchmarks;
// values here are floor defaults, never a hard-coded contract.
//
// CLI:
//   xct-executor serve  --queue <dir> --trainer <path-to-exe>
//   xct-executor submit --queue <dir> --job <job.json> [--id <id>]
//   xct-executor cancel --queue <dir> --id <id>
//   xct-executor status --queue <dir>

using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

static string Arg(string[] a, string name, string def = "")
{
    for (int i = 0; i < a.Length - 1; i++)
        if (a[i] == name) return a[i + 1];
    return def;
}

static JsonNode? ParseJson(string path)
{
    try { return JsonNode.Parse(File.ReadAllText(path)); }
    catch { return null; }
}

static (int cap, int workers, int poll, double defDeadline) LoadTunables(string queueDir)
{
    var j = ParseJson(Path.Combine(queueDir, "executor.json"));
    int cap = (int?)j?["capacity"] ?? 16;
    int workers = (int?)j?["workers"] ?? 1;
    int poll = (int?)j?["poll_ms"] ?? 250;
    double dl = (double?)j?["default_deadline_s"] ?? 3600.0;
    return (Math.Max(1, cap), Math.Max(1, workers), Math.Max(50, poll), dl);
}

static string IdOf(string file) =>
    Path.GetFileNameWithoutExtension(file).Split('-', 2)[1];

static void AtomicWrite(string path, string content)
{
    var tmp = path + ".tmp-" + Guid.NewGuid().ToString("N");
    File.WriteAllText(tmp, content);
    File.Move(tmp, path, true);
}

var cmd = args.Length > 0 ? args[0] : "";
var queueDir = Arg(args, "--queue");
if (cmd is not ("serve" or "submit" or "cancel" or "status") || queueDir.Length == 0)
{
    Console.Error.WriteLine(
        "usage: xct-executor serve|submit|cancel|status --queue <dir> [--trainer <exe>] [--job <f>] [--id <id>]");
    return 2;
}
Directory.CreateDirectory(queueDir);

if (cmd == "status")
{
    int pend = Directory.GetFiles(queueDir, "req-*.json").Length;
    int claim = Directory.GetFiles(queueDir, "claimed-*.json").Length;
    int done = Directory.GetFiles(queueDir, "done-*.json").Length;
    int fail = Directory.GetFiles(queueDir, "failed-*.json").Length;
    int rej = Directory.GetFiles(queueDir, "rejected-*.json").Length;
    int canc = Directory.GetFiles(queueDir, "cancelled-*.json").Length;
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        schema = "star-native-train-queue-status/v1",
        pending = pend, running = claim, done, failed = fail,
        rejected = rej, cancelled = canc
    }));
    return 0;
}

if (cmd == "cancel")
{
    var id = Arg(args, "--id");
    if (id.Length == 0) return 2;
    AtomicWrite(Path.Combine(queueDir, $"cancel-{id}.flag"), "cancel\n");
    Console.WriteLine($"cancel requested: {id}");
    return 0;
}

if (cmd == "submit")
{
    var jobFile = Arg(args, "--job");
    if (jobFile.Length == 0 || !File.Exists(jobFile)) return 2;
    var job = ParseJson(jobFile);
    var id = Arg(args, "--id");
    if (id.Length == 0) id = Guid.NewGuid().ToString("N")[..12];
    if (job is null || job["task"] is null || job["model"] is null || job["data"] is null)
    {
        AtomicWrite(Path.Combine(queueDir, $"rejected-{id}.json"),
            JsonSerializer.Serialize(new { id, reason = "malformed" }));
        Console.Error.WriteLine($"rejected: {id} (malformed)");
        return 4;
    }
    var (cap, _, _, _) = LoadTunables(queueDir);
    if (Directory.GetFiles(queueDir, "req-*.json").Length >= cap)
    {
        AtomicWrite(Path.Combine(queueDir, $"rejected-{id}.json"),
            JsonSerializer.Serialize(new { id, reason = "queue-full", capacity = cap }));
        Console.Error.WriteLine($"rejected: {id} (queue-full capacity={cap})");
        return 3;                                   // backpressure
    }
    job["job_id"] = id;
    AtomicWrite(Path.Combine(queueDir, $"req-{id}.json"), job.ToJsonString());
    Console.WriteLine($"queued: {id}");
    return 0;
}

// ---- serve ----
var trainer = Arg(args, "--trainer",
    Path.Combine(AppContext.BaseDirectory, "..", "xingcheng_trainer.exe"));
if (!File.Exists(trainer))
{
    Console.Error.WriteLine($"trainer not found: {trainer}");
    return 2;
}

var tun = LoadTunables(queueDir);
var running = new Dictionary<string, (Process proc, string claimed, DateTime started, double deadlineS)>();
Console.WriteLine($"xct-executor serving {queueDir} workers={tun.workers} capacity={tun.cap}");

while (true)
{
    // reaps: finished / cancelled / deadline
    foreach (var kv in running.ToArray())
    {
        var (proc, claimed, started, deadlineS) = kv.Value;
        var id = kv.Key;
        var cancelFlag = Path.Combine(queueDir, $"cancel-{id}.flag");
        bool deadlined = (DateTime.UtcNow - started).TotalSeconds > deadlineS;
        if (File.Exists(cancelFlag) || deadlined)
        {
            try { proc.Kill(entireProcessTree: true); } catch { }
            File.Move(claimed, Path.Combine(queueDir, $"cancelled-{id}.json"), true);
            AtomicWrite(Path.Combine(queueDir, $"cancelled-{id}.meta.json"),
                JsonSerializer.Serialize(new { id, reason = deadlined ? "deadline" : "cancel-requested" }));
            File.Delete(cancelFlag);
            running.Remove(id);
            continue;
        }
        if (proc.HasExited)
        {
            var report = Path.Combine(queueDir, $"report-{id}.json");
            var body = File.Exists(report) ? File.ReadAllText(report) : "{}";
            if (proc.ExitCode == 0)
            {
                File.Move(claimed, Path.Combine(queueDir, $"done-{id}.json"), true);
            }
            else
            {
                File.Move(claimed, Path.Combine(queueDir, $"failed-{id}.json"), true);
                AtomicWrite(Path.Combine(queueDir, $"failed-{id}.meta.json"),
                    JsonSerializer.Serialize(new { id, exit = proc.ExitCode }));
            }
            running.Remove(id);
        }
    }

    // admit: highest priority pending first (priority field desc, FIFO tie)
    while (running.Count < tun.workers)
    {
        var pend = Directory.GetFiles(queueDir, "req-*.json")
            .Select(f => (f, job: ParseJson(f)))
            .OrderByDescending(x => (int?)x.job?["priority"] ?? 0)
            .ThenBy(x => File.GetCreationTimeUtc(x.f))
            .FirstOrDefault();
        if (pend.f is null) break;
        var id = IdOf(pend.f);
        var claimed = Path.Combine(queueDir, $"claimed-{id}.json");
        try { File.Move(pend.f, claimed); }
        catch { continue; }                          // lost race -> retry next poll
        double dl = tun.defDeadline;
        var dlNode = pend.job?["train"]?["deadline_s"];
        if (dlNode is not null) dl = (double)dlNode;
        var reportPath = Path.Combine(queueDir, $"report-{id}.json");
        var proc = Process.Start(new ProcessStartInfo
        {
            FileName = trainer,
            Arguments = $"--job \"{claimed}\" --report \"{reportPath}\"",
            UseShellExecute = false,
            RedirectStandardError = true,
            CreateNoWindow = true,
        });
        if (proc is null)
        {
            File.Move(claimed, Path.Combine(queueDir, $"failed-{id}.json"), true);
            continue;
        }
        running[id] = (proc, claimed, DateTime.UtcNow, dl);
    }
    Thread.Sleep(tun.poll);
}
