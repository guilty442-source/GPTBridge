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
namespace {

// R6: positions per KV page block.
constexpr int64_t kKvBlockTokens = 16;

class InferenceError : public std::runtime_error {
public:
    explicit InferenceError(const std::string& message) : std::runtime_error(message) {}
};

// ── Minimal JSON parser ────────────────────────────────────────────────
// The bundle format only needs standards-compliant JSON values. The parser is
// deliberately bounded by input size and recursion depth and fails closed.

struct JsonValue {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool boolean = false;
    double number = 0.0;
    std::string string;
    std::vector<JsonValue> array;
    std::map<std::string, JsonValue> object;
};

void append_utf8(std::string& out, uint32_t cp) {
    if (cp <= 0x7F) {
        out.push_back(static_cast<char>(cp));
    } else if (cp <= 0x7FF) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0x10FFFF) {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        throw InferenceError("JSON_INVALID_UNICODE");
    }
}

class JsonParser {
public:
    explicit JsonParser(std::string_view input) : input_(input) {
        if (input_.size() > 64 * 1024 * 1024) {
            throw InferenceError("JSON_INPUT_TOO_LARGE");
        }
    }

    JsonValue parse() {
        JsonValue value = parse_value(0);
        skip_ws();
        if (pos_ != input_.size()) {
            throw InferenceError("JSON_TRAILING_CONTENT");
        }
        return value;
    }

private:
    std::string_view input_;
    size_t pos_ = 0;

    void skip_ws() {
        while (pos_ < input_.size() &&
               std::isspace(static_cast<unsigned char>(input_[pos_]))) {
            ++pos_;
        }
    }

    char peek() const {
        return pos_ < input_.size() ? input_[pos_] : '\0';
    }

    char take() {
        if (pos_ >= input_.size()) {
            throw InferenceError("JSON_UNEXPECTED_EOF");
        }
        return input_[pos_++];
    }

    void expect(char expected) {
        if (take() != expected) {
            throw InferenceError("JSON_UNEXPECTED_CHARACTER");
        }
    }

    bool consume(std::string_view literal) {
        if (input_.substr(pos_, literal.size()) == literal) {
            pos_ += literal.size();
            return true;
        }
        return false;
    }

    JsonValue parse_value(int depth) {
        if (depth > 128) {
            throw InferenceError("JSON_DEPTH_EXCEEDED");
        }
        skip_ws();
        const char ch = peek();
        if (ch == 'n') {
            if (!consume("null")) throw InferenceError("JSON_INVALID_LITERAL");
            return JsonValue{};
        }
        if (ch == 't') {
            if (!consume("true")) throw InferenceError("JSON_INVALID_LITERAL");
            JsonValue value;
            value.type = JsonValue::Type::Bool;
            value.boolean = true;
            return value;
        }
        if (ch == 'f') {
            if (!consume("false")) throw InferenceError("JSON_INVALID_LITERAL");
            JsonValue value;
            value.type = JsonValue::Type::Bool;
            return value;
        }
        if (ch == '"') {
            JsonValue value;
            value.type = JsonValue::Type::String;
            value.string = parse_string();
            return value;
        }
        if (ch == '[') {
            return parse_array(depth + 1);
        }
        if (ch == '{') {
            return parse_object(depth + 1);
        }
        return parse_number();
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        while (true) {
            const char ch = take();
            if (ch == '"') {
                return out;
            }
            if (static_cast<unsigned char>(ch) < 0x20) {
                throw InferenceError("JSON_UNESCAPED_CONTROL");
            }
            if (ch != '\\') {
                out.push_back(ch);
                continue;
            }
            const char esc = take();
            switch (esc) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'u': {
                    uint32_t cp = parse_hex4();
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        if (take() != '\\' || take() != 'u') {
                            throw InferenceError("JSON_INVALID_SURROGATE");
                        }
                        const uint32_t low = parse_hex4();
                        if (low < 0xDC00 || low > 0xDFFF) {
                            throw InferenceError("JSON_INVALID_SURROGATE");
                        }
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                    }
                    append_utf8(out, cp);
                    break;
                }
                default:
                    throw InferenceError("JSON_INVALID_ESCAPE");
            }
        }
    }

    uint32_t parse_hex4() {
        uint32_t value = 0;
        for (int i = 0; i < 4; ++i) {
            const char ch = take();
            value <<= 4;
            if (ch >= '0' && ch <= '9') value += static_cast<uint32_t>(ch - '0');
            else if (ch >= 'a' && ch <= 'f') value += static_cast<uint32_t>(ch - 'a' + 10);
            else if (ch >= 'A' && ch <= 'F') value += static_cast<uint32_t>(ch - 'A' + 10);
            else throw InferenceError("JSON_INVALID_UNICODE_ESCAPE");
        }
        return value;
    }

    JsonValue parse_number() {
        const size_t start = pos_;
        if (peek() == '-') ++pos_;
        if (!std::isdigit(static_cast<unsigned char>(peek()))) {
            throw InferenceError("JSON_INVALID_NUMBER");
        }
        if (peek() == '0') {
            ++pos_;
        } else {
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (peek() == '.') {
            ++pos_;
            if (!std::isdigit(static_cast<unsigned char>(peek()))) {
                throw InferenceError("JSON_INVALID_NUMBER");
            }
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            if (!std::isdigit(static_cast<unsigned char>(peek()))) {
                throw InferenceError("JSON_INVALID_NUMBER");
            }
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        JsonValue value;
        value.type = JsonValue::Type::Number;
        value.number = std::stod(std::string(input_.substr(start, pos_ - start)));
        return value;
    }

    JsonValue parse_array(int depth) {
        expect('[');
        JsonValue value;
        value.type = JsonValue::Type::Array;
        skip_ws();
        if (peek() == ']') {
            ++pos_;
            return value;
        }
        while (true) {
            value.array.push_back(parse_value(depth));
            skip_ws();
            const char ch = take();
            if (ch == ']') return value;
            if (ch != ',') throw InferenceError("JSON_ARRAY_SEPARATOR_EXPECTED");
        }
    }

    JsonValue parse_object(int depth) {
        expect('{');
        JsonValue value;
        value.type = JsonValue::Type::Object;
        skip_ws();
        if (peek() == '}') {
            ++pos_;
            return value;
        }
        while (true) {
            skip_ws();
            if (peek() != '"') throw InferenceError("JSON_OBJECT_KEY_EXPECTED");
            std::string key = parse_string();
            skip_ws();
            expect(':');
            value.object.emplace(std::move(key), parse_value(depth));
            skip_ws();
            const char ch = take();
            if (ch == '}') return value;
            if (ch != ',') throw InferenceError("JSON_OBJECT_SEPARATOR_EXPECTED");
        }
    }
};

const JsonValue& json_field(const JsonValue& object, const char* name) {
    if (object.type != JsonValue::Type::Object) {
        throw InferenceError("JSON_OBJECT_EXPECTED");
    }
    const auto it = object.object.find(name);
    if (it == object.object.end()) {
        throw InferenceError(std::string("JSON_FIELD_MISSING:") + name);
    }
    return it->second;
}

const JsonValue* json_optional(const JsonValue& object, const char* name) {
    if (object.type != JsonValue::Type::Object) return nullptr;
    const auto it = object.object.find(name);
    return it == object.object.end() ? nullptr : &it->second;
}

std::string json_string(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::String) {
        throw InferenceError(std::string("JSON_STRING_EXPECTED:") + name);
    }
    return value.string;
}

int64_t json_int(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::Number ||
        value.number < static_cast<double>(std::numeric_limits<int64_t>::min()) ||
        value.number > static_cast<double>(std::numeric_limits<int64_t>::max()) ||
        std::floor(value.number) != value.number) {
        throw InferenceError(std::string("JSON_INT_EXPECTED:") + name);
    }
    return static_cast<int64_t>(value.number);
}

double json_number(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::Number) {
        throw InferenceError(std::string("JSON_NUMBER_EXPECTED:") + name);
    }
    return value.number;
}

bool json_bool(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::Bool) {
        throw InferenceError(std::string("JSON_BOOL_EXPECTED:") + name);
    }
    return value.boolean;
}

std::vector<int64_t> json_shape(const JsonValue& value) {
    if (value.type != JsonValue::Type::Array) {
        throw InferenceError("TENSOR_SHAPE_EXPECTED");
    }
    std::vector<int64_t> shape;
    for (const JsonValue& dim : value.array) {
        if (dim.type != JsonValue::Type::Number || dim.number <= 0 ||
            std::floor(dim.number) != dim.number ||
            dim.number > static_cast<double>(std::numeric_limits<int64_t>::max())) {
            throw InferenceError("TENSOR_SHAPE_INVALID");
        }
        shape.push_back(static_cast<int64_t>(dim.number));
    }
    return shape;
}

std::vector<unsigned char> read_binary(const std::filesystem::path& path, int64_t max_bytes) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) {
        throw InferenceError("BUNDLE_FILE_UNREADABLE:" + path.string());
    }
    const std::streamoff size = input.tellg();
    if (size < 0 || size > max_bytes) {
        throw InferenceError("BUNDLE_FILE_TOO_LARGE");
    }
    std::vector<unsigned char> data(static_cast<size_t>(size));
    input.seekg(0, std::ios::beg);
    if (size > 0 && !input.read(reinterpret_cast<char*>(data.data()), size)) {
        throw InferenceError("BUNDLE_FILE_READ_FAILED");
    }
    return data;
}

std::string read_text(const std::filesystem::path& path, int64_t max_bytes) {
    const std::vector<unsigned char> bytes = read_binary(path, max_bytes);
    return std::string(reinterpret_cast<const char*>(bytes.data()), bytes.size());
}

std::string sha256_hex(const unsigned char* data, size_t size) {
#ifdef _WIN32
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    unsigned char digest[32] = {0};
    const NTSTATUS open_status = BCryptOpenAlgorithmProvider(
        &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0);
    if (open_status != 0) {
        throw InferenceError("SHA256_PROVIDER_UNAVAILABLE");
    }
    const NTSTATUS create_status = BCryptCreateHash(
        algorithm, &hash, nullptr, 0, nullptr, 0, 0);
    if (create_status != 0) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
        throw InferenceError("SHA256_INIT_FAILED");
    }
    const NTSTATUS update_status = BCryptHashData(
        hash, const_cast<unsigned char*>(data),
        static_cast<ULONG>(size), 0);
    const NTSTATUS finish_status = update_status == 0
        ? BCryptFinishHash(hash, digest, sizeof(digest), 0)
        : update_status;
    BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (finish_status != 0) {
        throw InferenceError("SHA256_HASH_FAILED");
    }
    static const char* digits = "0123456789abcdef";
    std::string out;
    out.reserve(64);
    for (unsigned char byte : digest) {
        out.push_back(digits[byte >> 4]);
        out.push_back(digits[byte & 0x0F]);
    }
    return out;
#else
    (void)data;
    (void)size;
    throw InferenceError("SHA256_UNSUPPORTED_PLATFORM");
#endif
}

int64_t checked_product(const std::vector<int64_t>& shape) {
    int64_t product = 1;
    for (const int64_t dim : shape) {
        if (dim <= 0 || product > std::numeric_limits<int64_t>::max() / dim) {
            throw InferenceError("TENSOR_SHAPE_OVERFLOW");
        }
        product *= dim;
    }
    return product;
}

std::vector<double> transpose_matrix(const TensorView& matrix) {
    if (matrix.shape.size() != 2) {
        throw InferenceError("MATRIX_TENSOR_EXPECTED");
    }
    const int64_t rows = matrix.shape[0];
    const int64_t cols = matrix.shape[1];
    std::vector<double> out(static_cast<size_t>(rows * cols));
    for (int64_t r = 0; r < rows; ++r) {
        for (int64_t c = 0; c < cols; ++c) {
            out[static_cast<size_t>(c * rows + r)] = matrix.data[r * cols + c];
        }
    }
    return out;
}

void checked_c_call(int rc, const char* operation) {
    if (rc != 0) {
        throw InferenceError(std::string("C_ABI_CALL_FAILED:") + operation);
    }
}

// CUDA acceleration track (independent of the four-language layering; the
// pure-C core is untouched). Runtime-enable only: XINGCHENG_CPP_CUDA=1 is
// read at engine load — the governed Python layer owns the decision after
// GPU coordination; the native layer only provides capability.
#if defined(XINGCHENG_CUDA)
extern "C" int xcuda_available();
extern "C" int xcuda_matmul_f64(
    const double* a, long long m, long long k,
    const double* b, long long n, double* out);
extern "C" int xcuda_release_weights();
extern "C" int xcuda_bf16_available();
extern "C" int xcuda_matmul_bf16(
    const double* a, long long m, long long k,
    const double* b, long long n, double* out);
// P1-1② device-resident KV + online-softmax attention (kernels/kv_attention.cu).
extern "C" int xcuda_kv_available();
extern "C" int xcuda_kv_alloc(
    long long layers, long long kv_heads, long long head_dim,
    long long max_len);
