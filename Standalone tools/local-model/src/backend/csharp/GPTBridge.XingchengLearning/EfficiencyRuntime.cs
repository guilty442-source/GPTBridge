// EfficiencyRuntime.cs — Native Inference Efficiency Plane control
// plane (§1, §17-§19, §38-§46). Runtime-level only: no model
// generation, no architecture, no weights change.
//
//   HardwareCapabilityRegistry   probed hardware facts feed every
//                                policy decision — never assumed (§28).
//   InferenceMemoryPlanner       ONE budget authority for KV /
//                                experts / prefixes / delta state
//                                (§38/§39 tiered eviction priority).
//   NativeMemoryTier             DEVICE_HOT / HOST_PINNED / HOST_RAM
//                                (+ DISK_COLD future) asset classes.
//   RagPrefixManifest            §17 manifest hash → prefix scope;
//                                any revision/hash change invalidates.
//   RagEvidenceCache             §18 layer-1 retrieval cache — kept
//                                strictly separate from the model
//                                prefix cache.
//   PrefillDecodeScheduler       §24-§36 role assignment + decode
//                                latency guard (UNIFIED default; lane
//                                split only when bench-proven).
//   InferenceEfficiencyPolicy    §41-§45 OFF/AUTO/MEMORY_SAVER/
//                                LOW_LATENCY/THROUGHPUT resolution.
//   NativeInferenceEfficiencyManager §1 single façade: the three
//                                subsystems share one planner, never
//                                manage memory independently.

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

// --------------------------------------------- §28 hardware registry
internal sealed class HardwareCaps
{
    public int GpuCount;
    public long VramBytes;
    public long RamBytes;
    public double PcieGbps;        // measured/probed, not assumed
    public double CpuMemGbps;
    public bool Measured;          // false → registry refuses to split

    public static HardwareCaps From(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("RUNTIME_CAPS_INVALID",
                "hardware caps must be an object");
        long num(JsonElement r, string k, long d)
        {
            if (!r.TryGetProperty(k, out var v)) return d;
            if (v.ValueKind == JsonValueKind.Number &&
                v.TryGetInt64(out long n)) return n;
            return d;
        }
        double dnum(JsonElement r, string k, double d)
        {
            if (!r.TryGetProperty(k, out var v)) return d;
            if (v.ValueKind == JsonValueKind.Number) return v.GetDouble();
            return d;
        }
        var c = new HardwareCaps
        {
            GpuCount = (int)num(el, "gpu_count", 0),
            VramBytes = num(el, "vram_bytes", 0),
            RamBytes = num(el, "ram_bytes", 0),
            PcieGbps = dnum(el, "pcie_gbps", 0),
            CpuMemGbps = dnum(el, "cpu_mem_gbps", 0),
            Measured = el.TryGetProperty("measured", out var m) &&
                       m.ValueKind == JsonValueKind.True,
        };
        if (c.GpuCount < 0 || c.VramBytes < 0)
            throw new ExecutorError("RUNTIME_CAPS_INVALID",
                "negative hardware capacity");
        return c;
    }

    public Dictionary<string, object?> ToDict() => new()
    {
        ["gpu_count"] = GpuCount, ["vram_bytes"] = VramBytes,
        ["ram_bytes"] = RamBytes, ["pcie_gbps"] = PcieGbps,
        ["cpu_mem_gbps"] = CpuMemGbps, ["measured"] = Measured,
    };
}

// ------------------------------------------- §38/§39 memory tiers ---
internal static class EfficiencyRuntime
{
    public const string PolicyFormat = "star-inference-efficiency-policy/v1";
    public const string TelemetryFormat = "star-inference-efficiency/v1";
    public const string ManifestFormat = "star-rag-prefix-manifest/v1";

