using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using StarBusinessLogic.Application;
using StarBusinessLogic.Infrastructure;

namespace StarBusinessLogic.Tests;

// P13 契約測試：C# GovernedIpcClient ↔ ipc/server_tokens.py + server_handler.py
public class GovernedIpcClientTests
{
    private sealed class FakeTransport : IIpcTransport
    {
        public readonly List<string> Sent = new();
        private readonly Queue<string> _incoming;
        public Uri? ConnectedTo;

        public FakeTransport(params string[] incoming) => _incoming = new Queue<string>(incoming);

        public Task ConnectAsync(Uri uri, CancellationToken cancellationToken)
        {
            ConnectedTo = uri;
            return Task.CompletedTask;
        }

        public Task SendTextAsync(string text, CancellationToken cancellationToken)
        {
            Sent.Add(text);
            return Task.CompletedTask;
        }

        public Task<string?> ReceiveTextAsync(CancellationToken cancellationToken)
            => Task.FromResult(_incoming.Count > 0 ? _incoming.Dequeue() : null);

        public Task CloseAsync(CancellationToken cancellationToken) => Task.CompletedTask;
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private static string Event(string name, object payload)
        => JsonSerializer.Serialize(new { @event = name, payload });

    [Fact]
    public void WorkspaceInstanceId_MatchesPythonNormcase()
    {
        // ipc/server_tokens.py: normcase(str(Path(root).absolute())).replace("\\","/")
        // For "E:\GPTBridge": normcase → "e:\gptbridge" → "e:/gptbridge".
        var expected = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes("e:/gptbridge"))).ToLowerInvariant()[..24];
        Assert.Equal(expected, GovernedIpcDiscovery.WorkspaceInstanceId(@"E:\GPTBridge"));
    }

    [Fact]
    public void Port_DefaultsAndRejectsOutOfRange()
    {
        var saved = Environment.GetEnvironmentVariable("GPTBRIDGE_IPC_PORT");
        try
        {
            Environment.SetEnvironmentVariable("GPTBRIDGE_IPC_PORT", null);
            Assert.Equal(8765, GovernedIpcDiscovery.Port());
            Environment.SetEnvironmentVariable("GPTBRIDGE_IPC_PORT", "80");
            Assert.Throws<InvalidOperationException>(() => GovernedIpcDiscovery.Port());
            Environment.SetEnvironmentVariable("GPTBRIDGE_IPC_PORT", "19001");
            Assert.Equal(19001, GovernedIpcDiscovery.Port());
        }
        finally { Environment.SetEnvironmentVariable("GPTBRIDGE_IPC_PORT", saved); }
    }

    [Fact]
    public async Task ExecuteAsync_SendsEnvelopeAndReturnsResultPayload()
    {
        var transport = new FakeTransport(
            Event("COMMAND_RECEIVED", new { command = "toolbox_list_tools", status = "processing" }),
            Event("toolbox_list_tools_result", new { ok = true, tools = new object[0] }));
        var client = new GovernedIpcClient(new Uri("ws://127.0.0.1:8765/?token=x&instance=y"), () => transport);

        var result = await client.ExecuteAsync("toolbox_list_tools", new Dictionary<string, object?>());

        Assert.Single(transport.Sent);
        using var sent = JsonDocument.Parse(transport.Sent[0]);
        Assert.Equal("toolbox_list_tools", sent.RootElement.GetProperty("command").GetString());
        Assert.True(result.GetProperty("ok").GetBoolean());
    }

    [Fact]
    public async Task ExecuteAsync_AnswersHeartbeatPing()
    {
        var transport = new FakeTransport(
            Event("heartbeat_ping", new { t = "2026-09-23T00:00:00Z" }),
            Event("ping_result", new { ok = true }));
        var client = new GovernedIpcClient(new Uri("ws://127.0.0.1:8765/?token=x&instance=y"), () => transport);

        var result = await client.ExecuteAsync("ping", new Dictionary<string, object?>());

        Assert.True(result.GetProperty("ok").GetBoolean());
        Assert.Equal(2, transport.Sent.Count);
        using var pong = JsonDocument.Parse(transport.Sent[1]);
        Assert.Equal("heartbeat_pong", pong.RootElement.GetProperty("command").GetString());
    }

    [Fact]
    public async Task ExecuteAsync_ClosedSocket_FailsClosed()
    {
        var transport = new FakeTransport(); // no events → immediate close
        var client = new GovernedIpcClient(new Uri("ws://127.0.0.1:8765/?token=x&instance=y"), () => transport);
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(
            () => client.ExecuteAsync("toolbox_list_tools", new Dictionary<string, object?>()));
        Assert.StartsWith("IPC_CONNECTION_CLOSED", ex.Message);
    }

    [Fact]
    public async Task WebSocketTransport_RejectsNonLoopback()
    {
        await using var transport = new WebSocketIpcTransport();
        await Assert.ThrowsAsync<InvalidOperationException>(
            () => transport.ConnectAsync(new Uri("ws://192.168.1.5:8765/"), CancellationToken.None));
        await Assert.ThrowsAsync<InvalidOperationException>(
            () => transport.ConnectAsync(new Uri("wss://127.0.0.1:8765/"), CancellationToken.None));
    }
}

