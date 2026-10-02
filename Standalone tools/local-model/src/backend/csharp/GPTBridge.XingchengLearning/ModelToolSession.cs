// ModelToolSession.cs — process-scoped resident client for
// `xc_modeltool serve` (load-once residency).
//
// One session holds one serve process for the duration of a governed
// operation (a training job's tokenize pair, one suite evaluation's
// baseline+candidate runs). The serve side caches tokenizers by path
// and up to two bundle engines (candidate + baseline), so repeated
// calls skip reloads; every call that cannot be served falls back to
// the one-shot `NativeTools.Run` path — the session is strictly an
// accelerator, never a single point of failure.
//
// Threading: one op at a time (lock); concurrent first-use from two
// threads serializes inside the session. Cross-process sharing is out
// of scope: each process holds its own session (the machine-resident
// multiplexed daemon is a later phase; the runtime-host lease already
// reserves its discovery slot).

using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal sealed class ModelToolSession : IDisposable
{
    private readonly Process _proc;
    private readonly StreamWriter _stdin;
    private readonly StreamReader _stdout;
    private readonly object _gate = new();
    private readonly string _stderrLog;
    private bool _dead;
    private bool _disposed;

    private ModelToolSession(
        Process proc, StreamWriter stdin, StreamReader stdout,
        string stderrLog)
    {
        _proc = proc;
        _stdin = stdin;
        _stdout = stdout;
        _stderrLog = stderrLog;
    }

    /// <summary>Start a serve session against a bundle directory (used
    /// for the startup descriptor read; engines stay lazy until an eval
    /// op names a bundle). Returns null when the session cannot start —
    /// callers fall back to one-shot. Never throws.</summary>
    public static ModelToolSession? TryStart(
        string toolRoot, string bundleDir, string stderrLog,
        double timeoutS = 30)
    {
        string exe;
        try
        {
            exe = NativeTools.ModelToolExe(toolRoot);
        }
        catch
        {
            return null;
        }
        if (!Directory.Exists(bundleDir)) return null;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(stderrLog)!);
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                WorkingDirectory = toolRoot,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = new UTF8Encoding(false),
                StandardErrorEncoding = new UTF8Encoding(false),
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.ArgumentList.Add("serve");
            psi.ArgumentList.Add("--bundle");
            psi.ArgumentList.Add(bundleDir);
            var proc = new Process
            {
                StartInfo = psi,
                EnableRaisingEvents = true,
            };
            // stderr drains to the job log asynchronously so a chatty
            // child can never block the stdout framing.
            var stderrLock = new object();
            proc.ErrorDataReceived += (_, e) =>
            {
                if (e.Data == null) return;
                try
                {
                    lock (stderrLock)
                        File.AppendAllText(stderrLog, e.Data + "\n",
                                           new UTF8Encoding(false));
                }
                catch { /* log failure never breaks the session */ }
            };
            proc.Start();
            try { proc.PriorityClass = ProcessPriorityClass.BelowNormal; }
            catch { /* lowering priority must never abort governed work */ }
            proc.BeginErrorReadLine();
            var session = new ModelToolSession(
                proc, proc.StandardInput, proc.StandardOutput, stderrLog);
            // Handshake: status must answer before the session is trusted.
            var (hello, _) = session.Request(
                new Dictionary<string, object?> { ["op"] = "status" },
                timeoutS);
            if (!TransformerTrainingRepository.Truthy(
                    hello.GetValueOrDefault("ok")))
            {
                session.Dispose();
                return null;
            }
            return session;
        }
        catch
        {
            return null;
        }
    }

    public bool IsAlive
    {
        get
        {
            try { return !_dead && !_proc.HasExited; }
            catch { return false; }
        }
    }

    /// <summary>Send one request line, read one response line. Returns
    /// the decoded JSON plus the op's exit_code (0 ok, 2 gate-failed,
    /// 1 tool error). Throws ExecutorError on transport failure — the
    /// session is marked dead and callers fall back to one-shot.</summary>
    public (Dictionary<string, object?> json, int exitCode) Request(
        IReadOnlyDictionary<string, object?> request, double timeoutS = 7200)
    {
        lock (_gate)
        {
            if (_disposed)
                throw new ExecutorError("EXECUTOR_SERVE_SESSION_CLOSED",
                    "serve session already disposed");
            if (!IsAlive)
            {
                _dead = true;
                throw new ExecutorError("EXECUTOR_SERVE_SESSION_DEAD",
                    "serve process exited");
            }
            string line;
            try
            {
                line = CanonicalJson.PlainDict(request);
                _stdin.WriteLine(line);
                _stdin.Flush();
            }
            catch (Exception exc)
            {
                _dead = true;
                throw new ExecutorError("EXECUTOR_SERVE_SESSION_DEAD",
                    $"serve stdin failed: {exc.Message}");
            }
            string? response = null;
            try
            {
                var read = Task.Run(() => _stdout.ReadLine());
                if (!read.Wait(TimeSpan.FromSeconds(
                        timeoutS > 0 ? timeoutS : 7200)))
                {
                    _dead = true;
                    try { _proc.Kill(entireProcessTree: true); }
                    catch { /* already dead */ }
                    throw new ExecutorError("EXECUTOR_SERVE_SESSION_DEAD",
                        $"serve response timed out after {timeoutS}s");
                }
                response = read.Result;
            }
            catch (Exception exc) when (exc is ExecutorError)
            {
                throw;
            }
            catch (Exception exc)
            {
                _dead = true;
                throw new ExecutorError("EXECUTOR_SERVE_SESSION_DEAD",
                    $"serve stdout failed: {exc.Message}");
            }
            if (response == null)
            {
                _dead = true;
                throw new ExecutorError("EXECUTOR_SERVE_SESSION_DEAD",
                    "serve closed stdout (see " + _stderrLog + ")");
            }
            Dictionary<string, object?> json;
            try
            {
                using var doc = JsonDocument.Parse(
                    response[response.LastIndexOf('{')..]);
                json = new Dictionary<string, object?>();
                foreach (var p in doc.RootElement.EnumerateObject())
                    json[p.Name] = ModelLifecycle.Decode(p.Value);
            }
            catch (Exception exc) when (
                exc is JsonException or ArgumentOutOfRangeException)
            {
                _dead = true;
                throw new ExecutorError("EXECUTOR_SERVE_SESSION_DEAD",
                    "serve produced no JSON: " +
                    response[..Math.Min(200, response.Length)]);
            }
            int exitCode = 0;
            if (json.TryGetValue("exit_code", out object? ec) && ec != null)
                try { exitCode = Convert.ToInt32(ec); } catch { exitCode = 1; }
            else if (!TransformerTrainingRepository.Truthy(
                         json.GetValueOrDefault("ok")))
                exitCode = 1;
            return (json, exitCode);
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            if (_disposed) return;
            _disposed = true;
            try
            {
                if (!_proc.HasExited)
                {
                    _stdin.WriteLine("{\"op\":\"quit\"}");
                    _stdin.Flush();
                    if (!_proc.WaitForExit(10000))
                        _proc.Kill(entireProcessTree: true);
                }
            }
            catch { /* teardown never throws */ }
            try { _stdin.Dispose(); } catch { }
            try { _proc.Dispose(); } catch { }
        }
    }
}
