// CapabilitySeed.cs — canonical capability seed set for the
// CapabilityRegistry (capability unification directive §83/§84).
//
// Code is the source of truth for the canonical vocabulary; the
// persisted ``star-capability-registry/v1`` file is its governed
// projection. One canonical capability_id per capability — every
// historical spelling lives in ``aliases`` (§83). Capabilities are
// never models, runtimes, stores or schedulers (§0-§2): each row only
// describes what it needs from the single xc-fused-1 stack.
//
// §32-§37 binding examples are encoded in each row's
// architecture_binding; §42 training policy, §61 resource hint, §66
// floor and §73 dataset classes ride inside the descriptor.

namespace GPTBridge.XingchengLearning;

internal static class CapabilitySeed
{
    private static CapabilityDescriptor D(
        string id, string category, string cls, string plane,
        string[] evals, string[] aliases,
        string[] model, string[] runtime, string[] data,
        string[] evalComponents,
        string[]? regresses = null,
        string[]? trainingClasses = null,
        string[]? evalClasses = null,
        string resMin = "CPU_LOW", string resPref = "CPU_BALANCED",
        string status = "IMPLEMENTED",
        string[]? requiredEvals = null,
        string[]? anchors = null) =>
        new()
        {
            CapabilityId = id,
            Category = category,
            CapabilityClass = cls,
            OwnerPlane = plane,
            ModelDependency =
                cls == "MODEL_NATIVE" ? "model_weights"
                : cls == "RUNTIME_AUGMENTED" ? "model_weights+runtime"
                : "none",
            RuntimeDependency = runtime.Length > 0
                ? string.Join("+", runtime) : "native_inference_engine",
            EvaluationSuites = evals,
            Aliases = aliases,
            RegressionDependencies = regresses ?? Array.Empty<string>(),
            Resource = new CapabilityResourceHint
                { Minimum = resMin, Preferred = resPref },
            Data = new CapabilityDataDependency
            {
                TrainingDatasetClasses =
                    trainingClasses ?? new[] { "sft", "curriculum" },
                EvaluationDatasetClasses =
                    evalClasses ?? evals,
            },
            Training = new CapabilityTrainingPolicy
            {
                AnchorRequirements = anchors ?? Array.Empty<string>(),
                EvaluationRequirements = requiredEvals ?? evals,
            },
            Binding = new CapabilityBinding
            {
                ModelComponents = model,
                RuntimeComponents = runtime,
                DataComponents = data,
                EvalComponents = evalComponents,
            },
            Floor = new CapabilityFloor
            {
                RequiredEvals = requiredEvals ?? evals,
            },
            Status = status,
        };

