// 星澄 AI 投資管理與自動操盤系統 — governed AI-channel client (C#).
//
// Production implementation of the sealed route contract
// (governance_rule/permission_directory/registries/permissions/
// tool_routes.json):
//   MOBILE_ACTOR = governance/tool/investment-mobile
//   MOBILE_ROUTE_COMMAND = {xingcheng_mobile_get_investment_snapshot,
//                           xingcheng_mobile_submit_investment_instruction}
//   ai-connections.entry_gateway = xingcheng, direct_model_access=false.
//
// Semantics mirror native/test_suites/csharp_investment/Semantics.cs
// (the retired Python channel client parity target): payload passthrough,
// submit-instruction normalization, fail-closed unconnected result.

using System.Text.Json.Nodes;

namespace InvestmentMobile.Service;

/// <summary>Governed channel seam — the production binding is the
/// shared-layer governed request client; tests inject stubs.</summary>
public interface IXingchengChannel
{
    /// <summary>Synchronous governed request to a routed tool.</summary>
    JsonObject Request(string targetToolId, string command, JsonObject payload);
}

public static class XingchengRoute
{
    public const string ToolId = "investment-mobile";
    public const string XingchengToolId = "xingcheng";
    public const string GovernanceMainActor = "governance/main-system";
    public const string SelfActor = "governance/tool/investment-mobile";

    public const string SnapshotCommand =
        "xingcheng_mobile_get_investment_snapshot";
    public const string InstructionCommand =
        "xingcheng_mobile_submit_investment_instruction";

    /// <summary>Commands that pass through as snapshot requests.</summary>
    public static readonly IReadOnlySet<string> SnapshotCommands =
        new HashSet<string>
        {
            "investment-analysis",
            "investment-mobile-get-snapshot",
        };

    /// <summary>Commands that pass through as instruction submits.</summary>
    public static readonly IReadOnlySet<string> InstructionCommands =
        new HashSet<string>
        {
            "investment-manager",
            "investment-market-search",
            "investment-mobile-submit-instruction",
            "investment-mobile-rotate-pairing",
        };

    /// <summary>Commands handled locally (lifecycle/status).</summary>
    public static readonly IReadOnlySet<string> LocalCommands =
        new HashSet<string>
        {
            "investment-mobile-status",
            "investment-mobile-start",
            "investment-mobile-stop",
        };

    public static bool Owns(string command) =>
        SnapshotCommands.Contains(command)
        || InstructionCommands.Contains(command)
        || LocalCommands.Contains(command);

    public static bool AuthorizedRequester(string? requester) =>
        requester is GovernanceMainActor or SelfActor;

    /// <summary>Python ``result.get("ok") is not False`` — only the
    /// literal false is negative.</summary>
    public static bool OkIsNotFalse(JsonObject result) =>
        !(result["ok"] is JsonValue v
          && v.TryGetValue<bool>(out var b) && !b);

    public static bool OkIsFalse(JsonObject result) =>
        result["ok"] is JsonValue v
        && v.TryGetValue<bool>(out var b) && !b;
}

/// <summary>
/// Channel request normalization — the retired
/// ``integration/clients.py::ChannelClient`` semantics.
/// </summary>
public sealed class XingchengChannelClient
{
    private readonly IXingchengChannel? _channel;

    public XingchengChannelClient(IXingchengChannel? channel)
    {
        _channel = channel;
    }

    public bool Connected => _channel is not null;

    /// <summary>Snapshot payload passthrough (null → empty object).</summary>
    public static JsonObject SnapshotPayload(JsonObject? payload) =>
        payload is null ? new JsonObject()
        : (JsonObject)payload.DeepClone();

    /// <summary>submit_instruction: fills a missing instruction unless
    /// operation == "update_shared_settings".</summary>
    public static JsonObject InstructionPayload(JsonObject payload)
    {
        var request = (JsonObject)payload.DeepClone();
        var operation =
            (request["operation"]?.GetValue<string>() ?? "").Trim();
        var instruction =
            (request["instruction"]?.GetValue<string>() ?? "").Trim();
        if (operation != "update_shared_settings"
            && instruction.Length == 0)
            request["instruction"] =
                operation.Length > 0 ? operation : "status";
        return request;
    }

    /// <summary>Channel-unconnected fail-closed result.</summary>
    public static JsonObject NotConnected() => new()
    {
        ["ok"] = false,
        ["queued"] = false,
        ["error_code"] = "AI_CHANNEL_NOT_CONNECTED",
        ["message"] = "投資手機版 AI 通道尚未連線。",
    };

    /// <summary>Governed request — fail-closed when no channel is
    /// bound.</summary>
    public JsonObject Request(string command, JsonObject payload)
    {
        if (_channel is null)
            return NotConnected();
        return _channel.Request(
            XingchengRoute.XingchengToolId, command,
            (JsonObject)payload.DeepClone());
    }

    public JsonObject Snapshot(JsonObject? payload) =>
        Request(XingchengRoute.SnapshotCommand,
            SnapshotPayload(payload));

    public JsonObject SubmitInstruction(JsonObject payload) =>
        Request(XingchengRoute.InstructionCommand,
            InstructionPayload(payload));
}
