// Self-built bf16 GEMM kernel for the Xingcheng C++ inference engine
// (P1-1③ — nvcc-built device code; the host orchestration and policy stay
// in cuda_bridge.cpp / engine.cpp).
//
// Contract mirrors xcuda_matmul_f64: row-major C[m,n] = A[m,k] * B[k,n],
// where B is a transposed weight whose host pointer is stable for the
// engine lifetime — cached on device in bf16 (half the VRAM of fp32, a
// quarter of fp64). Accumulation is fp32; this is an opt-in evaluation
// path gated by XINGCHENG_CPP_CUDA_BF16 — capability lives here, the
// governed layer decides whether to request it.

#include <cuda_runtime.h>
#include <cuda_bf16.h>

#include <mutex>
#include <unordered_map>

namespace {

constexpr int kTile = 16;

__global__ void f64_to_bf16_kernel(
    const double* in, __nv_bfloat16* out, long long n) {
    const long long i =
        static_cast<long long>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i < n) out[i] = __double2bfloat16(in[i]);
}

__global__ void gemm_bf16_kernel(
    const __nv_bfloat16* a, const __nv_bfloat16* b, float* c,
    int m, int k, int n) {
    __shared__ __nv_bfloat16 as[kTile][kTile];
    __shared__ __nv_bfloat16 bs[kTile][kTile];
    const int row = blockIdx.y * kTile + threadIdx.y;
    const int col = blockIdx.x * kTile + threadIdx.x;
    float acc = 0.0f;
    for (int t = 0; t < (k + kTile - 1) / kTile; ++t) {
        const int a_col = t * kTile + threadIdx.x;
        const int b_row = t * kTile + threadIdx.y;
        as[threadIdx.y][threadIdx.x] =
            (row < m && a_col < k)
                ? a[static_cast<long long>(row) * k + a_col]
                : __float2bfloat16(0.0f);
        bs[threadIdx.y][threadIdx.x] =
            (b_row < k && col < n)
                ? b[static_cast<long long>(b_row) * n + col]
                : __float2bfloat16(0.0f);
        __syncthreads();
        for (int i = 0; i < kTile; ++i) {
            acc += __bfloat162float(as[threadIdx.y][i]) *
                   __bfloat162float(bs[i][threadIdx.x]);
        }
        __syncthreads();
    }
    if (row < m && col < n) c[static_cast<long long>(row) * n + col] = acc;
}

// bf16 device-resident weight cache — separate precision domain from the
// fp64 cache in cuda_bridge.cpp (same host-pointer stability contract).
std::mutex g_mu;
std::unordered_map<const void*, __nv_bfloat16*> g_bf16_weights;

__nv_bfloat16* device_weight_bf16(const double* host, long long elems) {
    auto it = g_bf16_weights.find(host);
    if (it != g_bf16_weights.end()) return it->second;
    double* staging = nullptr;
    __nv_bfloat16* dev = nullptr;
    if (cudaMalloc(&staging, elems * sizeof(double)) != cudaSuccess)
        return nullptr;
    if (cudaMalloc(&dev, elems * sizeof(__nv_bfloat16)) != cudaSuccess) {
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
    const long long blocks = (elems + threads - 1) / threads;
    f64_to_bf16_kernel<<<static_cast<unsigned int>(blocks), threads>>>(
        staging, dev, elems);
    cudaFree(staging);
    if (cudaGetLastError() != cudaSuccess ||
        cudaDeviceSynchronize() != cudaSuccess) {
        cudaFree(dev);
        return nullptr;
    }
    g_bf16_weights.emplace(host, dev);
    return dev;
}

}  // namespace

extern "C" {

// Kernel presence + device probe; cuda_bridge.cpp calls this only when the
// kernels object was linked in (XINGCHENG_CUDA_KERNELS).
int xcuda_bf16_kernel_probe() {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess) return 0;
    return count > 0 ? 1 : 0;
}

int xcuda_bf16_release_weights() {
    std::lock_guard<std::mutex> lk(g_mu);
    for (auto& kv : g_bf16_weights) cudaFree(kv.second);
    g_bf16_weights.clear();
    return 0;
}

int xcuda_matmul_bf16(
    const double* a, long long m, long long k,
    const double* b, long long n, double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;

    const long long a_elems = m * k;
    const long long b_elems = k * n;
    const long long c_elems = m * n;
    int rc = 3;

    double* a_stage = nullptr;
    __nv_bfloat16* da = nullptr;
    float* dc = nullptr;
    float* c_host = nullptr;

    {
        std::lock_guard<std::mutex> lk(g_mu);
        __nv_bfloat16* db = device_weight_bf16(b, b_elems);
        if (db == nullptr) return 3;
        if (cudaMalloc(&a_stage, a_elems * sizeof(double)) != cudaSuccess)
            goto done;
        if (cudaMalloc(&da, a_elems * sizeof(__nv_bfloat16)) != cudaSuccess)
            goto done;
        if (cudaMalloc(&dc, c_elems * sizeof(float)) != cudaSuccess)
            goto done;
        c_host = static_cast<float*>(malloc(c_elems * sizeof(float)));
        if (c_host == nullptr) goto done;
        if (cudaMemcpy(a_stage, a, a_elems * sizeof(double),
                       cudaMemcpyHostToDevice) != cudaSuccess)
            goto done;
        f64_to_bf16_kernel<<<
            static_cast<unsigned int>((a_elems + 255) / 256), 256>>>(
            a_stage, da, a_elems);
        if (cudaGetLastError() != cudaSuccess) goto done;
        {
            dim3 threads(kTile, kTile);
            dim3 blocks(
                static_cast<unsigned int>((n + kTile - 1) / kTile),
                static_cast<unsigned int>((m + kTile - 1) / kTile));
            gemm_bf16_kernel<<<blocks, threads>>>(
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
