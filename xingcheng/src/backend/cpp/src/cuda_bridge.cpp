// Stable C ABI for governed CUDA offloading of the fp64 layer path.
//
// Architecture: pure NVIDIA Driver API via nvcuda.dll (cuda_drvapi.h) —
// no CUDA runtime, no cuBLAS, no NVRTC, no toolkit linkage anywhere in
// this TU.  Device memory is owned by UnifiedCudaMemoryManager (§3 §66);
// a two-lane stream layout mirrors the retirement of the old cublas path:
//   PREFILL lane  — all in-tree fp64 GEMM launches (xc_gemm_f64 PTX)
//   D2H lane      — result copies back to the host
// The H2D lane feeds PREFILL via a driver event so transient layer inputs
// never block the decode graph lane; D2H waits on PREFILL via a second
// event.  The compiler/runtime split keeps ROCm and CPU parity intact —
// XC_DEVICE=cpu is honoured before any device work is scheduled.
//
// Numerical contract (identical to the retired cuBLAS call):
//   C(m×n) = A(m×k) * B(k×n)   row-major, fp64, no alpha/beta scaling —
//   handled by xc_gemm_f64 in the in-tree PTX module with the same
//   streaming-sequential FMA accumulation order the CPU reference uses.
//
// Local decode prefill chooses this lane only for m >= 48 (smaller batches
// stay on the CPU AVX2 path, matching xc_model_prefill_matmul_f64).

#ifdef XINGCHENG_CUDA

#include <windows.h>

#include <climits>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>

#include "cuda_memplane.h"   // UnifiedCudaMemoryManager + xcd alias
#include "cuda_drvapi.h"

namespace mp = xcm_memplane;

// The in-tree PTX module lives in cuda_kernels.cpp (XINGCHENG_CUDA_KERNELS).
// These are the two cross-TU entry points it exports; both are fail-closed.
#if defined(XINGCHENG_CUDA_KERNELS)
extern "C" int xcuda_dev_probe();
extern "C" int xcuda_dev_gemm_f64(unsigned long long da,
                                  unsigned long long db,
                                  unsigned long long dc, long long m,
                                  long long k, long long n,
                                  unsigned long long stream);
extern "C" int xcuda_bf16_kernel_probe();
extern "C" int xcuda_fp8_kernel_probe();
extern "C" int xcuda_kv_kernel_probe();
extern "C" int xcuda_bf16_release_weights();
extern "C" int xcuda_fp8_release_weights();
extern "C" void xcuda_kv_free();
#else
extern "C" int xcuda_dev_probe() { return 0; }
extern "C" int xcuda_dev_gemm_f64(unsigned long long, unsigned long long,
                                  unsigned long long, long long, long long,
                                  long long, unsigned long long) {
    return 3;
}
extern "C" int xcuda_bf16_release_weights() { return 0; }
extern "C" int xcuda_fp8_release_weights() { return 0; }
extern "C" void xcuda_kv_free() {}
#endif

