// Self-built fp8 GEMM kernel for the Xingcheng C++ inference engine
// (P1-1③ residual — nvcc-built device code; host orchestration and policy
// stay in cuda_bridge.cpp / engine.cpp).
//
// Contract mirrors xcuda_matmul_f64: row-major C[m,n] = A[m,k] * B[k,n],
// where B is a transposed weight whose host pointer is stable for the
// engine lifetime — cached on device in e4m3 (half the VRAM of bf16, an
// eighth of fp64). Activations stay fp32; accumulation is fp32.
//
// sm_86 (RTX 3050) has no fp8 hardware path — cuda_fp8.h conversions are
// software-emulated on this arch, so this is an opt-in EVALUATION path
// (weight-storage compression), not a throughput claim. Gated by
// XINGCHENG_CPP_CUDA_FP8 via the governed layer; requesting it without
// kernels/device fails closed — never silent bf16/fp64 fallback.

#include <cuda_runtime.h>
#include <cuda_fp8.h>

#include <mutex>
#include <unordered_map>

namespace {

constexpr int kTile = 16;

__global__ void f64_to_fp32_kernel(
    const double* in, float* out, long long n) {
    const long long i =
        static_cast<long long>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i < n) out[i] = static_cast<float>(in[i]);
}

__global__ void f64_to_fp8_kernel(
    const double* in, __nv_fp8_e4m3* out, long long n) {
    const long long i =
        static_cast<long long>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i < n) out[i] = __nv_fp8_e4m3(in[i]);
}

__global__ void gemm_fp8_kernel(
    const float* a, const __nv_fp8_e4m3* b, float* c,
    int m, int k, int n) {
    __shared__ float as[kTile][kTile];
    __shared__ __nv_fp8_e4m3 bs[kTile][kTile];
    const int row = blockIdx.y * kTile + threadIdx.y;
    const int col = blockIdx.x * kTile + threadIdx.x;
    float acc = 0.0f;
    for (int t = 0; t < (k + kTile - 1) / kTile; ++t) {
        const int a_col = t * kTile + threadIdx.x;
        const int b_row = t * kTile + threadIdx.y;
        as[threadIdx.y][threadIdx.x] =
            (row < m && a_col < k)
                ? a[static_cast<long long>(row) * k + a_col]
                : 0.0f;
        bs[threadIdx.y][threadIdx.x] =
            (b_row < k && col < n)
                ? b[static_cast<long long>(b_row) * n + col]
                : __nv_fp8_e4m3(0.0f);
        __syncthreads();
        for (int i = 0; i < kTile; ++i) {
            acc += as[threadIdx.y][i] *
                   static_cast<float>(bs[i][threadIdx.x]);
        }
        __syncthreads();
    }
    if (row < m && col < n) c[static_cast<long long>(row) * n + col] = acc;
}

// fp8 device-resident weight cache — separate precision domain from the
// fp64/bf16 caches (same host-pointer stability contract).
std::mutex g_mu;
std::unordered_map<const void*, __nv_fp8_e4m3*> g_fp8_weights;

// Shared GEMM body: a (host f64) × db (device fp8) → out (host f64).
// Caller owns db's lifetime; the two extern entries differ only in how db
// is obtained (cached for engine weights / fresh for the probe).
int run_fp8(
    const double* a, long long m, long long k,
    const __nv_fp8_e4m3* db, long long n, double* out) {
    const long long a_elems = m * k;
    const long long c_elems = m * n;
    int rc = 3;

    double* a_stage = nullptr;
    float* da = nullptr;
    float* dc = nullptr;
    float* c_host = nullptr;

    if (cudaMalloc(&a_stage, a_elems * sizeof(double)) != cudaSuccess)
        goto done;
    if (cudaMalloc(&da, a_elems * sizeof(float)) != cudaSuccess)
        goto done;
    if (cudaMalloc(&dc, c_elems * sizeof(float)) != cudaSuccess)
        goto done;
    c_host = static_cast<float*>(malloc(c_elems * sizeof(float)));
    if (c_host == nullptr) goto done;
    if (cudaMemcpy(a_stage, a, a_elems * sizeof(double),
                   cudaMemcpyHostToDevice) != cudaSuccess)
        goto done;
    f64_to_fp32_kernel<<<
        static_cast<unsigned int>((a_elems + 255) / 256), 256>>>(
        a_stage, da, a_elems);
    if (cudaGetLastError() != cudaSuccess) goto done;
    {
        dim3 threads(kTile, kTile);
        dim3 blocks(
            static_cast<unsigned int>((n + kTile - 1) / kTile),
            static_cast<unsigned int>((m + kTile - 1) / kTile));
        gemm_fp8_kernel<<<blocks, threads>>>(
            da, db, dc, static_cast<int>(m), static_cast<int>(k),
            static_cast<int>(n));
    }
    if (cudaGetLastError() != cudaSuccess) goto done;
    if (cudaMemcpy(c_host, dc, c_elems * sizeof(float),
                   cudaMemcpyDeviceToHost) != cudaSuccess)
        goto done;
    for (long long i = 0; i < c_elems; ++i)
        out[i] = static_cast<double>(c_host[i]);
    rc = 0;
done:
    if (a_stage) cudaFree(a_stage);
    if (da) cudaFree(da);
    if (dc) cudaFree(dc);
    free(c_host);
    return rc;
}

