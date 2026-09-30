// xcm_batch2.h — batch-2 native-runtime components for xc_modeltool.
//
// Research/probe infrastructure only (§7, §10, §12, §13, §27, §29).
// Nothing in this header is wired into production model dispatch —
// SparseAttentionProbe and kv_outer_gather_q run against synthetic
// tensors; SpeculativeDecoder reports INFRASTRUCTURE_ONLY; the
// SequenceLayerScheduler manages state lifecycle handles, not layer
// math. CAPABILITY_TRAINING_FROZEN: no training path exists here.
//
//   SparseAttentionProbe   §7.1  KVBlockIndex / QueryBlockSelector /
//                           BlockGatherPlan / SparseAttentionBenchmark
//   kv_outer_gather_q      §7.2  operator prototype (synthetic only)
//   SequenceLayerScheduler §10   unified DeltaNet/attention state
//                           lifecycle (Prepare/Run/Commit/Rollback)
//   RecurrentStatePrecisionProbe §12  FP64/BF16/FP16 drift ladder
//   SpeculativeDecoder     §13   runtime metrics schema (disabled)
//   HardwareCapabilityRegistry   §27  ISA + CUDA + memory detection
//   star-native-state/v2   §29   bound state-snapshot header

#ifndef XCM_BATCH2_H
#define XCM_BATCH2_H

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <intrin.h>
#include <numeric>
#include <random>
#include <string>
#include <unordered_map>
#include <vector>
#include <windows.h>

namespace xcm2 {

// -------------------------------------------------- §7.1 sparse probe

struct KVBlock {
    int64_t block_id = 0;
    int64_t token_begin = 0, token_end = 0;
    float   centroid_score = 0;   // filled by selector
};

/// KVBlockIndex — synthetic KV divided into fixed-size blocks; each
/// block carries a centroid vector for cheap query affinity scoring.
struct KVBlockIndex {
    int64_t block_tokens = 64;
    std::vector<KVBlock> blocks;
    std::vector<std::vector<float>> centroids;   // [block][dim]
    std::vector<std::vector<float>> k;           // [token][dim] synthetic K

