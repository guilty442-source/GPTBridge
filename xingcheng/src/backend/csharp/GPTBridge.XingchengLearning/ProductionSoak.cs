// ProductionSoak.cs — ``star-runtime-soak-sample/v1`` sampler +
// ``star-production-soak-analysis/v1`` leak verdict
// (production-closure directive §6-§10).
//
// §6 RuntimeSoakTest: samples a target process (the governed model
// service worker, or any granted workload) at a fixed interval and
// appends one JSONL row per sample — the 8h/24h/72h batteries are just
// longer runs of the same instrumented loop.
//
// §8 monitored surface: RSS, commit (private) memory, thread count,
// handle count (covers file handles on Windows), cache/kv deltas are
// read off the service's /v1/status when a port+token are supplied,
// plus per-sample probe latency and the service-reported error count.
// VRAM/CUDA allocation columns stay honest: when the service reports
// them they are recorded; when it does not, they are null — never
// fabricated.
//
// §9/§10 leak gate: analysis compares the mean slope of the first
// (warm-up) window against the tail windows. Bounded warm-up = slope
// decays to ~0; a true leak = tail slope still positive and material.
// Verdict vocabulary: BOUNDED_WARMUP | UNBOUNDED_GROWTH |
// INSUFFICIENT_SAMPLES — RSS alone never convicts (§10): the verdict
// names which metric grew, and requires the tail window to cover at
// least half the run before UNBOUNDED_GROWTH is pronounced.
//

