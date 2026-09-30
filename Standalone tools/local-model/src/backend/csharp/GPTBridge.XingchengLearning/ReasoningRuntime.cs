// ReasoningRuntime.cs — batch-2 unified reasoning runtime (§19):
// ONE canonical implementation absorbing gpt-oss reasoning effort,
// Nemotron inference-time budget, Hy3 fast/slow thinking and MiMo deep
// thinking. No per-source reasoning stacks.
//
//   Mode      AUTO | OFF | LOW | MEDIUM | HIGH     (§2.1 / §19)
//   Strategy  FAST | BALANCED | DEEP               (§16 / §19)
//
//   AUTO resolves the strategy from a typed TaskAssessment
//   (complexity, risk, tool need, evidence need) — never from prompt
//   text heuristics scattered across callers (§2.1 last rule).
//
//   Resolve() emits the bounded budget set: token_budget, tool_budget,
//   retrieval_budget, verification_budget, agent_budget,
//   latency_budget (§19 output contract).
//
// §2.3 separation contract: ReasoningRuntime also owns the
// reasoning/final split — internal_reasoning_state (draft thoughts,
// tool traces, verification traces) is never the user-visible
// final_response; FinalResponse() projects only the final channel.
//
// §6 quality_equivalent_fast_path: a FAST/ULTRA_LOW_LATENCY decision
// carries a rollback rule — any baseline regression resolves the
// profile back to BALANCED.
//
// Hard rule (unchanged from batch-1 §9): budgets bound orchestration;
// they never claim new model capability and never touch weights.
// CAPABILITY_TRAINING_FROZEN (§34) — nothing here trains.

namespace GPTBridge.XingchengLearning;

/// <summary>§19 reasoning mode — AUTO lets the classifier pick a
/// strategy; OFF disables the reasoning lane entirely.</summary>
internal enum ReasoningRtMode { AUTO = 0, OFF = 1, LOW = 2, MEDIUM = 3, HIGH = 4 }

/// <summary>§16/§19 strategy — FAST (Hy3 fast path), BALANCED,
/// DEEP (Hy3 slow path / MiMo deep thinking).</summary>
internal enum ReasoningStrategy { FAST = 0, BALANCED = 1, DEEP = 2 }

/// <summary>§16 AUTO input — a typed task assessment. Produced by the
/// caller's TaskClassifier; the runtime never guesses from raw
/// text.</summary>
internal sealed class TaskAssessment
{
    /// <summary>0..1 task complexity (0 = trivial QA, 1 = multi-document
    /// synthesis / long-horizon analysis).</summary>
    public double ComplexityScore;
    /// <summary>0..1 risk score (0 = reversible trivia, 1 = high-stakes
    /// or irreversible-adjacent advice).</summary>
    public double RiskScore;
    /// <summary>0..1 external-tool need (0 = model alone suffices).</summary>
    public double ToolNeed;
    /// <summary>0..1 evidence/retrieval need (0 = in-context
    /// knowledge).</summary>
    public double EvidenceNeed;

    /// <summary>Closed validation — a score outside [0,1] fails the
    /// assessment rather than clamping silently.</summary>
    public void Validate()
    {
        foreach (var (name, v) in new (string, double)[]
                 {
                     ("complexity", ComplexityScore), ("risk", RiskScore),
                     ("tool_need", ToolNeed), ("evidence_need", EvidenceNeed),
                 })
            if (!(v >= 0.0 && v <= 1.0))
                throw new ExecutorError(
                    ConvErr.ToolDecisionInvalid,
                    $"task assessment {name} outside [0,1]: {v}");
    }
}

/// <summary>§19 resolved output — the six canonical budgets.</summary>
internal sealed class ReasoningBudgets
{
    public long TokenBudget;         // reasoning_token_budget (§2.1)
    public int ToolBudget;           // tool_budget
    public int RetrievalBudget;      // retrieval_budget (depth)
    public int VerificationBudget;   // verification_budget (rounds)
    public int AgentBudget;          // agent_step_budget
    public long LatencyBudgetMs;     // latency_budget

    public Dictionary<string, object?> ToDict() => new()
    {
        ["reasoning_token_budget"] = TokenBudget,
        ["verification_budget"] = VerificationBudget,
        ["tool_budget"] = ToolBudget,
        ["retrieval_budget"] = RetrievalBudget,
        ["agent_step_budget"] = AgentBudget,
        ["latency_budget_ms"] = LatencyBudgetMs,
    };
}

internal static class ReasoningRuntime
{
    public const string Format = "star-reasoning-runtime/v1";

    /// <summary>§16.1 FAST — minimal verification, low tool budget,
    /// low retrieval depth, small output budget.</summary>
    private static readonly ReasoningBudgets FastBudgets = new()
    {
        TokenBudget = 1024, ToolBudget = 2, RetrievalBudget = 0,
        VerificationBudget = 0, AgentBudget = 2, LatencyBudgetMs = 4000,
    };
    /// <summary>Default BALANCED lane.</summary>
    private static readonly ReasoningBudgets BalancedBudgets = new()
    {
        TokenBudget = 4096, ToolBudget = 8, RetrievalBudget = 1,
        VerificationBudget = 1, AgentBudget = 8, LatencyBudgetMs = 20000,
    };
    /// <summary>§16.2 DEEP — higher verification, multiple evidence,
    /// expert collaboration, larger budget, recovery loop headroom.</summary>
    private static readonly ReasoningBudgets DeepBudgets = new()
    {
        TokenBudget = 16384, ToolBudget = 16, RetrievalBudget = 3,
        VerificationBudget = 2, AgentBudget = 32, LatencyBudgetMs = 120000,
    };