namespace {

// Device-resident weights cache: host weight pointer → (device ptr, bytes).
// Pointers live under PINNED_PERMANENT so they are never pooled back, matching
// the retirement of the old cudart cache.  Protected by g_mu.
std::mutex g_mu;
std::unordered_map<const void*, std::pair<void*, size_t>> g_dev_weights;

// Transient device operands for the current matmul call.  These are not
// owned across the ABI boundary — scratch is refreshed each call and the
// pointer may rotate inside the unified pool (bytes are budgeted).
struct DevBuf {
    void* ptr = nullptr;
    size_t bytes = 0;
};

DevBuf g_dev_a;
DevBuf g_dev_c;

bool dev_get(DevBuf& b, size_t need) {
    if (b.bytes >= need) return true;
    if (b.ptr != nullptr) {
        mp::mgr().free(b.ptr, mp::StreamLane::PREFILL_NORMAL);
        b = {};
    }
    b.ptr = mp::mgr().alloc(mp::Tier::LAYER_TEMP, (int64_t)need,
                            mp::StreamLane::PREFILL_NORMAL);
    if (b.ptr == nullptr || b.ptr == &mp::mgr()) return false;
    b.bytes = need;
    return true;
}

struct HostBuf {
    void* ptr = nullptr;
    size_t bytes = 0;
};

HostBuf g_pin_a;
HostBuf g_pin_c;
int g_mm_async = 0;  // one outstanding xcuda_matmul_f64_begin flight

bool host_get(HostBuf& b, size_t need) {
    if (b.bytes >= need) return true;
    if (b.ptr != nullptr) {
        mp::mgr().pinned_free(b.ptr);
        b = {};
    }
    b.ptr = mp::mgr().pinned_alloc(need);
    if (b.ptr == nullptr) return false;
    b.bytes = need;
    return true;
}

// One-shot events record the H2D → compute → D2H dependency chain.
xcd::CUevent_t g_ev_in = 0;
xcd::CUevent_t g_ev_out = 0;

bool ensure_events() {
    const xcd::Api& a = xcd::api();
    if (g_ev_in == 0 &&
        a.event_create(&g_ev_in, xcd::kEventDisableTiming) != xcd::kOk)
        return false;
    if (g_ev_out == 0 &&
        a.event_create(&g_ev_out, xcd::kEventDisableTiming) != xcd::kOk)
        return false;
    return true;
}

// Upload `host` once and return the resident device pointer, or nullptr on
// fail-closed (no device, OOM, copy failure).  The H2D lane owns the copy.
void* device_weight(const double* host, size_t bytes) {
    auto it = g_dev_weights.find(host);
    if (it != g_dev_weights.end()) return it->second.first;
    void* dev = mp::mgr().alloc(mp::Tier::PINNED_PERMANENT, (int64_t)bytes,
                                mp::StreamLane::PREFILL_NORMAL);
    if (dev == nullptr || dev == &mp::mgr()) return nullptr;
    const xcd::Api& a = xcd::api();
    const xcd::CUstream_t s = mp::mgr().stream(mp::StreamLane::H2D);
    const xcd::CUdevptr_t dp = static_cast<xcd::CUdevptr_t>(
        reinterpret_cast<uintptr_t>(dev));
    if (a.memcpy_htod_async(dp, host, bytes, s) != xcd::kOk ||
        a.stream_sync(s) != xcd::kOk) {
        mp::mgr().free(dev, mp::StreamLane::PREFILL_NORMAL);
        return nullptr;
    }
    g_dev_weights.emplace(host, std::make_pair(dev, bytes));
    return dev;
}

}  // namespace

