// M1 investment-mobile C# shadow prototype — decision-free execution
// semantics mirrored from the Python tool (module-language-migration-order).
//
// Sources mirrored:
//   src/channel_runtime.py                        (InvestmentMobileService)
//   integration/clients.py                        (ChannelClient/ExternalAPIClient)
//   infrastructure/clients.py                     (MarketDataClient/DatabaseClient)
//   application/use_cases.py                      (Analyze/Manage use cases)
//   presentation/presenters.py                    (presenter wraps)
//
// Boundary (§10.65 invariants + ABI v1 mode B): no network, no database,
// no token issuing, no subprocess — all channel effects go through the
// injected IChannelStub so the semantic layer stays pure/deterministic.
// Python remains authoritative; this is a shadow parity target only.
using System.Text.Json.Nodes;

namespace InvestmentMobileShadow;

/// <summary>Stub of the governed AI channel (Python ChannelClient._client).</summary>
public interface IChannelStub
{
    /// <summary>Returns the response a governed request would produce.</summary>
    JsonObject Request(string targetToolId, string command, JsonObject payload);
}

/// <summary>Deterministic echo channel used by the parity harness.</summary>
public sealed class EchoChannelStub : IChannelStub
{
    public JsonObject Request(string targetToolId, string command, JsonObject payload)
    {
        return new JsonObject
        {
            ["ok"] = true,
            ["echo"] = payload.DeepClone(),
            ["command"] = command,
        };
    }
}

/// <summary>Stub channel that reports a transport failure (ok:false).</summary>
public sealed class FailingChannelStub : IChannelStub
{
    public JsonObject Request(string targetToolId, string command, JsonObject payload)
    {
        return new JsonObject { ["ok"] = false, ["error_code"] = "CHANNEL_REQUEST_FAILED" };
    }
}

public static class InvestmentMobileSemantics
{
    /// <summary>Python ``result.get("ok") is not False`` — only the
    /// literal false is negative; missing/other values count as true.</summary>
    public static bool OkIsNotFalse(JsonObject result) =>
        !(result["ok"] is JsonValue v && v.TryGetValue<bool>(out var b) && !b);

    /// <summary>Python ``result.get("ok") is False``.</summary>
    public static bool OkIsFalse(JsonObject result) =>
        result["ok"] is JsonValue v && v.TryGetValue<bool>(out var b) && !b;
    public const string ToolId = "investment-mobile";
    public const string GovernanceMainActor = "governance/main-system";
    public const string SelfActor = "governance/tool/investment-mobile";
    public const string SnapshotCommand = "xingcheng_mobile_get_investment_snapshot";
    public const string InstructionCommand = "xingcheng_mobile_submit_investment_instruction";
    public const string XingchengToolId = "xingcheng";

    public static readonly IReadOnlySet<string> SnapshotCommands = new HashSet<string>
    {
        "investment-analysis",
        "investment-mobile-get-snapshot",
    };

    public static readonly IReadOnlySet<string> InstructionCommands = new HashSet<string>
    {
        "investment-manager",
        "investment-market-search",
        "investment-mobile-submit-instruction",
        "investment-mobile-rotate-pairing",
    };

    public static readonly IReadOnlySet<string> LocalCommands = new HashSet<string>
    {
        "investment-mobile-status",
        "investment-mobile-start",
        "investment-mobile-stop",
    };

    public static bool AuthorizedRequester(string? requester) =>
        requester is GovernanceMainActor or SelfActor;

    public static bool Owns(string command) =>
        SnapshotCommands.Contains(command)
        || InstructionCommands.Contains(command)
        || LocalCommands.Contains(command);

    /// <summary>channel_runtime.execute(): requester allowlist then owns().</summary>
    public static string ExecuteGate(string? requester, string command) =>
        AuthorizedRequester(requester) && Owns(command)
            ? "ALLOW"
            : "PERMISSION_DENIED";

    /// <summary>ChannelClient.snapshot payload passthrough.</summary>
    public static JsonObject SnapshotRequestPayload(JsonObject? payload) =>
        payload is null ? new JsonObject() : (JsonObject)payload.DeepClone();