extern "C" void xcuda_kv_free();
extern "C" int xcuda_kv_write_rows(
    int is_k, long long layer, long long head, long long pos0,
    long long rows, const double* src);
extern "C" int xcuda_kv_attention(
    long long layer, const double* q, long long heads, long long seq,
    long long kv_heads, long long head_dim, long long position_offset,
    double* out, long long out_stride);
#endif

namespace {
std::atomic<bool> g_cuda_requested{false};
std::atomic<bool> g_cuda_bf16_requested{false};
std::atomic<bool> g_cuda_kv_requested{false};

bool env_flag(const char* name) {
    const char* value = std::getenv(name);
    if (value == nullptr) return false;
    const std::string v(value);
    return v == "1" || v == "true" || v == "TRUE" || v == "yes";
}
}  // namespace

std::vector<double> matmul(
    const double* a,
    int64_t m,
    int64_t k,
    const double* b,
    int64_t n) {
    std::vector<double> out(static_cast<size_t>(m * n));
#if defined(XINGCHENG_CUDA)
    if (g_cuda_requested.load()) {
        if (g_cuda_bf16_requested.load()) {
            if (xcuda_matmul_bf16(a, m, k, b, n, out.data()) != 0) {
                throw InferenceError("CUDA_BF16_MATMUL_FAILED");
            }
            return out;
        }
        if (xcuda_matmul_f64(a, m, k, b, n, out.data()) != 0) {
            throw InferenceError("CUDA_MATMUL_FAILED");
        }
        return out;
    }
#else
    if (g_cuda_requested.load()) {
        throw InferenceError("CUDA_UNAVAILABLE");
    }
#endif
    checked_c_call(
        gptbridge_native_transformer_matmul(a, m, k, b, k, n, out.data()),
        "matmul");
    return out;
}

std::vector<double> linear(
    const std::vector<double>& input,
    int64_t rows,
    int64_t in_features,
    const std::vector<double>& transposed_weight,
    int64_t out_features) {
    return matmul(input.data(), rows, in_features, transposed_weight.data(), out_features);
}

// R5 grouped GEMM: a holds the groups' row-blocks concatenated
// ([sum(group_rows) x k]); b_list[g] is group g's [k x n] weight; the
// concatenated [sum x n] outputs come back in group order. One C dispatch
// for the whole group loop; under a requested-CUDA build there is no
// grouped device entry, so the group loop dispatches per group instead.
std::vector<double> matmul_grouped(
    const std::vector<double>& a,
    const std::vector<int64_t>& group_rows,
    const std::vector<const double*>& b_list,
    int64_t k,
    int64_t n) {
    int64_t total_rows = 0;
    for (const int64_t rows : group_rows) total_rows += rows;
    std::vector<double> out(static_cast<size_t>(total_rows * n));
    if (g_cuda_requested.load()) {
#if defined(XINGCHENG_CUDA)
        int64_t a_off = 0;
        int64_t c_off = 0;
        for (size_t g = 0; g < group_rows.size(); ++g) {
            const int64_t m_g = group_rows[g];
            if (m_g == 0) continue;
            const int rc = g_cuda_bf16_requested.load()
                ? xcuda_matmul_bf16(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off)
                : xcuda_matmul_f64(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off);
            if (rc != 0) {
                throw InferenceError("CUDA_MATMUL_FAILED");
            }
            a_off += m_g * k;
            c_off += m_g * n;
        }
        return out;
#else
        throw InferenceError("CUDA_UNAVAILABLE");
#endif
    }
    checked_c_call(
        gptbridge_native_transformer_matmul_grouped(
            a.data(), group_rows.data(),
            static_cast<int64_t>(group_rows.size()), b_list.data(), k, n,
            out.data()),
        "matmul-grouped");
    return out;
}

// ── W1 attention kernels ───────────────────────────────────────────────
// Streaming Q·K dot and score·V accumulate used by the attention loop.
// AVX is used when the CPU supports it (runtime-detected once); scalar
// fallback keeps identical semantics on machines without AVX.

bool cpu_has_avx() {
#if XINGCHENG_W1_X64
    static const bool supported = [] {
#if defined(_MSC_VER)
        int regs[4] = {0, 0, 0, 0};
        __cpuidex(regs, 1, 0);
        const bool osxsave = (regs[2] & (1 << 27)) != 0;
        const bool avx = (regs[2] & (1 << 28)) != 0;
        if (!osxsave || !avx) return false;
        return (_xgetbv(0) & 0x6) == 0x6;
#else
        __builtin_cpu_init();
        return __builtin_cpu_supports("avx");
#endif
    }();
    return supported;
#else
    return false;
#endif
}

double dot_f64(const double* a, const double* b, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        __m256d acc0 = _mm256_setzero_pd();
        __m256d acc1 = _mm256_setzero_pd();
        int64_t i = 0;
        for (; i + 8 <= n; i += 8) {
            acc0 = _mm256_add_pd(acc0, _mm256_mul_pd(
                _mm256_loadu_pd(a + i), _mm256_loadu_pd(b + i)));
            acc1 = _mm256_add_pd(acc1, _mm256_mul_pd(
                _mm256_loadu_pd(a + i + 4), _mm256_loadu_pd(b + i + 4)));
        }
        acc0 = _mm256_add_pd(acc0, acc1);
        const __m128d pair = _mm_add_pd(
            _mm256_castpd256_pd128(acc0), _mm256_extractf128_pd(acc0, 1));
        double sum = _mm_cvtsd_f64(pair)
            + _mm_cvtsd_f64(_mm_unpackhi_pd(pair, pair));
        for (; i < n; ++i) sum += a[i] * b[i];
        return sum;
    }
#endif
    double sum = 0.0;
    for (int64_t i = 0; i < n; ++i) sum += a[i] * b[i];
    return sum;
}

void axpy_f64(double* out, double weight, const double* v, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        const __m256d wv = _mm256_set1_pd(weight);
        int64_t i = 0;
        for (; i + 4 <= n; i += 4) {
            _mm256_storeu_pd(out + i, _mm256_add_pd(
                _mm256_loadu_pd(out + i),
                _mm256_mul_pd(wv, _mm256_loadu_pd(v + i))));
        }
        for (; i < n; ++i) out[i] += weight * v[i];
        return;
    }
#endif
    for (int64_t i = 0; i < n; ++i) out[i] += weight * v[i];
}

// KV INT8 helpers: fp64 operand against packed int8 storage; the caller
// folds the per-token/per-head scale into the weight or the score.
// AVX2 mirrors the fp64 kernels — sign-extend 8 int8 lanes per step.
double dot_int8(const double* a, const int8_t* q, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        __m256d acc0 = _mm256_setzero_pd();
        __m256d acc1 = _mm256_setzero_pd();
        int64_t i = 0;
        for (; i + 8 <= n; i += 8) {
            const __m128i v8 = _mm_loadl_epi64(
                reinterpret_cast<const __m128i*>(q + i));
            const __m128i v32 = _mm_cvtepi8_epi32(v8);
            acc0 = _mm256_add_pd(acc0, _mm256_mul_pd(
                _mm256_loadu_pd(a + i), _mm256_cvtepi32_pd(v32)));
            acc1 = _mm256_add_pd(acc1, _mm256_mul_pd(
                _mm256_loadu_pd(a + i + 4),
                _mm256_cvtepi32_pd(_mm_srli_si128(v32, 8))));
        }
        acc0 = _mm256_add_pd(acc0, acc1);
        const __m128d pair = _mm_add_pd(
            _mm256_castpd256_pd128(acc0), _mm256_extractf128_pd(acc0, 1));
        double sum = _mm_cvtsd_f64(pair)
            + _mm_cvtsd_f64(_mm_unpackhi_pd(pair, pair));
        for (; i < n; ++i) sum += a[i] * static_cast<double>(q[i]);
        return sum;
    }
#endif
    double sum = 0.0;
    for (int64_t i = 0; i < n; ++i) sum += a[i] * static_cast<double>(q[i]);
    return sum;
}

void axpy_int8(double* out, double weight, const int8_t* q, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        const __m256d wv = _mm256_set1_pd(weight);
        int64_t i = 0;
        for (; i + 8 <= n; i += 8) {
            const __m128i v8 = _mm_loadl_epi64(
                reinterpret_cast<const __m128i*>(q + i));
            const __m128i v32 = _mm_cvtepi8_epi32(v8);
            _mm256_storeu_pd(out + i, _mm256_add_pd(
                _mm256_loadu_pd(out + i),
                _mm256_mul_pd(wv, _mm256_cvtepi32_pd(v32))));
            _mm256_storeu_pd(out + i + 4, _mm256_add_pd(
                _mm256_loadu_pd(out + i + 4),
                _mm256_mul_pd(wv, _mm256_cvtepi32_pd(_mm_srli_si128(v32, 8)))));
        }
        for (; i < n; ++i) out[i] += weight * static_cast<double>(q[i]);
        return;
    }
#endif
    for (int64_t i = 0; i < n; ++i) out[i] += weight * static_cast<double>(q[i]);
}

std::vector<double> rmsnorm(
    const std::vector<double>& input,
    int64_t rows,
    int64_t cols,
    const TensorView& weight,
    double eps) {
    std::vector<double> out(input.size());
    checked_c_call(
        gptbridge_native_transformer_rmsnorm(
            input.data(), rows, cols, weight.data, eps, out.data()),
        "rmsnorm");
    return out;
}

void rope_tables(
    int64_t seq_len,
    int64_t offset,
    int64_t dim,
    double theta,
    std::vector<double>& cos_out,
    std::vector<double>& sin_out) {
    cos_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    sin_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    const int64_t half = dim / 2;
    for (int64_t s = 0; s < seq_len; ++s) {
        const double position = static_cast<double>(offset + s);
        for (int64_t i = 0; i < half; ++i) {
            const double exponent = -2.0 * static_cast<double>(i) / static_cast<double>(dim);
            const double angle = position * std::pow(theta, exponent);
            cos_out[static_cast<size_t>(s * dim + i)] = std::cos(angle);
            cos_out[static_cast<size_t>(s * dim + i + half)] = std::cos(angle);
            sin_out[static_cast<size_t>(s * dim + i)] = std::sin(angle);
            sin_out[static_cast<size_t>(s * dim + i + half)] = std::sin(angle);
        }
    }
}

std::string json_escape(const std::string& text) {
    std::string out;
    for (const unsigned char ch : text) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20) {
                    constexpr char digits[] = "0123456789abcdef";
                    out += "\\u00";
                    out.push_back(digits[ch >> 4]);
                    out.push_back(digits[ch & 0x0F]);
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    return out;
}

}  // namespace

// ── Weight blob memory mapping (P3b) ──────────────────────────────────

struct WeightBundle::Blob {
    std::vector<unsigned char> fallback;
    const unsigned char* data = nullptr;
    size_t size = 0;
#ifdef _WIN32
    HANDLE file = INVALID_HANDLE_VALUE;
    HANDLE mapping = nullptr;
    void* view = nullptr;
#endif

    ~Blob() {
#ifdef _WIN32
        if (view != nullptr) UnmapViewOfFile(view);
        if (mapping != nullptr) CloseHandle(mapping);
        if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
#endif
    }
};

WeightBundle::~WeightBundle() = default;
WeightBundle::WeightBundle(WeightBundle&&) noexcept = default;
WeightBundle& WeightBundle::operator=(WeightBundle&&) noexcept = default;

