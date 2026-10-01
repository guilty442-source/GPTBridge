// CapabilityFreeze.cs — capability-training freeze enforcement
// (Native Production Convergence II §5). The guard rejects any request
// that would mutate weights or run a formal training lane while the
// freeze holds; SINGLE_CAPABILITY_RECOVERY narrows the freeze to exactly
// one declared-capability SFT lane and can never widen it.
// Ported from the devin worktree's RuntimeContracts.cs §34 guard.

namespace GPTBridge.XingchengLearning;

internal static class CapabilityFreeze
{
    public const bool CAPABILITY_TRAINING_FROZEN = true;

    /// <summary>Operations that always require live training.</summary>
    public static readonly string[] FrozenOperations =
    {
        "trainer_full_run", "sft", "dpo", "rl", "weight_update",
        "pretrain", "distill", "model_merge",
        "architecture_changing_weight_migration",
    };

    /// <summary>Operations allowed under the freeze (§34 allowlist).</summary>
    public static readonly string[] AllowedOperations =
    {
        "tiny_synthetic_probe", "operator_correctness_test",
        "runtime_conversion", "precision_conversion",
        "benchmark", "evaluation",
    };

    /// <summary>Gate an operation label — unknown labels fail closed
    /// (a caller that cannot name its lane is not trusted to run it).</summary>
    public static void Guard(string operation)
    {
        if (!CAPABILITY_TRAINING_FROZEN) return;
        if (AllowedOperations.Contains(operation)) return;
        throw new ExecutorError("CAPABILITY_TRAINING_FROZEN",
            $"operation '{operation}' requires capability training which is frozen");
    }

    /// <summary>True while the policy declares the single-capability
    /// recovery lane — the lane is admitted by the MODE declaration,
    /// not by the frozen flag.</summary>
    public static bool RecoveryLaneOpen(SelfLearningPolicy policy) =>
        string.Equals(policy.CapabilityTrainingMode,
                      "SINGLE_CAPABILITY_RECOVERY",
                      StringComparison.OrdinalIgnoreCase);

    /// <summary>Single-capability recovery lane
    /// (star-single-capability-recovery/v1): while the freeze holds, a
    /// policy may declare capability_training_mode =
    /// SINGLE_CAPABILITY_RECOVERY plus active_capability. Under that mode
    /// exactly one SFT job whose declared capability equals the active
    /// capability may proceed; every other capability and every non-SFT
    /// kind stays denied. Recovery mode only narrows the freeze — it can
    /// never widen it (an unconfigured/again-frozen policy falls back to
    /// the plain guard).</summary>
    public static void GuardJob(string operation, string? capability,
                                SelfLearningPolicy policy)
    {
        if (!CAPABILITY_TRAINING_FROZEN) return;
        // §33 unfreeze order: the human governor lifts the phase freeze
        // by setting capability_training_frozen=false in
        // runtime/settings/self-learning.json. The policy flag is
        // authoritative for job admission; the const remains the
        // phase default when the flag is absent or true.
        if (!policy.CapabilityTrainingFrozen) return;
        // The lane is admitted by the MODE declaration, not by the frozen
        // flag. frozen=false without the mode falls through to the plain
        // guard and stays denied.
        if (!RecoveryLaneOpen(policy))
        {
            Guard(operation);
            return;
        }
        if (operation != "sft")
            throw new ExecutorError("CAPABILITY_TRAINING_FROZEN",
                $"SINGLE_CAPABILITY_RECOVERY permits only sft; " +
                $"operation '{operation}' stays frozen");
        if (policy.ActiveCapability.Length == 0)
            throw new ExecutorError("CAPABILITY_RECOVERY_UNCONFIGURED",
                "SINGLE_CAPABILITY_RECOVERY requires active_capability");
        if (!string.Equals(capability ?? "", policy.ActiveCapability,
                           StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("MULTI_CAPABILITY_TRAINING_DENIED",
                $"job capability '{capability ?? ""}' is not the active " +
                $"recovery capability '{policy.ActiveCapability}'");
    }
}
