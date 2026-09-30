// 星澄 AI 投資管理與自動操盤系統 — service contract tests.
//
// Locks the sealed route + retired-Python parity semantics:
// requester allowlist, command ownership, snapshot passthrough,
// instruction defaulting, fail-closed unconnected channel, local
// lifecycle handling, and literal ok=false failure semantics.

using System.Text.Json.Nodes;
using InvestmentMobile.Service;
using Xunit;

namespace InvestmentMobile.Service.Tests;

public sealed class ServiceContractTests
{
    private sealed class StubChannel : IXingchengChannel
    {
        public string? LastTarget;
        public string? LastCommand;
        public JsonObject? LastPayload;
        public JsonObject Response =
            new() { ["ok"] = true, ["echo"] = "pong" };

        public Task<JsonObject> RequestAsync(
            string targetToolId, string command, JsonObject payload,
            CancellationToken ct = default)
        {
            LastTarget = targetToolId;
            LastCommand = command;
            LastPayload = payload;
            return Task.FromResult(Response);
        }
    }

    [Theory]
    [InlineData("governance/main-system", true)]
    [InlineData("governance/tool/investment-mobile", true)]
    [InlineData("governance/tool/other-tool", false)]
    [InlineData("ai-assistant", false)]
    [InlineData(null, false)]
    public void AuthorizedRequester_allowlist(
        string? requester, bool expected) =>
        Assert.Equal(expected,
            XingchengRoute.AuthorizedRequester(requester));

    [Theory]
    [InlineData("investment-analysis", true)]
    [InlineData("investment-mobile-get-snapshot", true)]
    [InlineData("investment-manager", true)]
    [InlineData("investment-mobile-submit-instruction", true)]
    [InlineData("investment-mobile-rotate-pairing", true)]
    [InlineData("investment-market-search", true)]
    [InlineData("investment-mobile-status", true)]
    [InlineData("investment-mobile-start", true)]
    [InlineData("investment-mobile-stop", true)]
    [InlineData("xingcheng_self_learning_cycle", false)]
    [InlineData("drop-table", false)]
    public void Owns_command_sets(string command, bool expected) =>
        Assert.Equal(expected, XingchengRoute.Owns(command));

    [Fact]
    public void ExecuteGate_requires_both_requester_and_command()
    {
        var svc = new InvestmentMobileService(
            new XingchengChannelClient(new StubChannel()));
        Assert.Equal("ALLOW",
            svc.ExecuteGate(
                "governance/tool/investment-mobile",
                "investment-mobile-get-snapshot"));
        Assert.Equal("PERMISSION_DENIED",
            svc.ExecuteGate(
                "governance/tool/other",
                "investment-mobile-get-snapshot"));
        Assert.Equal("PERMISSION_DENIED",
            svc.ExecuteGate(
                "governance/tool/investment-mobile", "bogus"));
        // Engine commands owned only when the cluster is composed.
        Assert.Equal("PERMISSION_DENIED",
            svc.ExecuteGate(
                "governance/tool/investment-mobile",
                "investment-mobile-signal-ingest"));
    }

    [Fact]
    public void Snapshot_clones_and_passthroughs_payload()
    {
        var payload = new JsonObject
        {
            ["scope"] = "portfolio", ["deep"] = new JsonObject
            { ["n"] = 1 },
        };
        var sent = XingchengChannelClient.SnapshotPayload(payload);
        Assert.Equal("portfolio", sent["scope"]!.GetValue<string>());
        sent["deep"]!["n"] = 99;
        Assert.Equal(1, payload["deep"]!["n"]!.GetValue<int>());
        Assert.Equal(
            "{}",
            XingchengChannelClient.SnapshotPayload(null).ToJsonString());
    }

    [Theory]
    [InlineData("analyze", null, "analyze")]
    [InlineData("analyze", "", "analyze")]
    [InlineData("", null, "status")]
    [InlineData("update_shared_settings", null, null)]
    [InlineData("update_shared_settings", "", null)]
    [InlineData("op", "keep-me", "keep-me")]
    public void Instruction_defaults_missing_instruction(
        string? operation, string? instruction, string? expected)
    {
        var payload = new JsonObject();
        if (operation is not null) payload["operation"] = operation;
        if (instruction is not null) payload["instruction"] = instruction;
        var sent = XingchengChannelClient.InstructionPayload(payload);
        if (expected is null)
            Assert.False(sent.ContainsKey("instruction")
                && sent["instruction"]!.GetValue<string>().Length > 0);
        else
            Assert.Equal(expected,
                sent["instruction"]!.GetValue<string>());
    }

