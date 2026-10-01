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
// Pure Driver API binding (nvcuda.dll) — no toolkit headers, no cudart
// link. The manager's pool/streams/pinned memory all come from the
// retained primary context in cuda_drvapi.h.
#include "cuda_drvapi.h"
namespace xcd = xcuda_drv;
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
    xcd::CUmempool_t pool = 0;       // device default pool
    xcd::CUstream_t streams[6] = {};
    void* pinned_dev = nullptr;      // cuMemHostAlloc ring
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
        cuda_present = xcd::device_ready();
        if (cuda_present) {
            xcd::Api& a = xcd::api();
            if (!xcd::use_ctx()) return 3;
            xcd::Dev& d = xcd::dev();
            if (d.cc_major * 10 + d.cc_minor < 86)
                return 4;   // §62: sm_86 floor
            if (budget_bytes <= 0) {
                size_t free_b = 0, total_b = 0;
                if (a.mem_get_info(&free_b, &total_b) == xcd::kOk)
                    budget_bytes =
                        (int64_t)free_b - (256LL << 20);
            }
            if (a.device_default_pool(&pool, d.device) != xcd::kOk)
                return 5;
            unsigned long long thresh = ~0ull;  // §6 high-water reuse
            if (a.pool_set_attribute(
                    pool, xcd::kMempoolReleaseThreshold,
                    &thresh) != xcd::kOk)
                return 6;
            for (int i = 0; i < 6; ++i) {
                int pri = i == (int)StreamLane::DECODE_HIGH ? -5 : 0;
                if (a.stream_create_pri(
                        &streams[i], xcd::kStreamNonBlocking,
                        pri) != xcd::kOk)
                    return 7;
            }
            if (pinned_cap > 0 &&
                a.host_alloc(&pinned_dev, (size_t)pinned_cap,
                             xcd::kHostAllocDefault) == xcd::kOk) {
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
    xcd::CUstream_t stream(StreamLane l) const { return streams[(int)l]; }
#endif

    /// §4/§5: stream-ordered pool allocation; never a naked cuMemAlloc.
    /// Returns nullptr over budget — callers walk the ladder (§11).
    void* alloc(Tier tier, int64_t bytes, StreamLane lane) {
        if (!initialized || bytes <= 0) return nullptr;
        if (free_budget() < bytes) return nullptr;
#if defined(XINGCHENG_CUDA)
        if (cuda_present) {
            std::lock_guard<std::mutex> lk(mu);
            xcd::Api& a = xcd::api();
            if (!xcd::use_ctx()) return nullptr;
            xcd::CUdevptr_t p = 0;
            if (a.pool_alloc_async(
                    &p, (size_t)bytes, pool,
                    streams[(int)lane]) != xcd::kOk || p == 0)
                return nullptr;
            // Pool memory's availability is stream-ordered to the
            // allocating lane — synchronize before handing the pointer
            // to any other stream (the legacy stream, compute lanes).
            // Allocation is a growth/lifecycle event, never a
            // per-token one, so the scoped wait stays off the hot path.
            if (a.stream_sync(streams[(int)lane]) != xcd::kOk) {
                a.mem_free_async(p, streams[(int)lane]);
                return nullptr;
            }
            void* h = reinterpret_cast<void*>(static_cast<uintptr_t>(p));
            owned[h] = {tier, bytes};
            account(tier, bytes);
            return h;
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
        if (cuda_present && p != this) {
            // Same stream-ordering rule as alloc: drain the lane before
            // the pool may recycle the block for another stream's use.
            xcd::Api& a = xcd::api();
            if (!xcd::use_ctx()) return;
            a.stream_sync(streams[(int)lane]);
            a.mem_free_async(
                static_cast<xcd::CUdevptr_t>(
                    reinterpret_cast<uintptr_t>(p)),
                streams[(int)lane]);
        }
#endif
    }

    /// Release every outstanding owned allocation — engine unload /
    /// generation switch / explicit maintenance only (§6).
    void free_all(StreamLane lane = StreamLane::H2D) {
        std::lock_guard<std::mutex> lk(mu);
#if defined(XINGCHENG_CUDA)
        if (cuda_present && xcd::use_ctx()) {
            xcd::Api& a = xcd::api();
            a.stream_sync(streams[(int)lane]);
            for (auto& kv : owned)
                a.mem_free_async(
                    static_cast<xcd::CUdevptr_t>(
                        reinterpret_cast<uintptr_t>(kv.first)),
                    streams[(int)lane]);
        }
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
        if (!xcd::use_ctx()) return nullptr;
        void* p = nullptr;
        if (xcd::api().host_alloc(&p, (size_t)bytes,
                                  xcd::kHostAllocDefault) != xcd::kOk)
            return nullptr;
        pinned_extra.emplace_back(p, bytes);
        pinned_host_bytes += bytes;
        return p;
#else
        return nullptr;
#endif
    }

    // managed non-ring pinned bufs (ptr, bytes)
    std::vector<std::pair<void*, int64_t>> pinned_extra;

    /// Individually free a managed-extra pinned allocation; ring bump
    /// space is not individually freeable — its accounting stays
    /// honest until pinned_release_all.
    void pinned_free(void* p) {
#if defined(XINGCHENG_CUDA)
        if (p == nullptr) return;
        for (auto it = pinned_extra.begin(); it != pinned_extra.end();
             ++it) {
            if (it->first == p) {
                pinned_host_bytes -= it->second;
                if (xcd::use_ctx()) xcd::api().host_free(it->first);
                pinned_extra.erase(it);
                return;
            }
        }
#else
        (void)p;
#endif
    }

    void pinned_release_all() {
#if defined(XINGCHENG_CUDA)
        if (xcd::use_ctx())
            for (auto& e : pinned_extra) xcd::api().host_free(e.first);
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
        if (cuda_present && xcd::use_ctx()) {
            xcd::Api& a = xcd::api();
            pinned_release_all();
            for (auto& s : streams) if (s) a.stream_destroy(s);
            if (pinned_dev) a.host_free(pinned_dev);
            // The pool is the device default mempool — owned by the
            // primary context, never destroyed here.
        }
#endif
        initialized = false;
        cuda_present = false;
        budget = emergency_headroom = cuda_reserve = graph_reserve = 0;
        for (auto& b : tier_bytes) b = 0;
        for (auto& b : tier_peak) b = 0;
        pool_reserved = pool_used = pool_peak = 0;
        pinned_host_bytes = pinned_host_cap = 0;
        h2d_bytes = d2h_bytes = d2d_bytes = 0;
        workspace_peak = ladder_events = allocs = 0;
        owned.clear();
#if defined(XINGCHENG_CUDA)
        pool = nullptr;
        for (auto& s : streams) s = nullptr;
        pinned_dev = nullptr;
        pinned_ring_size = pinned_ring_off = 0;
#endif
    }
};

inline Manager& mgr() {
    static Manager m;   // one instance per process across all TUs
    return m;
}

}  // namespace xcm_memplane
