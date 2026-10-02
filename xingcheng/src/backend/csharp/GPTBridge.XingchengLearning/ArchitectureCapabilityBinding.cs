// ArchitectureCapabilityBinding.cs —
// ``star-architecture-capability-binding/v1`` (capability unification
// directive §31-§37, §86-§87).
//
// The architecture-side half of the capability chain
// (Registry → Graph → Binding → Evidence): where a descriptor says
// *that* a capability is carried by components, this module owns the
// canonical component vocabulary those names resolve against and the
// governed projection of every binding on the single xc-fused-1
// surface. A bound component is a name in one vocabulary — unknown
// tokens fail closed (``binding_component_unknown``) so a capability
// can never silently imply a second model core, runtime, store or
// scheduler (§0-§2, §96).
//
// Two read APIs serve admission lanes (self-learning, recovery,
// Multi-Lane): ``AdmissionCheck`` composes resolve → REQUIRES
// closure → binding integrity into one fail-closed answer, and
// ``SharedComponents`` reports the architecture-side blast radius —
// every other capability bound to a component the lane would train.

using System.Security.Cryptography;
using System.Text;

namespace GPTBridge.XingchengLearning;

internal static class ArchitectureCapabilityBinding
{
    public const string Format = "star-architecture-capability-binding/v1";
    public const string Rel =
        "xingcheng/runtime/state/architecture-capability-binding.json";

    // ------------------------------------------------- vocabulary ---
    // §31 closed component vocabularies. A binding may only ever name
    // these tokens; extending a capability's architecture surface is a
    // conscious vocabulary change here, never a seed-row typo.

    /// <summary>Model-side carriers: parts of the xc-fused-1 weight
    /// surface a capability may bind. The core topology itself is
    /// never a legal value — capabilities ride the core (§6).</summary>
    public static readonly string[] ModelComponents =
    {
        "model_weights", "delta_state", "full_attention",
        "position_encoding", "kv_cache", "dialogue_state",
        "tool_schema", "moe_router", "shared_experts",
        "routed_experts", "vision_encoder", "early_fusion",
        "fim_head", "hidden_state_feedback", "system1_path",
        "thinking_path", "native_thinking_path", "mtp_head",
        "instruction_curriculum",
    };

    /// <summary>Runtime/service carriers: execution surfaces of the
    /// single NativeInferenceEngine or governed host services (§7/§8).</summary>
    public static readonly string[] RuntimeComponents =
    {
        "native_inference_engine", "structured_output_validator",
        "runtime_validator", "tool_schema_parser",
        "reasoning_execution_policy", "rag_engine",
        "retrieval_adapter", "kv_paging", "prefix_cache",
        "speculative_decoder", "vision_runtime",
        // service-only: only SERVICE_AUGMENTED rows may bind these.
        "tool_host", "main_system_resource_governor",
    };

    /// <summary>Service-surface tokens no MODEL_NATIVE or
    /// RUNTIME_AUGMENTED row may ever claim (§8: external results are
    /// never a model-internal ability).</summary>
    public static readonly string[] ServiceOnlyComponents =
        { "tool_host", "main_system_resource_governor" };

    /// <summary>Tokens that name architecture itself — a capability
    /// binding one is capability/architecture confusion (§86).</summary>
    public static readonly string[] CoreTokens =
        { "hybridcausaldecoder", "xc-fused-1", "model_core" };

    /// <summary>Data-component suffix vocabulary — dataset classes
    /// are named positions, not enumerated rows.</summary>
    private static readonly string[] DataSuffixes =
        { "_dataset", "_corpus", "_curriculum", "_traces" };

    /// <summary>Non-``eval:`` eval components (validators/probes
    /// living on the runtime surface).</summary>
    private static readonly string[] EvalComponents =
    {
        "schema_validator", "tool_schema_parser",
        "grounded_claim_validator", "runtime_correctness_probe",
    };

    private static string Path_(string toolRoot) =>
        Path.Combine(toolRoot, Rel.Replace('/', Path.DirectorySeparatorChar));

    private static string Normalize(string token) =>
        (token ?? "").Trim().ToLowerInvariant();

    /// <summary>Channel-aware component membership test.</summary>
    public static bool KnownComponent(string channel, string token)
    {
        string t = Normalize(token);
        return channel switch
        {
            "model_components" => ModelComponents.Contains(t),
            "runtime_components" => RuntimeComponents.Contains(t),
            "data_components" =>
                DataSuffixes.Any(sfx => t.EndsWith(sfx,
                    StringComparison.Ordinal)),
            "eval_components" =>
                t.StartsWith("eval:", StringComparison.Ordinal) ||
                EvalComponents.Contains(t),
            _ => false,
        };
    }

    // ----------------------------------------------------- emit -----

