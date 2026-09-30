/*
 * transformer.c — transformation compute core (A221/E186).  Pure C + SIMD.
 *
 * Owns transformation compute (tensor operations, model inference).
 * Python owns all memory; C borrows raw pointers + length.
 * No unbounded allocation; no per-request thread pool.
 *
 * SIMD: AVX-512F/VL/DQ (8×double) 優先；其次 AVX2+FMA (4×double)；
 * 皆編譯時偵測，執行期 CPUID 派送；不可用回退純量，語意等價。 */
#include "transformer.h"
#include "memory.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

/* Always expose AVX-512/AVX2 intrinsics on x86-64 when immintrin.h is
 * available; actual execution is gated by runtime CPUID in
 * gptbridge_native_simd_level().  This avoids the __AVX512F__ compile-flag
 * dependency (/arch:AVX512) while keeping the fallback scalar path. */
#if defined(_M_X64) || defined(_M_IX86) || defined(__x86_64__) || defined(__i386__) || defined(__AVX512F__) || defined(__AVX2__)
#include <immintrin.h>
#define GPTBRIDGE_HAVE_AVX512 1
#define GPTBRIDGE_HAVE_AVX2 1
#elif defined(__AVX512F__) && defined(__AVX512VL__) && defined(__AVX512DQ__)
#include <immintrin.h>
#define GPTBRIDGE_HAVE_AVX512 1
#elif defined(__AVX2__)
#include <immintrin.h>
#define GPTBRIDGE_HAVE_AVX2 1
#endif

/* ------------------------------------------------------------------
 * Runtime CPU feature detection (CPUID) for AVX-512 / AVX2 dispatch
 * ------------------------------------------------------------------ */
#ifdef _WIN32
#include <intrin.h>
#include <windows.h>
#else
#include <cpuid.h>
#endif

/* B94 self-decomposition: kernel/op groups are textually included so
   this file remains the single translation unit (static row kernels
   stay visible).  Fragments are not compiled separately. */
#include "transformer_kernels.c"
#include "transformer_ops_a.c"
#include "transformer_ops_b.c"
