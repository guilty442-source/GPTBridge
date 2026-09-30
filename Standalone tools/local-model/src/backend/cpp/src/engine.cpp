// Xingcheng formal C++ inference layer (P3b–P3f).
//
// Scope: causal-LM inference (dense + sparse MoE), batch=1, FP64 compute
// over exported weights. Primitive tensor operations call the public C
// ABI. Unsupported model features fail closed instead of silently
// changing semantics.

#include "xingcheng_inference.hpp"

#include "gptbridge_native.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <limits>
#include <map>
#include <numeric>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <unordered_set>

#if defined(_M_X64) || defined(__x86_64__)
#define XINGCHENG_W1_X64 1
#include <immintrin.h>
#if defined(_MSC_VER)
#include <intrin.h>
#endif
#endif

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#pragma comment(lib, "bcrypt.lib")
#endif

namespace xingcheng::inference {
#include "engine_json.h"
#include "engine_util.h"
#include "engine_kernels.h"
#include "engine_weights.h"
#include "engine_tokenizer.h"
#include "engine_lifecycle.h"
#include "engine_forward.h"
#include "engine_gemma4.h"
#include "engine_generate.h"

}  // namespace xingcheng::inference
