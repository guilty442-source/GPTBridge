// Deferred IToolTransport: the store-less stand-in for tools whose
// governed claim lane has no transport-proxy sidecar yet.
//
// Convention (shared with the Go lane govenv.go): when the launcher
// injects no GPTBRIDGE_TOOLHOST_PROXY_ENTRY the host still runs —
// hello reports the deferred marker, every claim returns null (no
// queue exists), submit-side ops answer null/false so callers resolve
// AI_CHANNEL_NOT_CONNECTED-style fail-closed paths instead of
// fabricating a binding. The WS command surface remains the only
// request path. When the real star-governed-transport-proxy/v1 sidecar
// ships, launchers inject the entry and TransportProxyClient.Start
// takes over — this class is never used then.
using System.Text.Json.Nodes;

namespace GPTBridge.ToolHost;

/// <summary>
/// Store-free IToolTransport: hello reports the deferred marker, every
/// claim returns null (no queue exists yet), side ops answer false/null.
/// </summary>
public sealed class DeferredStoreTransport : IToolTransport
{
#pragma warning disable CS0067 // never fires until a real transport lands
    public event Action? Disconnected;
#pragma warning restore CS0067
    public Task<JsonObject> HelloAsync(
        string toolId, string workspaceInstanceId,
        IReadOnlyDictionary<string, string> channels,
        IReadOnlyDictionary<string, SubmitBinding>? submitBindings =
            null,
        CancellationToken ct = default) => Task.FromResult(new JsonObject
    {
        ["ok"] = true,
        ["deferred"] = true,
        ["reason"] = "transport-store-native-successor-pending",
    });

    public Task<JsonObject?> ClaimAsync(
        string channel, CancellationToken ct = default)
        => Task.FromResult<JsonObject?>(null);

    public Task<bool> RespondAsync(
        string channel, string requestId, JsonNode? response,
        CancellationToken ct = default) => Task.FromResult(false);

    public Task<bool> RequestCancelledAsync(
        string channel, string requestId, CancellationToken ct = default)
        => Task.FromResult(false);

    public Task<bool> ProgressAsync(
        string channel, string requestId, JsonNode? payload,
        CancellationToken ct = default) => Task.FromResult(false);

    public Task<JsonNode?> NotificationStampAsync(
        string channel, CancellationToken ct = default)
        => Task.FromResult<JsonNode?>(null);

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