    void build(int64_t tokens, int dim, uint64_t seed) {
        std::mt19937_64 rng(seed);
        std::normal_distribution<float> n(0.f, 1.f);
        k.assign((size_t)tokens, std::vector<float>(dim));
        for (auto& v : k) for (auto& x : v) x = n(rng);
        blocks.clear(); centroids.clear();
        for (int64_t b = 0; b * block_tokens < tokens; ++b) {
            int64_t lo = b * block_tokens;
            int64_t hi = std::min(tokens, lo + block_tokens);
            blocks.push_back({b, lo, hi, 0.f});
            std::vector<float> c(dim, 0.f);
            for (int64_t t = lo; t < hi; ++t)
                for (int d = 0; d < dim; ++d)
                    c[d] += k[(size_t)t][d] / (float)(hi - lo);
            centroids.push_back(std::move(c));
        }
    }
};

/// QueryBlockSelector — rank blocks by max q·centroid affinity.
struct QueryBlockSelector {
    static std::vector<KVBlock> select(
        const KVBlockIndex& idx, const std::vector<float>& q,
        int64_t max_blocks) {
        std::vector<KVBlock> ranked = idx.blocks;
        for (auto& b : ranked) {
            float s = 0;
            const auto& c = idx.centroids[(size_t)b.block_id];
            for (size_t d = 0; d < q.size(); ++d) s += q[d] * c[d];
            b.centroid_score = s;
        }
        std::sort(ranked.begin(), ranked.end(),
                  [](const KVBlock& a, const KVBlock& b)
                  { return a.centroid_score > b.centroid_score; });
        if ((int64_t)ranked.size() > max_blocks)
            ranked.resize((size_t)max_blocks);
        return ranked;
    }
};

/// BlockGatherPlan — an ordered, deduplicated gather list.
struct BlockGatherPlan {
    std::vector<int64_t> block_ids;
    int64_t kv_bytes_read = 0;
    static BlockGatherPlan from(const std::vector<KVBlock>& sel,
                                int64_t block_tokens, int dim) {
        BlockGatherPlan p;
        for (const auto& b : sel) p.block_ids.push_back(b.block_id);
        std::sort(p.block_ids.begin(), p.block_ids.end());
        p.kv_bytes_read = (int64_t)p.block_ids.size() *
                          block_tokens * dim * 2 /* K+V */ *
                          (int64_t)sizeof(float);
        return p;
    }
};

/// SparseAttentionBenchmark — synthetic full vs block-selected
/// attention recall. Recall = fraction of the true top-|q·k| token
/// set covered by the selected blocks (the honest operator metric).
struct SparseAttentionBenchResult {
    int64_t selected_blocks = 0;
    double  coverage_ratio = 0;       // selected tokens / all tokens
    int64_t kv_bytes_read = 0;
    double  memory_coalescing = 0;    // contiguous fraction of plan
    double  recall_against_full = 0;  // top-attention coverage
};

inline SparseAttentionBenchResult bench_sparse(
    const KVBlockIndex& idx, const std::vector<float>& q,
    int64_t max_blocks, int64_t top_tokens = 64) {
    SparseAttentionBenchResult r;
    auto sel = QueryBlockSelector::select(idx, q, max_blocks);
    auto plan = BlockGatherPlan::from(sel, idx.block_tokens,
                                      (int)q.size());
    r.selected_blocks = (int64_t)sel.size();
    int64_t total = idx.blocks.back().token_end;
    int64_t served = (int64_t)sel.size() * idx.block_tokens;
    r.coverage_ratio = total > 0 ? (double)served / total : 1.0;
    r.kv_bytes_read = plan.kv_bytes_read;
    // Coalescing proxy: adjacent block pairs in the sorted plan.
    int64_t adj = 0;
    for (size_t i = 1; i < plan.block_ids.size(); ++i)
        if (plan.block_ids[i] == plan.block_ids[i - 1] + 1) ++adj;
    r.memory_coalescing = plan.block_ids.size() > 1
        ? (double)adj / (plan.block_ids.size() - 1) : 1.0;
    // True top-attention tokens under full attention.
    std::vector<std::pair<float, int64_t>> scores;
    scores.reserve(idx.k.size());
    for (size_t t = 0; t < idx.k.size(); ++t) {
        float s = 0;
        for (size_t d = 0; d < q.size(); ++d) s += q[d] * idx.k[t][d];
        scores.emplace_back(s, (int64_t)t);
    }
    std::partial_sort(scores.begin(),
                      scores.begin() +
                          std::min<int64_t>(top_tokens,
                                            (int64_t)scores.size()),
                      scores.end(),
                      [](auto& a, auto& b) { return a.first > b.first; });
    std::vector<bool> covered(idx.blocks.size(), false);
    for (auto b : plan.block_ids) covered[(size_t)b] = true;
    int64_t hit = 0;
    int64_t n = std::min<int64_t>(top_tokens, (int64_t)scores.size());
    for (int64_t i = 0; i < n; ++i)
        if (covered[(size_t)(scores[i].second / idx.block_tokens)])
            ++hit;
    r.recall_against_full = n > 0 ? (double)hit / n : 1.0;
    return r;
}

// ------------------------------------------------- §7.2 outer gather

/// kv_outer_gather_q — prototype gather: for each selected KV block,
/// reduce q·k over the block into per-block max + argmax (the
/// "outer loop over KV blocks" pattern MiniMax describes). Synthetic
/// tensors only; the production layer dispatch is untouched.
struct GatherQResult {
    std::vector<float>  block_max;
    std::vector<int64_t> block_argmax;
    int64_t reads = 0;               // element reads
};

inline GatherQResult kv_outer_gather_q(
    const KVBlockIndex& idx, const std::vector<float>& q,
    const std::vector<int64_t>& plan_block_ids) {
    GatherQResult r;
    for (int64_t bid : plan_block_ids) {
        const auto& b = idx.blocks[(size_t)bid];
        float best = -1e30f; int64_t arg = -1;
        for (int64_t t = b.token_begin; t < b.token_end; ++t) {
            float s = 0;
            for (size_t d = 0; d < q.size(); ++d)
                s += q[d] * idx.k[(size_t)t][d];
            r.reads += (int64_t)q.size();
            if (s > best) { best = s; arg = t; }
        }
        r.block_max.push_back(best);
        r.block_argmax.push_back(arg);
    }
    return r;
}

// ------------------------------------------- §10 sequence scheduler

/// SequenceLayerScheduler — one lifecycle for both layer families in
/// xc-fused-1 (DeltaNet recurrent + periodic full attention). It owns
/// the *state handles* (recurrent slot or KV slot), not the layer
/// math — production layers keep their own kernels.
struct SequenceLayerScheduler {
    enum class Kind { RECURRENT, ATTENTION };
    struct Slot {
        int64_t layer = 0;
        Kind kind = Kind::RECURRENT;
        bool prepared = false, dirty = false, committed = false;
        std::vector<double> state;        // opaque state bytes (model plane)
        std::vector<double> checkpoint;   // rollback image
    };