    [Fact]
    public async Task Unconnected_channel_fails_closed()
    {
        var client = new XingchengChannelClient(null);
        Assert.False(client.Connected);
        var result = await client.SnapshotAsync(new JsonObject());
        Assert.False(result["ok"]!.GetValue<bool>());
        Assert.False(result["queued"]!.GetValue<bool>());
        Assert.Equal("AI_CHANNEL_NOT_CONNECTED",
            result["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task
        Snapshot_routes_through_xingcheng_with_sealed_command()
    {
        var stub = new StubChannel();
        var client = new XingchengChannelClient(stub);
        var result = await client.SnapshotAsync(
            new JsonObject { ["scope"] = "holdings" });
        Assert.True(result["ok"]!.GetValue<bool>());
        Assert.Equal("xingcheng", stub.LastTarget);
        Assert.Equal("xingcheng_mobile_get_investment_snapshot",
            stub.LastCommand);
        Assert.Equal("holdings",
            stub.LastPayload!["scope"]!.GetValue<string>());
    }

    [Fact]
    public async Task SubmitInstruction_routes_and_normalizes()
    {
        var stub = new StubChannel();
        var client = new XingchengChannelClient(stub);
        await client.SubmitInstructionAsync(
            new JsonObject { ["operation"] = "ai_analysis" });
        Assert.Equal("xingcheng_mobile_submit_investment_instruction",
            stub.LastCommand);
        Assert.Equal("ai_analysis",
            stub.LastPayload!["instruction"]!.GetValue<string>());
    }

    [Fact]
    public void Handle_local_lifecycle_and_status()
    {
        var svc = new InvestmentMobileService(
            new XingchengChannelClient(new StubChannel()));
        var (evtStart, resStart) =
            svc.Handle("investment-mobile-start", new JsonObject());
        Assert.Equal("investment-mobile-start_result", evtStart);
        Assert.True(resStart["started"]!.GetValue<bool>());

        var (evtStatus, resStatus) =
            svc.Handle("investment-mobile-status", new JsonObject());
        Assert.Equal("investment-mobile-status_result", evtStatus);
        Assert.True(resStatus["ok"]!.GetValue<bool>());
        Assert.True(resStatus["channel_connected"]!.GetValue<bool>());
        Assert.Equal("investment-mobile",
            resStatus["tool_id"]!.GetValue<string>());

        svc.Handle("investment-mobile-stop", new JsonObject());
        Assert.False(svc.Started);
    }

    [Fact]
    public async Task
        Handle_snapshot_and_instruction_delegate_to_channel()
    {
        var stub = new StubChannel();
        var svc = new InvestmentMobileService(
            new XingchengChannelClient(stub));
        var (evt1, res1) = await svc.HandleAsync(
            "investment-mobile-get-snapshot", new JsonObject(),
            "governance/tool/investment-mobile");
        Assert.Equal(
            "investment-mobile-get-snapshot_result", evt1);
        Assert.True(res1["ok"]!.GetValue<bool>());
        var (evt2, _) = await svc.HandleAsync(
            "investment-manager", new JsonObject(),
            "governance/tool/investment-mobile");
        Assert.Equal("investment-manager_result", evt2);
        Assert.Equal("xingcheng_mobile_submit_investment_instruction",
            stub.LastCommand);
    }

    [Fact]
    public void Handle_unknown_command_denied()
    {
        var svc = new InvestmentMobileService(
            new XingchengChannelClient(new StubChannel()));
        var (evt, res) = svc.Handle("bogus", new JsonObject());
        Assert.Equal("PERMISSION_DENIED", evt);
        Assert.False(res["ok"]!.GetValue<bool>());
    }

    [Fact]
    public void Ok_semantics_literal_false()
    {
        Assert.True(XingchengRoute.OkIsFalse(
            new JsonObject { ["ok"] = false }));
        Assert.False(XingchengRoute.OkIsFalse(
            new JsonObject { ["ok"] = true }));
        Assert.False(XingchengRoute.OkIsFalse(new JsonObject()));
        Assert.True(XingchengRoute.OkIsNotFalse(
            new JsonObject { ["ok"] = true }));
        Assert.True(XingchengRoute.OkIsNotFalse(new JsonObject()));
        Assert.False(XingchengRoute.OkIsNotFalse(
            new JsonObject { ["ok"] = false }));
    }
}
