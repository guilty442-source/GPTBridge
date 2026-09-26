// Transport adapter contract owned by the information layer (A177).
//
// C# port of the ChannelTransport Protocol in
// shared-layer/src/shared_layer/channel_runtime.py.  Messages travel as
// JsonObject so every endpoint shares the Python wire vocabulary.

using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

public interface IChannelTransport
{
    Task SendAsync(JsonObject message, CancellationToken cancellationToken);
    /// <summary>Next message, or null when the peer closed the channel.</summary>
    Task<JsonObject?> ReceiveAsync(CancellationToken cancellationToken);
    Task CloseAsync(int code, string reason, CancellationToken cancellationToken);
    bool IsClosed { get; }
}