std::unique_ptr<WeightBundle::Blob> map_readonly_file(
    const std::filesystem::path& path,
    int64_t max_bytes) {
    auto blob = std::make_unique<WeightBundle::Blob>();
#ifdef _WIN32
    blob->file = CreateFileW(
        path.wstring().c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (blob->file == INVALID_HANDLE_VALUE) {
        throw InferenceError("BUNDLE_FILE_UNREADABLE:" + path.string());
    }
    LARGE_INTEGER file_size{};
    if (!GetFileSizeEx(blob->file, &file_size) ||
        file_size.QuadPart < 0 || file_size.QuadPart > max_bytes) {
        throw InferenceError("BUNDLE_FILE_TOO_LARGE");
    }
    blob->size = static_cast<size_t>(file_size.QuadPart);
    if (blob->size > 0) {
        blob->mapping = CreateFileMappingW(
            blob->file, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (blob->mapping == nullptr) {
            throw InferenceError("BUNDLE_MMAP_FAILED");
        }
        blob->view = MapViewOfFile(blob->mapping, FILE_MAP_READ, 0, 0, 0);
        if (blob->view == nullptr) {
            throw InferenceError("BUNDLE_MMAP_VIEW_FAILED");
        }
        blob->data = static_cast<const unsigned char*>(blob->view);
    }
    return blob;
#else
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) throw InferenceError("BUNDLE_FILE_UNREADABLE:" + path.string());
    const std::streamoff size = input.tellg();
    if (size < 0 || size > max_bytes) throw InferenceError("BUNDLE_FILE_TOO_LARGE");
    blob->fallback.resize(static_cast<size_t>(size));
    input.seekg(0, std::ios::beg);
    if (size > 0 &&
        !input.read(reinterpret_cast<char*>(blob->fallback.data()),
                    static_cast<std::streamsize>(size))) {
        throw InferenceError("BUNDLE_FILE_READ_FAILED");
    }
    blob->data = blob->fallback.data();
    blob->size = blob->fallback.size();
    return blob;
#endif
}

// ── Weight bundle (P3b) ───────────────────────────────────────────────

int64_t TensorView::size() const {
    return shape.empty() ? 0 : checked_product(shape);
}

WeightBundle WeightBundle::load(const std::string& manifest_path) {
    WeightBundle bundle;
    const std::filesystem::path manifest_file(manifest_path);
    const JsonValue manifest = JsonParser(read_text(manifest_file, 64 * 1024 * 1024)).parse();
    if (json_string(manifest, "schema_version") != "star-native-inference-bundle/v1") {
        throw InferenceError("BUNDLE_SCHEMA_UNSUPPORTED");
    }

    const JsonValue& config_json = json_field(manifest, "config");
    ModelConfig& cfg = bundle.config_;
    cfg.vocab_size = json_int(config_json, "vocab_size");
    cfg.hidden_size = json_int(config_json, "hidden_size");
    cfg.intermediate_size = json_int(config_json, "intermediate_size");
    cfg.num_hidden_layers = json_int(config_json, "num_hidden_layers");
    cfg.num_attention_heads = json_int(config_json, "num_attention_heads");
    cfg.num_key_value_heads = json_int(config_json, "num_key_value_heads");
    cfg.head_dim = json_int(config_json, "head_dim");
    cfg.max_position_embeddings = json_int(config_json, "max_position_embeddings");
    cfg.bos_token_id = json_int(config_json, "bos_token_id");
    cfg.eos_token_id = json_int(config_json, "eos_token_id");
    cfg.pad_token_id = json_int(config_json, "pad_token_id");
    cfg.rms_norm_eps = json_number(config_json, "rms_norm_eps");
    cfg.rope_theta = json_number(config_json, "rope_theta");
    cfg.use_swiglu = json_bool(config_json, "use_swiglu");
    cfg.tie_word_embeddings = json_bool(config_json, "tie_word_embeddings");
    cfg.norm_type = json_string(config_json, "norm_type");
    cfg.hidden_act = json_string(config_json, "hidden_act");
    cfg.position_embedding_type = json_string(config_json, "position_embedding_type");
    cfg.use_moe = json_bool(config_json, "use_moe");
    // MoE shape fields are optional in the manifest: bundles exported
    // before R5 predate them and always carry use_moe=false.
    if (const JsonValue* v = json_optional(config_json, "moe_num_experts")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_num_experts");
        cfg.moe_num_experts = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "moe_top_k")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_top_k");
        cfg.moe_top_k = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "moe_layer_interval")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_layer_interval");
        cfg.moe_layer_interval = static_cast<int64_t>(v->number);
    }
    cfg.quantization = json_string(config_json, "quantization");

    const std::string weights_name = json_string(manifest, "weights_file");
    bundle.weights_sha256_ = json_string(manifest, "weights_sha256");
    const std::filesystem::path weights_path = manifest_file.parent_path() / weights_name;
    bundle.blob_ = map_readonly_file(weights_path, 16LL * 1024 * 1024 * 1024);
    bundle.weights_bytes_ = static_cast<int64_t>(bundle.blob_->size);
    if (sha256_hex(bundle.blob_->data, bundle.blob_->size) != bundle.weights_sha256_) {
        throw InferenceError("BUNDLE_WEIGHTS_SHA256_MISMATCH");
    }

    const JsonValue& tensors = json_field(manifest, "tensors");
    if (tensors.type != JsonValue::Type::Object) {
        throw InferenceError("BUNDLE_TENSORS_OBJECT_EXPECTED");
    }
    for (const auto& [name, info] : tensors.object) {
        if (info.type != JsonValue::Type::Object) {
            throw InferenceError("TENSOR_INFO_INVALID");
        }
        const std::string dtype = json_string(info, "dtype");
        if (dtype != "float64" && dtype != "int8" && dtype != "int4_packed") {
            throw InferenceError("TENSOR_DTYPE_UNSUPPORTED:" + name);
        }
        const std::string endian = json_string(info, "endianness");
        if (endian != "little") {
            throw InferenceError("TENSOR_ENDIANNESS_UNSUPPORTED:" + name);
        }
        TensorInfo item;
        item.offset = json_int(info, "offset");
        item.bytes = json_int(info, "bytes");
        item.shape = json_shape(json_field(info, "shape"));
        const int64_t elements = checked_product(item.shape);
        int64_t expected_bytes = elements * 8;
        if (dtype == "int8") {
            expected_bytes = elements;
        } else if (dtype == "int4_packed") {
            if (item.shape.size() != 2) {
                throw InferenceError("TENSOR_INT4_SHAPE_UNSUPPORTED:" + name);
            }
            const int64_t rows = elements / item.shape.back();
            expected_bytes = rows * ((item.shape.back() + 1) / 2);
        }
        if (item.offset < 0 || item.bytes != expected_bytes ||
            item.offset > bundle.weights_bytes_ ||
            item.bytes > bundle.weights_bytes_ - item.offset) {
            throw InferenceError("TENSOR_BOUNDS_INVALID:" + name);
        }
        TensorView view;
        view.shape = item.shape;
        if (dtype == "float64") {
            view.data = reinterpret_cast<const double*>(
                bundle.blob_->data + item.offset);
        } else {
            // Weight-only per-tensor symmetric quantization (mirrors
            // kernels/quant.py): dequantize once at load into owned fp64
            // storage so every downstream GEMM is unchanged.
            const JsonValue* scale_v = json_optional(info, "scale");
            if (scale_v == nullptr ||
                scale_v->type != JsonValue::Type::Number ||
                !(scale_v->number > 0.0)) {
                throw InferenceError("TENSOR_SCALE_INVALID:" + name);
            }
            const double scale = scale_v->number;
            const unsigned char* raw = bundle.blob_->data + item.offset;
            bundle.owned_tensors_.emplace_back(
                static_cast<size_t>(elements));
            std::vector<double>& dst = bundle.owned_tensors_.back();
            if (dtype == "int8") {
                for (int64_t i = 0; i < elements; ++i) {
                    dst[static_cast<size_t>(i)] =
                        static_cast<double>(
                            reinterpret_cast<const int8_t*>(raw)[i]) * scale;
                }
            } else {
                // int4_packed: two 4-bit values per byte along the last dim
                // (low nibble = even index, high nibble = odd), shifted +8.
                const int64_t last = item.shape.back();
                const int64_t rows = elements / last;
                const int64_t packed_row = (last + 1) / 2;
                for (int64_t r = 0; r < rows; ++r) {
                    const unsigned char* prow = raw + r * packed_row;
                    double* drow = dst.data() + r * last;
                    for (int64_t c = 0; c < last; ++c) {
                        const unsigned char byte = prow[c / 2];
                        const int64_t nibble =
                            (c % 2 == 0) ? (byte & 0x0F) : (byte >> 4);
                        drow[c] = static_cast<double>(nibble - 8) * scale;
                    }
                }
            }
            view.data = dst.data();
        }
        bundle.tensors_.emplace(name, item);
        bundle.views_.emplace(name, view);
    }
    return bundle;
}

const TensorView& WeightBundle::tensor(const std::string& name) const {
    const auto it = views_.find(name);
    if (it == views_.end()) {
        throw InferenceError("TENSOR_MISSING:" + name);
    }
    return it->second;
}

bool WeightBundle::has_tensor(const std::string& name) const {
    return views_.find(name) != views_.end();
}

std::vector<std::string> WeightBundle::tensor_names() const {
    std::vector<std::string> names;
    names.reserve(views_.size());
    for (const auto& [name, unused] : views_) {
        (void)unused;
        names.push_back(name);
    }
    std::sort(names.begin(), names.end());
    return names;
}

// ── Tokenizer (P3c) ───────────────────────────────────────────────────

ByteLevelBPETokenizer ByteLevelBPETokenizer::load(const std::string& tokenizer_json_path) {
    ByteLevelBPETokenizer tokenizer;
    const JsonValue root = JsonParser(read_text(tokenizer_json_path, 64 * 1024 * 1024)).parse();
    const JsonValue& model = json_field(root, "model");
    const JsonValue& vocab = json_field(model, "vocab");
    if (vocab.type != JsonValue::Type::Object) {
        throw InferenceError("TOKENIZER_VOCAB_INVALID");
    }
    int64_t max_id = -1;
    for (const auto& [token, id_json] : vocab.object) {
        if (id_json.type != JsonValue::Type::Number || id_json.number < 0 ||
            std::floor(id_json.number) != id_json.number) {
            throw InferenceError("TOKENIZER_VOCAB_ID_INVALID");
        }
        const int64_t id = static_cast<int64_t>(id_json.number);
        tokenizer.vocab_.emplace(token, id);
        max_id = std::max(max_id, id);
    }
    tokenizer.vocab_size_ = max_id + 1;
    tokenizer.id_to_token_.assign(static_cast<size_t>(tokenizer.vocab_size_), "");
    for (const auto& [token, id] : tokenizer.vocab_) {
        tokenizer.id_to_token_[static_cast<size_t>(id)] = token;
    }

    const JsonValue* merges = json_optional(model, "merges");
    if (merges != nullptr) {
        if (merges->type != JsonValue::Type::Array) {
            throw InferenceError("TOKENIZER_MERGES_INVALID");
        }
        for (const JsonValue& item : merges->array) {
            std::pair<std::string, std::string> pair;
            if (item.type == JsonValue::Type::String) {
                const size_t split = item.string.find(' ');
                if (split == std::string::npos || split == 0 ||
                    split + 1 >= item.string.size()) {
                    throw InferenceError("TOKENIZER_MERGE_FORMAT_INVALID");
                }
                pair = std::make_pair(
                    item.string.substr(0, split),
                    item.string.substr(split + 1));
            } else if (
                item.type == JsonValue::Type::Array && item.array.size() == 2 &&
                item.array[0].type == JsonValue::Type::String &&
                item.array[1].type == JsonValue::Type::String) {
                // HF tokenizers serializes merges as ["left","right"] pairs.
                pair = std::make_pair(item.array[0].string, item.array[1].string);
            } else {
                throw InferenceError("TOKENIZER_MERGE_INVALID");
            }
            tokenizer.merge_rank_.emplace(
                pair.first + "\x1f" + pair.second,
                static_cast<int64_t>(tokenizer.merges_.size()));
            tokenizer.merges_.push_back(std::move(pair));
        }
    }

    std::vector<int> printable;
    for (int b = 33; b <= 126; ++b) printable.push_back(b);
    for (int b = 161; b <= 172; ++b) printable.push_back(b);
    for (int b = 174; b <= 255; ++b) printable.push_back(b);
    std::unordered_set<int> printable_set(printable.begin(), printable.end());
    tokenizer.byte_to_token_.assign(256, "");
    int extra = 0;
    for (int b = 0; b < 256; ++b) {
        uint32_t cp = static_cast<uint32_t>(b);
        if (!printable_set.count(b)) {
            cp = static_cast<uint32_t>(256 + extra++);
        }
        append_utf8(tokenizer.byte_to_token_[static_cast<size_t>(b)], cp);
        tokenizer.token_to_byte_[tokenizer.byte_to_token_[static_cast<size_t>(b)]] =
            static_cast<unsigned char>(b);
    }

    const std::vector<std::string> specials = {
        "<|pad|>", "<|bos|>", "<|eos|>", "<|unk|>", "<|system|>",
        "<|user|>", "<|assistant|>", "<|tool|>", "<|eot|>",
    };
    for (const std::string& text : specials) {
        const auto it = tokenizer.vocab_.find(text);
        if (it != tokenizer.vocab_.end()) {
            tokenizer.special_tokens_.push_back({text, it->second});
        }
    }
    std::sort(
        tokenizer.special_tokens_.begin(), tokenizer.special_tokens_.end(),
        [](const SpecialToken& a, const SpecialToken& b) { return a.text.size() > b.text.size(); });
    return tokenizer;
}

std::vector<int64_t> ByteLevelBPETokenizer::encode(
    const std::string& text,
    bool add_bos,
    bool add_eos,
    int64_t max_length) const {
    std::vector<int64_t> ids;
    if (add_bos) ids.push_back(1);

    size_t pos = 0;
    while (pos < text.size()) {
        const SpecialToken* matched = nullptr;
        for (const SpecialToken& special : special_tokens_) {
            if (text.compare(pos, special.text.size(), special.text) == 0) {
                matched = &special;
                break;
            }
        }
        if (matched != nullptr) {
            ids.push_back(matched->id);
            pos += matched->text.size();
            continue;
        }
        size_t end = pos;
        while (end < text.size()) {
            bool is_special = false;
            for (const SpecialToken& special : special_tokens_) {
                if (text.compare(end, special.text.size(), special.text) == 0) {
                    is_special = true;
                    break;
                }
            }
            if (is_special) break;
            ++end;
        }
        const std::vector<int64_t> segment = encode_segment(text.substr(pos, end - pos));
        ids.insert(ids.end(), segment.begin(), segment.end());
        pos = end;
    }

    if (add_eos) ids.push_back(2);
    if (max_length > 0 && static_cast<int64_t>(ids.size()) > max_length) {
        ids.resize(static_cast<size_t>(max_length));
        if (add_eos && !ids.empty()) ids.back() = 2;
    }
    return ids;
}

