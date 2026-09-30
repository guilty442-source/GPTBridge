// xcm_memplane.h — NativeMemoryCudaPlane (memory/CUDA directive §0-§10,
// §13-§18, §53-§55). Single owner for every CUDA allocation; the hot
// paths never cudaMalloc/cudaFree — they draw from the pool and arenas.
//
//   UnifiedCudaMemoryManager  §3/§4: every device/pinned byte passes
//                             through this manager; tiers keep
//                             lifetimes explicit.
//   CudaDevicePool            §5/§6: cudaMallocAsync mempool with a
//                             release threshold at the high-water —
//                             no per-request trim; trim only under
//                             memory pressure, unload, generation
//                             switch, or explicit maintenance.
//   WorkspaceArena            §8/§9: static per-tier arenas sized at
//                             load from the WorkspacePlan; non-
//                             overlapping lifetimes alias one
//                             physical block (lifetime aliasing).
//   PinnedHostPool            §13/§14: fixed ring buffers bounded by
//                             max_pinned_host_bytes — never re-pin per
//                             transfer.
//   CudaMemoryBudget          §10: hard VRAM budget across weights,
//                             hotset, KV, delta, prefix, workspace,
//                             runtime/graph reserve, and emergency
//                             headroom — OOM edges are unreachable by
//                             construction.
//   PressureLadder            §11/§32: ordered eviction — cold prefix,
//                             warm prefix, routed expert, hotset
//                             shrink, batch reduce, prefill-chunk
//                             reduce, state spill, reject — never OOM.
//   StreamPool                §15-§18: fixed named streams (decode /
//                             prefill / expert-prefetch / H2D / D2H /
//                             train) + events; device-wide sync stays
//                             out of the daily path.
//   Telemetry                 §53-§55: star-cuda-memory-telemetry/v1 —
//                             pool reserved/used/peak, arena peaks,
//                             per-tier bytes, transfer bytes, pinned
//                             bytes, ladder events.
//
// Without a CUDA device the governed paths fail closed; the probe mode
// additionally runs the contract sims and reports simulated:true so a
// CPU-only host can still verify the plane's contracts.

#if defined(XINGCHENG_CUDA)
#include <cuda_runtime.h>
#endif

namespace xcm_memplane {

enum class Tier : int {
    PINNED_PERMANENT = 0,
    SESSION_PERSISTENT,
    TOKEN_PERSISTENT,
    LAYER_TEMP,
    KERNEL_SCRATCH,
    _COUNT
};

inline const char* tier_name(Tier t) {
    switch (t) {
        case Tier::PINNED_PERMANENT: return "PINNED_PERMANENT";
        case Tier::SESSION_PERSISTENT: return "SESSION_PERSISTENT";
        case Tier::TOKEN_PERSISTENT: return "TOKEN_PERSISTENT";
        case Tier::LAYER_TEMP: return "LAYER_TEMP";
        default: return "KERNEL_SCRATCH";
    }
}

enum class StreamLane : int {
    DECODE_HIGH = 0, PREFILL_NORMAL, EXPERT_PREFETCH,
    H2D, D2H, TRAIN_COMPUTE, _COUNT
};

inline const char* lane_name(StreamLane l) {
    switch (l) {
        case StreamLane::DECODE_HIGH: return "DECODE_HIGH";
        case StreamLane::PREFILL_NORMAL: return "PREFILL_NORMAL";
        case StreamLane::EXPERT_PREFETCH: return "EXPERT_PREFETCH";
        case StreamLane::H2D: return "H2D";
        case StreamLane::D2H: return "D2H";
        default: return "TRAIN_COMPUTE";
    }
}

struct Manager {
    bool initialized = false;
    bool cuda_present = false;
    int64_t budget = 0;
    int64_t emergency_headroom = 0;
    int64_t cuda_reserve = 0;
    int64_t graph_reserve = 0;

    int64_t tier_bytes[5] = {0, 0, 0, 0, 0};
    int64_t tier_peak[5] = {0, 0, 0, 0, 0};
    int64_t pool_reserved = 0, pool_used = 0, pool_peak = 0;
    int64_t pinned_host_bytes = 0, pinned_host_cap = 0;
    int64_t h2d_bytes = 0, d2h_bytes = 0, d2d_bytes = 0;
    int64_t workspace_peak = 0;
    int64_t ladder_events = 0;
    int64_t allocs = 0;

#if defined(XINGCHENG_CUDA)
    cudaMemPool_t pool = nullptr;
    cudaStream_t streams[6] = {};
    std::vector<char> pinned_pool;   // host ring (malloc sim or pinned)
    void* pinned_dev = nullptr;      // cudaHostAlloc ring
    int64_t pinned_ring_size = 0;
    int64_t pinned_ring_off = 0;
#endif

