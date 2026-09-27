// Per-tool entry-point helper: a governed native tool exe is
//   return await ToolHostProgram.RunAsync(new MyExecutor(), "1.0.0");
// Spawn parity with _spawn_tool_process: the process must survive past
// startup — a PERMISSION_DENIED env or a denied hello exits non-zero so
// main-system reports SOURCE_RUNTIME_EXITED / PROCESS_START_FAILED.
namespace GPTBridge.ToolHost;

public static class ToolHostProgram
{
    public static async Task<int> RunAsync(
        IGovernedCommandExecutor executor,
        string version,
        string[]? processingChannels = null)
    {
        GovernedEnvironment env;
        try
        {
            env = GovernedEnvironment.Load();
        }
        catch (PermissionDeniedException)
        {
            return 13; // fail-closed, mirrors python PermissionError exit
        }

        await using var host = new GovernedToolHost(
            env, executor, version,
            processingChannels: processingChannels);
        await using var server = new ToolHostServer(host);
        server.Start();
        try
        {
            await host.RunWorkerAsync().ConfigureAwait(false);
        }
        catch (ProxyErrorException exc) when (exc.Code is "PROXY_DISCONNECTED")
        {
            return 0; // sidecar gone → fail-closed quiet exit
        }
        catch (ProxyErrorException)
        {
            return 13;
        }
        return 0;
    }
}
