// CUDA bridge for the Xingcheng C++ inference engine (independent
// acceleration track — not part of the four-language layering; the pure-C
// core stays untouched). v2: cuBLAS Dgemm with device-resident weights —
// the B operand of every engine matmul is a transposed weight whose host
// pointer is stable for the engine lifetime, so it is uploaded once and
// cached by pointer; activations stay transient per call.
//
// Host-API only; device kernels live in cuda_kernels.cpp, which compiles
// them through NVRTC at run time (nvcc needs an MSVC release it no longer
// supports, so a precompiled cubin is not an option on this toolchain).
// cuBLAS is resolved via LoadLibrary instead of an import lib, so the
// built binary keeps zero CUDA dll dependencies: on a host without the
// toolkit every probe returns 0 and the governed request paths fail
// closed rather than failing to start.
//
// Governance: the engine only calls into this when XINGCHENG_CPP_CUDA=1 is
// set by the governed layer — capability lives here, the decision stays
// above.

#ifdef XINGCHENG_CUDA

#include <windows.h>

#include <cuda_runtime.h>

#include <climits>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>

// NativeMemoryCudaPlane §3: every device allocation routes through the
// single UnifiedCudaMemoryManager — no naked cudaMalloc/cudaFree here.
#include "cuda_memplane.h"

namespace mp = xcm_memplane;

