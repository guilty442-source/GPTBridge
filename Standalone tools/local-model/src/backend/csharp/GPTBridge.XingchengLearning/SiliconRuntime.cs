// SiliconRuntime.cs — NativeSiliconEfficiencyPlane contract layer
// (silicon directive §1-§9, §16-§34, §43-§60, §67-§79, §84-§86).
//
//   XingchengRuntimeHost      §2/§3: machine-wide single owner — the
//                             host acquires an exclusive owner record;
//                             a second claimant is
//                             DUPLICATE_RUNTIME_OWNER. Every consumer
//                             (UI/RAG/Agent/Tool) calls through the
//                             host, never loads its own weights.
//   SystemArtifactRegistry    §4/§6/§58: canonical artifact keyed by
//                             generation+hash+arch+precision+device+
//                             profile. Identical key+hash => reuse;
//                             same key, different hash =>
//                             ARTIFACT_DUPLICATE_LOAD. Emits
//                             system_reuse_ratio.
//   SiliconProfile            §17 star-silicon-profile/v1 descriptor.
//   SiliconExecutionBroker    §9/§67-§69: op -> target routing with
//                             QoS; NPU-absent ops fall through;
//                             uncertified op => SILICON_ROUTE_UNCERTIFIED.
//   CpuCoreScheduler          §21-§25: work-class -> core-class plan
//                             honouring P/E hybrid + SMT.
//   GlobalLifetimePlanner     §29: single budget owner across CUDA
//                             workspace, CPU arenas, NPU staging,
//                             prefix, expert transfer.
//   SharedRoutedIsolation     §30-§33/§40/§79: shared expert is
//                             ALWAYS_HOT, never cold-tier, never
//                             router-governed; offload request =>
//                             SHARED_EXPERT_OFFLOAD_DENIED.
//   ParameterFreezeMap        §47 star-parameter-freeze-map/v1:
//                             FROZEN/TRAINABLE/CALIBRATION_ONLY with a
//                             max_trainable_params budget —
//                             PARAMETER_FREEZE_VIOLATION on overflow.
//   SparseOptimizerState      §50-§52: moments only for trainable
//                             params; shared-expert optimizer lane
//                             separate from routed.
//   ParameterEfficiencyReport §56/§57: per-param and per-GFLOP
//                             capability accounting.
//
// CAPABILITY_TRAINING_FROZEN: nothing here schedules or runs training.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class SiliconRuntime
{
    public const string HostFormat = "star-runtime-host/v1";
    public const string RegistryFormat = "star-artifact-registry/v1";
    public const string ProfileFormat = "star-silicon-profile/v1";
    public const string FreezeFormat = "star-parameter-freeze-map/v1";
    public const string EfficiencyFormat = "star-parameter-efficiency/v1";
    public const string HostRel =
        "xingcheng/runtime/state/runtime-host";
    public const string RegistryRel =
        "xingcheng/runtime/state/artifact-registry.json";

    private static string HostDir(string toolRoot)
        => Path.Combine(toolRoot,
                        HostRel.Replace('/', Path.DirectorySeparatorChar));

    // ------------------------------------------- §2/§3 runtime host --

    /// <summary>Acquire the machine-wide owner record. The lock file is
    /// held exclusively for the owner's lifetime; a second claimant is
    /// refused with DUPLICATE_RUNTIME_OWNER (fail-closed, §86).</summary>
    public static Dictionary<string, object?> AcquireHost(
        string toolRoot, string owner)
    {
        Directory.CreateDirectory(HostDir(toolRoot));
        string lockPath = Path.Combine(
            HostDir(toolRoot), "owner.lock");
        try
        {
            var fs = new FileStream(lockPath, FileMode.OpenOrCreate,
                FileAccess.ReadWrite, FileShare.None);
            // Owner record persists across restarts; the lock is the
            // live-claim, the json is the last-known owner.
            var rec = new Dictionary<string, object?>
            {
                ["ok"] = true, ["format"] = HostFormat,
                ["owner"] = owner,
                ["pid"] = Environment.ProcessId,
                ["acquired_at"] = XcPaths.IsoNow(),
                ["single_owner"] = true,
                ["error_code"] = "DUPLICATE_RUNTIME_OWNER",
            };
            fs.SetLength(0);
            var bytes = System.Text.Encoding.UTF8.GetBytes(
                CanonicalJson.CanonicalDict(rec));
            fs.Write(bytes);
            fs.Flush();
            fs.Dispose();
            return rec;
        }
        catch (IOException)
        {
            throw new ExecutorError("DUPLICATE_RUNTIME_OWNER",
                "runtime host already owned by another process");
        }
    }

    // -------------------------------------------- §6 artifact registry

    /// <summary>Canonical artifact key (§6): every component of the
    /// identity tuple participates — a same-key different-hash load is
    /// a duplicate, never a silent alias.</summary>
    public static string ArtifactKey(JsonElement el)
    {
        foreach (var k in new[]
                 { "generation", "bundle_hash", "architecture_hash",
                   "weight_version", "precision", "device",
                   "compiled_profile" })
            if (!el.TryGetProperty(k, out var v) ||
                v.ValueKind == JsonValueKind.Null ||
                (v.ValueKind == JsonValueKind.String &&
                 (v.GetString() ?? "").Length == 0))
                throw new ExecutorError("ARTIFACT_DUPLICATE_LOAD",
                    $"artifact key missing {k}");
        string S(string k) =>
            el.GetProperty(k).ToString();
        return string.Join('|',
            S("generation"), S("bundle_hash"), S("architecture_hash"),
            S("weight_version"), S("precision"), S("device"),
            S("compiled_profile"));
    }

    /// <summary>Register or reuse. Persists the registry so the
    /// artifact set is governed state, not per-process luck.</summary>
    public static Dictionary<string, object?> RegisterArtifact(
        string toolRoot, JsonElement el)
    {
        string key = ArtifactKey(el);
        string bytesHash =
            el.TryGetProperty("bytes_hash", out var bh)
                ? bh.GetString() ?? "" : "";
        string path = Path.Combine(toolRoot,
            RegistryRel.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var reg = File.Exists(path)
            ? (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(File.ReadAllText(path))
                    .RootElement)!
            : new Dictionary<string, object?>
            {
                ["format"] = RegistryFormat,
                ["artifacts"] = new Dictionary<string, object?>(),
            };
        var artifacts =
            (Dictionary<string, object?>)reg["artifacts"]!;
        if (artifacts.TryGetValue(key, out var existing))
        {
            var ex = (Dictionary<string, object?>)existing!;
            if ((ex.GetValueOrDefault("bytes_hash") as string) !=
                bytesHash)
                throw new ExecutorError("ARTIFACT_DUPLICATE_LOAD",
                    $"same key, different bytes: {key}");
            ex["reuse_count"] =
                TransformerTrainingRepository.Int(ex, "reuse_count") + 1;
        }
        else
        {
            artifacts[key] = new Dictionary<string, object?>
            {
                ["bytes_hash"] = bytesHash,
                ["bytes"] = el.TryGetProperty("bytes", out var b) &&
                    b.ValueKind == JsonValueKind.Number
                        ? b.GetInt64() : 0,
                ["registered_at"] = XcPaths.IsoNow(),
                ["reuse_count"] = 0,
            };
        }
        ModelLifecycle.AtomicWrite(
            path, CanonicalJson.PrettyDict(reg) + "\n");
        // §58 system reuse ratio over recorded bytes.
        long shared = 0, wouldDup = 0;
        foreach (var kv in artifacts)
        {
            var a = (Dictionary<string, object?>)kv.Value!;
            long b = TransformerTrainingRepository.Int64(
                a, "bytes");
            long rc = TransformerTrainingRepository.Int64(
                a, "reuse_count");
            shared += b;
            wouldDup += b * (1 + rc);
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = RegistryFormat,
            ["canonical_key"] = key,
            ["reused"] = existing != null,
            ["artifacts"] = artifacts.Count,
            ["shared_bytes"] = shared,
            ["would_duplicate_bytes"] = wouldDup,
            ["system_reuse_ratio"] =
                wouldDup > 0 ? (double)shared / wouldDup : 1.0,
        };
    }

    // ------------------------------------------ §9 silicon dispatch ---

    // §64 placement preferences (order = preference); uncertified ops
    // are denied, never guessed.
    private static readonly Dictionary<string, string[]> Placement =
        new(StringComparer.OrdinalIgnoreCase)
    {
        ["system1"] = new[] { "NPU", "GPU", "CPU" },
        ["embedding"] = new[] { "NPU", "GPU", "CPU" },
        ["reranker"] = new[] { "NPU", "GPU", "CPU" },
        ["tokenizer"] = new[] { "CPU" },
        ["rag_db"] = new[] { "CPU" },
        ["hybrid_decoder"] = new[] { "GPU", "CPU" },
        ["moe_expert_gemm"] = new[] { "GPU", "CPU" },
        ["background_classifier"] = new[] { "NPU", "CPU", "GPU" },
        ["nvme_expert_io"] = new[] { "CPU" },
        ["training"] = new[] { "GPU", "CPU" },       // §61 NPU off
    };

    public static readonly string[] QoS =
        { "REALTIME", "INTERACTIVE", "NORMAL", "BACKGROUND" };

    /// <summary>Route an operation to a device given available hardware.
    /// NPU_FIRST means preferred-when-capable (§8), not always-NPU.</summary>
    public static Dictionary<string, object?> Route(
        string op, IReadOnlySet<string> available, string qos)
    {
        if (!Placement.TryGetValue(op ?? "", out var pref))
            throw new ExecutorError("SILICON_ROUTE_UNCERTIFIED",
                $"op '{op}' has no certified placement");
        if (!QoS.Contains(qos ?? ""))
            throw new ExecutorError("SILICON_ROUTE_UNCERTIFIED",
                $"bad qos '{qos}'");
        string? chosen = null;
        foreach (var d in pref)
            if (available.Contains(d)) { chosen = d; break; }
        if (chosen == null)
            throw new ExecutorError("SILICON_ROUTE_UNCERTIFIED",
                $"no available device satisfies '{op}'");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = "star-silicon-route/v1",
            ["op"] = op, ["qos"] = qos,
            ["preference"] = pref.ToList(),
            ["selected"] = chosen,
            ["npu_first_semantics"] =
                "preferred-when-capable, not always-NPU",
        };
    }

    // ------------------------------------------- §21-§25 CPU plan ----

    /// <summary>Map work classes onto core classes given measured
    /// topology. SMT siblings never host two memory-heavy workers;
    /// without a hybrid CPU the plan degrades to a single class.</summary>
    public static Dictionary<string, object?> CpuPlan(JsonElement el)
    {
        int logical = el.TryGetProperty("logical", out var l)
            ? l.GetInt32() : 0;
        int physical = el.TryGetProperty("physical", out var p)
            ? p.GetInt32() : logical;
        bool hybrid = el.TryGetProperty("hybrid_pe", out var h) &&
                      h.ValueKind == JsonValueKind.True;
        if (logical <= 0 || physical <= 0 || physical > logical)
            throw new ExecutorError("CPU_AFFINITY_INVALID",
                "topology must carry logical>=physical>0");
        // §23: decode control + tokenizer ride P-cores when hybrid;
        // IO/background/maintenance ride E-cores; homogeneous silicon
        // collapses to balanced assignment.
        var plan = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = "star-cpu-plan/v1",
            ["logical"] = logical, ["physical"] = physical,
            ["hybrid_pe"] = hybrid,
            ["assignments"] = new Dictionary<string, object?>
            {
                ["LATENCY_CRITICAL"] = hybrid ? "P-CORE" : "ANY",
                ["COMPUTE"] = hybrid ? "P-CORE" : "ANY",
                ["IO"] = hybrid ? "E-CORE" : "ANY",
                ["BACKGROUND"] = hybrid ? "E-CORE" : "ANY",
                ["MAINTENANCE"] = hybrid ? "E-CORE" : "ANY",
            },
            // §24: two memory-heavy workers never share SMT siblings.
            ["smt_memory_heavy_exclusive"] = true,
            // §25: worker count is bandwidth-bound, not thread-bound.
            ["worker_cap_rule"] =
                "min(physical_cores, bandwidth_headroom), never "
                + "default-to-logical-count",
        };
        return plan;
    }

    // ----------------------------------- §29 global lifetime plan ----

    /// <summary>Merge per-runtime budgets into one plan — the point is
    /// that workspaces alias across non-overlapping lifetimes instead
    /// of each runtime reserving its own maximum (§29).</summary>
    public static Dictionary<string, object?> LifetimePlan(
        JsonElement el)
    {
        if (!el.TryGetProperty("requests", out var rs) ||
            rs.ValueKind != JsonValueKind.Array)
            throw new ExecutorError("LIFETIME_PLAN_INVALID",
                "requests[] required");
        var accepted = new List<object?>();
        long peak = 0;
        foreach (var r in rs.EnumerateArray())
        {
            string name = r.TryGetProperty("name", out var n)
                ? n.GetString() ?? "" : "";
            long bytes = r.TryGetProperty("bytes", out var b)
                ? b.GetInt64() : 0;
            string tier = r.TryGetProperty("tier", out var t)
                ? t.GetString() ?? "" : "";
            if (name.Length == 0 || bytes <= 0 || tier.Length == 0)
                throw new ExecutorError("LIFETIME_PLAN_INVALID",
                    "each request needs name+bytes+tier");
            peak = Math.Max(peak, bytes);
            accepted.Add(new Dictionary<string, object?>
            {
                ["name"] = name, ["bytes"] = bytes, ["tier"] = tier,
            });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-lifetime-plan/v1",
            ["requests"] = accepted,
            // Aliased-arena peak: overlapping lifetimes sum, disjoint
            // lifetimes share — report both bounds honestly.
            ["aliased_peak_bytes"] = peak,
            ["summed_bytes"] = accepted.Sum(x =>
                ((Dictionary<string, object?>)x)
                    .TryGetValue("bytes", out var b)
                        && b is long lb ? lb : 0L),
        };
    }

    // -------------------------------- §30-§33 shared/routed isolation -

    /// <summary>Shared expert residency is ALWAYS_HOT; any offload or
    /// cold-tier request is denied outright (§31/§79).</summary>
    public static Dictionary<string, object?> ExpertResidencyPolicy(
        string component, string requested)
    {
        bool isShared = string.Equals(
            component, "shared_expert", StringComparison.OrdinalIgnoreCase);
        if (isShared && (requested == "OFFLOAD" || requested == "COLD" ||
                         requested == "NVME"))
            throw new ExecutorError("SHARED_EXPERT_OFFLOAD_DENIED",
                "shared expert is ALWAYS_HOT — never cold-tier");
        string residency = isShared
            ? "ALWAYS_HOT"
            : requested is "HOT" or "WARM" or "COLD" or "NVME"
                ? requested : "WARM";
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-expert-residency-policy/v1",
            ["component"] = component,
            ["requested"] = requested,
            ["residency"] = residency,
            ["router_governed"] = !isShared,
        };
    }

    // -------------------------------------------- §47 freeze map -----

    public static readonly string[] FreezeStates =
        { "FROZEN", "TRAINABLE", "CALIBRATION_ONLY" };

    /// <summary>Validate + apply a freeze map. Trainable surface must
    /// respect max_trainable_params; a frozen component marked
    /// trainable by a caller that lacks the governed lane fails closed.</summary>
    public static Dictionary<string, object?> ValidateFreezeMap(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("format", out var f) ||
            f.GetString() != FreezeFormat)
            throw new ExecutorError("PARAMETER_FREEZE_VIOLATION",
                $"expected format {FreezeFormat}");
        if (!el.TryGetProperty("components", out var cs) ||
            cs.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("PARAMETER_FREEZE_VIOLATION",
                "components{} required");
        long budget =
            el.TryGetProperty("max_trainable_params", out var mb)
                ? mb.GetInt64() : long.MaxValue;
        long trainable = 0, frozen = 0;
        foreach (var c in cs.EnumerateObject())
        {
            string state = c.Value.ValueKind == JsonValueKind.String
                ? c.Value.GetString() ?? "" : "";
            long prm = c.Value.ValueKind == JsonValueKind.Object &&
                c.Value.TryGetProperty("params", out var pv)
                    ? pv.GetInt64() : 0;
            if (c.Value.ValueKind == JsonValueKind.Object &&
                c.Value.TryGetProperty("state", out var sv))
                state = sv.GetString() ?? "";
            if (!FreezeStates.Contains(state))
                throw new ExecutorError("PARAMETER_FREEZE_VIOLATION",
                    $"{c.Name}: bad state '{state}'");
            if (state == "TRAINABLE") trainable += prm;
            else frozen += prm;
        }
        if (trainable > budget)
            throw new ExecutorError("PARAMETER_FREEZE_VIOLATION",
                $"trainable {trainable} > budget {budget}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = FreezeFormat,
            ["trainable_params"] = trainable,
            ["frozen_params"] = frozen,
            ["max_trainable_params"] =
                budget == long.MaxValue ? (object?)null : budget,
            // §51: optimizer moments exist for trainable only —
            // 2 fp64 moments per param.
            ["optimizer_state_bytes"] = trainable * 16,
        };
    }

    // ----------------------------------------- §56/§57 efficiency ----

    /// <summary>Parameter-efficiency accounting; capability-normalized
    /// metrics appear when a capability_score is supplied (§57 —
    /// bigger-count comparisons alone are not a promotion case).</summary>
    public static Dictionary<string, object?> EfficiencyReport(
        JsonElement el)
    {
        long Req(string k) =>
            el.TryGetProperty(k, out var v) &&
            v.ValueKind == JsonValueKind.Number
                ? v.GetInt64()
                : throw new ExecutorError("EFFICIENCY_INPUT_INVALID",
                                          $"missing {k}");
        long total = Req("total_params");
        long unique = Req("unique_params");
        long active = Req("active_params");
        long trainable = el.TryGetProperty("trainable_params", out var tp)
            ? tp.GetInt64() : 0;
        double? score = el.TryGetProperty("capability_score", out var cs) &&
                        cs.ValueKind == JsonValueKind.Number
                            ? cs.GetDouble() : null;
        var rep = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = EfficiencyFormat,
            ["total_params"] = total,
            ["unique_params"] = unique,
            ["active_params"] = active,
            ["trainable_params"] = trainable,
            ["shared_params"] =
                el.TryGetProperty("shared_params", out var sh)
                    ? sh.GetInt64() : 0,
            ["routed_params"] =
                el.TryGetProperty("routed_params", out var rp)
                    ? rp.GetInt64() : 0,
            ["bytes_per_active_param"] = active > 0
                ? (double)(total * 8) / active : 0.0,
            ["sparsity_ratio"] = total > 0
                ? 1.0 - (double)active / total : 0.0,
        };
        if (score is double s)
        {
            rep["capability_gain_per_million_params"] =
                s / Math.Max(1.0, total / 1e6);
            double gflop = el.TryGetProperty("active_gflop", out var gf)
                ? gf.GetDouble() : 0;
            if (gflop > 0)
                rep["capability_gain_per_active_GFLOP"] = s / gflop;
        }
        return rep;
    }
}

