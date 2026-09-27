// system-rescue native host — governed C# runtime for the
// "migrate-csharp" queue entry. Commands mirror
// src/channel_runtime.py's executor exactly; the governed transport is
// delegated to the Python sidecar (star-governed-transport-proxy/v1).
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.SystemRescue;

public sealed class SystemRescueExecutor : IGovernedCommandExecutor
{
    private const string ToolId = "system-rescue";

    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
    {
        switch (command)
        {
            case "system_health_check":
                return Task.FromResult(("system_health_check_result",
                    new JsonObject
                    {
                        ["ok"] = true,
                        ["tool_id"] = ToolId,
                        ["authority"] = "main-system-central-packaging-only",
                        ["resident"] = false,
                    }));
            case "system_rescue_status":
                return Task.FromResult(("system_rescue_status_result",
                    new JsonObject
                    {
                        ["ok"] = true,
                        ["tool_id"] = ToolId,
                        ["authority"] = "main-system",
                        ["channels"] = new JsonArray("system"),
                        ["audit_records_owner"] = true,
                        ["runtime_logs_owner"] = true,
                    }));
            default:
                throw new PermissionDeniedException();
        }
    }

    public JsonObject Health() => new()
    {
        ["service_ready"] = true,
        ["resident"] = false,
        ["authority"] = "main-system-central-packaging-only",
        ["tool_id"] = ToolId,
    };
}

internal static class Program
{
    private static async Task<int> Main()
    {
        var version = "1.0.0";
        try
        {
            var toolDir = Environment
                .GetEnvironmentVariable("GPTBRIDGE_TOOL_DIR")?.Trim();
            var manifestPath = Path.Combine(
                toolDir is { Length: > 0 }
                    ? toolDir
                    : Path.Combine(AppContext.BaseDirectory, ".."),
                "manifest.json");
            if (File.Exists(manifestPath))
            {
                using var doc = System.Text.Json.JsonDocument.Parse(
                    File.ReadAllText(manifestPath));
                var v = doc.RootElement
                    .TryGetProperty("version", out var el)
                    ? el.GetString() : null;
                if (!string.IsNullOrWhiteSpace(v))
                    version = v.Trim();
            }
        }
        catch { /* version falls back to constant */ }
        return await ToolHostProgram.RunAsync(
            new SystemRescueExecutor(), version);
    }
}