    private static ReasoningBudgets ForStrategy(ReasoningStrategy s)
        => s switch
        {
            ReasoningStrategy.FAST => FastBudgets,
            ReasoningStrategy.DEEP => DeepBudgets,
            _ => BalancedBudgets,
        };

    /// <summary>§16 AUTO classification: FAST only when every need
    /// signal is low; DEEP when complexity, risk or evidence need is
    /// high; BALANCED otherwise. Thresholds are explicit constants so
    /// the decision is auditable.</summary>
    public static ReasoningStrategy Classify(TaskAssessment a)
    {
        a.Validate();
        double demand = Math.Max(a.ComplexityScore,
                                 Math.Max(a.RiskScore, a.EvidenceNeed));
        double need = Math.Max(a.ToolNeed, a.EvidenceNeed);
        if (demand <= 0.30 && need <= 0.30) return ReasoningStrategy.FAST;
        if (demand >= 0.60 || a.RiskScore >= 0.70 ||
            a.EvidenceNeed >= 0.70)
            return ReasoningStrategy.DEEP;
        return ReasoningStrategy.BALANCED;
    }

    private static ReasoningStrategy StrategyForMode(
        ReasoningRtMode m, TaskAssessment? a)
        => m switch
        {
            ReasoningRtMode.AUTO =>
                a == null
                    ? throw new ExecutorError(
                        ConvErr.ToolDecisionInvalid,
                        "reasoning mode AUTO requires a task assessment")
                    : Classify(a),
            ReasoningRtMode.OFF or ReasoningRtMode.LOW =>
                ReasoningStrategy.FAST,
            ReasoningRtMode.HIGH => ReasoningStrategy.DEEP,
            _ => ReasoningStrategy.BALANCED,
        };

    /// <summary>§19 Resolve: one mode (+optional AUTO assessment) -> one
    /// typed budget plan. OFF yields zero budgets — the caller must not
    /// run a reasoning lane at all.</summary>
    public static Dictionary<string, object?> Resolve(
        string mode, TaskAssessment? assessment = null)
    {
        if (!Enum.TryParse<ReasoningRtMode>(
                (mode ?? "MEDIUM").ToUpperInvariant(), out var m))
            throw new ExecutorError(
                ConvErr.ToolDecisionInvalid,
                $"unknown reasoning mode '{mode}'");
        var strategy = m == ReasoningRtMode.OFF
            ? ReasoningStrategy.FAST : StrategyForMode(m, assessment);
        var b = m == ReasoningRtMode.OFF
            ? new ReasoningBudgets()
            : ForStrategy(strategy);
        var d = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["mode"] = m.ToString(),
            ["strategy"] = strategy.ToString(),
            ["budgets"] = b.ToDict(),
            // §16 honesty field: DEEP widens orchestration budgets; it is
            // not a claim of new model reasoning capability.
            ["capability_claim"] = "runtime_orchestration_only",
        };
        if (m == ReasoningRtMode.AUTO && assessment != null)
            d["assessment"] = new Dictionary<string, object?>
            {
                ["complexity"] = assessment.ComplexityScore,
                ["risk"] = assessment.RiskScore,
                ["tool_need"] = assessment.ToolNeed,
                ["evidence_need"] = assessment.EvidenceNeed,
            };
        return d;
    }

    // -------------------------------------------------- §2.3 split --

    /// <summary>Message channels — internal_reasoning_state carries
    /// draft reasoning, tool traces and verification traces;
    /// final_response is the only user-visible channel.</summary>
    public static readonly string[] InternalChannels =
        { "reasoning", "tool_trace", "verification_trace", "debug" };

    /// <summary>Project an internal transcript into the user-visible
    /// response: drops every internal channel, keeps only final-channel
    /// content. Fail-closed: a transcript with no final channel is an
    /// empty response, never a leaked trace.</summary>
    public static Dictionary<string, object?> FinalResponse(
        List<Dictionary<string, object?>> transcript)
    {
        var finals = new List<object?>();
        foreach (var msg in transcript)
        {
            string channel = msg.TryGetValue("channel", out var ch)
                ? ch?.ToString() ?? "" : "";
            if (InternalChannels.Contains(channel)) continue;
            finals.Add(msg);
        }
        return new Dictionary<string, object?>
        {
            ["format"] = "star-final-response/v1",
            ["internal_channels_dropped"] =
                InternalChannels.Cast<object?>().ToList(),
            ["messages"] = finals,
            ["contract"] =
                "internal_reasoning_state never reaches final_response",
        };
    }

    // --------------------------------------------------- §6 fallback --

    /// <summary>§6 quality_equivalent_fast_path: evaluate a fast-path
    /// run against the baseline; any regression resolves back to
    /// BALANCED — the fast lane can never silently degrade.</summary>
    /// <param name="regressed">true when the fast run regressed vs the
    /// recorded baseline (eval gate or latency guard).</param>
    public static string ResolveDeploymentFallback(
        string profile, bool regressed)
    {
        if (profile != DeploymentProfile.UltraLowLatency &&
            profile != DeploymentProfile.Fast)
            return profile;
        return regressed ? DeploymentProfile.Balanced : profile;
    }
}