    /// <summary>The canonical capability catalog. MODEL_NATIVE rows
    /// are carried by weights alone (§6); RUNTIME_AUGMENTED rows need
    /// model × native-runtime evidence separated (§7);
    /// SERVICE_AUGMENTED rows depend on services outside xingcheng and
    /// can never be claimed as model-internal (§8).</summary>
    public static readonly CapabilityDescriptor[] All =
    {
        // ── MODEL_NATIVE ───────────────────────────────────────────
        D("instruction_following", "language", "MODEL_NATIVE", "COMPUTE",
          new[] { "instruction", "zh-TW", "en" },
          new[] { "instruction" },
          new[] { "model_weights", "instruction_curriculum" },
          new[] { "native_inference_engine" },
          new[] { "instruction_dataset" },
          new[] { "eval:instruction", "eval:zh-TW", "eval:en" },
          regresses: new[] { "structured_output", "multi_turn" }),
        D("context_tracking", "dialogue", "MODEL_NATIVE", "COMPUTE",
          new[] { "context_tracking" },
          new[] { "context", "long-context-tracking" },
          new[] { "delta_state", "full_attention", "position_encoding",
                  "kv_cache" },
          new[] { "native_inference_engine" },
          new[] { "context_dataset" },
          new[] { "eval:context_tracking" },
          regresses: new[] { "multi_turn" }),
        D("multi_turn", "dialogue", "MODEL_NATIVE", "COMPUTE",
          new[] { "multi_turn" },
          new[] { "multiturn", "dialogue", "conversation" },
          new[] { "model_weights", "dialogue_state" },
          new[] { "native_inference_engine" },
          new[] { "dialogue_dataset" },
          new[] { "eval:multi_turn" },
          regresses: new[] { "context_tracking" }),
        D("structured_output", "structure", "MODEL_NATIVE", "COMPUTE",
          new[] { "structured_output", "tool_call_format" },
          new[] { "structured", "json_output" },
          new[] { "model_weights" },
          new[] { "structured_output_validator" },
          new[] { "structured_dataset" },
          new[] { "eval:structured_output", "schema_validator" },
          regresses: new[] { "tool_calling" }),
        D("tool_calling", "tooling", "MODEL_NATIVE", "MIXED",
          new[] { "tool_call_format", "tool_calling" },
          new[] { "tool", "tool-call", "tool_call", "tool_call_format",
                  "tooldecision", "tool-decision", "ToolCapability" },
          new[] { "model_weights", "tool_schema" },
          new[] { "native_inference_engine", "runtime_validator" },
          new[] { "tool_call_dataset" },
          new[] { "eval:tool_call_format", "tool_schema_parser" },
          regresses: new[] { "structured_output", "instruction_following",
                             "context_tracking" }),
        D("reading", "knowledge", "MODEL_NATIVE", "COMPUTE",
          new[] { "reading" },
          new[] { "reading_grounding", "reading-comprehension" },
          new[] { "model_weights", "full_attention" },
          new[] { "native_inference_engine" },
          new[] { "reading_dataset" },
          new[] { "eval:reading" },
          regresses: new[] { "rag" }),
        D("math", "reasoning", "MODEL_NATIVE", "COMPUTE",
          new[] { "math" },
          new[] { "arithmetic" },
          new[] { "model_weights", "native_thinking_path" },
          new[] { "native_inference_engine" },
          new[] { "math_dataset", "math_curriculum" },
          new[] { "eval:math" },
          regresses: new[] { "native_thinking" }),
        D("coding", "tooling", "MODEL_NATIVE", "COMPUTE",
          new[] { "code" },
          new[] { "code", "fim" },
          new[] { "model_weights", "fim_head" },
          new[] { "native_inference_engine" },
          new[] { "code_dataset" },
          new[] { "eval:code" },
          regresses: new[] { "structured_output" },
          resMin: "CPU_BALANCED", resPref: "RAM_HIGH"),
        D("native_thinking", "reasoning", "MODEL_NATIVE", "COMPUTE",
          new[] { "native_thinking", "thinking", "system1" },
          new[] { "thinking", "system1", "reasoning" },
          new[] { "hidden_state_feedback", "system1_path",
                  "thinking_path" },
          new[] { "reasoning_execution_policy" },
          new[] { "reasoning_dataset" },
          new[] { "eval:native_thinking", "eval:system1" }),
        D("vision", "modality", "MODEL_NATIVE", "COMPUTE",
          new[] { "vision" },
          new[] { "vision-understanding" },
          new[] { "vision_encoder", "early_fusion" },
          new[] { "native_inference_engine" },
          new[] { "vision_dataset" },
          new[] { "eval:vision" },
          resPref: "GPU_PREFERRED"),
        D("expert_routing", "structure", "MODEL_NATIVE", "COMPUTE",
          new[] { "expert_routing" },
          new[] { "routing", "moe-routing", "moe_routing",
                  "moe_routing_agreement" },
          new[] { "moe_router", "shared_experts", "routed_experts" },
          new[] { "native_inference_engine" },
          new[] { "expert_routing_dataset" },
          new[] { "eval:expert_routing" }),

        // ── RUNTIME_AUGMENTED ──────────────────────────────────────
        // §37: rag = model + runtime; it decomposes into the four
        // sub-capabilities below it requires.
        D("rag", "knowledge", "RUNTIME_AUGMENTED", "MIXED",
          new[] { "rag", "citation" },
          new[] { "rag_grounding", "rag-grounding" },
          new[] { "model_weights", "full_attention" },
          new[] { "rag_engine", "retrieval_adapter" },
          new[] { "rag_dataset", "retrieval_corpus" },
          new[] { "eval:rag", "eval:citation" },
          regresses: new[] { "reading" },
          resPref: "RAM_HIGH"),
        D("retrieval_understanding", "knowledge", "RUNTIME_AUGMENTED",
          "MIXED",
          new[] { "rag" },
          new[] { "retrieval-understanding" },
          new[] { "model_weights" },
          new[] { "retrieval_adapter" },
          new[] { "retrieval_corpus" },
          new[] { "eval:rag" }),
        D("grounding", "knowledge", "RUNTIME_AUGMENTED", "MIXED",
          new[] { "rag", "citation" },
          new[] { "rag_grounding_sub" },
          new[] { "model_weights" },
          new[] { "rag_engine" },
          new[] { "grounding_dataset" },
          new[] { "eval:rag", "grounded_claim_validator" }),
        D("citation_attribution", "knowledge", "RUNTIME_AUGMENTED",
          "MIXED",
          new[] { "citation" },
          new[] { "citation", "attribution" },
          new[] { "model_weights" },
          new[] { "rag_engine" },
          new[] { "citation_dataset" },
          new[] { "eval:citation" }),
        D("context_integration", "knowledge", "RUNTIME_AUGMENTED",
          "MIXED",
          new[] { "rag" },
          new[] { "context-integration" },
          new[] { "model_weights", "kv_cache" },
          new[] { "rag_engine" },
          new[] { "context_dataset" },
          new[] { "eval:rag" }),
        D("long_context", "runtime", "RUNTIME_AUGMENTED", "MIXED",
          new[] { "long-context" },
          new[] { "long-context", "yarn" },
          new[] { "position_encoding", "kv_cache" },
          new[] { "kv_paging", "prefix_cache" },
          new[] { "long_context_dataset" },
          new[] { "eval:long-context" },
          resMin: "RAM_HIGH", resPref: "RAM_HIGH"),
        D("speculative_decode", "runtime", "RUNTIME_AUGMENTED", "MIXED",
          new[] { "mtp-speedup" },
          new[] { "mtp", "spec_decode", "speculative-decode" },
          new[] { "mtp_head" },
          new[] { "speculative_decoder" },
          new[] { "mtp_dataset" },
          new[] { "eval:mtp-speedup", "runtime_correctness_probe" }),
        D("prefix_reuse", "runtime", "RUNTIME_AUGMENTED", "MIXED",
          new[] { "prefix-reuse" },
          new[] { "prefix_cache", "prefix-reuse" },
          new[] { "kv_cache" },
          new[] { "prefix_cache" },
          new[] { "prefix_dataset" },
          new[] { "eval:prefix-reuse" }),
        D("tool_formatting", "tooling", "RUNTIME_AUGMENTED", "MIXED",
          new[] { "tool_call_format" },
          new[] { "tool-format", "tool_format" },
          new[] { "tool_schema" },
          new[] { "tool_schema_parser", "runtime_validator" },
          new[] { "tool_call_dataset" },
          new[] { "eval:tool_call_format", "tool_schema_parser" }),
        D("vision_execution", "modality", "RUNTIME_AUGMENTED", "MIXED",
          new[] { "vision" },
          new[] { "vision-execution" },
          new[] { "vision_encoder" },
          new[] { "vision_runtime" },
          new[] { "vision_dataset" },
          new[] { "eval:vision" },
          resPref: "GPU_PREFERRED"),

        // ── SERVICE_AUGMENTED ──────────────────────────────────────
        // §8: results produced outside xingcheng — never claimed as a
        // model-internal ability.
        D("external_tool_execution", "tooling", "SERVICE_AUGMENTED",
          "SYSTEM",
          new[] { "tool_call_format" },
          new[] { "tool_execution", "external-tools" },
          Array.Empty<string>(),
          new[] { "tool_host" },
          new[] { "tool_result_dataset" },
          new[] { "eval:tool_call_format" },
          trainingClasses: new[] { "tool_result_traces" }),
        D("resource_governance", "system", "SERVICE_AUGMENTED",
          "SYSTEM",
          Array.Empty<string>(),
          new[] { "resource-governor", "resource_governor" },
          Array.Empty<string>(),
          new[] { "main_system_resource_governor" },
          Array.Empty<string>(),
          Array.Empty<string>(),
          trainingClasses: Array.Empty<string>(),
          evalClasses: Array.Empty<string>()),
    };
}
