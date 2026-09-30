// cuda_memplane.h — NativeMemoryCudaPlane core (memory/CUDA directive
// §0-§18, §32, §53-§55). Shared by the engine TUs (cuda_bridge.cpp)
// and the tool probes (xcm_memplane.h). Single owner for every CUDA
// allocation: the hot paths draw from the pool and arenas — they never
// cudaMalloc/cudaFree per call.
//
//   Tiers      PINNED_PERMANENT / SESSION_PERSISTENT /
//              TOKEN_PERSISTENT / LAYER_TEMP / KERNEL_SCRATCH — every
//              byte carries an explicit lifetime.
//   Pool       cudaMallocFromPoolAsync with the release threshold at
//              high-water — no per-request trim (§5/§6).
//   Budget     hard budget minus emergency headroom, cuda reserve and
//              graph reserve; an over-budget alloc is refused BEFORE
//              touching the device (§10).
//   Ladder     §11/§32 ordered eviction — never OOM.
//   Streams    fixed lanes; decode has elevated priority; daily sync
//              is stream-scoped or event-based, never device-wide.
//   Pinned     bounded host ring (max_pinned_host_bytes, §13/§14).
//   Telemetry  star-cuda-memory-telemetry/v1 (§53-§55).
//
// Governed callers leave allow_sim=false — allocation failure is a
// typed miss, never a silent host fallback. The contract probe may
// enable allow_sim to verify plane semantics on GPU-less hosts.

#pragma once

#if defined(XINGCHENG_CUDA)
#include <cuda_runtime.h>
#endif

#include <cstdint>
#include <mutex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

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
    // Contract probes may set this on GPU-less hosts; governed lanes
    // never do — allocation failure there is a typed miss.
    bool allow_sim = false;
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

    std::mutex mu;
    // ptr -> (tier, bytes) for every outstanding device allocation so
    // shutdown and engine-unload can release exactly what they own.
    std::unordered_map<void*, std::pair<Tier, int64_t>> owned;

#if defined(XINGCHENG_CUDA)
    cudaMemPool_t pool = nullptr;
    cudaStream_t streams[6] = {};
    void* pinned_dev = nullptr;      // cudaHostAlloc ring
    int64_t pinned_ring_size = 0;
    int64_t pinned_ring_off = 0;
#endif

    /// budget_bytes=0 derives the budget from the device's free memory
    /// (free - 256MB floor) so the engine lane needs no external
    /// sizing; explicit callers pass a hard cap instead.
    int init(int64_t budget_bytes, int64_t pinned_cap,
             bool allow_sim_ = false) {
        if (initialized) return 0;
        allow_sim = allow_sim_;
        pinned_host_cap = pinned_cap;
#if defined(XINGCHENG_CUDA)
        int dev = 0;
        cuda_present =
            cudaGetDeviceCount(&dev) == cudaSuccess && dev > 0;
        if (cuda_present) {
            if (budget_bytes <= 0) {
                size_t free_b = 0, total_b = 0;
                if (cudaMemGetInfo(&free_b, &total_b) == cudaSuccess)
                    budget_bytes =
                        (int64_t)free_b - (256LL << 20);
            }
            cudaDeviceProp prop{};
            if (cudaGetDeviceProperties(&prop, 0) != cudaSuccess)
                return 3;
            if (prop.major * 10 + prop.minor < 86)
                return 4;   // §62: sm_86 floor
            cudaMemPoolProps props{};
            props.allocType = cudaMemAllocationTypePinned;
            props.handleTypes = cudaMemHandleTypeNone;
            props.location.type = cudaMemLocationTypeDevice;
            props.location.id = 0;
            if (cudaMemPoolCreate(&pool, &props) != cudaSuccess)
                return 5;
            uint64_t thresh = ~0ull;   // §6 high-water reuse
            if (cudaMemPoolSetAttribute(
                    pool, cudaMemPoolAttrReleaseThreshold,
                    &thresh) != cudaSuccess)
                return 6;
            for (int i = 0; i < 6; ++i) {
                int pri = i == (int)StreamLane::DECODE_HIGH ? -5 : 0;
                if (cudaStreamCreateWithPriority(
                        &streams[i], cudaStreamNonBlocking,
                        pri) != cudaSuccess)
                    return 7;
            }
            if (pinned_cap > 0 &&
                cudaHostAlloc(&pinned_dev, (size_t)pinned_cap,
                              cudaHostAllocDefault) == cudaSuccess) {
                pinned_ring_size = pinned_cap;
            }
        }
#else
        cuda_present = false;
#endif
        if (budget_bytes <= 0) budget_bytes = 4LL << 30;
        budget = budget_bytes;
        emergency_headroom = budget_bytes / 10;
        cuda_reserve = budget_bytes / 20;
        graph_reserve = budget_bytes / 20;
        initialized = true;
        return 0;
    }

    bool ensure(int64_t pinned_cap = 64LL << 20) {
        if (initialized) return true;
        return init(0, pinned_cap, false) == 0;
    }

    int64_t free_budget() const {
        return budget - emergency_headroom - cuda_reserve -
               graph_reserve - pool_used;
    }

#if defined(XINGCHENG_CUDA)
    cudaStream_t stream(StreamLane l) const { return streams[(int)l]; }
