using System.Net.Http.Json;
using System.Text.Json;

namespace StarBusinessLogic.Application;

// 真實 HTTP 客戶端：呼叫 Python 的推論服務（如 StarTransformerRuntime / local-model 的 HTTP 端口）
// Python 端需暴露 POST /v1/infer {prompt, context, intent, max_new_tokens, temperature, top_k, top_p}
public sealed class HttpModelClient : IModelClient
{
    private readonly HttpClient _http;
    private readonly string _endpoint;
    private static readonly JsonSerializerOptions JsonOpts = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

    public HttpModelClient(HttpClient http, string endpoint)
    {
        _http = http ?? throw new ArgumentNullException(nameof(http));
        _endpoint = endpoint?.TrimEnd('/') ?? throw new ArgumentNullException(nameof(endpoint));
    }

    public async Task<ModelInferenceResponse> InferAsync(ModelInferenceRequest request, CancellationToken cancellationToken = default)
    {
        // 正確性：輸入驗證，fail-closed
        if (request == null) throw new ArgumentNullException(nameof(request));
        if (string.IsNullOrWhiteSpace(request.Prompt)) throw new ArgumentException("PROMPT_REQUIRED");
        if (!IsLoopback(_endpoint))
            throw new InvalidOperationException("MODEL_ENDPOINT_MUST_BE_LOOPBACK");

        var payload = new
        {
            prompt = request.Prompt,
            context = request.Context ?? string.Empty,
            intent = request.Intent ?? "Conversation",
            max_new_tokens = Math.Clamp(request.MaxNewTokens, 1, 2048),
            temperature = Math.Clamp(request.Temperature, 0.0, 2.0),
            top_k = Math.Clamp(request.TopK, 0, 100),
            top_p = Math.Clamp(request.TopP, 0.0, 1.0),
            extra = request.Extra
        };

        // 速度：單次請求超時 15s（對應 Python 的 inference 預期），避免無限等待
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(TimeSpan.FromSeconds(15));
        var sw = System.Diagnostics.Stopwatch.StartNew();
        try
        {
            using var resp = await _http.PostAsJsonAsync($"{_endpoint}/v1/infer", payload, JsonOpts, cts.Token).ConfigureAwait(false);
            resp.EnsureSuccessStatusCode();
            var doc = await resp.Content.ReadFromJsonAsync<JsonElement>(cancellationToken: cts.Token).ConfigureAwait(false);

            // 正確性：欄位缺失時回退而非拋例外
            string text = doc.TryGetProperty("text", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString() ?? string.Empty : string.Empty;
            var tokens = new List<int>();
            if (doc.TryGetProperty("token_ids", out var ti) && ti.ValueKind == JsonValueKind.Array)
            {
                foreach (var e in ti.EnumerateArray())
                {
                    if (e.TryGetInt32(out var v)) tokens.Add(v);
                }
            }
            string modelId = doc.TryGetProperty("model_id", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() ?? "native_transformer" : "native_transformer";
            double latency = doc.TryGetProperty("latency_ms", out var l) && l.TryGetDouble(out var d) ? d : sw.Elapsed.TotalMilliseconds;
            return new ModelInferenceResponse(text, tokens, modelId, latency);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex) when (ex is TaskCanceledException or TimeoutException or HttpRequestException)
        {
            // 正確性：網路/超時 fail-closed，回傳錯誤而非偽造成功
            throw new InvalidOperationException($"MODEL_INFERENCE_FAILED: {ex.Message}", ex);
        }
    }

    private static bool IsLoopback(string endpoint)
    {
        return endpoint.Contains("127.0.0.1") || endpoint.Contains("localhost") || endpoint.Contains("[::1]");
    }
}
