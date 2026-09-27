// Transport abstraction for the governed channel ops a tool host needs.
// The live implementation is TransportProxyClient (star-governed-
// transport-proxy/v1, P2 stdio sidecar); tests substitute an in-memory
// fake. Method set = the proxy's "process"-side ops plus submit-side
// ops for completeness (submit requires a submit-bound channel in hello).
using System.Text.Json.Nodes;

namespace GPTBridge.ToolHost;

public sealed class ProxyErrorException : Exception
{
    public ProxyErrorException(string code, string message)
        : base(string.IsNullOrEmpty(message) ? code : message)
    {
        Code = code;
    }

    public string Code { get; }

    public bool IsPermissionDenied => Code == "PERMISSION_DENIED";
}

public interface IToolTransport : IAsyncDisposable
{
    /// <summary>hello: bind tool identity and channel modes.</summary>
    Task<JsonObject> HelloAsync(
        string toolId,
        string workspaceInstanceId,
        IReadOnlyDictionary<string, string> channels,
        CancellationToken ct = default);

    /// <summary>claim: next queued request for this tool, or null.</summary>
    Task<JsonObject?> ClaimAsync(string channel, CancellationToken ct = default);

    /// <summary>respond: attach the executor result to a claimed request.</summary>
    Task<bool> RespondAsync(
        string channel, string requestId, JsonNode? response,
        CancellationToken ct = default);

    /// <summary>request_cancelled: poll whether the requester cancelled.</summary>
    Task<bool> RequestCancelledAsync(
        string channel, string requestId, CancellationToken ct = default);

    /// <summary>progress: publish intermediate progress for a request.</summary>
    Task<bool> ProgressAsync(
        string channel, string requestId, JsonNode? payload,
        CancellationToken ct = default);

    /// <summary>notification_stamp: cheap transport write stamp or null.</summary>
    Task<JsonNode?> NotificationStampAsync(
        string channel, CancellationToken ct = default);

    /// <summary>Fired once when the transport is unrecoverably lost.</summary>
    event Action? Disconnected;
}