// Quantize host f64 → fresh device fp8 buffer (caller frees).
__nv_fp8_e4m3* upload_fp8(const double* host, long long elems) {
    double* staging = nullptr;
    __nv_fp8_e4m3* dev = nullptr;
    if (cudaMalloc(&staging, elems * sizeof(double)) != cudaSuccess)
        return nullptr;
    if (cudaMalloc(&dev, elems * sizeof(__nv_fp8_e4m3)) != cudaSuccess) {
        cudaFree(staging);
        return nullptr;
    }
    if (cudaMemcpy(staging, host, elems * sizeof(double),
                   cudaMemcpyHostToDevice) != cudaSuccess) {
        cudaFree(staging);
        cudaFree(dev);
        return nullptr;
    }
    const int threads = 256;
    f64_to_fp8_kernel<<<static_cast<unsigned int>(
        (elems + threads - 1) / threads), threads>>>(staging, dev, elems);
    cudaFree(staging);
    if (cudaGetLastError() != cudaSuccess ||
        cudaDeviceSynchronize() != cudaSuccess) {
        cudaFree(dev);
        return nullptr;
    }
    return dev;
}

// fp8 device-resident weight cache — separate precision domain from the
// fp64 cache in cuda_bridge.cpp (same host-pointer stability contract:
// engine weight vectors are immutable for the engine lifetime). The probe
// entry bypasses this entirely because transient caller buffers may reuse
// the same address for different content.
__nv_fp8_e4m3* device_weight_fp8(const double* host, long long elems) {
    auto it = g_fp8_weights.find(host);
    if (it != g_fp8_weights.end()) return it->second;
    __nv_fp8_e4m3* dev = upload_fp8(host, elems);
    if (dev == nullptr) return nullptr;
    g_fp8_weights.emplace(host, dev);
    return dev;
}

}  // namespace

extern "C" {

int xcuda_fp8_kernel_probe() {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess) return 0;
    return count > 0 ? 1 : 0;
}

int xcuda_fp8_release_weights() {
    std::lock_guard<std::mutex> lk(g_mu);
    for (auto& kv : g_fp8_weights) cudaFree(kv.second);
    g_fp8_weights.clear();
    return 0;
}

int xcuda_matmul_fp8(
    const double* a, long long m, long long k,
    const double* b, long long n, double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;

    const long long a_elems = m * k;
    const long long b_elems = k * n;
    const long long c_elems = m * n;
    int rc = 3;

    double* a_stage = nullptr;
    float* da = nullptr;
    float* dc = nullptr;
    float* c_host = nullptr;

    {
        std::lock_guard<std::mutex> lk(g_mu);
        __nv_fp8_e4m3* db = device_weight_fp8(b, b_elems);
        if (db == nullptr) return 3;
        if (cudaMalloc(&a_stage, a_elems * sizeof(double)) != cudaSuccess)
            goto done;
        if (cudaMalloc(&da, a_elems * sizeof(float)) != cudaSuccess)
            goto done;
        if (cudaMalloc(&dc, c_elems * sizeof(float)) != cudaSuccess)
            goto done;
        c_host = static_cast<float*>(malloc(c_elems * sizeof(float)));
        if (c_host == nullptr) goto done;
        if (cudaMemcpy(a_stage, a, a_elems * sizeof(double),
                       cudaMemcpyHostToDevice) != cudaSuccess)
            goto done;
        f64_to_fp32_kernel<<<
            static_cast<unsigned int>((a_elems + 255) / 256), 256>>>(
            a_stage, da, a_elems);
        if (cudaGetLastError() != cudaSuccess) goto done;
        {
            dim3 threads(kTile, kTile);
            dim3 blocks(
                static_cast<unsigned int>((n + kTile - 1) / kTile),
                static_cast<unsigned int>((m + kTile - 1) / kTile));
            gemm_fp8_kernel<<<blocks, threads>>>(
                da, db, dc, static_cast<int>(m), static_cast<int>(k),
                static_cast<int>(n));
        }
        if (cudaGetLastError() != cudaSuccess) goto done;
        if (cudaMemcpy(c_host, dc, c_elems * sizeof(float),
                       cudaMemcpyDeviceToHost) != cudaSuccess)
            goto done;
        for (long long i = 0; i < c_elems; ++i)
            out[i] = static_cast<double>(c_host[i]);
        rc = 0;
done:;
    }
    if (a_stage) cudaFree(a_stage);
    if (da) cudaFree(da);
    if (dc) cudaFree(dc);
    free(c_host);
    return rc;
}

}  // extern "C"