extern "C" {

// ---------------------------------------------------------------------------
// 1 if the driver + unified memory plane + PTX GEMM are ready for fp64
//    offload (post-§66 admission), 0 otherwise.  Fail-closed.
// ---------------------------------------------------------------------------
int xcuda_available() {
    return (xcuda_dev_probe() != 0 && mp::mgr().ensure()) ? 1 : 0;
}

// bf16/fp8/kv availability: 1 only when the kernels TU is linked AND a
// CUDA device + the JIT'd module exist. Without the kernels TU these
// fail closed to 0, so a quantized request can never silently degrade.
#if defined(XINGCHENG_CUDA_KERNELS)
int xcuda_bf16_available() { return xcuda_bf16_kernel_probe(); }
int xcuda_fp8_available() { return xcuda_fp8_kernel_probe(); }
int xcuda_kv_available() { return xcuda_kv_kernel_probe(); }
#else
int xcuda_bf16_available() { return 0; }
int xcuda_fp8_available() { return 0; }
int xcuda_kv_available() { return 0; }
#endif

// ---------------------------------------------------------------------------
// fp64 GEMM  C(m×n) = A(m×k) * B(k×n)  row-major — in-tree PTX kernel.
// B is always a transposed weight — resolved through the device-resident
// cache. Returns 0 on success, nonzero on fail-closed.
// ---------------------------------------------------------------------------
int xcuda_matmul_f64(const double* a, long long m, long long k,
                     const double* b, long long n, double* out) {
    if (a == nullptr || b == nullptr || out == nullptr || m <= 0 ||
        k <= 0 || n <= 0)
        return 2;
    if (!xcuda_available()) return 3;

    const size_t a_bytes = (size_t)m * (size_t)k * sizeof(double);
    const size_t c_bytes = (size_t)m * (size_t)n * sizeof(double);
    const size_t b_bytes = (size_t)k * (size_t)n * sizeof(double);

    std::lock_guard<std::mutex> g(g_mu);

    void* db = device_weight(b, b_bytes);
    if (db == nullptr) return 3;

    if (!dev_get(g_dev_a, a_bytes) || !dev_get(g_dev_c, c_bytes))
        return 3;

    // §15/§16: H2D on the transfer lane -> event -> GEMM on the compute
    // lane -> event -> D2H. The only host wait is the final D2H
    // completion. Pinned staging when the ring serves it — pinning is a
    // cap, not a right (§14): a miss falls back to the pageable copy.
    void* ha = host_get(g_pin_a, a_bytes) ? g_pin_a.ptr : nullptr;
    void* hc = host_get(g_pin_c, c_bytes) ? g_pin_c.ptr : nullptr;

    const xcd::Api& api = xcd::api();
    const xcd::CUstream_t h2d = mp::mgr().stream(mp::StreamLane::H2D);
    const xcd::CUstream_t prefill =
        mp::mgr().stream(mp::StreamLane::PREFILL_NORMAL);
    const xcd::CUstream_t d2h = mp::mgr().stream(mp::StreamLane::D2H);
    const xcd::CUdevptr_t da = static_cast<xcd::CUdevptr_t>(
        reinterpret_cast<uintptr_t>(g_dev_a.ptr));
    const xcd::CUdevptr_t dcv = static_cast<xcd::CUdevptr_t>(
        reinterpret_cast<uintptr_t>(g_dev_c.ptr));

    if (!ensure_events()) return 3;
    const void* src = a;
    if (ha != nullptr) {
        std::memcpy(ha, a, a_bytes);
        src = ha;
    }
    if (api.memcpy_htod_async(da, src, a_bytes, h2d) != xcd::kOk ||
        api.event_record(g_ev_in, h2d) != xcd::kOk ||
        api.stream_wait_event(prefill, g_ev_in, 0) != xcd::kOk)
        return 3;
    if (xcuda_dev_gemm_f64(static_cast<unsigned long long>(da),
                           static_cast<unsigned long long>(
                               reinterpret_cast<uintptr_t>(db)),
                           static_cast<unsigned long long>(dcv),
                           m, k, n,
                           static_cast<unsigned long long>(prefill)) != 0)
        return 3;
    if (api.event_record(g_ev_out, prefill) != xcd::kOk ||
        api.stream_wait_event(d2h, g_ev_out, 0) != xcd::kOk)
        return 3;
    void* dst = out;
    if (hc != nullptr) dst = hc;
    if (api.memcpy_dtoh_async(dst, dcv, c_bytes, d2h) != xcd::kOk ||
        api.stream_sync(d2h) != xcd::kOk)
        return 3;
    if (hc != nullptr) std::memcpy(out, hc, c_bytes);

    mp::mgr().h2d_bytes += (int64_t)(a_bytes + b_bytes);
    mp::mgr().d2h_bytes += (int64_t)c_bytes;
    return 0;
}

// ---------------------------------------------------------------------------
// Async fp64 GEMM pair for the hybrid CPU+GPU lane: begin() enqueues the
// whole H2D -> GEMM -> D2H chain and returns before device completion so
// the host can run its own row block concurrently; wait() joins the D2H
// lane and copies the result.  At most ONE outstanding call — the engine
// forward is single-threaded, and a second begin while a flight is
// pending fails closed so the shared pinned/scratch slabs can never be
// clobbered mid-flight.  rc codes match xcuda_matmul_f64.
// ---------------------------------------------------------------------------
int xcuda_matmul_f64_begin(const double* a, long long m, long long k,
                           const double* b, long long n) {
    if (a == nullptr || b == nullptr || m <= 0 || k <= 0 || n <= 0)
        return 2;
    if (!xcuda_available()) return 3;

    const size_t a_bytes = (size_t)m * (size_t)k * sizeof(double);
    const size_t b_bytes = (size_t)k * (size_t)n * sizeof(double);
    const size_t c_bytes = (size_t)m * (size_t)n * sizeof(double);

    std::lock_guard<std::mutex> g(g_mu);
    if (g_mm_async) return 3;  // one flight at a time

    void* db = device_weight(b, b_bytes);
    if (db == nullptr) return 3;
    if (!dev_get(g_dev_a, a_bytes) || !dev_get(g_dev_c, c_bytes))
        return 3;
    if (!ensure_events()) return 3;
    void* ha = host_get(g_pin_a, a_bytes) ? g_pin_a.ptr : nullptr;
    if (!host_get(g_pin_c, c_bytes)) return 3;  // wait() needs pinned dst

    const xcd::Api& api = xcd::api();
    const xcd::CUstream_t h2d = mp::mgr().stream(mp::StreamLane::H2D);
    const xcd::CUstream_t prefill =
        mp::mgr().stream(mp::StreamLane::PREFILL_NORMAL);
    const xcd::CUstream_t d2h = mp::mgr().stream(mp::StreamLane::D2H);
    const xcd::CUdevptr_t da = static_cast<xcd::CUdevptr_t>(
        reinterpret_cast<uintptr_t>(g_dev_a.ptr));
    const xcd::CUdevptr_t dcv = static_cast<xcd::CUdevptr_t>(
        reinterpret_cast<uintptr_t>(g_dev_c.ptr));

    const void* src = a;
    if (ha != nullptr) {
        std::memcpy(ha, a, a_bytes);
        src = ha;
    }
    if (api.memcpy_htod_async(da, src, a_bytes, h2d) != xcd::kOk ||
        api.event_record(g_ev_in, h2d) != xcd::kOk ||
        api.stream_wait_event(prefill, g_ev_in, 0) != xcd::kOk)
        return 3;
    if (xcuda_dev_gemm_f64(static_cast<unsigned long long>(da),
                           static_cast<unsigned long long>(
                               reinterpret_cast<uintptr_t>(db)),
                           static_cast<unsigned long long>(dcv),
                           m, k, n,
                           static_cast<unsigned long long>(prefill)) != 0)
        return 3;
    if (api.event_record(g_ev_out, prefill) != xcd::kOk ||
        api.stream_wait_event(d2h, g_ev_out, 0) != xcd::kOk)
        return 3;
    // D2H is enqueued into the pinned slab now; wait() only needs to
    // join the lane — the copy itself overlaps the CPU row block.
    if (api.memcpy_dtoh_async(g_pin_c.ptr, dcv, c_bytes, d2h) != xcd::kOk)
        return 3;
    mp::mgr().h2d_bytes += (int64_t)(a_bytes + b_bytes);
    mp::mgr().d2h_bytes += (int64_t)c_bytes;
    g_mm_async = 1;
    return 0;
}

int xcuda_matmul_f64_wait(double* out, long long m, long long n) {
    if (out == nullptr || m <= 0 || n <= 0) return 2;
    if (!g_mm_async) return 3;
    const size_t c_bytes = (size_t)m * (size_t)n * sizeof(double);
    const xcd::Api& api = xcd::api();
    const xcd::CUstream_t d2h = mp::mgr().stream(mp::StreamLane::D2H);
    const int rc = api.stream_sync(d2h) == xcd::kOk ? 0 : 3;
    if (rc == 0 && g_pin_c.bytes >= c_bytes)
        std::memcpy(out, g_pin_c.ptr, c_bytes);
    g_mm_async = 0;
    return rc == 0 && g_pin_c.bytes >= c_bytes ? 0 : 3;
}

// ---------------------------------------------------------------------------
// Grouped fp64 GEMM: one H2D upload of the concatenated activation block
// and one D2H download of the concatenated output, with a per-group PTX
// GEMM over device-resident weights in between — identical math to
// xcuda_matmul_f64 per group minus the transfer/sync overhead (MoE
// expert dispatch hot path). a holds the groups' row-blocks concatenated
// ([sum(group_rows) x k]); b_list[g] is group g's [k x n] weight; out
// receives the concatenated [sum x n] rows in the same order.
// ---------------------------------------------------------------------------
int xcuda_matmul_f64_grouped(
    const double* a, const long long* group_rows, long long groups,
    const double* const* b_list, long long k, long long n,
    double* out) {
    if (a == nullptr || group_rows == nullptr || b_list == nullptr ||
        out == nullptr || groups <= 0 || k <= 0 || n <= 0)
        return 2;
    long long total = 0;
    for (long long gi = 0; gi < groups; ++gi) {
        if (group_rows[gi] < 0 || b_list[gi] == nullptr ||
            group_rows[gi] > LLONG_MAX - total)
            return 2;
        total += group_rows[gi];
    }
    if (total <= 0) return 0;
    if (!xcuda_available()) return 3;

    const size_t a_bytes = (size_t)total * (size_t)k * sizeof(double);
    const size_t c_bytes = (size_t)total * (size_t)n * sizeof(double);
    const size_t b_bytes = (size_t)k * (size_t)n * sizeof(double);

    std::lock_guard<std::mutex> g(g_mu);

    if (!dev_get(g_dev_a, a_bytes) || !dev_get(g_dev_c, c_bytes))
        return 3;
    void* ha = host_get(g_pin_a, a_bytes) ? g_pin_a.ptr : nullptr;
    void* hc = host_get(g_pin_c, c_bytes) ? g_pin_c.ptr : nullptr;

    const xcd::Api& api = xcd::api();
    const xcd::CUstream_t h2d = mp::mgr().stream(mp::StreamLane::H2D);
    const xcd::CUstream_t prefill =
        mp::mgr().stream(mp::StreamLane::PREFILL_NORMAL);
    const xcd::CUstream_t d2h = mp::mgr().stream(mp::StreamLane::D2H);
    const xcd::CUdevptr_t da = static_cast<xcd::CUdevptr_t>(
        reinterpret_cast<uintptr_t>(g_dev_a.ptr));
    const xcd::CUdevptr_t dcv = static_cast<xcd::CUdevptr_t>(
        reinterpret_cast<uintptr_t>(g_dev_c.ptr));

    const void* src = a;
    if (ha != nullptr) {
        std::memcpy(ha, a, a_bytes);
        src = ha;
    }
    if (!ensure_events() ||
        api.memcpy_htod_async(da, src, a_bytes, h2d) != xcd::kOk ||
        api.event_record(g_ev_in, h2d) != xcd::kOk ||
        api.stream_wait_event(prefill, g_ev_in, 0) != xcd::kOk)
        return 3;
    long long off = 0;
    for (long long gi = 0; gi < groups; ++gi) {
        const long long m_g = group_rows[gi];
        if (m_g == 0) continue;
        void* db = device_weight(b_list[gi], b_bytes);
        if (db == nullptr) return 3;
        if (xcuda_dev_gemm_f64(
                static_cast<unsigned long long>(
                    da + (xcd::CUdevptr_t)off * (xcd::CUdevptr_t)k *
                             sizeof(double)),
                static_cast<unsigned long long>(
                    reinterpret_cast<uintptr_t>(db)),
                static_cast<unsigned long long>(
                    dcv + (xcd::CUdevptr_t)off * (xcd::CUdevptr_t)n *
                              sizeof(double)),
                m_g, k, n,
                static_cast<unsigned long long>(prefill)) != 0)
            return 3;
        off += m_g;
    }
    if (api.event_record(g_ev_out, prefill) != xcd::kOk ||
        api.stream_wait_event(d2h, g_ev_out, 0) != xcd::kOk)
        return 3;
    void* dst = out;
    if (hc != nullptr) dst = hc;
    if (api.memcpy_dtoh_async(dst, dcv, c_bytes, d2h) != xcd::kOk ||
        api.stream_sync(d2h) != xcd::kOk)
        return 3;
    if (hc != nullptr) std::memcpy(out, hc, c_bytes);
    mp::mgr().h2d_bytes += (int64_t)a_bytes;
    mp::mgr().d2h_bytes += (int64_t)c_bytes;
    return 0;
}

// ---------------------------------------------------------------------------
// Free every cached device weight and scratch operand. Called by the
// engine on unload so a stale pointer can never alias a new tensor.
// Device bytes release through the memory plane (§3/§6 maintenance).
// ---------------------------------------------------------------------------
int xcuda_release_weights() {
    std::lock_guard<std::mutex> g(g_mu);
    if (g_mm_async) {  // drain an in-flight async GEMM before freeing slabs
        xcd::api().stream_sync(mp::mgr().stream(mp::StreamLane::D2H));
        g_mm_async = 0;
    }
    for (auto& kv : g_dev_weights) {
        if (kv.second.first != nullptr)
            mp::mgr().free(kv.second.first, mp::StreamLane::H2D);
    }
    g_dev_weights.clear();
    if (g_dev_a.ptr != nullptr)
        mp::mgr().free(g_dev_a.ptr, mp::StreamLane::H2D);
    if (g_dev_c.ptr != nullptr)
        mp::mgr().free(g_dev_c.ptr, mp::StreamLane::H2D);
    g_dev_a = {};
    g_dev_c = {};
    mp::mgr().pinned_release_all();
    g_pin_a = {};
    g_pin_c = {};
    const xcd::Api& api = xcd::api();
    if (g_ev_in != 0) {
        api.event_destroy(g_ev_in);
        g_ev_in = 0;
    }
    if (g_ev_out != 0) {
        api.event_destroy(g_ev_out);
        g_ev_out = 0;
    }
    xcuda_bf16_release_weights();
    xcuda_fp8_release_weights();
    xcuda_kv_free();
    return 0;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