    /// <summary>Content-bound binding fingerprint: sha256 over the
    /// canonical JSON of one row's four component channels — drift in
    /// any binding is detectable without re-running evals.</summary>
    public static string BindingHash(CapabilityBinding b)
    {
        var body = new SortedDictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["model_components"] =
                b.ModelComponents.OrderBy(s => s,
                     StringComparer.Ordinal).ToArray(),
            ["runtime_components"] =
                b.RuntimeComponents.OrderBy(s => s,
                     StringComparer.Ordinal).ToArray(),
            ["data_components"] =
                b.DataComponents.OrderBy(s => s,
                     StringComparer.Ordinal).ToArray(),
            ["eval_components"] =
                b.EvalComponents.OrderBy(s => s,
                     StringComparer.Ordinal).ToArray(),
        };
        string canonical = CanonicalJson.Canonical(
            ModelLifecycle.Encode(body));
        return Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
    }

    /// <summary>Emit and persist the governed binding projection:
    /// per-capability rows, the reverse component index and the
    /// shared-component blast-radius summary.</summary>
    public static Dictionary<string, object?> Emit(string toolRoot)
    {
        var rows = new List<object?>();
        var index = new SortedDictionary<string, List<string>>(
            StringComparer.Ordinal);
        var findings = Check();
        var bad = findings
            .Select(f => (Dictionary<string, object?>)f!)
            .Where(f => f["capability_id"]?.ToString() is { Length: > 0 })
            .Select(f => f["capability_id"]!.ToString()!)
            .ToHashSet(StringComparer.Ordinal);
        foreach (var d in CapabilityRegistry.Canonical)
        {
            var b = d.Binding;
            rows.Add(new Dictionary<string, object?>
            {
                ["capability_id"] = d.CapabilityId,
                ["capability_class"] = d.CapabilityClass,
                ["binding"] = b.ToDict(),
                ["binding_hash"] = BindingHash(b),
                ["ok"] = !bad.Contains(d.CapabilityId),
            });
            foreach (var (channel, comps) in new (string, string[])[]
            {
                ("model", b.ModelComponents),
                ("runtime", b.RuntimeComponents),
                ("data", b.DataComponents),
                ("eval", b.EvalComponents),
            })
                foreach (var c in comps)
                {
                    string key = channel + ":" + Normalize(c);
                    if (!index.TryGetValue(key, out var l))
                        index[key] = l = new List<string>();
                    l.Add(d.CapabilityId);
                }
        }
        var componentIndex = index.ToDictionary(
            kv => kv.Key,
            kv => (object?)kv.Value
                .OrderBy(s => s, StringComparer.Ordinal)
                .Cast<object?>().ToList());
        var shared = componentIndex
            .Where(kv => ((List<object?>)kv.Value!).Count > 1)
            .ToDictionary(kv => kv.Key, kv => kv.Value);
        var doc = new Dictionary<string, object?>
        {
            ["ok"] = findings.Count == 0,
            ["format"] = Format,
            ["generated_at"] = XcPaths.IsoNow(),
            ["architecture_generation"] =
                ArchitectureTaxonomy.CanonicalArchitecture,
            ["model_core"] = ArchitectureTaxonomy.ModelCore,
            ["capability_count"] = CapabilityRegistry.Canonical.Length,
            ["bindings"] = rows,
            ["component_index"] = componentIndex,
            ["shared_components"] = shared,
            ["finding_count"] = findings.Count,
            ["findings"] = findings,
        };
        ModelLifecycle.AtomicWrite(
            Path_(toolRoot), CanonicalJson.PrettyDict(doc) + "\n");
        doc["binding_file"] = Rel;
        return doc;
    }

    // ------------------------------------------------ validation ---

    /// <summary>Every §86/§87 binding violation class, fail-closed:
    /// incomplete binding, unknown component token, service-surface
    /// claims by non-service rows, and capability/architecture
    /// confusion (a bound core token or experimental architecture).</summary>
    public static List<object?> Check()
    {
        var findings = new List<object?>();
        void Flag(string cls, string id, string detail) =>
            findings.Add(new Dictionary<string, object?>
            {
                ["class"] = cls,
                ["capability_id"] = id,
                ["detail"] = detail,
            });
        foreach (var d in CapabilityRegistry.Canonical)
        {
            var b = d.Binding;
            bool service = d.CapabilityClass == "SERVICE_AUGMENTED";
            bool complete = service
                ? b.RuntimeComponents.Length > 0
                : b.Complete;
            if (!complete)
                Flag("binding_incomplete", d.CapabilityId,
                     "binding lacks required components");
            // §8: external-service rows never bind model surface.
            if (service && b.ModelComponents.Length > 0)
                Flag("cross_plane_claim", d.CapabilityId,
                     "SERVICE_AUGMENTED binds model components");
            foreach (var (channel, comps) in new (string, string[])[]
            {
                ("model_components", b.ModelComponents),
                ("runtime_components", b.RuntimeComponents),
                ("data_components", b.DataComponents),
                ("eval_components", b.EvalComponents),
            })
                foreach (var raw in comps)
                {
                    string c = Normalize(raw);
                    var axis = ArchitectureTaxonomy.Classify(c)
                        .PrimaryAxis;
                    if (CoreTokens.Contains(c) || axis == "MODEL_CORE")
                    {
                        Flag("capability_as_architecture",
                             d.CapabilityId,
                             $"component '{raw}' names architecture, " +
                             "not a carrier");
                        continue;
                    }
                    // The binding name collides with an experimental-
                    // architecture term (§86 vocabulary confusion) —
                    // e.g. ``speculative_decoder`` shadows the draft-
                    // model research token while the canonical carrier
                    // is ``mtp_drafter``. The carrier exists; the name
                    // must be converged.
                    if (axis == "EXPERIMENTAL_ARCHITECTURE")
                        Flag("binding_name_conflict", d.CapabilityId,
                             $"component '{raw}' collides with an " +
                             "experimental-architecture term — " +
                             "rename to the canonical carrier token");
                    if (!service &&
                        ServiceOnlyComponents.Contains(c))
                        Flag("cross_plane_claim", d.CapabilityId,
                             $"non-service capability binds " +
                             $"service surface '{raw}'");
                    if (!KnownComponent(channel, c))
                        Flag("binding_component_unknown",
                             d.CapabilityId,
                             $"{channel} token '{raw}' is not in the " +
                             "canonical component vocabulary");
                }
        }
        return findings;
    }

    // ------------------------------------------------- read APIs ----

    /// <summary>Components bound by this capability that are also
    /// bound by other capabilities — the architecture-side blast
    /// radius corroborating the graph's REGRESSES_WITH edges.</summary>
    public static Dictionary<string, string[]> SharedComponents(
        string capabilityId)
    {
        var d = CapabilityRegistry.Get(capabilityId)
            ?? throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{capabilityId}' is not in the registry");
        var result = new Dictionary<string, string[]>(
            StringComparer.Ordinal);
        var others = CapabilityRegistry.Canonical
            .Where(o => o.CapabilityId != d.CapabilityId).ToArray();
        foreach (var (channel, comps) in new (string, string[])[]
        {
            ("model", d.Binding.ModelComponents),
            ("runtime", d.Binding.RuntimeComponents),
            ("data", d.Binding.DataComponents),
            ("eval", d.Binding.EvalComponents),
        })
            foreach (var c in comps)
            {
                string n = Normalize(c);
                var sharers = others
                    .Where(o => o.Binding.ModelComponents
                                    .Select(Normalize).Contains(n) ||
                                o.Binding.RuntimeComponents
                                    .Select(Normalize).Contains(n) ||
                                o.Binding.DataComponents
                                    .Select(Normalize).Contains(n) ||
                                o.Binding.EvalComponents
                                    .Select(Normalize).Contains(n))
                    .Select(o => o.CapabilityId)
                    .OrderBy(s => s, StringComparer.Ordinal).ToArray();
                if (sharers.Length > 0)
                    result[channel + ":" + n] = sharers;
            }
        return result;
    }

    /// <summary>Unified lane admission answer (self-learning /
    /// recovery / Multi-Lane): resolve the name, expand the REQUIRES
    /// closure, and verify this capability and every prerequisite
    /// carry complete, vocabulary-clean bindings. Unknown names throw
    /// CAPABILITY_UNKNOWN — a lane never runs on an unresolved
    /// string.</summary>
    public static Dictionary<string, object?> AdmissionCheck(
        string capabilityId)
    {
        var d = CapabilityRegistry.Get(capabilityId)
            ?? throw new ExecutorError("CAPABILITY_UNKNOWN",
                $"capability '{capabilityId}' is not in the " +
                "CapabilityRegistry");
        string[] closure = CapabilityGraph.RequiresClosure(
            d.CapabilityId);
        var findings = Check();
        var involved = new HashSet<string>(StringComparer.Ordinal)
            { d.CapabilityId };
        foreach (var c in closure) involved.Add(c);
        var relevant = findings
            .Select(f => (Dictionary<string, object?>)f!)
            .Where(f => involved.Contains(
                f["capability_id"]?.ToString() ?? ""))
            .ToList<object?>();
        return new Dictionary<string, object?>
        {
            ["ok"] = relevant.Count == 0,
            ["format"] = Format,
            ["capability_id"] = d.CapabilityId,
            ["capability_class"] = d.CapabilityClass,
            ["requires_closure"] =
                closure.Cast<object?>().ToList(),
            ["binding"] = d.Binding.ToDict(),
            ["binding_hash"] = BindingHash(d.Binding),
            ["shared_components"] = SharedComponents(d.CapabilityId)
                .ToDictionary(kv => kv.Key,
                              kv => (object?)kv.Value
                                  .Cast<object?>().ToList()),
            ["findings"] = relevant,
        };
    }
}
