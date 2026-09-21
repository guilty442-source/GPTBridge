using System.Text.Json;
using StarBusinessLogic.Application;
using StarBusinessLogic.Infrastructure;

// Hybrid-host boundary (P7): C# owns business orchestration requests but is
// only a consumer of the Python-owned model-service lifecycle.  Python is not
// spawned or stopped here; Electron remains the outer process supervisor.
var toolRoot = Environment.GetEnvironmentVariable("GPTBRIDGE_TOOL_ROOT");
if (string.IsNullOrWhiteSpace(toolRoot))
{
    Console.Error.WriteLine("GPTBRIDGE_TOOL_ROOT_REQUIRED");
    return 2;
}

HttpModelClient? client = null;
try
{
    client = ModelServiceLocator.CreateClient(toolRoot);
    await WriteAsync(new { ok = true, event_name = "ready", owner = "local-model/channel_runtime.py" });

    string? line;
    while ((line = await Console.In.ReadLineAsync()) is not null)
    {
        if (string.IsNullOrWhiteSpace(line)) continue;
        try
        {
            using var document = JsonDocument.Parse(line);
            var root = document.RootElement;
            var command = root.TryGetProperty("command", out var commandElement)
                ? commandElement.GetString()
                : null;
            if (command == "shutdown")
            {
                await WriteAsync(new { ok = true, event_name = "stopping" });
                break;
            }
            if (command == "status")
            {
                var status = await client.StatusAsync();
                await WriteAsync(new { ok = true, command, status });
                continue;
            }
            if (command == "infer")
            {
                var prompt = root.TryGetProperty("prompt", out var promptElement)
                    ? promptElement.GetString() ?? ""
                    : "";
                var context = root.TryGetProperty("context", out var contextElement)
                    ? contextElement.GetString() ?? ""
                    : "";
                var intent = root.TryGetProperty("intent", out var intentElement)
                    ? intentElement.GetString() ?? ""
                    : "";
                var response = await client.InferAsync(
                    new ModelInferenceRequest(prompt, context, intent),
                    CancellationToken.None);
                await WriteAsync(new { ok = true, command, response });
                continue;
            }
            await WriteAsync(new { ok = false, error_code = "UNKNOWN_COMMAND" });
        }
        catch (Exception error)
        {
            await WriteAsync(new
            {
                ok = false,
                error_code = "BUSINESS_HOST_COMMAND_FAILED",
                message = error.GetType().Name,
            });
        }
    }
}
catch (Exception error)
{
    await WriteAsync(new
    {
        ok = false,
        error_code = "BUSINESS_HOST_NOT_READY",
        message = error.GetType().Name,
    });
    return 1;
}
finally
{
    client?.Dispose();
}

return 0;

static async Task WriteAsync(object value)
{
    await Console.Out.WriteLineAsync(JsonSerializer.Serialize(value));
    await Console.Out.FlushAsync();
}
