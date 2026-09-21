using StarBusinessLogic.Domain;

namespace StarBusinessLogic.Application;

// 核心編排器：串起 Intent → Plan → Tool → Grounding → Context → Model 推論
// 訓練仍以 Python+PyTorch 為主，推論允許 Python/C++ 雙路徑，此層為 C# 業務編排
public sealed record OrchestratorRequest(
    string Prompt,
    string? Context = null,
    int MaxNewTokens = 256,
    double Temperature = 0.7
);

public sealed record OrchestratorResponse(
    string Text,
    StarIntent Intent,
    IReadOnlyList<ToolCallResult> ToolResults,
    IReadOnlyList<Evidence> Evidences,
    string ModelId,
    bool UsedGrounding
);

public sealed class StarOrchestrator
{
    private readonly IIntentClassifier _intent;
    private readonly IPlanBuilder _plan;
    private readonly IToolRouter _router;
    private readonly IGroundingService _grounding;
    private readonly IContextBuilder _contextBuilder;
    private readonly IModelClient _model;

    public StarOrchestrator(
        IIntentClassifier intent,
        IPlanBuilder plan,
        IToolRouter router,
        IGroundingService grounding,
        IContextBuilder contextBuilder,
        IModelClient model)
    {
        _intent = intent;
        _plan = plan;
        _router = router;
        _grounding = grounding;
        _contextBuilder = contextBuilder;
        _model = model;
    }

    public async Task<OrchestratorResponse> ExecuteAsync(OrchestratorRequest request, CancellationToken cancellationToken = default)
    {
        // 正確性：輸入驗證
        if (request == null) throw new ArgumentNullException(nameof(request));
        if (string.IsNullOrWhiteSpace(request.Prompt)) throw new ArgumentException("PROMPT_REQUIRED");

        // 1. Intent（快取加速）
        var intent = _intent.Classify(request.Prompt);

        // 2. Plan（正確性：context 可能為 null，速度：避免重複分配）
        var plan = _plan.Build(intent, request.Context ?? string.Empty);

        // 3. Tool（速度：無工具時直接跳過，避免空任務排程；正確性：單工具失敗不影響整體）
        IReadOnlyList<ToolCallResult> toolResults;
        if (plan.RequiredTools.Count == 0)
            toolResults = Array.Empty<ToolCallResult>();
        else
            toolResults = await _router.RouteAsync(plan, cancellationToken).ConfigureAwait(false);

        // 4. Grounding（正確性：僅取成功結果，空結果時仍正確標記是否需 grounding）
        // 速度：預分配 List，單次遍歷
        var evidences = new List<Evidence>(toolResults.Count);
        foreach (var r in toolResults)
        {
            if (r.Success && !string.IsNullOrWhiteSpace(r.Content))
                evidences.Add(new Evidence(r.ToolId, r.Content, r.ContentHash, 0.8, r.ToolId));
        }
        var grounding = _grounding.Ground(plan, evidences);

        // 5. Context（速度：無證據時回傳空字串，避免多餘 Join）
        var contextForModel = grounding.Evidences.Count == 0 ? string.Empty : _contextBuilder.Build(plan, grounding);

        // 6. 推論（Python 模型，C# 不直接持有權重；正確性：模型失敗不偽造成功，速度：直接透傳 cancellation）
        var inferReq = new ModelInferenceRequest(
            Prompt: plan.GenerationPrompt,
            Context: contextForModel,
            Intent: plan.Intent.ToString(),
            MaxNewTokens: Math.Clamp(request.MaxNewTokens, 1, 2048),
            Temperature: Math.Clamp(request.Temperature, 0.0, 2.0)
        );
        var inferResp = await _model.InferAsync(inferReq, cancellationToken).ConfigureAwait(false);

        return new OrchestratorResponse(
            inferResp.Text,
            plan.Intent,
            toolResults,
            grounding.Evidences,
            inferResp.ModelId,
            grounding.HasSufficientEvidence && grounding.Evidences.Count > 0
        );
    }
}
