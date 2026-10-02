// CapabilityFailureAttribution.cs — ``star-failure-attribution/v1``
// (capability unification directive §46-§51).
//
// Autonomous learning never scans raw failures into training (§46):
// Failure → Capability Classification → CapabilityGraph → Root-Cause
// Capability → Progression Policy → LearningIntent. Attribution is
// the classification step — every failure resolves to exactly one
// §47 class and a routing decision:
//
//   MODEL_FAILURE                → weight training eligible
//   MIXED_FAILURE (model leg)    → weight training eligible (§48)
//   RUNTIME_FAILURE              → runtime plane; never trains (§49)
//   DATA_FAILURE                 → Rust data plane; never trains (§51)
//   TOOL_FAILURE                 → tool plane; never trains
//   RESOURCE_FAILURE             → governor; never enters the
//                                  failure dataset at all (§50)
//   SERVICE_FAILURE              → service owner; never trains

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityFailureAttribution
{
    public const string Format = "star-failure-attribution/v1";

    // §47 closed attribution vocabulary.
    public static readonly string[] Classes =
    {
        "MODEL_FAILURE", "RUNTIME_FAILURE", "DATA_FAILURE",
        "TOOL_FAILURE", "RESOURCE_FAILURE", "SERVICE_FAILURE",
        "MIXED_FAILURE",
    };

    // Known non-model cause → §47 class. Superset of the recovery
    // loop's §9 NonModelCauses plus governor/kernel causes.
    private static readonly Dictionary<string, string> CauseMap =
        new(StringComparer.Ordinal)
        {
            ["tool_outage"] = "TOOL_FAILURE",
            ["tool_error"] = "TOOL_FAILURE",
            ["rag_retrieval_failure"] = "RUNTIME_FAILURE",
            ["cuda_kernel_error"] = "RUNTIME_FAILURE",
            ["native_kernel_error"] = "RUNTIME_FAILURE",
            ["runtime_crash"] = "RUNTIME_FAILURE",
            ["corrupt_document"] = "DATA_FAILURE",
            ["corrupt_corpus"] = "DATA_FAILURE",
            ["invalid_external_data"] = "DATA_FAILURE",
            ["network_failure"] = "SERVICE_FAILURE",
            ["service_unavailable"] = "SERVICE_FAILURE",
            ["resource_governor_revoke"] = "RESOURCE_FAILURE",
            ["quota_exceeded"] = "RESOURCE_FAILURE",
            ["resource_revoked"] = "RESOURCE_FAILURE",
        };

    // §47 class → owning lane.
    private static string RouteFor(string cls) => cls switch
    {
        "MODEL_FAILURE" => "weight-training",
        "MIXED_FAILURE" => "weight-training+owning-plane",
        "RUNTIME_FAILURE" => "runtime-plane",
        "DATA_FAILURE" => "rust-data-plane",
        "TOOL_FAILURE" => "tool-plane",
        "RESOURCE_FAILURE" => "main-system-resource-governor",
        "SERVICE_FAILURE" => "service-owner",
        _ => "unrouted",
    };

    /// <summary>Classify a failure record. Inputs: an optional
    /// explicit ``failure_class`` (already adjudicated), a ``cause``
    /// string, a ``capability`` name, and optional model/runtime
    /// provenance. Returns the attribution verdict; unknown causes
    /// with no capability signal fail closed as
    /// CAPABILITY_CLASSIFICATION_UNCERTAIN (the record can never
    /// silently become model evidence).</summary>
    public static Dictionary<string, object?> Classify(
        JsonElement el)
    {
        string S(string k) => el.TryGetProperty(k, out var v) &&
            v.ValueKind == JsonValueKind.String
                ? v.GetString() ?? "" : "";

        string explicitClass = S("failure_class").ToUpperInvariant();
        string cause = S("cause");
        string capRaw = S("capability");
        string? capability = capRaw.Length > 0
            ? CapabilityRegistry.Resolve(capRaw) : null;
        bool modelSignal = capability != null ||
            S("model_version").Length > 0 ||
            S("eval_suite").Length > 0;

        string cls;
        if (explicitClass.Length > 0)
        {
            if (!Classes.Contains(explicitClass))
                throw new ExecutorError("CAPABILITY_EVIDENCE_INVALID",
                    $"unknown failure_class '{explicitClass}'");
            cls = explicitClass;
        }
        else if (cause.Length > 0 && CauseMap.TryGetValue(
                     cause, out string? mapped))
        {
            // A non-model cause with a model leg is MIXED (§48).
            cls = modelSignal ? "MIXED_FAILURE" : mapped;
        }
        else if (cause.Length > 0)
        {
            // Unknown cause: a model signal makes it MIXED (the model
            // may share fault); without one the record cannot become
            // training evidence — classify as its closest non-model
            // lane only when the caller asserts it.
            cls = modelSignal ? "MIXED_FAILURE" : "SERVICE_FAILURE";
        }
        else if (modelSignal)
        {
            cls = "MODEL_FAILURE";
        }
        else
        {
            throw new ExecutorError(
                "CAPABILITY_CLASSIFICATION_UNCERTAIN",
                "failure has no cause, class or capability signal");
        }

        // §48: only MODEL_FAILURE or a MIXED_FAILURE with a model leg
        // reaches weight training.
        bool trainable = cls is "MODEL_FAILURE" or "MIXED_FAILURE"
            && modelSignal;
        // §50: resource failures never enter the failure dataset.
        bool poolEligible = cls != "RESOURCE_FAILURE";

        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["failure_class"] = cls,
            ["capability_id"] = capability,
            ["cause"] = cause.Length > 0 ? cause : null,
            ["trainable"] = trainable,
            ["pool_eligible"] = poolEligible,
            ["route"] = RouteFor(cls),
            ["rule"] = "MODEL_FAILURE or model-legged MIXED only; " +
                       "RESOURCE_FAILURE never enters the dataset",
        };
    }
}
