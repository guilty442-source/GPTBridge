// Channel data types — A263 channel contract types.
//
// C# port of shared-layer/src/shared_layer/channel_types.py
// (information-channel-gateway, migrate-csharp).  Pure data containers —
// no business logic.  Field names and value encodings are wire-compatible
// with the Python dataclasses so a C# endpoint speaks the same protocol.

using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

/// <summary>Channel lifecycle states per A263.</summary>
public enum ChannelState
{
    Closed,
    Connecting,
    Open,
    Reconnecting,
    Dead,
}

/// <summary>Wire value of <see cref="ChannelState"/> (Python enum values).</summary>
public static class ChannelStateNames
{
    public static string ToWire(this ChannelState state) => state switch
    {
        ChannelState.Closed => "closed",
        ChannelState.Connecting => "connecting",
        ChannelState.Open => "open",
        ChannelState.Reconnecting => "reconnecting",
        ChannelState.Dead => "dead",
        _ => state.ToString().ToLowerInvariant(),
    };
}

/// <summary>Message priority for the control channel (A263).</summary>
public enum MessagePriority
{
    Control = 0,   // heartbeat, ack, cursor, reconnect
    State = 1,     // state events from outbox
    Command = 2,   // user commands
}

/// <summary>Typed generation identifier for a channel (A263).</summary>
public sealed class ChannelGeneration
{
    public string ChannelId { get; set; } = "";
    public int Generation { get; set; }
    public string CreatedAt { get; set; } =
        DateTime.UtcNow.ToString("o");
    public string BackendGeneration { get; set; } = "";
    public string SessionId { get; set; } = "";

    /// <summary>Wire projection — keys match ChannelGeneration.as_dict().</summary>
    public JsonObject AsDict() => new()
    {
        ["channel_id"] = ChannelId,
        ["generation"] = Generation,
        ["created_at"] = CreatedAt,
        ["backend_generation"] = BackendGeneration,
        ["session_id"] = SessionId,
    };

    public string FullId => $"{ChannelId}:{Generation}";
}

/// <summary>Channel configuration per A263 (same defaults as Python).</summary>
public sealed class ChannelConfig
{
    public required string ChannelId { get; set; }
    public int MaxQueueSize { get; set; } = 1000;
    public double HeartbeatIntervalSeconds { get; set; } = 10.0;
    public double HeartbeatTimeoutSeconds { get; set; } = 30.0;
    public int ReconnectMaxAttempts { get; set; } = 3;
    public double ReconnectBaseDelaySeconds { get; set; } = 1.0;
    public int ControlChannelCapacity { get; set; } = 100;
    public bool EnableBackpressure { get; set; } = true;
    public int MaxOutboundBatch { get; set; } = 100;
    public int MaxEventPayloadBytes { get; set; } = 1_048_576;
    public double SendIdleSleepSeconds { get; set; } = 0.001;
}

/// <summary>Transactional outbox event (A195/A263).</summary>
public sealed class OutboxEvent
{
    public int Sequence { get; set; }
    public string EntityId { get; set; } = "";
    public string EntityType { get; set; } = "";
    public string Operation { get; set; } = "";
    public JsonObject Payload { get; set; } = new();
    public string StateHash { get; set; } = "";
    public string IdempotencyKey { get; set; } = "";
    public string Timestamp { get; set; } =
        DateTime.UtcNow.ToString("o");
}
