using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.Tests;

/// <summary>HTTP/WS boundary tests against a real HttpListener.</summary>
public class ToolHostServerTests
{
    private static int FreePort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static async Task<(GovernedToolHost host, ToolHostServer server, int port)>
        StartServerAsync()
    {
        var port = FreePort();
        var env = new GovernedEnvironment
        {
            ToolId = "system-rescue",
            ProjectRoot = @"E:\GPTBridge",
            ToolRoot = @"E:\GPTBridge\Standalone tools\system-rescue",
            SessionToken = new string('b', 64),
            Port = port,
            ShutdownToken = "shutdown-secret",
            PythonExecutable = "python.exe",
        };
        var host = new GovernedToolHost(
            env, new RecordingExecutor(), "1.0.0", _ => new FakeTransport());
        var server = new ToolHostServer(host);
        server.Start();
        await Task.Delay(50);
        return (host, server, port);
    }

    private sealed class RecordingExecutor : IGovernedCommandExecutor
    {
        public Task<(string Event, JsonObject Result)> ExecuteAsync(
            string command, JsonObject payload, string requestId,
            CancellationToken cancellationToken)
            => Task.FromResult(($"{command}_result",
                new JsonObject { ["ok"] = true }));
    }

    [Fact]
    public async Task Health_endpoint_returns_governance_ready()
    {
        var (host, server, port) = await StartServerAsync();
        await using var _ = server;
        await using var __ = host;

        using var client = new HttpClient();
        var body = await client.GetStringAsync(
            $"http://127.0.0.1:{port}/health");
        var json = JsonNode.Parse(body)!.AsObject();
        Assert.True(json["ok"]!.GetValue<bool>());
        Assert.True(json["governance_ready"]!.GetValue<bool>());
        Assert.Equal("system-rescue", json["tool_id"]!.GetValue<string>());
        Assert.Equal(
            host.WorkspaceInstanceId,
            json["workspace_instance_id"]!.GetValue<string>());
    }

    [Fact]
    public async Task Metrics_endpoint_lists_channels()
    {
        var (host, server, port) = await StartServerAsync();
        await using var _ = server;
        await using var __ = host;

        using var client = new HttpClient();
        var body = await client.GetStringAsync(
            $"http://127.0.0.1:{port}/metrics");
        var json = JsonNode.Parse(body)!.AsObject();
        Assert.NotNull(json["channel_health"]);
        Assert.NotNull(json["uptime_seconds"]);
    }

    [Fact]
    public async Task Shutdown_requires_token()
    {
        var (host, server, port) = await StartServerAsync();
        await using var _ = server;
        await using var __ = host;

        using var client = new HttpClient();
        var denied = await client.GetAsync(
            $"http://127.0.0.1:{port}/shutdown");
        Assert.Equal(HttpStatusCode.Forbidden, denied.StatusCode);
        Assert.False(host.ShutdownToken.IsCancellationRequested);

        var request = new HttpRequestMessage(
            HttpMethod.Get, $"http://127.0.0.1:{port}/shutdown");
        request.Headers.Add(
            "X-GPTBridge-Shutdown-Token", "shutdown-secret");
        var granted = await client.SendAsync(request);
        Assert.Equal(HttpStatusCode.OK, granted.StatusCode);
        Assert.True(host.ShutdownToken.IsCancellationRequested);
    }

    [Fact]
    public async Task Websocket_requires_token_and_instance()
    {
        var (host, server, port) = await StartServerAsync();
        await using var _ = server;
        await using var __ = host;

        using var client = new HttpClient();
        var bad = await client.GetAsync(
            $"http://127.0.0.1:{port}/?token=wrong&instance=x");
        Assert.Equal(HttpStatusCode.Forbidden, bad.StatusCode);
    }

    [Fact]
    public async Task Websocket_cancel_command_replies()
    {
        var (host, server, port) = await StartServerAsync();
        await using var _ = server;
        await using var __ = host;

        using var socket = new ClientWebSocket();
        var uri = new Uri(
            $"ws://127.0.0.1:{port}/?token={new string('b', 64)}"
            + $"&instance={host.WorkspaceInstanceId}");
        await socket.ConnectAsync(uri, CancellationToken.None);

        var message = Encoding.UTF8.GetBytes(
            new JsonObject
            {
                ["command"] = "toolbox_cancel_tool_run",
                ["payload"] = new JsonObject { ["request_id"] = "req-x" },
            }.ToJsonString());
        await socket.SendAsync(
            message, WebSocketMessageType.Text, true,
            CancellationToken.None);

        var buffer = new byte[4096];
        var result = await socket.ReceiveAsync(
            buffer, CancellationToken.None);
        var reply = JsonNode.Parse(
            Encoding.UTF8.GetString(buffer, 0, result.Count))!.AsObject();
        Assert.Equal("toolbox_cancel_tool_run_result",
            reply["event"]!.GetValue<string>());
        var payload = reply["payload"]!.AsObject();
        // req-x is not in flight → cancelled=false
        Assert.False(payload["cancelled"]!.GetValue<bool>());
        Assert.Equal("system-rescue",
            payload["tool_id"]!.GetValue<string>());
    }

    [Fact]
    public async Task Websocket_unknown_command_denied()
    {
        var (host, server, port) = await StartServerAsync();
        await using var _ = server;
        await using var __ = host;

        using var socket = new ClientWebSocket();
        var uri = new Uri(
            $"ws://127.0.0.1:{port}/?token={new string('b', 64)}"
            + $"&instance={host.WorkspaceInstanceId}");
        await socket.ConnectAsync(uri, CancellationToken.None);

        var message = Encoding.UTF8.GetBytes(
            new JsonObject
            {
                ["command"] = "toolbox_request_tool_execution",
                ["payload"] = new JsonObject { ["request_id"] = "r1" },
            }.ToJsonString());
        await socket.SendAsync(
            message, WebSocketMessageType.Text, true,
            CancellationToken.None);

        var buffer = new byte[4096];
        var result = await socket.ReceiveAsync(
            buffer, CancellationToken.None);
        var reply = JsonNode.Parse(
            Encoding.UTF8.GetString(buffer, 0, result.Count))!.AsObject();
        Assert.Equal("toolbox_request_tool_execution_result",
            reply["event"]!.GetValue<string>());
        Assert.Equal("PERMISSION_DENIED",
            reply["payload"]!["error_code"]!.GetValue<string>());
    }
}