#endif

    /// §4/§5: stream-ordered pool allocation; never a naked cudaMalloc.
    /// Returns nullptr over budget — callers walk the ladder (§11).
    void* alloc(Tier tier, int64_t bytes, StreamLane lane) {
        if (!initialized || bytes <= 0) return nullptr;
        if (free_budget() < bytes) return nullptr;
#if defined(XINGCHENG_CUDA)
        if (cuda_present) {
            std::lock_guard<std::mutex> lk(mu);
            void* p = nullptr;
            if (cudaMallocFromPoolAsync(
                    &p, (size_t)bytes, pool,
                    streams[(int)lane]) != cudaSuccess)
                return nullptr;
            owned[p] = {tier, bytes};
            account(tier, bytes);
            return p;
        }
#endif
        if (!allow_sim) return nullptr;
        account(tier, bytes);
        return this;   // sentinel — sims dereference nothing
    }

    void free(void* p, StreamLane lane) {
        if (p == nullptr) return;
        std::lock_guard<std::mutex> lk(mu);
        auto it = owned.find(p);
        if (it != owned.end()) {
            unaccount(it->second.first, it->second.second);
            owned.erase(it);
        }
#if defined(XINGCHENG_CUDA)
        if (cuda_present && p != this)
            cudaFreeAsync(p, streams[(int)lane]);
#endif
    }

    /// Release every outstanding owned allocation — engine unload /
    /// generation switch / explicit maintenance only (§6).
    void free_all(StreamLane lane = StreamLane::H2D) {
        std::lock_guard<std::mutex> lk(mu);
#if defined(XINGCHENG_CUDA)
        if (cuda_present)
            for (auto& kv : owned)
                cudaFreeAsync(kv.first, streams[(int)lane]);
#endif
        owned.clear();
        for (auto& b : tier_bytes) b = 0;
        pool_used = 0;
    }

    /// Pinned ring acquire: bump-allocate inside the bounded ring; a
    /// miss means "use pageable" — pinning is a cap, not a right (§14).
    void* pinned_acquire(int64_t bytes) {
#if defined(XINGCHENG_CUDA)
        if (!cuda_present || pinned_dev == nullptr) return nullptr;
        if (pinned_ring_off + bytes > pinned_ring_size) return nullptr;
        void* p = (char*)pinned_dev + pinned_ring_off;
        pinned_ring_off += bytes;
        pinned_host_bytes += bytes;
        return p;
#else
        return nullptr;
#endif
    }

    /// §3/§13: every pinned host allocation flows through the manager.
    /// The bounded ring serves the frequent small transfers; a larger
    /// request gets a managed cudaHostAlloc tracked for release — the
    /// cap still binds the total.
    void* pinned_alloc(int64_t bytes) {
#if defined(XINGCHENG_CUDA)
        if (!cuda_present) return nullptr;
        if (pinned_host_bytes + bytes > pinned_host_cap &&
            pinned_host_cap > 0)
            return nullptr;   // §14: the cap binds
        if (void* p = pinned_acquire(bytes)) return p;
        void* p = nullptr;
        if (cudaHostAlloc(&p, (size_t)bytes,
                          cudaHostAllocDefault) != cudaSuccess)
            return nullptr;
        pinned_extra.push_back(p);
        pinned_host_bytes += bytes;
        return p;
#else
        return nullptr;
#endif
    }

    std::vector<void*> pinned_extra;   // managed non-ring pinned bufs

    void pinned_release_all() {
#if defined(XINGCHENG_CUDA)
        for (void* p : pinned_extra) cudaFreeHost(p);
        pinned_extra.clear();
#endif
        pinned_ring_off = 0;
        pinned_host_bytes = 0;
    }

    void account(Tier tier, int64_t bytes) {
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
    }

    void unaccount(Tier tier, int64_t bytes) {
        tier_bytes[(int)tier] -= bytes;
        pool_used -= bytes;
    }

    /// §8/§9 token-boundary reset: TOKEN/LAYER/SCRATCH lifetimes end
    /// together — arenas alias, they do not grow per layer.
    void reset_token_scope() {
        std::vector<void*> gone;
        for (auto& kv : owned)
            if (kv.second.first == Tier::TOKEN_PERSISTENT ||
                kv.second.first == Tier::LAYER_TEMP ||
                kv.second.first == Tier::KERNEL_SCRATCH)
                gone.push_back(kv.first);
        for (void* p : gone) free(p, StreamLane::DECODE_HIGH);
        if (!cuda_present) {   // sim path tracks bytes only
            pool_used -= tier_bytes[(int)Tier::TOKEN_PERSISTENT] +
                         tier_bytes[(int)Tier::LAYER_TEMP] +
                         tier_bytes[(int)Tier::KERNEL_SCRATCH];
            tier_bytes[(int)Tier::TOKEN_PERSISTENT] = 0;
            tier_bytes[(int)Tier::LAYER_TEMP] = 0;
            tier_bytes[(int)Tier::KERNEL_SCRATCH] = 0;
        }
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
            pinned_release_all();
            for (auto& s : streams) if (s) cudaStreamDestroy(s);
            if (pinned_dev) cudaFreeHost(pinned_dev);
            if (pool) cudaMemPoolDestroy(pool);
        }
#endif
        bool sim = allow_sim;
        *this = Manager{};
        allow_sim = sim;
    }
};

inline Manager& mgr() {
    static Manager m;   // one instance per process across all TUs
    return m;
}

}  // namespace xcm_memplane
