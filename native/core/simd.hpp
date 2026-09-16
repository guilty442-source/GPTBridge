// simd.hpp — SIMD utilities, ISA dispatch, alignment, scalar fallback (A221/E186).
//
// Private to native/core/.  CPU ISA dispatch is fully encapsulated in C++.
// Python/C ABI never exposes AVX/SSE/NEON details.
// No global fast-math that breaks semantics.

#ifndef GPTBRIDGE_NATIVE_SIMD_HPP
#define GPTBRIDGE_NATIVE_SIMD_HPP

#include <cstdint>
#include <cstddef>
#include <cmath>
#include <cstring>

#if defined(_MSC_VER)
    #include <intrin.h>
#elif defined(__GNUC__) || defined(__clang__)
    #include <cpuid.h>
    #include <x86intrin.h>
#endif

namespace gptbridge_native_simd {

// ============================================================================
// ISA feature detection (x86/x64 only for now)
// ============================================================================

enum class IsaLevel : uint8_t {
    Scalar = 0,   // Pure scalar fallback
    SSE2  = 1,    // SSE2 (baseline x86-64)
    AVX   = 2,    // AVX (256-bit)
    AVX2  = 3,    // AVX2 (256-bit integer + FMA)
    AVX512F = 4,  // AVX-512 Foundation (512-bit)
};

// Detect highest available ISA at runtime (called once at init).
IsaLevel detect_isa() noexcept {
    // Baseline: SSE2 is guaranteed on x86-64
    IsaLevel level = IsaLevel::SSE2;

#if defined(_MSC_VER) || defined(__GNUC__) || defined(__clang__)
    int cpu_info[4] = {0, 0, 0, 0};

    // CPUID leaf 1: SSE2, SSE3, SSSE3, SSE4.1, SSE4.2, AVX
    __cpuid(cpu_info, 1);
    bool has_avx = (cpu_info[2] & (1 << 28)) != 0;      // ECX bit 28
    bool has_osxsave = (cpu_info[2] & (1 << 27)) != 0;  // ECX bit 27

    if (has_avx && has_osxsave) {
        // Check XGETBV for AVX state save
        uint64_t xcr0 = _xgetbv(0);
        if ((xcr0 & 0x6) == 0x6) {  // XMM and YMM state
            level = IsaLevel::AVX;

            // CPUID leaf 7: AVX2, AVX-512
            __cpuid(cpu_info, 7);
            bool has_avx2 = (cpu_info[1] & (1 << 5)) != 0;     // EBX bit 5
            bool has_avx512f = (cpu_info[1] & (1 << 16)) != 0; // EBX bit 16

            if (has_avx2) {
                level = IsaLevel::AVX2;
            }
            if (has_avx512f) {
                // Check OS support for AVX-512
                if ((xcr0 & 0xe6) == 0xe6) {  // XMM, YMM, ZMM, opmask
                    level = IsaLevel::AVX512F;
                }
            }
        }
    }
#endif

    return level;
}

// Cached ISA level (initialized once)
inline IsaLevel get_isa_level() noexcept {
    static const IsaLevel level = detect_isa();
    return level;
}

// Compile-time ISA macros for explicit specialization
#if defined(__AVX512F__)
    #define GPTBRIDGE_NATIVE_HAS_AVX512F 1
#else
    #define GPTBRIDGE_NATIVE_HAS_AVX512F 0
#endif

#if defined(__AVX2__)
    #define GPTBRIDGE_NATIVE_HAS_AVX2 1
#else
    #define GPTBRIDGE_NATIVE_HAS_AVX2 0
#endif

#if defined(__AVX__)
    #define GPTBRIDGE_NATIVE_HAS_AVX 1
#else
    #define GPTBRIDGE_NATIVE_HAS_AVX 0
#endif

#if defined(__SSE2__)
    #define GPTBRIDGE_NATIVE_HAS_SSE2 1
#else
    #define GPTBRIDGE_NATIVE_HAS_SSE2 0
#endif

// ============================================================================
// Alignment utilities
// ============================================================================

constexpr size_t kSimdAlignment = 64;  // 64-byte cache line (AVX-512 friendly)

inline void* aligned_alloc(size_t size, size_t alignment = kSimdAlignment) noexcept {
#if defined(_MSC_VER)
    return _aligned_malloc(size, alignment);
#else
    void* ptr = nullptr;
    if (posix_memalign(&ptr, alignment, size) != 0) return nullptr;
    return ptr;
#endif
}

inline void aligned_free(void* ptr) noexcept {
#if defined(_MSC_VER)
    _aligned_free(ptr);
#else
    std::free(ptr);
#endif
}

// Check if pointer is aligned
inline bool is_aligned(const void* ptr, size_t alignment = kSimdAlignment) noexcept {
    return (reinterpret_cast<uintptr_t>(ptr) & (alignment - 1)) == 0;
}

// Align size up to alignment boundary
inline size_t align_up(size_t size, size_t alignment = kSimdAlignment) noexcept {
    return (size + alignment - 1) & ~(alignment - 1);
}

// ============================================================================
// Tail handling utilities
// ============================================================================

// Process remainder elements with scalar fallback
template <typename Func>
inline void process_tail(size_t start, size_t end, Func&& func) noexcept {
    for (size_t i = start; i < end; ++i) {
        func(i);
    }
}

// Process in chunks of `chunk_size`, with scalar tail
template <typename Func>
inline void process_chunked(size_t total, size_t chunk_size, Func&& func) noexcept {
    size_t full_chunks = total / chunk_size;
    for (size_t c = 0; c < full_chunks; ++c) {
        func(c * chunk_size, (c + 1) * chunk_size);
    }
    size_t remainder_start = full_chunks * chunk_size;
    if (remainder_start < total) {
        process_tail(remainder_start, total, func);
    }
}

// ============================================================================
// Scalar fallback implementations (always available)
// ============================================================================

namespace scalar {

inline double dot(const double* a, const double* b, int64_t dim) noexcept {
    if (!a || !b || dim <= 0) return 0.0;
    double result = 0.0;
    for (int64_t i = 0; i < dim; ++i) result += a[i] * b[i];
    return result;
}

inline double l2_norm(const double* a, int64_t dim) noexcept {
    if (!a || dim <= 0) return 0.0;
    double sum = 0.0;
    for (int64_t i = 0; i < dim; ++i) {
        double v = a[i];
        sum += v * v;
    }
    return std::sqrt(sum);
}

inline double cosine_similarity(const double* a, const double* b, int64_t dim) noexcept {
    if (!a || !b || dim <= 0) return 0.0;
    double dot_val = 0.0, norm_a = 0.0, norm_b = 0.0;
    for (int64_t i = 0; i < dim; ++i) {
        double va = a[i], vb = b[i];
        dot_val += va * vb;
        norm_a += va * va;
        norm_b += vb * vb;
    }
    norm_a = std::sqrt(norm_a);
    norm_b = std::sqrt(norm_b);
    if (norm_a == 0.0 || norm_b == 0.0) return 0.0;
    return dot_val / (norm_a * norm_b);
}

}  // namespace scalar

// ============================================================================
// FP tolerance utilities (for parity testing)
// ============================================================================

inline bool fp_equal(double a, double b, double abs_tol = 1e-12, double rel_tol = 1e-9) noexcept {
    double diff = std::abs(a - b);
    if (diff <= abs_tol) return true;
    double max_ab = std::max(std::abs(a), std::abs(b));
    return diff <= rel_tol * max_ab;
}

inline bool fp_array_equal(const double* a, const double* b, int64_t dim,
                           double abs_tol = 1e-12, double rel_tol = 1e-9) noexcept {
    if (!a || !b || dim <= 0) return true;
    for (int64_t i = 0; i < dim; ++i) {
        if (!fp_equal(a[i], b[i], abs_tol, rel_tol)) return false;
    }
    return true;
}

// ============================================================================
// Edge case handling
// ============================================================================

// Check for NaN/Inf in array
inline bool has_nan_or_inf(const double* a, int64_t dim) noexcept {
    if (!a || dim <= 0) return false;
    for (int64_t i = 0; i < dim; ++i) {
        if (!std::isfinite(a[i])) return true;
    }
    return false;
}

// Replace NaN/Inf with 0.0 (for safe compute)
inline void sanitize_array(double* a, int64_t dim) noexcept {
    if (!a || dim <= 0) return;
    for (int64_t i = 0; i < dim; ++i) {
        if (!std::isfinite(a[i])) a[i] = 0.0;
    }
}

}  // namespace gptbridge_native_simd

#endif  // GPTBRIDGE_NATIVE_SIMD_HPP