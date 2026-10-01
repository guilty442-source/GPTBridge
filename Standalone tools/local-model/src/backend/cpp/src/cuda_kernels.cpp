// Driver-API + embedded-PTX CUDA kernels for the Xingcheng C++ inference
// engine (B132 native GPU path, zero-install build).
//
// Device code is authored in-tree as PTX (cuda_ptx_*.h) — a 1:1 semantic
// port of the retired NVRTC C++ source — and JIT-compiled by the NVIDIA
// driver itself through cuModuleLoadData on the retained primary
// context. The only vendor dependency is nvcuda.dll (shipped inside the
// display driver); nvrtc, cuBLAS, cudart and every toolkit artefact are
// no longer referenced anywhere in the lane.
//
// Nothing is import-linked, so the binary stays portable: on a host
// without an NVIDIA driver every probe returns 0 and the governed
// request paths fail closed exactly as before.
//
// Governance: unchanged — the engine only reaches these entries after
// the governed layer sets XINGCHENG_CPP_CUDA{_BF16,_FP8,_KV}; capability
// lives here, the decision stays above, and failures never silently
// fall back.

#ifdef XINGCHENG_CUDA

#include <windows.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

// §3 single allocator: this TU's device bytes — bf16/fp8 weight
// copies, per-call scratch pools and the device KV cache — all flow
// through the same UnifiedCudaMemoryManager as the fp64 bridge. The
// manager and this TU share the retained primary context in
// cuda_drvapi.h, so no second allocator or context is ever created.
#include "cuda_memplane.h"
namespace mp = xcm_memplane;

// Embedded PTX bodies — self-authored device code (star-cuda-language).
#include "cuda_ptx_conv.h"
#include "cuda_ptx_fp8.h"
#include "cuda_ptx_fp8w.h"
#include "cuda_ptx_gemm.h"
#include "cuda_ptx_gemmw.h"
#include "cuda_ptx_gemv.h"
#include "cuda_ptx_kv.h"
#include "cuda_ptx_math.h"
#include "cuda_ptx_train.h"

