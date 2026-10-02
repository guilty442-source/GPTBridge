// XingchengModelServiceExecutor — xingcheng's governed executor: owns the
// xc-model-service loopback HTTP surface (star-model-service/v1) and the
// resident xc_modeltool serve child (the native inference worker).
//
// Topology: this executor runs inside the xingcheng ToolHost process —
// the institution owns its own service process, lifecycle and identity.
// It opens an authenticated loopback listener, publishes the
// star-model-service-descriptor/v1 descriptor + session token under
// xingcheng/runtime/ipc/, and lazily spawns `xc_modeltool serve` the
// first time /v1/infer arrives — the 2.4 GB bundle load stays deferred
// until a consumer actually needs the engine.
//
// Fail-closed invariants:
//  - descriptor is only written after the listener is bound;
//  - descriptor + token file are removed on dispose;
//  - every HTTP request requires the X-GPTBridge-Session-Token header;
//  - a dead child fails the request truthfully (never fabricates text).
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal sealed class XingchengModelServiceExecutor
    : IGovernedCommandExecutor, IWsCommandSurface, IAsyncDisposable
{
    internal const string LifecycleOwner = "xingcheng/toolhost-model-service";
    internal const string ConsumerPolicy = "csharp-orchestrator-client-only";
    private const string TokenFileName =
        ModelServiceDescriptor.TokenFileName;
    private const string DescriptorFileName =
        ModelServiceDescriptor.DescriptorFileName;
    private static readonly TimeSpan ServeOpTimeout = TimeSpan.FromMinutes(10);

    // Read-only Codex diagnostic commands routed model-dialogue/
    // star-chat -> xingcheng (tool_routes.json). Both verbs run on the
    // governed CodexPipeline and never touch the model engine.
    private static readonly HashSet<string> OwnedCommands = new(
        StringComparer.Ordinal)
    {
        "xingcheng_codex_alignment",
        "xingcheng_codex_mirror_check",
    };

    private readonly GovernedEnvironment _env;
    private readonly string _ownerId;
    private readonly string _xcRoot;
    private readonly string _ipcDir;
    private readonly string _modeltoolExe;
    private readonly string _bundleDir;
    private readonly JsonObject _samplingDefaults;
    private readonly int _cpuThreads;
    private readonly bool _cppCuda;

    private readonly SemaphoreSlim _childLock = new(1, 1);
    private readonly CancellationTokenSource _cts = new();
    private HttpListener? _listener;
    private Task? _acceptLoop;
    private Task? _watchdog;
    private Process? _child;
    private int _port;
    private string _token = "";
    private volatile bool _engineLoaded;
    private volatile string? _lastError;
    private volatile string? _lastRelease;

    // AutoRelease (successor of the retired auto_release.py
    // AutoReleaseManager): the loaded engine is evicted when idle past
    // native-engine.json:auto_release_idle_seconds (default 300) or
    // under governed memory pressure (resource-governor model class
    // paused / critical memory). In-flight inference holds the child
    // lock and is never evicted mid-request.
    private readonly int _idleReleaseSeconds;
    private int _inflight;
    private long _lastActivityTicks = DateTime.UtcNow.Ticks;

    public XingchengModelServiceExecutor(GovernedEnvironment env, string ownerId)
    {
        _env = env;
        _ownerId = ownerId;
        _xcRoot = Path.Combine(env.ProjectRoot, "xingcheng");
        _ipcDir = Path.Combine(_xcRoot, "xingcheng", "runtime", "ipc");
        _modeltoolExe = Path.Combine(
            _xcRoot, "src", "backend", "services", "xingcheng",
            "infrastructure", "native_transformer", "tools",
            "xc_modeltool.exe");
        (_bundleDir, _samplingDefaults, _cpuThreads, _cppCuda,
            _idleReleaseSeconds) = ResolveBundle(_xcRoot);
    }

    /// <summary>Canonical Xingcheng-owned settings path; a pre-migration
    /// copy under the legacy pre-relocation settings dir is accepted
    /// read-only. Writes always target the canonical path.</summary>
    internal static string EngineSettingsPath(string toolRoot)
    {
        var canonical = Path.Combine(
            toolRoot, "xingcheng", "runtime", "settings",
            "native-engine.json");
        if (File.Exists(canonical)) return canonical;
        var legacy = Path.Combine(
            toolRoot, "runtime", "settings", "native-engine.json");
        return File.Exists(legacy) ? legacy : canonical;
    }

    /// <summary>
    /// Read xingcheng/runtime/settings/native-engine.json and resolve the
    /// pinned
    /// inference bundle. The checkpoint value may point at a bundle dir
    // directly or at a source .pt whose exported bundle is matched by
    /// source_checkpoint + size (parity with ModelServiceLocator).
    /// cpu_threads feeds the native core's striped-GEMM stripe count via
    /// GPTBRIDGE_MATMUL_THREADS on the worker process; absent/<=1 leaves
    /// the core's auto default.
    /// cpp_cuda is the governed GPU opt-in flag (native-engine.json):
    /// true requests the CUDA fp64 path after a device+VRAM admission
    /// probe; denial is CPU fail-soft, never a load failure.
    /// </summary>
    private static (string Bundle, JsonObject Defaults, int CpuThreads,
        bool CppCuda, int IdleReleaseSeconds)
        ResolveBundle(string toolRoot)
    {
        var settingsPath = EngineSettingsPath(toolRoot);
        if (!File.Exists(settingsPath))
            throw new InvalidOperationException(
                "XC_ENGINE_SETTINGS_MISSING");
        using var doc = JsonDocument.Parse(File.ReadAllText(settingsPath));
        var root = doc.RootElement;
        if (root.TryGetProperty("enabled", out var en)
            && en.ValueKind == JsonValueKind.False)
            throw new InvalidOperationException("XC_ENGINE_DISABLED");

        var defaults = new JsonObject();
        foreach (var key in new[]
                 {
                     "max_new_tokens", "temperature", "top_k", "top_p",
                     "repetition_penalty",
                 })
        {
            if (root.TryGetProperty(key, out var el)
                && el.ValueKind == JsonValueKind.Number)
                defaults[key] = el.GetDouble();
        }

        var checkpoint = root.TryGetProperty("checkpoint", out var c)
            ? c.GetString()
            : null;
        if (string.IsNullOrWhiteSpace(checkpoint))
            throw new InvalidOperationException("XC_BUNDLE_CHECKPOINT_UNPINNED");
        var checkpointPath = Path.GetFullPath(Path.IsPathRooted(checkpoint)
            ? checkpoint : Path.Combine(toolRoot, checkpoint));
        // Data residency: an out-of-boundary pin refuses to serve —
        // the pinned artifact must resolve inside a registered
        // xingcheng domain root.
        var inBoundary = new[]
        {
            Path.Combine(toolRoot, "xingcheng"),
        }.Any(r =>
        {
            var boundary = Path.GetFullPath(r);
            return checkpointPath.Equals(boundary,
                    StringComparison.OrdinalIgnoreCase)
                || checkpointPath.StartsWith(
                    boundary + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase);
        });
        if (!inBoundary)
            throw new InvalidOperationException(
                "XINGCHENG_DATA_BOUNDARY");
        var cpuThreads =
            root.TryGetProperty("cpu_threads", out var ct)
            && ct.ValueKind == JsonValueKind.Number
                ? ct.GetInt32()
                : 0;
        var cppCuda = root.TryGetProperty("cpp_cuda", out var cu)
            && cu.ValueKind == JsonValueKind.True;
        // AutoRelease idle budget (auto_release_idle_seconds, default
        // 300 s; 0/negative disables idle eviction — pressure eviction
        // stays armed either way).
        var idleReleaseSeconds =
            root.TryGetProperty("auto_release_idle_seconds", out var ar)
            && ar.ValueKind == JsonValueKind.Number
                ? Math.Max(0, ar.GetInt32())
                : 300;

        // Pinned bundle directory (current contract).
        if (Directory.Exists(checkpointPath)
            && IsBundleDir(checkpointPath))
            return (checkpointPath, defaults, cpuThreads, cppCuda,
                idleReleaseSeconds);

        // Legacy contract: checkpoint is the source .pt; find its bundle.
        if (File.Exists(checkpointPath))
        {
            var size = new FileInfo(checkpointPath).Length;
            var bundlesDir = Path.Combine(
                toolRoot, "xingcheng", "runtime", "models", "cpp-bundles");
            if (Directory.Exists(bundlesDir))
            {
                foreach (var dir in Directory.GetDirectories(bundlesDir))
                {
                    var manifest = Path.Combine(dir, "manifest.json");
                    if (!File.Exists(manifest)) continue;
                    try
                    {
                        using var m = JsonDocument.Parse(
                            File.ReadAllText(manifest));
                        var mr = m.RootElement;
                        if (mr.TryGetProperty("schema_version", out var sv)
                            is false
                            || sv.GetString()
                                != "star-native-inference-bundle/v1")
                            continue;
                        var source = mr.TryGetProperty("source_checkpoint",
                            out var sc) ? sc.GetString() : null;
                        if (string.IsNullOrWhiteSpace(source)
                            || !string.Equals(
                                Path.GetFullPath(source), checkpointPath,
                                StringComparison.OrdinalIgnoreCase))
                            continue;
                        if (mr.TryGetProperty("source_size", out var sz)
                            && sz.GetInt64() != size)
                            continue;
                        if (IsBundleDir(dir))
                            return (dir, defaults, cpuThreads, cppCuda,
                                idleReleaseSeconds);
                    }
                    catch (JsonException) { /* skip unreadable bundle */ }
                }
            }
        }
        throw new InvalidOperationException("XC_BUNDLE_MISSING");
    }

    private static bool IsBundleDir(string dir) =>
        File.Exists(Path.Combine(dir, "manifest.json"))
        && File.Exists(Path.Combine(dir, "weights.bin"))
        && File.Exists(Path.Combine(dir, "tokenizer.json"));

    // Governed GPU admission (cpp_cuda contract): device + toolkit must be
    // present and free VRAM must cover the resident fp64 weight set plus
    // workspace headroom. Any probe failure means denial → the worker is
    // spawned without the CUDA env and runs the CPU path (fail-soft).
    private const long GpuAdmissionFreeMb = 3400;

    private bool GpuAdmitted()
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = _modeltoolExe,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = new UTF8Encoding(false),
            };
            psi.ArgumentList.Add("probe-cuda");
            using var probe = Process.Start(psi);
            if (probe == null) return false;
            var stdout = probe.StandardOutput.ReadToEnd();
            if (!probe.WaitForExit(15000))
            {
                try { probe.Kill(); } catch { }
                return false;
            }
            if (probe.ExitCode != 0) return false;
            using var doc = JsonDocument.Parse(stdout);
            var cuda = doc.RootElement.GetProperty("cuda");
            return cuda.TryGetProperty("available", out var av)
                && av.ValueKind == JsonValueKind.True
                && cuda.TryGetProperty("vram_free_mb", out var fm)
                && fm.GetInt64() >= GpuAdmissionFreeMb;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Bind the loopback listener and publish the descriptor. Called by
    /// Program after env validation; failure here must abort startup
    /// (a model service that cannot publish its contract is fail-closed).
    /// </summary>
    public void StartService()
    {
        Directory.CreateDirectory(_ipcDir);
        _token = Convert.ToHexString(
            RandomNumberGenerator.GetBytes(32)).ToLowerInvariant();

        // Ephemeral loopback port: reserve via TcpListener, hand the port
        // to HttpListener (retry once on the inevitable small race).
        for (var attempt = 0; attempt < 3; attempt++)
        {
            var probe = new TcpListener(IPAddress.Loopback, 0);
            probe.Start();
            _port = ((IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            var listener = new HttpListener();
            listener.Prefixes.Add($"http://127.0.0.1:{_port}/");
            try
            {
                listener.Start();
                _listener = listener;
                break;
            }
            catch (HttpListenerException) when (attempt < 2) { }
        }
        if (_listener is null)
            throw new InvalidOperationException("MODEL_SERVICE_BIND_FAILED");

        var descriptor = new JsonObject
        {
            ["schema"] = ModelServiceDescriptor.Schema,
            ["tool_id"] = _ownerId,
            ["pid"] = Environment.ProcessId,
            ["port"] = _port,
            ["token_file"] = TokenFileName,
            ["lifecycle_owner"] = LifecycleOwner,
            ["consumer_policy"] = ConsumerPolicy,
            ["session_token_sha256"] = Convert.ToHexString(
                SHA256.HashData(Encoding.UTF8.GetBytes(_token)))
                .ToLowerInvariant(),
            ["created_at"] = DateTimeOffset.UtcNow.ToString("O"),
        };
        ModelServiceDescriptor.WriteAtomically(
            Path.Combine(_ipcDir, TokenFileName), _token + "\n");
        ModelServiceDescriptor.WriteAtomically(
            Path.Combine(_ipcDir, DescriptorFileName),
            descriptor.ToJsonString() + "\n");

        _acceptLoop = Task.Run(AcceptLoopAsync);
        _watchdog = Task.Run(AutoReleaseLoopAsync);
    }

    private static void WriteJson(
        HttpListenerResponse response, int status, JsonObject body)
    {
        var bytes = Encoding.UTF8.GetBytes(body.ToJsonString());
        response.StatusCode = status;
        response.ContentType = "application/json";
        response.ContentLength64 = bytes.Length;
        try
        {
            response.OutputStream.Write(bytes, 0, bytes.Length);
        }
        finally
        {
            response.Close();
        }
    }

    private async Task AcceptLoopAsync()
    {
        var ct = _cts.Token;
        while (!ct.IsCancellationRequested)
        {
            HttpListenerContext context;
            try
            {
                context = await _listener!.GetContextAsync()
                    .ConfigureAwait(false);
            }
            catch (Exception) when (ct.IsCancellationRequested
                                    || _listener is null
                                    || !_listener.IsListening)
            {
                break;
            }
            // Sequential handling: the engine is single-tenant anyway;
            // bounding concurrency here bounds it everywhere.
            try { await HandleAsync(context).ConfigureAwait(false); }
            catch (Exception ex)
            {
                _lastError = ex.Message;
                try
                {
                    WriteJson(context.Response, 500, new JsonObject
                    {
                        ["ok"] = false,
                        ["error_code"] = "MODEL_SERVICE_INTERNAL",
                    });
                }
                catch { /* response already broken */ }
            }
        }
    }

    private async Task HandleAsync(HttpListenerContext context)
    {
        var request = context.Request;
        var supplied = request.Headers["X-GPTBridge-Session-Token"] ?? "";
        if (!CryptographicOperations.FixedTimeEquals(
                Encoding.UTF8.GetBytes(supplied),
                Encoding.UTF8.GetBytes(_token)))
        {
            WriteJson(context.Response, 403, new JsonObject
            {
                ["ok"] = false, ["error_code"] = "MODEL_SERVICE_AUTH",
            });
            return;
        }
        var path = request.Url?.AbsolutePath ?? "";
        switch (path)
        {
            case "/v1/status" when request.HttpMethod == "GET":
            {
                var status = await ServeOpAsync(
                        new JsonObject { ["op"] = "status" },
                        spawnIfAbsent: false, _cts.Token)
                    .ConfigureAwait(false);
                status ??= new JsonObject
                {
                    ["ok"] = true,
                    ["service"] = "xc-model-service",
                    ["decoder"] = "native-cpp",
                    ["cpp_runtime"] = true,
                    ["loaded"] = false,
                    ["worker"] = "not-started",
                };
                status["service_port"] = _port;
                status["auto_release"] = new JsonObject
                {
                    ["idle_seconds"] = _idleReleaseSeconds,
                    ["inflight"] = Volatile.Read(ref _inflight),
                    ["idle_for_s"] = (DateTime.UtcNow.Ticks
                        - Interlocked.Read(ref _lastActivityTicks))
                        / TimeSpan.TicksPerSecond,
                    ["last_release"] = _lastRelease,
                };
                WriteJson(context.Response, 200, status);
                return;
            }
            case "/v1/infer" when request.HttpMethod == "POST":
            {
                JsonObject body;
                try
                {
                    body = JsonNode.Parse(
                        await new StreamReader(request.InputStream)
                            .ReadToEndAsync(_cts.Token)
                            .ConfigureAwait(false)) as JsonObject
                        ?? throw new InvalidOperationException("BODY");
                }
                catch (Exception)
                {
                    WriteJson(context.Response, 400, new JsonObject
                    {
                        ["ok"] = false,
                        ["error_code"] = "MODEL_REQUEST_INVALID",
                    });
                    return;
                }
                var op = new JsonObject { ["op"] = "infer" };
                foreach (var key in new[]
                         {
                             "prompt", "messages", "max_new_tokens",
                             "temperature", "top_k", "top_p",
                             "repetition_penalty", "do_sample", "seed",
                         })
                {
                    if (body[key] is { } v) op[key] = v.DeepClone();
                }
                // Settings defaults fill anything the caller omitted —
                // the governed settings file owns the policy numbers.
                foreach (var (key, value) in _samplingDefaults)
                    if (op[key] is null && value is not null)
                        op[key] = value.DeepClone();
                // In-flight reference: the request holds a strong ref
                // on the engine — the AutoRelease watchdog can never
                // evict while this counter is non-zero.
                Interlocked.Increment(ref _inflight);
                Interlocked.Exchange(ref _lastActivityTicks,
                    DateTime.UtcNow.Ticks);
                JsonObject? reply;
                try
                {
                    reply = await ServeOpAsync(
                            op, spawnIfAbsent: true, _cts.Token)
                        .ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    // Host shutdown mid-infer — the child teardown path
                    // owns cleanup; answer the socket honestly.
                    reply = new JsonObject
                    {
                        ["ok"] = false,
                        ["error_code"] = "MODEL_SERVICE_SHUTDOWN",
                    };
                }
                catch (Exception ex)
                {
                    reply = new JsonObject
                    {
                        ["ok"] = false,
                        ["error_code"] = "MODEL_INFERENCE_FAILED",
                        ["message"] = ex.Message.Length > 240
                            ? ex.Message[..240] : ex.Message,
                    };
                }
                finally
                {
                    Interlocked.Exchange(ref _lastActivityTicks,
                        DateTime.UtcNow.Ticks);
                    Interlocked.Decrement(ref _inflight);
                }
                WriteJson(context.Response,
                    reply?["ok"]?.GetValue<bool>() == true ? 200 : 500,
                    reply ?? new JsonObject
                    {
                        ["ok"] = false,
                        ["error_code"] = "MODEL_INFERENCE_FAILED",
                    });
                return;
            }
            case "/v1/release" when request.HttpMethod == "POST":
            {
                var reply = await ServeOpAsync(
                        new JsonObject { ["op"] = "unload" },
                        spawnIfAbsent: false, _cts.Token)
                    .ConfigureAwait(false)
                    ?? new JsonObject
                    {
                        ["ok"] = true, ["released"] = new JsonArray(),
                        ["loaded"] = false,
                    };
                WriteJson(context.Response, 200, reply);
                return;
            }
            default:
                WriteJson(context.Response, 404, new JsonObject
                {
                    ["ok"] = false, ["error_code"] = "MODEL_ROUTE_UNKNOWN",
                });
                return;
        }
    }

    /// <summary>
    /// Round-trip one op against the serve child: serialize under the
    /// child lock, spawn lazily, and treat a dead worker as a bounded
    /// respawn once per call. Returns null when no child is running and
    /// spawning was not requested (status/release stay cheap).
    /// </summary>
    private async Task<JsonObject?> ServeOpAsync(
        JsonObject op, bool spawnIfAbsent, CancellationToken ct)
    {
        await _childLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var child = EnsureChild(spawnIfAbsent);
            if (child is null) return null;

            var line = op.ToJsonString() + "\n";
            await child.StandardInput.WriteAsync(
                    line.AsMemory(), ct).ConfigureAwait(false);
            await child.StandardInput.FlushAsync(ct).ConfigureAwait(false);

            using var timeout = CancellationTokenSource
                .CreateLinkedTokenSource(ct, _cts.Token);
            timeout.CancelAfter(ServeOpTimeout);
            string? response;
            try
            {
                response = await child.StandardOutput
                    .ReadLineAsync(timeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
                when (!ct.IsCancellationRequested
                      && !_cts.IsCancellationRequested)
            {
                // Op timeout: kill the worker — its stdout framing is
                // unrecoverable once a response was half-written.
                KillChild();
                throw new InvalidOperationException(
                    "MODEL_WORKER_TIMEOUT");
            }
            if (response is null)
            {
                // EOF: child exited mid-request.
                var dead = _child;
                _child = null;
                try { dead?.Kill(); } catch { /* already dead */ }
                throw new InvalidOperationException(
                    "MODEL_WORKER_EXITED");
            }
            var parsed = JsonNode.Parse(response) as JsonObject;
            _engineLoaded = parsed?["loaded"]?.GetValue<bool>()
                ?? _engineLoaded;
            if (op["op"]?.GetValue<string>() == "infer"
                && parsed?["ok"]?.GetValue<bool>() == true)
                _engineLoaded = true;
            if (op["op"]?.GetValue<string>() == "unload")
                _engineLoaded = false;
            return parsed ?? new JsonObject
            {
                ["ok"] = false, ["error_code"] = "MODEL_WORKER_BAD_REPLY",
            };
        }
        finally
        {
            _childLock.Release();
        }
    }

    private Process? EnsureChild(bool spawn)
    {
        if (_child is { HasExited: false }) return _child;
        var dead = _child;
        _child = null;
        try { dead?.Kill(); } catch { /* already dead */ }
        dead?.Dispose();
        if (!spawn) return null;
        if (!File.Exists(_modeltoolExe))
            throw new InvalidOperationException("XC_MODELTOOL_MISSING");
        var psi = new ProcessStartInfo
        {
            FileName = _modeltoolExe,
            WorkingDirectory = _xcRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
        };
        psi.ArgumentList.Add("serve");
        psi.ArgumentList.Add("--bundle");
        psi.ArgumentList.Add(_bundleDir);
        if (_cpuThreads > 0)
            psi.Environment["GPTBRIDGE_MATMUL_THREADS"] =
                _cpuThreads.ToString();
        if (_cppCuda && GpuAdmitted())
        {
            psi.Environment["XINGCHENG_CPP_CUDA"] = "1";
            psi.Environment["XINGCHENG_CPP_CUDA_KV"] = "1";
        }
        var child = Process.Start(psi)
            ?? throw new InvalidOperationException("MODEL_WORKER_SPAWN_FAILED");
        _ = Task.Run(async () =>
        {
            try
            {
                var err = await child.StandardError.ReadToEndAsync();
                if (!string.IsNullOrWhiteSpace(err)) _lastError = err.Trim();
            }
            catch { /* child pipe closed */ }
        });
        _child = child;
        return child;
    }

    // ---------------------------------------------------- auto-release --

    /// <summary>Idle/pressure eviction watchdog (AutoReleaseManager
    /// successor). Periodically checks: engine loaded → no in-flight
    /// request → idle timeout or governed memory pressure → send the
    /// serve child's ``unload`` op (graceful eviction; the worker
    /// process stays resident-but-empty). Every eviction appends an
    /// audit entry to xingcheng/runtime/logs/auto-release.jsonl.</summary>
    private async Task AutoReleaseLoopAsync()
    {
        var ct = _cts.Token;
        var interval = TimeSpan.FromSeconds(
            Math.Clamp(_idleReleaseSeconds / 4, 15, 60));
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(interval, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) { break; }
            if (!_engineLoaded) continue;
            if (Volatile.Read(ref _inflight) > 0) continue;

            string? reason = null;
            var idleS = (DateTime.UtcNow.Ticks
                - Interlocked.Read(ref _lastActivityTicks))
                / TimeSpan.TicksPerSecond;
            if (_idleReleaseSeconds > 0
                && idleS >= _idleReleaseSeconds)
                reason = $"idle-timeout:{idleS}s>={_idleReleaseSeconds}s";
            var pressure = MemoryPressure();
            if (reason is null && pressure is not null)
                reason = pressure;
            if (reason is null) continue;

            // Re-check under the op lock implicitly: ServeOpAsync
            // serializes against any infer that started meanwhile —
            // worst case a just-completed engine unloads once and the
            // next request reloads it (never mid-request eviction).
            var reply = await ServeOpAsync(
                    new JsonObject { ["op"] = "unload" },
                    spawnIfAbsent: false, ct)
                .ConfigureAwait(false);
            var evicted = reply?["ok"]?.GetValue<bool>() != false;
            _lastRelease =
                $"{reason} at {DateTime.UtcNow:O} (evicted={evicted})";
            AppendReleaseAudit(reason, evicted, idleS);
        }
    }

    /// <summary>Governed memory-pressure signal: the resource
    /// governor's concurrency budget marks the model class
    /// ``paused`` (shed signal) or reports critical memory usage.</summary>
    private string? MemoryPressure()
    {
        try
        {
            var state = Path.Combine(_env.ProjectRoot, "main-system",
                "runtime", "state", "resource-governor.json");
            if (!File.Exists(state)) return null;
            using var doc = JsonDocument.Parse(File.ReadAllText(state));
            var root = doc.RootElement;
            if (root.TryGetProperty("concurrency_budget", out var cb)
                && cb.TryGetProperty("classes", out var classes)
                && classes.TryGetProperty("model", out var model)
                && model.TryGetProperty("state", out var s)
                && s.GetString() == "paused")
                return "memory-pressure:model-class-paused";
            if (root.TryGetProperty("mem_used_pct", out var m)
                && m.ValueKind == JsonValueKind.Number
                && m.GetDouble() >= 92.0)
                return "memory-pressure:mem-used>=92%";
        }
        catch { /* unreadable governor state → no pressure signal */ }
        return null;
    }

    private void AppendReleaseAudit(
        string reason, bool evicted, long idleSeconds)
    {
        try
        {
            var logs = Path.Combine(
                _xcRoot, "xingcheng", "runtime", "logs");
            Directory.CreateDirectory(logs);
            var entry = new JsonObject
            {
                ["format"] = "star-auto-release/v1",
                ["at"] = DateTimeOffset.UtcNow.ToString("O"),
                ["event"] = "engine-evict",
                ["reason"] = reason,
                ["evicted"] = evicted,
                ["idle_seconds"] = idleSeconds,
                ["bundle"] = Path.GetFileName(_bundleDir),
            };
            File.AppendAllText(
                Path.Combine(logs, "auto-release.jsonl"),
                entry.ToJsonString() + "\n",
                new UTF8Encoding(false));
        }
        catch { /* audit is best-effort; eviction already happened */ }
    }

    private void KillChild()
    {
        var dead = _child;
        _child = null;
        _engineLoaded = false;
        if (dead is null) return;
        try { dead.Kill(entireProcessTree: true); } catch { }
        try { dead.WaitForExit(3000); } catch { }
        dead.Dispose();
    }

    // IWsCommandSurface — the two read-only Codex diagnostic commands
    // (tool_routes.json: (model-dialogue|star-chat) -> xingcheng). Both
    // delegate to CodexDiagnostics over the governed CodexPipeline
    // subprocess; neither requires the model engine.
    public bool OwnsCommand(string command) =>
        OwnedCommands.Contains(command);

    public async Task<(string Event, JsonObject Result)> ExecuteWsAsync(
        string command, JsonObject payload, string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken cancellationToken)
    {
        var result = command switch
        {
            "xingcheng_codex_alignment" => await CodexDiagnostics
                .RunAsync(_env, "--arch-docs", cancellationToken)
                .ConfigureAwait(false),
            "xingcheng_codex_mirror_check" => await CodexDiagnostics
                .RunAsync(_env, "--mirror-check", cancellationToken)
                .ConfigureAwait(false),
            _ => throw new PermissionDeniedException(),
        };
        result["tool_id"] = _ownerId;
        result["command"] = command;
        return ($"{command}_result", result);
    }

    // IGovernedCommandExecutor — the store claim lane carries no
    // xingcheng business commands; WS commands are the read-only
    // diagnostics above (the model service is reached via HTTP).
    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
        => OwnedCommands.Contains(command)
            ? ExecuteWsAsync(command, payload, requestId, null,
                cancellationToken)
            : Task.FromResult(($"{command}_result", new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = _ownerId,
                ["error_code"] = "COMMAND_NOT_OWNED",
                ["message"] =
                    "xingcheng owns the model-service HTTP surface; " +
                    "the command lane is not implemented for this tool.",
            }));

    public JsonObject Health() => new()
    {
        ["model_service"] = new JsonObject
        {
            ["running"] = _listener?.IsListening == true,
            ["port"] = _port,
            ["engine_loaded"] = _engineLoaded,
            ["worker_alive"] = _child is { HasExited: false },
            ["bundle"] = Path.GetFileName(_bundleDir),
            ["last_error"] = _lastError,
            ["auto_release"] = new JsonObject
            {
                ["idle_seconds"] = _idleReleaseSeconds,
                ["inflight"] = Volatile.Read(ref _inflight),
                ["last_release"] = _lastRelease,
            },
        },
        ["executor_state"] = "active:model-service",
    };

    public async ValueTask DisposeAsync()
    {
        _cts.Cancel();
        try { _listener?.Stop(); } catch { /* not started */ }
        if (_acceptLoop is not null)
        {
            try { await _acceptLoop.ConfigureAwait(false); }
            catch { /* shutdown */ }
        }
        // Teardown takes _childLock: an in-flight ServeOpAsync owns the
        // child's stdin/stdout framing, and its awaits are all linked to
        // _cts — cancelled above — so it drains promptly and releases.
        // No new ServeOpAsync can start (accept loop joined). Waiting
        // without a token: shutdown must not fail-open on an already
        // cancelled token.
        await _childLock.WaitAsync().ConfigureAwait(false);
        try
        {
            // Ask the worker to quit before killing — a clean quit unloads
            // the engine's mapped weights instead of abandoning them.
            if (_child is { HasExited: false } child)
            {
                try
                {
                    await child.StandardInput
                        .WriteLineAsync("{\"op\":\"quit\"}")
                        .ConfigureAwait(false);
                    await child.StandardInput.FlushAsync()
                        .ConfigureAwait(false);
                    if (!child.WaitForExit(5000)) KillChild();
                }
                catch { KillChild(); }
            }
            else
            {
                KillChild();
            }
        }
        finally
        {
            _childLock.Release();
        }
        foreach (var name in new[] { DescriptorFileName, TokenFileName })
        {
            try { File.Delete(Path.Combine(_ipcDir, name)); }
            catch { /* descriptor cleanup is best-effort */ }
        }
        _listener?.Close();
        _cts.Dispose();
        _childLock.Dispose();
    }
}

