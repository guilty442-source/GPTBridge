// 星澄 AI 投資管理與自動操盤系統 — governed submit-lane tests.
//
// Locks the production channel binding: request→poll→response over
// IToolTransport submit ops; bounded timeout with cancel; denied and
// unbound transports fail closed.

using System.Text.Json.Nodes;
using GPTBridge.ToolHost;
using InvestmentMobile.Service;
using Xunit;

namespace InvestmentMobile.Service.Tests;

public sealed class SubmitChannelTests
{
    private sealed class FakeTransport : IToolTransport
    {
        public int SubmitCalls;
        public int ResponseCalls;
        public int CancelCalls;
        public string? LastTarget;
        public string? LastCommand;
        public JsonObject? LastPayload;
        public JsonObject? SubmitResult =
            new() { ["request_id"] = "req-1", ["queued"] = true };
        public Queue<JsonObject> Responses = new();
        public ProxyErrorException? FailWith;

        public event Action? Disconnected
        { add { } remove { } }

        public Task<JsonObject> HelloAsync(
            string toolId, string workspaceInstanceId,
            IReadOnlyDictionary<string, string> channels,
            IReadOnlyDictionary<string, SubmitBinding>? submitBindings =
                null,
            CancellationToken ct = default) =>
            Task.FromResult(new JsonObject { ["ok"] = true });

        public Task<JsonObject?> ClaimAsync(
            string channel, CancellationToken ct = default) =>
            Task.FromResult<JsonObject?>(null);

        public Task<bool> RespondAsync(
            string channel, string requestId, JsonNode? response,
            CancellationToken ct = default) => Task.FromResult(true);

        public Task<bool> RequestCancelledAsync(
            string channel, string requestId,
            CancellationToken ct = default) => Task.FromResult(false);

        public Task<bool> ProgressAsync(
            string channel, string requestId, JsonNode? payload,
            CancellationToken ct = default) => Task.FromResult(true);

        public Task<JsonNode?> NotificationStampAsync(
            string channel, CancellationToken ct = default) =>
            Task.FromResult<JsonNode?>(null);

        public Task<JsonObject?> SubmitRequestAsync(
            string channel, string targetToolId, string command,
            JsonObject payload, string? requestId = null,
            CancellationToken ct = default)
        {
            SubmitCalls++;
            LastTarget = targetToolId;
            LastCommand = command;
            LastPayload = payload;
            if (FailWith is not null) throw FailWith;
            return Task.FromResult(SubmitResult);
        }

        public Task<JsonObject?> SubmitResponseAsync(
            string channel, string requestId, string targetToolId,
            CancellationToken ct = default)
        {
            ResponseCalls++;
            if (FailWith is not null) throw FailWith;
            return Task.FromResult<JsonObject?>(
                Responses.Count > 0
                    ? Responses.Dequeue()
                    : new JsonObject
                    {
                        ["status"] = "pending",
                        ["request_id"] = requestId,
                    });
        }

        public Task<bool> SubmitCancelAsync(
            string channel, string requestId, string targetToolId,
            CancellationToken ct = default)
        {
            CancelCalls++;
            return Task.FromResult(true);
        }

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private static ProxySubmitChannel Channel(
        FakeTransport transport, int timeoutMs = 2000) =>
        new(() => transport,
            timeout: TimeSpan.FromMilliseconds(timeoutMs),
            pollInterval: TimeSpan.FromMilliseconds(10));

    [Fact]
    public async Task Request_completed_returns_response()
    {
        var t = new FakeTransport();
        t.Responses.Enqueue(new JsonObject
        {
            ["status"] = "completed", ["request_id"] = "req-1",
            ["response"] = new JsonObject
            { ["ok"] = true, ["quote"] = 123 },
        });
        var ch = Channel(t);
        var res = await ch.RequestAsync(
            "xingcheng", "xingcheng_mobile_get_investment_snapshot",
            new JsonObject());
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.Equal(123, res["quote"]!.GetValue<int>());
        Assert.Equal("xingcheng", t.LastTarget);
        Assert.Equal(1, t.SubmitCalls);
    }

    [Fact]
    public async Task Request_polls_until_completed()
    {
        var t = new FakeTransport();
        t.Responses.Enqueue(new JsonObject
        { ["status"] = "pending", ["request_id"] = "req-1" });
        t.Responses.Enqueue(new JsonObject
        {
            ["status"] = "completed", ["request_id"] = "req-1",
            ["response"] = new JsonObject { ["ok"] = true },
        });
        var res = await Channel(t).RequestAsync(
            "xingcheng", "cmd", new JsonObject());
        Assert.True(res["ok"]!.GetValue<bool>());
        Assert.True(t.ResponseCalls >= 2);
    }

    [Fact]
    public async Task Request_timeout_cancels_and_fails_closed()
    {
        var t = new FakeTransport();   // stays pending
        var res = await Channel(t, timeoutMs: 120).RequestAsync(
            "xingcheng", "cmd", new JsonObject());
        Assert.False(res["ok"]!.GetValue<bool>());
        Assert.Equal("REQUEST_TIMEOUT",
            res["error_code"]!.GetValue<string>());
        Assert.Equal(1, t.CancelCalls);
    }

    [Fact]
    public async Task No_transport_returns_not_connected()
    {
        var ch = new ProxySubmitChannel(() => null);
        var res = await ch.RequestAsync(
            "xingcheng", "cmd", new JsonObject());
        Assert.Equal("AI_CHANNEL_NOT_CONNECTED",
            res["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task Denied_submit_surfaces_permission_denied()
    {
        var t = new FakeTransport
        {
            FailWith = new ProxyErrorException(
                "PERMISSION_DENIED", "route denied"),
        };
        var res = await Channel(t).RequestAsync(
            "xingcheng", "cmd", new JsonObject());
        Assert.False(res["ok"]!.GetValue<bool>());
        Assert.Equal("PERMISSION_DENIED",
            res["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task Not_queued_result_passes_through()
    {
        var t = new FakeTransport
        {
            SubmitResult = new JsonObject
            {
                ["queued"] = false,
                ["error_code"] = "CHANNEL_NOT_BOUND",
            },
        };
        var res = await Channel(t).RequestAsync(
            "xingcheng", "cmd", new JsonObject());
        Assert.Equal("CHANNEL_NOT_BOUND",
            res["error_code"]!.GetValue<string>());
        Assert.Equal(0, t.ResponseCalls);
    }

    [Fact]
    public async Task Null_submit_lane_fails_closed()
    {
        var t = new FakeTransport { SubmitResult = null };
        var res = await Channel(t).RequestAsync(
            "xingcheng", "cmd", new JsonObject());
        Assert.Equal("AI_CHANNEL_NOT_CONNECTED",
            res["error_code"]!.GetValue<string>());
    }
}
