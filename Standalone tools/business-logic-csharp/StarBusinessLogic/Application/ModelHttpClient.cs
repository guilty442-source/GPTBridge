using System.Net.Http.Json;
using System.Text.Json;

namespace StarBusinessLogic.Application;

// 對接 Python 模型服務端點（star-model-service/v1）：
//   POST /v1/infer    — governed 推論（Python 引擎或 XINGCHENG_CPP_RUNTIME 路由的 C++ 執行層）
//   GET  /v1/status   — 控制面狀態
//   POST /v1/release  — 顯式 auto-release（對應 Python AutoReleaseManager）
// 僅允許 loopback 端點；session token 與 Python IPC 信任邊界一致（X-GPTBridge-Session-Token）。
// 速度：連線重用 + 有界超時；正確性：契約失敗 fail-closed（不產生偽造回應）。
public sealed class HttpModelClient : IModelClient, IDisposable
{
    private readonly HttpClient _http;
    private readonly string _base;
    private readonly string? _sessionToken;

    public HttpModelClient(
        string endpoint,
        string? sessionToken = null,
        HttpMessageHandler? handler = null,
        TimeSpan? timeout = null)
    {
        if (!IsLoopback(endpoint)) throw new InvalidOperationException("MODEL_ENDPOINT_MUST_BE_LOOPBACK");
        _base = endpoint.TrimEnd('/');
        _sessionToken = sessionToken;
        _http = handler is null ? new HttpClient { BaseAddress = new Uri(_base) }
                                : new HttpClient(handler);
        _http.Timeout = timeout ?? TimeSpan.FromSeconds(15);
    }

    private HttpRequestMessage BuildRequest(HttpMethod method, string path, object? body = null)
    {
        var request = new HttpRequestMessage(method, _base + path);
        if (!string.IsNullOrEmpty(_sessionToken))
            request.Headers.Add("X-GPTBridge-Session-Token", _sessionToken);
        if (body is not null)
            request.Content = JsonContent.Create(body);
        return request;
    }

    public async Task<ModelInferenceResponse> InferAsync(ModelInferenceRequest request, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(request.Prompt)) throw new ArgumentException("PROMPT_REQUIRED", nameof(request));
        using var httpRequest = BuildRequest(HttpMethod.Post, "/v1/infer", new
        {
            prompt = request.Prompt,
            context = request.Context,
            intent = request.Intent,
            max_new_tokens = request.MaxNewTokens,
            temperature = request.Temperature,
            top_k = request.TopK,
            top_p = request.TopP,
            extra = request.Extra,
        });
        try
        {
            using var response = await _http.SendAsync(httpRequest, cancellationToken).ConfigureAwait(false);
            var doc = await response.Content.ReadFromJsonAsync<JsonDocument>(cancellationToken).ConfigureAwait(false);
            if (doc is null) throw new InvalidOperationException("MODEL_RESPONSE_INVALID");

            var root = doc.RootElement;
            if (root.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.False)
            {
                var code = root.TryGetProperty("error_code", out var ec) ? ec.GetString() ?? "INFERENCE_FAILED" : "INFERENCE_FAILED";
                throw new InvalidOperationException($"MODEL_INFERENCE_FAILED: {code}");
            }
            if (!response.IsSuccessStatusCode)
                throw new InvalidOperationException($"MODEL_HTTP_{(int)response.StatusCode}");

            var text = root.TryGetProperty("text", out var t) ? t.GetString() ?? string.Empty : string.Empty;
            var tokenIds = root.TryGetProperty("token_ids", out var ids) && ids.ValueKind == JsonValueKind.Array
                ? ids.EnumerateArray().Select(e => e.GetInt32()).ToArray()
                : Array.Empty<int>();
            var modelId = root.TryGetProperty("model_id", out var m) ? m.GetString() ?? "unknown" : "unknown";
            var latency = root.TryGetProperty("latency_ms", out var l) ? l.GetDouble() : 0.0;
            var metadata = new Dictionary<string, object>();
            if (root.TryGetProperty("decoder", out var d)) metadata["decoder"] = d.GetString() ?? string.Empty;
            if (root.TryGetProperty("cpp_runtime", out var cpp) && (cpp.ValueKind == JsonValueKind.True || cpp.ValueKind == JsonValueKind.False))
                metadata["cpp_runtime"] = cpp.GetBoolean();
            return new ModelInferenceResponse(text, tokenIds, modelId, latency, metadata);
        }
        catch (TaskCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new InvalidOperationException("MODEL_INFERENCE_TIMEOUT");
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw; // 上層取消向外傳遞
        }
        catch (HttpRequestException ex)
        {
            throw new InvalidOperationException($"MODEL_INFERENCE_FAILED: {ex.Message}", ex);
        }
    }

    // 控制面：引擎/旗標狀態（GET /v1/status）
    public async Task<IReadOnlyDictionary<string, object>> StatusAsync(CancellationToken cancellationToken = default)
    {
        using var httpRequest = BuildRequest(HttpMethod.Get, "/v1/status");
        using var response = await _http.SendAsync(httpRequest, cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException($"MODEL_HTTP_{(int)response.StatusCode}");
        var doc = await response.Content.ReadFromJsonAsync<JsonDocument>(cancellationToken).ConfigureAwait(false);
        return doc?.RootElement.EnumerateObject().ToDictionary(p => p.Name, p => (object)p.Value.Clone())
               ?? new Dictionary<string, object>();
    }

    // 釋放生命週期：顯式卸載 cached engine（POST /v1/release；key 為 null 時全部釋放）
    public async Task<IReadOnlyList<string>> ReleaseAsync(string? key = null, CancellationToken cancellationToken = default)
    {
        using var httpRequest = BuildRequest(HttpMethod.Post, "/v1/release", new { key });
        using var response = await _http.SendAsync(httpRequest, cancellationToken).ConfigureAwait(false);
        var doc = await response.Content.ReadFromJsonAsync<JsonDocument>(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode || doc is null)
            throw new InvalidOperationException($"MODEL_HTTP_{(int)response.StatusCode}");
        return doc.RootElement.TryGetProperty("released", out var r) && r.ValueKind == JsonValueKind.Array
            ? r.EnumerateArray().Select(e => e.GetString() ?? string.Empty).ToArray()
            : Array.Empty<string>();
    }

    private static bool IsLoopback(string endpoint)
    {
        if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri)) return false;
        if (uri.Scheme != Uri.UriSchemeHttp) return false;
        return uri.Host is "127.0.0.1" or "localhost" or "::1";
    }

    public void Dispose() => _http.Dispose();
}
