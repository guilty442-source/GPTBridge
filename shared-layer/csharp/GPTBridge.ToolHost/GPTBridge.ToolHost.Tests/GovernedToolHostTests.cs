using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.Tests;

/// <summary>In-memory IToolTransport driving the worker loop.</summary>
internal sealed class FakeTransport : IToolTransport
{
    private readonly Queue<JsonObject?> _claims = new();
    private readonly SemaphoreSlim _gate = new(0);
    public List<(string RequestId, JsonNode? Response)> Responded = new();
    public Dictionary<string, bool> Cancelled = new();
    public JsonObject? Hello;

    public event Action? Disconnected;

    public void SimulateDisconnect() => Disconnected?.Invoke();

    public void Enqueue(JsonObject? request)
    {
        _claims.Enqueue(request);
        _gate.Release();
    }

    public Task<JsonObject> HelloAsync(
        string toolId, string instance,
        IReadOnlyDictionary<string, string> channels,
        CancellationToken ct = default)
    {
        Hello = new JsonObject
        {
            ["tool_id"] = toolId,
            ["instance"] = instance,
            ["channels"] = new JsonObject(channels.Select(p =>
                new KeyValuePair<string, JsonNode?>(
                    p.Key, p.Value)).ToArray()),
        };
        return Task.FromResult(Hello);
    }

    public async Task<JsonObject?> ClaimAsync(
        string channel, CancellationToken ct = default)
    {
        // Block until a request is queued so tests are deterministic.
        await _gate.WaitAsync(ct);
        lock (_claims)
            return _claims.Count > 0 ? _claims.Dequeue() : null;
    }

    public Task<bool> RespondAsync(
        string channel, string requestId, JsonNode? response,
        CancellationToken ct = default)
    {
        lock (Responded)
            Responded.Add((requestId, response?.DeepClone()));
        return Task.FromResult(true);
    }

    public Task<bool> RequestCancelledAsync(
        string channel, string requestId, CancellationToken ct = default)
        => Task.FromResult(Cancelled.TryGetValue(requestId, out var c) && c);

    public Task<bool> ProgressAsync(
        string channel, string requestId, JsonNode? payload,
        CancellationToken ct = default) => Task.FromResult(true);

    public Task<JsonNode?> NotificationStampAsync(
        string channel, CancellationToken ct = default)
        => Task.FromResult<JsonNode?>(null);

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal sealed class RecordingExecutor : IGovernedCommandExecutor
{
    public List<string> Commands = new();
    public TimeSpan Delay = TimeSpan.Zero;

    public async Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
    {
        Commands.Add(command);
        if (command == "bogus_command")
            throw new PermissionDeniedException();
        if (Delay > TimeSpan.Zero)
            await Task.Delay(Delay, cancellationToken);
        return ($"{command}_result", new JsonObject { ["ok"] = true });
    }
}

public class GovernedToolHostTests
{
    private static GovernedEnvironment Env() => new()
    {
        ToolId = "vaultly",
        ProjectRoot = @"E:\GPTBridge",
        ToolRoot = @"E:\GPTBridge\Standalone tools\vaultly",
        SessionToken = new string('a', 64),
        Port = 0,
        ShutdownToken = "shtok",
        PythonExecutable = "python.exe",
    };

    private static JsonObject Request(
        string requestId, string command, string actor = "governance/main-system")
        => new()
        {
            ["request_id"] = requestId,
            ["requester_actor"] = actor,
            ["payload"] = new JsonObject { ["_governed_command"] = command },
        };

    private static async Task<GovernedToolHost> RunHostAsync(
        FakeTransport transport, IGovernedCommandExecutor executor,
        CancellationTokenSource cts)
    {
        var host = new GovernedToolHost(
            Env(), executor, "1.0.0", _ => transport);
        _ = Task.Run(async () =>
        {
            try { await host.RunWorkerAsync(cts.Token); }
            catch (OperationCanceledException) { }
        });
        return host;
    }

