// Per-tool entry-point helper: a governed native tool exe is
//   return await ToolHostProgram.RunAsync(new MyExecutor(), "1.0.0");
// Spawn parity with _spawn_tool_process: the process must survive past
// startup — a PERMISSION_DENIED env or a denied hello exits non-zero so
// main-system reports SOURCE_RUNTIME_EXITED / PROCESS_START_FAILED.
namespace GPTBridge.ToolHost;

public static class ToolHostProgram
{
    public static Task<int> RunAsync(
        IGovernedCommandExecutor executor,
        string version,
        string[]? processingChannels = null) =>
        RunAsync(_ => executor, version, processingChannels);

    /// <summary>Factory form — the executor is built from the validated
    /// governed environment (tool root, identity) instead of before it,
    /// so state dirs and native component paths resolve correctly.</summary>
    public static async Task<int> RunAsync(
        Func<GovernedEnvironment, IGovernedCommandExecutor>
            executorFactory,
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
            Console.Error.WriteLine(
                "[toolhost] PERMISSION_DENIED: governed environment "
                + "validation failed");
            return 13; // fail-closed, mirrors python PermissionError exit
        }

        var executor = executorFactory(env);
        await using var host = new GovernedToolHost(
            env, executor, version,
            processingChannels: processingChannels);
        await using var server = new ToolHostServer(host);
        server.Start();
        try
        {
            await host.RunWorkerAsync().ConfigureAwait(false);
            // Graceful exit after an unexpected sidecar death still counts
            // as a failure surface — record the tail for stderr.log.
            var lostProxy = host.Transport as TransportProxyClient;
            if (lostProxy?.DisconnectedFlag == true)
            {
                var tail = lostProxy.StderrTail;
                Console.Error.WriteLine(
                    "[toolhost] exited after proxy disconnect"
                    + (string.IsNullOrWhiteSpace(tail)
                        ? "" : $" :: sidecar stderr tail: {tail.Trim()}"));
            }
        }
        catch (ProxyErrorException exc) when (exc.Code is "PROXY_DISCONNECTED")
        {
            // Sidecar gone → fail-closed quiet exit, but keep the proxy's
            // last stderr bytes in stderr.log so the death is diagnosable.
            var tail = (host.Transport as TransportProxyClient)?.StderrTail;
            Console.Error.WriteLine(
                "[toolhost] PROXY_DISCONNECTED"
                + (string.IsNullOrWhiteSpace(tail)
                    ? "" : $" :: sidecar stderr tail: {tail.Trim()}"));
            return 0;
        }
        catch (ProxyErrorException exc)
        {
            var tail = (host.Transport as TransportProxyClient)?.StderrTail;
            Console.Error.WriteLine(
                $"[toolhost] proxy error {exc.Code}: {exc.Message}"
                + (string.IsNullOrWhiteSpace(tail)
                    ? "" : $" :: sidecar stderr tail: {tail.Trim()}"));
            return 13;
        }
        return 0;
    }
}
