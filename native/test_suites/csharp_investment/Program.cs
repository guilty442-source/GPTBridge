// M1 investment-mobile C# shadow — case-matrix emitter.
// Prints a JSON array of {id, output} covering the decision-free
// semantics surface; the Python parity test replays the same matrix
// through the authoritative Python functions and diffs field-by-field.
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using InvestmentMobileShadow;

Console.OutputEncoding = Encoding.UTF8;

static JsonObject J(params (string Key, JsonNode? Value)[] pairs)
{
    var o = new JsonObject();
    foreach (var (k, v) in pairs) o[k] = v;
    return o;
}

static JsonArray Arr(params JsonNode?[] items)
{
    var a = new JsonArray();
    foreach (var i in items) a.Add(i);
    return a;
}

var cases = new JsonArray();
void Emit(string id, JsonNode? output) =>
    cases.Add(new JsonObject { ["id"] = id, ["output"] = output });

var echo = new EchoChannelStub();
var failing = new FailingChannelStub();

// --- owns / authorized requester ---
foreach (var cmd in new[]
{
    "investment-analysis", "investment-mobile-get-snapshot",
    "investment-manager", "investment-market-search",
    "investment-mobile-submit-instruction", "investment-mobile-rotate-pairing",
    "investment-mobile-status", "investment-mobile-start",
    "investment-mobile-stop", "bogus-command", "",
})
{
    Emit($"owns:{cmd}", JsonValue.Create(
        InvestmentMobileSemantics.Owns(cmd)));
}

foreach (var actor in new[]
{
    "governance/main-system", "governance/tool/investment-mobile",
    "governance/tool/other", "", null as string,
})
{
    Emit($"requester:{actor ?? "<null>"}", JsonValue.Create(
        InvestmentMobileSemantics.AuthorizedRequester(actor)));
}

Emit("execute_gate:authorized+owned", JsonValue.Create(
    InvestmentMobileSemantics.ExecuteGate(
        "governance/main-system", "investment-mobile-status")));
Emit("execute_gate:authorized+foreign", JsonValue.Create(
    InvestmentMobileSemantics.ExecuteGate(
        "governance/main-system", "bogus")));
Emit("execute_gate:unauthorized+owned", JsonValue.Create(
    InvestmentMobileSemantics.ExecuteGate(
        "governance/tool/other", "investment-mobile-status")));

// --- command routing (handle) ---
var payload = J(("request_id", JsonValue.Create("r-1")),
                ("key", JsonValue.Create("v")));
foreach (var (cmd, client) in new[]
{
    ("investment-mobile-status", (IChannelStub?)echo),
    ("investment-mobile-start", (IChannelStub?)echo),
    ("investment-analysis", (IChannelStub?)echo),
    ("investment-manager", (IChannelStub?)echo),
    ("bogus", (IChannelStub?)echo),
})
{
    var (evt, result) = InvestmentMobileSemantics.Handle(
        cmd, (JsonObject)payload.DeepClone(), client,
        started: true, channelConnected: client is not null);
    Emit($"handle:{cmd}", J(("event", JsonValue.Create(evt)),
                           ("result", result)));
}

// --- ChannelClient.submit_instruction instruction-filling ---
Emit("submit:fills_instruction_from_operation",
    InvestmentMobileSemantics.SubmitInstructionPayload(
        J(("operation", JsonValue.Create("market_search")))));
Emit("submit:update_shared_settings_keeps_empty",
    InvestmentMobileSemantics.SubmitInstructionPayload(
        J(("operation", JsonValue.Create("update_shared_settings")))));
Emit("submit:no_operation_falls_back_status",
    InvestmentMobileSemantics.SubmitInstructionPayload(J()));
Emit("submit:explicit_instruction_kept",
    InvestmentMobileSemantics.SubmitInstructionPayload(
        J(("operation", JsonValue.Create("market_search")),
          ("instruction", JsonValue.Create("custom")))));

// --- ChannelClient._request unconnected ---
Emit("request:unconnected", InvestmentMobileSemantics.RequestUnconnected());

