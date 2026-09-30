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

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

// ---------------------------------------------------------------- types --

typedef int CUresult_t;
typedef int CUdevice_t;
typedef void* CUcontext_t;
typedef void* CUmodule_t;
typedef void* CUfunction_t;
typedef unsigned long long CUdevptr_t;
typedef unsigned long long CUstream_t;
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
    __shared__ __nv_bfloat16 as[XC_TILE][XC_TILE];
    __shared__ __nv_bfloat16 bs[XC_TILE][XC_TILE];
    const int row = blockIdx.y * XC_TILE + threadIdx.y;
    const int col = blockIdx.x * XC_TILE + threadIdx.x;
    float acc = 0.0f;
    for (int t = 0; t < (k + XC_TILE - 1) / XC_TILE; ++t) {
        const int a_col = t * XC_TILE + threadIdx.x;
        const int b_row = t * XC_TILE + threadIdx.y;
        as[threadIdx.y][threadIdx.x] =
            (row < m && a_col < k)
                ? a[(long long)row * k + a_col]
                : __float2bfloat16(0.0f);
        bs[threadIdx.y][threadIdx.x] =
            (b_row < k && col < n)
                ? b[(long long)b_row * n + col]
                : __float2bfloat16(0.0f);
        __syncthreads();
        for (int i = 0; i < XC_TILE; ++i) {
            acc += __bfloat162float(as[threadIdx.y][i]) *
                   __bfloat162float(bs[i][threadIdx.x]);
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
    for (int r = 0; r < XC_GEMV_MAX_M; ++r) acc[r] = 0.0f;
    for (int i = i0; i < i1; ++i) {
        const float bv = __bfloat162float(b[(long long)i * n + col]);
        for (int r = 0; r < m; ++r) {
            acc[r] += __bfloat162float(a[(long long)r * k + i]) * bv;
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
    __shared__ float as[XC_TILE][XC_TILE];
    __shared__ __nv_fp8_e4m3 bs[XC_TILE][XC_TILE];
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
                ? b[(long long)b_row * n + col] : __nv_fp8_e4m3(0.0f);
        __syncthreads();
        for (int i = 0; i < XC_TILE; ++i) {
            acc += as[threadIdx.y][i] * (float)(bs[i][threadIdx.x]);
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
    for (int r = 0; r < XC_GEMV_MAX_M; ++r) acc[r] = 0.0f;
    for (int i = i0; i < i1; ++i) {
        const float bv = (float)(b[(long long)i * n + col]);
        for (int r = 0; r < m; ++r) {
            acc[r] += a[(long long)r * k + i] * bv;
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
    if (!ok) {
        XCK_DBG("get_func fail");
        g_drv.module_unload(g_module);
        g_module = nullptr;
        return false;
    }
    return true;
}

// Launch helper: kernel params are passed by address.
bool launch(CUfunction_t f, unsigned int gx, unsigned int gy,
            unsigned int bx, unsigned int by, unsigned int shmem,
            void** params) {
    return g_drv.launch_kernel(f, gx, gy, 1, bx, by, 1, shmem, 0, params,
                               nullptr) == kCudaSuccess;
}

// -------------------------------------------------- bf16 / fp8 domains --

constexpr long long kConvThreads = 256;

std::mutex g_bf16_mu;
std::unordered_map<const void*, CUdevptr_t> g_bf16_weights;
std::mutex g_fp8_mu;
std::unordered_map<const void*, CUdevptr_t> g_fp8_weights;

CUdevptr_t dev_alloc(size_t bytes) {
    CUdevptr_t p = 0;
    if (g_drv.mem_alloc(&p, bytes) != kCudaSuccess) return 0;
    return p;
}

void dev_free(CUdevptr_t p) {
    if (p != 0) g_drv.mem_free(p);
}

// Quantize host f64 → fresh device bf16 buffer (caller frees).
CUdevptr_t upload_bf16(const double* host, long long elems) {
    const size_t f64b = static_cast<size_t>(elems) * sizeof(double);
    const size_t bfb = static_cast<size_t>(elems) * 2;
    CUdevptr_t staging = dev_alloc(f64b);
    if (staging == 0) return 0;
    CUdevptr_t dev = dev_alloc(bfb);
    if (dev == 0) {
        dev_free(staging);
        return 0;
    }
    if (g_drv.memcpy_htod(staging, host, f64b) != kCudaSuccess) {
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
    CUdevptr_t next = dev_alloc(bytes);
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

// Skinny-m threshold mirroring XC_GEMV_MAX_M in the device source.
constexpr long long kGemvMaxM = 16;
constexpr long long kGemvThreads = 256;
// Split-k factor: widens the launch so decode-size shapes still cover
// enough SMs to hide memory latency (fixed → deterministic reduce order).
constexpr long long kGemvKSplit = 8;

// Shared GEMM body: a (host f64) x db (device bf16) → out (host f64).
int run_bf16(const double* a, long long m, long long k, CUdevptr_t db,
             long long n, double* out) {
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
    if (g_drv.memcpy_htod(a_stage, a,
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
    if (g_drv.memcpy_dtoh(g_bf16_c_host.data(), dc,
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
    CUdevptr_t staging = dev_alloc(f64b);
    if (staging == 0) return 0;
    CUdevptr_t dev = dev_alloc(f8b);
    if (dev == 0) {
        dev_free(staging);
        return 0;
    }
    if (g_drv.memcpy_htod(staging, host, f64b) != kCudaSuccess) {
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
    if (g_drv.memcpy_htod(a_stage, a,
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
    if (g_drv.memcpy_dtoh(g_fp8_c_host.data(), dc,
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

bool grow_scratch(CUdevptr_t* buf, size_t* cap, size_t need_elems) {
    if (*cap >= need_elems) return true;
    dev_free(*buf);
    *buf = 0;
    *cap = 0;
    *buf = dev_alloc(need_elems * sizeof(double));
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
}

}  // namespace

extern "C" {

// ------------------------------------------------------- probe/release --

int xcuda_bf16_kernel_probe() {
    return device_ready() && ensure_module() ? 1 : 0;
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
    g_k = dev_alloc(elems * sizeof(double));
    if (g_k == 0) return 2;
    g_v = dev_alloc(elems * sizeof(double));
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
    if (g_drv.memcpy_htod(
            base, src,
            static_cast<size_t>(rows) * static_cast<size_t>(g_head_dim) *
                sizeof(double)) != kCudaSuccess) {
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
    if (g_drv.memcpy_htod(g_qbuf, q_host, q_elems * sizeof(double)) !=
        kCudaSuccess) {
        return 2;
    }
    const unsigned int shmem = static_cast<unsigned int>(
        (static_cast<size_t>(kKvTile) + static_cast<size_t>(head_dim) +
         32 + 5) * sizeof(double));
    {
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
        if (!launch(g_f_kv_attn,
                    static_cast<unsigned int>(heads * seq), 1,
                    static_cast<unsigned int>(kKvThreads), 1, shmem,
                    params)) {
            return 3;
        }
    }
    if (g_drv.ctx_sync() != kCudaSuccess) return 3;
    if (g_drv.memcpy_dtoh(out_host, g_obuf, o_elems * sizeof(double)) !=
        kCudaSuccess) {
        return 3;
    }
    return 0;
}

}  // extern "C"

#endif  // XINGCHENG_CUDA