std::vector<int64_t> ByteLevelBPETokenizer::encode_segment(const std::string& text) const {
    std::vector<std::string> pieces;
    pieces.reserve(text.size());
    for (const unsigned char byte : text) {
        pieces.push_back(byte_to_token_[byte]);
    }
    while (pieces.size() > 1) {
        int64_t best_rank = std::numeric_limits<int64_t>::max();
        size_t best_index = pieces.size();
        for (size_t i = 0; i + 1 < pieces.size(); ++i) {
            const auto it = merge_rank_.find(pieces[i] + "\x1f" + pieces[i + 1]);
            if (it != merge_rank_.end() && it->second < best_rank) {
                best_rank = it->second;
                best_index = i;
            }
        }
        if (best_index == pieces.size()) break;
        pieces[best_index] += pieces[best_index + 1];
        pieces.erase(pieces.begin() + static_cast<std::ptrdiff_t>(best_index + 1));
    }
    std::vector<int64_t> ids;
    ids.reserve(pieces.size());
    for (const std::string& piece : pieces) {
        const auto it = vocab_.find(piece);
        ids.push_back(it == vocab_.end() ? 3 : it->second);
    }
    return ids;
}

std::string ByteLevelBPETokenizer::decode(
    const std::vector<int64_t>& ids,
    bool skip_special) const {
    const std::unordered_set<int64_t> special_ids = {0, 1, 2, 3, 4, 5, 6, 7, 8};
    std::string bytes;
    for (const int64_t id : ids) {
        if (id < 0 || id >= static_cast<int64_t>(id_to_token_.size())) {
            if (!skip_special) bytes.push_back(' ');
            continue;
        }
        const std::string& token = id_to_token_[static_cast<size_t>(id)];
        if (skip_special && special_ids.count(id)) continue;
        for (size_t i = 0; i < token.size();) {
            const unsigned char lead = static_cast<unsigned char>(token[i]);
            size_t width = 1;
            if ((lead & 0xE0) == 0xC0) width = 2;
            else if ((lead & 0xF0) == 0xE0) width = 3;
            else if ((lead & 0xF8) == 0xF0) width = 4;
            if (i + width > token.size()) {
                bytes.push_back(' ');
                break;
            }
            const std::string unit = token.substr(i, width);
            const auto it = token_to_byte_.find(unit);
            if (it != token_to_byte_.end()) {
                bytes.push_back(static_cast<char>(it->second));
            } else {
                bytes.append(unit);
            }
            i += width;
        }
    }
    return bytes;
}

// ── Engine (P3d–P3f) ──────────────────────────────────────────────────

NativeInferenceEngine::~NativeInferenceEngine() { unload(); }

void NativeInferenceEngine::load(const std::string& bundle_dir) {
    unload();
    const std::filesystem::path root(bundle_dir);
    bundle_ = std::make_unique<WeightBundle>(WeightBundle::load((root / "manifest.json").string()));
    const ModelConfig& cfg = bundle_->config();
    validate_supported();
    // CUDA opt-in is decided by the governed layer via env; requesting it
    // without a CUDA build or device fails closed at load.
    g_cuda_requested.store(env_flag("XINGCHENG_CPP_CUDA"));
    if (g_cuda_requested.load()) {
#if defined(XINGCHENG_CUDA)
        if (!xcuda_available()) {
            g_cuda_requested.store(false);
            throw InferenceError("CUDA_UNAVAILABLE");
        }
#else
        g_cuda_requested.store(false);
        throw InferenceError("CUDA_UNAVAILABLE");
#endif
    }
    // bf16 GEMM is a further opt-in on the CUDA path (P1-1③): requested but
    // kernels/device absent → fail closed at load, never silent fp64.
    g_cuda_bf16_requested.store(env_flag("XINGCHENG_CPP_CUDA_BF16"));
    if (g_cuda_bf16_requested.load()) {
#if defined(XINGCHENG_CUDA)
        if (!g_cuda_requested.load() || !xcuda_bf16_available()) {
            g_cuda_bf16_requested.store(false);
            throw InferenceError("CUDA_BF16_UNAVAILABLE");
        }
#else
        g_cuda_bf16_requested.store(false);
        throw InferenceError("CUDA_BF16_UNAVAILABLE");
#endif
    }
    // P1-1② device-resident KV: opt-in on the CUDA path; the int8 KV format
    // has no device representation, so requesting both fails closed.
    g_cuda_kv_requested.store(env_flag("XINGCHENG_CPP_CUDA_KV"));
    if (g_cuda_kv_requested.load()) {
#if defined(XINGCHENG_CUDA)
        if (!g_cuda_requested.load() || !xcuda_kv_available()) {
            g_cuda_kv_requested.store(false);
            throw InferenceError("CUDA_KV_UNAVAILABLE");
        }
#else
        g_cuda_kv_requested.store(false);
        throw InferenceError("CUDA_KV_UNAVAILABLE");
#endif
    }

    embedding_ = bundle_->tensor("model.embeddings.word_embeddings.weight");
    final_norm_ = bundle_->tensor("model.final_norm.weight");
    lm_head_ = bundle_->has_tensor("lm_head.weight")
        ? bundle_->tensor("lm_head.weight")
        : embedding_;
    lm_head_t_ = transpose_matrix(lm_head_);

    layers_.assign(static_cast<size_t>(cfg.num_hidden_layers), LayerWeights{});
    for (int64_t i = 0; i < cfg.num_hidden_layers; ++i) {
        LayerWeights& layer = layers_[static_cast<size_t>(i)];
        const std::string prefix = "model.layers." + std::to_string(i) + ".";
        layer.input_norm = bundle_->tensor(prefix + "input_norm.weight");
        layer.q_proj = bundle_->tensor(prefix + "attention.q_proj.weight");
        layer.k_proj = bundle_->tensor(prefix + "attention.k_proj.weight");
        layer.v_proj = bundle_->tensor(prefix + "attention.v_proj.weight");
        layer.o_proj = bundle_->tensor(prefix + "attention.o_proj.weight");
        layer.post_norm = bundle_->tensor(prefix + "post_attention_norm.weight");
        layer.is_moe = cfg.use_moe &&
            (i % std::max<int64_t>(1, cfg.moe_layer_interval)) == 0;
        if (layer.is_moe) {
            // R5 sparse MoE (token-choice routing, mirrors
            // modules/moe.py): router Linear + per-expert SwiGLU MLPs.
            layer.router = bundle_->tensor(prefix + "mlp.router.weight");
            layer.router_t = transpose_matrix(layer.router);
            const int64_t experts = cfg.moe_num_experts;
            layer.expert_gate.reserve(static_cast<size_t>(experts));
            layer.expert_up.reserve(static_cast<size_t>(experts));
            layer.expert_down.reserve(static_cast<size_t>(experts));
            layer.expert_gate_t.reserve(static_cast<size_t>(experts));
            layer.expert_up_t.reserve(static_cast<size_t>(experts));
            layer.expert_down_t.reserve(static_cast<size_t>(experts));
            for (int64_t e = 0; e < experts; ++e) {
                const std::string ep =
                    prefix + "mlp.experts." + std::to_string(e) + ".";
                layer.expert_gate.push_back(
                    bundle_->tensor(ep + "gate_proj.weight"));
                layer.expert_up.push_back(
                    bundle_->tensor(ep + "up_proj.weight"));
                layer.expert_down.push_back(
                    bundle_->tensor(ep + "down_proj.weight"));
                layer.expert_gate_t.push_back(
                    transpose_matrix(layer.expert_gate.back()));
                layer.expert_up_t.push_back(
                    transpose_matrix(layer.expert_up.back()));
                layer.expert_down_t.push_back(
                    transpose_matrix(layer.expert_down.back()));
            }
        } else {
            layer.gate_proj = bundle_->tensor(prefix + "mlp.gate_proj.weight");
            layer.up_proj = bundle_->tensor(prefix + "mlp.up_proj.weight");
            layer.down_proj = bundle_->tensor(prefix + "mlp.down_proj.weight");
            layer.gate_proj_t = transpose_matrix(layer.gate_proj);
            layer.up_proj_t = transpose_matrix(layer.up_proj);
            layer.down_proj_t = transpose_matrix(layer.down_proj);
        }
        layer.q_proj_t = transpose_matrix(layer.q_proj);
        layer.k_proj_t = transpose_matrix(layer.k_proj);
        layer.v_proj_t = transpose_matrix(layer.v_proj);
        layer.o_proj_t = transpose_matrix(layer.o_proj);
    }

    const int64_t kv_dim = cfg.num_key_value_heads * cfg.head_dim;
    // KV INT8 (opt-in): per-token/per-head symmetric quantization shrinks
    // the packed element stride ~8x; the governed layer owns the env flag.
    kv_int8_ = env_flag("XINGCHENG_CPP_KV_INT8");
    kv_elem_stride_bytes_ = kv_int8_
        ? ((cfg.head_dim + 7) & ~int64_t{7}) + 8
        : cfg.head_dim * static_cast<int64_t>(sizeof(double));
    const int64_t kv_bytes = kv_int8_
        ? cfg.num_hidden_layers * cfg.max_position_embeddings *
              cfg.num_key_value_heads * kv_elem_stride_bytes_ * 2
        : cfg.num_hidden_layers * cfg.max_position_embeddings * kv_dim * 8 * 2;
    if (kv_limit_bytes_ > 0 && kv_bytes > kv_limit_bytes_) {
        throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
    }
    // R6 paged KV: allocate on demand instead of the worst-case footprint.
    kv_block_stride_ = kv_int8_
        ? (cfg.num_hidden_layers * kKvBlockTokens *
               cfg.num_key_value_heads * kv_elem_stride_bytes_ + 7) /
              8
        : cfg.num_hidden_layers * kKvBlockTokens * kv_dim;
    if (kv_pool_ != nullptr) {
        gptbridge_kv_pool_destroy(kv_pool_);
        kv_pool_ = nullptr;
    }
    kv_pool_ = gptbridge_kv_pool_create(kv_block_stride_, kv_limit_bytes_);
    if (kv_pool_ == nullptr) throw InferenceError("KV_POOL_CREATE_FAILED");
    kv_block_tables_.clear();
    kv_slot_active_.clear();
    kv_lens_.clear();

    // Device-resident KV mirror (P1-1②): fp64 device buffers sized to the
    // worst-case footprint; writes are mirrored per row for slot 0 only.
    // int8 KV has no device format — the combination fails closed rather
    // than silently serving a different precision than requested.
    kv_device_active_ = false;
#if defined(XINGCHENG_CUDA)
    if (g_cuda_kv_requested.load()) {
        if (kv_int8_) throw InferenceError("CUDA_KV_UNSUPPORTED_CONFIG");
        if (xcuda_kv_alloc(
                cfg.num_hidden_layers, cfg.num_key_value_heads,
                cfg.head_dim, cfg.max_position_embeddings) != 0) {
            throw InferenceError("CUDA_KV_UNAVAILABLE");
        }
        kv_device_active_ = true;
    }
#endif

    const std::filesystem::path tokenizer_path = root / "tokenizer.json";
    if (std::filesystem::exists(tokenizer_path)) {
        tokenizer_ = std::make_unique<ByteLevelBPETokenizer>(
            ByteLevelBPETokenizer::load(tokenizer_path.string()));
    }
}

bool NativeInferenceEngine::cuda_active() const {
    return g_cuda_requested.load();
}

void NativeInferenceEngine::unload() {
    bundle_.reset();
    tokenizer_.reset();
#if defined(XINGCHENG_CUDA)
    if (g_cuda_requested.load()) xcuda_release_weights();
#endif
    g_cuda_requested.store(false);
    g_cuda_kv_requested.store(false);
    kv_device_active_ = false;
    layers_.clear();
    prefix_cache_.clear();
    prefix_tick_ = 0;
    prefix_hits_ = 0;
    prefix_misses_ = 0;
    if (kv_pool_ != nullptr) {
        gptbridge_kv_pool_destroy(kv_pool_);
        kv_pool_ = nullptr;
    }
    kv_block_tables_.clear();
    kv_slot_active_.clear();
    kv_lens_.clear();
    kv_block_stride_ = 0;
    kv_int8_ = false;
    kv_elem_stride_bytes_ = 0;
    lm_head_t_.clear();
    sequence_.clear();
}