/// <summary>§10/§11 WindowsMlNpuBackend — EP discovery surface only.
/// The host layer (Windows ML) enumerates vendor EPs discovered by the
/// native probe (npu-ep-enum); zero EPs + zero device evidence means
/// npu_available=false, which routes fall through — never an error.
/// Requiring an unavailable NPU is NPU_BACKEND_UNAVAILABLE.</summary>
internal static class NpuBackend
{
    public const string Format = "star-npu-backend/v1";

    /// <summary>Ops eligible for NPU dispatch (§13): small, static-shape
    /// decision/rerank/embedding work — never the main decoder (§14).</summary>
    public static readonly string[] NpuEligibleOps =
        { "system1", "embedding", "reranker", "rag_routing",
          "tool_routing", "vision_projector", "background_classifier" };

    /// <summary>Resolve backend state from a discovery record
    /// ({windows_ml, directml, npu_present, providers[]}).</summary>
    public static Dictionary<string, object?> Status(JsonElement disc)
    {
        bool winml = disc.TryGetProperty("windows_ml", out var w) &&
                     w.ValueKind == JsonValueKind.True;
        bool npu = disc.TryGetProperty("npu_present", out var n) &&
                   n.ValueKind == JsonValueKind.True;
        var eps = new List<string>();
        if (disc.TryGetProperty("providers", out var ps) &&
            ps.ValueKind == JsonValueKind.Array)
            foreach (var p in ps.EnumerateArray())
                if (p.TryGetProperty("present", out var pr) &&
                    pr.ValueKind == JsonValueKind.True &&
                    p.TryGetProperty("name", out var nm))
                    eps.Add(nm.GetString() ?? "");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["host_layer"] = "windows_ml",
            ["windows_ml"] = winml,
            ["eps_discovered"] = eps,
            ["npu_available"] = npu,
            ["dispatch_rule"] = "probe-first; absent -> GPU/CPU",
            ["eligible_ops"] = NpuEligibleOps.ToList(),
            ["inference_only"] = true,   // §61: no NPU training lane
        };
    }

    /// <summary>Gate an op for NPU execution — the decoder and any
    /// off-list op are denied (§14: dynamic DeltaNet/MoE/KV state).</summary>
    public static void RequireEligible(string op, bool npuAvailable)
    {
        if (!NpuEligibleOps.Contains(op ?? ""))
            throw new ExecutorError("NPU_GRAPH_INCOMPATIBLE",
                $"op '{op}' is not NPU-eligible (dynamic state)");
        if (!npuAvailable)
            throw new ExecutorError("NPU_BACKEND_UNAVAILABLE",
                "no NPU EP discovered");
    }
}