namespace {

// cuBLAS via dynamic binding — cublas64_12.dll ships with the toolkit,
// not the driver, so it must never be an import dependency of the exe.
typedef void* cublasHandle_t;
typedef int cublasStatus_t;
typedef int cublasOperation_t;
constexpr cublasStatus_t kCublasSuccess = 0;
constexpr cublasOperation_t kCublasOpN = 0;

struct CublasApi {
    HMODULE dll = nullptr;
    cublasStatus_t (*create)(cublasHandle_t*) = nullptr;
    cublasStatus_t (*destroy)(cublasHandle_t) = nullptr;
    cublasStatus_t (*set_stream)(cublasHandle_t, cudaStream_t) = nullptr;
    cublasStatus_t (*dgemm)(cublasHandle_t, cublasOperation_t,
                            cublasOperation_t, int, int, int,
                            const double*, const double*, int,
                            const double*, int, const double*, double*,
                            int) = nullptr;
};

CublasApi g_cublas;
bool g_cublas_tried = false;

bool cublas_ready() {
    if (g_cublas_tried) return g_cublas.dll != nullptr;
    g_cublas_tried = true;
    HMODULE dll = LoadLibraryA("cublas64_12.dll");
    if (dll == nullptr) {
        const char* cp = getenv("CUDA_PATH");
        if (cp != nullptr && *cp != '\0') {
            std::string p = std::string(cp) +
                "\\bin\\cublas64_12.dll";
            dll = LoadLibraryA(p.c_str());
        }
    }
    if (dll == nullptr) {
        dll = LoadLibraryA(
            "C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\"
            "v12.0\\bin\\cublas64_12.dll");
    }
    if (dll == nullptr) return false;
    g_cublas.dll = dll;
    g_cublas.create = reinterpret_cast<decltype(g_cublas.create)>(
        GetProcAddress(dll, "cublasCreate_v2"));
    g_cublas.destroy = reinterpret_cast<decltype(g_cublas.destroy)>(
        GetProcAddress(dll, "cublasDestroy_v2"));
    g_cublas.set_stream = reinterpret_cast<decltype(g_cublas.set_stream)>(
        GetProcAddress(dll, "cublasSetStream_v2"));
    g_cublas.dgemm = reinterpret_cast<decltype(g_cublas.dgemm)>(
        GetProcAddress(dll, "cublasDgemm_v2"));
    if (g_cublas.create == nullptr || g_cublas.destroy == nullptr ||
        g_cublas.dgemm == nullptr || g_cublas.set_stream == nullptr) {
        g_cublas.dll = nullptr;
        return false;
    }
    return true;
}

// Device-resident weight cache: host weight pointer -> device buffer.
// Weight pointers are stable for the engine lifetime (bundle storage /
// owned dequantized tensors) and never mutate after load, so a cached
// device copy stays valid. Released via xcuda_release_weights() on
// engine unload. Allocations route through the unified memory manager
// (§3) — PINNED_PERMANENT tier.
std::mutex g_mu;
std::unordered_map<const void*, std::pair<void*, size_t>> g_dev_weights;
cublasHandle_t g_handle = nullptr;

// Transient activation buffers, pooled engine-lifetime-wide through the
// manager (LAYER_TEMP tier). Grow-on-demand keeps allocation off the hot
// path; growth is a load-time event, not a per-token one.
struct DevBuf {
    void* ptr = nullptr;
    size_t cap = 0;
};
DevBuf g_dev_a, g_dev_c;

void* dev_get(DevBuf& buf, size_t bytes) {
    if (buf.cap >= bytes) return buf.ptr;
    void* next = mp::mgr().alloc(
        mp::Tier::LAYER_TEMP, (int64_t)bytes,
        mp::StreamLane::PREFILL_NORMAL);
    if (next == nullptr) return nullptr;
    if (buf.ptr) mp::mgr().free(buf.ptr, mp::StreamLane::PREFILL_NORMAL);
    buf.ptr = next;
    buf.cap = bytes;
    return buf.ptr;
}

// Pinned host staging through the manager's bounded pool (§13/§14):
// pageable H2D/D2H copies stage through pinned memory so both
// directions go direct on the transfer lanes. A failed pinned alloc
// falls back to the direct pageable copy below — pageable is legal,
// unmanaged pinning is not.
struct HostBuf {
    double* ptr = nullptr;
    size_t cap = 0;  // elements
};
HostBuf g_pin_a, g_pin_c;

double* host_get(HostBuf& buf, size_t elems) {
    if (buf.cap >= elems) return buf.ptr;
    double* next = static_cast<double*>(mp::mgr().pinned_alloc(
        (int64_t)(elems * sizeof(double))));
    if (next == nullptr) return nullptr;
    buf.ptr = next;
    buf.cap = elems;
    return buf.ptr;
}

cublasHandle_t get_handle() {
    if (g_handle == nullptr) {
        if (!mp::mgr().ensure() || !cublas_ready() ||
            g_cublas.create(&g_handle) != kCublasSuccess) {
            return nullptr;
        }
        // §16-§17: GEMM work rides the PREFILL lane, never the legacy
        // default stream — cross-lane ordering is by event, so a D2H
        // copy can never read a buffer the GEMM is still writing.
        if (g_cublas.set_stream(
                g_handle, mp::mgr().stream(
                              mp::StreamLane::PREFILL_NORMAL)) !=
                kCublasSuccess) {
            g_cublas.destroy(g_handle);
            g_handle = nullptr;
            return nullptr;
        }
    }
    return g_handle;
}

// Event chain for H2D -> compute -> D2H ordering across lanes.
// Non-blocking streams never synchronize with the legacy default
// stream; explicit events are the contract (§16).
cudaEvent_t g_ev_in = nullptr, g_ev_out = nullptr;

bool ensure_events() {
    if (g_ev_in == nullptr &&
        cudaEventCreateWithFlags(&g_ev_in, cudaEventDisableTiming)
            != cudaSuccess)
        return false;
    if (g_ev_out == nullptr &&
        cudaEventCreateWithFlags(&g_ev_out, cudaEventDisableTiming)
            != cudaSuccess)
        return false;
    return true;
}

void* device_weight(const double* host, size_t bytes) {
    auto it = g_dev_weights.find(host);
    if (it != g_dev_weights.end()) return it->second.first;
    // §3/§7 PINNED_PERMANENT + §15 async H2D on the transfer lane;
    // the stream-scoped sync keeps first-use semantics without a
    // device-wide barrier (§16).
    void* dev = mp::mgr().alloc(
        mp::Tier::PINNED_PERMANENT, (int64_t)bytes,
        mp::StreamLane::H2D);
    if (dev == nullptr) return nullptr;
    cudaStream_t s = mp::mgr().stream(mp::StreamLane::H2D);
    if (cudaMemcpyAsync(dev, host, bytes, cudaMemcpyHostToDevice, s)
            != cudaSuccess ||
        cudaStreamSynchronize(s) != cudaSuccess) {
        mp::mgr().free(dev, mp::StreamLane::H2D);
        return nullptr;
    }
    mp::mgr().h2d_bytes += (int64_t)bytes;
    g_dev_weights.emplace(host, std::make_pair(dev, bytes));
    return dev;
}

}  // namespace

