// CUDA bridge for the Xingcheng C++ inference engine (independent
// acceleration track — not part of the four-language layering; the pure-C
// core stays untouched). v1: cuBLAS Dgemm per call with per-call H2D/D2H
// copies; device-resident weights are a follow-up increment.
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

extern "C" {

int xcuda_available() {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess) return 0;
    return count > 0 ? 1 : 0;
}

// Row-major C[m,n] = A[m,k] * B[k,n]. cuBLAS is column-major, so compute
// C^T = B^T * A^T with the standard row/col-major swap.
int xcuda_matmul_f64(
    const double* a, long long m, long long k,
    const double* b, long long n,
    double* out) {
    if (!a || !b || !out || m <= 0 || k <= 0 || n <= 0) return 2;

    const size_t a_bytes = static_cast<size_t>(m) * k * sizeof(double);
    const size_t b_bytes = static_cast<size_t>(k) * n * sizeof(double);
    const size_t c_bytes = static_cast<size_t>(m) * n * sizeof(double);

    double *da = nullptr, *db = nullptr, *dc = nullptr;
    cublasHandle_t handle = nullptr;
    int rc = 3;

    if (cudaMalloc(&da, a_bytes) != cudaSuccess) goto done;
    if (cudaMalloc(&db, b_bytes) != cudaSuccess) goto done;
    if (cudaMalloc(&dc, c_bytes) != cudaSuccess) goto done;
    if (cudaMemcpy(da, a, a_bytes, cudaMemcpyHostToDevice) != cudaSuccess)
        goto done;
    if (cudaMemcpy(db, b, b_bytes, cudaMemcpyHostToDevice) != cudaSuccess)
        goto done;
    if (cublasCreate(&handle) != CUBLAS_STATUS_SUCCESS) goto done;
    {
        const double alpha = 1.0;
        const double beta = 0.0;
        if (cublasDgemm(
                handle, CUBLAS_OP_N, CUBLAS_OP_N,
                static_cast<int>(n), static_cast<int>(m), static_cast<int>(k),
                &alpha, db, static_cast<int>(n),
                da, static_cast<int>(k), &beta,
                dc, static_cast<int>(n)) != CUBLAS_STATUS_SUCCESS) {
            goto done;
        }
    }
    if (cudaMemcpy(out, dc, c_bytes, cudaMemcpyDeviceToHost) != cudaSuccess)
        goto done;
    rc = 0;

done:
    if (handle) cublasDestroy(handle);
    if (da) cudaFree(da);
    if (db) cudaFree(db);
    if (dc) cudaFree(dc);
    return rc;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