    /// <summary>ChannelClient.submit_instruction: fills a missing instruction
    /// unless operation == "update_shared_settings".</summary>
    public static JsonObject SubmitInstructionPayload(JsonObject payload)
    {
        var request = (JsonObject)payload.DeepClone();
        var operation = (request["operation"]?.GetValue<string>() ?? "").Trim();
        var instruction = (request["instruction"]?.GetValue<string>() ?? "").Trim();
        if (operation != "update_shared_settings" && instruction.Length == 0)
        {
            request["instruction"] = operation.Length > 0 ? operation : "status";
        }
        return request;
    }

    /// <summary>ChannelClient._request with no bound client.</summary>
    public static JsonObject RequestUnconnected() => new()
    {
        ["ok"] = false,
        ["queued"] = false,
        ["error_code"] = "AI_CHANNEL_NOT_CONNECTED",
        ["message"] = "投資手機版 AI 通道尚未連線。",
    };

    /// <summary>ChannelClient.send routing + ok/error event selection.</summary>
    public static (string Event, JsonObject Result) ChannelSend(
        string command, JsonObject payload, IChannelStub? client)
    {
        JsonObject result;
        if (command == SnapshotCommand)
        {
            result = ChannelRequest(SnapshotCommand, payload, client);
        }
        else if (command == InstructionCommand)
        {
            result = ChannelRequest(
                InstructionCommand, SubmitInstructionPayload(payload), client);
        }
        else
        {
            return ("error", new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "COMMAND_NOT_OWNED",
                ["command"] = command,
            });
        }
        var evt = OkIsNotFalse(result) ? "ok" : "error";
        return (evt, result);
    }

    /// <summary>ChannelClient._request semantics with an injected stub.</summary>
    public static JsonObject ChannelRequest(
        string command, JsonObject payload, IChannelStub? client)
    {
        if (client is null)
        {
            return RequestUnconnected();
        }
        return client.Request(XingchengToolId, command,
            (JsonObject)payload.DeepClone());
    }

    /// <summary>InvestmentMobileService._status() — dynamic fields injected.</summary>
    public static JsonObject StatusResult(string command, bool started,
        bool channelConnected) => new()
    {
        ["ok"] = true,
        ["tool_id"] = ToolId,
        ["command"] = command,
        ["started"] = started,
        ["channel_connected"] = channelConnected,
        ["transport"] = "governance-authenticated-shared-layer",
        ["lifecycle_owner"] = "governed-runtime",
        ["business_layer_owner"] = "ai-assistant",
        ["permission_profile"] = "ai-investment-manager-v1",
        ["relay_chain"] = "investment-mobile -> xingcheng -> ai-assistant",
    };

    /// <summary>InvestmentMobileService.handle() command routing.</summary>
    public static (string Event, JsonObject Result) Handle(
        string command, JsonObject payload, IChannelStub? client,
        bool started, bool channelConnected)
    {
        if (LocalCommands.Contains(command))
        {
            return ($"{command}_result",
                StatusResult(command, started, channelConnected));
        }
        if (SnapshotCommands.Contains(command))
        {
            return ($"{command}_result",
                ChannelRequest(SnapshotCommand, payload, client));
        }
        if (InstructionCommands.Contains(command))
        {
            return ($"{command}_result",
                ChannelRequest(InstructionCommand,
                    SubmitInstructionPayload(payload), client));
        }
        return ("PERMISSION_DENIED", new JsonObject { ["ok"] = false });
    }

    /// <summary>AnalyzeInvestmentUseCase.execute().</summary>
    public static JsonObject AnalyzeExecute(
        string portfolioId, IChannelStub? channel, JsonObject? dbPortfolio)
    {
        if (channel is not null)
        {
            return channel.Request(XingchengToolId, SnapshotCommand,
                new JsonObject { ["portfolio_id"] = portfolioId });
        }
        if (dbPortfolio is null)
        {
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "PORTFOLIO_NOT_FOUND",
                ["portfolio_id"] = portfolioId,
            };
        }
        return new JsonObject
        {
            ["ok"] = true,
            ["portfolio_id"] = portfolioId,
            ["portfolio"] = dbPortfolio.DeepClone(),
        };
    }

    /// <summary>ManagePortfolioUseCase.create_portfolio().</summary>
    public static JsonObject CreatePortfolio(
        string name, JsonArray assets, IChannelStub? channel)
    {
        var portfolio = new JsonObject
        {
            ["id"] = $"portfolio-{name}",
            ["name"] = name,
            ["assets"] = assets.DeepClone(),
        };
        if (channel is not null)
        {
            var instruction = SubmitInstructionPayload(new JsonObject
            {
                ["operation"] = "manage_portfolio",
                ["action"] = "create",
                ["portfolio"] = portfolio.DeepClone(),
                ["instruction"] = $"建立投資組合 {name}",
            });
            return channel.Request(XingchengToolId, InstructionCommand,
                instruction);
        }
        return new JsonObject { ["ok"] = true, ["portfolio"] = portfolio };
    }

    /// <summary>MarketDataClient.fetch_price().</summary>
    public static JsonObject FetchPrice(string symbol, IChannelStub? channel)
    {
        if (channel is null)
        {
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "NETWORK_ACCESS_DISABLED",
                ["symbol"] = symbol,
                ["source"] = "governed-channel",
            };
        }
        var result = channel.Request(XingchengToolId, InstructionCommand,
            SubmitInstructionPayload(new JsonObject
            {
                ["operation"] = "market_search",
                ["symbols"] = new JsonArray(symbol),
                ["instruction"] = $"market-search {symbol}",
            }));
        return new JsonObject
        {
            ["ok"] = OkIsNotFalse(result),
            ["symbol"] = symbol,
            ["source"] = "ai-assistant",
            ["quote"] = result,
        };
    }

    /// <summary>DatabaseClient.save_portfolio() — false when unbound.</summary>
    public static JsonNode SavePortfolio(JsonObject portfolio, IChannelStub? channel)
    {
        if (channel is null)
        {
            return JsonValue.Create(false)!;
        }
        var result = channel.Request(XingchengToolId, InstructionCommand,
            SubmitInstructionPayload(new JsonObject
            {
                ["operation"] = "manage_portfolio",
                ["action"] = "save",
                ["portfolio"] = portfolio.DeepClone(),
                ["instruction"] =
                    $"儲存投資組合 {portfolio["name"]?.GetValue<string>() ?? portfolio["id"]?.GetValue<string>() ?? ""}",
            }));
        return JsonValue.Create(OkIsNotFalse(result))!;
    }

    /// <summary>DatabaseClient.load_portfolio() — null when unbound/failed.</summary>
    public static JsonNode? LoadPortfolio(string portfolioId, IChannelStub? channel)
    {
        if (channel is null)
        {
            return null;
        }
        var snapshot = channel.Request(XingchengToolId, SnapshotCommand,
            new JsonObject { ["portfolio_id"] = portfolioId });
        if (OkIsFalse(snapshot))
        {
            return null;
        }
        return snapshot;
    }

    /// <summary>ExternalAPIClient.fetch_market_data().</summary>
    public static JsonObject FetchMarketData(string symbol, IChannelStub? channel)
    {
        if (channel is null)
        {
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "NETWORK_ACCESS_DISABLED",
                ["symbol"] = symbol,
                ["data"] = new JsonObject(),
            };
        }
        var result = channel.Request(XingchengToolId, InstructionCommand,
            SubmitInstructionPayload(new JsonObject
            {
                ["operation"] = "market_search",
                ["symbols"] = new JsonArray(symbol),
                ["instruction"] = $"market-search {symbol}",
            }));
        return new JsonObject
        {
            ["ok"] = OkIsNotFalse(result),
            ["symbol"] = symbol,
            ["data"] = result,
        };
    }

    /// <summary>InvestmentMobilePresenter.present_analysis wrap.</summary>
    public static JsonObject PresentAnalysis(JsonObject result) => new()
    {
        ["status"] = "success",
        ["data"] = result.DeepClone(),
    };

    /// <summary>PortfolioPresenter.present_create wrap.</summary>
    public static JsonObject PresentCreate(JsonObject result) => new()
    {
        ["status"] = "created",
        ["portfolio"] = result.DeepClone(),
    };
}