extern "C" {

#if defined(XINGCHENG_CUDA_KERNELS)
// Provided by cuda_kernels.cpp (NVRTC + Driver API) when the CUDA build
// flag XINGCHENG_CUDA_KERNELS is set.
int xcuda_bf16_kernel_probe();
int xcuda_bf16_release_weights();
int xcuda_fp8_kernel_probe();
int xcuda_fp8_release_weights();
int xcuda_kv_kernel_probe();
int xcuda_kv_alloc(long long layers, long long kv_heads, long long head_dim,
                   long long max_len);
void xcuda_kv_free();
int xcuda_kv_write_rows(int is_k, long long layer, long long head,
                        long long pos0, long long rows, const double* src);
int xcuda_kv_attention(long long layer, const double* q, long long heads,
                       long long seq, long long kv_heads, long long head_dim,
                       long long position_offset, double* out,
                       long long out_stride);
#else
// Stubs so the symbols always resolve; the bf16/KV request paths fail
// closed through *_available()==0 before ever reaching these.
int xcuda_matmul_bf16(
    const double*, long long, long long,
    const double*, long long, double*) {
    return 3;
}
int xcuda_matmul_fp8(
    const double*, long long, long long,
    const double*, long long, double*) {
    return 3;
}
int xcuda_kv_alloc(long long, long long, long long, long long) { return 1; }
void xcuda_kv_free() {}
int xcuda_kv_write_rows(int, long long, long long, long long, long long,
                        const double*) {
    return 1;
}
int xcuda_kv_attention(long long, const double*, long long, long long,
                       long long, long long, long long, double*,
                       long long) {
    return 1;
}
int xcuda_probe(long long*, long long*, int*, int*) { return 0; }
int xcuda_gpu_stats(unsigned*, unsigned*) { return 0; }
void xcuda_graph_enable(int) {}
int xcuda_graph_state() { return 0; }
#endif

int xcuda_available() {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess) return 0;
    // The base CUDA path is cuBLAS Dgemm — without the toolkit dll the
    // capability is absent, not partially present. The memory plane is
    // initialised lazily here so pool/streams exist before first use.
    return count > 0 && cublas_ready() &&
           mp::mgr().ensure() ? 1 : 0;
}

// Free every cached device weight and the shared cuBLAS handle. Called by
// the engine on unload so a stale pointer can never alias a new tensor.
// Device bytes release through the memory plane (§3/§6 maintenance trim).
int xcuda_release_weights() {
    std::lock_guard<std::mutex> lk(g_mu);
    for (auto& kv : g_dev_weights)
        mp::mgr().free(kv.second.first, mp::StreamLane::H2D);
    g_dev_weights.clear();
    if (g_dev_a.ptr) mp::mgr().free(g_dev_a.ptr, mp::StreamLane::H2D);
    if (g_dev_c.ptr) mp::mgr().free(g_dev_c.ptr, mp::StreamLane::H2D);
    g_dev_a = DevBuf{};
    g_dev_c = DevBuf{};
    mp::mgr().pinned_release_all();
    g_pin_a = HostBuf{};
    g_pin_c = HostBuf{};
    if (g_ev_in) { cudaEventDestroy(g_ev_in); g_ev_in = nullptr; }
    if (g_ev_out) { cudaEventDestroy(g_ev_out); g_ev_out = nullptr; }
    if (g_handle != nullptr) {
        g_cublas.destroy(g_handle);
        g_handle = nullptr;
    }
#if defined(XINGCHENG_CUDA_KERNELS)
    xcuda_bf16_release_weights();
    xcuda_fp8_release_weights();
#endif
    xcuda_kv_free();
    return 0;
}

// bf16 kernel availability: 1 only when the kernels object was linked AND a
// CUDA device exists. Without the kernels TU this fails closed to 0, so the
// engine's BF16 request path can never silently degrade to fp64.
int xcuda_bf16_available() {
#if defined(XINGCHENG_CUDA_KERNELS)
    return xcuda_bf16_kernel_probe();
#else
    return 0;
#endif
}

// fp8 kernel availability: same fail-closed contract.
int xcuda_fp8_available() {
#if defined(XINGCHENG_CUDA_KERNELS)
    return xcuda_fp8_kernel_probe();
#else
    return 0;
#endif
}

// Device-resident KV availability: same fail-closed contract — 1 only when
// the kernels TU is linked and a CUDA device exists.
int xcuda_kv_available() {
#if defined(XINGCHENG_CUDA_KERNELS)
    return xcuda_kv_kernel_probe();
#else
    return 0;
#endif
}