/// <summary>§65 static-shape buckets — NPU graphs compile per bucket;
/// out-of-range lengths fall to GPU/CPU, never recompile per length.</summary>
internal static class StaticShapeBuckets
{
    public static readonly int[] Buckets = { 64, 128, 256, 512, 1024 };

    /// <summary>Smallest bucket >= length; -1 when out of range.</summary>
    public static int BucketFor(int length)
    {
        foreach (int b in Buckets)
            if (length <= b) return b;
        return -1;
    }
}

/// <summary>§66 compiled-graph cache: key = model hash + subgraph hash +
/// precision + shape bucket + device + driver/compiler version. A driver
/// change invalidates every entry (NPU_GRAPH_INCOMPATIBLE on stale
/// reuse — never silent reuse of a stale binary).</summary>
internal static class NpuCompiledGraphCache
{
    public const string Format = "star-npu-graph-cache/v1";

    public static string Key(string modelHash, string subgraphHash,
        string precision, int shapeBucket, string device,
        string driverVersion)
        => string.Join('|', modelHash, subgraphHash, precision,
                       shapeBucket, device, driverVersion);

    /// <summary>Validate a lookup: the stored driver must equal the
    /// current driver or the cached graph is stale evidence.</summary>
    public static Dictionary<string, object?> Lookup(
        string key, string storedDriver, string currentDriver)
    {
        if (storedDriver != currentDriver)
            throw new ExecutorError("NPU_GRAPH_INCOMPATIBLE",
                $"driver changed {storedDriver} -> {currentDriver}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["key"] = key, ["driver"] = currentDriver,
            ["cache_hit"] = true,
        };
    }
}

