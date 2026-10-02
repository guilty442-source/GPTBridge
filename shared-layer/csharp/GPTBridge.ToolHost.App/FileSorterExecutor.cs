// file-sorter governed executor — WS surface for the arg-typed
// ``toolbox_run_tool`` contract. Owns exactly one command; every run
// carries a tool_id guard so a frame addressed to another tool fails
// closed, and the serial gate mirrors the UI's single-run queue.
// Engine semantics live in FileSorterEngine.

using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal sealed class FileSorterExecutor
    : IGovernedCommandExecutor, IWsCommandSurface
{
    internal const string ToolId = "file-sorter";
    private const int DefaultTimeoutSeconds = 600;

    private readonly GovernedEnvironment _env;
    private readonly SemaphoreSlim _runGate = new(1, 1);
    private int _runsCompleted;

    public FileSorterExecutor(GovernedEnvironment env) => _env = env;

    public bool OwnsCommand(string command) =>
        command == "toolbox_run_tool";

    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
        => ExecuteWsAsync(command, payload, requestId, null,
            cancellationToken);

    public async Task<(string Event, JsonObject Result)> ExecuteWsAsync(
        string command, JsonObject payload, string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken cancellationToken)
    {
        if (!OwnsCommand(command))
            throw new PermissionDeniedException();
        var toolId = payload["tool_id"]?.GetValue<string>()?.Trim() ?? "";
        if (toolId != ToolId)
            throw new PermissionDeniedException();
        var args = (payload["args"] as JsonArray)
            ?.Select(n => n?.GetValue<string>() ?? "")
            .Where(a => a.Length > 0)
            .ToList() ?? [];
        var timeoutSeconds = payload["timeout_seconds"]?.GetValue<int>()
            ?? DefaultTimeoutSeconds;
        timeoutSeconds = Math.Clamp(timeoutSeconds, 1, 3600);

        // Serial run queue (one active run per tool window contract).
        if (!await _runGate.WaitAsync(0, cancellationToken)
            .ConfigureAwait(false))
        {
            return ("toolbox_run_tool_result", new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = ToolId,
                ["stdout"] = "",
                ["error_code"] = "FILE_SORTER_BUSY",
                ["message"] = "已有整理工作正在執行",
            });
        }
        try
        {
            using var timeoutCts = CancellationTokenSource
                .CreateLinkedTokenSource(cancellationToken);
            timeoutCts.CancelAfter(
                TimeSpan.FromSeconds(timeoutSeconds));
            var stdout = await Task.Run(
                () => FileSorterEngine.Run(
                    args,
                    progress =>
                    {
                        if (emitProgress is not null)
                            _ = emitProgress(progress);
                        return true;
                    },
                    timeoutCts.Token),
                timeoutCts.Token).ConfigureAwait(false);
            _runsCompleted++;
            // Apply results carry their own ok flag inside the plan
            // frame; surface it at the receipt level too.
            var planOk = stdout.StartsWith(
                FileSorterEngine.PlanPrefix,
                StringComparison.Ordinal)
                && JsonNode.Parse(
                    stdout[FileSorterEngine.PlanPrefix.Length..])
                    is JsonObject planResult
                && planResult["ok"]?.GetValue<bool>() == false;
            return ("toolbox_run_tool_result", new JsonObject
            {
                ["ok"] = !planOk,
                ["tool_id"] = ToolId,
                ["stdout"] = stdout,
            });
        }
        catch (FileSorterException exc)
        {
            return ("toolbox_run_tool_result", new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = ToolId,
                ["stdout"] = "",
                ["error_code"] = exc.ErrorCode,
                ["message"] = exc.Message,
            });
        }
        finally
        {
            _runGate.Release();
        }
    }

    public JsonObject Health() => new()
    {
        ["executor_state"] = "file-sorter",
        ["surface"] = new JsonArray("toolbox_run_tool"),
        ["runs_completed"] = _runsCompleted,
        ["state_root"] = FileSorterEngine.StateRoot(),
        ["pending_seams"] = new JsonArray(
            "cleanup-scan:media-analysis-lane"),
    };
}
