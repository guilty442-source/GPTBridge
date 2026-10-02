// InteractionModeRouter.cs — §19 Chat vs Agent routing.
//
// Hermes 4 leans toward Chat/Reasoning/Roleplay/Long-form Writing,
// not high-frequency agent tool loop. Modes: CHAT, CREATIVE,
// ROLEPLAY, RESEARCH, AGENT, CODING, DOCUMENT. Not all modes go
// through AgentWorkGraph. ROLEPLAY goes directly to PersonaRuntime
// + NarrativeMemory + NativeInference. AGENT goes to
// LongHorizonTaskCoordinator + ToolDecisionGate + AgentWorkGraph.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class InteractionModeRouter
{
    public const string Format = "star-interaction-mode/v1";

    public static readonly string[] Modes =
    {
        "CHAT", "CREATIVE", "ROLEPLAY", "RESEARCH",
        "AGENT", "CODING", "DOCUMENT",
    };

    public sealed class RouteResult
    {
        public string Mode = "CHAT";
        public bool UseAgentWorkGraph = false;
        public bool UsePersonaRuntime = false;
        public bool UseNarrativeMemory = false;
        public bool UseToolDecisionGate = false;
        public bool UseLongHorizonCoordinator = false;
        public string FactualityMode = "GENERAL";
        public string RagDecision = "RAG_NOT_REQUIRED";
    }

    public static RouteResult Resolve(string mode)
    {
        var result = new RouteResult { Mode = mode };
        switch (mode)
        {
            case "CHAT":
                result.FactualityMode = "GENERAL";
                result.RagDecision = "RAG_NOT_REQUIRED";
                break;
            case "CREATIVE":
                result.UsePersonaRuntime = true;
                result.FactualityMode = "FICTIONAL";
                result.RagDecision = "RAG_NOT_REQUIRED";
                break;
            case "ROLEPLAY":
                result.UsePersonaRuntime = true;
                result.UseNarrativeMemory = true;
                result.FactualityMode = "FICTIONAL";
                result.RagDecision = "RAG_NOT_REQUIRED";
                break;
            case "RESEARCH":
                result.FactualityMode = "GROUNDED";
                result.RagDecision = "RAG_REQUIRED";
                break;
            case "AGENT":
                result.UseAgentWorkGraph = true;
                result.UseToolDecisionGate = true;
                result.UseLongHorizonCoordinator = true;
                result.FactualityMode = "FACTUAL_STRICT";
                result.RagDecision = "RAG_OPTIONAL";
                break;
            case "CODING":
                result.UseToolDecisionGate = true;
                result.FactualityMode = "FACTUAL_STRICT";
                result.RagDecision = "RAG_NOT_REQUIRED";
                break;
            case "DOCUMENT":
                result.FactualityMode = "GROUNDED";
                result.RagDecision = "RAG_REQUIRED";
                break;
        }
        return result;
    }

    public static Dictionary<string, object?> Validate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "INTERACTION_MODE_INVALID", "mode must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != Format)
            throw new ExecutorError(
                "INTERACTION_MODE_INVALID", $"expected format {Format}");
        string mode = el.GetProperty("mode").GetString() ?? "";
        if (!Modes.Contains(mode))
            throw new ExecutorError(
                "INTERACTION_MODE_INVALID", $"bad mode {mode}");
        var route = Resolve(mode);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format, ["mode"] = mode,
            ["use_agent_work_graph"] = route.UseAgentWorkGraph,
            ["use_persona_runtime"] = route.UsePersonaRuntime,
            ["use_narrative_memory"] = route.UseNarrativeMemory,
            ["factuality_mode"] = route.FactualityMode,
            ["rag_decision"] = route.RagDecision,
        };
    }
}
