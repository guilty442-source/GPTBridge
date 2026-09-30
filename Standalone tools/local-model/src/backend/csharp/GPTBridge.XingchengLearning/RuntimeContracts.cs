// RuntimeContracts.cs — batch-2 control-plane contracts that the
// unified runtimes compose:
//
//   RequestBudgetLedger   §22/§20 — one token/budget ledger shared by
//                          dialogue, retrieval, tools and agent steps.
//   TaskResumeState        agent resume — persisted fields + the six
//                          fail-closed resume checks.
//   UnifiedMetrics         §36 — the closed metric-name vocabulary;
//                          no module invents duplicate metrics.
//   CapabilityFreeze       §34 — guard invoked at execution boundaries.
//   PostTrainingPriority   §17 — Hy3 post-training-first evolution
//                          order for future self-learning cycles.

namespace GPTBridge.XingchengLearning;

// ------------------------------------------------ §36 unified metrics

/// <summary>§36 unified metric names — the only names runtime
/// components may emit. New needs go through this table, not local
/// renames.</summary>
internal static class UnifiedMetrics
{
    public static readonly string[] Names =
    {
        "TTFT", "TPOT", "TPS",
        "prefill_tps", "decode_tps",
        "cache_hit_rate", "prefix_hit_rate",
        "kv_bytes", "recurrent_bytes", "workspace_bytes", "weight_bytes",
        "tool_latency", "retrieval_latency",
        "agent_steps", "reasoning_budget_used",
        "vision_patch_count", "context_blocks_selected",
        "speculative_acceptance_rate",
    };

    /// <summary>Reject a metrics dict carrying names outside the unified
    /// vocabulary — silently-renamed duplicates are how observability
    /// rots.</summary>
    public static void Validate(Dictionary<string, object?> metrics)
    {
        var foreign = metrics.Keys
            .Where(k => !Names.Contains(k)).ToList();
        if (foreign.Count > 0)
            throw new ExecutorError(
                ConvErr.StructuredSchemaFailed,
                $"metrics outside unified schema: {string.Join(",", foreign)}");
    }
}

// -------------------------------------------------- context budget --

/// <summary>One budget ledger for a request — every consumer (context
/// tokens, retrieval depth, tool calls, agent steps, reasoning tokens,
/// latency) draws from it; exhaustion is a typed state, not an
/// exception mid-flight.</summary>
internal sealed class RequestBudgetLedger
{
    public long ContextTokens;
    public int RetrievalDepth;
    public int ToolCalls;
    public int AgentSteps;
    public long ReasoningTokens;
    public long LatencyMs;

    private readonly DateTimeOffset _start = DateTimeOffset.UtcNow;
    private long _usedContext;
    private int _usedRetrieval, _usedTools, _usedAgent;
    private long _usedReasoning;

    /// <summary>Construct from a resolved ReasoningBudgets plan plus
    /// the runtime context cap.</summary>
    public static RequestBudgetLedger From(
        ReasoningBudgets b, long contextTokens)
        => new()
        {
            ContextTokens = contextTokens,
            RetrievalDepth = b.RetrievalBudget,
            ToolCalls = b.ToolBudget,
            AgentSteps = b.AgentBudget,
            ReasoningTokens = b.TokenBudget,
            LatencyMs = b.LatencyBudgetMs,
        };

    /// <summary>Spend; returns false (budget_exhausted semantics)
    /// instead of throwing so callers can route to the exhausted
    /// state.</summary>
    public bool SpendContext(long tokens)
    {
        if (_usedContext + tokens > ContextTokens) return false;
        _usedContext += tokens; return true;
    }
    public bool SpendRetrieval()
    {
        if (_usedRetrieval >= RetrievalDepth) return false;
        _usedRetrieval++; return true;
    }
    public bool SpendTool()
    {
        if (_usedTools >= ToolCalls) return false;
        _usedTools++; return true;
    }
    public bool SpendAgentStep()
    {
        if (_usedAgent >= AgentSteps) return false;
        _usedAgent++; return true;
    }
    public bool SpendReasoning(long tokens)
    {
        if (_usedReasoning + tokens > ReasoningTokens) return false;
        _usedReasoning += tokens; return true;
    }
    public bool LatencyExhausted =>
        (DateTimeOffset.UtcNow - _start).TotalMilliseconds > LatencyMs;