namespace {

using xcd::CUresult_t;
using xcd::CUdevptr_t;
using xcd::CUmodule_t;
using xcd::CUfunction_t;
using xcd::CUstream_t;
using xcd::CUgraph_t;
using xcd::CUgraphExec_t;

constexpr int kCudaSuccess = xcd::kOk;

xcd::Api& g_drv = xcd::api();

bool device_ready() { return xcd::device_ready(); }
bool use_ctx() { return xcd::use_ctx(); }

// ------------------------------------------------------ kernel sources --

// PTX ISA 7.1 introduces the sm_86 target — the lane's capability floor
// (§62) — so the emitted module is valid on every admissible device and
// JITs upward (sm_89/90/120) through driver forward compatibility.
const char* kPtxHeader =
    "// Xingcheng native CUDA lane — self-authored PTX (B132)\n"
    "// JIT-compiled by the driver via cuModuleLoadData; no toolkit.\n"
    ".version 7.1\n"
    ".target sm_86\n"
    ".address_size 64\n";

// Helpers must precede kernels that call them in the module text.
std::string ptx_image() {
    std::string s;
    s.reserve(96 * 1024);
    s += kPtxHeader;
    s += xcuda_ptx::math();
    s += xcuda_ptx::conv();
    s += xcuda_ptx::gemm();
    s += xcuda_ptx::gemmw();
    s += xcuda_ptx::gemv();
    s += xcuda_ptx::fp8();
    s += xcuda_ptx::fp8w();
    s += xcuda_ptx::kv();
    s += xcuda_ptx::train();
    return s;
}


// ------------------------------------------------- module load/cache ---

CUmodule_t g_module = 0;
CUfunction_t g_f_conv_bf16 = 0;
CUfunction_t g_f_gemm_f64 = 0;
CUfunction_t g_f_gemm_f64_w = 0;
CUfunction_t g_f_gemm_bf16 = 0;
CUfunction_t g_f_gemm_bf16_w = 0;
CUfunction_t g_f_gemv_bf16 = 0;
CUfunction_t g_f_gemv_bf16_m1 = 0;
CUfunction_t g_f_gemv_reduce = 0;
CUfunction_t g_f_conv_fp32 = 0;
CUfunction_t g_f_conv_fp8 = 0;
CUfunction_t g_f_gemm_fp8 = 0;
CUfunction_t g_f_gemm_fp8_w = 0;
CUfunction_t g_f_gemv_fp8 = 0;
CUfunction_t g_f_gemv_fp8_m1 = 0;
CUfunction_t g_f_kv_attn = 0;
CUfunction_t g_f_adamw = 0;
CUfunction_t g_f_sqsum = 0;
std::mutex g_module_mu;
bool g_module_tried = false;

bool get_func(CUfunction_t* out, const char* name) {
    return g_drv.module_get_function(out, g_module, name) == kCudaSuccess &&
           *out != 0;
}

// JIT the embedded PTX once on the retained primary context; any failure
// latches the kernels TU as unavailable (fail-closed, no retries).
bool ensure_module() {
    if (g_module_tried) return g_module != 0;
    std::lock_guard<std::mutex> lk(g_module_mu);
    if (g_module_tried) return g_module != 0;
    g_module_tried = true;
    if (!use_ctx()) return false;
    const std::string ptx = ptx_image();
    CUresult_t rc;
    if (g_drv.module_load_data_ex != nullptr) {
        // CU_JIT_ERROR_LOG_BUFFER(_SIZE_BYTES) — capture the driver's JIT
        // diagnostics so a reject names the failing PTX line (audit
        // evidence for the fail-closed path).
        char jit_err[4096];
        jit_err[0] = '\0';
        size_t jit_err_len = sizeof(jit_err);
        int opts[] = {5 /*ERROR_LOG_BUFFER*/, 6 /*ERROR_LOG_BUFFER_SIZE*/};
        void* vals[] = {jit_err, &jit_err_len};
        rc = g_drv.module_load_data_ex(&g_module, ptx.c_str(), 2,
                                       opts, vals);
        if (rc != kCudaSuccess && jit_err[0] != '\0')
            std::fprintf(stderr, "xcuda: PTX JIT failed rc=%d\n%s\n",
                         (int)rc, jit_err);
        else if (rc != kCudaSuccess)
            std::fprintf(stderr, "xcuda: PTX module load failed rc=%d\n",
                         (int)rc);
    } else {
        rc = g_drv.module_load_data(&g_module, ptx.c_str());
    }
    if (rc != kCudaSuccess) return false;
    bool ok = true;
    ok &= get_func(&g_f_conv_bf16, "xc_f64_to_bf16");
    ok &= get_func(&g_f_gemm_f64, "xc_gemm_f64");
    ok &= get_func(&g_f_gemm_f64_w, "xc_gemm_f64_w");
    ok &= get_func(&g_f_gemm_bf16, "xc_gemm_bf16");
    ok &= get_func(&g_f_gemm_bf16_w, "xc_gemm_bf16_w");
    ok &= get_func(&g_f_gemv_bf16, "xc_gemv_bf16_part");
    ok &= get_func(&g_f_gemv_bf16_m1, "xc_gemv_bf16_m1");
    ok &= get_func(&g_f_gemv_reduce, "xc_gemv_reduce");
    ok &= get_func(&g_f_conv_fp32, "xc_f64_to_fp32");
    ok &= get_func(&g_f_conv_fp8, "xc_f64_to_fp8");
    ok &= get_func(&g_f_gemm_fp8, "xc_gemm_fp8");
    ok &= get_func(&g_f_gemm_fp8_w, "xc_gemm_fp8_w");
    ok &= get_func(&g_f_gemv_fp8, "xc_gemv_fp8_part");
    ok &= get_func(&g_f_gemv_fp8_m1, "xc_gemv_fp8_m1");
    ok &= get_func(&g_f_kv_attn, "xc_kv_attention");
    ok &= get_func(&g_f_adamw, "xc_adamw_fused");
    ok &= get_func(&g_f_sqsum, "xc_sqsum_part");
    if (!ok) {
        g_drv.module_unload(g_module);
        g_module = 0;
        return false;
    }
    return true;
}

// Launch helper: kernel params are passed by address. launch_s takes an
// explicit stream — required by graph capture, whose nodes must be
// recorded on a real stream (the legacy stream is not capturable).
bool launch_s(CUfunction_t f, unsigned int gx, unsigned int gy,
              unsigned int bx, unsigned int by, unsigned int shmem,
              void** params, CUstream_t stream) {
    return g_drv.launch_kernel(f, gx, gy, 1, bx, by, 1, shmem, stream,
                               params, nullptr) == kCudaSuccess;
}

bool launch(CUfunction_t f, unsigned int gx, unsigned int gy,
            unsigned int bx, unsigned int by, unsigned int shmem,
            void** params) {
    return launch_s(f, gx, gy, bx, by, shmem, params, 0);
}

// -------------------------------------------------- bf16 / fp8 domains --

constexpr long long kConvThreads = 256;
// Skinny-m threshold mirroring XC_GEMV_MAX_M in the device source.
constexpr long long kGemvMaxM = 16;
constexpr long long kGemvThreads = 256;
// Legacy fixed split-k, kept as the floor of the dynamic range.
constexpr long long kGemvKSplit = 8;

// ------------------------------------------------- adaptive dispatch --
// Deterministic given (m,k,n,device): the 64x64 wide tiles win when the
// grid still covers the SMs; m==1 decode gets the fused single-row
// kernel; split-k scales the launch to ~4 waves of the live SM count
// instead of a fixed 8 slices. The reduce stays in fixed slice order,
// so results remain bit-stable for a given shape on a given device.
constexpr long long kWideMin = 32;

bool wide_gemm_shape(long long m, long long k, long long n) {
    return m >= kWideMin && n >= kWideMin && k >= 16;
}

int pick_ksplit(long long k, long long n) {
    const long long sm =
        xcd::dev().sm_count > 0 ? xcd::dev().sm_count : 1;
    const long long col_blocks = (n + kGemvThreads - 1) / kGemvThreads;
    long long ks = kGemvKSplit;
    while (ks < 32 && col_blocks * ks < sm * 4) ks <<= 1;
    const long long cap = (k + 63) / 64;    // keep >=64 k per slice
    if (ks > cap) ks = cap;
    if (ks < kGemvKSplit) ks = kGemvKSplit;
    return static_cast<int>(ks);
}

std::mutex g_bf16_mu;
std::unordered_map<const void*, CUdevptr_t> g_bf16_weights;
std::mutex g_fp8_mu;
std::unordered_map<const void*, CUdevptr_t> g_fp8_weights;

// §3: every device byte comes from the unified manager — its budget,
// ladder and telemetry all apply to the kernel lanes too. A pool miss
// is a typed failure (0), never a driver-side fallback allocator.
// Pool blocks are made visible to stream 0 by the manager's alloc-time
// lane drain, so kernel launches need no extra dependency.
CUdevptr_t dev_alloc(size_t bytes, mp::Tier tier) {
    if (!mp::mgr().ensure()) return 0;
    void* p = mp::mgr().alloc(tier, static_cast<int64_t>(bytes),
                              mp::StreamLane::PREFILL_NORMAL);
    return (p != nullptr && p != &mp::mgr()) ?
               static_cast<CUdevptr_t>(
                   reinterpret_cast<uintptr_t>(p)) :
               0;
}

void dev_free(CUdevptr_t p) {
    if (p == 0) return;
    // Kernels ride driver stream 0; drain the context so a pooled
    // block is never recycled under an in-flight launch. Frees are
    // lifecycle/growth events — never per-token (§4).
    if (g_drv.ctx_sync) g_drv.ctx_sync();
    mp::mgr().free(reinterpret_cast<void*>(
                       static_cast<uintptr_t>(p)),
                   mp::StreamLane::PREFILL_NORMAL);
}

// Transfer wrappers so §53 copy accounting covers the kernel lanes.
CUresult_t xmemcpy_htod(CUdevptr_t dst, const void* src, size_t n) {
    CUresult_t rc = g_drv.memcpy_htod(dst, src, n);
    if (rc == kCudaSuccess) mp::mgr().h2d_bytes += (int64_t)n;
    return rc;
}

CUresult_t xmemcpy_dtoh(void* dst, CUdevptr_t src, size_t n) {
    CUresult_t rc = g_drv.memcpy_dtoh(dst, src, n);
    if (rc == kCudaSuccess) mp::mgr().d2h_bytes += (int64_t)n;
    return rc;
}

// Quantize host f64 → fresh device bf16 buffer (caller frees).
CUdevptr_t upload_bf16(const double* host, long long elems) {
    const size_t f64b = static_cast<size_t>(elems) * sizeof(double);
    const size_t bfb = static_cast<size_t>(elems) * 2;
    CUdevptr_t staging = dev_alloc(f64b, mp::Tier::KERNEL_SCRATCH);
    if (staging == 0) return 0;
    CUdevptr_t dev = dev_alloc(bfb, mp::Tier::PINNED_PERMANENT);
    if (dev == 0) {
        dev_free(staging);
        return 0;
    }
    if (xmemcpy_htod(staging, host, f64b) != kCudaSuccess) {
        dev_free(staging);
        dev_free(dev);
        return 0;
    }
    long long n = elems;
    void* params[] = {&staging, &dev, &n};
    if (!launch(g_f_conv_bf16,
                static_cast<unsigned int>((elems + kConvThreads - 1) /
                                          kConvThreads),
                1, static_cast<unsigned int>(kConvThreads), 1, 0,
                params) ||
        g_drv.ctx_sync() != kCudaSuccess) {
        dev_free(staging);
        dev_free(dev);
        return 0;
    }
    dev_free(staging);
    return dev;
}

CUdevptr_t device_weight_bf16(const double* host, long long elems) {
    auto it = g_bf16_weights.find(host);
    if (it != g_bf16_weights.end()) return it->second;
    CUdevptr_t dev = upload_bf16(host, elems);
    if (dev == 0) return 0;
    g_bf16_weights.emplace(host, dev);
    return dev;
}

// Pooled per-call buffers for the bf16/fp8 lanes (grow-on-demand):
// cuMemAlloc/cuMemFree/malloc per GEMM put driver syncs and heap churn on
// the decode hot path. Released by the matching *_release_weights.
struct DevPool {
    CUdevptr_t p = 0;
    size_t cap = 0;
};

CUdevptr_t dev_get_pooled(DevPool& pool, size_t bytes) {
    if (pool.cap >= bytes) return pool.p;
    CUdevptr_t next = dev_alloc(bytes, mp::Tier::KERNEL_SCRATCH);
    if (next == 0) return 0;
    if (pool.p != 0) dev_free(pool.p);
    pool.p = next;
    pool.cap = bytes;
    return pool.p;
}

void dev_pool_release(DevPool& pool) {
    if (pool.p != 0) dev_free(pool.p);
    pool = DevPool{};
}

DevPool g_bf16_a_stage, g_bf16_da, g_bf16_dc, g_bf16_part;
std::vector<float> g_bf16_c_host;
DevPool g_fp8_a_stage, g_fp8_da, g_fp8_dc, g_fp8_part;
std::vector<float> g_fp8_c_host;

// ------------------------------------------------- decode graph plane --
// §37 DecodeCudaGraph: capture one fixed-shape bf16 GEMM unit (H2D
// activation -> f64->bf16 convert -> gemv/gemm(+reduce) -> D2H result)
// per (m,k,n,db) and replay it. Host buffers are pinned staging at fixed
// addresses so the captured copy nodes re-read the same location on
// every replay; device buffers are entry-owned (never DevPool-grown,
// baked pointers must stay valid). Everything records on the manager's
// DECODE_HIGH lane — a real non-blocking stream; the legacy stream is
// not capturable. Fail-closed: missing API, allocation miss or a
// capture error drops the call back to the synchronous path.

CUstream_t decode_stream() {
    return mp::mgr().stream(mp::StreamLane::DECODE_HIGH);
}

bool graph_api_ready() {
    return g_drv.stream_sync != nullptr &&
           g_drv.memcpy_htod_async != nullptr &&
           g_drv.memcpy_dtoh_async != nullptr &&
           g_drv.stream_begin_capture != nullptr &&
           g_drv.stream_end_capture != nullptr &&
           g_drv.graph_instantiate != nullptr &&
           g_drv.graph_launch != nullptr &&
           g_drv.graph_exec_destroy != nullptr &&
           g_drv.graph_destroy != nullptr;
}

// -1 unread / 0 off / 1 on. Env-gated (XINGCHENG_CPP_CUDA_GRAPH) plus a
// governed extern switch for the parity probe; never a silent default.
int g_graph_flag = -1;
bool graph_wanted() {
    if (g_graph_flag < 0)
        g_graph_flag =
            std::getenv("XINGCHENG_CPP_CUDA_GRAPH") != nullptr ? 1 : 0;
    return g_graph_flag != 0;
}

struct Bf16Graph {
    CUdevptr_t db = 0;
    long long m = 0, k = 0, n = 0;
    CUdevptr_t a_stage = 0, da = 0, dc = 0, part = 0;
    double* a_host = nullptr;
    float* c_host = nullptr;
    CUgraphExec_t exec = 0;
};

std::vector<Bf16Graph> g_bf16_graphs;
constexpr size_t kMaxBf16Graphs = 256;

void graph_entry_release(Bf16Graph& g) {
    if (g.exec != 0) g_drv.graph_exec_destroy(g.exec);
    g.exec = 0;
    if (g.a_stage != 0) dev_free(g.a_stage);
    if (g.da != 0) dev_free(g.da);
    if (g.dc != 0) dev_free(g.dc);
    if (g.part != 0) dev_free(g.part);
    // Pinned host buffers stay manager-owned until pinned_release_all —
    // they are lifecycle, not per-request, resources.
    g.a_stage = g.da = g.dc = g.part = 0;
    g.a_host = nullptr;
    g.c_host = nullptr;
}

// Returns 0 replayed, -1 fall back to the synchronous path, 3 failure.
int run_bf16_graph(const double* a, long long m, long long k,
                   CUdevptr_t db, long long n, double* out) {
    if (!graph_api_ready() || !mp::mgr().ensure()) return -1;
    const long long a_elems = m * k;
    const long long c_elems = m * n;
    const bool skinny = m <= kGemvMaxM;

    Bf16Graph* e = nullptr;
    for (auto& g : g_bf16_graphs) {
        if (g.db == db && g.m == m && g.k == k && g.n == n) {
            e = &g;
            break;
        }
    }

    const size_t ab = static_cast<size_t>(a_elems) * sizeof(double);
    const size_t cb = static_cast<size_t>(c_elems) * sizeof(float);
    if (e == nullptr) {
        if (g_bf16_graphs.size() >= kMaxBf16Graphs) return -1;
        Bf16Graph g;
        g.db = db; g.m = m; g.k = k; g.n = n;
        g.a_stage = dev_alloc(ab, mp::Tier::KERNEL_SCRATCH);
        g.da = dev_alloc(static_cast<size_t>(a_elems) * 2,
                         mp::Tier::KERNEL_SCRATCH);
        g.dc = dev_alloc(cb, mp::Tier::KERNEL_SCRATCH);
        if (skinny)
            g.part = dev_alloc(static_cast<size_t>(
                                   pick_ksplit(k, n) * m * n) *
                                   sizeof(float),
                               mp::Tier::KERNEL_SCRATCH);
        g.a_host = static_cast<double*>(
            mp::mgr().pinned_alloc(static_cast<int64_t>(ab)));
        g.c_host = static_cast<float*>(
            mp::mgr().pinned_alloc(static_cast<int64_t>(cb)));
        bool ok = g.a_stage != 0 && g.da != 0 && g.dc != 0 &&
                  g.a_host != nullptr && g.c_host != nullptr &&
                  (!skinny || g.part != 0);
        CUgraph_t graph = 0;
        const CUstream_t s = decode_stream();
        if (ok && g_drv.stream_begin_capture(s, 0) != kCudaSuccess)
            ok = false;
        if (ok) {
            ok = g_drv.memcpy_htod_async(g.a_stage, g.a_host, ab, s) ==
                 kCudaSuccess;
            {
                long long ne = a_elems;
                void* cp[] = {&g.a_stage, &g.da, &ne};
                ok &= launch_s(
                    g_f_conv_bf16,
                    static_cast<unsigned int>(
                        (a_elems + kConvThreads - 1) / kConvThreads),
                    1, static_cast<unsigned int>(kConvThreads), 1, 0, cp,
                    s);
            }
            int mi = static_cast<int>(m), ki = static_cast<int>(k),
                ni = static_cast<int>(n);
            if (skinny) {
                int ksi = pick_ksplit(k, n);
                int kci = static_cast<int>((k + ksi - 1) / ksi);
                void* pp[] = {&g.da, &g.db, &g.part, &mi, &ki, &ni,
                              &ksi, &kci};
                ok &= launch_s(
                    m == 1 ? g_f_gemv_bf16_m1 : g_f_gemv_bf16,
                    static_cast<unsigned int>(
                        (n + kGemvThreads - 1) / kGemvThreads),
                    static_cast<unsigned int>(ksi),
                    static_cast<unsigned int>(kGemvThreads), 1, 0, pp,
                    s);
                void* rp[] = {&g.part, &g.dc, &mi, &ni, &ksi};
                ok &= launch_s(
                    g_f_gemv_reduce,
                    static_cast<unsigned int>(
                        (n + kGemvThreads - 1) / kGemvThreads),
                    1, static_cast<unsigned int>(kGemvThreads), 1, 0, rp,
                    s);
            } else {
                const bool wide = wide_gemm_shape(m, k, n);
                const long long ts = wide ? 64 : 16;
                void* pp[] = {&g.da, &g.db, &g.dc, &mi, &ki, &ni};
                ok &= launch_s(
                    wide ? g_f_gemm_bf16_w : g_f_gemm_bf16,
                    static_cast<unsigned int>((n + ts - 1) / ts),
                    static_cast<unsigned int>((m + ts - 1) / ts), 16, 16,
                    0, pp, s);
            }
            ok &= g_drv.memcpy_dtoh_async(g.c_host, g.dc, cb, s) ==
                  kCudaSuccess;
            ok &= g_drv.stream_end_capture(s, &graph) == kCudaSuccess &&
                  graph != 0;
        }
        if (ok) {
            ok = g_drv.graph_instantiate(&g.exec, graph,
                                         0ull) == kCudaSuccess &&
                 g.exec != 0;
            g_drv.graph_destroy(graph);
        }
        if (!ok) {
            graph_entry_release(g);
            return -1;
        }
        g_bf16_graphs.push_back(g);
        e = &g_bf16_graphs.back();
    }

    // Replay: stage A into the pinned buffer the captured H2D node
    // reads, launch the whole unit as one graph, sync the lane.
    std::memcpy(e->a_host, a, ab);
    const CUstream_t s = decode_stream();
    if (g_drv.graph_launch(e->exec, s) != kCudaSuccess) return 3;
    if (g_drv.stream_sync(s) != kCudaSuccess) return 3;
    for (long long i = 0; i < c_elems; ++i)
        out[i] = static_cast<double>(e->c_host[static_cast<size_t>(i)]);
    mp::mgr().h2d_bytes += static_cast<int64_t>(ab);
    mp::mgr().d2h_bytes += static_cast<int64_t>(cb);
    return 0;
}

// Shared GEMM body: a (host f64) x db (device bf16) → out (host f64).
int run_bf16(const double* a, long long m, long long k, CUdevptr_t db,
             long long n, double* out) {
    if (graph_wanted()) {
        const int gr = run_bf16_graph(a, m, k, db, n, out);
        if (gr >= 0) return gr;
    }
    const long long a_elems = m * k;
    const long long c_elems = m * n;
    int rc = 3;

    CUdevptr_t a_stage =
        dev_get_pooled(g_bf16_a_stage,
                       static_cast<size_t>(a_elems) * sizeof(double));
    CUdevptr_t da =
        dev_get_pooled(g_bf16_da, static_cast<size_t>(a_elems) * 2);
    CUdevptr_t dc =
        dev_get_pooled(g_bf16_dc, static_cast<size_t>(c_elems) * sizeof(float));
    if (a_stage == 0 || da == 0 || dc == 0) goto done;
    g_bf16_c_host.resize(static_cast<size_t>(c_elems));
    if (xmemcpy_htod(a_stage, a,
                          static_cast<size_t>(a_elems) * sizeof(double)) !=
        kCudaSuccess) {
        goto done;
    }
    {
        long long ne = a_elems;
        void* params[] = {&a_stage, &da, &ne};
        if (!launch(g_f_conv_bf16,
                    static_cast<unsigned int>((a_elems + kConvThreads - 1) /
                                              kConvThreads),
                    1, static_cast<unsigned int>(kConvThreads), 1, 0,
                    params)) {
            goto done;
        }
    }
    {
        int mi = static_cast<int>(m), ki = static_cast<int>(k),
            ni = static_cast<int>(n);
        if (m <= kGemvMaxM) {
            // Decode/skinny-m: split-k GEMV — part partials then a
            // fixed-order reduce; bandwidth-bound and deterministic.
            // ksplit adapts to the device SM count (pick_ksplit).
            int ksi = pick_ksplit(k, n);
            CUdevptr_t part = dev_get_pooled(
                g_bf16_part,
                static_cast<size_t>(ksi) * m * n * sizeof(float));
            if (part == 0) goto done;
            int kci = static_cast<int>((k + ksi - 1) / ksi);
            void* pparams[] = {&da, &db, &part, &mi, &ki, &ni, &ksi, &kci};
            if (!launch(m == 1 ? g_f_gemv_bf16_m1 : g_f_gemv_bf16,
                        static_cast<unsigned int>(
                            (n + kGemvThreads - 1) / kGemvThreads),
                        static_cast<unsigned int>(ksi),
                        static_cast<unsigned int>(kGemvThreads), 1, 0,
                        pparams)) {
                goto done;
            }
            void* rparams[] = {&part, &dc, &mi, &ni, &ksi};
            if (!launch(g_f_gemv_reduce,
                        static_cast<unsigned int>(
                            (n + kGemvThreads - 1) / kGemvThreads),
                        1, static_cast<unsigned int>(kGemvThreads), 1, 0,
                        rparams)) {
                goto done;
            }
        } else {
            const bool wide = wide_gemm_shape(m, k, n);
            const long long ts = wide ? 64 : 16;
            void* params[] = {&da, &db, &dc, &mi, &ki, &ni};
            if (!launch(wide ? g_f_gemm_bf16_w : g_f_gemm_bf16,
                        static_cast<unsigned int>((n + ts - 1) / ts),
                        static_cast<unsigned int>((m + ts - 1) / ts),
                        16, 16, 0, params)) {
                goto done;
            }
        }
    }
    // The synchronous D2H is stream-ordered after the queued kernels, so
    // it drains the pipeline and surfaces kernel errors on its own — the
    // extra device-wide cuCtxSynchronize on the hot path is redundant.
    if (xmemcpy_dtoh(g_bf16_c_host.data(), dc,
                          static_cast<size_t>(c_elems) * sizeof(float)) !=
        kCudaSuccess) {
        goto done;
    }
    for (long long i = 0; i < c_elems; ++i)
        out[i] = static_cast<double>(g_bf16_c_host[static_cast<size_t>(i)]);
    rc = 0;
done:
    return rc;
}

// Quantize host f64 → fresh device fp8(e4m3) buffer (caller frees).
CUdevptr_t upload_fp8(const double* host, long long elems) {
    const size_t f64b = static_cast<size_t>(elems) * sizeof(double);
    const size_t f8b = static_cast<size_t>(elems);
    CUdevptr_t staging = dev_alloc(f64b, mp::Tier::KERNEL_SCRATCH);
    if (staging == 0) return 0;
    CUdevptr_t dev = dev_alloc(f8b, mp::Tier::PINNED_PERMANENT);
    if (dev == 0) {
        dev_free(staging);
        return 0;
    }
    if (xmemcpy_htod(staging, host, f64b) != kCudaSuccess) {
        dev_free(staging);
        dev_free(dev);
        return 0;
    }
    long long n = elems;
    void* params[] = {&staging, &dev, &n};
    if (!launch(g_f_conv_fp8,
                static_cast<unsigned int>((elems + kConvThreads - 1) /
                                          kConvThreads),
                1, static_cast<unsigned int>(kConvThreads), 1, 0,
                params) ||
        g_drv.ctx_sync() != kCudaSuccess) {
        dev_free(staging);
        dev_free(dev);
        return 0;
    }
    dev_free(staging);
    return dev;
}

CUdevptr_t device_weight_fp8(const double* host, long long elems) {
    auto it = g_fp8_weights.find(host);
    if (it != g_fp8_weights.end()) return it->second;
    CUdevptr_t dev = upload_fp8(host, elems);
    if (dev == 0) return 0;
    g_fp8_weights.emplace(host, dev);
    return dev;
}

// Shared GEMM body: a (host f64→device fp32) x db (device fp8) → out.
int run_fp8(const double* a, long long m, long long k, CUdevptr_t db,
            long long n, double* out) {
    const long long a_elems = m * k;
    const long long c_elems = m * n;
    int rc = 3;

    CUdevptr_t a_stage =
        dev_get_pooled(g_fp8_a_stage,
                       static_cast<size_t>(a_elems) * sizeof(double));
    CUdevptr_t da =
        dev_get_pooled(g_fp8_da, static_cast<size_t>(a_elems) * sizeof(float));
    CUdevptr_t dc =
        dev_get_pooled(g_fp8_dc, static_cast<size_t>(c_elems) * sizeof(float));
    if (a_stage == 0 || da == 0 || dc == 0) goto done;
    g_fp8_c_host.resize(static_cast<size_t>(c_elems));
    if (xmemcpy_htod(a_stage, a,
                          static_cast<size_t>(a_elems) * sizeof(double)) !=
        kCudaSuccess) {
        goto done;
    }
    {
        long long ne = a_elems;
        void* params[] = {&a_stage, &da, &ne};
        if (!launch(g_f_conv_fp32,
                    static_cast<unsigned int>((a_elems + kConvThreads - 1) /
                                              kConvThreads),
                    1, static_cast<unsigned int>(kConvThreads), 1, 0,
                    params)) {
            goto done;
        }
    }
    {
        int mi = static_cast<int>(m), ki = static_cast<int>(k),
            ni = static_cast<int>(n);
        if (m <= kGemvMaxM) {
            int ksi = pick_ksplit(k, n);
            CUdevptr_t part = dev_get_pooled(
                g_fp8_part,
                static_cast<size_t>(ksi) * m * n * sizeof(float));
            if (part == 0) goto done;
            int kci = static_cast<int>((k + ksi - 1) / ksi);
            void* pparams[] = {&da, &db, &part, &mi, &ki, &ni, &ksi, &kci};
            if (!launch(m == 1 ? g_f_gemv_fp8_m1 : g_f_gemv_fp8,
                        static_cast<unsigned int>(
                            (n + kGemvThreads - 1) / kGemvThreads),
                        static_cast<unsigned int>(ksi),
                        static_cast<unsigned int>(kGemvThreads), 1, 0,
                        pparams)) {
                goto done;
            }
            void* rparams[] = {&part, &dc, &mi, &ni, &ksi};
            if (!launch(g_f_gemv_reduce,
                        static_cast<unsigned int>(
                            (n + kGemvThreads - 1) / kGemvThreads),
                        1, static_cast<unsigned int>(kGemvThreads), 1, 0,
                        rparams)) {
                goto done;
            }
        } else {
            const bool wide = wide_gemm_shape(m, k, n);
            const long long ts = wide ? 64 : 16;
            void* params[] = {&da, &db, &dc, &mi, &ki, &ni};
            if (!launch(wide ? g_f_gemm_fp8_w : g_f_gemm_fp8,
                        static_cast<unsigned int>((n + ts - 1) / ts),
                        static_cast<unsigned int>((m + ts - 1) / ts),
                        16, 16, 0, params)) {
                goto done;
            }
        }
    }
    // Same reasoning as run_bf16: the synchronous D2H drains the stream.
    if (xmemcpy_dtoh(g_fp8_c_host.data(), dc,
                          static_cast<size_t>(c_elems) * sizeof(float)) !=
        kCudaSuccess) {
        goto done;
    }
    for (long long i = 0; i < c_elems; ++i)
        out[i] = static_cast<double>(g_fp8_c_host[static_cast<size_t>(i)]);
    rc = 0;
done:
    return rc;
}

// ------------------------------------------------------------- KV domain --

constexpr long long kKvTile = 128;
constexpr long long kKvThreads = 128;
constexpr long long kMaxHeadDim = 256;

std::mutex g_kv_mu;
CUdevptr_t g_k = 0;
CUdevptr_t g_v = 0;
CUdevptr_t g_qbuf = 0;
CUdevptr_t g_obuf = 0;
long long g_layers = 0;
long long g_kv_heads = 0;
long long g_head_dim = 0;
long long g_max_len = 0;
size_t g_qcap = 0;
size_t g_ocap = 0;

// KV lane pinned staging — host sources are caller-owned pageable
// memory, so async copies stage through persistent pinned buffers
// ordered on the DECODE_HIGH lane (the same stream graph replays
// use: FIFO ordering between KV writes and the attention kernel,
// no per-call context sync on the token path).
//
// Staging reuse hazard: an enqueued cuMemcpyHtoDAsync may still be
// reading its pinned source when the next call wants to overwrite
// it. Small writes rotate through a fixed slot ring — a wrap drains
// the lane once per kKvSlotN writes instead of once per write;
// large writes use the growable buffer with a sync before each
// reuse (writes this size are prefill-scale and rare).
constexpr int kKvSlotN = 8;
constexpr size_t kKvSlotBytes = 64 * 1024;
double* g_kv_wslot[kKvSlotN] = {};
long long g_kv_wseq = 0;
double* g_kv_wpin = nullptr;
double* g_kv_qpin = nullptr;
double* g_kv_opin = nullptr;
size_t g_kv_wcap = 0;
size_t g_kv_qcap = 0;
size_t g_kv_ocap = 0;
bool g_kv_wpend = false;

bool kv_pin_grow(double** pp, size_t* cap, size_t bytes) {
    if (*cap >= bytes) return true;
    if (*pp != nullptr) mp::mgr().pinned_free(*pp);
    *pp = nullptr;
    *cap = 0;
    void* p = mp::mgr().pinned_alloc(static_cast<int64_t>(bytes));
    if (p == nullptr) return false;
    *pp = static_cast<double*>(p);
    *cap = bytes;
    return true;
}

bool grow_scratch(CUdevptr_t* buf, size_t* cap, size_t need_elems) {
    if (*cap >= need_elems) return true;
    dev_free(*buf);
    *buf = 0;
    *cap = 0;
    *buf = dev_alloc(need_elems * sizeof(double),
                     mp::Tier::KERNEL_SCRATCH);
    if (*buf == 0) return false;
    *cap = need_elems;
    return true;
}

void kv_free_locked() {
    dev_free(g_k);
    dev_free(g_v);
    dev_free(g_qbuf);
    dev_free(g_obuf);
    g_k = g_v = g_qbuf = g_obuf = 0;
    g_qcap = g_ocap = 0;
    g_layers = g_kv_heads = g_head_dim = g_max_len = 0;
    if (g_kv_wpin != nullptr) mp::mgr().pinned_free(g_kv_wpin);
    if (g_kv_qpin != nullptr) mp::mgr().pinned_free(g_kv_qpin);
    if (g_kv_opin != nullptr) mp::mgr().pinned_free(g_kv_opin);
    for (double*& slot : g_kv_wslot) {
        if (slot != nullptr) mp::mgr().pinned_free(slot);
        slot = nullptr;
    }
    g_kv_wpin = g_kv_qpin = g_kv_opin = nullptr;
    g_kv_wcap = g_kv_qcap = g_kv_ocap = 0;
    g_kv_wseq = 0;
    g_kv_wpend = false;
}

// ------------------------------------------------- fused AdamW state --
// NativeCudaTrainingPlane §26/§9/§24: optimizer state lives on device,
// keyed by the caller's host weight pointer (stable for a run). One
// fused kernel pass per element replaces the trainer's 5 host sweeps;
// H2D ships only the gradient per step, D2H runs at checkpoint/eval
// boundaries — never per step.
struct AdamwState {
    CUdevptr_t dw = 0, dm = 0, dv = 0, dg = 0;
    long long n = 0;
};

std::mutex g_adamw_mu;
std::unordered_map<const void*, AdamwState> g_adamw;
DevPool g_adamw_norm_part;
std::vector<float> g_adamw_norm_host;

constexpr int kSqsumBlocks = 128;

}  // namespace

