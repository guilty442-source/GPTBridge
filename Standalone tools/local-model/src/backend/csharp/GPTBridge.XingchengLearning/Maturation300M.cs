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
                ["phase"] = PhaseId,
                ["model_scale"] = ModelScale,
            };
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

    /// <summary>The sequence head: first capability not yet frozen.
    /// Null when every capability is frozen (maturation complete).</summary>
    public static CapabilitySpec? Head(
        IReadOnlyDictionary<string, object?> state)
    {
        if (!state.TryGetValue("capabilities", out object? raw) ||
            raw is not Dictionary<string, object?> caps)
            return Sequence[0];
        foreach (var spec in Sequence)
            if (StatusOf(caps, spec.Id) != "frozen")
                return spec;
        return null;
    }

    /// <summary>§4/§50 ordered-activation guard, invoked alongside the
    /// CapabilityFreeze policy guard at job admission: the declared
    /// capability must equal the current sequence head. Any other
    /// capability — including a later one whose prerequisites are not
    /// yet frozen — is denied.</summary>
    public static void GuardSequence(string toolRoot, string capability)
    {
        var state = LoadState(toolRoot);
        CapabilitySpec? head = Head(state);
        if (head == null)
            throw new ExecutorError("MATURATION_SEQUENCE_COMPLETE",
                "all capabilities frozen; no capability training lane " +
                "remains in 300M_MODEL_MATURATION");
        if (IndexOf(capability) < 0)
            throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{capability}' is not in the 300M " +
                "maturation sequence");
        if (!string.Equals(capability, head.Id,
                           StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("CAPABILITY_OUT_OF_SEQUENCE",
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
            throw new ExecutorError("CAPABILITY_OUT_OF_SEQUENCE",
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
            if (f == "power_watts") continue;   // may be null
            if (v.ValueKind != JsonValueKind.Number)
                throw new ExecutorError("HW_BASELINE_FIELD_INVALID",
                    $"hardware baseline field '{f}' must be numeric");
        }
    }
}