    private static async Task WaitForAsync(
        Func<bool> condition, int timeoutMs = 3000)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromMilliseconds(timeoutMs);
        while (!condition() && DateTime.UtcNow < deadline)
            await Task.Delay(20);
        Assert.True(condition(), "condition not met within timeout");
    }

    [Fact]
    public async Task Worker_claims_executes_responds()
    {
        var transport = new FakeTransport();
        var executor = new RecordingExecutor();
        using var cts = new CancellationTokenSource();
        var host = await RunHostAsync(transport, executor, cts);

        transport.Enqueue(Request("req-1", "system_health_check"));
        await WaitForAsync(() =>
        {
            lock (transport.Responded)
                return transport.Responded.Count == 1;
        });
        cts.Cancel();

        Assert.Single(executor.Commands);
        Assert.Equal("system_health_check", executor.Commands[0]);
        var (requestId, response) = transport.Responded[0];
        Assert.Equal("req-1", requestId);
        var result = (JsonObject)response!;
        Assert.True(result["ok"]!.GetValue<bool>());
        Assert.Equal("req-1", result["request_id"]!.GetValue<string>());
    }

    [Fact]
    public async Task Worker_denied_command_wraps_permission_denied()
    {
        var transport = new FakeTransport();
        var executor = new RecordingExecutor();
        using var cts = new CancellationTokenSource();
        var host = await RunHostAsync(transport, executor, cts);

        transport.Enqueue(Request("req-2", "bogus_command"));
        await WaitForAsync(() =>
        {
            lock (transport.Responded)
                return transport.Responded.Count == 1;
        });
        cts.Cancel();

        var result = (JsonObject)transport.Responded[0].Response!;
        Assert.False(result["ok"]!.GetValue<bool>());
        Assert.Equal("PERMISSION_DENIED",
            result["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task Worker_cancelled_request_never_responds()
    {
        var transport = new FakeTransport();
        var executor = new RecordingExecutor
        {
            Delay = TimeSpan.FromSeconds(30),
        };
        using var cts = new CancellationTokenSource();
        var host = await RunHostAsync(transport, executor, cts);

        transport.Enqueue(Request("req-3", "system_health_check"));
        await WaitForAsync(() => executor.Commands.Count == 1);
        transport.Cancelled["req-3"] = true;
        await WaitForAsync(() => executor.Commands.Count == 1
            && transport.Responded.Count == 0
            && host.HealthSnapshot() != null, 2000);
        cts.Cancel();
        // Cancelled in-flight: executor abandoned, no respond (store lease
        // expiry reclaims; the requester already marked it cancelled).
        Assert.Empty(transport.Responded);
    }

    [Fact]
    public async Task Health_snapshot_carries_governance_contract()
    {
        var transport = new FakeTransport();
        var executor = new RecordingExecutor();
        var host = new GovernedToolHost(
            Env(), executor, "1.0.0", _ => transport);
        var health = host.HealthSnapshot();
        Assert.True(health["ok"]!.GetValue<bool>());
        Assert.True(health["governance_ready"]!.GetValue<bool>());
        Assert.Equal("vaultly", health["tool_id"]!.GetValue<string>());
        Assert.Equal("independent-tool",
            health["runtime_scope"]!.GetValue<string>());
        Assert.Equal(24,
            health["workspace_instance_id"]!.GetValue<string>().Length);
        Assert.Equal("csharp-toolhost",
            health["runtime_host"]!.GetValue<string>());
        await host.DisposeAsync();
    }

    [Fact]
    public async Task CancelRequest_flags_in_flight()
    {
        var transport = new FakeTransport();
        var executor = new RecordingExecutor
        {
            Delay = TimeSpan.FromSeconds(30),
        };
        using var cts = new CancellationTokenSource();
        var host = await RunHostAsync(transport, executor, cts);

        transport.Enqueue(Request("req-4", "system_health_check"));
        await WaitForAsync(() => executor.Commands.Count == 1);
        Assert.True(host.CancelRequest("req-4"));
        Assert.False(host.CancelRequest("req-missing"));
        cts.Cancel();
    }
}
