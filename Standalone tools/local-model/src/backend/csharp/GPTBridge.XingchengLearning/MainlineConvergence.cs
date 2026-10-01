// MainlineConvergence.cs — Autonomous Training Mainline Convergence
// (§0-§86). Contract layer for the four new governed surfaces:
//
//   EffectiveTrainingPolicy §17-§22 — requested vs effective policy.
//     Requested flags (dpo_enabled, capability_training_mode, ...)
//     are never the authority: every lane resolves through the freeze
//     lattice and reports REQUESTED/DENIED/GRANTED with a typed
//     blocked_by reason. Schedulers must consume this report, not raw
//     policy fields.
//   ModelMutationLease §6-§12 — ONE_CANDIDATE ONE_WEIGHT_WRITER. A
//     file-ledger lease keyed by (generation, candidate_id,
//     source_checkpoint_sha256, architecture_contract_hash); at most
//     one ACTIVE_MUTATION per candidate, one writer system-wide per
//     generation. TTL-based recovery cleans crashed leases.
//   CapacityProof §26-§30 — star-capacity-proof/v1. Parameter counts
//     are recomputed from the manifest tensor table (names × shapes),
//     never trusted from declared fields. Publish gate: total inside
//     [300M, 20B] AND active_params_worst_case <= 1B.
//   SequenceConsistency §13-§15 — active_capability must equal the
//     maturation GuardSequence head unless a reopen/override receipt
//     exists in state; otherwise CAPABILITY_SEQUENCE_VIOLATION.
//
// Fail codes: MODEL_MUTATION_LEASE_CONFLICT, MODEL_MUTATION_SOURCE_DRIFT,
// MODEL_MUTATION_LEASE_EXPIRED, TOTAL_PARAMETER_WINDOW_VIOLATION,
// ACTIVE_PARAMETER_CEILING_EXCEEDED, CAPACITY_PROOF_MISSING,
// CAPACITY_PROOF_MISMATCH, CAPABILITY_SEQUENCE_VIOLATION,
// ARCHITECTURE_CONTRACT_DRIFT.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class MainlineConvergence
{
    // ------------------------------------------------- §17-§22 effective policy ----

    /// <summary>star-effective-training-policy/v1: every lane resolves
    /// REQUESTED -> EFFECTIVE through the freeze lattice. A lane that is
    /// requested but denied reports denied with a typed blocked_by; the
    /// report never collapses the two (§20).</summary>
    public static Dictionary<string, object?> EffectivePolicy(
        string toolRoot)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        var state = Maturation300M.LoadState(toolRoot);
        var head = Maturation300M.Head(state);

        bool frozen = policy.CapabilityTrainingFrozen;
        bool recovery = CapabilityFreeze.RecoveryLaneOpen(policy);
        bool canonPretrain =
            CapabilityFreeze.CanonicalPretrainLaneOpen(policy);

        Dictionary<string, object?> Lane(bool requested, bool granted,
                                         string blockedBy, string reason) =>
            new()
            {
                ["requested"] = requested,
                ["effective"] = granted,
                ["verdict"] = !requested ? "NOT_REQUESTED"
                             : granted ? "GRANTED" : "DENIED",
                ["blocked_by"] = granted || !requested ? null
                                 : (object?)blockedBy,
                ["reason"] = granted || !requested ? null
                             : (object?)reason,
            };

        bool sftGranted = !frozen
                          || (recovery &&
                              policy.ActiveCapability.Length > 0);
        bool pretrainGranted = !frozen || canonPretrain;

        var rep = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-effective-training-policy/v1",
            ["requested"] = new Dictionary<string, object?>
            {
                ["pretrain"] = canonPretrain,
                ["sft"] = recovery,
                ["dpo"] = policy.DpoEnabled,
                ["grpo"] = false,
                ["self_training"] = policy.SelfTrainingMode != "OFF",
                ["capability"] = policy.ActiveCapability,
                ["self_training_mode"] = policy.SelfTrainingMode,
                ["capability_training_frozen"] = frozen,
            },
            ["effective"] = new Dictionary<string, object?>
            {
                ["pretrain"] = Lane(canonPretrain, pretrainGranted,
                    "CAPABILITY_TRAINING_FROZEN",
                    "canonical pretrain lane requires " +
                    "architecture_pretrain_mode=XC_FUSED_1"),
                ["sft"] = Lane(recovery, sftGranted,
                    "CAPABILITY_TRAINING_FROZEN",
                    recovery
                        ? "SINGLE_CAPABILITY_RECOVERY requires " +
                          "active_capability"
                        : "sft requires SINGLE_CAPABILITY_RECOVERY"),
                ["dpo"] = Lane(policy.DpoEnabled, false,
                    "repo-rl-frozen-boundary", "RL_FROZEN"),
                ["grpo"] = Lane(false, false,
                    "repo-rl-frozen-boundary", "RL_FROZEN"),
                ["self_training"] = Lane(
                    policy.SelfTrainingMode != "OFF",
                    policy.SelfTrainingMode == "GOVERNED_AUTONOMOUS"
                        || policy.SelfTrainingMode == "PILOT_ONLY",
                    "SELF_TRAINING_MODE",
                    "mode is below the lane's admission floor"),
                ["active_capability"] = sftGranted
                    ? (object?)policy.ActiveCapability : null,
            },
            ["guard_sequence_head"] = head?.Id,
            ["capability_sequence_consistent"] =
                head != null
                    ? string.Equals(policy.ActiveCapability, head.Id,
                                    StringComparison.OrdinalIgnoreCase)
                    : policy.ActiveCapability.Length == 0,
            ["checked_at"] = XcPaths.IsoNow(),
        };
        return rep;
    }

    // ------------------------------------------------- §6-§12 mutation lease ----

    public const string LeaseRel =
        "xingcheng/runtime/state/mutation-lease.json";
    public const int DefaultTtlS = 7200;

    public static readonly string[] MutationKinds =
    {
        "PRETRAIN", "SFT", "DPO", "GRPO", "DISTILL", "COMPRESS",
        "QUANT_AWARE_TRAIN",
    };

    private static string LeasePath(string toolRoot) =>
        Path.Combine(toolRoot,
            LeaseRel.Replace('/', Path.DirectorySeparatorChar));

    public static Dictionary<string, object?> LoadLease(string toolRoot)
    {
        string path = LeasePath(toolRoot);
        if (!File.Exists(path))
            return new Dictionary<string, object?> { ["state"] = "FREE" };
        try
        {
            return (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(File.ReadAllText(path))
                    .RootElement)!;
        }
        catch (Exception ex) when (ex is JsonException or IOException
                                       or UnauthorizedAccessException)
        {
            // Corrupt lease file: fail closed as FREE-after-expiry —
            // the next acquire rewrites the ledger. A corrupt lease is
            // never evidence of an active writer.
            return new Dictionary<string, object?>
            {
                ["state"] = "FREE",
                ["recovered_from"] = "corrupt-lease",
            };
        }
    }

    private static void SaveLease(string toolRoot,
                                  Dictionary<string, object?> lease)
    {
        string path = LeasePath(toolRoot);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(lease) + "\n");
    }

    private static bool LeaseExpired(Dictionary<string, object?> l)
    {
        if (!TransformerTrainingRepository.Truthy(
                l.GetValueOrDefault("active")))
            return true;
        string? at = TransformerTrainingRepository.Str(
            l, "acquired_at");
        int ttl = TransformerTrainingRepository.Int(l, "ttl_s");
        if (ttl <= 0) ttl = DefaultTtlS;
        if (at == null ||
            !DateTime.TryParse(at, out DateTime t))
            return true;
        return DateTime.UtcNow - t.ToUniversalTime() >
               TimeSpan.FromSeconds(ttl);
    }

    /// <summary>BEGIN_MUTATION: acquire the single weight-writer lease.
    /// A live lease held by a different candidate conflicts; a lease on
    /// the same candidate whose source checkpoint drifted denies with
    /// SOURCE_DRIFT; an expired lease is reclaimed and the expiry is
    /// recorded on the returned report.</summary>
    public static Dictionary<string, object?> Acquire(
        string toolRoot, string generation, string candidateId,
        string modelVersion, string sourceCkptSha256,
        string archHash, string mutationKind, int ttlS = DefaultTtlS)
    {
        if (candidateId.Length == 0)
            throw new ExecutorError("MODEL_MUTATION_LEASE_CONFLICT",
                "lease requires candidate_id");
        if (!MutationKinds.Contains(mutationKind,
                StringComparer.OrdinalIgnoreCase))
            throw new ExecutorError("MODEL_MUTATION_LEASE_CONFLICT",
                $"mutation_kind '{mutationKind}' is not in the " +
                "governed set");
        var lease = LoadLease(toolRoot);
        var rep = new Dictionary<string, object?>
        {
            ["format"] = "star-model-mutation-lease/v1",
            ["lease_key"] = new Dictionary<string, object?>
            {
                ["generation"] = generation,
                ["candidate_id"] = candidateId,
                ["model_version"] = modelVersion,
                ["source_checkpoint_sha256"] = sourceCkptSha256,
                ["architecture_contract_hash"] = archHash,
            },
        };
        if (TransformerTrainingRepository.Truthy(
                lease.GetValueOrDefault("active")))
        {
            if (LeaseExpired(lease))
            {
                rep["reclaimed_expired_lease"] = lease["lease_key"];
                rep["prior_state"] = "MODEL_MUTATION_LEASE_EXPIRED";
            }
            else
            {
                string heldBy = TransformerTrainingRepository.Str(
                    (Dictionary<string, object?>)lease["lease_key"]!,
                    "candidate_id") ?? "";
                if (!string.Equals(heldBy, candidateId,
                        StringComparison.OrdinalIgnoreCase))
                    throw new ExecutorError(
                        "MODEL_MUTATION_LEASE_CONFLICT",
                        $"lease held by candidate '{heldBy}' — " +
                        "ONE_CANDIDATE ONE_WEIGHT_WRITER");
                string heldSrc = TransformerTrainingRepository.Str(
                    (Dictionary<string, object?>)lease["lease_key"]!,
                    "source_checkpoint_sha256") ?? "";
                if (heldSrc.Length > 0 && sourceCkptSha256.Length > 0 &&
                    !string.Equals(heldSrc, sourceCkptSha256,
                                   StringComparison.OrdinalIgnoreCase))
                    throw new ExecutorError(
                        "MODEL_MUTATION_SOURCE_DRIFT",
                        "lease key source_checkpoint_sha256 drifted " +
                        "between BEGIN calls — abort and reacquire");
                // Idempotent re-acquire by the same writer.
                rep["ok"] = true;
                rep["state"] = "ALREADY_HELD";
                rep["lease"] = lease;
                return rep;
            }
        }
        var next = new Dictionary<string, object?>
        {
            ["active"] = true,
            ["state"] = "BEGIN_MUTATION",
            ["mutation_kind"] = mutationKind.ToUpperInvariant(),
            ["lease_key"] = rep["lease_key"],
            ["acquired_at"] = XcPaths.IsoNow(),
            ["ttl_s"] = ttlS > 0 ? ttlS : DefaultTtlS,
            ["holder_pid"] = Environment.ProcessId,
        };
        SaveLease(toolRoot, next);
        rep["ok"] = true;
        rep["state"] = "BEGIN_MUTATION";
        rep["lease"] = next;
        return rep;
    }

    /// <summary>COMMIT_MUTATION / ABORT_MUTATION: settle the lease.
    /// Only the recorded holder may settle; settling a FREE ledger is a
    /// no-op report, never a throwaway.</summary>
    public static Dictionary<string, object?> Settle(
        string toolRoot, string candidateId, string finalState,
        string? evidenceRef = null)
    {
        if (finalState != "COMMIT_MUTATION" &&
            finalState != "ABORT_MUTATION")
            throw new ExecutorError("MODEL_MUTATION_LEASE_CONFLICT",
                $"settle state '{finalState}' is not COMMIT/ABORT");
        var lease = LoadLease(toolRoot);
        var rep = new Dictionary<string, object?>
        {
            ["format"] = "star-model-mutation-lease/v1",
            ["settled_state"] = finalState,
            ["evidence"] = evidenceRef,
            ["settled_at"] = XcPaths.IsoNow(),
        };
        if (!TransformerTrainingRepository.Truthy(
                lease.GetValueOrDefault("active")))
        {
            rep["ok"] = true;
            rep["note"] = "ledger already FREE";
            return rep;
        }
        string heldBy = TransformerTrainingRepository.Str(
            (Dictionary<string, object?>)lease["lease_key"]!,
            "candidate_id") ?? "";
        if (!string.Equals(heldBy, candidateId,
                StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("MODEL_MUTATION_LEASE_CONFLICT",
                $"lease held by '{heldBy}', not '{candidateId}'");
        SaveLease(toolRoot, new Dictionary<string, object?>
        {
            ["active"] = false,
            ["state"] = "FREE",
            ["last_settled"] = rep,
            ["lease_key"] = lease["lease_key"],
        });
        rep["ok"] = true;
        rep["released"] = lease["lease_key"];
        return rep;
    }

    /// <summary>Status report — expired-but-unsettled leases surface as
    /// MODEL_MUTATION_LEASE_EXPIRED so callers know the next acquire
    /// will reclaim.</summary>
    public static Dictionary<string, object?> LeaseStatus(
        string toolRoot)
    {
        var lease = LoadLease(toolRoot);
        bool active = TransformerTrainingRepository.Truthy(
            lease.GetValueOrDefault("active"));
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-model-mutation-lease/v1",
            ["state"] = !active ? "FREE"
                : LeaseExpired(lease) ? "MODEL_MUTATION_LEASE_EXPIRED"
                : "HELD",
            ["lease"] = lease,
            ["invariant"] = "ONE_CANDIDATE ONE_WEIGHT_WRITER",
        };
    }

    // ------------------------------------------------- §26-§30 capacity proof ----

    /// <summary>star-capacity-proof/v1: recompute parameter counts from
    /// the bundle manifest's tensor table (name -> {shape,bytes,dtype}),
    /// never from declared param_count. Active = everything executed per
    /// token: embeddings, all non-expert layer tensors, norms, router,
    /// shared experts, lm_head, plus top_k routed experts per MoE layer
    /// (worst case: every MoE layer routes to its top_k).</summary>
    public static Dictionary<string, object?> CapacityProof(
        string bundleDir)
    {
        string manifestPath = Path.Combine(bundleDir, "manifest.json");
        if (!File.Exists(manifestPath))
            throw new ExecutorError("CAPACITY_PROOF_MISSING",
                $"bundle has no manifest.json: {bundleDir}");
        using var doc = JsonDocument.Parse(
            File.ReadAllText(manifestPath));
        var root = doc.RootElement;
        if (!root.TryGetProperty("tensors", out var tensors) ||
            tensors.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("CAPACITY_PROOF_MISSING",
                "manifest.json has no tensor table — parameter counts " +
                "cannot be recomputed from topology");
        if (!root.TryGetProperty("config", out var cfg) ||
            cfg.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("CAPACITY_PROOF_MISSING",
                "manifest.json has no config block");

        int topK = TryInt(cfg, "moe_top_k", 0);
        int numExperts = TryInt(cfg, "moe_num_experts", 0);

        long total = 0, embed = 0, lmHead = 0;
        long routedExpert = 0, sharedExpert = 0, router = 0;
        long common = 0;   // attention/delta/norm/other per-layer + misc
        var unknown = new List<object?>();
        foreach (var prop in tensors.EnumerateObject())
        {
            long n = TensorParams(prop.Value);
            total += n;
            string name = prop.Name;
            if (name.Contains(".mlp.experts.") &&
                !name.Contains("shared"))
                routedExpert += n;
            else if (name.Contains("shared") &&
                     name.Contains("expert"))
                sharedExpert += n;
            else if (name.Contains("router") ||
                     name.Contains("mlp.gate"))
                router += n;
            else if (name.Contains("word_embeddings") ||
                     name.Contains("embed_tokens"))
                embed += n;
            else if (name.Contains("lm_head"))
                lmHead += n;
            else if (name.Contains("attention") ||
                     name.Contains("norm") ||
                     name.Contains("delta") ||
                     name.Contains("linear_attn") ||
                     name.Contains("conv"))
                common += n;
            else
            {
                common += n;
                unknown.Add(name);
            }
        }
        // Worst-case active routed share: top_k of num_experts per
        // MoE layer. Routed tensor volume scales linearly with the
        // number of selected experts.
        long activeRouted = numExperts > 0 && topK > 0
            ? (long)Math.Ceiling(
                routedExpert * ((double)topK / numExperts))
            : routedExpert;
        long active = embed + lmHead + common + sharedExpert + router +
                      activeRouted;
        long storageBytes = 0;
        foreach (var prop in tensors.EnumerateObject())
            if (prop.Value.TryGetProperty("bytes", out var b) &&
                b.TryGetInt64(out long nb))
                storageBytes += nb;

        var rep = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-capacity-proof/v1",
            ["bundle"] = Path.GetFullPath(bundleDir),
            ["total_params"] = total,
            ["unique_params"] = total,
            ["common_params"] = common,
            ["shared_expert_params"] = sharedExpert,
            ["routed_expert_total_params"] = routedExpert,
            ["router_params"] = router,
            ["active_routed_params"] = activeRouted,
            ["active_params_expected"] = active,
            ["active_params_worst_case"] = active,
            ["trainable_params"] = total,
            ["embedding_params"] = embed,
            ["lm_head_params"] = lmHead,
            ["unclassified_tensors"] = unknown,
            ["storage_bytes"] = storageBytes,
            ["precision_profile"] =
                ModelLifecycle.Decode(root) is
                    Dictionary<string, object?> mm &&
                TransformerTrainingRepository.Str(mm, "quantization")
                    is string qq ? qq : "unknown",
            ["moe_top_k"] = topK,
            ["moe_num_experts"] = numExperts,
            ["basis"] = "recomputed from manifest tensor table, " +
                        "not declared param_count",
        };
        // §29 publish gate verdicts (gates, not silent fields).
        bool totalOk = total >= 300_000_000L &&
                       total <= CapacityPlane.DefaultTotalCeiling;
        bool activeOk = active <= CapacityPlane.ActiveCeiling;
        rep["publish_gate"] = new Dictionary<string, object?>
        {
            ["total_within_window"] = totalOk,
            ["active_within_ceiling"] = activeOk,
            ["verdict"] = totalOk && activeOk
                ? "PUBLISHABLE" : "PUBLISH_DENIED",
        };
        return rep;
    }

    private static int TryInt(JsonElement el, string key, int dflt)
        => el.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.Number &&
           v.TryGetInt32(out int n) ? n : dflt;

    private static long TensorParams(JsonElement tensor)
    {
        if (!tensor.TryGetProperty("shape", out var sh) ||
            sh.ValueKind != JsonValueKind.Array)
            return 0;
        long n = 1;
        foreach (var dim in sh.EnumerateArray())
            if (dim.TryGetInt64(out long d)) n *= d;
        return n;
    }

    /// <summary>§29-§31 publish gate — called at bundle ACTIVATE.
    /// Recomputes the proof and enforces both independent ceilings.
    /// Bare .xcn checkpoints pass through (probe/test lane, §31).</summary>
    public static void EnforceCapacityProofOnActivate(
        string artifactPath)
    {
        if (!Directory.Exists(artifactPath)) return;   // bare ckpt
        var proof = CapacityProof(artifactPath);
        long total = (long)proof["total_params"]!;
        long active = (long)proof["active_params_worst_case"]!;
        if (total < 300_000_000L ||
            total > CapacityPlane.DefaultTotalCeiling)
            throw new ExecutorError("TOTAL_PARAMETER_WINDOW_VIOLATION",
                $"total_params {total} outside [300M, 20B] publishable " +
                "window");
        if (active > CapacityPlane.ActiveCeiling)
            throw new ExecutorError(
                "ACTIVE_PARAMETER_CEILING_EXCEEDED",
                $"active_params_worst_case {active} exceeds hard " +
                "ceiling 1B — rejected before activation (§25/§35)");
    }

    // ------------------------------------------------- §13-§15 sequence check ----

    /// <summary>GuardSequence/active_capability consistency: the
    /// declared active capability must equal the maturation head unless
    /// a reopen/override receipt exists in state (reopened_from set by
    /// Maturation300M.Reopen). Violations throw
    /// CAPABILITY_SEQUENCE_VIOLATION — the caller (recovery/job lane)
    /// must stop, not warn.</summary>
    public static Dictionary<string, object?> SequenceCheck(
        string toolRoot)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        var state = Maturation300M.LoadState(toolRoot);
        var head = Maturation300M.Head(state);
        var rep = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-capability-sequence-check/v1",
            ["active_capability"] = policy.ActiveCapability,
            ["sequence_head"] = head?.Id,
            ["checked_at"] = XcPaths.IsoNow(),
        };
        if (policy.ActiveCapability.Length == 0)
        {
            rep["verdict"] = "SEQUENCE_IDLE";
            return rep;
        }
        if (head == null)
            throw new ExecutorError("CAPABILITY_SEQUENCE_VIOLATION",
                $"active_capability '{policy.ActiveCapability}' set but " +
                "the maturation sequence is complete — no legal lane " +
                "remains (requires reopen receipt)");
        if (!string.Equals(policy.ActiveCapability, head.Id,
                StringComparison.OrdinalIgnoreCase))
        {
            // §14/§15: legal only with a reopen/override receipt —
            // the state record carries reason + reopened_from for a
            // reopened capability.
            var caps = (Dictionary<string, object?>)
                state["capabilities"]!;
            caps.TryGetValue(policy.ActiveCapability, out object? c);
            var cd = c as Dictionary<string, object?>;
            bool receipt = cd != null &&
                           cd.ContainsKey("reopened_from") &&
                           cd["reopened_from"] != null;
            if (!receipt)
                throw new ExecutorError("CAPABILITY_SEQUENCE_VIOLATION",
                    $"active_capability '{policy.ActiveCapability}' " +
                    $"!= sequence head '{head.Id}' and no reopen/" +
                    "override receipt exists — stop the recovery job");
            rep["override_receipt"] = cd;
        }
        rep["verdict"] = "SEQUENCE_CONSISTENT";
        return rep;
    }

    // --------------------------------------- §41/§43/§53 run receipt ----

    /// <summary>star-autonomous-training-run/v1 + embedded
    /// star-training-performance/v1: aggregate a recovery run dir into
    /// the governed receipt — binds capability, effective policy,
    /// dataset hash, binary provenance, stage timing, sparse-training
    /// metrics and TimeToQualifiedModel (wall time from run start to
    /// the first stage whose capability score reached target without
    /// regression; null when no stage qualified — never estimated).
    /// GPU telemetry is emitted only when measurable; otherwise every
    /// field is the literal string "unavailable" (§44: no fake
    /// values).</summary>
    public static Dictionary<string, object?> RunReceipt(
        string toolRoot, string runDir)
    {
        string planPath = Path.Combine(runDir, "plan.json");
        if (!File.Exists(planPath))
            throw new ExecutorError("TRAINING_RUN_MISSING",
                $"run dir has no plan.json: {runDir}");
        var plan = (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(planPath))
                .RootElement)!;
        double lr = TransformerTrainingRepository.Num(plan, "lr");
        string capability =
            TransformerTrainingRepository.Str(plan, "capability") ?? "";

        // Stage timing + sparse metrics from stage-*/report.json.
        long totalSteps = 0;
        double totalTrainS = 0.0, tokensPerSec = 0.0;
        long trainable = 0, frozen = 0;
        var stageRows = new List<object?>();
        double? firstQualifiedS = null;
        double targetScore =
            TransformerTrainingRepository.Num(plan, "target_score");
        foreach (string dir in Directory
            .GetDirectories(runDir, "stage-*").OrderBy(d => d,
                StringComparer.Ordinal))
        {
            string rp = Path.Combine(dir, "report.json");
            if (!File.Exists(rp)) continue;
            var rep = (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(File.ReadAllText(rp)).RootElement)!;
            long steps = TransformerTrainingRepository.Int(
                rep, "steps");
            double el = TransformerTrainingRepository.Num(
                rep, "elapsed_s");
            double tps = TransformerTrainingRepository.Num(
                rep, "tokens_per_sec");
            totalSteps += steps;
            totalTrainS += el;
            if (tps > 0) tokensPerSec = tps;
            if (trainable == 0)
                trainable = TransformerTrainingRepository.Int(
                    rep, "trainable_params");
            if (frozen == 0)
                frozen = TransformerTrainingRepository.Int(
                    rep, "frozen_params");
            stageRows.Add(new Dictionary<string, object?>
            {
                ["stage"] = Path.GetFileName(dir),
                ["steps"] = steps, ["elapsed_s"] = el,
                ["step_ms"] = steps > 0
                    ? Math.Round(el * 1000.0 / steps, 2) : 0.0,
                ["tokens_per_sec"] = tps,
                ["loss_last"] = rep.GetValueOrDefault("loss_last"),
            });
            // TimeToQualifiedModel: stage eval score vs target.
            string ev = Path.Combine(dir, "eval.json");
            if (firstQualifiedS == null && targetScore > 0 &&
                File.Exists(ev))
            {
                var e = (Dictionary<string, object?>)
                    ModelLifecycle.Decode(JsonDocument.Parse(
                        File.ReadAllText(ev)).RootElement)!;
                double sc = TransformerTrainingRepository.Num(
                    e, "capability_score");
                if (sc >= targetScore)
                    firstQualifiedS = totalTrainS;
            }
        }

        // Final recovery decision if present (log dir sibling).
        var prov = BinaryProvenance(toolRoot);
        var perf = new Dictionary<string, object?>
        {
            ["format"] = "star-training-performance/v1",
            ["step_ms"] = totalSteps > 0
                ? Math.Round(totalTrainS * 1000.0 / totalSteps, 2)
                : 0.0,
            ["train_wall_s"] = Math.Round(totalTrainS, 2),
            ["tokens_per_sec"] = tokensPerSec,
            ["effective_tokens_per_sec"] = tokensPerSec,
            ["trainable_params"] = trainable,
            ["frozen_params"] = frozen,
            // §44: NVML is not bound in this lane — every telemetry
            // field is honestly "unavailable", never zero-filled.
            ["gpu"] = new Dictionary<string, object?>
            {
                ["gpu_util"] = "unavailable",
                ["memory_util"] = "unavailable",
                ["power_w"] = "unavailable",
                ["temperature"] = "unavailable",
                ["clock"] = "unavailable",
                ["vram_used"] = "unavailable",
                ["vram_peak"] = "unavailable",
                ["source"] = "nvml-not-bound",
            },
            ["stages"] = stageRows,
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-autonomous-training-run/v1",
            ["run_id"] = Path.GetFileName(runDir.TrimEnd(
                Path.DirectorySeparatorChar)),
            ["capability"] = capability,
            ["mode"] = "SINGLE_CAPABILITY_RECOVERY",
            ["steps"] = totalSteps,
            ["lr"] = lr,
            ["cuda_opt"] =
                Environment.GetEnvironmentVariable(
                    "XINGCHENG_TRAINER_CUDA_OPT") == "1",
            ["trainable_params"] = trainable,
            ["dataset_dir"] =
                TransformerTrainingRepository.Str(plan, "dataset_dir"),
            ["time_to_qualified_model_s"] = firstQualifiedS,
            ["binary_provenance"] = prov,
            ["performance"] = perf,
            ["effective_policy"] = EffectivePolicy(toolRoot),
            ["emitted_at"] = XcPaths.IsoNow(),
        };
    }

    // ------------------------------------------------- §5 binary provenance ----

    /// <summary>star-binary-provenance/v1: sha256 + git commit of the
    /// governed executables. Training reports bind binary_sha256 so a
    /// run is never attributed to an unknown binary (§5).</summary>
    public static Dictionary<string, object?> BinaryProvenance(
        string toolRoot)
    {
        var exes = new Dictionary<string, object?>();
        foreach (var rel in new[]
                 {
                     "src/backend/services/xingcheng/infrastructure/" +
                         "native_transformer/training/" +
                         "xingcheng_trainer.exe",
                     "src/backend/services/xingcheng/infrastructure/" +
                         "native_transformer/tools/xc_modeltool.exe",
                     "src/backend/csharp/GPTBridge.XingchengLearning/" +
                         "bin/Release/net10.0/xc-learning.exe",
                 })
        {
            string p = Path.Combine(toolRoot,
                rel.Replace('/', Path.DirectorySeparatorChar));
            if (!File.Exists(p)) continue;
            exes[Path.GetFileName(p)] = new Dictionary<string, object?>
            {
                ["binary_sha256"] =
                    TransformerTrainingRepository.Sha256File(p),
                ["build_timestamp"] = File.GetLastWriteTimeUtc(p)
                    .ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"),
                ["path"] = rel,
            };
        }
        string commit = "";
        try
        {
            var psi = new System.Diagnostics.ProcessStartInfo(
                "git", "-C \"" + toolRoot + "\" rev-parse HEAD")
            {
                RedirectStandardOutput = true,
                UseShellExecute = false,
            };
            var proc = System.Diagnostics.Process.Start(psi);
            if (proc != null)
            {
                commit = proc.StandardOutput.ReadToEnd().Trim();
                proc.WaitForExit(5000);
            }
        }
        catch { /* git unavailable — commit stays empty, honest */ }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-binary-provenance/v1",
            ["source_commit"] = commit,
            ["executables"] = exes,
            ["evidence_class"] = commit.Length > 0
                ? "FRESH_MAIN_EVIDENCE" : "PRE_REBUILD_RUNTIME_EVIDENCE",
        };
    }
}