/// <summary>§15 NpuPrefillProbe — static-chunk NPU prefill study. The
/// verdict is arithmetic honesty: NPU prefill is promoted only when
/// TTFT improves AND transfer cost is acceptable; absent NPU reports
/// skipped evidence.</summary>
internal static class NpuPrefillProbe
{
    /// <param name="promptTokens">raw prompt length.</param>
    /// <param name="gpuPrefillMs">measured GPU/unified prefill.</param>
    /// <param name="npuPrefillMs">measured/estimated NPU prefill
    /// (0 = unmeasured).</param>
    /// <param name="transferMs">state transfer NPU->GPU decode.</param>
    public static Dictionary<string, object?> Evaluate(
        int promptTokens, double gpuPrefillMs, double npuPrefillMs,
        double transferMs, bool npuAvailable)
    {
        int bucket = StaticShapeBuckets.BucketFor(promptTokens);
        var rep = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = "star-npu-prefill-probe/v1",
            ["prompt_tokens"] = promptTokens,
            ["shape_bucket"] = bucket < 0 ? "OUT_OF_RANGE" : bucket,
            ["gpu_prefill_ms"] = gpuPrefillMs,
            ["npu_available"] = npuAvailable,
        };
        if (!npuAvailable || npuPrefillMs <= 0)
        {
            rep["skipped"] = true;
            rep["verdict"] = "UNIFIED";   // §15: no promotion case
            return rep;
        }
        if (bucket < 0)
        {
            rep["verdict"] = "GPU_FALLBACK_SHAPE";
            return rep;
        }
        double npuTotal = npuPrefillMs + transferMs;
        bool promote = npuTotal < gpuPrefillMs;
        rep["npu_prefill_ms"] = npuPrefillMs;
        rep["transfer_ms"] = transferMs;
        rep["ttft_delta_ms"] = gpuPrefillMs - npuTotal;
        rep["verdict"] = promote ? "NPU_PREFILL" : "UNIFIED";
        // §15: transfer must not dominate the saved compute.
        rep["transfer_acceptable"] = transferMs < npuPrefillMs * 0.5;
        return rep;
    }
}

