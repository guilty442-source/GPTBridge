// cuda_rtlane.h — CUDA runtime surface for the governed lanes.
//
// Two backends behind one contract:
//
//   default (toolkit build): thin inline forwards onto
//     <cuda_runtime.h> — cudart_static is linked exactly as before and
//     every call keeps its static semantics. Zero behavioral change.
//
//   XINGCHENG_CUDA_DYNRT: the same surface resolves the CUDA DRIVER API
//     from nvcuda.dll via LoadLibrary — the governed lane then compiles
//     with MSVC alone (no toolkit headers, no cudart import lib), which
//     extends the lane's existing zero-CUDA-dll-import binary contract
//     (nvcuda / nvrtc / cublas are already LoadLibrary'd) to hosts with
//     no CUDA toolkit at all. The driver mempool API
//     (cuDeviceGetDefaultMemPool + cuMemAllocFromPoolAsync) preserves
//     the pool/high-water semantics; the default pool is borrowed, not
//     owned, so pool_destroy() is a no-op on this backend. Every wrapper
//     fails closed: unresolved symbols or a missing driver report
//     non-zero, cuda_present stays false and the governed lanes never
//     reach the device.
//
// The two backends intentionally share only this header's surface —
// return 0 on success, pointers/handles are void*-sized, and the
// stream/event/copy argument order matches the runtime API so call
// sites stay byte-identical in shape.

#pragma once

#if defined(XINGCHENG_CUDA) && defined(XINGCHENG_CUDA_DYNRT)

#include <windows.h>

#include <cstdint>
#include <mutex>