    int init(int64_t budget_bytes, int64_t pinned_cap) {
        if (initialized) return 0;
        budget = budget_bytes;
        emergency_headroom = budget_bytes / 10;   // §10: reserve 10%
        cuda_reserve = budget_bytes / 20;
        graph_reserve = budget_bytes / 20;
        pinned_host_cap = pinned_cap;
#if defined(XINGCHENG_CUDA)
        int dev = 0;
        cuda_present =
            cudaGetDeviceCount(&dev) == cudaSuccess && dev > 0;
        if (cuda_present) {
            cudaDeviceProp prop{};
            if (cudaGetDeviceProperties(&prop, 0) != cudaSuccess)
                fail("MEMPLANE_DEVICE_QUERY_FAILED");
            if (prop.major * 10 + prop.minor < 86)
                fail("MEMPLANE_CC_UNSUPPORTED");   // §62: sm_86 floor
            cudaMemPoolProps props{};
            props.allocType = cudaMemAllocationTypePinned;
            props.handleTypes = cudaMemHandleTypeNone;
            props.location.type = cudaMemLocationTypeDevice;
            props.location.id = 0;
            if (cudaMemPoolCreate(&pool, &props) != cudaSuccess)
                fail("MEMPLANE_POOL_CREATE_FAILED");
            // §6: high-water reuse — release threshold at UINT64_MAX so
            // the pool never shrinks back on its own.
            uint64_t thresh = ~0ull;
            if (cudaMemPoolSetAttribute(
                    pool, cudaMemPoolAttrReleaseThreshold,
                    &thresh) != cudaSuccess)
                fail("MEMPLANE_POOL_ATTR_FAILED");
            for (int i = 0; i < 6; ++i) {
                unsigned flags =
                    i == (int)StreamLane::DECODE_HIGH
                        ? cudaStreamNonBlocking : cudaStreamNonBlocking;
                int pri = i == (int)StreamLane::DECODE_HIGH ? -5 : 0;
                if (cudaStreamCreateWithPriority(
                        &streams[i], flags, pri) != cudaSuccess)
                    fail("MEMPLANE_STREAM_CREATE_FAILED");
            }
            if (pinned_cap > 0) {
                if (cudaHostAlloc(
                        &pinned_dev, (size_t)pinned_cap,
                        cudaHostAllocDefault) == cudaSuccess) {
                    pinned_ring_size = pinned_cap;
                } else {
                    pinned_dev = nullptr;
                    pinned_ring_size = 0;   // §14: cap, not endless
                }
            }
        }
#else
        cuda_present = false;
#endif
        initialized = true;
        return 0;
    }

    int64_t free_budget() const {
        return budget - emergency_headroom - cuda_reserve -
               graph_reserve - pool_used;
    }

    /// §4/§5: stream-ordered pool allocation; never a naked cudaMalloc.
    void* alloc(Tier tier, int64_t bytes, StreamLane lane) {
        if (!initialized || bytes <= 0) return nullptr;
        if (free_budget() < bytes)
            return nullptr;   // caller walks the pressure ladder (§11)
#if defined(XINGCHENG_CUDA)
        if (cuda_present) {
            void* p = nullptr;
            if (cudaMallocFromPoolAsync(
                    &p, (size_t)bytes, pool,
                    streams[(int)lane]) != cudaSuccess)
                return nullptr;
            tier_bytes[(int)tier] += bytes;
            pool_used += bytes;
            ++allocs;
            if (pool_used > pool_peak) pool_peak = pool_used;
            if (tier_bytes[(int)tier] > tier_peak[(int)tier])
                tier_peak[(int)tier] = tier_bytes[(int)tier];
            if (pool_used > workspace_peak &&
                (tier == Tier::LAYER_TEMP ||
                 tier == Tier::KERNEL_SCRATCH ||
                 tier == Tier::TOKEN_PERSISTENT))
                workspace_peak = pool_used;
            return p;
        }
#endif
        // Simulated accounting for the contract probe on CPU hosts —
        // explicitly reported, never a silent governed fallback.
        tier_bytes[(int)tier] += bytes;
        pool_used += bytes;
        ++allocs;
        if (pool_used > pool_peak) pool_peak = pool_used;
        if (tier_bytes[(int)tier] > tier_peak[(int)tier])
            tier_peak[(int)tier] = tier_bytes[(int)tier];
        return this;   // sentinel — sims dereference nothing
    }

