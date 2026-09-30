// HTTP/WS boundary for the governed tool runtime — mirrors
// GovernedRuntimeMaintenanceMixin.run(): /health, /metrics, /shutdown
// plus the token/instance-authenticated WebSocket command path.
using System.Net;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;

namespace GPTBridge.ToolHost;

public sealed class ToolHostServer : IAsyncDisposable
{
    private readonly GovernedToolHost _host;
    private readonly HttpListener _listener = new();
    private readonly CancellationTokenSource _cts = new();
    private Task? _acceptLoop;
    private Task[] _workers = [];
    // bounded-concurrency/v1: declared envelope; the effective worker
    // count is the governor "network" class quota (concurrency-budget/v1)
    // clamped into [MinWorkers, MaxWorkers]; unreadable state fails open
    // to MaxWorkers — a dead governor never deadlocks the host.
    private const int MinWorkers = 2;
    private const int MaxWorkers = 8;
    private const int PendingCapacity = 64;
    private Channel<HttpListenerContext>? _pending;
    private long _rejected;

    public ToolHostServer(GovernedToolHost host)
    {
        _host = host;
        _listener.Prefixes.Add($"http://127.0.0.1:{host.Port}/");
    }

    /// <summary>Rejected-over-capacity connection count (drop metric).</summary>
    public long RejectedConnections => Interlocked.Read(ref _rejected);