void NativeInferenceEngine::validate_supported() const {
    const ModelConfig& cfg = bundle_->config();
    if (cfg.use_moe &&
        (cfg.moe_num_experts < 2 || cfg.moe_top_k < 1 ||
         cfg.moe_top_k > cfg.moe_num_experts || cfg.moe_layer_interval < 1)) {
        throw InferenceError("MOE_CONFIG_UNSUPPORTED");
    }
    if (cfg.quantization != "none" && cfg.quantization != "int8" &&
        cfg.quantization != "int4") {
        throw InferenceError("QUANTIZED_INFERENCE_UNSUPPORTED");
    }
    if (cfg.norm_type != "rmsnorm") throw InferenceError("NORM_TYPE_UNSUPPORTED");
    if (!cfg.use_swiglu || cfg.hidden_act != "silu") {
        throw InferenceError("MLP_TYPE_UNSUPPORTED");
    }
    if (cfg.position_embedding_type != "rope" && cfg.position_embedding_type != "learned") {
        throw InferenceError("POSITION_EMBEDDING_UNSUPPORTED");
    }
    if (cfg.hidden_size <= 0 || cfg.head_dim <= 0 ||
        cfg.hidden_size != cfg.num_attention_heads * cfg.head_dim ||
        cfg.num_key_value_heads <= 0 ||
        cfg.num_attention_heads % cfg.num_key_value_heads != 0) {
        throw InferenceError("MODEL_SHAPE_UNSUPPORTED");
    }
}

std::vector<int64_t> NativeInferenceEngine::encode(
    const std::string& text,
    bool add_bos,
    bool add_eos,
    int64_t max_length) const {
    if (tokenizer_ == nullptr) {
        throw InferenceError("TOKENIZER_NOT_LOADED");
    }
    return tokenizer_->encode(text, add_bos, add_eos, max_length);
}

std::string NativeInferenceEngine::decode(
    const std::vector<int64_t>& ids,
    bool skip_special) const {
    if (tokenizer_ == nullptr) {
        throw InferenceError("TOKENIZER_NOT_LOADED");
    }
    return tokenizer_->decode(ids, skip_special);
}

int32_t NativeInferenceEngine::kv_alloc_block() {
    if (kv_pool_ == nullptr) throw InferenceError("KV_POOL_NOT_INITIALIZED");
    const int32_t id = gptbridge_kv_pool_alloc(kv_pool_);
    if (id < 0) throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
    return id;
}

int64_t NativeInferenceEngine::kv_alloc_slot() {
    // R9: bounded per-sequence KV namespaces over the shared pool.
    for (int64_t i = 0; i < static_cast<int64_t>(kv_slot_active_.size()); ++i) {
        if (!kv_slot_active_[static_cast<size_t>(i)]) {
            kv_slot_active_[static_cast<size_t>(i)] = true;
            kv_block_tables_[static_cast<size_t>(i)].clear();
            kv_lens_[static_cast<size_t>(i)] = 0;
            return i;
        }
    }
    if (static_cast<int64_t>(kv_slot_active_.size()) >= kMaxBatchSeqs) {
        throw InferenceError("BATCH_SLOT_EXHAUSTED");
    }
    kv_slot_active_.push_back(true);
    kv_block_tables_.emplace_back();
    kv_lens_.push_back(0);
    return static_cast<int64_t>(kv_slot_active_.size()) - 1;
}

void NativeInferenceEngine::kv_free_slot(int64_t slot) {
    if (slot < 0 || slot >= static_cast<int64_t>(kv_block_tables_.size())) {
        return;
    }
    for (const int32_t block : kv_block_tables_[static_cast<size_t>(slot)]) {
        gptbridge_kv_pool_release(kv_pool_, block);
    }
    kv_block_tables_[static_cast<size_t>(slot)].clear();
    kv_lens_[static_cast<size_t>(slot)] = 0;
    kv_slot_active_[static_cast<size_t>(slot)] = false;
}

void NativeInferenceEngine::kv_ensure_position(int64_t slot, int64_t position) {
    const int64_t block_index = position / kKvBlockTokens;
    std::vector<int32_t>& table = kv_block_tables_[static_cast<size_t>(slot)];
    while (static_cast<int64_t>(table.size()) <= block_index) {
        table.push_back(kv_alloc_block());
    }
}

char* NativeInferenceEngine::kv_slot_bytes(
    int64_t slot, bool key_cache, int64_t layer, int64_t position, int64_t head) {
    const ModelConfig& cfg = bundle_->config();
    const int64_t block =
        kv_block_tables_[static_cast<size_t>(slot)]
            [static_cast<size_t>(position / kKvBlockTokens)];
    const int64_t elem_index =
        layer * (kKvBlockTokens * cfg.num_key_value_heads) +
        (position % kKvBlockTokens) * cfg.num_key_value_heads + head;
    char* base = reinterpret_cast<char*>(
        gptbridge_kv_pool_data(
            kv_pool_, static_cast<int32_t>(block), key_cache ? 1 : 0));
    if (base == nullptr) throw InferenceError("KV_BLOCK_NOT_ACTIVE");
    return base + static_cast<size_t>(elem_index) *
                      static_cast<size_t>(kv_elem_stride_bytes_);
}

// Per-token/per-head symmetric quantization: scale = amax/127 stored as a
// trailing double after the aligned int8 payload (scale 0 = zero vector).
void NativeInferenceEngine::kv_write(
    int64_t slot, bool key_cache, int64_t layer, int64_t position,
    int64_t head, const double* src) {
    const int64_t n = bundle_->config().head_dim;
    char* dst = kv_slot_bytes(slot, key_cache, layer, position, head);
#if defined(XINGCHENG_CUDA)
    // Write-through to the device-resident mirror (slot 0 only); the host
    // pool stays the source of truth and a failed mirror fails the forward
    // — never silently divergent caches.
    if (kv_device_active_ && slot == 0 &&
        xcuda_kv_write_rows(
            key_cache ? 1 : 0, layer, head, position, 1, src) != 0) {
        throw InferenceError("CUDA_KV_WRITE_FAILED");
    }
#endif
    if (!kv_int8_) {
        std::memcpy(dst, src, static_cast<size_t>(n) * sizeof(double));
        return;
    }
    double amax = 0.0;
    for (int64_t i = 0; i < n; ++i) {
        const double a = std::abs(src[i]);
        if (a > amax) amax = a;
    }
    const double scale = amax > 0.0 ? amax / 127.0 : 0.0;
    int8_t* q = reinterpret_cast<int8_t*>(dst);
    if (scale > 0.0) {
        for (int64_t i = 0; i < n; ++i) {
            long v = std::lround(src[i] / scale);
            if (v > 127) v = 127;
            if (v < -127) v = -127;
            q[i] = static_cast<int8_t>(v);
        }
    } else {
        std::memset(q, 0, static_cast<size_t>(n));
    }
    *reinterpret_cast<double*>(dst + ((n + 7) & ~int64_t{7})) = scale;
}

NativeInferenceEngine::KvSrc NativeInferenceEngine::kv_src(
    int64_t slot, bool key_cache, int64_t layer, int64_t position, int64_t head) {
    char* p = kv_slot_bytes(slot, key_cache, layer, position, head);
    KvSrc src;
    if (kv_int8_) {
        const int64_t n = bundle_->config().head_dim;
        src.q8 = reinterpret_cast<const int8_t*>(p);
        src.scale = *reinterpret_cast<const double*>(p + ((n + 7) & ~int64_t{7}));
    } else {
        src.fp = reinterpret_cast<const double*>(p);
    }
    return src;
}

void NativeInferenceEngine::kv_read_head(
    int64_t slot, bool key_cache, int64_t layer, int64_t position,
    int64_t head, double* out) {
    const KvSrc src = kv_src(slot, key_cache, layer, position, head);
    const int64_t n = bundle_->config().head_dim;
    if (src.q8 != nullptr) {
        for (int64_t i = 0; i < n; ++i) {
            out[i] = static_cast<double>(src.q8[i]) * src.scale;
        }
    } else {
        std::copy_n(src.fp, n, out);
    }
}

void NativeInferenceEngine::reset_cache() {
    for (int64_t slot = 0;
         slot < static_cast<int64_t>(kv_block_tables_.size()); ++slot) {
        kv_free_slot(slot);
    }
    // Slot 0 is the single-sequence namespace: keep it live after every
    // reset (kv_alloc_slot returns the first inactive slot → slot 0).
    kv_alloc_slot();
    sequence_.clear();
}

std::vector<double> NativeInferenceEngine::logits(const std::vector<int64_t>& input_ids) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return forward_last_logits(input_ids, 0, false);
}

std::pair<double, int64_t> NativeInferenceEngine::sequence_nll(
    const std::vector<int64_t>& input_ids) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (input_ids.size() < 2) return {0.0, 0};
    const std::vector<double> hidden =
        forward_hidden(input_ids, 0, false);
    const ModelConfig& cfg = bundle_->config();
    const int64_t rows = static_cast<int64_t>(input_ids.size()) - 1;
    const int64_t h = cfg.hidden_size;
    const int64_t v = cfg.vocab_size;
    double nll = 0.0;
    int64_t scored = 0;
    for (int64_t i = 0; i < rows; ++i) {
        const int64_t target = input_ids[static_cast<size_t>(i + 1)];
        if (target < 0 || target >= v) continue;
        const std::vector<double> row = matmul(
            hidden.data() + static_cast<size_t>(i) * h, 1, h,
            lm_head_t_.data(), v);
        const double mx =
            *std::max_element(row.begin(), row.end());
        double se = 0.0;
        for (const double x : row) se += std::exp(x - mx);
        nll += (mx + std::log(se)) - row[static_cast<size_t>(target)];
        ++scored;
    }
    return {nll, scored};
}

std::vector<double> NativeInferenceEngine::forward_last_logits(
    const std::vector<int64_t>& input_ids,
    int64_t position_offset,
    bool append_cache) {
    const std::vector<double> hidden = forward_hidden(input_ids, position_offset, append_cache);
    const ModelConfig& cfg = bundle_->config();
    const int64_t rows = static_cast<int64_t>(input_ids.size());
    const double* last = hidden.data() + static_cast<size_t>((rows - 1) * cfg.hidden_size);
    return matmul(last, 1, cfg.hidden_size, lm_head_t_.data(), cfg.vocab_size);
}

static double hidden_rms(
    const std::vector<double>& hidden, int64_t seq, int64_t hidden_size) {
    double sum_sq = 0.0;
    for (double value : hidden) sum_sq += value * value;
    const int64_t count = seq * hidden_size;
    return count > 0 ? std::sqrt(sum_sq / static_cast<double>(count)) : 0.0;
}

std::vector<double> NativeInferenceEngine::layer_metrics(
    const std::vector<int64_t>& input_ids) {
    std::vector<double> trace;
    forward_hidden(input_ids, 0, false, &trace);
    return trace;
}

std::vector<double> NativeInferenceEngine::module_metrics(
    const std::vector<int64_t>& input_ids) {
    std::vector<double> trace;
    forward_hidden(input_ids, 0, false, nullptr, &trace);
    return trace;
}

std::vector<double> NativeInferenceEngine::forward_hidden(
    const std::vector<int64_t>& input_ids,
    int64_t position_offset,
    bool append_cache,
    std::vector<double>* layer_rms,
    std::vector<double>* module_rms) {
    // R9: the single-sequence path is the packed batch path with one span.
    BatchSpan span;
    span.slot = 0;
    span.ids = &input_ids;
    span.position_offset = position_offset;
    span.append_cache = append_cache;
    return forward_batch_hidden({span}, layer_rms, module_rms);
}