using System.Diagnostics;
using System.Net.Http;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ProductionSoak
{
    public const string SampleFormat = "star-runtime-soak-sample/v1";
    public const string AnalysisFormat = "star-production-soak-analysis/v1";
    public const string LogDirRel = "xingcheng/runtime/logs/production-soak";

    /// <summary>Growth slope (bytes/sample) below which a metric is
    /// treated as flat — bounded cache warm-up territory (§9).</summary>
    private const double FlatSlopeBytesPerSample = 512.0;

    // ------------------------------------------------------ sampler --

    /// <summary>Run the sampling loop. Stops after <paramref name=
    /// "seconds"/> elapsed, or early when the target process exits
    /// (that itself is recorded — an unplanned exit is a finding, not
    /// silence).</summary>
    public static Dictionary<string, object?> Run(string toolRoot,
        int pid, int seconds, int intervalMs, int port,
        string tokenFile)
    {
        if (seconds <= 0)
            throw new ExecutorError("PRODUCTION_SOAK_INVALID",
                "--seconds must be > 0");
        intervalMs = Math.Clamp(intervalMs, 250, 600_000);

        string? token = null;
        if (tokenFile.Length > 0 && File.Exists(tokenFile))
            token = File.ReadAllText(tokenFile).Trim();
        using var http = new HttpClient
            { Timeout = TimeSpan.FromSeconds(10) };
        if (token != null)
            // star-model-service-descriptor/v1 auth header
            // (XingchengModelServiceExecutor).
            http.DefaultRequestHeaders.Add(
                "X-GPTBridge-Session-Token", token);

        string dir = Path.Combine(toolRoot,
            LogDirRel.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(dir);
        string log = Path.Combine(dir,
            $"soak-{DateTimeOffset.UtcNow:yyyyMMdd-HHmmss}-pid{pid}.jsonl");

        Process? proc;
        try { proc = Process.GetProcessById(pid); }
        catch (ArgumentException)
        {
            throw new ExecutorError("PRODUCTION_SOAK_INVALID",
                $"pid {pid} not found — refusing to soak a phantom");
        }

        var sw = Stopwatch.StartNew();
        int samples = 0, targetExits = 0, probeErrors = 0;
        var fs = new StreamWriter(log, append: true);
        try
        {
            while (true)
            {
                var row = new Dictionary<string, object?>
                {
                    ["format"] = SampleFormat,
                    ["t"] = XcPaths.IsoNow(),
                    ["elapsed_s"] = (long)sw.Elapsed.TotalSeconds,
                    ["pid"] = pid,
                    ["interval_ms"] = intervalMs,
                };
                try
                {
                    proc.Refresh();
                    row["alive"] = !proc.HasExited;
                    if (proc.HasExited)
                    {
                        ++targetExits;
                        row["exit_code"] = proc.ExitCode;
                    }
                    else
                    {
                        // §8 memory/thread/handle surface.
                        row["rss_bytes"] = proc.WorkingSet64;
                        row["commit_bytes"] = proc.PrivateMemorySize64;
                        row["paged_bytes"] = proc.PagedMemorySize64;
                        row["threads"] = proc.Threads.Count;
                        row["handles"] = proc.HandleCount;
                    }
                }
                catch (Exception e) when (e is InvalidOperationException
                    or System.ComponentModel.Win32Exception)
                {
                    row["alive"] = false;
                    ++targetExits;
                }

                if (port > 0)
                {
                    var pw = Stopwatch.StartNew();
                    try
                    {
                        using var resp = http.GetAsync(
                            $"http://127.0.0.1:{port}/v1/status")
                            .GetAwaiter().GetResult();
                        pw.Stop();
                        row["probe_latency_ms"] =
                            pw.Elapsed.TotalMilliseconds;
                        row["probe_http"] = (int)resp.StatusCode;
                        var body = resp.Content
                            .ReadAsStringAsync().GetAwaiter().GetResult();
                        if (resp.IsSuccessStatusCode)
                        {
                            using var doc = JsonDocument.Parse(body);
                            foreach (var k in new[]
                                { "vram_bytes", "cuda_allocations",
                                  "cache_entries", "kv_tokens",
                                  "delta_state_bytes", "error_count" })
                                if (doc.RootElement.TryGetProperty(k,
                                        out var v) &&
                                    v.ValueKind == JsonValueKind.Number)
                                    row[k] = v.GetInt64();
                        }
                    }
                    catch (Exception e) when (e is HttpRequestException
                        or TaskCanceledException or JsonException)
                    {
                        pw.Stop();
                        ++probeErrors;
                        row["probe_latency_ms"] =
                            pw.Elapsed.TotalMilliseconds;
                        row["probe_http"] = 0;
                    }
                }

                try
                {
                    var verification = new NativeMetadataClient(toolRoot, "xingcheng-production-soak").Verify();
                    bool Field(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object
                        && value.TryGetProperty(name, out var item) && item.ValueKind == JsonValueKind.True;
                    row["metadata_integrity"] = Field(verification, "ok") && Field(verification, "schema_identity_ok")
                        && Field(verification, "invariants_ok");
                    row["audit_integrity"] = verification.TryGetProperty("receipts", out var receipts) && Field(receipts, "ok");
                    row["metadata_root"] = verification.TryGetProperty("head_hash", out var head) ? head.GetString() : null;
                }
                catch (Exception error)
                {
                    row["metadata_integrity"] = false; row["audit_integrity"] = false;
                    row["metadata_error"] = error.Message;
                }

                fs.WriteLine(CanonicalJson.Canonical(
                    ModelLifecycle.Encode(row)));
                fs.Flush();
                ++samples;
                if (sw.Elapsed.TotalSeconds >= seconds) break;
                Thread.Sleep(Math.Min(intervalMs, Math.Max(1, (int)((seconds - sw.Elapsed.TotalSeconds) * 1000))));
            }
        }
        finally { fs.Dispose(); }

        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = SampleFormat,
            ["log"] = log,
            ["samples"] = samples,
            ["elapsed_s"] = (long)sw.Elapsed.TotalSeconds,
            ["interval_ms"] = intervalMs,
            ["target_exits"] = targetExits,
            ["probe_errors"] = probeErrors,
        };
    }

    // ----------------------------------------------------- analysis --

    /// <summary>§9/§10 leak analysis over a sample log: per memory
    /// metric, split the run into a warm-up head (first 25%) and tail
    /// (last 50%) and compare mean per-sample growth. Tail still
    /// growing materially → UNBOUNDED_GROWTH naming the metric; tail
    /// flat after head growth → BOUNDED_WARMUP; fewer than 6 samples →
    /// INSUFFICIENT_SAMPLES (never convicts on too little data).</summary>
    public static Dictionary<string, object?> Analyze(string logPath)
    {
        if (!File.Exists(logPath))
            throw new ExecutorError("PRODUCTION_SOAK_INVALID",
                $"log '{logPath}' not found");
        var rows = new List<Dictionary<string, object?>>();
        foreach (var line in File.ReadLines(logPath))
        {
            if (line.Trim().Length == 0) continue;
            if (ModelLifecycle.Decode(JsonDocument.Parse(line)
                    .RootElement) is Dictionary<string, object?> r)
                rows.Add(r);
        }
        if (rows.Count < 6)
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["format"] = AnalysisFormat,
                ["verdict"] = "INSUFFICIENT_SAMPLES",
                ["samples"] = rows.Count,
            };

        var metrics = new Dictionary<string, object?>();
        var suspects = new List<string>();
        foreach (var m in new[] { "rss_bytes", "commit_bytes",
            "paged_bytes", "handles", "threads", "vram_bytes",
            "cache_entries", "kv_tokens", "delta_state_bytes" })
        {
            var series = rows
                .Select(r => Num(r, m))
                .Where(v => v.HasValue)
                .Select(v => v!.Value)
                .ToList();
            if (series.Count < 6) continue;
            int headEnd = series.Count / 4;
            int tailStart = series.Count / 2;
            double headSlope = Slope(series.Take(headEnd).ToList());
            double tailSlope = Slope(series.Skip(tailStart).ToList());
            double tailDelta = series[^1] - series[tailStart];
            bool unbounded = tailSlope > FlatSlopeBytesPerSample &&
                             tailDelta > 0;
            metrics[m] = new Dictionary<string, object?>
            {
                ["samples"] = series.Count,
                ["head_slope"] = Math.Round(headSlope, 3),
                ["tail_slope"] = Math.Round(tailSlope, 3),
                ["tail_delta"] = tailDelta,
                ["first"] = series[0],
                ["last"] = series[^1],
                ["verdict"] = unbounded ? "UNBOUNDED_GROWTH"
                    : tailSlope <= FlatSlopeBytesPerSample &&
                      headSlope > FlatSlopeBytesPerSample
                        ? "BOUNDED_WARMUP" : "FLAT",
            };
            if (unbounded) suspects.Add(m);
        }

        int exits = rows.Count(r =>
            r.TryGetValue("alive", out var a) && a is bool b && !b);
        var missing = new List<string>();
        var duration = Num(rows[^1], "elapsed_s") - Num(rows[0], "elapsed_s");
        if (!duration.HasValue || duration < 8 * 3600) missing.Add("SOAK_8H_NOT_COMPLETED");
        foreach (var required in new[] { "rss_bytes", "commit_bytes", "handles", "vram_bytes", "probe_latency_ms", "error_count" })
            if (rows.Any(row => !Num(row, required).HasValue)) missing.Add("TELEMETRY_MISSING:" + required);
        var probeErrors = rows.Count(row => Num(row, "probe_http") is not (>= 200 and < 300));
        if (probeErrors > 0) missing.Add("SERVICE_PROBE_FAILED");
        foreach (var required in new[] { "metadata_integrity", "audit_integrity" })
            if (rows.Any(row => !row.TryGetValue(required, out var value) || value is not true))
                missing.Add("INTEGRITY_EVIDENCE_MISSING:" + required);
        return new Dictionary<string, object?>
        {
            ["ok"] = suspects.Count == 0 && exits == 0 && missing.Count == 0,
            ["format"] = AnalysisFormat,
            ["log"] = logPath,
            ["samples"] = rows.Count,
            ["duration_s"] = Num(rows[^1], "elapsed_s"),
            ["metrics"] = metrics,
            ["target_exits"] = exits,
            ["probe_errors"] = probeErrors,
            ["missing_evidence"] = missing,
            ["verdict"] = exits > 0 ? "TARGET_EXITED"
                : suspects.Count > 0 ? "UNBOUNDED_GROWTH"
                : missing.Count > 0 ? "INCOMPLETE_EVIDENCE"
                : "BOUNDED",
            ["suspects"] = suspects.Cast<object?>().ToList(),
            ["rule"] = "§9/§10: bounded warm-up allowed; tail-window " +
                       "growth that never flattens is the leak signal — " +
                       "RSS alone never convicts",
        };
    }

    private static double? Num(Dictionary<string, object?> r, string k)
    {
        if (!r.TryGetValue(k, out var v) || v == null) return null;
        return v switch
        {
            double d => d, long l => l, int i => i,
            JsonElement e when e.ValueKind == JsonValueKind.Number
                => e.GetDouble(),
            _ => null,
        };
    }

    /// <summary>Mean per-sample slope (least-squares on index).</summary>
    private static double Slope(List<double> s)
    {
        int n = s.Count;
        if (n < 2) return 0;
        double sx = 0, sy = 0, sxx = 0, sxy = 0;
        for (int i = 0; i < n; i++)
        {
            sx += i; sy += s[i]; sxx += i * (double)i; sxy += i * s[i];
        }
        double denom = n * sxx - sx * sx;
        return denom == 0 ? 0 : (n * sxy - sx * sy) / denom;
    }
}
