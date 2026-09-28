// CUDA bridge for the Xingcheng C++ inference engine (independent
// acceleration track — not part of the four-language layering; the pure-C
// core stays untouched). v2: cuBLAS Dgemm with device-resident weights —
// the B operand of every engine matmul is a transposed weight whose host
// pointer is stable for the engine lifetime, so it is uploaded once and
// cached by pointer; activations stay transient per call.
//
// Host-API only (no device kernels), so it compiles as plain C++ — this
// also sidesteps nvcc's MSVC-version ceiling; custom kernels will need a
// compatible nvcc/MSVC pair or a precompiled cubin.
//
// Governance: the engine only calls into this when XINGCHENG_CPP_CUDA=1 is
// set by the governed Python layer after GPU coordination — capability
// lives here, the decision stays above.

#ifdef XINGCHENG_CUDA

#include <cublas_v2.h>
#include <cuda_runtime.h>

#include <cstring>
#include <mutex>
#include <unordered_map>

namespace {

// Device-resident weight cache: host weight pointer -> device buffer.
// Weight pointers are stable for the engine lifetime (bundle storage /
// owned dequantized tensors) and never mutate after load, so a cached
// device copy stays valid. Released via xcuda_release_weights() on
// engine unload.
std::mutex g_mu;
std::unordered_map<const void*, void*> g_dev_weights;
cublasHandle_t g_handle = nullptr;

// Transient activation buffers, pooled engine-lifetime-wide. cudaMalloc/
// cudaFree per GEMM call was the dominant per-token overhead (cudaFree also
// implies a device sync); grow-on-demand keeps allocation off the hot path.
struct DevBuf {
    void* ptr = nullptr;
    size_t cap = 0;
};
DevBuf g_dev_a, g_dev_c;

void* dev_get(DevBuf& buf, size_t bytes) {
    if (buf.cap >= bytes) return buf.ptr;
    void* next = nullptr;
    if (cudaMalloc(&next, bytes) != cudaSuccess) return nullptr;
    if (buf.ptr) cudaFree(buf.ptr);
    buf.ptr = next;
    buf.cap = bytes;
    return buf.ptr;
}

// Pinned host staging: pageable H2D/D2H copies are staged through a driver
// bounce buffer; pinned memory makes both directions direct (still
// synchronous — no behaviour change). Optional: a failed pinned alloc falls
// back to the direct pageable copy below.
struct HostBuf {
    double* ptr = nullptr;
    size_t cap = 0;  // elements
};
HostBuf g_pin_a, g_pin_c;

double* host_get(HostBuf& buf, size_t elems) {
    if (buf.cap >= elems) return buf.ptr;
    double* next = nullptr;
    if (cudaHostAlloc(
            reinterpret_cast<void**>(&next), elems * sizeof(double),
            cudaHostAllocDefault) != cudaSuccess) {
        return nullptr;
    }
    if (buf.ptr) cudaFreeHost(buf.ptr);
    buf.ptr = next;
    buf.cap = elems;
    return buf.ptr;
}

cublasHandle_t get_handle() {
    if (g_handle == nullptr &&
        cublasCreate(&g_handle) != CUBLAS_STATUS_SUCCESS) {
        return nullptr;
    }
    return g_handle;
}

void* device_weight(const double* host, size_t bytes) {
    auto it = g_dev_weights.find(host);
    if (it != g_dev_weights.end()) return it->second;
    void* dev = nullptr;
    if (cudaMalloc(&dev, bytes) != cudaSuccess) return nullptr;
    if (cudaMemcpy(dev, host, bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
        cudaFree(dev);
        return nullptr;
    }
    g_dev_weights.emplace(host, dev);
    return dev;
}

}  // namespace

extern "C" {

#if defined(XINGCHENG_CUDA_KERNELS)
// Provided by src/kernels/*.cu when the nvcc toolchain is available at
// build time (build_cpp.py sets XINGCHENG_CUDA_KERNELS).
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
#endif

int xcuda_available() {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess) return 0;
    return count > 0 ? 1 : 0;
}

// Free every cached device weight and the shared cuBLAS handle. Called by
// the engine on unload so a stale pointer can never alias a new tensor.
int xcuda_release_weights() {
    std::lock_guard<std::mutex> lk(g_mu);
    for (auto& kv : g_dev_weights) cudaFree(kv.second);
    g_dev_weights.clear();
    if (g_dev_a.ptr) cudaFree(g_dev_a.ptr);
    if (g_dev_c.ptr) cudaFree(g_dev_c.ptr);
    g_dev_a = DevBuf{};
    g_dev_c = DevBuf{};
    if (g_pin_a.ptr) cudaFreeHost(g_pin_a.ptr);
    if (g_pin_c.ptr) cudaFreeHost(g_pin_c.ptr);
    g_pin_a = HostBuf{};
    g_pin_c = HostBuf{};
    if (g_handle != nullptr) {
        cublasDestroy(g_handle);
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
        double* db = static_cast<double*>(device_weight(b, b_bytes));
        cublasHandle_t handle = get_handle();
        double* da = static_cast<double*>(dev_get(g_dev_a, a_bytes));
        double* dc = static_cast<double*>(dev_get(g_dev_c, c_bytes));
        if (db == nullptr || handle == nullptr ||
            da == nullptr || dc == nullptr) {
            return 3;
        }
        double* ha = host_get(g_pin_a, a_elems);
        double* hc = host_get(g_pin_c, c_elems);
        if (ha != nullptr) {
            std::memcpy(ha, a, a_bytes);
            if (cudaMemcpy(da, ha, a_bytes, cudaMemcpyHostToDevice) !=
                cudaSuccess) {
                return 3;
            }
        } else if (
            cudaMemcpy(da, a, a_bytes, cudaMemcpyHostToDevice) !=
            cudaSuccess) {
            return 3;
        }
        {
            const double alpha = 1.0;
            const double beta = 0.0;
            if (cublasDgemm(
                    handle, CUBLAS_OP_N, CUBLAS_OP_N,
                    static_cast<int>(n), static_cast<int>(m),
                    static_cast<int>(k),
                    &alpha, db, static_cast<int>(n),
                    da, static_cast<int>(k), &beta,
                    dc, static_cast<int>(n)) != CUBLAS_STATUS_SUCCESS) {
                return 3;
            }
        }
        if (hc != nullptr) {
            if (cudaMemcpy(hc, dc, c_bytes, cudaMemcpyDeviceToHost) !=
                cudaSuccess) {
                return 3;
            }
            std::memcpy(out, hc, c_bytes);
        } else if (
            cudaMemcpy(out, dc, c_bytes, cudaMemcpyDeviceToHost) !=
            cudaSuccess) {
            return 3;
        }
        rc = 0;
    }
    return rc;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