    // §39 eviction priority — never a flat cache.
    public static readonly string[] MemoryTiers =
        { "DEVICE_HOT", "HOST_PINNED", "HOST_RAM", "DISK_COLD" };
    public static readonly (string asset, string priority)[] AssetPriority =
    {
        ("MODEL_COMMON", "PINNED"),   // router/shared expert/embedding
        ("DECODE_STATE", "HIGH"),     // currently decoding state
        ("EXPERT", "MEDIUM"),         // hot experts
        ("PREFIX", "MEDIUM"),         // hot prefixes
        ("KV", "LOW"),                // cold KV spill
        ("DELTA_STATE", "LOW"),
        ("VISION", "LOW"),
        ("WORKSPACE", "RECONSTRUCTABLE"),
    };

    /// <summary>§38 unified tier plan: budgets for every asset class
    /// from ONE planner input (device + host capacities).</summary>
    public static Dictionary<string, object?> TierPlan(JsonElement el)
    {
        var caps = HardwareCaps.From(el);
        if (!caps.Measured)
            throw new ExecutorError("RUNTIME_CAPS_INVALID",
                "tier planning requires measured hardware caps " +
                "(measured=true)");
        long modelBytes = 0, expertBytes = 0;
        if (el.TryGetProperty("model_bytes", out var mb))
            modelBytes = mb.GetInt64();
        if (el.TryGetProperty("expert_bytes", out var eb))
            expertBytes = eb.GetInt64();
        if (modelBytes <= 0)
            throw new ExecutorError("RUNTIME_CAPS_INVALID",
                "model_bytes required");
        // §23 GPU hot prefix, RAM warm prefix; experts only offload
        // when they cannot fit the device budget.
        long prefixGpu = Math.Max(0,
            Math.Min(caps.VramBytes / 8, caps.VramBytes - modelBytes));
        long prefixRam = Math.Max(0, caps.RamBytes / 4);
        bool expertOffload = caps.VramBytes > 0 &&
            modelBytes + expertBytes > caps.VramBytes;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-memory-tier-plan/v1",
            ["tiers"] = MemoryTiers,
            ["asset_priority"] = AssetPriority.Select(p =>
                (object?)new Dictionary<string, object?>
                {
                    ["asset"] = p.asset, ["priority"] = p.priority,
                }).ToList(),
            ["budgets"] = new Dictionary<string, object?>
            {
                ["prefix_gpu_budget"] = prefixGpu,
                ["prefix_ram_budget"] = prefixRam,
                ["expert_device_budget"] = expertOffload
                    ? Math.Max(0, caps.VramBytes - modelBytes)
                    : expertBytes,
                ["expert_host_budget"] = expertBytes,
            },
            ["expert_offload_required"] = expertOffload,
            ["disk_cold_tier"] = "future",
        };
    }

    // ------------------------------------------------- §17 manifest --
    /// <summary>RagPrefixManifest → canonical hash → prefix scope id.
    /// Any document revision / chunk hash / retrieval-policy /
    /// prompt-template / tokenizer change yields a different scope —
    /// which is exactly the cache key (§17/§19).</summary>
    public static Dictionary<string, object?> RagPrefixManifest(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("PREFIX_STATE_INCOMPATIBLE",
                "manifest must be an object");
        foreach (var k in new[] { "knowledge_scope", "document_ids",
                                  "document_revisions", "chunk_ids",
                                  "chunk_order", "content_hashes",
                                  "retrieval_policy_version",
                                  "prompt_template_version" })
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError("PREFIX_STATE_INCOMPATIBLE",
                    $"manifest missing {k}");
        string canonical = CanonicalJson.Canonical(el);
        string hash = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ManifestFormat,
            ["manifest_hash"] = hash,
            ["prefix_scope"] = "rag:" + hash[..16],
            ["invalidates_on"] = new[]
            {
                "document_revision", "chunk_hash", "chunk_order",
                "rag_index_revision", "retrieval_policy_version",
                "prompt_template_version", "tokenizer_hash",
            },
        };
    }

    // ------------------------------------------- §18 evidence cache --
    /// <summary>RAG Evidence Cache (layer 1): retrieved chunks +
    /// rerank results keyed by manifest hash. Kept strictly separate
    /// from the model prefix cache — a manifest change revokes every
    /// stored evidence entry.</summary>
    public static Dictionary<string, object?> EvidenceCacheOp(
        string toolRoot, JsonElement el)
    {
        string dir = Path.Combine(toolRoot,
            "xingcheng/runtime/state/rag-evidence-cache");
        string op = el.TryGetProperty("op", out var o)
            ? o.GetString() ?? "" : "";
        string manifestHash = el.TryGetProperty("manifest_hash", out var mh)
            ? mh.GetString() ?? "" : "";
        string queryHash = el.TryGetProperty("query_hash", out var qh)
            ? qh.GetString() ?? "" : "";
        if (manifestHash.Length == 0)
            throw new ExecutorError("PREFIX_STATE_INCOMPATIBLE",
                "manifest_hash required");
        string scopeDir = Path.Combine(dir, manifestHash);
        switch (op)
        {
            case "get":
            {
                string f = Path.Combine(scopeDir, queryHash + ".json");
                bool hit = File.Exists(f);
                return new Dictionary<string, object?>
                {
                    ["ok"] = true, ["layer"] = "RAG_EVIDENCE_CACHE",
                    ["hit"] = hit,
                    ["chunks"] = hit
                        ? ModelLifecycle.Decode(JsonDocument.Parse(
                            File.ReadAllText(f)).RootElement)
                        : null,
                };
            }
            case "put":
            {
                Directory.CreateDirectory(scopeDir);
                string f = Path.Combine(scopeDir, queryHash + ".json");
                if (!el.TryGetProperty("chunks", out var chunks))
                    throw new ExecutorError("PREFIX_STATE_INCOMPATIBLE",
                        "chunks required for put");
                ModelLifecycle.AtomicWrite(f,
                    CanonicalJson.Canonical(chunks) + "\n");
                return new Dictionary<string, object?>
                {
                    ["ok"] = true, ["layer"] = "RAG_EVIDENCE_CACHE",
                    ["stored"] = true,
                };
            }
            case "invalidate":
            {
                // §19: manifest revision change → the whole scope's
                // evidence entries are revoked (directory removal is
                // scoped to the manifest hash, never global).
                long removed = 0;
                if (Directory.Exists(scopeDir))
                {
                    removed = Directory.GetFiles(scopeDir).Length;
                    Directory.Delete(scopeDir, true);
                }
                return new Dictionary<string, object?>
                {
                    ["ok"] = true, ["layer"] = "RAG_EVIDENCE_CACHE",
                    ["invalidated"] = removed,
                };
            }
            default:
                throw new ExecutorError("PREFIX_STATE_INCOMPATIBLE",
                    $"unknown evidence-cache op '{op}'");
        }
    }

    // ------------------------------------- §41-§45 efficiency policy --
    /// <summary>Resolve the runtime efficiency policy. AUTO decides
    /// from measured hardware + model shape; P/D split requires a
    /// benchmark verdict, never an assumption (§28/§29/§45).</summary>
    public static Dictionary<string, object?> ResolvePolicy(
        JsonElement el)
    {
        var caps = HardwareCaps.From(el);
        string mode = el.TryGetProperty("mode", out var m)
            ? (m.GetString() ?? "AUTO").ToUpperInvariant() : "AUTO";
        if (mode is not ("OFF" or "AUTO" or "MEMORY_SAVER" or
                         "LOW_LATENCY" or "THROUGHPUT"))
            throw new ExecutorError("RUNTIME_CAPS_INVALID",
                $"unknown efficiency mode {mode}");
        long modelBytes = el.TryGetProperty("model_bytes", out var mb)
            ? mb.GetInt64() : 0;
        long expertBytes = el.TryGetProperty("expert_bytes", out var eb)
            ? eb.GetInt64() : 0;

        var d = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = PolicyFormat,
            ["mode"] = mode, ["hardware"] = caps.ToDict(),
            ["model_bytes"] = modelBytes,
            ["expert_bytes"] = expertBytes,
        };
        if (mode == "OFF")
        {
            d["expert_offload"] = "OFF";
            d["hybrid_prefix_cache"] = false;
            d["pd_mode"] = "UNIFIED";
            return d;
        }

        bool fits = caps.VramBytes <= 0 ||
            modelBytes + expertBytes <= caps.VramBytes;
        switch (mode)
        {
            case "AUTO":
                // §45: current ~300M policy — prefix reuse is where the
                // real benefit is; offload only when experts cannot
                // fit; P/D stays UNIFIED until bench proves otherwise.
                d["expert_offload"] = fits ? "ALL_RESIDENT" : "AUTO";
                d["hybrid_prefix_cache"] = true;
                d["rag_prefix_cache"] = true;
                d["pd_mode"] = "BENCHMARK_FIRST";
                d["expert_prefetch_depth"] = 1;
                break;
            case "LOW_LATENCY":
                d["expert_offload"] = "PIN_HOT";
                d["hybrid_prefix_cache"] = true;
                d["prefix_gpu_bias"] = true;
                d["decode_priority"] = true;
                d["pd_mode"] = caps.GpuCount >= 2
                    ? "SPLIT_IF_BENCH" : "UNIFIED";
                d["aggressive_ram_transfer"] = false;
                break;
            case "MEMORY_SAVER":
                d["expert_offload"] = "OFFLOAD";
                d["kv_precision"] = "int8";
                d["prefix_host_bias"] = true;
                d["hybrid_prefix_cache"] = true;
                d["pd_mode"] = "UNIFIED";
                break;
            case "THROUGHPUT":
                d["expert_offload"] = fits ? "ALL_RESIDENT" : "PREFETCH";
                d["expert_prefetch_depth"] = 1;
                d["prefill_batch_bias"] = true;
                d["pd_mode"] = caps.GpuCount >= 2
                    ? "SPLIT_IF_BENCH" : "PIPELINE";
                d["hybrid_prefix_cache"] = true;
                break;
        }
        // §28: lane splits require measured hardware — an unmeasured
        // registry never splits.
        if (!caps.Measured &&
            (string)(d["pd_mode"] ?? "UNIFIED") != "UNIFIED" &&
            mode != "AUTO")
            d["pd_mode"] = "BENCHMARK_FIRST";
        return d;
    }

    /// <summary>§40 telemetry aggregation: combine the native modes'
    /// outputs into one star-inference-efficiency/v1 record.</summary>
    public static Dictionary<string, object?> Telemetry(
        JsonElement residency, JsonElement prefixBench,
        JsonElement pdBench)
    {
        var expert = new Dictionary<string, object?>
        {
            ["expert_resident_count"] =
                residency.TryGetProperty("resident_experts", out var re)
                    ? ModelLifecycle.Decode(re) : null,
            ["expert_cache_hit"] =
                residency.TryGetProperty("cache_hits", out var ch)
                    ? ModelLifecycle.Decode(ch) : null,
            ["expert_miss_rate"] =
                residency.TryGetProperty("cache_misses", out var cm)
                    ? ModelLifecycle.Decode(cm) : null,
        };
        var prefix = new Dictionary<string, object?>
        {
            ["prefix_tokens"] =
                prefixBench.TryGetProperty("prefix_tokens", out var pt)
                    ? ModelLifecycle.Decode(pt) : null,
            ["ttft_reduction_pct"] =
                prefixBench.TryGetProperty("ttft_reduction_pct", out var tr)
                    ? ModelLifecycle.Decode(tr) : null,
        };
        var pd = new Dictionary<string, object?>
        {
            ["prefill_ms"] =
                pdBench.TryGetProperty("prefill_ms", out var pm)
                    ? ModelLifecycle.Decode(pm) : null,
            ["state_transfer_ms"] =
                pdBench.TryGetProperty("state_transfer_ms", out var st)
                    ? ModelLifecycle.Decode(st) : null,
            ["itl_ms"] =
                pdBench.TryGetProperty("itl_ms", out var it)
                    ? ModelLifecycle.Decode(it) : null,
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TelemetryFormat,
            ["expert"] = expert, ["prefix"] = prefix, ["pd"] = pd,
            ["weights_unchanged"] = true,
            ["generation_unchanged"] = true,
        };
    }
}
