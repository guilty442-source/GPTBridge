using System.Text.Json;

namespace StarBusinessLogic.Infrastructure;

// star-model-service-descriptor/v1：Python ModelService 啟動時原子寫入
// <toolRoot>/xingcheng/runtime/ipc/model-service.json，停止時移除。
// C# 編排層據此定位 loopback endpoint 與 session token，fail-closed。
public sealed record ModelServiceEndpoint(
    string Endpoint,
    string SessionToken,
    int Pid,
    string TokenFile,
    string LifecycleOwner,
    string ConsumerPolicy
);

public static class ModelServiceLocator
{
    public const string DescriptorSchema = "star-model-service-descriptor/v1";

    public static ModelServiceEndpoint Discover(string toolRoot)
    {
        if (string.IsNullOrWhiteSpace(toolRoot)) throw new ArgumentException("TOOL_ROOT_REQUIRED", nameof(toolRoot));
        var ipcDir = Path.Combine(toolRoot, "xingcheng", "runtime", "ipc");
        var descriptorPath = Path.Combine(ipcDir, "model-service.json");
        if (!File.Exists(descriptorPath))
            throw new InvalidOperationException("MODEL_SERVICE_NOT_RUNNING");

        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(File.ReadAllText(descriptorPath));
        }
        catch (JsonException ex)
        {
            throw new InvalidOperationException("MODEL_SERVICE_DESCRIPTOR_INVALID", ex);
        }

        var root = doc.RootElement;
        var schema = root.TryGetProperty("schema", out var s) ? s.GetString() : null;
        if (schema != DescriptorSchema)
            throw new InvalidOperationException("MODEL_SERVICE_DESCRIPTOR_SCHEMA_MISMATCH");
        var port = root.TryGetProperty("port", out var p) ? p.GetInt32() : 0;
        if (port < 1 || port > 65535)
            throw new InvalidOperationException("MODEL_SERVICE_PORT_INVALID");
        var pid = root.TryGetProperty("pid", out var pidEl) ? pidEl.GetInt32() : 0;
        var tokenFile = root.TryGetProperty("token_file", out var tf) ? tf.GetString() : null;
        if (string.IsNullOrWhiteSpace(tokenFile) || tokenFile.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
            || tokenFile.Contains(Path.DirectorySeparatorChar) || tokenFile.Contains(Path.AltDirectorySeparatorChar))
            throw new InvalidOperationException("MODEL_SERVICE_TOKEN_FILE_INVALID");

        var tokenPath = Path.Combine(ipcDir, tokenFile);
        string token;
        try
        {
            token = File.ReadAllText(tokenPath).Trim();
        }
        catch (IOException ex)
        {
            throw new InvalidOperationException("MODEL_SERVICE_TOKEN_UNAVAILABLE", ex);
        }
        if (string.IsNullOrEmpty(token))
            throw new InvalidOperationException("MODEL_SERVICE_TOKEN_UNAVAILABLE");

        var lifecycleOwner = root.TryGetProperty("lifecycle_owner", out var owner)
            ? owner.GetString()
            : null;
        var consumerPolicy = root.TryGetProperty("consumer_policy", out var policy)
            ? policy.GetString()
            : null;
        if (lifecycleOwner != "local-model/channel_runtime.py")
            throw new InvalidOperationException("MODEL_SERVICE_LIFECYCLE_OWNER_MISMATCH");
        if (consumerPolicy != "csharp-orchestrator-client-only")
            throw new InvalidOperationException("MODEL_SERVICE_CONSUMER_POLICY_MISMATCH");

        return new ModelServiceEndpoint(
            $"http://127.0.0.1:{port}", token, pid, tokenFile,
            lifecycleOwner, consumerPolicy);
    }

    // 建立已驗證的模型用戶端；服務未運行時 fail-closed
    public static Application.HttpModelClient CreateClient(string toolRoot, TimeSpan? timeout = null)
    {
        var endpoint = Discover(toolRoot);
        return new Application.HttpModelClient(endpoint.Endpoint, sessionToken: endpoint.SessionToken, timeout: timeout);
    }
}