std::vector<double> NativeInferenceEngine::forward_batch_hidden(
    const std::vector<BatchSpan>& spans,
    std::vector<double>* layer_rms,
    std::vector<double>* module_rms) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (spans.empty()) throw InferenceError("INPUT_EMPTY");
    const ModelConfig& cfg = bundle_->config();
    const int64_t hidden_size = cfg.hidden_size;
    std::vector<int64_t> starts(spans.size());
    int64_t total_tokens = 0;
    for (size_t i = 0; i < spans.size(); ++i) {
        const BatchSpan& span = spans[i];
        if (span.ids == nullptr || span.ids->empty()) {
            throw InferenceError("INPUT_EMPTY");
        }
        const int64_t seq = static_cast<int64_t>(span.ids->size());
        if (span.position_offset < 0 ||
            span.position_offset + seq > cfg.max_position_embeddings) {
            throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
        }
        if (span.append_cache || span.position_offset > 0) {
            // Cached-KV access needs a live per-sequence namespace.
            if (span.slot < 0 ||
                span.slot >= static_cast<int64_t>(kv_lens_.size()) ||
                !kv_slot_active_[static_cast<size_t>(span.slot)]) {
                throw InferenceError("KV_SLOT_INVALID");
            }
            if (span.append_cache &&
                span.position_offset != kv_lens_[static_cast<size_t>(span.slot)]) {
                throw InferenceError("KV_CACHE_POSITION_MISMATCH");
            }
        }
        starts[i] = total_tokens;
        total_tokens += seq;
    }

    std::vector<double> hidden(static_cast<size_t>(total_tokens * hidden_size));
    for (size_t i = 0; i < spans.size(); ++i) {
        const BatchSpan& span = spans[i];
        const int64_t seq = static_cast<int64_t>(span.ids->size());
        const int64_t base = starts[i];
        for (int64_t s = 0; s < seq; ++s) {
            const int64_t token = (*span.ids)[static_cast<size_t>(s)];
            if (token < 0 || token >= cfg.vocab_size) {
                throw InferenceError("TOKEN_ID_OUT_OF_RANGE");
            }
            std::copy_n(
                embedding_.data + token * hidden_size,
                hidden_size,
                hidden.data() + static_cast<size_t>((base + s) * hidden_size));
            if (cfg.position_embedding_type == "learned") {
                const TensorView& pos = bundle_->tensor("model.embeddings.position_embeddings.weight");
                const int64_t position = span.position_offset + s;
                for (int64_t d = 0; d < hidden_size; ++d) {
                    hidden[static_cast<size_t>((base + s) * hidden_size + d)] +=
                        pos.data[position * hidden_size + d];
                }
            }
        }
    }
    if (layer_rms != nullptr) {
        layer_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
    }
    if (module_rms != nullptr) {
        module_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
    }

    const int64_t q_dim = cfg.num_attention_heads * cfg.head_dim;
    const int64_t kv_dim = cfg.num_key_value_heads * cfg.head_dim;
    // Per-span RoPE tables (each sequence carries its own position offset).
    std::vector<std::vector<double>> rope_cos(spans.size());
    std::vector<std::vector<double>> rope_sin(spans.size());
    if (cfg.position_embedding_type == "rope") {
        for (size_t i = 0; i < spans.size(); ++i) {
            rope_tables(
                static_cast<int64_t>(spans[i].ids->size()),
                spans[i].position_offset, cfg.head_dim, cfg.rope_theta,
                rope_cos[i], rope_sin[i]);
        }
    }

    for (int64_t layer_idx = 0; layer_idx < cfg.num_hidden_layers; ++layer_idx) {
        LayerWeights& layer = layers_[static_cast<size_t>(layer_idx)];
        std::vector<double> normed = rmsnorm(
            hidden, total_tokens, hidden_size, layer.input_norm, cfg.rms_norm_eps);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(normed, total_tokens, hidden_size));
        }
        // RoPE probe accumulator: RMS of post-RoPE queries across all spans
        // (post-projection queries when the config is not rope).
        double rope_q_sumsq = 0.0;
        int64_t rope_q_count = 0;
        // Projections and FFN are token-wise: the packed rows of all spans
        // share one GEMM — that sharing is the R9 throughput win.
        std::vector<double> q_flat = linear(
            normed, total_tokens, hidden_size, layer.q_proj_t, q_dim);
        std::vector<double> k_flat = linear(
            normed, total_tokens, hidden_size, layer.k_proj_t, kv_dim);
        std::vector<double> v_flat = linear(
            normed, total_tokens, hidden_size, layer.v_proj_t, kv_dim);

        std::vector<double> attn_flat(static_cast<size_t>(total_tokens * q_dim), 0.0);
        const int64_t head_ratio = cfg.num_attention_heads / cfg.num_key_value_heads;
        const double scale = 1.0 / std::sqrt(static_cast<double>(cfg.head_dim));
        // Attention is the only span-aware stage: each sequence attends
        // exclusively to its own KV namespace (fresh projections + its own
        // cached prefix); no cross-sequence leakage is possible.
        for (size_t i = 0; i < spans.size(); ++i) {
            const BatchSpan& span = spans[i];
            const int64_t seq = static_cast<int64_t>(span.ids->size());
            const int64_t base = starts[i];
            const int64_t total_len = span.position_offset + seq;

            std::vector<double> q_heads(static_cast<size_t>(cfg.num_attention_heads * seq * cfg.head_dim));
            std::vector<double> k_heads(static_cast<size_t>(cfg.num_key_value_heads * seq * cfg.head_dim));
            std::vector<double> v_heads(static_cast<size_t>(cfg.num_key_value_heads * seq * cfg.head_dim));
            for (int64_t s = 0; s < seq; ++s) {
                for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                    std::copy_n(
                        q_flat.data() + static_cast<size_t>((base + s) * q_dim + h * cfg.head_dim),
                        cfg.head_dim,
                        q_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                }
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    std::copy_n(
                        k_flat.data() + static_cast<size_t>((base + s) * kv_dim + h * cfg.head_dim),
                        cfg.head_dim,
                        k_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                    std::copy_n(
                        v_flat.data() + static_cast<size_t>((base + s) * kv_dim + h * cfg.head_dim),
                        cfg.head_dim,
                        v_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                }
            }

            if (cfg.position_embedding_type == "rope") {
                std::vector<double> q_rope(q_heads.size());
                std::vector<double> k_rope(k_heads.size());
                checked_c_call(
                    gptbridge_native_transformer_rope(
                        q_heads.data(), 1, cfg.num_attention_heads, seq, cfg.head_dim,
                        rope_cos[i].data(), rope_sin[i].data(), q_rope.data()),
                    "rope-q");
                checked_c_call(
                    gptbridge_native_transformer_rope(
                        k_heads.data(), 1, cfg.num_key_value_heads, seq, cfg.head_dim,
                        rope_cos[i].data(), rope_sin[i].data(), k_rope.data()),
                    "rope-k");
                q_heads.swap(q_rope);
                k_heads.swap(k_rope);
            }
            if (module_rms != nullptr) {
                for (double value : q_heads) rope_q_sumsq += value * value;
                rope_q_count += static_cast<int64_t>(q_heads.size());
            }

            if (span.append_cache) {
                for (int64_t s = 0; s < seq; ++s) {
                    const int64_t position = span.position_offset + s;
                    kv_ensure_position(span.slot, position);
                    for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                        kv_write(
                            span.slot, true, layer_idx, position, h,
                            k_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                        kv_write(
                            span.slot, false, layer_idx, position, h,
                            v_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                    }
                }
            }

            // P1-1② device-resident KV: when the governed opt-in is active
            // and this span is the single-sequence slot, the current-step
            // K/V rows are mirrored on-device and attention runs entirely
            // in the CUDA online-softmax kernel (same causal bound and
            // fp64 semantics as the host path below). Other slots keep the
            // host loop.
            bool device_attn_done = false;
#if defined(XINGCHENG_CUDA)
            if (kv_device_active_ && span.slot == 0) {
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    if (xcuda_kv_write_rows(
                            1, layer_idx, h, span.position_offset, seq,
                            k_heads.data() +
                                static_cast<size_t>(h * seq * cfg.head_dim)) != 0 ||
                        xcuda_kv_write_rows(
                            0, layer_idx, h, span.position_offset, seq,
                            v_heads.data() +
                                static_cast<size_t>(h * seq * cfg.head_dim)) != 0) {
                        throw InferenceError("CUDA_KV_WRITE_FAILED");
                    }
                }
                if (xcuda_kv_attention(
                        layer_idx, q_heads.data(), cfg.num_attention_heads,
                        seq, cfg.num_key_value_heads, cfg.head_dim,
                        span.position_offset,
                        attn_flat.data() + static_cast<size_t>(base * q_dim),
                        q_dim) != 0) {
                    throw InferenceError("CUDA_ATTENTION_FAILED");
                }
                device_attn_done = true;
            }
#endif

            // W1 residual: online/blocked attention — K/V streamed in tiles
            // with a running max/sum/accumulator (FlashAttention-style online
            // softmax); the seq x total_len scores matrix is never
            // materialized, so per-query state stays O(head_dim) instead of
            // O(total_len). Masked positions are skipped via the causal bound
            // — identical semantics to the former -inf mask + softmax pass.
            // KV INT8: cached entries carry packed int8 + per-head scale;
            // current-step sources stay fp64 (KvSrc.fp).
            constexpr int64_t kAttnTile = 64;
            if (!device_attn_done) {
            std::vector<KvSrc> k_srcs(static_cast<size_t>(total_len));
            std::vector<KvSrc> v_srcs(static_cast<size_t>(total_len));
            std::vector<double> tile_scores(static_cast<size_t>(kAttnTile));
            std::vector<double> acc(static_cast<size_t>(cfg.head_dim));
            for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                const int64_t kv_head = h / head_ratio;
                for (int64_t t = 0; t < total_len; ++t) {
                    if (t < span.position_offset) {
                        k_srcs[static_cast<size_t>(t)] =
                            kv_src(span.slot, true, layer_idx, t, kv_head);
                        v_srcs[static_cast<size_t>(t)] =
                            kv_src(span.slot, false, layer_idx, t, kv_head);
                    } else {
                        const int64_t s = t - span.position_offset;
                        k_srcs[static_cast<size_t>(t)].fp =
                            k_heads.data() + static_cast<size_t>((kv_head * seq + s) * cfg.head_dim);
                        v_srcs[static_cast<size_t>(t)].fp =
                            v_heads.data() + static_cast<size_t>((kv_head * seq + s) * cfg.head_dim);
                    }
                }
                const double* q_head =
                    q_heads.data() + static_cast<size_t>(h * seq * cfg.head_dim);
                for (int64_t s = 0; s < seq; ++s) {
                    const double* q_row = q_head + s * cfg.head_dim;
                    double* out = attn_flat.data() +
                        static_cast<size_t>((base + s) * q_dim + h * cfg.head_dim);
                    std::fill(acc.begin(), acc.end(), 0.0);
                    double m = -std::numeric_limits<double>::infinity();
                    double l = 0.0;
                    const int64_t last = span.position_offset + s;  // inclusive
                    for (int64_t t0 = 0; t0 <= last; t0 += kAttnTile) {
                        const int64_t tn = std::min(kAttnTile, last - t0 + 1);
                        double tile_max = -std::numeric_limits<double>::infinity();
                        for (int64_t j = 0; j < tn; ++j) {
                            const KvSrc& ksrc = k_srcs[static_cast<size_t>(t0 + j)];
                            const double score =
                                (ksrc.q8 != nullptr
                                     ? dot_int8(q_row, ksrc.q8, cfg.head_dim) *
                                           ksrc.scale
                                     : dot_f64(q_row, ksrc.fp, cfg.head_dim)) *
                                scale;
                            tile_scores[static_cast<size_t>(j)] = score;
                            if (score > tile_max) tile_max = score;
                        }
                        const double m_new = std::max(m, tile_max);
                        const double rescale = std::exp(m - m_new);
                        if (rescale != 1.0) {
                            for (int64_t d = 0; d < cfg.head_dim; ++d)
                                acc[static_cast<size_t>(d)] *= rescale;
                            l *= rescale;
                        }
                        for (int64_t j = 0; j < tn; ++j) {
                            const double w = std::exp(
                                tile_scores[static_cast<size_t>(j)] - m_new);
                            l += w;
                            const KvSrc& vsrc = v_srcs[static_cast<size_t>(t0 + j)];
                            if (vsrc.q8 != nullptr) {
                                axpy_int8(
                                    acc.data(), w * vsrc.scale, vsrc.q8,
                                    cfg.head_dim);
                            } else {
                                axpy_f64(acc.data(), w, vsrc.fp, cfg.head_dim);
                            }
                        }
                        m = m_new;
                    }
                    const double inv_l = 1.0 / l;
                    for (int64_t d = 0; d < cfg.head_dim; ++d) {
                        out[d] = acc[static_cast<size_t>(d)] * inv_l;
                    }
                }
            }
            }
        }

        if (module_rms != nullptr) {
            module_rms->push_back(
                rope_q_count > 0
                    ? std::sqrt(
                          rope_q_sumsq / static_cast<double>(rope_q_count))
                    : 0.0);
        }
        std::vector<double> attn_out = linear(
            attn_flat, total_tokens, q_dim, layer.o_proj_t, cfg.hidden_size);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(attn_out, total_tokens, hidden_size));
        }
        for (size_t i = 0; i < hidden.size(); ++i) hidden[i] += attn_out[i];

        normed = rmsnorm(hidden, total_tokens, hidden_size, layer.post_norm, cfg.rms_norm_eps);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(normed, total_tokens, hidden_size));
        }
        if (layer.is_moe) {
            // R5 grouped sparse MoE (token-choice, mirrors modules/moe.py):
            // router softmax over experts → deterministic top-k → weights
            // renormalized inside top-k → per-expert gather / SwiGLU GEMM /
            // weighted scatter-add. Positions index the packed rows, so
            // expert groups naturally span all sequences in the batch.
            const int64_t experts = cfg.moe_num_experts;
            const int64_t top_k = cfg.moe_top_k;
            std::vector<double> probs = linear(
                normed, total_tokens, hidden_size, layer.router_t, experts);
            std::vector<int64_t> top_idx(static_cast<size_t>(total_tokens * top_k));
            std::vector<double> top_w(static_cast<size_t>(total_tokens * top_k));
            for (int64_t s = 0; s < total_tokens; ++s) {
                double* row = probs.data() + static_cast<size_t>(s * experts);
                const double mx = *std::max_element(row, row + experts);
                double total = 0.0;
                for (int64_t e = 0; e < experts; ++e) {
                    row[e] = std::exp(row[e] - mx);
                    total += row[e];
                }
                for (int64_t e = 0; e < experts; ++e) row[e] /= total;
                std::vector<int64_t> order(static_cast<size_t>(experts));
                std::iota(order.begin(), order.end(), 0);
                std::stable_sort(
                    order.begin(), order.end(),
                    [&](int64_t a, int64_t b) { return row[a] > row[b]; });
                double selected = 0.0;
                for (int64_t k = 0; k < top_k; ++k) {
                    selected += row[order[static_cast<size_t>(k)]];
                }
                for (int64_t k = 0; k < top_k; ++k) {
                    const int64_t e = order[static_cast<size_t>(k)];
                    top_idx[static_cast<size_t>(s * top_k + k)] = e;
                    top_w[static_cast<size_t>(s * top_k + k)] = row[e] / selected;
                }
            }
            // R5 residual: grouped GEMM — each token's top-k expert rows are
            // gathered once into a single contiguous buffer (expert-major,
            // token order preserved inside each group, matching the former
            // per-expert `positions` order row for row), then gate / up /
            // down each run as one grouped GEMM dispatch instead of a
            // per-expert GEMM call chain. Identical rows + identical kernel
            // → identical outputs; only the dispatch/allocation shape changed.
            std::vector<int64_t> group_count(static_cast<size_t>(experts), 0);
            for (int64_t s = 0; s < total_tokens; ++s) {
                for (int64_t k = 0; k < top_k; ++k) {
                    ++group_count[static_cast<size_t>(
                        top_idx[static_cast<size_t>(s * top_k + k)])];
                }
            }
            std::vector<int64_t> group_offset(
                static_cast<size_t>(experts) + 1, 0);
            for (int64_t e = 0; e < experts; ++e) {
                group_offset[static_cast<size_t>(e + 1)] =
                    group_offset[static_cast<size_t>(e)] +
                    group_count[static_cast<size_t>(e)];
            }
            const int64_t grouped_rows =
                group_offset[static_cast<size_t>(experts)];
            std::vector<int64_t> group_fill(
                group_offset.begin(), group_offset.end() - 1);
            std::vector<double> grouped_in(
                static_cast<size_t>(grouped_rows * hidden_size));
            std::vector<int64_t> row_token(
                static_cast<size_t>(grouped_rows));
            std::vector<double> row_weight(
                static_cast<size_t>(grouped_rows));
            for (int64_t s = 0; s < total_tokens; ++s) {
                for (int64_t k = 0; k < top_k; ++k) {
                    const int64_t e =
                        top_idx[static_cast<size_t>(s * top_k + k)];
                    const int64_t r =
                        group_fill[static_cast<size_t>(e)]++;
                    std::copy_n(
                        normed.data() +
                            static_cast<size_t>(s * hidden_size),
                        hidden_size,
                        grouped_in.data() +
                            static_cast<size_t>(r * hidden_size));
                    row_token[static_cast<size_t>(r)] = s;
                    row_weight[static_cast<size_t>(r)] =
                        top_w[static_cast<size_t>(s * top_k + k)];
                }
            }
            std::vector<int64_t> group_rows;
            std::vector<const double*> gate_list;
            std::vector<const double*> up_list;
            std::vector<const double*> down_list;
            group_rows.reserve(static_cast<size_t>(experts));
            for (int64_t e = 0; e < experts; ++e) {
                if (group_count[static_cast<size_t>(e)] == 0) continue;
                group_rows.push_back(group_count[static_cast<size_t>(e)]);
                gate_list.push_back(
                    layer.expert_gate_t[static_cast<size_t>(e)].data());
                up_list.push_back(
                    layer.expert_up_t[static_cast<size_t>(e)].data());
                down_list.push_back(
                    layer.expert_down_t[static_cast<size_t>(e)].data());
            }
            std::vector<double> mlp_out(
                static_cast<size_t>(total_tokens * hidden_size), 0.0);
            if (!group_rows.empty()) {
                std::vector<double> gate = matmul_grouped(
                    grouped_in, group_rows, gate_list, hidden_size,
                    cfg.intermediate_size);
                std::vector<double> up = matmul_grouped(
                    grouped_in, group_rows, up_list, hidden_size,
                    cfg.intermediate_size);
                std::vector<double> act(
                    static_cast<size_t>(grouped_rows * cfg.intermediate_size));
                for (size_t i = 0; i < act.size(); ++i) {
                    const double g = gate[i];
                    act[i] = (g / (1.0 + std::exp(-g))) * up[i];
                }
                std::vector<double> grouped_out = matmul_grouped(
                    act, group_rows, down_list, cfg.intermediate_size,
                    hidden_size);
                for (int64_t r = 0; r < grouped_rows; ++r) {
                    const double w = row_weight[static_cast<size_t>(r)];
                    const double* src = grouped_out.data() +
                        static_cast<size_t>(r * hidden_size);
                    double* dst = mlp_out.data() + static_cast<size_t>(
                        row_token[static_cast<size_t>(r)] * hidden_size);
                    for (int64_t d = 0; d < hidden_size; ++d) {
                        dst[d] += w * src[d];
                    }
                }
            }
            if (module_rms != nullptr) {
                module_rms->push_back(
                    hidden_rms(mlp_out, total_tokens, hidden_size));
            }
            for (size_t i = 0; i < hidden.size(); ++i) hidden[i] += mlp_out[i];
        } else {
            std::vector<double> gate = linear(
                normed, total_tokens, hidden_size, layer.gate_proj_t, cfg.intermediate_size);
            std::vector<double> up = linear(
                normed, total_tokens, hidden_size, layer.up_proj_t, cfg.intermediate_size);
            std::vector<double> mlp_in(static_cast<size_t>(total_tokens * cfg.intermediate_size));
            for (size_t i = 0; i < mlp_in.size(); ++i) {
                const double g = gate[i];
                mlp_in[i] = (g / (1.0 + std::exp(-g))) * up[i];
            }
            std::vector<double> mlp_out = linear(
                mlp_in, total_tokens, cfg.intermediate_size, layer.down_proj_t, hidden_size);
            if (module_rms != nullptr) {
                module_rms->push_back(
                    hidden_rms(mlp_out, total_tokens, hidden_size));
            }
            for (size_t i = 0; i < hidden.size(); ++i) hidden[i] += mlp_out[i];
        }
        if (layer_rms != nullptr) {
            layer_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
        }
    }

    for (const BatchSpan& span : spans) {
        if (span.append_cache) {
            kv_lens_[static_cast<size_t>(span.slot)] =
                span.position_offset + static_cast<int64_t>(span.ids->size());
        }
    }
    std::vector<double> normed = rmsnorm(
        hidden, total_tokens, hidden_size, final_norm_, cfg.rms_norm_eps);
    if (layer_rms != nullptr) {
        layer_rms->push_back(hidden_rms(normed, total_tokens, hidden_size));
    }
    if (module_rms != nullptr) {
        module_rms->push_back(hidden_rms(normed, total_tokens, hidden_size));
    }
    return normed;
}