/// <summary>Single owned implementation of the
/// star-model-service-descriptor/v1 file contract. Writer:
/// XingchengModelServiceExecutor.StartService. Reader:
/// ModelDialogueExecutor.DiscoverService (StarBusinessLogic's
/// ModelServiceLocator.Discover is the orphan twin kept in parity by
/// inspection — it is not referenced by this host). Wire format and
/// validation are unchanged; this type only removes the second copy of
/// the parse/validate logic so the two executors cannot drift.</summary>
internal static class ModelServiceDescriptor
{
    public const string Schema = "star-model-service-descriptor/v1";
    public const string DescriptorFileName = "model-service.json";
    public const string TokenFileName = "model-service-session-token";

    public sealed record Endpoint(string Url, int Port, string SessionToken);

    public static void WriteAtomically(string path, string content)
    {
        var tmp = path + "." + Environment.ProcessId + ".tmp";
        File.WriteAllText(tmp, content, new UTF8Encoding(false));
        if (File.Exists(path)) File.Delete(path);
        File.Move(tmp, path);
    }

    public static Endpoint? TryRead(
        string ipcDir, string lifecycleOwner, string consumerPolicy)
    {
        var descriptorPath = Path.Combine(ipcDir, DescriptorFileName);
        if (!File.Exists(descriptorPath)) return null;
        try
        {
            using var doc = JsonDocument.Parse(
                File.ReadAllText(descriptorPath));
            var root = doc.RootElement;
            if (root.TryGetProperty("schema", out var s)
                is false
                || s.GetString() != Schema)
                return null;
            var port = root.TryGetProperty("port", out var p)
                ? p.GetInt32() : 0;
            if (port < 1 || port > 65535) return null;
            var tokenFile = root.TryGetProperty("token_file", out var tf)
                ? tf.GetString() : null;
            if (string.IsNullOrWhiteSpace(tokenFile)
                || tokenFile.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
                || tokenFile.Contains(Path.DirectorySeparatorChar)
                || tokenFile.Contains(Path.AltDirectorySeparatorChar))
                return null;
            var token = File.ReadAllText(
                Path.Combine(ipcDir, tokenFile)).Trim();
            if (token.Length == 0) return null;
            var owner = root.TryGetProperty("lifecycle_owner", out var lo)
                ? lo.GetString() : null;
            if (owner != lifecycleOwner) return null;
            if (root.TryGetProperty("consumer_policy", out var cp)
                && cp.GetString() != consumerPolicy)
                return null;
            return new Endpoint($"http://127.0.0.1:{port}", port, token);
        }
        catch { return null; }
    }
}
