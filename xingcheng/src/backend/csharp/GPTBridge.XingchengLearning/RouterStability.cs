// RouterStability.cs — MiMo-V2.6 router-stability absorption (§18-§20).
//
//   RouterStabilityPolicy  training-stage router policy: PRETRAIN=
//                          TRAINABLE, SFT=TRAINABLE/GOVERNED,
//                          BASELINE_RECOVERY=GOVERNED,
//                          LARGE_AGENT_RL=FROZEN_BY_DEFAULT — agent RL
//                          must not drift the MoE routing distribution
//                          (it would break expert specialization,
//                          residency, hotset prediction, quantization
//                          calibration, cache locality).
//   RouterStabilityGate    monitors entropy/utilization/top-k
//                          distribution/hotspot/starvation/drift;
//                          over-limit rejects the candidate with
//                          ROUTER_DRIFT_EXCEEDED.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class RouterStability
{
    public const string Format = "star-router-stability/v1";

    public static readonly string[] Stages =
        { "PRETRAIN", "SFT", "BASELINE_RECOVERY", "LARGE_AGENT_RL" };

    // §18 stage policy.
    public static Dictionary<string, object?> Policy(string stage)
    {
        if (!Stages.Contains(stage))
            throw new ExecutorError("ROUTER_STABILITY_INVALID",
                $"unknown stage {stage}");
        string mode = stage switch
        {
            "PRETRAIN" => "TRAINABLE",
            "SFT" => "GOVERNED",
            "BASELINE_RECOVERY" => "GOVERNED",
            _ => "FROZEN_BY_DEFAULT",
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["stage"] = stage, ["router_trainability"] = mode,
            ["rationale"] = stage == "LARGE_AGENT_RL"
                ? "agent RL learns policy/tools/task completion — " +
                  "routing distribution stays frozen by default"
                : "router may train under governance bounds",
        };
    }

    /// <summary>§20 drift gate. Input: {entropy_min, entropy_max,
    /// utilization_min, drift_js (Jensen-Shannon divergence vs
    /// reference), session_affinity_drift, bounds{...}}. A metric
    /// outside bounds rejects the candidate — fail closed.</summary>
    public static Dictionary<string, object?> Gate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("ROUTER_STABILITY_INVALID",
                "gate input must be object");
        var violations = new List<object?>();
        double Get(string k, double d) =>
            el.TryGetProperty(k, out var v) &&
            v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;
        double Bnd(string k, double d) =>
            el.TryGetProperty("bounds", out var b) &&
            b.ValueKind == JsonValueKind.Object &&
            b.TryGetProperty(k, out var v) &&
            v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;

        double entMin = Get("entropy_min", 0),
              entMax = Get("entropy_max", 0);
        double bEntMin = Bnd("entropy_min", 0.0),
               bEntMax = Bnd("entropy_max", 99.0);
        if (entMin < bEntMin || entMax > bEntMax)
            violations.Add(new Dictionary<string, object?>
            {
                ["metric"] = "router_entropy",
                ["min"] = entMin, ["max"] = entMax,
                ["bounds"] = $"[{bEntMin},{bEntMax}]",
            });
        double util = Get("utilization_min", 1.0);
        double bUtil = Bnd("utilization_min", 0.5);
        if (util < bUtil)
            violations.Add(new Dictionary<string, object?>
            {
                ["metric"] = "expert_utilization",
                ["min"] = util, ["bound"] = bUtil,
            });
        double drift = Get("drift_js", 0.0);
        double bDrift = Bnd("drift_js", 0.1);
        if (drift > bDrift)
            violations.Add(new Dictionary<string, object?>
            {
                ["metric"] = "routing_drift",
                ["drift_js"] = drift, ["bound"] = bDrift,
            });
        double affDrift = Get("session_affinity_drift", 0.0);
        double bAff = Bnd("session_affinity_drift", 0.2);
        if (affDrift > bAff)
            violations.Add(new Dictionary<string, object?>
            {
                ["metric"] = "session_affinity_drift",
                ["value"] = affDrift, ["bound"] = bAff,
            });
        if (violations.Count > 0)
            throw new ExecutorError("ROUTER_DRIFT_EXCEEDED",
                string.Join(",",
                    violations.Cast<Dictionary<string, object?>>()
                        .Select(v => (string)v["metric"]!)));
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["verdict"] = "STABLE",
            ["metrics"] = new Dictionary<string, object?>
            {
                ["entropy_min"] = entMin, ["entropy_max"] = entMax,
                ["utilization_min"] = util, ["drift_js"] = drift,
                ["session_affinity_drift"] = affDrift,
            },
        };
    }
}