    /// <summary>Remaining budget snapshot — persisted into
    /// TaskResumeState so a resumed task keeps its spent ledger.</summary>
    public Dictionary<string, object?> Remaining() => new()
    {
        ["context_tokens"] = ContextTokens - _usedContext,
        ["retrieval_depth"] = RetrievalDepth - _usedRetrieval,
        ["tool_calls"] = ToolCalls - _usedTools,
        ["agent_steps"] = AgentSteps - _usedAgent,
        ["reasoning_tokens"] = ReasoningTokens - _usedReasoning,
        ["latency_ms"] = LatencyMs -
            (long)(DateTimeOffset.UtcNow - _start).TotalMilliseconds,
    };
}

// ------------------------------------------------------ task resume --

/// <summary>Required task-state vocabulary — a task outside this set
/// cannot be persisted or resumed.</summary>
internal static class TaskStates
{
    public static readonly string[] Names =
    {
        "queued", "running", "waiting_tool", "waiting_retrieval",
        "waiting_user", "verifying", "completed", "failed",
        "cancelled", "budget_exhausted",
    };
}

/// <summary>Persisted task resume record — the fields are normative;
/// a record missing any of them fails validation closed.</summary>
internal sealed class TaskResumeState
{
    public const string Format = "star-task-resume/v1";

    public string TaskId = "";
    public string GenerationId = "";
    public string BundleId = "";
    public string RequestHash = "";
    public string StateHash = "";
    public string NextStep = "";
    public Dictionary<string, object?>? PendingToolCall;
    public Dictionary<string, object?>? PendingRetrieval;
    public Dictionary<string, object?> BudgetRemaining = new();
    public string State = "queued";       // TaskStates vocab
    public long CreatedAtUnix;
    public long UpdatedAtUnix;

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = Format,
        ["task_id"] = TaskId,
        ["generation_id"] = GenerationId,
        ["bundle_id"] = BundleId,
        ["request_hash"] = RequestHash,
        ["state_hash"] = StateHash,
        ["next_step"] = NextStep,
        ["pending_tool_call"] = PendingToolCall,
        ["pending_retrieval"] = PendingRetrieval,
        ["budget_remaining"] = BudgetRemaining,
        ["state"] = State,
        ["created_at"] = CreatedAtUnix,
        ["updated_at"] = UpdatedAtUnix,
    };

    /// <summary>The six fail-closed resume checks (agent-resume
    /// contract): generation match, bundle match, state-hash validity,
    /// request-hash validity, policy compatibility, pending-call
    /// freshness. Any failure throws; there is no best-effort resume.</summary>
    public void ValidateResume(
        string activeGeneration, string activeBundle,
        string recomputedStateHash, string recomputedRequestHash,
        string policyHash, string persistedPolicyHash,
        long maxAgeS = 86400)
    {
        if (GenerationId != activeGeneration)
            throw new ExecutorError(ConvErr.StateGenerationMismatch,
                $"resume generation {GenerationId} != {activeGeneration}");
        if (BundleId != activeBundle)
            throw new ExecutorError(ConvErr.StateModelMismatch,
                $"resume bundle {BundleId} != {activeBundle}");
        if (StateHash.Length == 0 || StateHash != recomputedStateHash)
            throw new ExecutorError(ConvErr.StateModelMismatch,
                "resume state hash invalid");
        if (RequestHash.Length == 0 || RequestHash != recomputedRequestHash)
            throw new ExecutorError(ConvErr.ToolDecisionInvalid,
                "resume request hash invalid");
        if (persistedPolicyHash != policyHash)
            throw new ExecutorError(ConvErr.ToolDecisionInvalid,
                "policy changed incompatibly since checkpoint");
        if (!TaskStates.Names.Contains(State))
            throw new ExecutorError(ConvErr.StructuredSchemaFailed,
                $"unknown task state '{State}'");
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if (PendingToolCall != null)
        {
            if (State != "waiting_tool")
                throw new ExecutorError(ConvErr.ToolDecisionInvalid,
                    "pending tool call on non-waiting_tool state");
            if (now - UpdatedAtUnix > maxAgeS)
                throw new ExecutorError(ConvErr.ToolDecisionInvalid,
                    "pending tool call stale");
        }
    }
}

// ------------------------------------------------- §17 evolution order

/// <summary>§17 post-training-first priority for future self-learning
/// cycles — quality layers before post-training, backbone last.
/// Merged with the existing GLM/Phi/Storm integration principle: a
/// backbone change is only considered when every upstream layer is
/// exhausted.</summary>
internal static class PostTrainingPriority
{
    public static readonly string[] Order =
    {
        "data_quality",
        "instruction_quality",
        "agent_trace_quality",
        "reasoning_example_quality",
        "post_training",
        "backbone_change",       // last resort only
    };
}
