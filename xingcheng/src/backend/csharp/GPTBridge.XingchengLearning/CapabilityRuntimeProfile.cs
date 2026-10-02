// CapabilityRuntimeProfile.cs — ``star-capability-runtime-profile/v1``
// (capability unification directive §55-§65).
//
// A capability runtime profile is NOT a runtime (§56): it is one
// profile of the shared CompiledExecutionPlan on the single
// NativeInferenceEngine. It carries only quality/latency/memory
// requirements — never kernel selection (§64: kernel choice belongs
// to AccelerationPlane + KernelRegistry) and never resource demands
// (§60-§62: resource hints may be overridden by the governor).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityRuntimeProfile
{
    public const string Format = "star-capability-runtime-profile/v1";

    // §57 example profile names — execution-policy labels, not
    // capabilities and not deployment profiles (§58).
    public static readonly string[] Profiles =
    {
        "BALANCED", "CONTEXT_HEAVY", "REASONING_ENABLED",
        "TOOL_STRICT", "EDGE",
    };

    /// <summary>Emit the execution-profile view of a canonical
    /// capability. Returns ``profile:null`` when the capability is
    /// service-augmented — the model itself runs under the caller's
    /// plan; only its service leg differs.</summary>
    public static Dictionary<string, object?> Emit(
        string capabilityInput)
    {
        string? cap = CapabilityRegistry.Resolve(capabilityInput);
        if (cap == null)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                capabilityInput);
        var d = CapabilityRegistry.Get(cap)
            ?? throw new ExecutorError("CAPABILITY_UNKNOWN",
                capabilityInput);
        string cls = d.CapabilityClass;

        var profile = new Dictionary<string, object?>
        {
            // §64: requirements only — no kernel names, no tile ids.
            ["latency_class"] = "standard",
            ["memory_class"] = "standard",
            ["kv_policy"] = "standard",
            ["reasoning_policy"] = "off",
            ["strict_schema"] = false,
        };
        string name = cap switch
        {
            "coding" or "context_tracking" or "long_context" or
            "multi_turn" => "CONTEXT_HEAVY",
            "native_thinking" or "math" => "REASONING_ENABLED",
            "tool_calling" or "structured_output" => "TOOL_STRICT",
            _ => "BALANCED",
        };
        switch (name)
        {
            case "CONTEXT_HEAVY":
                profile["memory_class"] = "high";
                profile["kv_policy"] = "retain";
                break;
            case "REASONING_ENABLED":
                profile["reasoning_policy"] = "normal";
                profile["latency_class"] = "relaxed";
                break;
            case "TOOL_STRICT":
                profile["strict_schema"] = true;
                break;
            case "EDGE":
                profile["memory_class"] = "low";
                profile["latency_class"] = "aggressive";
                break;
        }

        var spec = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["capability_id"] = cap,
            ["capability_class"] = cls,
            // §56: same engine, one CompiledExecutionPlan profile.
            ["execution_profile"] = name,
            ["engine"] = "NativeInferenceEngine",
            ["profile"] = profile,
            // §61: descriptor hints surface to the governor — it may
            // degrade, defer or mark unavailable (§62); the
            // capability never demands.
            ["resource_hint"] = d.Resource.ToDict(),
            ["rule"] = "profile only — kernel selection stays with " +
                       "AccelerationPlane/KernelRegistry; resource " +
                       "authority stays with the main governor",
        };
        return spec;
    }
}
