// 星澄 AI 投資管理與自動操盤系統 — production IXingchengChannel
// binding over the governed transport submit lane
// (star-governed-transport-proxy/v1 ops: request/response/cancel on a
// hello submit-bound channel, actor = governance/tool/<tool>).
//
// Fail-closed throughout: no transport yet → AI_CHANNEL_NOT_CONNECTED;
// proxy denies → PERMISSION_DENIED passthrough; bounded timeout →
// cancel + REQUEST_TIMEOUT. Nothing retries silently or fabricates a
// response.

using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace InvestmentMobile.Service;

public sealed class ProxySubmitChannel : IXingchengChannel
{
    private readonly Func<IToolTransport?> _transport;
    private readonly string _channel;
    private readonly TimeSpan _timeout;
    private readonly TimeSpan _poll;

    public ProxySubmitChannel(
        Func<IToolTransport?> transportAccessor,
        string channel = "ai",
        TimeSpan? timeout = null,
        TimeSpan? pollInterval = null)
    {
        _transport = transportAccessor;
        _channel = channel;
        _timeout = timeout ?? TimeSpan.FromSeconds(15);
        _poll = pollInterval ?? TimeSpan.FromMilliseconds(250);
    }

    public async Task<JsonObject> RequestAsync(
        string targetToolId, string command, JsonObject payload,
        CancellationToken ct = default)
    {
        var transport = _transport();
        if (transport is null)
            return XingchengChannelClient.NotConnected();
        try
        {
            var queued = await transport.SubmitRequestAsync(
                _channel, targetToolId, command, payload,
                ct: ct).ConfigureAwait(false);
            if (queued is null)
                return XingchengChannelClient.NotConnected();
            if (queued["queued"] is JsonValue qv
                && qv.TryGetValue<bool>(out var q) && !q)
                return queued;
            var requestId =
                queued["request_id"]?.GetValue<string>() ?? "";
            if (requestId.Length == 0)
                return Fail("BAD_ENVELOPE");

            var deadline = DateTimeOffset.UtcNow + _timeout;
            while (DateTimeOffset.UtcNow < deadline)
            {
                var state = await transport.SubmitResponseAsync(
                    _channel, requestId, targetToolId, ct)
                    .ConfigureAwait(false);
                if (state is null)
                    return XingchengChannelClient.NotConnected();
                var status =
                    state["status"]?.GetValue<string>() ?? "";
                if (status == "completed")
                    return state["response"] as JsonObject
                        ?? Fail("BAD_ENVELOPE");
                if (status is "cancelled" or "failed")
                    return Fail($"REQUEST_{status.ToUpperInvariant()}");
                await Task.Delay(_poll, ct).ConfigureAwait(false);
            }
            try
            {
                await transport.SubmitCancelAsync(
                    _channel, requestId, targetToolId, ct)
                    .ConfigureAwait(false);
            }
            catch (ProxyErrorException) { /* best-effort cancel */ }
            return Fail("REQUEST_TIMEOUT");
        }
        catch (ProxyErrorException exc)
        {
            return Fail(exc.Code);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
    }

    private static JsonObject Fail(string code) => new()
    {
        ["ok"] = false,
        ["queued"] = false,
        ["error_code"] = code,
    };
}
