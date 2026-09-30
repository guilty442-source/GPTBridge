// LocalModelExecutor — local-model's governed executor: owns the
// xc-model-service loopback HTTP surface (star-model-service/v1) and the
// resident xc_modeltool serve child (the native inference worker).
//
// Topology: this executor runs inside the local-model ToolHost process.
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

internal sealed class LocalModelExecutor
    : IGovernedCommandExecutor, IAsyncDisposable
{
    internal const string LifecycleOwner = "local-model/toolhost-model-service";
    internal const string ConsumerPolicy = "csharp-orchestrator-client-only";
    private const string TokenFileName = "model-service-session-token";
    private const string DescriptorFileName = "model-service.json";
    private static readonly TimeSpan ServeOpTimeout = TimeSpan.FromMinutes(10);

    private readonly GovernedEnvironment _env;
    private readonly string _ownerId;
    private readonly string _ipcDir;
    private readonly string _modeltoolExe;
    private readonly string _bundleDir;
    private readonly JsonObject _samplingDefaults;

    private readonly SemaphoreSlim _childLock = new(1, 1);
    private readonly CancellationTokenSource _cts = new();
    private HttpListener? _listener;
    private Task? _acceptLoop;
    private Process? _child;
    private int _port;
    private string _token = "";
    private volatile bool _engineLoaded;
    private volatile string? _lastError;

    public LocalModelExecutor(GovernedEnvironment env, string ownerId)
    {
        _env = env;
        _ownerId = ownerId;
        _ipcDir = Path.Combine(env.ToolRoot, "xingcheng", "runtime", "ipc");
        _modeltoolExe = Path.Combine(
            env.ToolRoot, "src", "backend", "services", "xingcheng",
            "infrastructure", "native_transformer", "tools",
            "xc_modeltool.exe");
        (_bundleDir, _samplingDefaults) = ResolveBundle(env.ToolRoot);
    }

    /// <summary>
    /// Read runtime/settings/native-engine.json and resolve the pinned
    /// inference bundle. The checkpoint value may point at a bundle dir
    // directly or at a source .pt whose exported bundle is matched by
    /// source_checkpoint + size (parity with ModelServiceLocator).
    /// </summary>
    private static (string Bundle, JsonObject Defaults) ResolveBundle(
        string toolRoot)
    {
        var settingsPath = Path.Combine(
            toolRoot, "runtime", "settings", "native-engine.json");
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

        // Pinned bundle directory (current contract).
        if (Directory.Exists(checkpointPath)
            && IsBundleDir(checkpointPath))
            return (checkpointPath, defaults);

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
                        if (IsBundleDir(dir)) return (dir, defaults);
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
            ["schema"] = "star-model-service-descriptor/v1",
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
        WriteAtomically(
            Path.Combine(_ipcDir, TokenFileName), _token + "\n");
        WriteAtomically(
            Path.Combine(_ipcDir, DescriptorFileName),
            descriptor.ToJsonString() + "\n");

        _acceptLoop = Task.Run(AcceptLoopAsync);
    }

    private static void WriteAtomically(string path, string content)
    {
        var tmp = path + "." + Environment.ProcessId + ".tmp";
        File.WriteAllText(tmp, content, new UTF8Encoding(false));
        if (File.Exists(path)) File.Delete(path);
        File.Move(tmp, path);
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
            WorkingDirectory = _env.ToolRoot,
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

    // IGovernedCommandExecutor — the store claim lane carries no
    // local-model business commands today; WS commands are not owned by
    // this executor either (the model service is reached via HTTP).
    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
        => Task.FromResult(($"{command}_result", new JsonObject
        {
            ["ok"] = false,
            ["tool_id"] = _ownerId,
            ["error_code"] = "COMMAND_NOT_OWNED",
            ["message"] =
                "local-model owns the model-service HTTP surface; " +
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
        // Ask the worker to quit before killing — a clean quit unloads
        // the engine's mapped weights instead of abandoning them.
        if (_child is { HasExited: false } child)
        {
            try
            {
                await child.StandardInput
                    .WriteLineAsync("{\"op\":\"quit\"}")
                    .ConfigureAwait(false);
                await child.StandardInput.FlushAsync().ConfigureAwait(false);
                if (!child.WaitForExit(5000)) KillChild();
            }
            catch { KillChild(); }
        }
        else
        {
            KillChild();
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