// Row-major C[m,n] = A[m,k] * B[k,n]. cuBLAS is column-major, so compute
// C^T = B^T * A^T with the standard row/col-major swap. B is always a
// transposed weight — resolved through the device-resident cache.
int xcuda_matmul_f64(
    const double* a, long long m, long long k,
    const double* b, long long n,
    double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;

    const size_t a_bytes = static_cast<size_t>(m) * k * sizeof(double);
    const size_t b_bytes = static_cast<size_t>(k) * n * sizeof(double);
    const size_t c_bytes = static_cast<size_t>(m) * n * sizeof(double);
    const size_t a_elems = static_cast<size_t>(m) * k;
    const size_t c_elems = static_cast<size_t>(m) * n;

    int rc = 3;

    {
        std::lock_guard<std::mutex> lk(g_mu);
        // get_handle() first: it owns mp::mgr().ensure() — the pool
        // alloc in device_weight/dev_get returns nullptr until the
        // manager is initialized.
        cublasHandle_t handle = get_handle();
        double* db = static_cast<double*>(device_weight(b, b_bytes));
        double* da = static_cast<double*>(dev_get(g_dev_a, a_bytes));
        double* dc = static_cast<double*>(dev_get(g_dev_c, c_bytes));
        if (db == nullptr || handle == nullptr ||
            da == nullptr || dc == nullptr) {
            return 3;
        }
        double* ha = host_get(g_pin_a, a_elems);
        double* hc = host_get(g_pin_c, c_elems);
        cudaStream_t h2d = mp::mgr().stream(mp::StreamLane::H2D);
        cudaStream_t prefill =
            mp::mgr().stream(mp::StreamLane::PREFILL_NORMAL);
        cudaStream_t d2h = mp::mgr().stream(mp::StreamLane::D2H);
        if (!ensure_events()) return 3;
        // §15/§16: H2D on the transfer lane -> event -> GEMM on the
        // compute lane -> event -> D2H. The only host wait is the
        // final D2H completion; no default-stream semantics involved.
        if (ha != nullptr) {
            std::memcpy(ha, a, a_bytes);
            if (cudaMemcpyAsync(da, ha, a_bytes,
                    cudaMemcpyHostToDevice, h2d) != cudaSuccess)
                return 3;
        } else if (
            cudaMemcpyAsync(da, a, a_bytes,
                cudaMemcpyHostToDevice, h2d) != cudaSuccess) {
            return 3;
        }
        mp::mgr().h2d_bytes += (int64_t)a_bytes;
        if (cudaEventRecord(g_ev_in, h2d) != cudaSuccess ||
            cudaStreamWaitEvent(prefill, g_ev_in, 0) != cudaSuccess)
            return 3;
        {
            const double alpha = 1.0;
            const double beta = 0.0;
            if (g_cublas.dgemm(
                    handle, kCublasOpN, kCublasOpN,
                    static_cast<int>(n), static_cast<int>(m),
                    static_cast<int>(k),
                    &alpha, db, static_cast<int>(n),
                    da, static_cast<int>(k), &beta,
                    dc, static_cast<int>(n)) != kCublasSuccess) {
                return 3;
            }
        }
        if (cudaEventRecord(g_ev_out, prefill) != cudaSuccess ||
            cudaStreamWaitEvent(d2h, g_ev_out, 0) != cudaSuccess)
            return 3;
        if (hc != nullptr) {
            if (cudaMemcpyAsync(hc, dc, c_bytes,
                    cudaMemcpyDeviceToHost, d2h) != cudaSuccess ||
                cudaStreamSynchronize(d2h) != cudaSuccess) {
                return 3;
            }
            std::memcpy(out, hc, c_bytes);
        } else if (
            cudaMemcpyAsync(out, dc, c_bytes,
                cudaMemcpyDeviceToHost, d2h) != cudaSuccess ||
            cudaStreamSynchronize(d2h) != cudaSuccess) {
            return 3;
        }
        mp::mgr().d2h_bytes += (int64_t)c_bytes;
        rc = 0;
    }
    return rc;
}

