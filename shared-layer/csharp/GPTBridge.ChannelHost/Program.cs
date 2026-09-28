using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using GPTBridge.Channels;

namespace GPTBridge.ChannelHost;

internal sealed class ClientWebSocketTransport : IChannelTransport, IDisposable
{
    private readonly ClientWebSocket _socket;
    private readonly SemaphoreSlim _sendLock = new(1, 1);

    private ClientWebSocketTransport(ClientWebSocket socket) => _socket = socket;

    public static async Task<ClientWebSocketTransport> ConnectAsync(
        Uri endpoint, CancellationToken cancellationToken)
    {
        var socket = new ClientWebSocket();
        socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(10);
        await socket.ConnectAsync(endpoint, cancellationToken).ConfigureAwait(false);
        return new ClientWebSocketTransport(socket);
    }

    public bool IsClosed => _socket.State is WebSocketState.Closed
        or WebSocketState.Aborted or WebSocketState.CloseReceived;

    public async Task SendAsync(JsonObject message, CancellationToken cancellationToken)
    {
        var bytes = Encoding.UTF8.GetBytes(message.ToJsonString());
        await _sendLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await _socket.SendAsync(bytes, WebSocketMessageType.Text, true,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _sendLock.Release();
        }
    }

    public async Task<JsonObject?> ReceiveAsync(CancellationToken cancellationToken)
    {
        var buffer = new byte[1024 * 1024];
        using var output = new MemoryStream();
        WebSocketReceiveResult result;
        do
        {
            result = await _socket.ReceiveAsync(buffer, cancellationToken)
                .ConfigureAwait(false);
            if (result.MessageType == WebSocketMessageType.Close)
                return null;
            output.Write(buffer, 0, result.Count);
            if (output.Length > 1024 * 1024)
                throw new InvalidOperationException("CHANNEL_MESSAGE_TOO_LARGE");
        } while (!result.EndOfMessage);
        return JsonNode.Parse(output.ToArray()) as JsonObject
            ?? throw new InvalidOperationException("CHANNEL_MESSAGE_INVALID_JSON");
    }

    public async Task CloseAsync(int code, string reason,
        CancellationToken cancellationToken)
    {
        if (!IsClosed)
            await _socket.CloseAsync((WebSocketCloseStatus)code, reason,
                cancellationToken).ConfigureAwait(false);
    }

    public void Dispose()
    {
        _socket.Dispose();
        _sendLock.Dispose();
    }
}

internal static class Program
{
    public static async Task<int> Main()
    {
        var raw = Environment.GetEnvironmentVariable(
            "GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL");
        if (!Uri.TryCreate(raw, UriKind.Absolute, out var endpoint)
            || endpoint.Scheme is not ("ws" or "wss"))
        {
            Console.Error.WriteLine("CHANNEL_HOST_WEBSOCKET_URL_INVALID");
            return 13;
        }

        using var stop = new CancellationTokenSource();
        Console.CancelKeyPress += (_, args) =>
        {
            args.Cancel = true;
            stop.Cancel();
        };
        try
        {
            using var transport = await ClientWebSocketTransport
                .ConnectAsync(endpoint, stop.Token).ConfigureAwait(false);
            var channel = await A263Channel.CreateAsync(
                Environment.GetEnvironmentVariable("GPTBRIDGE_CHANNEL_ID")
                    ?? "system",
                transport,
                cancellationToken: stop.Token).ConfigureAwait(false);
            channel.SetCallbacks(onMessage: _ => Task.CompletedTask,
                onControl: _ => Task.CompletedTask);
            await Task.Delay(Timeout.InfiniteTimeSpan, stop.Token)
                .ConfigureAwait(false);
            await channel.DisconnectAsync(1000, "shutdown", CancellationToken.None);
            return 0;
        }
        catch (OperationCanceledException)
        {
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine($"CHANNEL_HOST_FAILED:{error.Message}");
            return 13;
        }
    }
}
