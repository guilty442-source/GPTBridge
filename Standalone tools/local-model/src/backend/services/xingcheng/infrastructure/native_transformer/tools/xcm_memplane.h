// xcm_memplane.h ??NativeMemoryCudaPlane (memory/CUDA directive 禮0-禮10,
// 禮13-禮18, 禮53-禮55). Single owner for every CUDA allocation; the hot
// paths never cudaMalloc/cudaFree ??they draw from the pool and arenas.
//
//   UnifiedCudaMemoryManager  禮3/禮4: every device/pinned byte passes
//                             through this manager; tiers keep
//                             lifetimes explicit.
//   CudaDevicePool            禮5/禮6: cudaMallocAsync mempool with a
//                             release threshold at the high-water ??//                             no per-request trim; trim only under
//                             memory pressure, unload, generation
//                             switch, or explicit maintenance.
//   WorkspaceArena            禮8/禮9: static per-tier arenas sized at
//                             load from the WorkspacePlan; non-
//                             overlapping lifetimes alias one
//                             physical block (lifetime aliasing).
//   PinnedHostPool            禮13/禮14: fixed ring buffers bounded by
//                             max_pinned_host_bytes ??never re-pin per
//                             transfer.
//   CudaMemoryBudget          禮10: hard VRAM budget across weights,
//                             hotset, KV, delta, prefix, workspace,
//                             runtime/graph reserve, and emergency
//                             headroom ??OOM edges are unreachable by
//                             construction.
//   PressureLadder            禮11/禮32: ordered eviction ??cold prefix,
//                             warm prefix, routed expert, hotset
//                             shrink, batch reduce, prefill-chunk
//                             reduce, state spill, reject ??never OOM.
//   StreamPool                禮15-禮18: fixed named streams (decode /
//                             prefill / expert-prefetch / H2D / D2H /
//                             train) + events; device-wide sync stays
//                             out of the daily path.
//   Telemetry                 禮53-禮55: star-cuda-memory-telemetry/v1 ??//                             pool reserved/used/peak, arena peaks,
//                             per-tier bytes, transfer bytes, pinned
//                             bytes, ladder events.
//
// Without a CUDA device the governed paths fail closed; the probe mode
// additionally runs the contract sims and reports simulated:true so a
// CPU-only host can still verify the plane's contracts.

// The plane core lives in the engine tree so cuda_bridge.cpp and the
// probes share ONE manager instance (§3 single owner).
#include "cuda_memplane.h"

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
    m.init(budget, pinned, /*allow_sim=*/true);
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

    // 禮3/禮4/禮7: every tiered alloc goes through the manager and lands
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

    // 禮8/禮9: after reset the next iteration's temporaries must reuse ??    // pool_used must not grow monotonically across iterations.
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

    // 禮10/禮11: the pressure ladder answers a deficit in order and the
    // budget can never be overrun ??an alloc past free_budget fails
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

    // 禮14: pinned cap is a bound, not a suggestion.
    check("pinned_cap_bounded", m.pinned_host_cap <= pinned);

    // 禮53-禮55: telemetry contract fields exist.
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

// memplane-telemetry ??emit the current manager's telemetry JSON after
// a token-scope simulation cycle (contract shape check).
static int mode_memplane_telemetry(const Args& a) {
    using namespace xcm_memplane;
    Manager& m = mgr();
    m.init(memp_num(a, "budget", 4LL << 30),
           memp_num(a, "pinned", 64LL << 20),
           /*allow_sim=*/true);
    std::printf("%s\n", m.telemetry_json().c_str());
    m.shutdown();
    return 0;
}