// --- ChannelClient.send routing ---
foreach (var (id, cmd) in new[]
{
    ("send:snapshot", InvestmentMobileSemantics.SnapshotCommand),
    ("send:instruction", InvestmentMobileSemantics.InstructionCommand),
    ("send:unowned", "other_command"),
})
{
    var (evt, result) = InvestmentMobileSemantics.ChannelSend(
        cmd, J(("operation", JsonValue.Create("market_search"))), echo);
    Emit(id, J(("event", JsonValue.Create(evt)), ("result", result)));
}
{
    var (evt, result) = InvestmentMobileSemantics.ChannelSend(
        InvestmentMobileSemantics.SnapshotCommand, J(), failing);
    Emit("send:failing_channel", J(("event", JsonValue.Create(evt)),
                                   ("result", result)));
}

// --- AnalyzeInvestmentUseCase.execute ---
Emit("analyze:channel_connected",
    InvestmentMobileSemantics.AnalyzeExecute("p-1", echo, null));
Emit("analyze:db_found",
    InvestmentMobileSemantics.AnalyzeExecute("p-2", null,
        J(("id", JsonValue.Create("p-2")), ("name", JsonValue.Create("n")))));
Emit("analyze:not_found",
    InvestmentMobileSemantics.AnalyzeExecute("p-3", null, null));

// --- ManagePortfolioUseCase.create_portfolio ---
var assets = Arr(J(("symbol", JsonValue.Create("BTC")),
                   ("qty", JsonValue.Create(2))));
Emit("create:channel_connected",
    InvestmentMobileSemantics.CreatePortfolio("alpha",
        (JsonArray)assets.DeepClone(), echo));
Emit("create:db_fallback",
    InvestmentMobileSemantics.CreatePortfolio("alpha",
        (JsonArray)assets.DeepClone(), null));

// --- MarketDataClient.fetch_price ---
Emit("fetch_price:no_channel",
    InvestmentMobileSemantics.FetchPrice("BTC", null));
Emit("fetch_price:ok",
    InvestmentMobileSemantics.FetchPrice("BTC", echo));
Emit("fetch_price:channel_failure",
    InvestmentMobileSemantics.FetchPrice("BTC", failing));

// --- DatabaseClient ---
var portfolio = J(("id", JsonValue.Create("p-9")),
                  ("name", JsonValue.Create("beta")),
                  ("assets", Arr()));
Emit("save:no_channel", InvestmentMobileSemantics.SavePortfolio(
    (JsonObject)portfolio.DeepClone(), null));
Emit("save:ok", InvestmentMobileSemantics.SavePortfolio(
    (JsonObject)portfolio.DeepClone(), echo));
Emit("save:channel_failure", InvestmentMobileSemantics.SavePortfolio(
    (JsonObject)portfolio.DeepClone(), failing));
Emit("load:no_channel",
    InvestmentMobileSemantics.LoadPortfolio("p-9", null) ?? JsonValue.Create<string?>(null));
Emit("load:ok",
    InvestmentMobileSemantics.LoadPortfolio("p-9", echo) ?? JsonValue.Create<string?>(null));
Emit("load:channel_failure",
    InvestmentMobileSemantics.LoadPortfolio("p-9", failing) ?? JsonValue.Create<string?>(null));

// --- ExternalAPIClient.fetch_market_data ---
Emit("ext_market:no_channel",
    InvestmentMobileSemantics.FetchMarketData("ETH", null));
Emit("ext_market:ok",
    InvestmentMobileSemantics.FetchMarketData("ETH", echo));

// --- presenters ---
Emit("present:analysis",
    InvestmentMobileSemantics.PresentAnalysis(
        J(("ok", JsonValue.Create(true)))));
Emit("present:create",
    InvestmentMobileSemantics.PresentCreate(
        J(("ok", JsonValue.Create(true)),
          ("portfolio", J(("id", JsonValue.Create("p-1")))))));

Console.WriteLine(cases.ToJsonString(
    new JsonSerializerOptions { WriteIndented = false }));
return 0;
