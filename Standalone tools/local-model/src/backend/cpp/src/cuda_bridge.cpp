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
// Provided by src/kernels/matmul_bf16.cu when the nvcc toolchain is
// available at build time (build_cpp.py sets XINGCHENG_CUDA_KERNELS).
int xcuda_bf16_kernel_probe();
int xcuda_bf16_release_weights();
#else
// Stub so the symbol always resolves; the bf16 request path fails closed
// through xcuda_bf16_available()==0 before ever reaching this.
int xcuda_matmul_bf16(
    const double*, long long, long long,
    const double*, long long, double*) {
    return 3;
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
    if (g_handle != nullptr) {
        cublasDestroy(g_handle);
        g_handle = nullptr;
    }
#if defined(XINGCHENG_CUDA_KERNELS)
    xcuda_bf16_release_weights();
#endif
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

    double *da = nullptr, *dc = nullptr;
    int rc = 3;

    {
        std::lock_guard<std::mutex> lk(g_mu);
        double* db = static_cast<double*>(device_weight(b, b_bytes));
        cublasHandle_t handle = get_handle();
        if (db == nullptr || handle == nullptr) return 3;
        if (cudaMalloc(&da, a_bytes) != cudaSuccess) goto done;
        if (cudaMalloc(&dc, c_bytes) != cudaSuccess) goto done;
        if (cudaMemcpy(da, a, a_bytes, cudaMemcpyHostToDevice) != cudaSuccess)
            goto done;
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
                goto done;
            }
        }
        if (cudaMemcpy(out, dc, c_bytes, cudaMemcpyDeviceToHost) !=
            cudaSuccess) {
            goto done;
        }
        rc = 0;
done:;
    }
    if (da) cudaFree(da);
    if (dc) cudaFree(dc);
    return rc;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