namespace xcm_rt {

inline constexpr int kSuccess = 0;

enum class CopyKind : int { H2D, D2H, D2D };

// Handles: CUstream/CUevent/CUmemoryPool/CUcontext are pointer-sized
// opaque types; CUdeviceptr is a 64-bit integer. Both are carried as
// void*/u64 so no CUDA headers are needed.
using stream_t = void*;
using event_t = void*;
using pool_t = void*;

struct Drv {
    HMODULE dll = nullptr;
    int (*init)(unsigned int) = nullptr;
    int (*dev_count)(int*) = nullptr;
    int (*dev_get)(int*, int) = nullptr;
    int (*dev_get_attr)(int*, int, int) = nullptr;
    int (*ctx_retain)(void**, int) = nullptr;
    int (*ctx_set)(void*) = nullptr;
    int (*mem_info)(size_t*, size_t*) = nullptr;
    int (*dev_default_pool)(void**, int) = nullptr;
    int (*pool_set_attr)(void*, int, void*) = nullptr;
    int (*stream_create_prio)(void**, unsigned int, int) = nullptr;
    int (*stream_destroy)(void*) = nullptr;
    int (*stream_sync)(void*) = nullptr;
    int (*stream_wait_event)(void*, void*, unsigned int) = nullptr;
    int (*event_create)(void**, unsigned int) = nullptr;
    int (*event_record)(void*, void*) = nullptr;
    int (*event_destroy)(void*) = nullptr;
    int (*pool_alloc_async)(unsigned long long*, size_t, void*, void*) =
        nullptr;
    int (*mem_free_async)(unsigned long long, void*) = nullptr;
    int (*mem_alloc)(unsigned long long*, size_t) = nullptr;
    int (*mem_free)(unsigned long long) = nullptr;
    int (*host_alloc)(void**, size_t, unsigned int) = nullptr;
    int (*host_free)(void*) = nullptr;
    int (*copy_htod_async)(unsigned long long, const void*, size_t,
                           void*) = nullptr;
    int (*copy_dtoh_async)(void*, unsigned long long, size_t, void*) =
        nullptr;
    int (*copy_dtod_async)(unsigned long long, unsigned long long, size_t,
                           void*) = nullptr;
    void* ctx = nullptr;
};

inline Drv& drv() {
    static Drv d;
    return d;
}

template <typename F>
inline bool resolve_one(HMODULE m, F& out, const char* name,
                        const char* alt = nullptr) {
    out = reinterpret_cast<F>(
        reinterpret_cast<void*>(GetProcAddress(m, name)));
    if (out == nullptr && alt != nullptr)
        out = reinterpret_cast<F>(
            reinterpret_cast<void*>(GetProcAddress(m, alt)));
    return out != nullptr;
}

// Resolves every required export from the driver and retains the primary
// context of device 0. Called lazily through call_once — a racing caller
// can never observe a half-initialised table, and a missing dll/export
// leaves the lane unavailable rather than aborting the process.
inline bool drv_init_impl() {
    Drv& d = drv();
    d.dll = LoadLibraryA("nvcuda.dll");
    if (d.dll == nullptr) d.dll = LoadLibraryA("cuda.dll");
    if (d.dll == nullptr) return false;
    bool o = true;
    o &= resolve_one(d.dll, d.init, "cuInit");
    o &= resolve_one(d.dll, d.dev_count, "cuDeviceGetCount");
    o &= resolve_one(d.dll, d.dev_get, "cuDeviceGet");
    o &= resolve_one(d.dll, d.dev_get_attr, "cuDeviceGetAttribute");
    o &= resolve_one(d.dll, d.ctx_retain, "cuDevicePrimaryCtxRetain");
    o &= resolve_one(d.dll, d.ctx_set, "cuCtxSetCurrent");
    o &= resolve_one(d.dll, d.mem_info, "cuMemGetInfo_v2", "cuMemGetInfo");
    o &= resolve_one(d.dll, d.dev_default_pool,
                     "cuDeviceGetDefaultMemPool");
    o &= resolve_one(d.dll, d.pool_set_attr, "cuMemPoolSetAttribute");
    o &= resolve_one(d.dll, d.stream_create_prio,
                     "cuStreamCreateWithPriority");
    o &= resolve_one(d.dll, d.stream_destroy, "cuStreamDestroy_v2",
                     "cuStreamDestroy");
    o &= resolve_one(d.dll, d.stream_sync, "cuStreamSynchronize");
    o &= resolve_one(d.dll, d.stream_wait_event, "cuStreamWaitEvent");
    o &= resolve_one(d.dll, d.event_create, "cuEventCreate");
    o &= resolve_one(d.dll, d.event_record, "cuEventRecord");
    o &= resolve_one(d.dll, d.event_destroy, "cuEventDestroy_v2",
                     "cuEventDestroy");
    o &= resolve_one(d.dll, d.pool_alloc_async, "cuMemAllocFromPoolAsync");
    o &= resolve_one(d.dll, d.mem_free_async, "cuMemFreeAsync_v2",
                     "cuMemFreeAsync");
    o &= resolve_one(d.dll, d.mem_alloc, "cuMemAlloc_v2", "cuMemAlloc");
    o &= resolve_one(d.dll, d.mem_free, "cuMemFree_v2", "cuMemFree");
    o &= resolve_one(d.dll, d.host_alloc, "cuMemHostAlloc");
    o &= resolve_one(d.dll, d.host_free, "cuMemFreeHost");
    o &= resolve_one(d.dll, d.copy_htod_async, "cuMemcpyHtoDAsync_v2",
                     "cuMemcpyHtoDAsync");
    o &= resolve_one(d.dll, d.copy_dtoh_async, "cuMemcpyDtoHAsync_v2",
                     "cuMemcpyDtoHAsync");
    o &= resolve_one(d.dll, d.copy_dtod_async, "cuMemcpyDtoDAsync_v2",
                     "cuMemcpyDtoDAsync");
    if (!o) return false;
    if (d.init(0) != 0) return false;
    int dev = 0;
    if (d.dev_get(&dev, 0) != 0) return false;
    if (d.ctx_retain(&d.ctx, dev) != 0) return false;
    if (d.ctx_set(d.ctx) != 0) return false;
    return true;
}

inline bool drv_init() {
    static std::once_flag once;
    static bool ok = false;
    std::call_once(once, [] { ok = drv_init_impl(); });
    return ok;
}

inline int device_count(int* n) {
    return drv_init() ? drv().dev_count(n) : 1;
}
inline int mem_get_info(size_t* free_b, size_t* total_b) {
    return drv_init() ? drv().mem_info(free_b, total_b) : 1;
}
// cudaDevAttrComputeCapabilityMajor/Minor — stable values shared by the
// runtime and driver enums.
inline int device_cc(int dev, int* major, int* minor) {
    if (!drv_init()) return 1;
    if (drv().dev_get_attr(major, 75, dev) != 0) return 1;
    if (drv().dev_get_attr(minor, 76, dev) != 0) return 1;
    return 0;
}
// Borrowed default pool — the release-threshold attribute gives the
// same high-water reuse semantics as a created pool; pool_destroy()
// intentionally no-ops because the handle is not owned.
inline int pool_create(pool_t* out) {
    if (!drv_init()) return 1;
    return drv().dev_default_pool(out, 0);
}
inline int pool_set_release_threshold(pool_t p, void* value) {
    // CU_MEMPOOL_ATTR_RELEASE_THRESHOLD (4) — same ordinal as the
    // runtime's cudaMemPoolAttrReleaseThreshold.
    return drv_init() ? drv().pool_set_attr(p, 4, value) : 1;
}
inline int pool_destroy(pool_t) { return 0; }
inline int stream_create_prio(stream_t* out, int priority) {
    // CU_STREAM_NON_BLOCKING = 0x1.
    return drv_init() ? drv().stream_create_prio(out, 0x1u, priority) : 1;
}
inline int stream_destroy(stream_t s) {
    return drv_init() ? drv().stream_destroy(s) : 1;
}
inline int stream_sync(stream_t s) {
    return drv_init() ? drv().stream_sync(s) : 1;
}
inline int stream_wait_event(stream_t s, event_t e) {
    return drv_init() ? drv().stream_wait_event(s, e, 0u) : 1;
}
inline int event_create(event_t* out) {
    // CU_EVENT_DISABLE_TIMING = 0x2.
    return drv_init() ? drv().event_create(out, 0x2u) : 1;
}
inline int event_record(event_t e, stream_t s) {
    return drv_init() ? drv().event_record(e, s) : 1;
}
inline int event_destroy(event_t e) {
    return drv_init() ? drv().event_destroy(e) : 1;
}
inline int pool_alloc(void** out, size_t bytes, pool_t pool,
                      stream_t s) {
    if (!drv_init()) return 1;
    unsigned long long dp = 0;
    const int rc = drv().pool_alloc_async(&dp, bytes, pool, s);
    *out = rc == 0 ? reinterpret_cast<void*>(dp) : nullptr;
    return rc;
}
inline int free_async(void* p, stream_t s) {
    return drv_init() ? drv().mem_free_async(
                            static_cast<unsigned long long>(
                                reinterpret_cast<uintptr_t>(p)),
                            s)
                      : 1;
}
inline int malloc_dev(void** out, size_t bytes) {
    if (!drv_init()) return 1;
    unsigned long long dp = 0;
    const int rc = drv().mem_alloc(&dp, bytes);
    *out = rc == 0 ? reinterpret_cast<void*>(dp) : nullptr;
    return rc;
}
inline int free_dev(void* p) {
    return drv_init() ? drv().mem_free(static_cast<unsigned long long>(
                            reinterpret_cast<uintptr_t>(p)))
                      : 1;
}
inline int host_alloc(void** out, size_t bytes) {
    return drv_init() ? drv().host_alloc(out, bytes, 0u) : 1;
}
inline int host_free(void* p) {
    return drv_init() ? drv().host_free(p) : 1;
}
inline int memcpy_async(void* dst, const void* src, size_t bytes,
                        CopyKind kind, stream_t s) {
    if (!drv_init()) return 1;
    const unsigned long long dstp = static_cast<unsigned long long>(
        reinterpret_cast<uintptr_t>(dst));
    const unsigned long long srcp = static_cast<unsigned long long>(
        reinterpret_cast<uintptr_t>(src));
    switch (kind) {
        case CopyKind::H2D:
            return drv().copy_htod_async(dstp, src, bytes, s);
        case CopyKind::D2H:
            return drv().copy_dtoh_async(dst, srcp, bytes, s);
        default:
            return drv().copy_dtod_async(dstp, srcp, bytes, s);
    }
}

}  // namespace xcm_rt