std::vector<std::vector<double>> NativeInferenceEngine::forward_batch_last_logits(
    const std::vector<BatchSpan>& spans) {
    // Packed forward → gather each span's last-position hidden row → one
    // lm_head GEMM for the whole batch → slice back per span.
    const std::vector<double> hidden = forward_batch_hidden(spans);
    const ModelConfig& cfg = bundle_->config();
    const int64_t hidden_size = cfg.hidden_size;
    std::vector<double> last_rows;
    last_rows.reserve(static_cast<size_t>(spans.size() * hidden_size));
    int64_t base = 0;
    for (const BatchSpan& span : spans) {
        const int64_t seq = static_cast<int64_t>(span.ids->size());
        const double* last =
            hidden.data() + static_cast<size_t>((base + seq - 1) * hidden_size);
        last_rows.insert(last_rows.end(), last, last + hidden_size);
        base += seq;
    }
    std::vector<double> logits = matmul(
        last_rows.data(), static_cast<int64_t>(spans.size()),
        hidden_size, lm_head_t_.data(), cfg.vocab_size);
    std::vector<std::vector<double>> out(spans.size());
    for (size_t i = 0; i < spans.size(); ++i) {
        const double* row = logits.data() + i * cfg.vocab_size;
        out[i].assign(row, row + cfg.vocab_size);
    }
    return out;
}

int64_t NativeInferenceEngine::sample_next(
    const std::vector<double>& logits,
    const std::vector<int64_t>& previous,
    const SamplingConfig& sampling,
    uint64_t& rng_state) const {
    std::vector<double> adjusted = logits;
    if (sampling.repetition_penalty != 1.0) {
        std::unordered_set<int64_t> seen(previous.begin(), previous.end());
        for (const int64_t token : seen) {
            if (token >= 0 && token < static_cast<int64_t>(adjusted.size())) {
                double& value = adjusted[static_cast<size_t>(token)];
                value = value > 0 ? value / sampling.repetition_penalty
                                  : value * sampling.repetition_penalty;
            }
        }
    }
    if (!sampling.do_sample || sampling.temperature <= 0.0) {
        return static_cast<int64_t>(std::distance(
            adjusted.begin(), std::max_element(adjusted.begin(), adjusted.end())));
    }
    for (double& value : adjusted) value /= sampling.temperature;
    if (sampling.top_k > 0 && sampling.top_k < static_cast<int64_t>(adjusted.size())) {
        std::vector<double> sorted = adjusted;
        std::nth_element(
            sorted.begin(), sorted.begin() + static_cast<std::ptrdiff_t>(sampling.top_k), sorted.end(),
            std::greater<double>());
        const double threshold = sorted[static_cast<size_t>(sampling.top_k)];
        for (double& value : adjusted) {
            if (value < threshold) value = -std::numeric_limits<double>::infinity();
        }
    }
    if (sampling.top_p > 0.0 && sampling.top_p < 1.0) {
        std::vector<size_t> order(adjusted.size());
        for (size_t i = 0; i < order.size(); ++i) order[i] = i;
        std::sort(order.begin(), order.end(), [&](size_t a, size_t b) {
            return adjusted[a] > adjusted[b];
        });
        const double max_value = *std::max_element(adjusted.begin(), adjusted.end());
        double cumulative = 0.0;
        double total = 0.0;
        for (const double value : adjusted) total += std::exp(value - max_value);
        std::vector<bool> keep(adjusted.size(), false);
        for (const size_t index : order) {
            keep[index] = true;
            cumulative += std::exp(adjusted[index] - max_value) / total;
            if (cumulative >= sampling.top_p) break;
        }
        for (size_t i = 0; i < adjusted.size(); ++i) {
            if (!keep[i]) adjusted[i] = -std::numeric_limits<double>::infinity();
        }
    }
    const double max_value = *std::max_element(adjusted.begin(), adjusted.end());
    double total = 0.0;
    for (const double value : adjusted) total += std::exp(value - max_value);
    std::mt19937_64 rng(rng_state);
    std::uniform_real_distribution<double> uniform(0.0, total);
    const double target = uniform(rng);
    rng_state = rng();
    double cumulative = 0.0;
    for (size_t i = 0; i < adjusted.size(); ++i) {
        cumulative += std::exp(adjusted[i] - max_value);
        if (target <= cumulative) return static_cast<int64_t>(i);
    }
    return static_cast<int64_t>(adjusted.size() - 1);
}

