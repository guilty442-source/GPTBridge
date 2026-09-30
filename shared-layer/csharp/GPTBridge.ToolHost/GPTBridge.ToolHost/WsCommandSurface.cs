// WebSocket command surface for tool executors.
//
// Source-UI tools (star-chat and siblings) drive their business executor
// over the authenticated loopback WebSocket instead of the transport
// store — the store path serves headless claim/execute traffic while the
// WS lane carries interactive commands with progress events and
// cancellation (design §10). Executors opt in by implementing this
// interface; commands not owned by the surface stay PERMISSION_DENIED.
using System.Text.Json.Nodes;

namespace GPTBridge.ToolHost;

/// <summary>
/// Executor-owned WS command surface. OwnsCommand is the allowlist —
/// every accepted command must be declared here; unknown commands are
/// rejected by the server before reaching the executor.
/// </summary>
public interface IWsCommandSurface
{
    /// <summary>True when this executor handles <paramref name="command"/>.</summary>
    bool OwnsCommand(string command);

    /// <summary>
    /// Execute one WS command. emitProgress publishes
    /// ``&lt;command&gt;_progress`` frames to the requesting socket; the
    /// returned event name is emitted as ``&lt;command&gt;_result`` (or an
    /// explicit event of the executor's choice).
    /// </summary>
    Task<(string Event, JsonObject Result)> ExecuteWsAsync(
        string command,
        JsonObject payload,
        string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken cancellationToken);
}