extern "C" {

// ------------------------------------------------------- probe/release --

int xcuda_bf16_kernel_probe() {
    return device_ready() && ensure_module() ? 1 : 0;
}

// §37 DecodeCudaGraph switch: governed opt-in — env
// XINGCHENG_CPP_CUDA_GRAPH for production admission, or this call for
// the certification probe. Same role as xengine_cuda_lane: a test hook,
// never a second admission path. state() reports live graph count.
void xcuda_graph_enable(int on) { g_graph_flag = on != 0 ? 1 : 0; }
int xcuda_graph_state() {
    std::lock_guard<std::mutex> lk(g_bf16_mu);
    return static_cast<int>(g_bf16_graphs.size());
}

int xcuda_fp8_kernel_probe() {
    return device_ready() && ensure_module() ? 1 : 0;
}

int xcuda_kv_kernel_probe() {
    return device_ready() && ensure_module() ? 1 : 0;
}

// fp64 lane admission probe for cuda_bridge.cpp — device + JIT'd
// module + the self-authored GEMM all present; fail-closed otherwise.
int xcuda_dev_probe() {
    return device_ready() && ensure_module() && g_f_gemm_f64 != 0 &&
                   g_f_gemm_f64_w != 0
               ? 1
               : 0;
}

// fp64 tiled GEMM over device-resident operands, launched on the
// caller's stream — the bridge keeps its H2D → kernel → D2H event
// chain on the PREFILL lane exactly as the retired cuBLAS call did.
// rc contract: 0 ok, 2 bad args, 3 device/module/launch failure.
int xcuda_dev_gemm_f64(unsigned long long da, unsigned long long db,
                       unsigned long long dc, long long m, long long k,
                       long long n, unsigned long long stream) {
    if (!use_ctx() || !ensure_module() || g_f_gemm_f64 == 0 ||
        g_f_gemm_f64_w == 0)
        return 3;
    if (da == 0 || db == 0 || dc == 0 || m <= 0 || k <= 0 || n <= 0)
        return 2;
    void* params[] = {&da, &db, &dc, &m, &k, &n};
    // Wide-tile when the 64x64 grid still covers the device; the 16x16
    // kernel remains for small/skinny shapes.
    const bool wide = wide_gemm_shape(m, k, n);
    const long long ts = wide ? 64 : 16;
    return launch_s(wide ? g_f_gemm_f64_w : g_f_gemm_f64,
                    static_cast<unsigned int>((n + ts - 1) / ts),
                    static_cast<unsigned int>((m + ts - 1) / ts),
                    16, 16, 0, params,
                    static_cast<CUstream_t>(stream))
               ? 0
               : 3;
}

// Lightweight capability probe for the governed admission check —
// resolves the device + free VRAM without compiling kernels, so it is
// cheap enough to run before every governed launch decision.
int xcuda_probe(long long* free_bytes, long long* total_bytes,
                int* cc_major, int* cc_minor) {
    if (!device_ready()) return 0;
    if (free_bytes != nullptr && total_bytes != nullptr) {
        size_t fb = 0, tb = 0;
        if (g_drv.ctx_set_current(xcd::dev().ctx) != kCudaSuccess ||
            g_drv.mem_get_info(&fb, &tb) != kCudaSuccess) {
            return 0;
        }
        *free_bytes = static_cast<long long>(fb);
        *total_bytes = static_cast<long long>(tb);
    }
    if (cc_major != nullptr) *cc_major = xcd::dev().cc_major;
    if (cc_minor != nullptr) *cc_minor = xcd::dev().cc_minor;
    return 1;
}

// SM count for the unified compute-plane report — same fail-closed
// probe rule: 0 when the device/driver is not ready.
int xcuda_sm_count() {
    if (!device_ready()) return 0;
    return xcd::dev().sm_count > 0 ? xcd::dev().sm_count : 0;
}

// NVML instantaneous sensors for the §66 hardware baseline — same
// run-time binding rule as every other CUDA dependency: nvml.dll ships
// with the driver, nothing is import-linked, and any missing symbol or
// device simply leaves that field unmeasured (callers emit null, never
// a fabricated value). Returns a bitmask: bit0 = utilization read,
// bit1 = power read (milliwatts).
int xcuda_gpu_stats(unsigned* gpu_util_pct, unsigned* power_mw) {
    struct NvmlLib {
        HMODULE dll;
        int (*init)();
        int (*shutdown)();
        int (*handle_by_index)(unsigned, void**);
        int (*utilization)(void*, void*);
        int (*power_usage)(void*, unsigned*);
        NvmlLib() : dll(nullptr), init(nullptr), shutdown(nullptr),
                    handle_by_index(nullptr), utilization(nullptr),
                    power_usage(nullptr) {
            dll = LoadLibraryA("nvml.dll");
            if (dll == nullptr) return;
            init = (int(*)())GetProcAddress(dll, "nvmlInit_v2");
            shutdown = (int(*)())GetProcAddress(dll, "nvmlShutdown");
            handle_by_index = (int(*)(unsigned, void**))GetProcAddress(
                dll, "nvmlDeviceGetHandleByIndex_v2");
            utilization = (int(*)(void*, void*))GetProcAddress(
                dll, "nvmlDeviceGetUtilizationRates");
            power_usage = (int(*)(void*, unsigned*))GetProcAddress(
                dll, "nvmlDeviceGetPowerUsage");
        }
    };
    static NvmlLib lib;
    if (lib.dll == nullptr || lib.init == nullptr ||
        lib.handle_by_index == nullptr) return 0;
    static bool nvml_up = lib.init() == 0;
    if (!nvml_up) return 0;
    void* dev = nullptr;
    if (lib.handle_by_index(0, &dev) != 0 || dev == nullptr) return 0;
    int mask = 0;
    if (lib.utilization != nullptr && gpu_util_pct != nullptr) {
        // nvmlUtilization_t { unsigned gpu; unsigned memory; }
        unsigned rates[2] = {0, 0};
        if (lib.utilization(dev, rates) == 0) {
            *gpu_util_pct = rates[0];
            mask |= 1;
        }
    }
    if (lib.power_usage != nullptr && power_mw != nullptr) {
        unsigned mw = 0;
        if (lib.power_usage(dev, &mw) == 0) {
            *power_mw = mw;
            mask |= 2;
        }
    }
    return mask;
}

int xcuda_bf16_release_weights() {
    std::lock_guard<std::mutex> lk(g_bf16_mu);
    for (auto& g : g_bf16_graphs) graph_entry_release(g);
    g_bf16_graphs.clear();
    for (auto& kv : g_bf16_weights) dev_free(kv.second);
    g_bf16_weights.clear();
    dev_pool_release(g_bf16_a_stage);
    dev_pool_release(g_bf16_da);
    dev_pool_release(g_bf16_dc);
    dev_pool_release(g_bf16_part);
    g_bf16_c_host.clear();
    g_bf16_c_host.shrink_to_fit();
    return 0;
}

int xcuda_fp8_release_weights() {
    std::lock_guard<std::mutex> lk(g_fp8_mu);
    for (auto& kv : g_fp8_weights) dev_free(kv.second);
    g_fp8_weights.clear();
    dev_pool_release(g_fp8_a_stage);
    dev_pool_release(g_fp8_da);
    dev_pool_release(g_fp8_dc);
    dev_pool_release(g_fp8_part);
    g_fp8_c_host.clear();
    g_fp8_c_host.shrink_to_fit();
    return 0;
}

// ------------------------------------------------------------ bf16 GEMM --

int xcuda_matmul_bf16(const double* a, long long m, long long k,
                      const double* b, long long n, double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;
    std::lock_guard<std::mutex> lk(g_bf16_mu);
    if (!use_ctx() || !ensure_module()) return 3;
    const CUdevptr_t db = device_weight_bf16(b, k * n);
    if (db == 0) return 3;
    return run_bf16(a, m, k, db, n, out);
}

// Probe entry: no pointer-keyed cache — the caller's buffer is transient
// and a reused address must never alias a different weight's device copy.
int xcuda_matmul_bf16_uncached(const double* a, long long m, long long k,
                               const double* b, long long n, double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;
    std::lock_guard<std::mutex> lk(g_bf16_mu);
    if (!use_ctx() || !ensure_module()) return 3;
    const CUdevptr_t db = upload_bf16(b, k * n);
    if (db == 0) return 3;
    const int rc = run_bf16(a, m, k, db, n, out);
    dev_free(db);
    return rc;
}

// ------------------------------------------------------------- fp8 GEMM --

int xcuda_matmul_fp8(const double* a, long long m, long long k,
                     const double* b, long long n, double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;
    std::lock_guard<std::mutex> lk(g_fp8_mu);
    if (!use_ctx() || !ensure_module()) return 3;
    const CUdevptr_t db = device_weight_fp8(b, k * n);
    if (db == 0) return 3;
    return run_fp8(a, m, k, db, n, out);
}

int xcuda_matmul_fp8_uncached(const double* a, long long m, long long k,
                              const double* b, long long n, double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;
    std::lock_guard<std::mutex> lk(g_fp8_mu);
    if (!use_ctx() || !ensure_module()) return 3;
    const CUdevptr_t db = upload_fp8(b, k * n);
    if (db == 0) return 3;
    const int rc = run_fp8(a, m, k, db, n, out);
    dev_free(db);
    return rc;
}

// ------------------------------------------------------------ device KV --

int xcuda_kv_alloc(long long layers, long long kv_heads, long long head_dim,
                   long long max_len) {
    if (layers <= 0 || kv_heads <= 0 || head_dim <= 0 || max_len <= 0 ||
        head_dim > kMaxHeadDim) {
        return 1;
    }
    std::lock_guard<std::mutex> lk(g_kv_mu);
    if (!use_ctx() || !ensure_module()) return 2;
    kv_free_locked();
    const size_t elems = static_cast<size_t>(layers) *
                       static_cast<size_t>(kv_heads) *
                       static_cast<size_t>(max_len) *
                       static_cast<size_t>(head_dim);
    g_k = dev_alloc(elems * sizeof(double),
                    mp::Tier::SESSION_PERSISTENT);
    if (g_k == 0) return 2;
    g_v = dev_alloc(elems * sizeof(double),
                    mp::Tier::SESSION_PERSISTENT);
    if (g_v == 0) {
        dev_free(g_k);
        g_k = 0;
        return 2;
    }
    g_layers = layers;
    g_kv_heads = kv_heads;
    g_head_dim = head_dim;
    g_max_len = max_len;
    return 0;
}

void xcuda_kv_free() {
    std::lock_guard<std::mutex> lk(g_kv_mu);
    kv_free_locked();
}

// Write `rows` contiguous head_dim rows for one (layer, kv_head) starting
// at position pos0. src is a host pointer of rows*head_dim doubles.
int xcuda_kv_write_rows(int is_k, long long layer, long long head,
                        long long pos0, long long rows, const double* src) {
    std::lock_guard<std::mutex> lk(g_kv_mu);
    if (g_k == 0 || src == nullptr || layer < 0 || layer >= g_layers ||
        head < 0 || head >= g_kv_heads || pos0 < 0 || rows <= 0 ||
        pos0 + rows > g_max_len) {
        return 1;
    }
    CUdevptr_t base = (is_k ? g_k : g_v) +
        static_cast<CUdevptr_t>(
            (layer * g_kv_heads + head) * g_max_len + pos0) *
            static_cast<CUdevptr_t>(g_head_dim) * sizeof(double);
    const size_t bytes = static_cast<size_t>(rows) *
                         static_cast<size_t>(g_head_dim) *
                         sizeof(double);
    // Async lane: stage through pinned memory and enqueue on
    // DECODE_HIGH — the same stream the attention kernel and the
    // bf16 graph replays run on, so writes are FIFO-ordered before
    // every consumer without a per-call context sync.
    const CUstream_t s = g_drv.memcpy_htod_async != nullptr &&
                                 mp::mgr().ensure()
                             ? decode_stream()
                             : 0;
    if (s != 0 && g_drv.stream_sync != nullptr) {
        if (bytes <= kKvSlotBytes) {
            const int slot =
                static_cast<int>(g_kv_wseq % kKvSlotN);
            if (g_kv_wseq >= kKvSlotN && slot == 0) {
                // Ring wrap: one lane drain retires every staged
                // copy; the previous occupant of each slot is then
                // safe to overwrite.
                g_drv.stream_sync(s);
            }
            if (g_kv_wslot[slot] == nullptr) {
                g_kv_wslot[slot] = static_cast<double*>(
                    mp::mgr().pinned_alloc(
                        static_cast<int64_t>(kKvSlotBytes)));
            }
            if (g_kv_wslot[slot] != nullptr) {
                std::memcpy(g_kv_wslot[slot], src, bytes);
                if (g_drv.memcpy_htod_async(
                        base, g_kv_wslot[slot], bytes, s) ==
                    kCudaSuccess) {
                    ++g_kv_wseq;
                    mp::mgr().h2d_bytes +=
                        static_cast<int64_t>(bytes);
                    return 0;
                }
            }
        } else {
            if (g_kv_wpend) {
                g_drv.stream_sync(s);
                g_kv_wpend = false;
            }
            if (kv_pin_grow(&g_kv_wpin, &g_kv_wcap, bytes)) {
                std::memcpy(g_kv_wpin, src, bytes);
                if (g_drv.memcpy_htod_async(
                        base, g_kv_wpin, bytes, s) ==
                    kCudaSuccess) {
                    g_kv_wpend = true;
                    mp::mgr().h2d_bytes +=
                        static_cast<int64_t>(bytes);
                    return 0;
                }
            }
        }
    }
    // Sync fallback: drain the lane first so the legacy-stream copy
    // cannot pass earlier enqueued writes.
    if (s != 0 && g_drv.stream_sync != nullptr) {
        g_drv.stream_sync(s);
        g_kv_wpend = false;
        g_kv_wseq = 0;   // all staged copies retired
    }
    if (xmemcpy_htod(base, src, bytes) != kCudaSuccess) {
        return 2;
    }
    return 0;
}

// Online-softmax attention over the device cache for one layer.
// q_host: [heads][seq][head_dim] (RoPE already applied).
// out_host: element (s,h,d) → out[s*out_stride + h*head_dim + d].
int xcuda_kv_attention(long long layer, const double* q_host,
                       long long heads, long long seq, long long kv_heads,
                       long long head_dim, long long position_offset,
                       double* out_host, long long out_stride) {
    std::lock_guard<std::mutex> lk(g_kv_mu);
    if (g_k == 0 || q_host == nullptr || out_host == nullptr ||
        layer < 0 || layer >= g_layers || heads <= 0 || seq <= 0 ||
        kv_heads <= 0 || heads % kv_heads != 0 || head_dim <= 0 ||
        head_dim != g_head_dim || head_dim > kMaxHeadDim ||
        kv_heads != g_kv_heads || position_offset < 0 ||
        position_offset + seq > g_max_len ||
        out_stride < heads * head_dim) {
        return 1;
    }
    if (!use_ctx()) return 2;
    const size_t q_elems = static_cast<size_t>(heads) *
                           static_cast<size_t>(seq) *
                           static_cast<size_t>(head_dim);
    const size_t o_elems = static_cast<size_t>(seq) *
                           static_cast<size_t>(out_stride);
    if (!grow_scratch(&g_qbuf, &g_qcap, q_elems) ||
        !grow_scratch(&g_obuf, &g_ocap, o_elems)) {
        return 2;
    }
    // scores[128] | acc[hd] | red[32] | scal[5] | staged q[hd]
    const unsigned int shmem = static_cast<unsigned int>(
        (static_cast<size_t>(kKvTile) + static_cast<size_t>(head_dim) * 2 +
         32 + 5) * sizeof(double));
    long long h_ll = heads, s_ll = seq, kh_ll = kv_heads,
              hd_ll = head_dim, ml_ll = g_max_len,
              po_ll = position_offset, os_ll = out_stride;
    CUdevptr_t kbase = g_k +
        static_cast<CUdevptr_t>(layer) *
            static_cast<CUdevptr_t>(g_kv_heads * g_max_len *
                                    g_head_dim) *
            sizeof(double);
    CUdevptr_t vbase = g_v +
        static_cast<CUdevptr_t>(layer) *
            static_cast<CUdevptr_t>(g_kv_heads * g_max_len *
                                    g_head_dim) *
            sizeof(double);
    void* params[] = {&g_qbuf, &kbase, &vbase,
                      &h_ll, &s_ll, &kh_ll, &hd_ll,
                      &ml_ll, &po_ll, &g_obuf, &os_ll};
    // Async lane: q staged through pinned memory, H2D + kernel + D2H
    // all ordered on DECODE_HIGH — one lane sync replaces the
    // whole-context drain, and the kernel sees every KV row written
    // earlier on the same stream.
    const size_t q_bytes = q_elems * sizeof(double);
    const size_t o_bytes = o_elems * sizeof(double);
    const CUstream_t s =
        g_drv.memcpy_htod_async != nullptr &&
                g_drv.memcpy_dtoh_async != nullptr &&
                g_drv.stream_sync != nullptr && mp::mgr().ensure()
            ? decode_stream()
            : 0;
    if (s != 0 && kv_pin_grow(&g_kv_qpin, &g_kv_qcap, q_bytes) &&
        kv_pin_grow(&g_kv_opin, &g_kv_ocap, o_bytes)) {
        std::memcpy(g_kv_qpin, q_host, q_bytes);
        if (g_drv.memcpy_htod_async(g_qbuf, g_kv_qpin, q_bytes, s) ==
                kCudaSuccess &&
            launch_s(g_f_kv_attn,
                     static_cast<unsigned int>(heads * seq), 1,
                     static_cast<unsigned int>(kKvThreads), 1, shmem,
                     params, s) &&
            g_drv.memcpy_dtoh_async(g_kv_opin, g_obuf, o_bytes, s) ==
                kCudaSuccess &&
            g_drv.stream_sync(s) == kCudaSuccess) {
            std::memcpy(out_host, g_kv_opin, o_bytes);
            mp::mgr().h2d_bytes += static_cast<int64_t>(q_bytes);
            mp::mgr().d2h_bytes += static_cast<int64_t>(o_bytes);
            return 0;
        }
        if (g_drv.stream_sync(s) != kCudaSuccess) return 3;
        return 3;
    }
    // Sync fallback: drain the decode lane first — pending async KV
    // writes must be visible before the stream-0 kernel reads them.
    if (s != 0) g_drv.stream_sync(s);
    if (xmemcpy_htod(g_qbuf, q_host, q_bytes) != kCudaSuccess) {
        return 2;
    }
    if (!launch(g_f_kv_attn,
                static_cast<unsigned int>(heads * seq), 1,
                static_cast<unsigned int>(kKvThreads), 1, shmem,
                params)) {
        return 3;
    }
    if (g_drv.ctx_sync() != kCudaSuccess) return 3;
    if (xmemcpy_dtoh(out_host, g_obuf, o_bytes) != kCudaSuccess) {
        return 3;
    }
    return 0;
}

// ------------------------------------------------- fused AdamW API ----
// §26 NativeCudaFusedAdamW host surface. rc contract: 0 ok, 2 bad args,
// 3 device/module/copy failure, 4 unknown key, 5 bind mismatch.

int xcuda_adamw_probe() {
    return device_ready() && ensure_module() &&
                   g_f_adamw != 0 && g_f_sqsum != 0
               ? 1 : 0;
}

// Bind (idempotent) one tensor's fp32 w/m/v onto the device, keyed by
// the host weight pointer — the same stability contract as
// device_weight_bf16: pointers are stable for the run; a rebind with a
// different element count fails closed rather than aliasing.
int xcuda_adamw_bind(const float* w_host, const float* m_host,
                     const float* v_host, long long n) {
    if (w_host == nullptr || m_host == nullptr || v_host == nullptr ||
        n <= 0)
        return 2;
    if (!use_ctx() || !ensure_module()) return 3;
    std::lock_guard<std::mutex> lk(g_adamw_mu);
    auto it = g_adamw.find(w_host);
    if (it != g_adamw.end())
        return it->second.n == n ? 0 : 5;
    AdamwState st;
    st.n = n;
    const size_t bytes = static_cast<size_t>(n) * sizeof(float);
    st.dw = dev_alloc(bytes, mp::Tier::PINNED_PERMANENT);
    st.dm = dev_alloc(bytes, mp::Tier::PINNED_PERMANENT);
    st.dv = dev_alloc(bytes, mp::Tier::PINNED_PERMANENT);
    st.dg = dev_alloc(bytes, mp::Tier::KERNEL_SCRATCH);
    if (st.dw == 0 || st.dm == 0 || st.dv == 0 || st.dg == 0) {
        if (st.dw) dev_free(st.dw);
        if (st.dm) dev_free(st.dm);
        if (st.dv) dev_free(st.dv);
        if (st.dg) dev_free(st.dg);
        return 3;
    }
    if (xmemcpy_htod(st.dw, w_host, bytes) != kCudaSuccess ||
        xmemcpy_htod(st.dm, m_host, bytes) != kCudaSuccess ||
        xmemcpy_htod(st.dv, v_host, bytes) != kCudaSuccess) {
        dev_free(st.dw); dev_free(st.dm); dev_free(st.dv);
        dev_free(st.dg);
        return 3;
    }
    g_adamw.emplace(w_host, st);
    return 0;
}

// §26 fused step: H2D the gradient then one kernel pass — moments,
// decoupled decay and the parameter update never touch the host.
// gscale folds the caller's global-clip coefficient (xc_adamw_sqsum or
// the host norm); b1/b2/eps mirror the trainer's adamw_step constants.
int xcuda_adamw_step_dev(const float* g_host, const void* w_key,
                         float gscale, float lr_t, float wd, int step) {
    if (g_host == nullptr || w_key == nullptr || step < 0) return 2;
    std::lock_guard<std::mutex> lk(g_adamw_mu);
    auto it = g_adamw.find(w_key);
    if (it == g_adamw.end()) return 4;
    AdamwState& st = it->second;
    if (!use_ctx() || !ensure_module()) return 3;
    const size_t bytes = static_cast<size_t>(st.n) * sizeof(float);
    if (xmemcpy_htod(st.dg, g_host, bytes) != kCudaSuccess) return 3;
    // Bias corrections are computed on the host in fp64 — identical to
    // the trainer's std::pow path, so the update matches bit-for-bit.
    float b1 = 0.9f, b2 = 0.999f, eps = 1e-8f;
    float bc1 = static_cast<float>(
        1.0 - std::pow(0.9, static_cast<double>(step + 1)));
    float bc2 = static_cast<float>(
        1.0 - std::pow(0.999, static_cast<double>(step + 1)));
    long long n = st.n;
    float gs = gscale, lt = lr_t, wdv = wd;
    void* params[] = {&st.dg, &st.dm, &st.dv, &st.dw,
                      &gs, &lt, &wdv, &b1, &b2, &bc1, &bc2, &eps, &n};
    const unsigned blocks = static_cast<unsigned int>(
        std::min<long long>((n + 255) / 256, 65535));
    if (!launch(g_f_adamw, blocks, 1, 256, 1, 0, params)) return 3;
    return 0;
}

// Checkpoint/eval boundary sync — never on the hot path.
int xcuda_adamw_sync(const void* w_key, float* w_out, float* m_out,
                     float* v_out) {
    if (w_key == nullptr) return 2;
    std::lock_guard<std::mutex> lk(g_adamw_mu);
    auto it = g_adamw.find(w_key);
    if (it == g_adamw.end()) return 4;
    if (!use_ctx()) return 3;
    const size_t bytes = static_cast<size_t>(it->second.n) * sizeof(float);
    if (w_out != nullptr &&
        xmemcpy_dtoh(w_out, it->second.dw, bytes) != kCudaSuccess)
        return 3;
    if (m_out != nullptr &&
        xmemcpy_dtoh(m_out, it->second.dm, bytes) != kCudaSuccess)
        return 3;
    if (v_out != nullptr &&
        xmemcpy_dtoh(v_out, it->second.dv, bytes) != kCudaSuccess)
        return 3;
    return 0;
}

// §26 clip front half: Σx² for one tensor — block partials on device,
// host reduces in fp64 (same precision class as the scalar loop; the
// partials reduce over 128 blocks, not per element, so the sum differs
// from the host's sequential order only below fp32 clip tolerance).
int xcuda_adamw_sqsum(const float* x_host, long long n, double* out) {
    if (x_host == nullptr || out == nullptr || n <= 0) return 2;
    if (!use_ctx() || !ensure_module() || g_f_sqsum == 0) return 3;
    const size_t bytes = static_cast<size_t>(n) * sizeof(float);
    CUdevptr_t dx = dev_alloc(bytes, mp::Tier::KERNEL_SCRATCH);
    if (dx == 0) return 3;
    int rc = 3;
    CUdevptr_t part = dev_get_pooled(
        g_adamw_norm_part, kSqsumBlocks * sizeof(float));
    if (part != 0 &&
        xmemcpy_htod(dx, x_host, bytes) == kCudaSuccess) {
        long long nl = n;
        void* params[] = {&dx, &part, &nl};
        if (launch(g_f_sqsum, kSqsumBlocks, 1, 256, 1, 0, params)) {
            g_adamw_norm_host.resize(kSqsumBlocks);
            if (xmemcpy_dtoh(g_adamw_norm_host.data(), part,
                             kSqsumBlocks * sizeof(float)) ==
                kCudaSuccess) {
                double s = 0.0;
                for (int b = 0; b < kSqsumBlocks; ++b)
                    s += static_cast<double>(g_adamw_norm_host[b]);
                *out = s;
                rc = 0;
            }
        }
    }
    dev_free(dx);
    return rc;
}

// Run teardown: free every bound tensor's device state.
int xcuda_adamw_release() {
    std::lock_guard<std::mutex> lk(g_adamw_mu);
    for (auto& kv : g_adamw) {
        dev_free(kv.second.dw);
        dev_free(kv.second.dm);
        dev_free(kv.second.dv);
        dev_free(kv.second.dg);
    }
    g_adamw.clear();
    dev_pool_release(g_adamw_norm_part);
    g_adamw_norm_host.clear();
    g_adamw_norm_host.shrink_to_fit();
    return 0;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