std::vector<int64_t> NativeInferenceEngine::generate(
    const std::vector<int64_t>& prompt_ids,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (prompt_ids.empty()) throw InferenceError("PROMPT_EMPTY");
    if (max_new_tokens <= 0) return {};
    const ModelConfig& cfg = bundle_->config();
    if (static_cast<int64_t>(prompt_ids.size()) + max_new_tokens > cfg.max_position_embeddings) {
        throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
    }
    reset_cache();
    sequence_ = prompt_ids;
    std::vector<int64_t> generated;
    generated.reserve(static_cast<size_t>(max_new_tokens));
    uint64_t rng_state = sampling.seed ? sampling.seed : 0x9E3779B97F4A7C15ULL;

    // P3d prefix reuse: restore the longest cached prompt prefix so only the
    // suffix is recomputed. The snapshot stores per-layer K/V slices; values
    // are deterministic, so a restored cache is bit-identical to recompute.
    const int64_t kv_dim = cfg.num_key_value_heads * cfg.head_dim;
    int64_t prefix_len = 0;
    size_t hit_index = prefix_cache_.size();
    for (size_t i = 0; i < prefix_cache_.size(); ++i) {
        const PrefixEntry& entry = prefix_cache_[i];
        const int64_t len = static_cast<int64_t>(entry.tokens.size());
        if (len > 0 && len <= static_cast<int64_t>(prompt_ids.size()) &&
            std::equal(
                entry.tokens.begin(), entry.tokens.end(), prompt_ids.begin()) &&
            len > prefix_len) {
            prefix_len = len;
            hit_index = i;
        }
    }
    if (hit_index != prefix_cache_.size()) {
        PrefixEntry& hit = prefix_cache_[hit_index];
        for (int64_t position = 0; position < prefix_len; ++position) {
            kv_ensure_position(0, position);
        }
        for (int64_t layer = 0; layer < cfg.num_hidden_layers; ++layer) {
            for (int64_t position = 0; position < prefix_len; ++position) {
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    kv_write(
                        0, true, layer, position, h,
                        hit.k.data() + (layer * prefix_len + position) * kv_dim +
                            h * cfg.head_dim);
                    kv_write(
                        0, false, layer, position, h,
                        hit.v.data() + (layer * prefix_len + position) * kv_dim +
                            h * cfg.head_dim);
                }
            }
        }
        hit.tick = ++prefix_tick_;
        ++prefix_hits_;
        kv_lens_[0] = prefix_len;
    } else {
        ++prefix_misses_;
    }

    // Always forward at least the final prompt token so logits exist; on a
    // full-prefix hit the recomputed K/V overwrite identical values.
    int64_t forward_begin = prefix_len;
    int64_t forward_offset = prefix_len;
    if (forward_begin == static_cast<int64_t>(prompt_ids.size())) {
        forward_begin -= 1;
        forward_offset = prefix_len - 1;
        kv_lens_[0] = forward_offset;
    }
    std::vector<int64_t> suffix(
        prompt_ids.begin() + forward_begin, prompt_ids.end());
    std::vector<double> next_logits =
        forward_last_logits(suffix, forward_offset, true);

    // Snapshot the prompt prefix for future reuse (bounded, LRU-evicted).
    if (prefix_cache_max_entries_ > 0 && kv_lens_[0] > 0) {
        const int64_t store_len = kv_lens_[0];
        const int64_t entry_bytes =
            2 * cfg.num_hidden_layers * store_len * kv_dim * 8;
        if (entry_bytes <= prefix_cache_max_bytes_) {
            auto existing = std::find_if(
                prefix_cache_.begin(), prefix_cache_.end(),
                [&](const PrefixEntry& entry) {
                    return entry.tokens == prompt_ids;
                });
            if (existing != prefix_cache_.end()) {
                existing->tick = ++prefix_tick_;
            } else {
                int64_t total_bytes = entry_bytes;
                for (const PrefixEntry& entry : prefix_cache_) {
                    total_bytes += static_cast<int64_t>(
                        (entry.k.size() + entry.v.size()) * sizeof(double));
                }
                while (
                    (!prefix_cache_.empty() &&
                     static_cast<int64_t>(prefix_cache_.size()) >=
                         prefix_cache_max_entries_) ||
                    (!prefix_cache_.empty() &&
                     total_bytes > prefix_cache_max_bytes_)) {
                    auto oldest = std::min_element(
                        prefix_cache_.begin(), prefix_cache_.end(),
                        [](const PrefixEntry& a, const PrefixEntry& b) {
                            return a.tick < b.tick;
                        });
                    total_bytes -= static_cast<int64_t>(
                        (oldest->k.size() + oldest->v.size()) *
                        sizeof(double));
                    prefix_cache_.erase(oldest);
                }
                PrefixEntry entry;
                entry.tokens = prompt_ids;
                // Snapshot stays fp64 in host memory; kv_read_head
                // dequantizes when the pool stores packed int8.
                entry.k.resize(
                    static_cast<size_t>(
                        cfg.num_hidden_layers * store_len * kv_dim));
                entry.v.resize(
                    static_cast<size_t>(
                        cfg.num_hidden_layers * store_len * kv_dim));
                for (int64_t layer = 0; layer < cfg.num_hidden_layers; ++layer) {
                    for (int64_t position = 0; position < store_len; ++position) {
                        for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                            const int64_t base_idx =
                                (layer * store_len + position) * kv_dim +
                                h * cfg.head_dim;
                            kv_read_head(
                                0, true, layer, position, h,
                                entry.k.data() + base_idx);
                            kv_read_head(
                                0, false, layer, position, h,
                                entry.v.data() + base_idx);
                        }
                    }
                }
                entry.tick = ++prefix_tick_;
                prefix_cache_.push_back(std::move(entry));
            }
        }
    }
    for (int64_t step = 0; step < max_new_tokens; ++step) {
        const int64_t token = sample_next(next_logits, sequence_, sampling, rng_state);
        generated.push_back(token);
        sequence_.push_back(token);
        if (token == cfg.eos_token_id || step + 1 >= max_new_tokens) break;
        next_logits = forward_last_logits({token}, kv_lens_[0], true);
    }
    return generated;
}

std::vector<std::vector<int64_t>> NativeInferenceEngine::generate_batch(
    const std::vector<std::vector<int64_t>>& prompts,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    // R9 continuous batching: packed prefill over all prompts, then decode
    // steps pack every still-active sequence's token into one forward.
    // A sequence leaves the active set on EOS (or the shared step cap) and
    // its KV blocks return to the pool immediately — later sequences never
    // wait for earlier ones to finish.
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (prompts.empty()) return {};
    if (max_new_tokens <= 0) return {};
    if (static_cast<int64_t>(prompts.size()) > kMaxBatchSeqs) {
        throw InferenceError("BATCH_SIZE_EXCEEDED");
    }
    const ModelConfig& cfg = bundle_->config();
    reset_cache();

    struct SeqState {
        int64_t slot = -1;
        std::vector<int64_t> prompt;
        std::vector<int64_t> generated;
        std::vector<int64_t> context;
        std::vector<double> logits;
        bool done = false;
    };
    std::vector<SeqState> seqs(prompts.size());
    size_t allocated = 0;
    try {
        for (size_t i = 0; i < prompts.size(); ++i) {
            if (prompts[i].empty()) throw InferenceError("PROMPT_EMPTY");
            if (static_cast<int64_t>(prompts[i].size()) + max_new_tokens >
                cfg.max_position_embeddings) {
                throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
            }
            seqs[i].prompt = prompts[i];
            seqs[i].context = prompts[i];
            seqs[i].slot = kv_alloc_slot();
            ++allocated;
        }
    } catch (...) {
        for (size_t i = 0; i < allocated; ++i) kv_free_slot(seqs[i].slot);
        throw;
    }

    // Prefill: every sequence packs into one forward.
    {
        std::vector<BatchSpan> spans;
        spans.reserve(seqs.size());
        for (SeqState& seq : seqs) {
            BatchSpan span;
            span.slot = seq.slot;
            span.ids = &seq.prompt;
            span.position_offset = 0;
            span.append_cache = true;
            spans.push_back(span);
        }
        std::vector<std::vector<double>> logits =
            forward_batch_last_logits(spans);
        for (size_t i = 0; i < seqs.size(); ++i) {
            seqs[i].logits = std::move(logits[i]);
        }
    }

    const uint64_t base_seed =
        sampling.seed ? sampling.seed : 0x9E3779B97F4A7C15ULL;
    std::vector<uint64_t> rng(seqs.size());
    for (size_t i = 0; i < seqs.size(); ++i) {
        rng[i] = base_seed + static_cast<uint64_t>(i) * 0x9E3779B97F4A7C15ULL;
    }

    std::vector<int64_t> step_tokens(seqs.size());
    for (int64_t step = 0; step < max_new_tokens; ++step) {
        int64_t active = 0;
        for (size_t i = 0; i < seqs.size(); ++i) {
            SeqState& seq = seqs[i];
            if (seq.done) continue;
            const int64_t token =
                sample_next(seq.logits, seq.context, sampling, rng[i]);
            seq.generated.push_back(token);
            seq.context.push_back(token);
            step_tokens[i] = token;
            if (token == cfg.eos_token_id || step + 1 >= max_new_tokens) {
                seq.done = true;
                kv_free_slot(seq.slot);  // continuous: release mid-batch
            } else {
                ++active;
            }
        }
        if (active == 0) break;
        // One-token span per active sequence — packed into a single forward.
        std::vector<std::vector<int64_t>> one_token(
            static_cast<size_t>(active), std::vector<int64_t>(1));
        std::vector<BatchSpan> spans;
        spans.reserve(static_cast<size_t>(active));
        size_t w = 0;
        for (SeqState& seq : seqs) {
            if (seq.done) continue;
            one_token[w][0] =
                step_tokens[static_cast<size_t>(&seq - seqs.data())];
            BatchSpan span;
            span.slot = seq.slot;
            span.ids = &one_token[w];
            span.position_offset = kv_lens_[static_cast<size_t>(seq.slot)];
            span.append_cache = true;
            spans.push_back(span);
            ++w;
        }
        std::vector<std::vector<double>> logits =
            forward_batch_last_logits(spans);
        w = 0;
        for (SeqState& seq : seqs) {
            if (seq.done) continue;
            seq.logits = std::move(logits[w++]);
        }
    }

    std::vector<std::vector<int64_t>> out(seqs.size());
    for (size_t i = 0; i < seqs.size(); ++i) {
        out[i] = std::move(seqs[i].generated);
        if (!seqs[i].done) kv_free_slot(seqs[i].slot);
    }
    return out;
}

std::string NativeInferenceEngine::generate_text(
    const std::string& prompt,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    return decode(generate(encode(prompt), max_new_tokens, sampling));
}

int64_t NativeInferenceEngine::kv_memory_bytes() const {
    return gptbridge_kv_pool_memory_bytes(kv_pool_);
}

int64_t NativeInferenceEngine::memory_bytes() const {
    return (bundle_ ? bundle_->weights_bytes() : 0) + kv_memory_bytes();
}

void NativeInferenceEngine::set_kv_memory_limit(int64_t bytes) {
    kv_limit_bytes_ = std::max<int64_t>(0, bytes);
    if (kv_pool_ != nullptr &&
        !gptbridge_kv_pool_set_limit(kv_pool_, kv_limit_bytes_)) {
        throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
    }
}

void NativeInferenceEngine::set_prefix_cache_limit(
    int64_t max_entries, int64_t max_bytes) {
    prefix_cache_max_entries_ = std::max<int64_t>(0, max_entries);
    prefix_cache_max_bytes_ = std::max<int64_t>(0, max_bytes);
    while (static_cast<int64_t>(prefix_cache_.size()) >
               prefix_cache_max_entries_ &&
           !prefix_cache_.empty()) {
        auto oldest = std::min_element(
            prefix_cache_.begin(), prefix_cache_.end(),
            [](const PrefixEntry& a, const PrefixEntry& b) {
                return a.tick < b.tick;
            });
        prefix_cache_.erase(oldest);
    }
}

std::string NativeInferenceEngine::describe() const {
    if (!loaded()) return "{\"loaded\":false}";
    const ModelConfig& cfg = bundle_->config();
    std::ostringstream out;
    out << "{\"loaded\":true,"
        << "\"schema\":\"star-native-inference-engine/v1\","
        << "\"vocab_size\":" << cfg.vocab_size << ","
        << "\"hidden_size\":" << cfg.hidden_size << ","
        << "\"layers\":" << cfg.num_hidden_layers << ","
        << "\"heads\":" << cfg.num_attention_heads << ","
        << "\"kv_heads\":" << cfg.num_key_value_heads << ","
        << "\"weights_bytes\":" << bundle_->weights_bytes() << ","
        << "\"kv_memory_bytes\":" << kv_memory_bytes() << ","
        << "\"prefix_cache_entries\":"
        << static_cast<int64_t>(prefix_cache_.size()) << ","
        << "\"prefix_cache_hits\":" << prefix_hits_ << ","
        << "\"prefix_cache_misses\":" << prefix_misses_ << "}";
    return out.str();
}

std::string parse_generated_output(const std::string& text, int64_t max_json_bytes) {
    if (max_json_bytes <= 0) max_json_bytes = 64 * 1024;
    const std::string open = "<tool_call>";
    const std::string close = "</tool_call>";
    const size_t start = text.find(open);
    if (start == std::string::npos) {
        return "{\"schema\":\"star-inference-output/v1\",\"text\":\"" +
            json_escape(text) + "\",\"tool_call\":null}";
    }
    const size_t json_start = start + open.size();
    const size_t end = text.find(close, json_start);
    if (end == std::string::npos) {
        throw InferenceError("TOOL_CALL_UNCLOSED");
    }
    const std::string payload = text.substr(json_start, end - json_start);
    if (static_cast<int64_t>(payload.size()) > max_json_bytes) {
        throw InferenceError("TOOL_CALL_JSON_TOO_LARGE");
    }
    try {
        JsonParser(payload).parse();  // bounded, fail-closed validation
    } catch (const InferenceError&) {
        throw InferenceError("TOOL_CALL_JSON_INVALID");
    }
    std::string cleaned = text.substr(0, start) + text.substr(end + close.size());
    const size_t eot = cleaned.find("<|eot|>");
    if (eot != std::string::npos) cleaned.erase(eot);
    return "{\"schema\":\"star-inference-output/v1\",\"text\":\"" +
        json_escape(cleaned) + "\",\"tool_call\":" + payload + "}";
}

}  // namespace xingcheng::inference