// R5 grouped fp64 GEMM: one H2D upload of the concatenated activation
// block and one D2H download of the concatenated output, with a per-group
// cuBLAS Dgemm over device-resident weights in between — identical math
// to calling xcuda_matmul_f64 per group, minus the per-group
// transfer/sync overhead (MoE expert dispatch hot path). a holds the
// groups' row-blocks concatenated ([sum(group_rows) x k]); b_list[g] is
// group g's [k x n] weight; out receives the concatenated [sum x n] rows
// in the same order.
int xcuda_matmul_f64_grouped(
    const double* a, const long long* group_rows, long long groups,
    const double* const* b_list, long long k, long long n,
    double* out) {
    if (a == nullptr || group_rows == nullptr || b_list == nullptr ||
        out == nullptr || groups <= 0 || k <= 0 || n <= 0) {
        return 2;
    }
    long long total = 0;
    for (long long g = 0; g < groups; ++g) {
        if (group_rows[g] < 0 || b_list[g] == nullptr ||
            group_rows[g] > LLONG_MAX - total) {
            return 2;
        }
        total += group_rows[g];
    }
    if (total <= 0) return 0;

    const size_t a_elems =
        static_cast<size_t>(total) * static_cast<size_t>(k);
    const size_t c_elems =
        static_cast<size_t>(total) * static_cast<size_t>(n);
    const size_t a_bytes = a_elems * sizeof(double);
    const size_t c_bytes = c_elems * sizeof(double);
    const size_t b_bytes =
        static_cast<size_t>(k) * static_cast<size_t>(n) * sizeof(double);

    std::lock_guard<std::mutex> lk(g_mu);
    cublasHandle_t handle = get_handle();
    double* da = static_cast<double*>(dev_get(g_dev_a, a_bytes));
    double* dc = static_cast<double*>(dev_get(g_dev_c, c_bytes));
    if (handle == nullptr || da == nullptr || dc == nullptr) return 3;
    double* ha = host_get(g_pin_a, a_elems);
    double* hc = host_get(g_pin_c, c_elems);
    cudaStream_t h2d = mp::mgr().stream(mp::StreamLane::H2D);
    cudaStream_t prefill =
        mp::mgr().stream(mp::StreamLane::PREFILL_NORMAL);
    cudaStream_t d2h = mp::mgr().stream(mp::StreamLane::D2H);
    if (!ensure_events()) return 3;
    if (ha != nullptr) {
        std::memcpy(ha, a, a_bytes);
        if (cudaMemcpyAsync(da, ha, a_bytes, cudaMemcpyHostToDevice,
                h2d) != cudaSuccess) {
            return 3;
        }
    } else if (
        cudaMemcpyAsync(da, a, a_bytes, cudaMemcpyHostToDevice,
            h2d) != cudaSuccess) {
        return 3;
    }
    mp::mgr().h2d_bytes += (int64_t)a_bytes;
    if (cudaEventRecord(g_ev_in, h2d) != cudaSuccess ||
        cudaStreamWaitEvent(prefill, g_ev_in, 0) != cudaSuccess)
        return 3;
    {
        const double alpha = 1.0;
        const double beta = 0.0;
        long long off = 0;
        for (long long g = 0; g < groups; ++g) {
            const long long m_g = group_rows[g];
            if (m_g == 0) continue;
            double* db = static_cast<double*>(
                device_weight(b_list[g], b_bytes));
            if (db == nullptr) return 3;
            // Column-major view: A^T is [k x total] (lda=k), C^T is
            // [n x total] (ldc=n); group g owns the column range starting
            // at off in both.
            if (g_cublas.dgemm(
                    handle, kCublasOpN, kCublasOpN,
                    static_cast<int>(n), static_cast<int>(m_g),
                    static_cast<int>(k),
                    &alpha, db, static_cast<int>(n),
                    da + off * k, static_cast<int>(k), &beta,
                    dc + off * n, static_cast<int>(n)) != kCublasSuccess) {
                return 3;
            }
            off += m_g;
        }
    }
    if (cudaEventRecord(g_ev_out, prefill) != cudaSuccess ||
        cudaStreamWaitEvent(d2h, g_ev_out, 0) != cudaSuccess)
        return 3;
    if (hc != nullptr) {
        if (cudaMemcpyAsync(hc, dc, c_bytes, cudaMemcpyDeviceToHost,
                d2h) != cudaSuccess ||
            cudaStreamSynchronize(d2h) != cudaSuccess) {
            return 3;
        }
        std::memcpy(out, hc, c_bytes);
    } else if (
        cudaMemcpyAsync(out, dc, c_bytes, cudaMemcpyDeviceToHost,
            d2h) != cudaSuccess ||
        cudaStreamSynchronize(d2h) != cudaSuccess) {
        return 3;
    }
    mp::mgr().d2h_bytes += (int64_t)c_bytes;
    return 0;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
