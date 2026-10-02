// Maturation300M.cs — 300M Model Maturation governance plane.
//
// Implements the maturation-phase contracts:
//   §0/§1  Phase lock: the only production scale is 300m; the model core
//          stays HybridCausalDecoder / xc-fused-1 / XCN10.
//   §3     star-capability-baseline-300m/v1 — three disjoint capability
//          sections (MODEL / RUNTIME_AUGMENTED / SERVICE); never mixed.
//   §4-§14 Capability sequence: exactly one active capability at a
//          time, fixed order, freeze-after-gate, weight versions
//          300m-w<N>-<capability>.
//   §16    Thinking OFF is evaluated before ON; a base-model regression
//          restored by thinking still counts as a regression.
//   §53/§54 300M-L0..L7 maturity ladder — certified only by executed
//          tests, capped at the first failed/skipped level.
//   §62    Scale unlock: >300m candidates require 300M >= L6 plus
//          capability/runtime/convergence evidence.
//   §63/§64 One primary change class per candidate.
//   §66    star-hardware-baseline-300m/v1 metric contract.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class Maturation300M
{
    public const string StateFormat = "star-300m-maturation-state/v1";
    public const string BaselineFormat = "star-capability-baseline-300m/v1";
    public const string HwBaselineFormat = "star-hardware-baseline-300m/v1";
    public const string StateRel =
        "xingcheng/runtime/state/model-maturation-300m.json";
    public const string BaselineRel =
        "xingcheng/runtime/state/capability-baseline-300m.json";
    public const string HwBaselineRel =
        "xingcheng/runtime/state/hardware-baseline-300m.json";

    // §1 locked model core — a maturation artifact that disagrees with
    // any of these is not a 300M artifact.
    public const string ModelScale = "300m";
    public const string ArchitectureGeneration = "xc-fused-1";
    public const string CheckpointVersion = "XCN10";
    public const string ActiveGeneration = "gen-2-consolidated";

    public const string PhaseId = "300M_MODEL_MATURATION";
    /// <summary>Governor-stamped terminal phase: set by Complete() once
    /// every capability resolved. GuardSequence only enforces order
    /// while phase == PhaseId — a completed phase releases generic
    /// (capability-undeclared) SFT admission again.</summary>
    public const string PhaseComplete = "300M_MATURATION_COMPLETE";

    // §64 single primary change class per candidate.
    public static readonly string[] ChangeClasses =
        { "CAPABILITY_CHANGE", "RUNTIME_CHANGE", "DATA_CHANGE" };

    // --------------------------------------------------- §4-§14 sequence --

    internal sealed class CapabilitySpec
    {
        public required string Id;
        public required string WeightTag;      // 300m-w<N>-<tag>
        public required string EvalKind;       // primary eval surface
        // §16: thinking must be benchmarked OFF before ON.
        public bool ThinkingOffFirst;
        public required string[] Metrics;
    }

    /// <summary>The fixed maturation order — §4 "one capability at a
    /// time". Context tracking and multi-turn are distinct entries;
    /// structured_output precedes tool_calling (§9 vs §10); vision is
    /// last (§17); audio is out of scope (§18).</summary>
    public static readonly CapabilitySpec[] Sequence =
    {
        new() { Id = "instruction_following", WeightTag = "instruction",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "multi_constraint", "negative_constraint", "format",
                    "priority", "language", "length", "order",
                    "conditional", "ambiguous",
                } },
        new() { Id = "context_tracking", WeightTag = "context",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "context_state_accuracy", "context_update_accuracy",
                    "stale_context_error_rate",
                } },
        new() { Id = "multi_turn", WeightTag = "multiturn",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "dialogue_consistency", "intent_continuation",
                    "no_repeated_questions", "constraint_persistence",
                    "topic_switch", "topic_recovery",
                } },
        new() { Id = "structured_output", WeightTag = "structured",
                EvalKind = "structured",
                Metrics = new[]
                {
                    "json_valid", "schema_conformant", "typed_fields",
                    "enum_membership", "nested_objects", "arrays",
                } },
        new() { Id = "tool_calling", WeightTag = "tool",
                EvalKind = "tool_call",
                Metrics = new[]
                {
                    "tool_necessity", "tool_selection",
                    "argument_correctness", "result_interpretation",
                    "failure_recovery",
                } },
        new() { Id = "reading_grounding", WeightTag = "reading",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "document_qa", "multi_passage", "conflicting_evidence",
                    "insufficient_evidence", "citation_alignment",
                    "summarization", "fact_extraction",
                    "retrieval_failure_isolated",
                    "comprehension_failure_isolated",
                } },
        new() { Id = "rag", WeightTag = "rag",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "retrieval_necessity", "retrieval_quality",
                    "evidence_use", "citation_correctness",
                    "document_conflict", "revision_awareness",
                } },
        new() { Id = "math", WeightTag = "math",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "add_sub", "mul_div", "carry_borrow", "percentage",
                    "ratio", "parentheses", "simple_algebra",
                    "word_problem",
                } },
        new() { Id = "coding", WeightTag = "coding",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "syntax", "function", "unit_task", "fim", "bug_fix",
                    "small_multi_file",
                } },
        new() { Id = "native_thinking", WeightTag = "thinking",
                EvalKind = "capability", ThinkingOffFirst = true,
                Metrics = new[]
                {
                    "off_baseline", "accuracy_gain", "token_cost",
                    "gpu_cost", "branch_acceptance", "latency",
                } },
        new() { Id = "vision", WeightTag = "vision",
                EvalKind = "capability",
                Metrics = new[]
                {
                    "ocr", "document_screenshot", "simple_chart",
                    "object_relation", "basic_image_qa",
                } },
    };

    public static int IndexOf(string capability) =>
        Array.FindIndex(Sequence,
            s => string.Equals(s.Id, capability,
                               StringComparison.OrdinalIgnoreCase));

    public static string WeightVersionFor(int index) =>
        $"300m-w{index + 1}-{Sequence[index].WeightTag}";

    /// <summary>§51 regression matrix — every capability update must
    /// re-run this set; §52 adds the runtime side.</summary>
    public static readonly string[] RegressionCapabilities =
        Sequence.Select(s => s.Id).Append("system1").ToArray();

    public static readonly string[] RegressionRuntime =
        { "memory", "prefill", "decode", "cache", "state", "cuda",
          "mtp" };

    // --------------------------------------------------------- state --

    /// <summary>Load the maturation state; absent file = all capabilities
    /// pending and the sequence head (instruction_following) is the only
    /// capability any lane may activate. Fail-closed on corrupt state:
    /// a state file that fails to parse denies training rather than
    /// silently resetting progress.</summary>
    public static Dictionary<string, object?> LoadState(string toolRoot)
    {
        string path = Path.Combine(toolRoot, StateRel);
        if (!File.Exists(path))
            return FreshState();
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.ValueKind != JsonValueKind.Object ||
                !doc.RootElement.TryGetProperty("capabilities", out var caps) ||
                caps.ValueKind != JsonValueKind.Object)
                throw new ExecutorError("MATURATION_STATE_INVALID",
                    "maturation state missing capabilities object");
            var state = new Dictionary<string, object?>
            {
                ["format"] = StateFormat,
                // Preserve the stored phase stamp: Complete() writes
                // PhaseComplete and GuardSequence honors it. A file
                // without a phase field defaults to the active phase.
                ["phase"] =
                    doc.RootElement.TryGetProperty("phase", out var ph) &&
                    ph.ValueKind == JsonValueKind.String
                        ? ph.GetString() ?? PhaseId
                        : PhaseId,
                ["model_scale"] = ModelScale,
            };
            // Completion evidence fields ride along so a load+save cycle
            // (Freeze/Reopen/Complete) never drops the governor stamp.
            foreach (var k in new[] { "completed_at", "completed_reason" })
                if (doc.RootElement.TryGetProperty(k, out var cv) &&
                    cv.ValueKind == JsonValueKind.String)
                    state[k] = cv.GetString();
            var capsDict = new Dictionary<string, object?>();
            foreach (var p in caps.EnumerateObject())
                capsDict[p.Name] = ModelLifecycle.Decode(p.Value);
            state["capabilities"] = capsDict;
            return state;
        }
        catch (JsonException e)
        {
            throw new ExecutorError("MATURATION_STATE_CORRUPT",
                $"maturation state unreadable: {e.Message}");
        }
        catch (IOException e)
        {
            throw new ExecutorError("MATURATION_STATE_CORRUPT",
                $"maturation state unreadable: {e.Message}");
        }
    }

    private static Dictionary<string, object?> FreshState() =>
        new()
        {
            ["format"] = StateFormat,
            ["phase"] = PhaseId,
            ["model_scale"] = ModelScale,
            ["architecture_generation"] = ArchitectureGeneration,
            ["checkpoint_version"] = CheckpointVersion,
            ["capabilities"] = Sequence.ToDictionary(
                s => s.Id,
                s => (object?)new Dictionary<string, object?>
                {
                    ["status"] = "pending",
                    ["weight_version"] = null,
                    ["evidence"] = null,
                    ["frozen_at"] = null,
                }),
            ["created_at"] = XcPaths.IsoNow(),
        };

    public static void SaveState(string toolRoot,
                                 IReadOnlyDictionary<string, object?> state)
    {
        string path = Path.Combine(toolRoot, StateRel);
        var payload = new Dictionary<string, object?>(state)
        {
            ["format"] = StateFormat,
            ["updated_at"] = XcPaths.IsoNow(),
        };
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(payload) + "\n");
    }

    private static string StatusOf(Dictionary<string, object?> caps,
                                   string id)
    {
        if (!caps.TryGetValue(id, out object? raw) ||
            raw is not Dictionary<string, object?> cap)
            return "pending";
        return cap.TryGetValue("status", out object? s)
            ? s?.ToString() ?? "pending" : "pending";
    }

    /// <summary>The sequence head: first capability not yet resolved.
    /// "frozen" (gate passed) and "unsupported" (modality absent on a
    /// text-only bundle — fail-closed evidence, not a pass) both
    /// resolve the slot; "unsupported" never counts as a gated
    /// capability. Null when every capability is resolved.</summary>
    public static CapabilitySpec? Head(
        IReadOnlyDictionary<string, object?> state)
    {
        if (!state.TryGetValue("capabilities", out object? raw) ||
            raw is not Dictionary<string, object?> caps)
            return Sequence[0];
        foreach (var spec in Sequence)
        {
            string st = StatusOf(caps, spec.Id);
            if (st != "frozen" && st != "unsupported")
                return spec;
        }
        return null;
    }

    /// <summary>§4/§50 ordered-activation guard, invoked alongside the
    /// CapabilityFreeze policy guard at job admission: while the
    /// sequence is active the declared capability must equal the
    /// current sequence head — any other capability, including a later
    /// one whose prerequisites are not yet frozen, is denied. Once the
    /// sequence is complete (no head: every capability frozen or
    /// unsupported) the guard releases untagged governed SFT — the
    /// recorded completion order — while capability-declared jobs
    /// remain denied until --maturation-reopen reopens a bounded
    /// lane.</summary>
    public static void GuardSequence(string toolRoot, string capability)
    {
        var state = LoadState(toolRoot);
        // Phase-complete release: the ordered-activation guard governs
        // admission only while the maturation phase is active. Once the
        // governor stamps PhaseComplete (all capabilities resolved), SFT
        // jobs no longer carry a sequence obligation — including the
        // capability-undeclared self-learning cycle.
        if (!string.Equals(
                state.TryGetValue("phase", out object? ph)
                    ? ph?.ToString() : null,
                PhaseId, StringComparison.Ordinal))
            return;
        CapabilitySpec? head = Head(state);
        if (head == null)
        {
            // Sequence complete: every capability resolved (frozen or
            // unsupported). The recorded completed_reason releases
            // GuardSequence for governed post-maturation SFT — an
            // untagged job (the self-learning lane declares no
            // capability) is admitted; a capability-declared job is
            // denied until a governor reopens a bounded lane via
            // --maturation-reopen.
            if (capability.Length == 0)
                return;
            throw new ExecutorError("CAPABILITY_SEQUENCE_VIOLATION",
                $"capability '{capability}' is denied: the 300M " +
                "maturation sequence is complete; reopen a bounded " +
                "lane with --maturation-reopen");
        }
        if (IndexOf(capability) < 0)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{capability}' is not in the 300M " +
                "maturation sequence");
        if (!string.Equals(capability, head.Id,
                           StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("CAPABILITY_SEQUENCE_VIOLATION",
                $"capability '{capability}' is not the sequence head " +
                $"'{head.Id}'; earlier capabilities must reach frozen " +
                "first");
    }

    /// <summary>§6/§50 freeze the active capability after its gate
    /// evidence exists; records the governed weight version and stamps
    /// frozen_at. Freezing a non-head or without evidence fails.</summary>
    public static Dictionary<string, object?> Freeze(
        string toolRoot, string capability, string evidenceRef)
    {
        var state = LoadState(toolRoot);
        CapabilitySpec? head = Head(state);
        if (head == null || !string.Equals(capability, head.Id,
                StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("CAPABILITY_SEQUENCE_VIOLATION",
                $"cannot freeze '{capability}': sequence head is " +
                $"'{head?.Id ?? "none"}'");
        if (string.IsNullOrWhiteSpace(evidenceRef))
            throw new ExecutorError("MATURATION_EVIDENCE_MISSING",
                "freeze requires gate evidence (eval report ref)");
        var caps = (Dictionary<string, object?>)state["capabilities"]!;
        caps[capability] = new Dictionary<string, object?>
        {
            ["status"] = "frozen",
            ["weight_version"] = WeightVersionFor(IndexOf(capability)),
            ["evidence"] = evidenceRef,
            ["frozen_at"] = XcPaths.IsoNow(),
        };
        SaveState(toolRoot, state);
        CapabilitySpec? next = Head(state);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["frozen"] = capability,
            ["weight_version"] = WeightVersionFor(IndexOf(capability)),
            ["next_capability"] = next?.Id,
        };
    }

    /// <summary>Governor-ordered reopen of a frozen capability: the
    /// capability returns to "pending" and becomes the sequence head
    /// again so a new bounded lane may run. The prior freeze record is
    /// preserved under "history" — reopen never erases evidence.
    /// Requires a reason (governor order reference).</summary>
    public static Dictionary<string, object?> Reopen(
        string toolRoot, string capability, string reason)
    {
        var state = LoadState(toolRoot);
        if (IndexOf(capability) < 0)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{capability}' is not in the 300M " +
                "maturation sequence");
        if (string.IsNullOrWhiteSpace(reason))
            throw new ExecutorError("MATURATION_EVIDENCE_MISSING",
                "reopen requires a reason (governor order reference)");
        var caps = (Dictionary<string, object?>)state["capabilities"]!;
        object? prior = caps.TryGetValue(capability, out object? p)
            ? p : null;
        // Reopen re-arms the ordered lane: after a completed phase the
        // guard only stays consistent if the sequence is live again —
        // the reopened capability becomes head and GuardSequence
        // resumes enforcing admission order until it re-freezes.
        // The stale completion stamp is lifted with the phase flip —
        // history survives under the reopened capability's "prior".
        state["phase"] = PhaseId;
        state.Remove("completed_at");
        state.Remove("completed_reason");
        caps[capability] = new Dictionary<string, object?>
        {
            ["status"] = "pending",
            ["reopened_from"] =
                prior is Dictionary<string, object?> pd
                    ? pd.GetValueOrDefault("status")?.ToString() : null,
            ["prior"] = prior,
            ["reason"] = reason,
            ["reopened_at"] = XcPaths.IsoNow(),
        };
        SaveState(toolRoot, state);
        CapabilitySpec? next = Head(state);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["reopened"] = capability,
            ["head"] = next?.Id,
        };
    }

    /// <summary>Resolve the head capability as unsupported — used when
    /// the governed evidence itself proves the capability cannot be
    /// exercised on this bundle (e.g. every vision-suite item is
    /// skipped "modality-unavailable" on the text-only 300M bundle).
    /// Records the evidence and reason; stamps no weight version —
    /// "unsupported" is never a pass. Only the sequence head may be
    /// marked.</summary>
    public static Dictionary<string, object?> MarkUnsupported(
        string toolRoot, string capability, string evidenceRef,
        string reason)
    {
        var state = LoadState(toolRoot);
        CapabilitySpec? head = Head(state);
        if (head == null || !string.Equals(capability, head.Id,
                StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("CAPABILITY_SEQUENCE_VIOLATION",
                $"cannot mark '{capability}' unsupported: sequence "
                + $"head is '{head?.Id ?? "none"}'");
        if (string.IsNullOrWhiteSpace(evidenceRef))
            throw new ExecutorError("MATURATION_EVIDENCE_MISSING",
                "unsupported requires fail-closed evidence "
                + "(eval report ref)");
        if (string.IsNullOrWhiteSpace(reason))
            throw new ExecutorError("MATURATION_EVIDENCE_MISSING",
                "unsupported requires a reason");
        var caps = (Dictionary<string, object?>)state["capabilities"]!;
        caps[capability] = new Dictionary<string, object?>
        {
            ["status"] = "unsupported",
            ["reason"] = reason,
            ["evidence"] = evidenceRef,
            ["marked_at"] = XcPaths.IsoNow(),
        };
        SaveState(toolRoot, state);
        CapabilitySpec? next = Head(state);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["unsupported"] = capability,
            ["reason"] = reason,
            ["next_capability"] = next?.Id,
        };
    }

    /// <summary>Governor-ordered phase completion: stamps the state
    /// PhaseComplete once every capability resolved (Head == null),
    /// releasing GuardSequence for capability-undeclared lanes. All
    /// freeze/unsupported evidence is preserved verbatim — completion
    /// records a phase stamp, never erases history. Requires a reason
    /// (governor order reference); refuse while any capability is still
    /// pending — the sequence must earn completion, not be waved.</summary>
    public static Dictionary<string, object?> Complete(
        string toolRoot, string reason)
    {
        var state = LoadState(toolRoot);
        string phase = state.TryGetValue("phase", out object? ph)
            ? ph?.ToString() ?? PhaseId : PhaseId;
        if (!string.Equals(phase, PhaseId, StringComparison.Ordinal))
            return new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["already_complete"] = true,
                ["phase"] = phase,
            };
        CapabilitySpec? head = Head(state);
        if (head != null)
            throw new ExecutorError("MATURATION_INCOMPLETE",
                $"cannot complete the phase: sequence head is " +
                $"'{head.Id}'; freeze or mark-unsupported first");
        if (string.IsNullOrWhiteSpace(reason))
            throw new ExecutorError("MATURATION_EVIDENCE_MISSING",
                "phase completion requires a reason (governor order)");
        state["phase"] = PhaseComplete;
        state["completed_at"] = XcPaths.IsoNow();
        state["completed_reason"] = reason;
        SaveState(toolRoot, state);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["phase"] = PhaseComplete,
            ["reason"] = reason,
            ["capabilities_resolved"] =
                ((Dictionary<string, object?>)state["capabilities"]!).Count,
        };
    }

    /// <summary>§64 candidate classification — exactly one primary
    /// change class; a candidate mixing classes cannot attribute its
    /// result and is denied.</summary>
    public static void GuardCandidateClasses(IEnumerable<string> classes)
    {
        var distinct = classes.Distinct(StringComparer.OrdinalIgnoreCase)
                              .ToList();
        foreach (var c in distinct)
            if (!ChangeClasses.Contains(c, StringComparer.OrdinalIgnoreCase))
                throw new ExecutorError("CHANGE_CLASS_UNKNOWN",
                    $"unknown change class '{c}'");
        if (distinct.Count > 1)
            throw new ExecutorError("MIXED_CHANGE_CLASSES",
                "a candidate may carry exactly one primary change class " +
                "(capability OR runtime OR data), not " +
                string.Join("+", distinct));
    }

    // -------------------------------------------- §3 capability baseline --

    /// <summary>Section keys of star-capability-baseline-300m/v1 — the
    /// three scores are recorded under disjoint sections and must never
    /// be averaged into a single number.</summary>
    public static readonly string[] BaselineSections =
        { "MODEL_CAPABILITY", "RUNTIME_AUGMENTED_CAPABILITY",
          "SERVICE_CAPABILITY" };

    /// <summary>Assemble and persist the baseline artifact. Each section
    /// carries its own metrics + evidence; the artifact is immutable
    /// identity + provenance for the 300M generation, not a live score.</summary>
    public static Dictionary<string, object?> WriteBaseline(
        string toolRoot, string weightsRef, string weightsSha256,
        Dictionary<string, object?> modelCapability,
        Dictionary<string, object?> runtimeAugmented,
        Dictionary<string, object?> serviceCapability)
    {
        // §3: the three sections must not share metric keys — conflation
        // is a contract violation, not a style issue.
        foreach (var k in modelCapability.Keys)
            if (runtimeAugmented.ContainsKey(k) ||
                serviceCapability.ContainsKey(k))
                throw new ExecutorError("BASELINE_SCORE_CONFLATION",
                    $"metric '{k}' appears in more than one capability " +
                    "section");
        foreach (var k in runtimeAugmented.Keys)
            if (serviceCapability.ContainsKey(k))
                throw new ExecutorError("BASELINE_SCORE_CONFLATION",
                    $"metric '{k}' appears in more than one capability " +
                    "section");

        var artifact = new Dictionary<string, object?>
        {
            ["format"] = BaselineFormat,
            ["phase"] = PhaseId,
            ["model_scale"] = ModelScale,
            ["architecture_generation"] = ArchitectureGeneration,
            ["checkpoint_version"] = CheckpointVersion,
            ["weights_ref"] = weightsRef,
            ["weights_sha256"] = weightsSha256,
            ["MODEL_CAPABILITY"] = modelCapability,
            ["RUNTIME_AUGMENTED_CAPABILITY"] = runtimeAugmented,
            ["SERVICE_CAPABILITY"] = serviceCapability,
            ["created_at"] = XcPaths.IsoNow(),
        };
        string path = Path.Combine(toolRoot, BaselineRel);
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(artifact) + "\n");
        return artifact;
    }

    // ------------------------------------------------- §53 ladder --

    internal sealed class LadderGate
    {
        public required int Level;
        public required string Code;
        public required string Requirement;
    }

    /// <summary>§53 300M-L0..L7 — certification is decided only by
    /// executed tests; the first failed/skipped level caps the result
    /// (§54: parameter count is never the criterion).</summary>
    public static readonly LadderGate[] Ladder =
    {
        new() { Level = 0, Code = "model_loads",
                Requirement = "300M bundle loads; params finite" },
        new() { Level = 1, Code = "core_tests_pass",
                Requirement = "trainer probe suite green" },
        new() { Level = 2, Code = "basic_language_stable",
                Requirement = "basic language eval stable" },
        new() { Level = 3, Code = "instruction_context_stable",
                Requirement = "instruction + context capabilities " +
                              "frozen at target" },
        new() { Level = 4, Code = "rag_tool_structured_stable",
                Requirement = "structured/tool/reading/rag frozen" },
        new() { Level = 5, Code = "math_code_vision_usable",
                Requirement = "math/coding/vision suites pass" },
        new() { Level = 6, Code = "thinking_system1_mtp_mature",
                Requirement = "thinking OFF/ON evidence + System-1 " +
                              "certification + MTP drafter verified" },
        new() { Level = 7, Code = "production_certified",
                Requirement = "resource + lifecycle + audit + " +
                              "convergence gate fully certified" },
    };

    /// <summary>Reduce per-level results (true=pass,false=fail,null=
    /// skipped) to the certified level: consecutive passes from L0,
    /// capped at the first non-pass.</summary>
    public static int CertifiedLevel(IReadOnlyDictionary<int, bool?> results)
    {
        int level = -1;
        foreach (var g in Ladder)
        {
            if (!results.TryGetValue(g.Level, out bool? r) || r != true)
                break;
            level = g.Level;
        }
        return level;
    }

    /// <summary>§62 scale unlock predicate — >300m candidates require
    /// L6+ and a fully frozen capability sequence.</summary>
    public static bool ScaleUnlocked(IReadOnlyDictionary<int, bool?> results,
                                     IReadOnlyDictionary<string, object?> state)
        => CertifiedLevel(results) >= 6 && Head(state) == null;

    // ------------------------------------------------- §66 hardware --

    /// <summary>Required fields of star-hardware-baseline-300m/v1 —
    /// every later optimization compares against this record (§67).</summary>
    public static readonly string[] HwBaselineFields =
    {
        "vram_peak_bytes", "ram_peak_bytes", "cpu_utilization",
        "gpu_utilization", "prefill_tps", "decode_tps", "ttft_ms",
        "itl_ms", "training_tokens_per_sec", "power_watts",
    };

    /// <summary>Validate a hardware baseline record — all §66 fields
    /// present (power may be null when unavailable); missing fields
    /// fail closed because a partial baseline enables cherry-picked
    /// comparisons.</summary>
    public static void ValidateHwBaseline(JsonElement root)
    {
        foreach (var f in HwBaselineFields)
        {
            if (!root.TryGetProperty(f, out var v))
                throw new ExecutorError("HW_BASELINE_FIELD_MISSING",
                    $"hardware baseline missing '{f}'");
            // Sensor-gated fields may be null — an absent sensor is
            // reported honestly, never fabricated: power_watts (no
            // meter), gpu_utilization (no NVML on this lane),
            // training_tokens_per_sec (filled by --train-report).
            if (f is "power_watts" or "gpu_utilization" or
                     "training_tokens_per_sec" &&
                v.ValueKind == JsonValueKind.Null)
                continue;
            if (v.ValueKind != JsonValueKind.Number)
                throw new ExecutorError("HW_BASELINE_FIELD_INVALID",
                    $"hardware baseline field '{f}' must be numeric");
        }
    }

    // --------------------------------------------- §15/§16 thinking --

    /// <summary>§15/§16 thinking OFF/ON comparison gate. Inputs are
    /// star-capability-eval/v1 reports over the SAME suite:
    /// baselineOff = previous weights, thinking OFF;
    /// candidateOff = candidate weights, thinking OFF (the §16 baseline
    /// — evaluated first, always);
    /// candidateOn = candidate weights, thinking ON.
    /// Verdicts: MASKED_REGRESSION (base-model drop recovered by
    /// thinking — still a regression), OFF_REGRESSION (drop thinking
    /// cannot explain), GAIN (real improvement from ON over OFF with no
    /// OFF regression), NO_GAIN (ON is not better than OFF — per §15
    /// "only actual gains are used"), NEUTRAL.</summary>
    public static Dictionary<string, object?> ThinkingCompare(
        JsonElement baselineOff, JsonElement candidateOff,
        JsonElement candidateOn, JsonElement cost)
    {
        var bo = PassRates(baselineOff, "baseline_off");
        var co = PassRates(candidateOff, "candidate_off");
        var cn = PassRates(candidateOn, "candidate_on");
        var cats = bo.Keys.Union(co.Keys).Union(cn.Keys)
                     .OrderBy(k => k, StringComparer.Ordinal).ToList();
        var regressions = new List<object?>();
        var masked = new List<object?>();
        var gains = new List<object?>();
        foreach (var cat in cats)
        {
            double b = bo.GetValueOrDefault(cat, double.NaN);
            double cOff = co.GetValueOrDefault(cat, double.NaN);
            double cOn = cn.GetValueOrDefault(cat, double.NaN);
            if (double.IsNaN(b) || double.IsNaN(cOff)) continue;
            double offDelta = cOff - b;
            if (offDelta < -1e-9)
            {
                var rec = new Dictionary<string, object?>
                {
                    ["category"] = cat,
                    ["baseline_off"] = b,
                    ["candidate_off"] = cOff,
                    ["delta"] = offDelta,
                };
                regressions.Add(rec);
                if (!double.IsNaN(cOn) && cOn >= b - 1e-9)
                    masked.Add(rec);   // §16: recovery ≠ no regression
            }
            if (!double.IsNaN(cOn) && cOn - cOff > 1e-9)
                gains.Add(new Dictionary<string, object?>
                {
                    ["category"] = cat,
                    ["off"] = cOff, ["on"] = cOn,
                    ["accuracy_gain"] = cOn - cOff,
                });
        }
        string verdict =
            masked.Count > 0 ? "MASKED_REGRESSION" :
            regressions.Count > 0 ? "OFF_REGRESSION" :
            gains.Count > 0 ? "GAIN" : "NO_GAIN";
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-thinking-compare/v1",
            ["protocol"] =
                "OFF evaluated before ON; thinking can never mask a "
                + "base-model regression (§15-§16)",
            ["verdict"] = verdict,
            ["off_regressions"] = regressions,
            ["masked_regressions"] = masked,
            ["on_gains"] = gains,
            ["cost"] = cost.ValueKind == JsonValueKind.Object
                ? ModelLifecycle.Decode(cost) : null,
            ["promotable"] = verdict == "GAIN" || verdict == "NO_GAIN",
        };
    }

    private static Dictionary<string, double> PassRates(
        JsonElement report, string tag)
    {
        if (!report.TryGetProperty("categories", out var cats) ||
            cats.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("THINKING_COMPARE_INVALID",
                $"{tag}: report missing categories");
        var map = new Dictionary<string, double>(StringComparer.Ordinal);
        foreach (var p in cats.EnumerateObject())
        {
            if (p.Value.ValueKind == JsonValueKind.Object &&
                p.Value.TryGetProperty("pass_rate", out var r) &&
                r.ValueKind == JsonValueKind.Number)
                map[p.Name] = r.GetDouble();
        }
        return map;
    }
}
