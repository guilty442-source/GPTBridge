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

    // P11/MS6 受管傳輸選擇：runtime/settings/native-engine.json 的
    // `csharp_transport` 決定編排層走哪條路——
    //   "http"（缺省，現行行為）：loopback HTTP + session token（Python 中介）
    //   "native-abi"：同行程 C ABI（NativeModelClient），Python 僅留治理語意
    // 未知值 / 映像或 bundle 缺失一律 fail-closed。傳輸切換不碰裁決／權限／
    // 稽核鏈（皆在上游 GovernedIpcClient），也不改變 shadow→parity→primary
    // 順序——native-abi 僅在設定顯式 pin 時啟用。
    public static Application.IModelClient CreateModelClient(string toolRoot, TimeSpan? timeout = null)
    {
        if (string.IsNullOrWhiteSpace(toolRoot)) throw new ArgumentException("TOOL_ROOT_REQUIRED", nameof(toolRoot));
        var transport = ReadTransport(toolRoot);
        switch (transport)
        {
            case "http":
                return CreateClient(toolRoot, timeout);
            case "native-abi":
                return new Application.NativeModelClient(
                    ResolveEngineImage(toolRoot), ResolveBundleDir(toolRoot),
                    toolRoot: toolRoot);
            default:
                throw new InvalidOperationException($"MODEL_TRANSPORT_UNKNOWN:{transport}");
        }
    }

    private static string ReadTransport(string toolRoot)
    {
        var settingsPath = Path.Combine(toolRoot, "runtime", "settings", "native-engine.json");
        if (!File.Exists(settingsPath)) return "http";
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(settingsPath));
            return doc.RootElement.TryGetProperty("csharp_transport", out var t)
                ? t.GetString() ?? "http"
                : "http";
        }
        catch (JsonException ex)
        {
            throw new InvalidOperationException("MODEL_TRANSPORT_SETTINGS_INVALID", ex);
        }
    }

    // dist-native 下最新的版本化引擎映像（.pyd 即 DLL；xc_engine_* 與
    // pybind11 模組共用同一映像）。無映像 → fail-closed。
    private static string ResolveEngineImage(string toolRoot)
    {
        var distDir = Path.Combine(toolRoot, "dist-native");
        if (!Directory.Exists(distDir))
            throw new InvalidOperationException("XC_ENGINE_IMAGE_MISSING");
        var candidates = Directory.GetFiles(distDir, "_xingcheng_inference*.pyd");
        var image = candidates.OrderByDescending(File.GetLastWriteTimeUtc).FirstOrDefault();
        if (image is null) throw new InvalidOperationException("XC_ENGINE_IMAGE_MISSING");
        return image;
    }

    // bundle 有效性契約與 Python cpp_runtime._bundle_matches_source 相同：
    // schema、source_checkpoint 解析後路徑一致、size/mtime 吻合、weights 檔存在。
    // bundle 只由受管 export 管線產生——此處純消費，缺合法 bundle 即 fail-closed。
    private static string ResolveBundleDir(string toolRoot)
    {
        var settingsPath = Path.Combine(toolRoot, "runtime", "settings", "native-engine.json");
        string? checkpoint = null;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(settingsPath));
            checkpoint = doc.RootElement.TryGetProperty("checkpoint", out var c) ? c.GetString() : null;
        }
        catch (JsonException ex)
        {
            throw new InvalidOperationException("MODEL_TRANSPORT_SETTINGS_INVALID", ex);
        }
        if (string.IsNullOrWhiteSpace(checkpoint))
            throw new InvalidOperationException("XC_BUNDLE_CHECKPOINT_UNPINNED");
        var checkpointPath = Path.GetFullPath(Path.IsPathRooted(checkpoint)
            ? checkpoint
            : Path.Combine(toolRoot, checkpoint));
        FileInfo checkpointInfo;
        try
        {
            checkpointInfo = new FileInfo(checkpointPath);
            if (!checkpointInfo.Exists) throw new InvalidOperationException("XC_BUNDLE_CHECKPOINT_MISSING");
        }
        catch (IOException ex)
        {
            throw new InvalidOperationException("XC_BUNDLE_CHECKPOINT_MISSING", ex);
        }
        // Windows FileInfo 無 st_mtime_ns——bundle 由本機 export 產生，
        // 以 size ＋ resolved path 比對，mtime 由 manifest 供應端自證。
        var bundlesDir = Path.Combine(toolRoot, "xingcheng", "runtime", "models", "cpp-bundles");
        if (!Directory.Exists(bundlesDir))
            throw new InvalidOperationException("XC_BUNDLE_MISSING");
        foreach (var dir in Directory.GetDirectories(bundlesDir))
        {
            var manifestPath = Path.Combine(dir, "manifest.json");
            if (!File.Exists(manifestPath)) continue;
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(manifestPath));
                var root = doc.RootElement;
                if (root.TryGetProperty("schema_version", out var sv) is false
                    || sv.GetString() != "star-native-inference-bundle/v1") continue;
                var source = root.TryGetProperty("source_checkpoint", out var sc) ? sc.GetString() : null;
                if (string.IsNullOrWhiteSpace(source)
                    || !string.Equals(Path.GetFullPath(source), checkpointPath, StringComparison.OrdinalIgnoreCase)) continue;
                var size = root.TryGetProperty("source_size", out var sz) ? sz.GetInt64() : -1;
                if (size != checkpointInfo.Length) continue;
                var weights = root.TryGetProperty("weights_file", out var wf) ? wf.GetString() : null;
                if (string.IsNullOrWhiteSpace(weights) || !File.Exists(Path.Combine(dir, weights))) continue;
                return dir;
            }
            catch (JsonException)
            {
                continue;
            }
        }
        throw new InvalidOperationException("XC_BUNDLE_MISSING");
    }
}
