// cuda_drvapi.h — pure CUDA Driver API binding for the Xingcheng
// native CUDA lane (B132).
//
// The lane binds exactly one vendor module: nvcuda.dll (the CUDA
// Driver API shipped inside every NVIDIA driver install; cuda.dll is
// the fallback alias). Device code is embedded PTX text authored in
// this repo and JIT-compiled by the driver itself through
// cuModuleLoadData, so no toolkit install — no nvcc, no NVRTC dll, no
// cuBLAS dll, no cudart import — is ever required. On a host without
// an NVIDIA driver every probe returns failure and the governed
// request paths fail closed, never a link-time error.
//
// Shared by cuda_memplane.h (allocation plane) and cuda_kernels.cpp
// (kernel module); the binding table and the retained primary context
// are process-wide singletons so both TUs see one device state.

#pragma once

#if defined(XINGCHENG_CUDA)

#include <windows.h>

#include <mutex>
#include <vector>

namespace xcuda_drv {

typedef int CUresult_t;
typedef int CUdevice_t;
typedef unsigned long long CUdevptr_t;
typedef unsigned long long CUcontext_t;
typedef unsigned long long CUmodule_t;
typedef unsigned long long CUfunction_t;
typedef unsigned long long CUstream_t;
typedef unsigned long long CUevent_t;
typedef unsigned long long CUmempool_t;
typedef unsigned long long CUgraph_t;
typedef unsigned long long CUgraphExec_t;

enum : int {
    kOk = 0,
    kAttrCcMajor = 75,          // CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR
    kAttrCcMinor = 76,          // CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR
    kStreamNonBlocking = 1,     // CU_STREAM_NON_BLOCKING
    kEventDisableTiming = 2,    // CU_EVENT_DISABLE_TIMING
    kMempoolReleaseThreshold = 4,  // CU_MEMPOOL_ATTR_RELEASE_THRESHOLD
    kHostAllocDefault = 0,      // CU_MEMHOSTALLOC_DEFAULT
    kCaptureGlobal = 0,         // CU_STREAM_CAPTURE_MODE_GLOBAL
};

struct Api {
    HMODULE dll = nullptr;
    CUresult_t (*init)(unsigned int) = nullptr;
    CUresult_t (*device_get_count)(int*) = nullptr;
    CUresult_t (*device_get)(CUdevice_t*, int) = nullptr;
    CUresult_t (*device_get_attribute)(int*, int, CUdevice_t) = nullptr;
    CUresult_t (*primary_ctx_retain)(CUcontext_t*, CUdevice_t) = nullptr;
    CUresult_t (*ctx_set_current)(CUcontext_t) = nullptr;
    CUresult_t (*ctx_sync)() = nullptr;
    CUresult_t (*mem_get_info)(size_t*, size_t*) = nullptr;
    // Device default pool + release threshold replace a private pool
    // create — identical stream-ordered high-water semantics without a
    // second mempool ABI surface.
    CUresult_t (*device_default_pool)(CUmempool_t*, CUdevice_t) = nullptr;
    CUresult_t (*pool_set_attribute)(CUmempool_t, int, void*) = nullptr;
    CUresult_t (*pool_alloc_async)(CUdevptr_t*, size_t, CUmempool_t,
                                   CUstream_t) = nullptr;
    CUresult_t (*mem_free_async)(CUdevptr_t, CUstream_t) = nullptr;
    CUresult_t (*host_alloc)(void**, size_t, unsigned int) = nullptr;
    CUresult_t (*host_free)(void*) = nullptr;
    CUresult_t (*memcpy_htod)(CUdevptr_t, const void*, size_t) = nullptr;
    CUresult_t (*memcpy_dtoh)(void*, CUdevptr_t, size_t) = nullptr;
    CUresult_t (*memcpy_htod_async)(CUdevptr_t, const void*, size_t,
                                    CUstream_t) = nullptr;
    CUresult_t (*memcpy_dtoh_async)(void*, CUdevptr_t, size_t,
                                    CUstream_t) = nullptr;
    CUresult_t (*stream_create_pri)(CUstream_t*, unsigned int,
                                    int) = nullptr;
    CUresult_t (*stream_destroy)(CUstream_t) = nullptr;
    CUresult_t (*stream_sync)(CUstream_t) = nullptr;
    CUresult_t (*stream_wait_event)(CUstream_t, CUevent_t,
                                    unsigned int) = nullptr;
    CUresult_t (*event_create)(CUevent_t*, unsigned int) = nullptr;
    CUresult_t (*event_record)(CUevent_t, CUstream_t) = nullptr;
    CUresult_t (*event_destroy)(CUevent_t) = nullptr;
    CUresult_t (*module_load_data)(CUmodule_t*, const void*) = nullptr;
    // Ex variant carries the JIT error log — governance evidence for a
    // fail-closed module load (which PTX line rejected, and why).
    CUresult_t (*module_load_data_ex)(CUmodule_t*, const void*,
                                      unsigned int, int*,
                                      void**) = nullptr;
    CUresult_t (*module_unload)(CUmodule_t) = nullptr;
    CUresult_t (*module_get_function)(CUfunction_t*, CUmodule_t,
                                      const char*) = nullptr;
    CUresult_t (*launch_kernel)(CUfunction_t, unsigned int,
                                unsigned int, unsigned int,
                                unsigned int, unsigned int, unsigned int,
                                unsigned int, CUstream_t, void**,
                                void**) = nullptr;
    // §37 graph plane — optional symbols; absence only disables graph
    // capture, never the base lane.
    CUresult_t (*stream_begin_capture)(CUstream_t, int) = nullptr;
    CUresult_t (*stream_end_capture)(CUstream_t, CUgraph_t*) = nullptr;
    CUresult_t (*graph_instantiate)(CUgraphExec_t*, CUgraph_t,
                                    unsigned long long) = nullptr;
    CUresult_t (*graph_launch)(CUgraphExec_t, CUstream_t) = nullptr;
    CUresult_t (*graph_exec_destroy)(CUgraphExec_t) = nullptr;
    CUresult_t (*graph_destroy)(CUgraph_t) = nullptr;
};

inline Api& api() {
    static Api a;
    return a;
}

template <typename T>
bool sym(HMODULE dll, T* fn, const char* name, const char* alt) {
    FARPROC p = GetProcAddress(dll, name);
    if (p == nullptr && alt != nullptr) p = GetProcAddress(dll, alt);
    if (p == nullptr) return false;
    *fn = reinterpret_cast<T>(p);
    return true;
}

// Optional-symbol variant: absence is tolerated (graph plane).
template <typename T>
void sym_opt(HMODULE dll, T* fn, const char* name, const char* alt) {
    FARPROC p = GetProcAddress(dll, name);
    if (p == nullptr && alt != nullptr) p = GetProcAddress(dll, alt);
    if (p != nullptr) *fn = reinterpret_cast<T>(p);
}

inline bool api_init() {
    Api& a = api();
    static std::once_flag once;
    static bool ok = false;
    std::call_once(once, [&] {
        a.dll = LoadLibraryA("nvcuda.dll");
        if (a.dll == nullptr) a.dll = LoadLibraryA("cuda.dll");
        if (a.dll == nullptr) return;
        bool r = true;
        r &= sym(a.dll, &a.init, "cuInit", nullptr);
        r &= sym(a.dll, &a.device_get_count, "cuDeviceGetCount", nullptr);
        r &= sym(a.dll, &a.device_get, "cuDeviceGet", nullptr);
        r &= sym(a.dll, &a.device_get_attribute, "cuDeviceGetAttribute",
                 nullptr);
        r &= sym(a.dll, &a.primary_ctx_retain, "cuDevicePrimaryCtxRetain",
                 nullptr);
        r &= sym(a.dll, &a.ctx_set_current, "cuCtxSetCurrent", nullptr);
        r &= sym(a.dll, &a.ctx_sync, "cuCtxSynchronize", nullptr);
        r &= sym(a.dll, &a.mem_get_info, "cuMemGetInfo_v2",
                 "cuMemGetInfo");
        r &= sym(a.dll, &a.device_default_pool, "cuDeviceGetDefaultMemPool",
                 nullptr);
        r &= sym(a.dll, &a.pool_set_attribute, "cuMemPoolSetAttribute",
                 nullptr);
        r &= sym(a.dll, &a.pool_alloc_async, "cuMemAllocFromPoolAsync",
                 nullptr);
        r &= sym(a.dll, &a.mem_free_async, "cuMemFreeAsync", nullptr);
        r &= sym(a.dll, &a.host_alloc, "cuMemHostAlloc", nullptr);
        r &= sym(a.dll, &a.host_free, "cuMemFreeHost", nullptr);
        r &= sym(a.dll, &a.memcpy_htod, "cuMemcpyHtoD_v2",
                 "cuMemcpyHtoD");
        r &= sym(a.dll, &a.memcpy_dtoh, "cuMemcpyDtoH_v2",
                 "cuMemcpyDtoH");
        r &= sym(a.dll, &a.memcpy_htod_async, "cuMemcpyHtoDAsync_v2",
                 "cuMemcpyHtoDAsync");
        r &= sym(a.dll, &a.memcpy_dtoh_async, "cuMemcpyDtoHAsync_v2",
                 "cuMemcpyDtoHAsync");
        r &= sym(a.dll, &a.stream_create_pri, "cuStreamCreateWithPriority",
                 nullptr);
        r &= sym(a.dll, &a.stream_destroy, "cuStreamDestroy_v2",
                 "cuStreamDestroy");
        r &= sym(a.dll, &a.stream_sync, "cuStreamSynchronize", nullptr);
        r &= sym(a.dll, &a.stream_wait_event, "cuStreamWaitEvent",
                 nullptr);
        r &= sym(a.dll, &a.event_create, "cuEventCreate", nullptr);
        r &= sym(a.dll, &a.event_record, "cuEventRecord", nullptr);
        r &= sym(a.dll, &a.event_destroy, "cuEventDestroy_v2",
                 "cuEventDestroy");
        r &= sym(a.dll, &a.module_load_data, "cuModuleLoadData", nullptr);
        sym_opt(a.dll, &a.module_load_data_ex, "cuModuleLoadDataEx",
                nullptr);
        r &= sym(a.dll, &a.module_unload, "cuModuleUnload", nullptr);
        r &= sym(a.dll, &a.module_get_function, "cuModuleGetFunction",
                 nullptr);
        r &= sym(a.dll, &a.launch_kernel, "cuLaunchKernel", nullptr);
        sym_opt(a.dll, &a.stream_begin_capture, "cuStreamBeginCapture_v2",
                "cuStreamBeginCapture");
        sym_opt(a.dll, &a.stream_end_capture, "cuStreamEndCapture",
                nullptr);
        sym_opt(a.dll, &a.graph_instantiate, "cuGraphInstantiateWithFlags",
                "cuGraphInstantiate_v2");
        sym_opt(a.dll, &a.graph_launch, "cuGraphLaunch", nullptr);
        sym_opt(a.dll, &a.graph_exec_destroy, "cuGraphExecDestroy_v2",
                "cuGraphExecDestroy");
        sym_opt(a.dll, &a.graph_destroy, "cuGraphDestroy", nullptr);
        ok = r;
    });
    return ok;
}

// ------------------------------------------------------ device probe --

struct Dev {
    CUcontext_t ctx = 0;
    CUdevice_t device = 0;
    int cc_major = 0;
    int cc_minor = 0;
    bool tried = false;
    bool ok = false;
};

inline Dev& dev() {
    static Dev d;
    return d;
}

// Load APIs, initialise the driver, retain the primary context of
// device 0 and record its compute capability (PTX target + sm_86 floor).
inline bool device_ready() {
    Dev& d = dev();
    if (d.tried) return d.ok;
    d.tried = true;
    if (!api_init()) return false;
    Api& a = api();
    if (a.init(0) != kOk) return false;
    int count = 0;
    if (a.device_get_count(&count) != kOk || count <= 0) return false;
    CUdevice_t dv = 0;
    if (a.device_get(&dv, 0) != kOk) return false;
    if (a.device_get_attribute(&d.cc_major, kAttrCcMajor, dv) != kOk ||
        a.device_get_attribute(&d.cc_minor, kAttrCcMinor, dv) != kOk)
        return false;
    if (a.primary_ctx_retain(&d.ctx, dv) != kOk || d.ctx == 0)
        return false;
    d.device = dv;
    d.ok = true;
    return true;
}

// Every entry point re-anchors the primary context so calls from any
// engine thread keep working (driver API is per-thread).
inline bool use_ctx() {
    return device_ready() &&
           api().ctx_set_current(dev().ctx) == kOk;
}

}  // namespace xcuda_drv

// Short alias lives in the one shared header so every consumer TUs sees
// exactly one definition (namespace aliases cannot be redeclared).
namespace xcd = xcuda_drv;

#endif  // XINGCHENG_CUDA