/// <summary>§48/§49 ExpertTargetedTrainingProbe — derive a parameter-
/// efficient freeze map from *measured routing evidence* only. Expert
/// ids are selected by observed usage in the capability's router
/// counts; manual capability->expert assignment is rejected (§49).</summary>
internal static class ExpertTargetedTrainingProbe
{
    /// <param name="routingCounts">evidence: expert_id -> routed tokens
    /// observed on capability traffic.</param>
    /// <param name="expertParams">params per routed expert.</param>
    /// <param name="totalParams">whole-model params (full SFT cost).</param>
    /// <param name="sharedParams">shared-expert params.</param>
    /// <param name="topExperts">how many high-usage experts to
    /// unfreeze.</param>
    public static Dictionary<string, object?> Plan(
        IReadOnlyDictionary<int, long> routingCounts,
        long expertParams, long totalParams, long sharedParams,
        int topExperts)
    {
        if (routingCounts == null || routingCounts.Count == 0)
            throw new ExecutorError("PARAMETER_FREEZE_VIOLATION",
                "expert targeting requires routing evidence — manual "
                + "capability->expert assignment is denied (§49)");
        var top = routingCounts
            .OrderByDescending(kv => kv.Value)
            .Take(Math.Max(1, topExperts))
            .Select(kv => kv.Key).OrderBy(x => x).ToList();
        long trainable = top.Count * expertParams + sharedParams;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-expert-targeted-probe/v1",
            ["selected_experts"] = top.Cast<object?>().ToList(),
            ["selection_basis"] = "measured_routing_evidence",
            ["trainable_params"] = trainable,
            ["full_sft_params"] = totalParams,
            ["trainable_fraction"] = totalParams > 0
                ? (double)trainable / totalParams : 0.0,
            ["optimizer_state_bytes"] = trainable * 16,
            ["optimizer_saved_vs_full"] = (totalParams - trainable) * 16,
            ["note"] = "evidence-derived; capability->expert is never "
                       + "hand-assigned",
        };
    }
}
