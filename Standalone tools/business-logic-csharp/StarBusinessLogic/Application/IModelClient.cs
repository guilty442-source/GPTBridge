namespace StarBusinessLogic.Application;

// 模型推論由 C++23 原生引擎負責（xingcheng_trainer / xc_modeltool serve），C# 經此介面呼叫
// 實作可為 HttpClient（呼叫 local-model loopback inference 端點）或 NativeModelClient（同行程 C ABI）
public sealed record ModelInferenceRequest(
    string Prompt,
    string Context,
    string Intent,
    int MaxNewTokens = 512,
    double Temperature = 0.7,
    int TopK = 50,
    double TopP = 0.9,
    IReadOnlyDictionary<string, object>? Extra = null
);

public sealed record ModelInferenceResponse(
    string Text,
    IReadOnlyList<int> TokenIds,
    string ModelId,
    double LatencyMs,
    IReadOnlyDictionary<string, object>? Metadata = null
);

public interface IModelClient
{
    Task<ModelInferenceResponse> InferAsync(ModelInferenceRequest request, CancellationToken cancellationToken = default);
}

// 測試/離線用假實作，正式環境替換為 HttpModelClient
public sealed class FakeModelClient : IModelClient
{
    public Task<ModelInferenceResponse> InferAsync(ModelInferenceRequest request, CancellationToken cancellationToken = default)
    {
        // 不直接生成權重文字，僅回傳可驗證的占位，證明 C# 業務已準備好 Context
        var text = $"[C# Business] Intent={request.Intent} Prompt=\"{request.Prompt}\" ContextLen={request.Context?.Length ?? 0} -> awaiting native C++ engine";
        return Task.FromResult(new ModelInferenceResponse(text, Array.Empty<int>(), "fake-native", 1.0));
    }
}
