// HTTP/WS boundary for the governed tool runtime — mirrors
// GovernedRuntimeMaintenanceMixin.run(): /health, /metrics, /shutdown
// plus the token/instance-authenticated WebSocket command path.
using System.Net;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.ToolHost;

public sealed class ToolHostServer : IAsyncDisposable
{
    private readonly GovernedToolHost _host;
    private readonly HttpListener _listener = new();
    private readonly CancellationTokenSource _cts = new();
    private Task? _acceptLoop;

    public ToolHostServer(GovernedToolHost host)
    {
        _host = host;
        _listener.Prefixes.Add($"http://127.0.0.1:{host.Port}/");
    }

    public void Start()
    {
        _listener.Start();
        _acceptLoop = Task.Run(AcceptLoopAsync);
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
            _ = Task.Run(() => HandleContextAsync(context));
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

    private async Task DrainWebSocketAsync(WebSocket socket)
    {
        var buffer = new byte[64 * 1024];
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
                await HandleWsMessageAsync(socket, message.ToArray())
                    .ConfigureAwait(false);
            }
        }
        catch (WebSocketException) { /* client disconnected mid-stream */ }
        catch (OperationCanceledException) { /* shutdown */ }
    }

    private async Task HandleWsMessageAsync(WebSocket socket, byte[] raw)
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
                await SendWsJsonAsync(socket, new JsonObject
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
            // v1: inbound WS commands other than cancel are denied —
            // governed commands reach headless tools through the store
            // claim path; submit-side re-queueing (source-UI tools) is a
            // later phase (design §10).
            throw new PermissionDeniedException();
        }
        catch (JsonException)
        {
            await SendWsJsonAsync(socket, new JsonObject
            {
                ["event"] = "error",
                ["payload"] = new JsonObject { ["ok"] = false },
            }).ConfigureAwait(false);
        }
        catch (Exception)
        {
            await SendWsJsonAsync(socket, new JsonObject
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

    private static async Task SendWsJsonAsync(
        WebSocket socket, JsonObject message)
    {
        var bytes = Encoding.UTF8.GetBytes(message.ToJsonString());
        await socket.SendAsync(
            bytes, WebSocketMessageType.Text, endOfMessage: true,
            CancellationToken.None).ConfigureAwait(false);
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
        try { _listener.Stop(); }
        catch { /* not started */ }
        if (_acceptLoop is not null)
        {
            try { await _acceptLoop.ConfigureAwait(false); }
            catch { /* shutdown */ }
        }
        _listener.Close();
        _cts.Dispose();
    }
}
