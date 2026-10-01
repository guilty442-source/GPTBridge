// NVRTC + Driver-API CUDA kernels for the Xingcheng C++ inference engine.
//
// This translation unit replaces the retired kernels/*.cu nvcc lane: nvcc
// requires a host compiler within its supported MSVC range, which the
// build machine's toolchain no longer satisfies. Instead, device code is
// embedded here as NVRTC source, compiled to PTX at first use for the
// detected compute capability, then loaded and launched through the CUDA
// Driver API. The kernel bodies are a 1:1 port of the retired .cu sources
// and keep their numerics and contracts.
//
// Every CUDA dependency is resolved at run time — cuda.dll (always present
// with the NVIDIA driver) and nvrtc64_120_0.dll (CUDA toolkit) are loaded
// via LoadLibrary. Nothing is import-linked, so the binary stays portable:
// on a machine without driver/toolkit the probe entries return 0 and the
// governed request paths fail closed exactly as an unlinked kernels TU
// did before. The include directory for NVRTC's device headers
// (cuda_bf16.h / cuda_fp8.h) is resolved relative to the loaded nvrtc
// module so headers always match the compiler that consumes them.
//
// Governance: unchanged — the engine only reaches these entries after the
// governed layer sets XINGCHENG_CPP_CUDA{_BF16,_FP8,_KV}; capability lives
// here, the decision stays above, and failures never silently fall back.

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
// through the same UnifiedCudaMemoryManager as the cuBLAS bridge.
// Driver kernels launch on stream 0 inside the primary context, which
// the runtime-API pool pointers are also valid in, so no second
// allocator or context is ever created.
#include "cuda_memplane.h"
namespace mp = xcm_memplane;