public class LifecycleOrchestratorTests
{
    private sealed class FakeTransport : IIpcTransport
    {
        public readonly List<string> Sent = new();
        private readonly Queue<string> _incoming;
        public FakeTransport(params string[] incoming) => _incoming = new Queue<string>(incoming);
        public Task ConnectAsync(Uri uri, CancellationToken ct) => Task.CompletedTask;
        public Task SendTextAsync(string text, CancellationToken ct) { Sent.Add(text); return Task.CompletedTask; }
        public Task<string?> ReceiveTextAsync(CancellationToken ct) => Task.FromResult(_incoming.Count > 0 ? _incoming.Dequeue() : null);
        public Task CloseAsync(CancellationToken ct) => Task.CompletedTask;
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private static string Event(string name, object payload)
        => JsonSerializer.Serialize(new { @event = name, payload });

    private static string WriteDescriptor(string toolRoot)
    {
        // Mirrors model_service_http.py descriptor layout.
        var ipcDir = Path.Combine(toolRoot, "xingcheng", "runtime", "ipc");
        Directory.CreateDirectory(ipcDir);
        var tokenFile = Path.Combine(ipcDir, "model-service-session-token");
        File.WriteAllText(tokenFile, "tok123");
        File.WriteAllText(Path.Combine(ipcDir, "model-service.json"), JsonSerializer.Serialize(new
        {
            schema = "star-model-service-descriptor/v1",
            port = 19999,
            pid = 4242,
            token_file = "model-service-session-token",
            lifecycle_owner = "local-model/channel_runtime.py",
            consumer_policy = "csharp-orchestrator-client-only",
        }));
        return ipcDir;
    }

    [Fact]
    public async Task EnsureModelService_AlreadyRunning_ShortCircuits()
    {
        var toolRoot = Path.Combine(Path.GetTempPath(), $"p13-{Guid.NewGuid():N}");
        WriteDescriptor(toolRoot);
        var ipcNeverCalled = new GovernedIpcClient(new Uri("ws://127.0.0.1:1/"), () =>
            throw new InvalidOperationException("must not connect"));
        var orch = new LifecycleOrchestrator(@"E:\GPTBridge", toolRoot, ipcNeverCalled,
            (_, _) => Task.CompletedTask, _ => Task.FromResult(true));

        var step = await orch.EnsureModelServiceAsync();
        Assert.True(step.Ok);
        Assert.Equal("already-running", step.Detail);
    }

    [Fact]
    public async Task EnsureModelService_StartsThroughGovernedPath_AndPollsLiveness()
    {
        var toolRoot = Path.Combine(Path.GetTempPath(), $"p13-{Guid.NewGuid():N}");
        Directory.CreateDirectory(toolRoot); // no descriptor → service down
        var transport = new FakeTransport(
            Event("toolbox_start_tool_result", new { ok = true, pid = 777 }));
        var ipc = new GovernedIpcClient(new Uri("ws://127.0.0.1:1/"), () => transport);
        var probes = 0;
        var orch = new LifecycleOrchestrator(@"E:\GPTBridge", toolRoot, ipc,
            (_, _) => Task.CompletedTask,
            _ => Task.FromResult(++probes > 1)); // down on entry, up after start

        var step = await orch.EnsureModelServiceAsync();
        Assert.True(step.Ok);
        Assert.Contains("governed toolbox path", step.Detail);
        using var sent = JsonDocument.Parse(transport.Sent[0]);
        Assert.Equal("toolbox_start_tool", sent.RootElement.GetProperty("command").GetString());
        Assert.Equal("local-model", sent.RootElement.GetProperty("payload").GetProperty("tool_id").GetString());
        Assert.True(sent.RootElement.GetProperty("payload").GetProperty("background").GetBoolean());
    }

    [Fact]
    public async Task EnsureModelService_GovernedDenial_IsFailClosed()
    {
        var toolRoot = Path.Combine(Path.GetTempPath(), $"p13-{Guid.NewGuid():N}");
        Directory.CreateDirectory(toolRoot);
        var transport = new FakeTransport(
            Event("toolbox_start_tool_result", new { ok = false, error_code = "TOOL_START_LOCKED" }));
        var ipc = new GovernedIpcClient(new Uri("ws://127.0.0.1:1/"), () => transport);
        var orch = new LifecycleOrchestrator(@"E:\GPTBridge", toolRoot, ipc,
            (_, _) => Task.CompletedTask, _ => Task.FromResult(false));

        var step = await orch.EnsureModelServiceAsync();
        Assert.False(step.Ok);
        Assert.Equal("TOOL_START_LOCKED", step.ErrorCode);
    }
}