#elif defined(XINGCHENG_CUDA)

// Toolkit build: the same surface forwards onto the statically linked
// runtime API — identical semantics, still zero dll imports.

#include <cuda_runtime.h>

#include <cstdint>

namespace xcm_rt {

inline constexpr int kSuccess = cudaSuccess;

enum class CopyKind : int { H2D, D2H, D2D };

using stream_t = cudaStream_t;
using event_t = cudaEvent_t;
using pool_t = cudaMemPool_t;

inline int device_count(int* n) { return (int)cudaGetDeviceCount(n); }
inline int mem_get_info(size_t* free_b, size_t* total_b) {
    return (int)cudaMemGetInfo(free_b, total_b);
}
inline int device_cc(int dev, int* major, int* minor) {
    cudaDeviceProp prop{};
    const int rc = (int)cudaGetDeviceProperties(&prop, dev);
    *major = prop.major;
    *minor = prop.minor;
    return rc;
}
inline int pool_create(pool_t* out) {
    cudaMemPoolProps props{};
    props.allocType = cudaMemAllocationTypePinned;
    props.handleTypes = cudaMemHandleTypeNone;
    props.location.type = cudaMemLocationTypeDevice;
    props.location.id = 0;
    return (int)cudaMemPoolCreate(out, &props);
}
inline int pool_set_release_threshold(pool_t p, void* value) {
    return (int)cudaMemPoolSetAttribute(
        p, cudaMemPoolAttrReleaseThreshold, value);
}
inline int pool_destroy(pool_t p) { return (int)cudaMemPoolDestroy(p); }
inline int stream_create_prio(stream_t* out, int priority) {
    return (int)cudaStreamCreateWithPriority(out, cudaStreamNonBlocking,
                                             priority);
}
inline int stream_destroy(stream_t s) {
    return (int)cudaStreamDestroy(s);
}
inline int stream_sync(stream_t s) { return (int)cudaStreamSynchronize(s); }
inline int stream_wait_event(stream_t s, event_t e) {
    return (int)cudaStreamWaitEvent(s, e, 0);
}
inline int event_create(event_t* out) {
    return (int)cudaEventCreateWithFlags(out, cudaEventDisableTiming);
}
inline int event_record(event_t e, stream_t s) {
    return (int)cudaEventRecord(e, s);
}
inline int event_destroy(event_t e) { return (int)cudaEventDestroy(e); }
inline int pool_alloc(void** out, size_t bytes, pool_t pool,
                      stream_t s) {
    return (int)cudaMallocFromPoolAsync(out, bytes, pool, s);
}
inline int free_async(void* p, stream_t s) {
    return (int)cudaFreeAsync(p, s);
}
inline int malloc_dev(void** out, size_t bytes) {
    return (int)cudaMalloc(out, bytes);
}
inline int free_dev(void* p) { return (int)cudaFree(p); }
inline int host_alloc(void** out, size_t bytes) {
    return (int)cudaHostAlloc(out, bytes, cudaHostAllocDefault);
}
inline int host_free(void* p) { return (int)cudaFreeHost(p); }
inline int memcpy_async(void* dst, const void* src, size_t bytes,
                        CopyKind kind, stream_t s) {
    const cudaMemcpyKind k = kind == CopyKind::H2D
                                 ? cudaMemcpyHostToDevice
                                 : kind == CopyKind::D2H
                                       ? cudaMemcpyDeviceToHost
                                       : cudaMemcpyDeviceToDevice;
    return (int)cudaMemcpyAsync(dst, src, bytes, k, s);
}

}  // namespace xcm_rt

#endif  // XINGCHENG_CUDA