namespace {

// ---------------------------------------------------------------- types --

typedef int CUresult_t;
typedef int CUdevice_t;
typedef void* CUcontext_t;
typedef void* CUmodule_t;
typedef void* CUfunction_t;
typedef unsigned long long CUdevptr_t;
typedef unsigned long long CUstream_t;
typedef void* CUevent_t;
typedef void* CUgraph_t;
typedef void* CUgraphExec_t;
typedef void* nvrtcProgram_t;
typedef int nvrtcResult_t;

enum : int {
    kCudaSuccess = 0,
    kCudaDevAttrCCMajor = 75,
    kCudaDevAttrCCMinor = 76,
    kNvrtcSuccess = 0,
};

// -------------------------------------------------- dynamic API binding --

struct DriverApi {
    HMODULE dll = nullptr;
    CUresult_t (*init)(unsigned int) = nullptr;
    CUresult_t (*device_get_count)(int*) = nullptr;
    CUresult_t (*device_get)(CUdevice_t*, int) = nullptr;
    CUresult_t (*device_get_attribute)(int*, int, CUdevice_t) = nullptr;
    CUresult_t (*primary_ctx_retain)(CUcontext_t*, CUdevice_t) = nullptr;
    CUresult_t (*ctx_set_current)(CUcontext_t) = nullptr;
    CUresult_t (*ctx_sync)() = nullptr;
    CUresult_t (*mem_alloc)(CUdevptr_t*, size_t) = nullptr;
    CUresult_t (*mem_free)(CUdevptr_t) = nullptr;
    CUresult_t (*memcpy_htod)(CUdevptr_t, const void*, size_t) = nullptr;
    CUresult_t (*memcpy_dtoh)(void*, CUdevptr_t, size_t) = nullptr;
    CUresult_t (*module_load_data)(CUmodule_t*, const void*) = nullptr;
    CUresult_t (*module_unload)(CUmodule_t) = nullptr;
    CUresult_t (*module_get_function)(CUfunction_t*, CUmodule_t,
                                      const char*) = nullptr;
    CUresult_t (*launch_kernel)(CUfunction_t, unsigned int, unsigned int,
                                unsigned int, unsigned int, unsigned int,
                                unsigned int, unsigned int, CUstream_t,
                                void**, void**) = nullptr;
    CUresult_t (*mem_get_info)(size_t*, size_t*) = nullptr;
    // §37 graph/stream plane — optional: absent symbols only mean
    // graph_ready()==0, never a failure of the base CUDA lane.
    CUresult_t (*stream_sync)(CUstream_t) = nullptr;
    CUresult_t (*memcpy_htod_async)(CUdevptr_t, const void*, size_t,
                                    CUstream_t) = nullptr;
    CUresult_t (*memcpy_dtoh_async)(void*, CUdevptr_t, size_t,
                                    CUstream_t) = nullptr;
    CUresult_t (*stream_begin_capture)(CUstream_t, int) = nullptr;
    CUresult_t (*stream_end_capture)(CUstream_t, CUgraph_t*) = nullptr;
    CUresult_t (*graph_instantiate)(CUgraphExec_t*, CUgraph_t,
                                    unsigned long long) = nullptr;
    CUresult_t (*graph_launch)(CUgraphExec_t, CUstream_t) = nullptr;
    CUresult_t (*graph_exec_destroy)(CUgraphExec_t) = nullptr;
    CUresult_t (*graph_destroy)(CUgraph_t) = nullptr;
    // §26 batch-pipeline plane — optional like the graph set: missing
    // event symbols only drop the pipelined wave path back to the
    // serial sync path, never the AdamW lane itself.
    CUresult_t (*event_create)(CUevent_t*, unsigned int) = nullptr;
    CUresult_t (*event_record)(CUevent_t, CUstream_t) = nullptr;
    CUresult_t (*stream_wait_event)(CUstream_t, CUevent_t,
                                    unsigned int) = nullptr;
    CUresult_t (*event_destroy)(CUevent_t) = nullptr;
    CUresult_t (*event_query)(CUevent_t) = nullptr;
};

struct NvrtcApi {
    HMODULE dll = nullptr;
    nvrtcResult_t (*create_program)(nvrtcProgram_t*, const char*,
                                    const char*, int, const char* const*,
                                    const char* const*) = nullptr;
    nvrtcResult_t (*destroy_program)(nvrtcProgram_t*) = nullptr;
    nvrtcResult_t (*compile_program)(nvrtcProgram_t, int,
                                     const char* const*) = nullptr;
    nvrtcResult_t (*get_ptx_size)(nvrtcProgram_t, size_t*) = nullptr;
    nvrtcResult_t (*get_ptx)(nvrtcProgram_t, char*) = nullptr;
    nvrtcResult_t (*get_log_size)(nvrtcProgram_t, size_t*) = nullptr;
    nvrtcResult_t (*get_log)(nvrtcProgram_t, char*) = nullptr;
};

DriverApi g_drv;
NvrtcApi g_rtc;
bool g_api_tried = false;
bool g_api_ok = false;

template <typename T>
bool resolve(HMODULE dll, T* out, const char* v2, const char* v1) {
    FARPROC p = GetProcAddress(dll, v2);
    if (p == nullptr && v1 != nullptr) p = GetProcAddress(dll, v1);
    if (p == nullptr) return false;
    *out = reinterpret_cast<T>(p);
    return true;
}

// Try each candidate module name; returns the first that loads.
HMODULE load_module(const std::vector<std::string>& names) {
    for (const std::string& n : names) {
        HMODULE m = LoadLibraryA(n.c_str());
        if (m != nullptr) return m;
    }
    return nullptr;
}

std::string join_path(const std::string& a, const std::string& b) {
    if (a.empty()) return b;
    return a + "\\" + b;
}

// Directory containing the loaded nvrtc dll, with "\bin" swapped for
// "\include" so NVRTC sees the headers shipped with its own toolkit.
std::string nvrtc_include_dir() {
    char path[MAX_PATH] = {};
    if (g_rtc.dll != nullptr &&
        GetModuleFileNameA(g_rtc.dll, path, MAX_PATH) > 0) {
        std::string p(path);
        const size_t pos = p.find_last_of("\\/");
        if (pos != std::string::npos) {
            std::string dir = p.substr(0, pos);          // ...\bin
            const size_t ppos = dir.find_last_of("\\/"); // toolkit root
            if (ppos != std::string::npos) {
                return dir.substr(0, ppos) + "\\include";
            }
        }
    }
    const char* cp = getenv("CUDA_PATH");
    if (cp != nullptr && *cp != '\0') return join_path(cp, "include");
    return "";
}

bool api_init() {
    if (g_api_tried) return g_api_ok;
    g_api_tried = true;
#define XCK_DBG(x) do { fprintf(stderr, "[xck] %s\n", x); } while (0)

    // The Driver API ships in the driver package as nvcuda.dll; cuda.dll
    // is an optional alias that is not present on every install.
    g_drv.dll = load_module({"nvcuda.dll", "cuda.dll"});
    if (g_drv.dll == nullptr) {
        XCK_DBG("driver dll load fail");
        return false;
    }
    bool ok = true;
    ok &= resolve(g_drv.dll, &g_drv.init, "cuInit", nullptr);
    ok &= resolve(g_drv.dll, &g_drv.device_get_count, "cuDeviceGetCount",
                  nullptr);
    ok &= resolve(g_drv.dll, &g_drv.device_get, "cuDeviceGet", nullptr);
    ok &= resolve(g_drv.dll, &g_drv.device_get_attribute,
                  "cuDeviceGetAttribute", nullptr);
    ok &= resolve(g_drv.dll, &g_drv.primary_ctx_retain,
                  "cuDevicePrimaryCtxRetain", nullptr);
    ok &= resolve(g_drv.dll, &g_drv.ctx_set_current, "cuCtxSetCurrent",
                  nullptr);
    ok &= resolve(g_drv.dll, &g_drv.ctx_sync, "cuCtxSynchronize", nullptr);
    ok &= resolve(g_drv.dll, &g_drv.mem_alloc, "cuMemAlloc_v2",
                  "cuMemAlloc");
    ok &= resolve(g_drv.dll, &g_drv.mem_free, "cuMemFree_v2", "cuMemFree");
    ok &= resolve(g_drv.dll, &g_drv.memcpy_htod, "cuMemcpyHtoD_v2",
                  "cuMemcpyHtoD");
    ok &= resolve(g_drv.dll, &g_drv.memcpy_dtoh, "cuMemcpyDtoH_v2",
                  "cuMemcpyDtoH");
    ok &= resolve(g_drv.dll, &g_drv.module_load_data, "cuModuleLoadData",
                  nullptr);
    ok &= resolve(g_drv.dll, &g_drv.module_unload, "cuModuleUnload",
                  nullptr);
    ok &= resolve(g_drv.dll, &g_drv.module_get_function,
                  "cuModuleGetFunction", nullptr);
    ok &= resolve(g_drv.dll, &g_drv.launch_kernel, "cuLaunchKernel",
                  nullptr);
    ok &= resolve(g_drv.dll, &g_drv.mem_get_info, "cuMemGetInfo_v2",
                  "cuMemGetInfo");
    if (!ok) { XCK_DBG("driver resolve fail"); return false; }
    // §37 graph capture is optional evidence, not an admission gate:
    // missing symbols leave graph_api_ready()==false and callers keep
    // the synchronous lane.
    resolve(g_drv.dll, &g_drv.stream_sync, "cuStreamSynchronize",
            nullptr);
    resolve(g_drv.dll, &g_drv.memcpy_htod_async, "cuMemcpyHtoDAsync_v2",
            "cuMemcpyHtoDAsync");
    resolve(g_drv.dll, &g_drv.memcpy_dtoh_async, "cuMemcpyDtoHAsync_v2",
            "cuMemcpyDtoHAsync");
    resolve(g_drv.dll, &g_drv.stream_begin_capture,
            "cuStreamBeginCapture_v2", "cuStreamBeginCapture");
    resolve(g_drv.dll, &g_drv.stream_end_capture, "cuStreamEndCapture",
            nullptr);
    resolve(g_drv.dll, &g_drv.graph_instantiate,
            "cuGraphInstantiateWithFlags", "cuGraphInstantiate_v2");
    resolve(g_drv.dll, &g_drv.graph_launch, "cuGraphLaunch", nullptr);
    resolve(g_drv.dll, &g_drv.graph_exec_destroy, "cuGraphExecDestroy_v2",
            "cuGraphExecDestroy");
    resolve(g_drv.dll, &g_drv.graph_destroy, "cuGraphDestroy", nullptr);
    // Optional event plane for the §26 batch pipeline — same contract:
    // unresolved symbols leave pipeline_api_ready()==false.
    resolve(g_drv.dll, &g_drv.event_create, "cuEventCreate", nullptr);
    resolve(g_drv.dll, &g_drv.event_record, "cuEventRecord", nullptr);
    resolve(g_drv.dll, &g_drv.stream_wait_event, "cuStreamWaitEvent",
            nullptr);
    resolve(g_drv.dll, &g_drv.event_destroy, "cuEventDestroy_v2",
            "cuEventDestroy");
    resolve(g_drv.dll, &g_drv.event_query, "cuEventQuery", nullptr);

    const char* cp = getenv("CUDA_PATH");
    std::vector<std::string> nvrtc_names = {"nvrtc64_120_0.dll"};
    if (cp != nullptr && *cp != '\0') {
        nvrtc_names.push_back(join_path(join_path(cp, "bin"),
                                        "nvrtc64_120_0.dll"));
    }
    nvrtc_names.push_back(
        "C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\v12.0\\"
        "bin\\nvrtc64_120_0.dll");
    g_rtc.dll = load_module(nvrtc_names);
    if (g_rtc.dll == nullptr) { XCK_DBG("nvrtc dll load fail"); return false; }
    ok = true;
    ok &= resolve(g_rtc.dll, &g_rtc.create_program, "nvrtcCreateProgram",
                  nullptr);
    ok &= resolve(g_rtc.dll, &g_rtc.destroy_program, "nvrtcDestroyProgram",
                  nullptr);
    ok &= resolve(g_rtc.dll, &g_rtc.compile_program, "nvrtcCompileProgram",
                  nullptr);
    ok &= resolve(g_rtc.dll, &g_rtc.get_ptx_size, "nvrtcGetPTXSize",
                  nullptr);
    ok &= resolve(g_rtc.dll, &g_rtc.get_ptx, "nvrtcGetPTX", nullptr);
    ok &= resolve(g_rtc.dll, &g_rtc.get_log_size, "nvrtcGetProgramLogSize",
                  nullptr);
    ok &= resolve(g_rtc.dll, &g_rtc.get_log, "nvrtcGetProgramLog", nullptr);
    if (!ok) { XCK_DBG("nvrtc resolve fail"); return false; }

    g_api_ok = true;
    return true;
}

// --------------------------------------------------------- device probe --

CUcontext_t g_ctx = nullptr;
int g_cc_major = 0;
int g_cc_minor = 0;
bool g_dev_tried = false;
bool g_dev_ok = false;

// Load APIs, initialise the driver, retain the primary context of device 0
// and record its compute capability for the NVRTC target flag.
bool device_ready() {
    if (g_dev_tried) return g_dev_ok;
    g_dev_tried = true;
    if (!api_init()) { XCK_DBG("api_init fail"); return false; }
    if (g_drv.init(0) != kCudaSuccess) { XCK_DBG("cuInit fail"); return false; }
    int count = 0;
    if (g_drv.device_get_count(&count) != kCudaSuccess || count <= 0) {
        XCK_DBG("device count fail"); return false;
    }
    CUdevice_t dev = 0;
    if (g_drv.device_get(&dev, 0) != kCudaSuccess) { XCK_DBG("device_get fail"); return false; }
    if (g_drv.device_get_attribute(
            &g_cc_major, kCudaDevAttrCCMajor, dev) != kCudaSuccess ||
        g_drv.device_get_attribute(
            &g_cc_minor, kCudaDevAttrCCMinor, dev) != kCudaSuccess) {
        XCK_DBG("cc attr fail"); return false;
    }
    if (g_drv.primary_ctx_retain(&g_ctx, dev) != kCudaSuccess ||
        g_ctx == nullptr) {
        XCK_DBG("ctx retain fail"); return false;
    }
    g_dev_ok = true;
    return true;
}

// Every entry point re-anchors the primary context so calls from any
// engine thread keep working.
bool use_ctx() {
    return device_ready() &&
           g_drv.ctx_set_current(g_ctx) == kCudaSuccess;
}

// ------------------------------------------------------ kernel sources --

// 1:1 port of the retired kernels/*.cu device code. extern "C" on every
// __global__ keeps names unmangled for cuModuleGetFunction — NVRTC lowered
// -name lookup is unnecessary.
const char* kKernelSource = R"XCSRC(
#include <cuda_bf16.h>
#include <cuda_fp8.h>

#define XC_TILE 16

extern "C" __global__ void xc_f64_to_bf16(
    const double* in, __nv_bfloat16* out, long long n) {
    const long long i =
        (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = __double2bfloat16(in[i]);
}

extern "C" __global__ void xc_gemm_bf16(
    const __nv_bfloat16* a, const __nv_bfloat16* b, float* c,
    int m, int k, int n) {
    // fp32 shared tiles: each tile element is converted exactly once at
    // load instead of once per consuming FMA (XC_TILE× per element).
    // Identical values and FMA order — bit-identical to the bf16-shared
    // version, minus the per-read conversion cost.
    __shared__ float as[XC_TILE][XC_TILE];
    __shared__ float bs[XC_TILE][XC_TILE];
    const int row = blockIdx.y * XC_TILE + threadIdx.y;
    const int col = blockIdx.x * XC_TILE + threadIdx.x;
    float acc = 0.0f;
    for (int t = 0; t < (k + XC_TILE - 1) / XC_TILE; ++t) {
        const int a_col = t * XC_TILE + threadIdx.x;
        const int b_row = t * XC_TILE + threadIdx.y;
        as[threadIdx.y][threadIdx.x] =
            (row < m && a_col < k)
                ? __bfloat162float(a[(long long)row * k + a_col])
                : 0.0f;
        bs[threadIdx.y][threadIdx.x] =
            (b_row < k && col < n)
                ? __bfloat162float(b[(long long)b_row * n + col])
                : 0.0f;
        __syncthreads();
#pragma unroll
        for (int i = 0; i < XC_TILE; ++i) {
            acc += as[threadIdx.y][i] * bs[i][threadIdx.x];
        }
        __syncthreads();
    }
    if (row < m && col < n) c[(long long)row * n + col] = acc;
}

// Skinny-m bf16 GEMV: one thread per output column with all m rows fused,
// so each b element is read once and feeds every accumulator — decode
// (m=1) becomes bandwidth-bound instead of tile-bound. Per-element
// accumulation order is the same sequential k-order as xc_gemm_bf16.
#define XC_GEMV_MAX_M 16

extern "C" __global__ void xc_gemv_bf16_part(
    const __nv_bfloat16* a, const __nv_bfloat16* b, float* part,
    int m, int k, int n, int ksplit, int kchunk) {
    const long long col =
        (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (col >= n) return;
    const int i0 = blockIdx.y * kchunk;
    const int i1 = min(k, i0 + kchunk);
    float acc[XC_GEMV_MAX_M];
#pragma unroll
    for (int r = 0; r < XC_GEMV_MAX_M; ++r) acc[r] = 0.0f;
    for (int i = i0; i < i1; ++i) {
        const float bv = __bfloat162float(b[(long long)i * n + col]);
        // Constant-bound unrolled loop keeps acc[] in registers — a
        // runtime-bound loop (r < m) forces the array to local memory.
        // Dead lanes contribute av=0, so the math is identical.
#pragma unroll
        for (int r = 0; r < XC_GEMV_MAX_M; ++r) {
            const float av = (r < m)
                ? __bfloat162float(a[(long long)r * k + i]) : 0.0f;
            acc[r] += av * bv;
        }
    }
    for (int r = 0; r < m; ++r)
        part[((long long)blockIdx.y * m + r) * n + col] = acc[r];
}

// Split-k reduce (shared by the bf16 and fp8 GEMV lanes): c[r][col] =
// sum over slices in fixed order — deterministic across runs.
extern "C" __global__ void xc_gemv_reduce(
    const float* part, float* c, int m, int n, int ksplit) {
    const long long col =
        (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (col >= n) return;
    for (int r = 0; r < m; ++r) {
        float acc = 0.0f;
        for (int s = 0; s < ksplit; ++s)
            acc += part[((long long)s * m + r) * n + col];
        c[(long long)r * n + col] = acc;
    }
}

extern "C" __global__ void xc_f64_to_fp32(
    const double* in, float* out, long long n) {
    const long long i =
        (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = (float)in[i];
}

extern "C" __global__ void xc_f64_to_fp8(
    const double* in, __nv_fp8_e4m3* out, long long n) {
    const long long i =
        (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = __nv_fp8_e4m3(in[i]);
}

extern "C" __global__ void xc_gemm_fp8(
    const float* a, const __nv_fp8_e4m3* b, float* c,
    int m, int k, int n) {
    // Same shared-tile conversion hoist as xc_gemm_bf16: bs lands in
    // shared as fp32 so the inner loop does zero per-FMA conversions.
    __shared__ float as[XC_TILE][XC_TILE];
    __shared__ float bs[XC_TILE][XC_TILE];
    const int row = blockIdx.y * XC_TILE + threadIdx.y;
    const int col = blockIdx.x * XC_TILE + threadIdx.x;
    float acc = 0.0f;
    for (int t = 0; t < (k + XC_TILE - 1) / XC_TILE; ++t) {
        const int a_col = t * XC_TILE + threadIdx.x;
        const int b_row = t * XC_TILE + threadIdx.y;
        as[threadIdx.y][threadIdx.x] =
            (row < m && a_col < k)
                ? a[(long long)row * k + a_col] : 0.0f;
        bs[threadIdx.y][threadIdx.x] =
            (b_row < k && col < n)
                ? (float)(b[(long long)b_row * n + col]) : 0.0f;
        __syncthreads();
#pragma unroll
        for (int i = 0; i < XC_TILE; ++i) {
            acc += as[threadIdx.y][i] * bs[i][threadIdx.x];
        }
        __syncthreads();
    }
    if (row < m && col < n) c[(long long)row * n + col] = acc;
}

// Skinny-m split-k fp8 GEMV (pass 1) — same fused-row pattern as
// xc_gemv_bf16_part, with the shared xc_gemv_reduce as pass 2.
extern "C" __global__ void xc_gemv_fp8_part(
    const float* a, const __nv_fp8_e4m3* b, float* part,
    int m, int k, int n, int ksplit, int kchunk) {
    const long long col =
        (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (col >= n) return;
    const int i0 = blockIdx.y * kchunk;
    const int i1 = min(k, i0 + kchunk);
    float acc[XC_GEMV_MAX_M];
#pragma unroll
    for (int r = 0; r < XC_GEMV_MAX_M; ++r) acc[r] = 0.0f;
    for (int i = i0; i < i1; ++i) {
        const float bv = (float)(b[(long long)i * n + col]);
#pragma unroll
        for (int r = 0; r < XC_GEMV_MAX_M; ++r) {
            const float av = (r < m) ? a[(long long)r * k + i] : 0.0f;
            acc[r] += av * bv;
        }
    }
    for (int r = 0; r < m; ++r)
        part[((long long)blockIdx.y * m + r) * n + col] = acc[r];
}

// Online-softmax attention over the device-resident KV cache — fp64,
// identical tile/rescale semantics to the host path in engine.cpp.
// Shared layout: scores[128] | acc[head_dim] | red[32] | scal[5].
#define XC_KV_TILE 128
#define XC_KV_THREADS 128

extern "C" __global__ void xc_kv_attention(
    const double* __restrict__ q,
    const double* __restrict__ kbuf,
    const double* __restrict__ vbuf,
    long long heads, long long seq, long long kv_heads,
    long long head_dim, long long max_len, long long position_offset,
    double* __restrict__ out, long long out_stride) {
    const long long s = blockIdx.x % seq;
    const long long h = blockIdx.x / seq;
    const long long kv_head = h / (heads / kv_heads);
    const long long last = position_offset + s;
    const double* q_row = q + (h * seq + s) * head_dim;
    const double* kbase = kbuf + kv_head * max_len * head_dim;
    const double* vbase = vbuf + kv_head * max_len * head_dim;
    const double scale = 1.0 / sqrt((double)head_dim);
    const double kNegInf =
        -__longlong_as_double(0x7FF0000000000000ull);

    extern __shared__ double sm[];
    double* scores = sm;
    double* acc = sm + XC_KV_TILE;
    double* red = acc + head_dim;
    double* scal = red + 32;

    const int tid = threadIdx.x;
    const int warps = (blockDim.x + 31) / 32;
    if (tid == 0) {
        scal[0] = kNegInf;
        scal[1] = 0.0;
    }
    for (long long d = tid; d < head_dim; d += blockDim.x) acc[d] = 0.0;
    __syncthreads();

    for (long long t0 = 0; t0 <= last; t0 += XC_KV_TILE) {
        const long long rem = last - t0 + 1;
        const long long tn = rem < XC_KV_TILE ? rem : XC_KV_TILE;

        for (long long j = tid; j < tn; j += blockDim.x) {
            const double* krow = kbase + (t0 + j) * head_dim;
            double dot = 0.0;
            for (long long d = 0; d < head_dim; ++d)
                dot += q_row[d] * krow[d];
            scores[j] = dot * scale;
        }
        __syncthreads();

        double tmax = kNegInf;
        for (long long j = tid; j < tn; j += blockDim.x)
            tmax = fmax(tmax, scores[j]);
        for (int off = 16; off > 0; off >>= 1)
            tmax = fmax(tmax, __shfl_down_sync(0xffffffffu, tmax, off));
        if ((tid & 31) == 0) red[tid >> 5] = tmax;
        __syncthreads();
        if (tid == 0) {
            double m_tile = red[0];
            for (int w = 1; w < warps; ++w) m_tile = fmax(m_tile, red[w]);
            const double m_new = fmax(scal[0], m_tile);
            scal[2] = exp(scal[0] - m_new);
            scal[3] = m_new;
        }
        __syncthreads();
        const double r = scal[2];
        const double m_new = scal[3];

        for (long long j = tid; j < tn; j += blockDim.x)
            scores[j] = exp(scores[j] - m_new);
        __syncthreads();
        double tsum = 0.0;
        for (long long j = tid; j < tn; j += blockDim.x)
            tsum += scores[j];
        for (int off = 16; off > 0; off >>= 1)
            tsum += __shfl_down_sync(0xffffffffu, tsum, off);
        if ((tid & 31) == 0) red[tid >> 5] = tsum;
        __syncthreads();
        if (tid == 0) {
            double l_tile = red[0];
            for (int w = 1; w < warps; ++w) l_tile += red[w];
            scal[1] = scal[1] * r + l_tile;
            scal[0] = m_new;
        }

        for (long long d = tid; d < head_dim; d += blockDim.x) {
            double a = acc[d] * r;
            for (long long j = 0; j < tn; ++j)
                a += scores[j] * vbase[(t0 + j) * head_dim + d];
            acc[d] = a;
        }
        __syncthreads();
    }

    if (tid == 0) scal[4] = 1.0 / scal[1];
    __syncthreads();
    const double inv_l = scal[4];
    double* orow = out + s * out_stride + h * head_dim;
    for (long long d = tid; d < head_dim; d += blockDim.x)
        orow[d] = acc[d] * inv_l;
}

// --------------------------------------------------- training plane ----
// NativeCudaTrainingPlane §26 NativeCudaFusedAdamW — one fused pass per
// element: gradient scale -> bias-corrected moments -> decoupled weight
// decay -> parameter update. Semantics mirror the trainer's scalar
// adamw_step exactly (fp32 state, bc1/bc2 computed host-side so the
// bias-correction matches std::pow to the bit).

extern "C" __global__ void xc_adamw_fused(
    const float* g, float* m, float* v, float* w,
    float gscale, float lr_t, float wd,
    float b1, float b2, float bc1, float bc2, float eps,
    long long n) {
    const long long stride = (long long)gridDim.x * blockDim.x;
    for (long long i = (long long)blockIdx.x * blockDim.x + threadIdx.x;
         i < n; i += stride) {
        const float gi = g[i] * gscale;
        const float mi = b1 * m[i] + (1.0f - b1) * gi;
        const float vi = b2 * v[i] + (1.0f - b2) * gi * gi;
        m[i] = mi;
        v[i] = vi;
        const float mh = mi / bc1;
        const float vh = vi / bc2;
        w[i] -= lr_t * (mh / (sqrtf(vh) + eps) + wd * w[i]);
    }
}

// Global gradient-norm front half (§26 clip fused into the same pass
// family): block partial sums of x*x; the host reduces partials in fp64
// — same accumulation precision class as the trainer's scalar loop.
extern "C" __global__ void xc_sqsum_part(
    const float* x, float* part, long long n) {
    __shared__ float red[256];
    const long long stride = (long long)gridDim.x * blockDim.x;
    float acc = 0.0f;
    for (long long i = (long long)blockIdx.x * blockDim.x + threadIdx.x;
         i < n; i += stride)
        acc += x[i] * x[i];
    for (int off = 16; off > 0; off >>= 1)
        acc += __shfl_down_sync(0xffffffffu, acc, off);
    if ((threadIdx.x & 31) == 0) red[threadIdx.x >> 5] = acc;
    __syncthreads();
    if (threadIdx.x == 0) {
        const int warps = (blockDim.x + 31) / 32;
        float s = 0.0f;
        for (int w2 = 0; w2 < warps; ++w2) s += red[w2];
        part[blockIdx.x] = s;
    }
}
)XCSRC";

// ------------------------------------------------- module compile/cache --

CUmodule_t g_module = nullptr;
CUfunction_t g_f_conv_bf16 = nullptr;
CUfunction_t g_f_gemm_bf16 = nullptr;
CUfunction_t g_f_gemv_bf16 = nullptr;
CUfunction_t g_f_gemv_reduce = nullptr;
CUfunction_t g_f_conv_fp32 = nullptr;
CUfunction_t g_f_conv_fp8 = nullptr;
CUfunction_t g_f_gemm_fp8 = nullptr;
CUfunction_t g_f_gemv_fp8 = nullptr;
CUfunction_t g_f_kv_attn = nullptr;
CUfunction_t g_f_adamw = nullptr;
CUfunction_t g_f_sqsum = nullptr;
std::mutex g_module_mu;
bool g_module_tried = false;

bool get_func(CUfunction_t* out, const char* name) {
    return g_drv.module_get_function(out, g_module, name) == kCudaSuccess &&
           *out != nullptr;
}

// Compile the embedded source for the detected device once; any failure
// latches the kernels TU as unavailable (fail-closed, no retries).
bool ensure_module() {
    if (g_module_tried) return g_module != nullptr;
    std::lock_guard<std::mutex> lk(g_module_mu);
    if (g_module_tried) return g_module != nullptr;
    g_module_tried = true;
    if (!use_ctx()) { XCK_DBG("use_ctx fail"); return false; }

    nvrtcProgram_t prog = nullptr;
    if (g_rtc.create_program(&prog, kKernelSource, "xc_kernels.cu", 0,
                             nullptr, nullptr) != kNvrtcSuccess ||
        prog == nullptr) {
        XCK_DBG("create_program fail");
        return false;
    }
    char arch[48];
    snprintf(arch, sizeof(arch), "--gpu-architecture=compute_%d%d",
             g_cc_major, g_cc_minor);
    const std::string inc = nvrtc_include_dir();
    std::string inc_flag = inc.empty() ? std::string() : "-I" + inc;
    const char* opts[3];
    int nopts = 0;
    opts[nopts++] = arch;
    opts[nopts++] = "--std=c++17";
    if (!inc_flag.empty()) opts[nopts++] = inc_flag.c_str();

    const nvrtcResult_t crc = g_rtc.compile_program(prog, nopts, opts);
    size_t lsz = 0;
    g_rtc.get_log_size(prog, &lsz);
    if (lsz > 1) {
        std::vector<char> log(lsz);
        g_rtc.get_log(prog, log.data());
        fprintf(stderr, "[xcuda] nvrtc log: %s\n", log.data());
    }
    if (crc != kNvrtcSuccess) {
        fprintf(stderr, "[xcuda] nvrtc compile failed rc=%d\n", (int)crc);
        g_rtc.destroy_program(&prog);
        return false;
    }
    size_t psize = 0;
    if (g_rtc.get_ptx_size(prog, &psize) != kNvrtcSuccess || psize == 0) {
        g_rtc.destroy_program(&prog);
        return false;
    }
    std::vector<char> ptx(psize);
    if (g_rtc.get_ptx(prog, ptx.data()) != kNvrtcSuccess) {
        g_rtc.destroy_program(&prog);
        return false;
    }
    g_rtc.destroy_program(&prog);

    if (g_drv.module_load_data(&g_module, ptx.data()) != kCudaSuccess) {
        XCK_DBG("module_load fail");
        return false;
    }
    bool ok = true;
    ok &= get_func(&g_f_conv_bf16, "xc_f64_to_bf16");
    ok &= get_func(&g_f_gemm_bf16, "xc_gemm_bf16");
    ok &= get_func(&g_f_gemv_bf16, "xc_gemv_bf16_part");
    ok &= get_func(&g_f_gemv_reduce, "xc_gemv_reduce");
    ok &= get_func(&g_f_conv_fp32, "xc_f64_to_fp32");
    ok &= get_func(&g_f_conv_fp8, "xc_f64_to_fp8");
    ok &= get_func(&g_f_gemm_fp8, "xc_gemm_fp8");
    ok &= get_func(&g_f_gemv_fp8, "xc_gemv_fp8_part");
    ok &= get_func(&g_f_kv_attn, "xc_kv_attention");
    ok &= get_func(&g_f_adamw, "xc_adamw_fused");
    ok &= get_func(&g_f_sqsum, "xc_sqsum_part");
    if (!ok) {
        XCK_DBG("get_func fail");
        g_drv.module_unload(g_module);
        g_module = nullptr;
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
// Split-k factor: widens the launch so decode-size shapes still cover
// enough SMs to hide memory latency (fixed → deterministic reduce order).
constexpr long long kGemvKSplit = 8;

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
    return static_cast<CUstream_t>(reinterpret_cast<uintptr_t>(
        mp::mgr().stream(mp::StreamLane::DECODE_HIGH)));
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
    CUgraphExec_t exec = nullptr;
};

std::vector<Bf16Graph> g_bf16_graphs;
constexpr size_t kMaxBf16Graphs = 256;

void graph_entry_release(Bf16Graph& g) {
    if (g.exec != nullptr) g_drv.graph_exec_destroy(g.exec);
    g.exec = nullptr;
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
            g.part = dev_alloc(static_cast<size_t>(kGemvKSplit * m * n) *
                                   sizeof(float),
                               mp::Tier::KERNEL_SCRATCH);
        g.a_host = static_cast<double*>(
            mp::mgr().pinned_alloc(static_cast<int64_t>(ab)));
        g.c_host = static_cast<float*>(
            mp::mgr().pinned_alloc(static_cast<int64_t>(cb)));
        bool ok = g.a_stage != 0 && g.da != 0 && g.dc != 0 &&
                  g.a_host != nullptr && g.c_host != nullptr &&
                  (!skinny || g.part != 0);
        CUgraph_t graph = nullptr;
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
                int ksi = static_cast<int>(kGemvKSplit);
                int kci = static_cast<int>(
                    (k + kGemvKSplit - 1) / kGemvKSplit);
                void* pp[] = {&g.da, &g.db, &g.part, &mi, &ki, &ni,
                              &ksi, &kci};
                ok &= launch_s(
                    g_f_gemv_bf16,
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
                void* pp[] = {&g.da, &g.db, &g.dc, &mi, &ki, &ni};
                ok &= launch_s(
                    g_f_gemm_bf16,
                    static_cast<unsigned int>((n + 15) / 16),
                    static_cast<unsigned int>((m + 15) / 16), 16, 16, 0,
                    pp, s);
            }
            ok &= g_drv.memcpy_dtoh_async(g.c_host, g.dc, cb, s) ==
                  kCudaSuccess;
            ok &= g_drv.stream_end_capture(s, &graph) == kCudaSuccess &&
                  graph != nullptr;
        }
        if (ok) {
            ok = g_drv.graph_instantiate(&g.exec, graph,
                                         0ull) == kCudaSuccess &&
                 g.exec != nullptr;
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
            CUdevptr_t part = dev_get_pooled(
                g_bf16_part,
                static_cast<size_t>(kGemvKSplit * m * n) * sizeof(float));
            if (part == 0) goto done;
            int ksi = static_cast<int>(kGemvKSplit);
            int kci =
                static_cast<int>((k + kGemvKSplit - 1) / kGemvKSplit);
            void* pparams[] = {&da, &db, &part, &mi, &ki, &ni, &ksi, &kci};
            if (!launch(g_f_gemv_bf16,
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
            void* params[] = {&da, &db, &dc, &mi, &ki, &ni};
            if (!launch(g_f_gemm_bf16,
                        static_cast<unsigned int>((n + 15) / 16),
                        static_cast<unsigned int>((m + 15) / 16),
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
            CUdevptr_t part = dev_get_pooled(
                g_fp8_part,
                static_cast<size_t>(kGemvKSplit * m * n) * sizeof(float));
            if (part == 0) goto done;
            int ksi = static_cast<int>(kGemvKSplit);
            int kci =
                static_cast<int>((k + kGemvKSplit - 1) / kGemvKSplit);
            void* pparams[] = {&da, &db, &part, &mi, &ki, &ni, &ksi, &kci};
            if (!launch(g_f_gemv_fp8,
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
            void* params[] = {&da, &db, &dc, &mi, &ki, &ni};
            if (!launch(g_f_gemm_fp8,
                        static_cast<unsigned int>((n + 15) / 16),
                        static_cast<unsigned int>((m + 15) / 16),
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

// §26 batch-pipeline state — one call drains a whole step's tensors over
// three manager lanes (H2D / TRAIN_COMPUTE / D2H) chained by events, so a
// tensor's D2H overlaps the next tensor's H2D+kernel. Staging rides two
// bounded pinned slabs; a wave never exceeds kAdamwWaveBytes per
// direction.
constexpr size_t kAdamwWaveBytes = 16u << 20;
float* g_adamw_pin_in = nullptr;    // gradient staging (H2D source)
float* g_adamw_pin_out = nullptr;   // weight staging (D2H destination)
size_t g_adamw_pin_cap = 0;         // bytes per direction, 0 = unallocated
std::vector<CUevent_t> g_adamw_ev_h;   // per-wave-item H2D completion
std::vector<CUevent_t> g_adamw_ev_k;   // per-wave-item kernel completion

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

// Lightweight capability probe for the governed admission check —
// resolves the device + free VRAM without compiling kernels, so it is
// cheap enough to run before every governed launch decision.
int xcuda_probe(long long* free_bytes, long long* total_bytes,
                int* cc_major, int* cc_minor) {
    if (!device_ready()) return 0;
    if (free_bytes != nullptr && total_bytes != nullptr) {
        size_t fb = 0, tb = 0;
        if (g_drv.ctx_set_current(g_ctx) != kCudaSuccess ||
            g_drv.mem_get_info(&fb, &tb) != kCudaSuccess) {
            return 0;
        }
        *free_bytes = static_cast<long long>(fb);
        *total_bytes = static_cast<long long>(tb);
    }
    if (cc_major != nullptr) *cc_major = g_cc_major;
    if (cc_minor != nullptr) *cc_minor = g_cc_minor;
    return 1;
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
    const unsigned int shmem = static_cast<unsigned int>(
        (static_cast<size_t>(kKvTile) + static_cast<size_t>(head_dim) +
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
                   g_f_adamw != nullptr && g_f_sqsum != nullptr
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
    if (!use_ctx() || !ensure_module() || g_f_sqsum == nullptr) return 3;
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

// ------------------------------------------------ AdamW batch pipeline --
// §26 whole-step batch surface: the caller hands the full tensor list in
// one call so the three engine stages (H2D gradient, fused kernel, D2H
// weights) pipeline across the manager's dedicated lanes instead of
// serialising per tensor. Per-item semantics are identical to
// step_dev+sync — same kernel, same bias-correction inputs, same "device
// m/v stay authoritative, host g consumed" contract.

// One tensor of a batch step — the trainer declares the identical POD
// in its own TU (same pattern as the extern "C" entry decls).
struct XcudaAdamwItem {
    const float* g_host;   // host gradient, length = bound tensor n
    const void*  w_key;    // bound host weight pointer (map key)
    float*       w_out;    // host dst for updated w (may equal w_key)
    int          rc;       // per-item verdict — written by the batch call
};

// Pipelined-path prerequisites — optional like the graph set: any absent
// piece degrades the batch to the serial path, never to an error.
static bool adamw_pipe_ready() {
    return g_drv.event_create != nullptr &&
           g_drv.event_record != nullptr &&
           g_drv.stream_wait_event != nullptr &&
           g_drv.event_query != nullptr &&
           g_drv.memcpy_htod_async != nullptr &&
           g_drv.memcpy_dtoh_async != nullptr &&
           g_drv.stream_sync != nullptr && mp::mgr().ensure();
}

static CUstream_t adamw_lane(mp::StreamLane l) {
    return static_cast<CUstream_t>(reinterpret_cast<uintptr_t>(
        mp::mgr().stream(l)));
}

// Allocate the two staging slabs once; a refused allocation (pinned cap
// bound) disables the pipelined path for the process — the serial path
// keeps identical semantics, just without overlap.
static bool adamw_staging_ready() {
    if (g_adamw_pin_in != nullptr && g_adamw_pin_out != nullptr)
        return true;
    void* in = mp::mgr().pinned_alloc(
        static_cast<int64_t>(kAdamwWaveBytes));
    void* out = mp::mgr().pinned_alloc(
        static_cast<int64_t>(kAdamwWaveBytes));
    if (in == nullptr || out == nullptr) {
        if (in != nullptr) mp::mgr().pinned_free(in);
        if (out != nullptr) mp::mgr().pinned_free(out);
        return false;
    }
    g_adamw_pin_in = static_cast<float*>(in);
    g_adamw_pin_out = static_cast<float*>(out);
    g_adamw_pin_cap = kAdamwWaveBytes;
    return true;
}

// Grow the per-wave event pools (CU_EVENT_DISABLE_TIMING = 0x2 — they
// carry ordering only, never profiling data).
static bool adamw_ensure_events(size_t n) {
    while (g_adamw_ev_h.size() < n) {
        CUevent_t h = nullptr, k = nullptr;
        if (g_drv.event_create(&h, 2u) != kCudaSuccess || h == nullptr ||
            g_drv.event_create(&k, 2u) != kCudaSuccess || k == nullptr) {
            if (h != nullptr) g_drv.event_destroy(h);
            if (k != nullptr) g_drv.event_destroy(k);
            return false;
        }
        g_adamw_ev_h.push_back(h);
        g_adamw_ev_k.push_back(k);
    }
    return true;
}

// Serial per-tensor step — identical work to step_dev + sync(w), used by
// the batch path for oversized tensors and whenever the pipeline lanes
// are unavailable. Caller holds g_adamw_mu.
static int adamw_step_serial(AdamwState& st, const float* g_host,
                             float* w_out, float gscale, float lr_t,
                             float wd, float b1, float b2, float bc1,
                             float bc2, float eps) {
    const size_t bytes = static_cast<size_t>(st.n) * sizeof(float);
    if (xmemcpy_htod(st.dg, g_host, bytes) != kCudaSuccess) return 3;
    long long n = st.n;
    float gs = gscale, lt = lr_t, wdv = wd;
    void* params[] = {&st.dg, &st.dm, &st.dv, &st.dw,
                      &gs, &lt, &wdv, &b1, &b2, &bc1, &bc2, &eps, &n};
    const unsigned blocks = static_cast<unsigned int>(
        std::min<long long>((n + 255) / 256, 65535));
    if (!launch(g_f_adamw, blocks, 1, 256, 1, 0, params)) return 3;
    if (w_out != nullptr &&
        xmemcpy_dtoh(w_out, st.dw, bytes) != kCudaSuccess)
        return 3;
    return 0;
}

// One call consumes a whole step: per item the gradient goes H2D, the
// fused kernel updates resident w/m/v, and the updated w comes back D2H
// (host forward still reads host weights this phase). rc per item: 0
// consumed incl. read-back; 2 bad args; 3 device untouched — caller may
// recompute on host; 4 unbound key; 6 kernel ran — device state is
// authoritative, caller pulls w/m/v but must NOT recompute. Return is
// transport-level — 0 means the batch ran and per-item rc is the
// verdict; nonzero means nothing was consumed and the caller's scalar
// path still owns every tensor.
int xcuda_adamw_step_all(XcudaAdamwItem* items, long long count,
                         float gscale, float lr_t, float wd, int step) {
    if (items == nullptr || count <= 0 || step < 0) return 2;
    if (!use_ctx() || !ensure_module() || g_f_adamw == nullptr) return 3;
    // Bias corrections in fp64 host-side — bit-identical to the
    // per-tensor path's std::pow inputs. Non-const: cuLaunchKernel
    // copies the params at enqueue and needs void* addresses.
    float b1 = 0.9f, b2 = 0.999f, eps = 1e-8f;
    float bc1 = static_cast<float>(
        1.0 - std::pow(0.9, static_cast<double>(step + 1)));
    float bc2 = static_cast<float>(
        1.0 - std::pow(0.999, static_cast<double>(step + 1)));
    std::lock_guard<std::mutex> lk(g_adamw_mu);
    for (long long i = 0; i < count; ++i) {
        XcudaAdamwItem& it = items[i];
        if (it.g_host == nullptr || it.w_key == nullptr) {
            it.rc = 2;
            continue;
        }
        it.rc = g_adamw.find(it.w_key) != g_adamw.end() ? -1 : 4;
    }
    const bool pipe = adamw_pipe_ready() && adamw_staging_ready();
    if (!pipe) {
        for (long long i = 0; i < count; ++i) {
            XcudaAdamwItem& it = items[i];
            if (it.rc != -1) continue;
            it.rc = adamw_step_serial(g_adamw.find(it.w_key)->second,
                                      it.g_host, it.w_out, gscale, lr_t,
                                      wd, b1, b2, bc1, bc2, eps);
        }
        return 0;
    }
    const CUstream_t s_h2d = adamw_lane(mp::StreamLane::H2D);
    const CUstream_t s_cmp = adamw_lane(mp::StreamLane::TRAIN_COMPUTE);
    const CUstream_t s_d2h = adamw_lane(mp::StreamLane::D2H);
    if (s_h2d == 0 || s_cmp == 0 || s_d2h == 0) {
        for (long long i = 0; i < count; ++i)
            if (items[i].rc == -1)
                items[i].rc = adamw_step_serial(
                    g_adamw.find(items[i].w_key)->second, items[i].g_host,
                    items[i].w_out, gscale, lr_t, wd, b1, b2, bc1, bc2,
                    eps);
        return 0;
    }
    // Wave loop: each wave's staging footprint is bounded by the slab;
    // oversized tensors run the serial path inline without stalling the
    // pipeline.
    long long i = 0;
    while (i < count) {
        std::vector<long long> wave;
        size_t wave_bytes = 0;
        for (; i < count; ++i) {
            XcudaAdamwItem& it = items[i];
            if (it.rc != -1) continue;
            AdamwState& st = g_adamw.find(it.w_key)->second;
            const size_t bytes =
                static_cast<size_t>(st.n) * sizeof(float);
            if (bytes > g_adamw_pin_cap) {
                it.rc = adamw_step_serial(st, it.g_host, it.w_out,
                                          gscale, lr_t, wd, b1, b2,
                                          bc1, bc2, eps);
                continue;
            }
            if (wave_bytes > 0 && wave_bytes + bytes > g_adamw_pin_cap)
                break;
            wave_bytes += bytes;
            wave.push_back(i);
        }
        if (wave.empty()) continue;
        if (!adamw_ensure_events(wave.size())) {
            for (long long wi : wave) {
                XcudaAdamwItem& it = items[wi];
                it.rc = adamw_step_serial(
                    g_adamw.find(it.w_key)->second, it.g_host, it.w_out,
                    gscale, lr_t, wd, b1, b2, bc1, bc2, eps);
            }
            continue;
        }
        // Stage + enqueue: H2D on its own lane, kernel on TRAIN_COMPUTE
        // gated by the H2D event, D2H on its own lane gated by the
        // kernel event — tensor i's D2H overlaps tensor i+1's H2D.
        size_t off = 0;
        size_t slot = 0;
        for (long long wi : wave) {
            XcudaAdamwItem& it = items[wi];
            AdamwState& st = g_adamw.find(it.w_key)->second;
            const size_t bytes =
                static_cast<size_t>(st.n) * sizeof(float);
            std::memcpy(reinterpret_cast<char*>(g_adamw_pin_in) + off,
                        it.g_host, bytes);
            long long n = st.n;
            float gs = gscale, lt = lr_t, wdv = wd;
            void* params[] = {&st.dg, &st.dm, &st.dv, &st.dw, &gs, &lt,
                              &wdv, &b1, &b2, &bc1, &bc2, &eps, &n};
            const unsigned blocks = static_cast<unsigned int>(
                std::min<long long>((n + 255) / 256, 65535));
            // Ordered enqueue — a stage failure stops this item's chain
            // before later stages could wait on an unrecorded event or
            // run on a gradient that never landed. rc=3 means the device
            // was untouched (caller may recompute on host); rc=6 means
            // the kernel was enqueued — device state is authoritative
            // and the caller must pull, never recompute (a recompute
            // would double-apply the update).
            bool ok = g_drv.memcpy_htod_async(
                          st.dg,
                          reinterpret_cast<const char*>(g_adamw_pin_in) + off,
                          bytes, s_h2d) == kCudaSuccess;
            if (ok) ok = g_drv.event_record(g_adamw_ev_h[slot],
                                            s_h2d) == kCudaSuccess;
            if (ok) ok = g_drv.stream_wait_event(
                             s_cmp, g_adamw_ev_h[slot],
                             0u) == kCudaSuccess;
            if (ok) ok = launch_s(g_f_adamw, blocks, 1, 256, 1, 0,
                                  params, s_cmp);
            if (!ok) {
                it.rc = 3;
                off += bytes;
                ++slot;
                continue;
            }
            // Kernel enqueued — from here on the device owns this item's
            // update even if the read-back stages fail.
            it.rc = 6;
            if (g_drv.event_record(g_adamw_ev_k[slot], s_cmp) ==
                    kCudaSuccess &&
                g_drv.stream_wait_event(s_d2h, g_adamw_ev_k[slot], 0u) ==
                    kCudaSuccess &&
                g_drv.memcpy_dtoh_async(
                    reinterpret_cast<char*>(g_adamw_pin_out) + off,
                    st.dw, bytes, s_d2h) == kCudaSuccess)
                it.rc = -2;    // fully enqueued, awaiting wave drain
            off += bytes;
            ++slot;
        }
        // One drain per wave, then write every staged weight back to its
        // host destination. On a drain failure the kernel event
        // disambiguates: complete => device applied the update (rc=6,
        // pull-only); not complete => device untouched (rc=3,
        // pull + host recompute stays consistent because the kernel's
        // math is bitwise-identical to the scalar lane).
        if (g_drv.stream_sync(s_d2h) != kCudaSuccess) {
            size_t slot2 = 0;
            for (long long wi : wave) {
                XcudaAdamwItem& it = items[wi];
                if (it.rc == -2 &&
                    g_drv.event_query(g_adamw_ev_k[slot2]) !=
                        kCudaSuccess)
                    it.rc = 3;   // 6 stays 6 — kernel ran
                ++slot2;
            }
            continue;
        }
        off = 0;
        for (long long wi : wave) {
            XcudaAdamwItem& it = items[wi];
            const size_t bytes = static_cast<size_t>(
                g_adamw.find(it.w_key)->second.n) * sizeof(float);
            if (it.rc == -2) {
                if (it.w_out != nullptr)
                    std::memcpy(it.w_out,
                                reinterpret_cast<const char*>(
                                    g_adamw_pin_out) + off,
                                bytes);
                it.rc = 0;
            }
            off += bytes;
        }
    }
    return 0;
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
    // Batch pipeline teardown: driver events are owned objects — always
    // destroy; the pinned slabs came from the manager, which frees them
    // via pinned_free (extras) or pinned_release_all (ring).
    if (g_drv.event_destroy != nullptr) {
        for (CUevent_t e : g_adamw_ev_h) g_drv.event_destroy(e);
        for (CUevent_t e : g_adamw_ev_k) g_drv.event_destroy(e);
    }
    g_adamw_ev_h.clear();
    g_adamw_ev_k.clear();
    if (g_adamw_pin_in != nullptr)
        mp::mgr().pinned_free(g_adamw_pin_in);
    if (g_adamw_pin_out != nullptr)
        mp::mgr().pinned_free(g_adamw_pin_out);
    g_adamw_pin_in = g_adamw_pin_out = nullptr;
    g_adamw_pin_cap = 0;
    return 0;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