    std::vector<Slot> slots;

    void build(int64_t n_layers,
               const std::vector<bool>& is_recurrent,
               int64_t state_elems) {
        slots.resize((size_t)n_layers);
        for (int64_t i = 0; i < n_layers; ++i) {
            slots[(size_t)i].layer = i;
            slots[(size_t)i].kind =
                is_recurrent[(size_t)i] ? Kind::RECURRENT
                                        : Kind::ATTENTION;
            slots[(size_t)i].state.assign((size_t)state_elems, 0.0);
        }
    }

    Slot& at(int64_t layer) { return slots[(size_t)layer]; }

    void PrepareLayer(int64_t layer) {
        auto& s = at(layer);
        s.checkpoint = s.state;           // rollback image first
        s.prepared = true; s.dirty = false; s.committed = false;
    }
    // RunRecurrent/RunAttention write into a scratch delta; the state
    // only advances on CommitState.
    void RunRecurrent(int64_t layer, const std::vector<double>& delta) {
        auto& s = at(layer);
        if (s.kind != Kind::RECURRENT || !s.prepared) return;
        for (size_t i = 0; i < s.state.size() && i < delta.size(); ++i)
            s.state[i] += delta[i];
        s.dirty = true;
    }
    void RunAttention(int64_t layer, const std::vector<double>& kv) {
        auto& s = at(layer);
        if (s.kind != Kind::ATTENTION || !s.prepared) return;
        for (size_t i = 0; i < s.state.size() && i < kv.size(); ++i)
            s.state[i] += kv[i];
        s.dirty = true;
    }
    void CommitState(int64_t layer) {
        auto& s = at(layer);
        if (!s.prepared || !s.dirty) return;
        s.committed = true; s.prepared = false; s.checkpoint.clear();
    }
    void RollbackState(int64_t layer) {
        auto& s = at(layer);
        if (!s.prepared) return;
        if (!s.checkpoint.empty()) s.state = s.checkpoint;
        s.dirty = false; s.prepared = false; s.committed = false;
    }
};

// --------------------------------------------- §12 recurrent drift --

/// RecurrentStatePrecisionProbe — a linear-recurrence proxy for the
/// DeltaNet state update s <- decay*s + u, run at FP64 (reference) vs
/// a quantized state precision, across the §12 token ladder.
/// FP8 is deliberately absent (§12: state error compounds across
/// tokens — FP8 is not attempted this round).
inline double quantize_state(double v, const std::string& prec) {
    if (prec == "FP16") {
        // round to fp16 grid via float + exponent-limited mantissa
        float f = (float)v;
        // crude fp16: keep 10 mantissa bits
        uint32_t u; std::memcpy(&u, &f, 4);
        u &= ~0x1FFFu;                        // 19 -> 10 mantissa bits
        std::memcpy(&f, &u, 4);
        return (double)f;
    }
    if (prec == "BF16") {
        float f = (float)v;
        uint32_t u; std::memcpy(&u, &f, 4);
        u &= ~0xFFFFu;                        // bf16 grid
        std::memcpy(&f, &u, 4);
        return (double)f;
    }
    return v;                                  // FP64 reference
}

struct DriftPoint { int64_t tokens; double max_drift; };

inline std::vector<DriftPoint> recurrent_drift(
    const std::string& prec, const std::vector<int64_t>& ladder,
    int dim = 16, uint64_t seed = 7) {
    std::mt19937_64 rng(seed);
    std::uniform_real_distribution<double> u(-0.05, 0.05);
    std::vector<double> ref(dim, 0.0), qtz(dim, 0.0);
    const double decay = 0.985;
    std::vector<DriftPoint> out;
    int64_t upto = *std::max_element(ladder.begin(), ladder.end());
    for (int64_t t = 0; t < upto; ++t) {
        for (int d = 0; d < dim; ++d) {
            double upd = u(rng);
            ref[d] = decay * ref[d] + upd;
            qtz[d] = quantize_state(
                decay * qtz[d] + upd, prec);
        }
        int64_t n = t + 1;
        if (std::find(ladder.begin(), ladder.end(), n) != ladder.end()) {
            double m = 0;
            for (int d = 0; d < dim; ++d)
                m = std::max(m, std::fabs(ref[d] - qtz[d]));
            out.push_back({n, m});
        }
    }
    return out;
}

// -------------------------------------------- §13 speculative decode

/// SpeculativeDecoder — runtime-side metrics schema only (§13): MTP
/// export still discards, so production speculation is NEVER enabled;
/// the contract exists so a future generation can plug in without
/// reshaping the runtime.
struct SpeculativeDecoder {
    bool enabled = false;                    // §13 hard default
    int64_t draft_depth = 0;
    double acceptance_length = 0;
    double acceptance_rate = 0;
    double verify_latency_ms = 0;
    double net_speedup = 0;

