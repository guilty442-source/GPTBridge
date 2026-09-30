// Transport abstraction for the governed channel ops a tool host needs.
// The live implementation is TransportProxyClient (star-governed-
// transport-proxy/v1, P2 stdio sidecar); tests substitute an in-memory
// fake. Method set = the proxy's "process"-side ops plus submit-side
// ops for completeness (submit requires a submit-bound channel in hello).
using System.Text.Json.Nodes;

namespace GPTBridge.ToolHost;

/// <summary>Submit-side channel binding declared in hello —
/// {channel: {actor, authorizer?}} per star-governed-transport-proxy/v1.</summary>
public sealed record SubmitBinding(string Actor, string? Authorizer = null);

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
    /// <summary>hello: bind tool identity and channel modes. Channels
    /// declared "submit" must carry a <paramref name="submitBindings"/>
    /// entry ({channel → actor}) or the proxy denies the bind.</summary>
    Task<JsonObject> HelloAsync(
        string toolId,
        string workspaceInstanceId,
        IReadOnlyDictionary<string, string> channels,
        IReadOnlyDictionary<string, SubmitBinding>? submitBindings = null,
        CancellationToken ct = default);

    /// <summary>submit-side "request": queue a governed request to
    /// another tool. Returns {request_id, queued} or null when the
    /// transport has no submit lane (fail-closed).</summary>
    Task<JsonObject?> SubmitRequestAsync(
        string channel, string targetToolId, string command,
        JsonObject payload, string? requestId = null,
        CancellationToken ct = default)
        => Task.FromResult<JsonObject?>(null);

    /// <summary>submit-side "response": poll a queued request's
    /// outcome. Result {status: pending|completed, request_id,
    /// response?}; null when no submit lane exists.</summary>
    Task<JsonObject?> SubmitResponseAsync(
        string channel, string requestId, string targetToolId,
        CancellationToken ct = default)
        => Task.FromResult<JsonObject?>(null);

    /// <summary>submit-side "cancel": mark a queued request cancelled.
    /// False when not cancellable or no submit lane.</summary>
    Task<bool> SubmitCancelAsync(
        string channel, string requestId, string targetToolId,
        CancellationToken ct = default)
        => Task.FromResult(false);

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