    /// <summary>`concurrency-budget/v1` read side: classes.network.quota
    /// from the governor state file; fail-open to MaxWorkers.</summary>
    private static int ResolveConnWorkers()
    {
        try
        {
            var path = Environment.GetEnvironmentVariable(
                "GPTBRIDGE_GOVERNOR_STATE");
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                return MaxWorkers;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.TryGetProperty("disabled", out var d)
                && d.ValueKind == JsonValueKind.True)
                return MaxWorkers;
            if (!root.TryGetProperty("concurrency_budget", out var budget)
                || budget.GetProperty("contract").GetString()
                    != "concurrency-budget/v1")
                return MaxWorkers;
            var quota = budget.GetProperty("classes")
                .GetProperty("network").GetProperty("quota").GetInt32();
            return Math.Clamp(quota > 0 ? quota : MinWorkers,
                              MinWorkers, MaxWorkers);
        }
        catch
        {
            return MaxWorkers;
        }
    }

    public void Start()
    {
        _listener.Start();
        // bounded queue + fixed worker pool — never a Task per request.
        _pending = Channel.CreateBounded<HttpListenerContext>(
            new BoundedChannelOptions(PendingCapacity)
            {
                FullMode = BoundedChannelFullMode.Wait,
                SingleReader = false,
                SingleWriter = true,
            });
        var workers = ResolveConnWorkers();
        _workers = new Task[workers];
        for (var i = 0; i < workers; i++)
            _workers[i] = Task.Run(WorkerLoopAsync);
        _acceptLoop = Task.Run(AcceptLoopAsync);
    }

    private async Task WorkerLoopAsync()
    {
        var reader = _pending!.Reader;
        try
        {
            while (await reader.WaitToReadAsync(_cts.Token)
                       .ConfigureAwait(false))
            {
                while (reader.TryRead(out var context))
                    await HandleContextAsync(context).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
        {
            /* shutdown: queued-but-unclaimed contexts are dropped */
        }
    }

    private static bool FixedTimeEquals(string a, string b) =>
        CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(a), Encoding.UTF8.GetBytes(b));

    private async Task AcceptLoopAsync()
    {
        while (!_cts.IsCancellationRequested)
        {
            HttpListenerContext context;
            try
            {
                context = await _listener.GetContextAsync()
                    .ConfigureAwait(false);
            }
            catch (Exception) when (_cts.IsCancellationRequested)
            {
                break;
            }
            catch (HttpListenerException)
            {
                break;
            }
            // backpressure + drop/reject: wait briefly for queue room,
            // then reject with 503 — the accepted socket is never
            // handed to a new Task per request.
            try
            {
                using var admission = CancellationTokenSource
                    .CreateLinkedTokenSource(_cts.Token);
                admission.CancelAfter(TimeSpan.FromSeconds(2));
                await _pending!.Writer
                    .WriteAsync(context, admission.Token)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
                when (!_cts.IsCancellationRequested)
            {
                Interlocked.Increment(ref _rejected);
                WriteText(context.Response, 503, "capacity-exhausted");
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private async Task HandleContextAsync(HttpListenerContext context)
    {
        try
        {
            var path = context.Request.Url?.AbsolutePath ?? "/";
            switch (path)
            {
                case "/health":
                    WriteJson(context.Response, 200,
                        _host.HealthSnapshot().ToJsonString());
                    return;
                case "/metrics":
                    WriteJson(context.Response, 200,
                        _host.MetricsSnapshot().ToJsonString());
                    return;
                case "/shutdown":
                    HandleShutdown(context);
                    return;
                default:
                    await HandleWebSocketPathAsync(context)
                        .ConfigureAwait(false);
                    return;
            }
        }
        catch (Exception)
        {
            try
            {
                context.Response.StatusCode = 500;
                context.Response.Close();
            }
            catch { /* best-effort */ }
        }
    }

    private void HandleShutdown(HttpListenerContext context)
    {
        var supplied = context.Request
            .Headers["X-GPTBridge-Shutdown-Token"] ?? "";
        if (_host.ShutdownTokenValue.Length == 0
            || !FixedTimeEquals(supplied, _host.ShutdownTokenValue))
        {
            WriteText(context.Response, 403, "Forbidden");
            return;
        }
        _host.RequestShutdown();
        WriteText(context.Response, 200, "OK");
    }

    private async Task HandleWebSocketPathAsync(HttpListenerContext context)
    {
        var query = context.Request.QueryString;
        var token = (query["token"] ?? "").ToLowerInvariant();
        var instance = query["instance"] ?? "";
        if (!FixedTimeEquals(token, _host.SessionToken)
            || instance != _host.WorkspaceInstanceId)
        {
            WriteText(context.Response, 403, "Forbidden");
            return;
        }
        if (!context.Request.IsWebSocketRequest)
        {
            WriteText(context.Response, 400, "Bad Request");
            return;
        }
        var wsContext = await context
            .AcceptWebSocketAsync(null).ConfigureAwait(false);
        await DrainWebSocketAsync(wsContext.WebSocket).ConfigureAwait(false);
    }

    /// <summary>Per-connection state: one writer at a time, a linked
    /// cancellation scope, and the running executor dispatches.</summary>
    private sealed class WsConnection : IDisposable
    {
        public readonly SemaphoreSlim SendLock = new(1, 1);
        public readonly CancellationTokenSource ConnCts = new();
        public readonly List<Task> Running = new();
        public readonly object RunningLock = new();

        public void Dispose()
        {
            ConnCts.Dispose();
            SendLock.Dispose();
        }
    }

    private async Task DrainWebSocketAsync(WebSocket socket)
    {
        var buffer = new byte[64 * 1024];
        using var conn = new WsConnection();
        try
        {
            while (socket.State == WebSocketState.Open
                   && !_cts.IsCancellationRequested)
            {
                var message = new MemoryStream();
                WebSocketReceiveResult frame;
                do
                {
                    frame = await socket.ReceiveAsync(
                        buffer, _cts.Token).ConfigureAwait(false);
                    if (frame.MessageType == WebSocketMessageType.Close)
                    {
                        await socket.CloseAsync(
                            WebSocketCloseStatus.NormalClosure, null,
                            CancellationToken.None).ConfigureAwait(false);
                        return;
                    }
                    message.Write(buffer, 0, frame.Count);
                }
                while (!frame.EndOfMessage);
                if (frame.MessageType != WebSocketMessageType.Text)
                    continue;
                await HandleWsMessageAsync(socket, conn, message.ToArray())
                    .ConfigureAwait(false);
            }
        }
        catch (WebSocketException) { /* client disconnected mid-stream */ }
        catch (OperationCanceledException) { /* shutdown */ }
        finally
        {
            // Socket teardown cancels the in-flight executor work it
            // carried; dispatched tasks may still be racing their own
            // cleanup, so give them a bounded drain before returning.
            conn.ConnCts.Cancel();
            Task[] running;
            lock (conn.RunningLock) running = conn.Running.ToArray();
            if (running.Length > 0)
            {
                try
                {
                    await Task.WhenAll(running)
                        .WaitAsync(TimeSpan.FromSeconds(5))
                        .ConfigureAwait(false);
                }
                catch { /* timeout/cancel — bounded drain is best-effort */ }
            }
        }
    }

    private async Task HandleWsMessageAsync(
        WebSocket socket, WsConnection conn, byte[] raw)
    {
        string command = "";
        string requestId = "";
        try
        {
            var message = JsonNode.Parse(raw) as JsonObject;
            command = message?["command"]?.GetValue<string>()?.Trim() ?? "";
            var payload = message?["payload"] as JsonObject;
            if (command.Length == 0 || payload is null)
                throw new PermissionDeniedException();
            requestId = payload["request_id"]?.GetValue<string>()?.Trim()
                ?? "";
            if (command == "toolbox_cancel_tool_run")
            {
                var cancelled = requestId.Length > 0
                    && _host.CancelRequest(requestId);
                await SendWsJsonAsync(socket, conn, new JsonObject
                {
                    ["event"] = "toolbox_cancel_tool_run_result",
                    ["payload"] = new JsonObject
                    {
                        ["ok"] = cancelled,
                        ["cancelled"] = cancelled,
                        ["tool_id"] = _host.ToolId,
                        ["request_id"] = requestId,
                    },
                }).ConfigureAwait(false);
                return;
            }
            // design §10: executor-declared commands dispatch on a
            // detached task so the read loop stays live for cancel and
            // heartbeat traffic while generation runs. Unknown commands
            // stay PERMISSION_DENIED — the surface is an allowlist, not
            // a passthrough.
            if (_host.OwnsWsCommand(command))
            {
                var task = RunWsCommandAsync(
                    socket, conn, command, (JsonObject)payload.DeepClone(),
                    requestId);
                lock (conn.RunningLock) conn.Running.Add(task);
                _ = task.ContinueWith(t =>
                {
                    lock (conn.RunningLock) conn.Running.Remove(t);
                }, TaskScheduler.Default);
                return;
            }
            throw new PermissionDeniedException();
        }
        catch (JsonException)
        {
            await SendWsJsonAsync(socket, conn, new JsonObject
            {
                ["event"] = "error",
                ["payload"] = new JsonObject { ["ok"] = false },
            }).ConfigureAwait(false);
        }
        catch (Exception)
        {
            await SendWsJsonAsync(socket, conn, new JsonObject
            {
                ["event"] = command.Length > 0 ? $"{command}_result" : "error",
                ["payload"] = new JsonObject
                {
                    ["ok"] = false,
                    ["tool_id"] = _host.ToolId,
                    ["request_id"] = requestId,
                    ["error_code"] = "PERMISSION_DENIED",
                    ["message"] = "PERMISSION_DENIED",
                },
            }).ConfigureAwait(false);
        }
    }

    private async Task RunWsCommandAsync(
        WebSocket socket,
        WsConnection conn,
        string command,
        JsonObject payload,
        string requestId)
    {
        try
        {
            Task EmitProgress(JsonObject progress)
            {
                progress["request_id"] = requestId;
                return SendWsJsonAsync(socket, conn, new JsonObject
                {
                    ["event"] = $"{command}_progress",
                    ["payload"] = progress,
                });
            }
            var (evt, result) = await _host.DispatchWsAsync(
                command, payload, requestId, EmitProgress,
                conn.ConnCts.Token).ConfigureAwait(false);
            result["request_id"] = requestId;
            await SendWsJsonAsync(socket, conn, new JsonObject
            {
                ["event"] = evt,
                ["payload"] = result,
            }).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            await SendWsJsonAsync(socket, conn, new JsonObject
            {
                ["event"] = $"{command}_result",
                ["payload"] = new JsonObject
                {
                    ["ok"] = false,
                    ["tool_id"] = _host.ToolId,
                    ["request_id"] = requestId,
                    ["error_code"] = "CANCELLED",
                    ["message"] = "Request cancelled.",
                },
            }).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            await SendWsJsonAsync(socket, conn, new JsonObject
            {
                ["event"] = $"{command}_result",
                ["payload"] = new JsonObject
                {
                    ["ok"] = false,
                    ["tool_id"] = _host.ToolId,
                    ["request_id"] = requestId,
                    ["error_code"] = "TOOL_COMMAND_FAILED",
                    ["message"] = ex.Message.Length > 240
                        ? ex.Message[..240] : ex.Message,
                },
            }).ConfigureAwait(false);
        }
    }

    private static async Task SendWsJsonAsync(
        WebSocket socket, WsConnection conn, JsonObject message)
    {
        var bytes = Encoding.UTF8.GetBytes(message.ToJsonString());
        await conn.SendLock.WaitAsync(CancellationToken.None)
            .ConfigureAwait(false);
        try
        {
            if (socket.State != WebSocketState.Open)
                return;
            await socket.SendAsync(
                bytes, WebSocketMessageType.Text, endOfMessage: true,
                CancellationToken.None).ConfigureAwait(false);
        }
        finally
        {
            conn.SendLock.Release();
        }
    }

    private static void WriteJson(
        HttpListenerResponse response, int status, string body)
    {
        var bytes = Encoding.UTF8.GetBytes(body);
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

    private static void WriteText(
        HttpListenerResponse response, int status, string body)
    {
        var bytes = Encoding.UTF8.GetBytes(body);
        response.StatusCode = status;
        response.ContentType = "text/plain";
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

    public async ValueTask DisposeAsync()
    {
        _cts.Cancel();
        _pending?.Writer.TryComplete();
        try { _listener.Stop(); }
        catch { /* not started */ }
        if (_acceptLoop is not null)
        {
            try { await _acceptLoop.ConfigureAwait(false); }
            catch { /* shutdown */ }
        }
        try { await Task.WhenAll(_workers).ConfigureAwait(false); }
        catch { /* shutdown */ }
        _listener.Close();
        _cts.Dispose();
    }
}