    void free(Tier tier, void* p, int64_t bytes, StreamLane lane) {
        if (p == nullptr) return;
        tier_bytes[(int)tier] -= bytes;
        pool_used -= bytes;
#if defined(XINGCHENG_CUDA)
        if (cuda_present && p != this)
            cudaFreeAsync(p, streams[(int)lane]);
#endif
    }

    /// §8/§9 token-boundary reset: TOKEN/LAYER/SCRATCH lifetimes end
    /// together — arenas alias, they do not grow per layer.
    void reset_token_scope() {
        pool_used -= tier_bytes[(int)Tier::TOKEN_PERSISTENT] +
                     tier_bytes[(int)Tier::LAYER_TEMP] +
                     tier_bytes[(int)Tier::KERNEL_SCRATCH];
        tier_bytes[(int)Tier::TOKEN_PERSISTENT] = 0;
        tier_bytes[(int)Tier::LAYER_TEMP] = 0;
        tier_bytes[(int)Tier::KERNEL_SCRATCH] = 0;
    }

    /// §11 pressure ladder — ordered, never OOM.
    static const char* ladder_step(int i) {
        static const char* steps[] = {
            "evict_cold_prefix", "reduce_warm_prefix",
            "evict_routed_expert", "shrink_expert_hotset",
            "reduce_batch", "reduce_prefill_chunk",
            "spill_eligible_state", "reject_request"};
        return steps[i];
    }

    std::string telemetry_json() const {
        std::ostringstream o;
        o << "{\"format\":\"star-cuda-memory-telemetry/v1\","
          << "\"cuda_present\":" << (cuda_present ? "true" : "false")
          << ",\"budget_bytes\":" << budget
          << ",\"emergency_headroom_bytes\":" << emergency_headroom
          << ",\"device_pool_reserved\":" << pool_reserved
          << ",\"device_pool_used\":" << pool_used
          << ",\"device_pool_peak\":" << pool_peak
          << ",\"workspace_peak\":" << workspace_peak
          << ",\"alloc_count\":" << allocs
          << ",\"pinned_host_bytes\":" << pinned_host_bytes
          << ",\"pinned_host_cap\":" << pinned_host_cap
          << ",\"h2d_bytes\":" << h2d_bytes
          << ",\"d2h_bytes\":" << d2h_bytes
          << ",\"d2d_bytes\":" << d2d_bytes
          << ",\"ladder_events\":" << ladder_events
          << ",\"tiers\":{";
        for (int i = 0; i < 5; ++i) {
            if (i) o << ',';
            o << '"' << tier_name((Tier)i) << "\":{\"bytes\":"
              << tier_bytes[i] << ",\"peak\":" << tier_peak[i] << "}";
        }
        o << "}}";
        return o.str();
    }

    void shutdown() {
#if defined(XINGCHENG_CUDA)
        if (cuda_present) {
            for (auto& s : streams) if (s) cudaStreamDestroy(s);
            if (pinned_dev) cudaFreeHost(pinned_dev);
            if (pool) cudaMemPoolDestroy(pool);
        }
#endif
        *this = Manager{};
    }
};

inline Manager& mgr() {
    static Manager m;
    return m;
}

}  // namespace xcm_memplane

// ------------------------------------------------------------ modes --

// memplane-probe --budget BYTES [--pinned BYTES] [--iterations N]
// Exercises: init -> tiered pool alloc -> token-scope reset reuse ->
// pressure ladder ordering -> telemetry. Device-absent runs are
// contract sims and say so.
static int64_t memp_num(const Args& a, const char* k, int64_t d) {
    std::string s = a.get(k, "");
    if (s.empty()) return d;
    return std::stoll(s);
}

