// CapabilityDescriptor.cs — ``star-capability-descriptor/v1``.
//
// Architecture × Capability integration (capability unification
// directive §3-§10, §21, §61-§66, §73): every xingcheng capability is a
// first-class descriptor row — never a runtime, model, store or
// scheduler of its own. All capabilities ride the single xc-fused-1
// HybridCausalDecoder core, the single NativeTrainer, the single
// NativeInferenceEngine and the single Lifecycle plane.
//
// The descriptor only describes (§11): what the capability is, what it
// needs, how it is verified, its maturity and its dependencies. It
// never executes anything.

namespace GPTBridge.XingchengLearning;

/// <summary>§73 dataset-class triplet: which dataset classes feed
/// training, which feed evaluation, and which are forbidden to leak
/// into training (golden/maturity/holdout/regression never train —
/// §74).</summary>
internal sealed class CapabilityDataDependency
{
    public string[] TrainingDatasetClasses = Array.Empty<string>();
    public string[] EvaluationDatasetClasses = Array.Empty<string>();
    public string[] ForbiddenDatasetClasses =
        { "golden", "maturity", "holdout", "regression" };

    public Dictionary<string, object?> ToDict() => new()
    {
        ["training_dataset_classes"] =
            TrainingDatasetClasses.Cast<object?>().ToList(),
        ["evaluation_dataset_classes"] =
            EvaluationDatasetClasses.Cast<object?>().ToList(),
        ["forbidden_dataset_classes"] =
            ForbiddenDatasetClasses.Cast<object?>().ToList(),
    };
}

/// <summary>§61 resource hint — a preference expression only. The
/// main-system Resource Governor is the sole resource authority
/// (§60/§62); a hint can never demand, only advise degrade/defer.</summary>
internal sealed class CapabilityResourceHint
{
    public string Minimum = "CPU_LOW";
    public string Preferred = "CPU_BALANCED";

    public Dictionary<string, object?> ToDict() => new()
    {
        ["minimum"] = Minimum,
        ["preferred"] = Preferred,
    };
}

/// <summary>§42 per-capability training policy — what a recovery or
/// maturation lane may touch. ``allowed_mutation`` can only ever name
/// weights/trainable-tensors/curriculum/data-mixture/training
/// parameters (§38); architecture fields are never legal values.</summary>
internal sealed class CapabilityTrainingPolicy
{
    public string[] AllowedMutation =
        { "weights", "allowed_trainable_tensors" };
    public string[] FreezeMap = Array.Empty<string>();
    public string[] DatasetRequirements = Array.Empty<string>();
    public string[] AnchorRequirements = Array.Empty<string>();
    public int MaxPilotSteps = 0;
    public string[] EvaluationRequirements = Array.Empty<string>();

    public Dictionary<string, object?> ToDict() => new()
    {
        ["allowed_mutation"] =
            AllowedMutation.Cast<object?>().ToList(),
        ["freeze_map"] = FreezeMap.Cast<object?>().ToList(),
        ["dataset_requirements"] =
            DatasetRequirements.Cast<object?>().ToList(),
        ["anchor_requirements"] =
            AnchorRequirements.Cast<object?>().ToList(),
        ["max_pilot_steps"] = MaxPilotSteps,
        ["evaluation_requirements"] =
            EvaluationRequirements.Cast<object?>().ToList(),
    };
}

/// <summary>§31/§86 architecture binding: which model / runtime /
/// data / eval components carry this capability. A capability without
/// a binding is CAPABILITY_ARCHITECTURE_INCOMPLETE (§87).</summary>
internal sealed class CapabilityBinding
{
    public string[] ModelComponents = Array.Empty<string>();
    public string[] RuntimeComponents = Array.Empty<string>();
    public string[] DataComponents = Array.Empty<string>();
    public string[] EvalComponents = Array.Empty<string>();

    public bool Complete =>
        ModelComponents.Length > 0 && EvalComponents.Length > 0;

    public Dictionary<string, object?> ToDict() => new()
    {
        ["model_components"] =
            ModelComponents.Cast<object?>().ToList(),
        ["runtime_components"] =
            RuntimeComponents.Cast<object?>().ToList(),
        ["data_components"] =
            DataComponents.Cast<object?>().ToList(),
        ["eval_components"] =
            EvalComponents.Cast<object?>().ToList(),
    };
}

/// <summary>§66 capability floor — the minimum a certified/mature
/// capability must keep; a candidate that drops a protected capability
/// below its floor is promotion-blocked (§92).</summary>
internal sealed class CapabilityFloor
{
    public double MinimumQuality = 0.0;
    public double MaxRegressionPct = 0.0;
    public string[] RequiredEvals = Array.Empty<string>();
    public string[] RequiredEvidence = { "eval_result" };
    // Maturation-closure §12: the floor is per-capability and hard —
    // runtime/architecture/stability checks are separate axes; a high
    // score on one can never compensate a failure on another (§13).
    public string[] RequiredRuntimeChecks = Array.Empty<string>();
    public string[] RequiredArchitectureChecks = Array.Empty<string>();
    public string[] RequiredStabilityChecks = Array.Empty<string>();

    public Dictionary<string, object?> ToDict() => new()
    {
        ["minimum_quality"] = MinimumQuality,
        ["max_regression_pct"] = MaxRegressionPct,
        ["required_evals"] = RequiredEvals.Cast<object?>().ToList(),
        ["required_evidence"] =
            RequiredEvidence.Cast<object?>().ToList(),
        ["required_runtime_checks"] =
            RequiredRuntimeChecks.Cast<object?>().ToList(),
        ["required_architecture_checks"] =
            RequiredArchitectureChecks.Cast<object?>().ToList(),
        ["required_stability_checks"] =
            RequiredStabilityChecks.Cast<object?>().ToList(),
    };
}

