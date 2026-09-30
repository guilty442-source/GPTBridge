// 星澄 AI 投資管理與自動操盤系統 — governed command service (C#).
//
// Production implementation of the retired
// ``channel_runtime.InvestmentMobileService`` routing contract:
// execute() gates on requester authorization + owned commands;
// handle() routes local lifecycle/status commands directly and passes
// snapshot/instruction commands through the xingcheng channel.

using System.Text.Json.Nodes;

namespace InvestmentMobile.Service;

public sealed class InvestmentMobileService
{
    private readonly XingchengChannelClient _channel;
    private readonly TradingEngineCluster? _engines;

    public bool Started { get; private set; }

    public InvestmentMobileService(
        XingchengChannelClient channel,
        TradingEngineCluster? engines = null)
    {
        _channel = channel;
        _engines = engines;
    }

    /// <summary>Owned commands = sealed channel sets + the ported
    /// engine-cluster commands (system-channel, governance actors).</summary>
    public bool Owns(string command) =>
        XingchengRoute.Owns(command)
        || (_engines is not null
            && TradingEngineCluster.Owns(command));

    /// <summary>execute() gate — requester allowlist + owned commands,
    /// fail-closed PERMISSION_DENIED.</summary>
    public string ExecuteGate(string? requester, string command) =>
        XingchengRoute.AuthorizedRequester(requester)
        && Owns(command)
            ? "ALLOW"
            : "PERMISSION_DENIED";

    /// <summary>Status result shape — parity with the retired
    /// ``_status()`` dynamic fields.</summary>
    public JsonObject StatusResult(string command)
    {
        var status = new JsonObject
        {
            ["ok"] = true,
            ["tool_id"] = XingchengRoute.ToolId,
            ["command"] = command,
            ["started"] = Started,
            ["channel_connected"] = _channel.Connected,
            ["transport"] = "governance-authenticated-shared-layer",
            ["lifecycle_owner"] = "governed-runtime",
            ["business_layer_owner"] = "ai-assistant",
            ["permission_profile"] = "ai-investment-manager-v1",
            ["relay_chain"] =
                "investment-mobile -> xingcheng -> ai-assistant",
        };
        _engines?.ContributeStatus(status);
        return status;
    }

    /// <summary>Command handler — returns (event, result).</summary>
    public (string Event, JsonObject Result) Handle(
        string command, JsonObject payload)
    {
        if (XingchengRoute.LocalCommands.Contains(command))
        {
            if (command == "investment-mobile-start")
                Started = true;
            else if (command == "investment-mobile-stop")
                Started = false;
            return ($"{command}_result", StatusResult(command));
        }
        return ("PERMISSION_DENIED",
            new JsonObject { ["ok"] = false });
    }

    /// <summary>Full dispatch — channel-routed and engine commands are
    /// bounded async operations; local lifecycle stays synchronous.</summary>
    public async Task<(string Event, JsonObject Result)> HandleAsync(
        string command, JsonObject payload, string requester,
        CancellationToken ct = default)
    {
        if (XingchengRoute.SnapshotCommands.Contains(command))
            return ($"{command}_result",
                await _channel.SnapshotAsync(payload, ct)
                    .ConfigureAwait(false));
        if (XingchengRoute.InstructionCommands.Contains(command))
            return ($"{command}_result",
                await _channel.SubmitInstructionAsync(payload, ct)
                    .ConfigureAwait(false));
        if (_engines is not null
            && TradingEngineCluster.Owns(command))
            return ($"{command}_result",
                await _engines.HandleAsync(
                    command, payload, requester, ct)
                    .ConfigureAwait(false));
        return Handle(command, payload);
    }
}