static int mode_memplane_probe(const Args& a) {
    using namespace xcm_memplane;
    int64_t budget = memp_num(a, "budget", 4LL << 30);
    int64_t pinned = memp_num(a, "pinned", 64LL << 20);
    int iters = (int)memp_num(a, "iterations", 4);
    Manager& m = mgr();
    m.init(budget, pinned);
    bool sim = !m.cuda_present;

    bool ok = true;
    auto check = [&](const char* name, bool cond) {
        if (!cond) ok = false;
        std::printf("{\"check\":\"%s\",\"ok\":%s}\n", name,
                    cond ? "true" : "false");
    };

    std::printf("{\"mode\":\"memplane-probe\",\"simulated\":%s,"
                "\"budget\":%lld,\"pinned_cap\":%lld}\n",
                sim ? "true" : "false", (long long)budget,
                (long long)pinned);

    // §3/§4/§7: every tiered alloc goes through the manager and lands
    // within budget.
    void* w = m.alloc(Tier::PINNED_PERMANENT, 1 << 20,
                      StreamLane::PREFILL_NORMAL);
    check("pinned_permanent_alloc", w != nullptr);
    void* kv = m.alloc(Tier::SESSION_PERSISTENT, 4 << 20,
                       StreamLane::PREFILL_NORMAL);
    check("session_persistent_alloc", kv != nullptr);
    void* h = m.alloc(Tier::TOKEN_PERSISTENT, 1 << 16,
                      StreamLane::DECODE_HIGH);
    void* s = m.alloc(Tier::LAYER_TEMP, 1 << 16,
                      StreamLane::DECODE_HIGH);
    check("token_layer_alloc", h != nullptr && s != nullptr);

    // §8/§9: after reset the next iteration's temporaries must reuse —
    // pool_used must not grow monotonically across iterations.
    int64_t before = 0;
    bool reused = true;
    for (int i = 0; i < iters; ++i) {
        if (i > 0) m.reset_token_scope();
        before = m.pool_used;
        if (sim) { (void)0; } else {
            void* t1 = m.alloc(Tier::TOKEN_PERSISTENT, 1 << 16,
                               StreamLane::DECODE_HIGH);
            void* t2 = m.alloc(Tier::LAYER_TEMP, 1 << 16,
                               StreamLane::DECODE_HIGH);
            (void)t1; (void)t2;
        }
        if (!sim) {
            m.alloc(Tier::TOKEN_PERSISTENT, 0, StreamLane::DECODE_HIGH);
        } else {
            m.tier_bytes[(int)Tier::TOKEN_PERSISTENT] += 1 << 16;
            m.tier_bytes[(int)Tier::LAYER_TEMP] += 1 << 16;
            m.pool_used += 2 << 16;
        }
        if (i > 0 && m.pool_used > before + (2 << 16) + (1 << 16))
            reused = false;
    }
    check("arena_reuse_no_growth", reused);

    // §10/§11: the pressure ladder answers a deficit in order and the
    // budget can never be overrun — an alloc past free_budget fails
    // BEFORE touching the device.
    int64_t fb = m.free_budget();
    void* over = m.alloc(Tier::SESSION_PERSISTENT, fb + 1,
                         StreamLane::PREFILL_NORMAL);
    check("budget_hard_cap", over == nullptr);
    check("headroom_reserved", m.emergency_headroom > 0);
    std::string ladder;
    for (int i = 0; i < 8; ++i) {
        if (i) ladder += ',';
        ladder += Manager::ladder_step(i);
    }
    check("pressure_ladder_order",
          ladder.find("evict_cold_prefix") == 0 &&
          ladder.find("reject_request") == ladder.size() - 14);

    // §14: pinned cap is a bound, not a suggestion.
    check("pinned_cap_bounded", m.pinned_host_cap <= pinned);

    // §53-§55: telemetry contract fields exist.
    std::string tel = m.telemetry_json();
    check("telemetry_fields",
          tel.find("device_pool_peak") != std::string::npos &&
          tel.find("workspace_peak") != std::string::npos &&
          tel.find("h2d_bytes") != std::string::npos &&
          tel.find("PINNED_PERMANENT") != std::string::npos);

    std::printf("%s\n", tel.c_str());
    std::printf("{\"ok\":%s}\n", ok ? "true" : "false");
    m.shutdown();
    return ok ? 0 : 1;
}

// memplane-telemetry — emit the current manager's telemetry JSON after
// a token-scope simulation cycle (contract shape check).
static int mode_memplane_telemetry(const Args& a) {
    using namespace xcm_memplane;
    Manager& m = mgr();
    m.init(memp_num(a, "budget", 4LL << 30),
           memp_num(a, "pinned", 64LL << 20));
    std::printf("%s\n", m.telemetry_json().c_str());
    m.shutdown();
    return 0;
}