/// <summary>``star-capability-descriptor/v1`` — the single canonical
/// description of one xingcheng capability (§4 field set plus §31
/// binding, §66 floor and §83 alias vocabulary).</summary>
internal sealed class CapabilityDescriptor
{
    public const string Format = "star-capability-descriptor/v1";

    // §5 capability classes.
    public static readonly string[] Classes =
        { "MODEL_NATIVE", "RUNTIME_AUGMENTED", "SERVICE_AUGMENTED" };
    // §9 ownership planes.
    public static readonly string[] OwnerPlanes =
        { "GOVERNANCE", "COMPUTE", "DATA", "SYSTEM", "MIXED" };
    // §21 formal status vocabulary — IMPLEMENTED != CERTIFIED (§21):
    // architecture existing never means the capability exists (§22-24).
    public static readonly string[] Statuses =
    {
        "UNAVAILABLE", "IMPLEMENTED", "TRAINING", "EVALUATED",
        "CERTIFIED", "MATURE", "REGRESSED", "REOPENED",
    };
    // §61 resource-hint vocabulary (hint only — never a demand).
    public static readonly string[] ResourceClasses =
    {
        "CPU_LOW", "CPU_BALANCED", "CPU_HIGH",
        "RAM_HIGH", "GPU_PREFERRED", "GPU_REQUIRED", "EDGE",
    };
    // §38 the only values allowed_mutation may ever carry — a
    // capability can never mutate the canonical architecture (§39:
    // mutations belong to the Architecture Evolution plane alone).
    public static readonly string[] AllowedMutationKinds =
    {
        "weights", "allowed_trainable_tensors", "curriculum",
        "data_mixture", "training_parameters",
    };

    public required string CapabilityId;
    public string Category = "";
    public string CapabilityClass = "MODEL_NATIVE";
    public string OwnerPlane = "COMPUTE";
    public string ModelDependency = "model_weights";
    public string RuntimeDependency = "native_inference_engine";
    public CapabilityDataDependency Data = new();
    public string[] EvaluationSuites = Array.Empty<string>();
    /// <summary>Progression-stage stamp filled at emit time from the
    /// CapabilityProgressionPolicy state (pending/frozen/unsupported);
    /// "unsequenced" for capabilities outside the ordered sequence.</summary>
    public string MaturityStage = "unsequenced";
    public CapabilityResourceHint Resource = new();
    public CapabilityTrainingPolicy Training = new();
    public string[] RegressionDependencies = Array.Empty<string>();
    public string Status = "IMPLEMENTED";
    /// <summary>§83 alias vocabulary — exactly one canonical id; every
    /// historical spelling resolves here and nowhere else.</summary>
    public string[] Aliases = Array.Empty<string>();
    public CapabilityBinding Binding = new();
    public CapabilityFloor Floor = new();
    /// <summary>§68/§69: a MATURE capability is protected — later
    /// recovery lanes must hold its floor; a REGRESSED mature
    /// capability auto-reopens and keeps its historical evidence.</summary>
    public bool Protected;

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = Format,
        ["capability_id"] = CapabilityId,
        ["category"] = Category,
        ["capability_class"] = CapabilityClass,
        ["owner_plane"] = OwnerPlane,
        ["model_dependency"] = ModelDependency,
        ["runtime_dependency"] = RuntimeDependency,
        ["data_dependency"] = Data.ToDict(),
        ["evaluation_suite"] =
            EvaluationSuites.Cast<object?>().ToList(),
        ["maturity_stage"] = MaturityStage,
        ["resource_class"] = Resource.ToDict(),
        ["training_policy"] = Training.ToDict(),
        ["regression_dependencies"] =
            RegressionDependencies.Cast<object?>().ToList(),
        ["status"] = Status,
        ["protected"] = Protected,
        ["aliases"] = Aliases.Cast<object?>().ToList(),
        ["architecture_binding"] = Binding.ToDict(),
        ["floor"] = Floor.ToDict(),
    };

    /// <summary>Structural validation of one descriptor — closed
    /// vocabularies, required fields, §38 mutation whitelist.</summary>
    public List<string> Validate()
    {
        var errors = new List<string>();
        if (string.IsNullOrWhiteSpace(CapabilityId))
            errors.Add("capability_id_missing");
        if (!Classes.Contains(CapabilityClass))
            errors.Add($"bad_capability_class:{CapabilityClass}");
        if (!OwnerPlanes.Contains(OwnerPlane))
            errors.Add($"bad_owner_plane:{OwnerPlane}");
        if (!Statuses.Contains(Status))
            errors.Add($"bad_status:{Status}");
        foreach (var m in Training.AllowedMutation)
            if (!AllowedMutationKinds.Contains(m))
                errors.Add($"bad_allowed_mutation:{m}");
        if (!ResourceClasses.Contains(Resource.Minimum))
            errors.Add($"bad_resource_min:{Resource.Minimum}");
        if (!ResourceClasses.Contains(Resource.Preferred))
            errors.Add($"bad_resource_pref:{Resource.Preferred}");
        if (Aliases.Any(a => string.IsNullOrWhiteSpace(a)))
            errors.Add("alias_empty");
        // §87 class-aware coverage: SERVICE_AUGMENTED rows never carry
        // model components (§8) — their binding is the service runtime
        // surface; MODEL_NATIVE/RUNTIME_AUGMENTED rows need model+eval.
        bool bindingOk = CapabilityClass == "SERVICE_AUGMENTED"
            ? Binding.RuntimeComponents.Length > 0
            : Binding.Complete;
        if (!bindingOk)
            errors.Add("architecture_binding_incomplete");
        return errors;
    }
}