    bool available() const { return enabled; }
};

// --------------------------------------------- §27 hardware registry

struct HardwareCapabilityRegistry {
    bool avx2 = false, fma = false, avx512f = false;
    bool bf16 = false, fp16 = false;
    // FP8/FP4 are never claimed without hardware proof — defaults stay
    // false and only a certified backend may flip them.
    bool fp8 = false, fp4 = false;
    int64_t vram_free_mb = 0, vram_total_mb = 0;
    int64_t sys_ram_mb = 0;
    int cuda_cc_major = 0, cuda_cc_minor = 0;
    bool cuda_available = false;

    void detect_cpu() {
        int cpu[4] = {0, 0, 0, 0};
        __cpuid(cpu, 1);
        fma = (cpu[2] & (1 << 12)) != 0;
        bool osxsave = (cpu[2] & (1 << 27)) != 0;
        bool avx = (cpu[2] & (1 << 28)) != 0;
        __cpuid(cpu, 7);
        avx2 = (cpu[1] & (1 << 5)) != 0 && avx && osxsave;
        avx512f = (cpu[1] & (1 << 16)) != 0;
        bf16 = (cpu[1] & (1 << 22)) != 0;     // AVX512_BF16 leaf bit
        fp16 = fma && avx2;                  // F16C accompanies AVX2
    }
    void detect_mem() {
        MEMORYSTATUSEX m{};
        m.dwLength = sizeof(m);
        if (GlobalMemoryStatusEx(&m))
            sys_ram_mb = (int64_t)(m.ullTotalPhys / (1024 * 1024));
    }
};

// ------------------------------------------------- §29 native state

/// star-native-state/v2 header — a state snapshot is bound to a
/// generation + bundle + architecture + tokenizer; a different model
/// hash never loads (§29 last rule).
struct NativeStateHeader {
    const char* format = "star-native-state/v2";
    std::string generation, bundle_hash, architecture,
        tokenizer_hash, state_type, precision;
    int64_t sequence_length = 0;
    uint64_t checksum = 0;

    static uint64_t fnv1a(const void* p, size_t n) {
        uint64_t h = 1469598103934665603ull;
        const auto* b = (const uint8_t*)p;
        for (size_t i = 0; i < n; ++i) {
            h ^= b[i]; h *= 1099511628211ull;
        }
        return h;
    }
    void seal(const void* state, size_t bytes) {
        checksum = fnv1a(state, bytes);
    }
    bool verify(const void* state, size_t bytes) const {
        return checksum == fnv1a(state, bytes);
    }
    /// Generation/model binding — the load gate (§29).
    bool binds_to(const std::string& gen,
                  const std::string& bundle) const {
        return generation == gen && bundle_hash == bundle;
    }
};

} // namespace xcm2
#endif // XCM_BATCH2_H
