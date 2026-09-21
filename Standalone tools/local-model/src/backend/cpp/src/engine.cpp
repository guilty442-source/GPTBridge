// Xingcheng formal C++ inference layer (P3b–P3f).
//
// Scope: dense causal-LM inference, batch=1, FP64 compute over exported
// weights. Primitive tensor operations call the public C ABI. Unsupported
// model features fail closed instead of silently changing semantics.

#include "xingcheng_inference.hpp"

#include "gptbridge_native.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <limits>
#include <map>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <unordered_set>

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

std::vector<double> matmul(
    const double* a,
    int64_t m,
    int64_t k,
    const double* b,
    int64_t n) {
    std::vector<double> out(static_cast<size_t>(m * n));
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
        if (json_string(info, "dtype") != "float64") {
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
        if (item.offset < 0 || item.bytes != elements * 8 ||
            item.offset > bundle.weights_bytes_ ||
            item.bytes > bundle.weights_bytes_ - item.offset) {
            throw InferenceError("TENSOR_BOUNDS_INVALID:" + name);
        }
        TensorView view;
        view.shape = item.shape;
        view.data = reinterpret_cast<const double*>(bundle.blob_->data + item.offset);
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

void NativeInferenceEngine::load(const std::string& bundle_dir) {
    unload();
    const std::filesystem::path root(bundle_dir);
    bundle_ = std::make_unique<WeightBundle>(WeightBundle::load((root / "manifest.json").string()));
    const ModelConfig& cfg = bundle_->config();
    validate_supported();

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
        layer.gate_proj = bundle_->tensor(prefix + "mlp.gate_proj.weight");
        layer.up_proj = bundle_->tensor(prefix + "mlp.up_proj.weight");
        layer.down_proj = bundle_->tensor(prefix + "mlp.down_proj.weight");
        layer.q_proj_t = transpose_matrix(layer.q_proj);
        layer.k_proj_t = transpose_matrix(layer.k_proj);
        layer.v_proj_t = transpose_matrix(layer.v_proj);
        layer.o_proj_t = transpose_matrix(layer.o_proj);
        layer.gate_proj_t = transpose_matrix(layer.gate_proj);
        layer.up_proj_t = transpose_matrix(layer.up_proj);
        layer.down_proj_t = transpose_matrix(layer.down_proj);
    }

    const int64_t kv_elems = cfg.num_hidden_layers * cfg.max_position_embeddings *
        cfg.num_key_value_heads * cfg.head_dim;
    const int64_t kv_bytes = kv_elems * 8 * 2;
    if (kv_limit_bytes_ > 0 && kv_bytes > kv_limit_bytes_) {
        throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
    }
    kv_k_.assign(static_cast<size_t>(kv_elems), 0.0);
    kv_v_.assign(static_cast<size_t>(kv_elems), 0.0);

    const std::filesystem::path tokenizer_path = root / "tokenizer.json";
    if (std::filesystem::exists(tokenizer_path)) {
        tokenizer_ = std::make_unique<ByteLevelBPETokenizer>(
            ByteLevelBPETokenizer::load(tokenizer_path.string()));
    }
}

void NativeInferenceEngine::unload() {
    bundle_.reset();
    tokenizer_.reset();
    layers_.clear();
    kv_k_.clear();
    kv_v_.clear();
    lm_head_t_.clear();
    sequence_.clear();
    kv_len_ = 0;
}

void NativeInferenceEngine::validate_supported() const {
    const ModelConfig& cfg = bundle_->config();
    if (cfg.use_moe) throw InferenceError("MOE_INFERENCE_UNSUPPORTED");
    if (cfg.quantization != "none") throw InferenceError("QUANTIZED_INFERENCE_UNSUPPORTED");
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

void NativeInferenceEngine::reset_cache() {
    kv_len_ = 0;
    sequence_.clear();
}

std::vector<double> NativeInferenceEngine::logits(const std::vector<int64_t>& input_ids) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return forward_last_logits(input_ids, 0, false);
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

std::vector<double> NativeInferenceEngine::forward_hidden(
    const std::vector<int64_t>& input_ids,
    int64_t position_offset,
    bool append_cache) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (input_ids.empty()) throw InferenceError("INPUT_EMPTY");
    const ModelConfig& cfg = bundle_->config();
    const int64_t seq = static_cast<int64_t>(input_ids.size());
    if (position_offset < 0 || seq <= 0 ||
        position_offset + seq > cfg.max_position_embeddings) {
        throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
    }
    if (append_cache && position_offset != kv_len_) {
        throw InferenceError("KV_CACHE_POSITION_MISMATCH");
    }

    std::vector<double> hidden(static_cast<size_t>(seq * cfg.hidden_size));
    for (int64_t s = 0; s < seq; ++s) {
        const int64_t token = input_ids[static_cast<size_t>(s)];
        if (token < 0 || token >= cfg.vocab_size) {
            throw InferenceError("TOKEN_ID_OUT_OF_RANGE");
        }
        std::copy_n(
            embedding_.data + token * cfg.hidden_size,
            cfg.hidden_size,
            hidden.data() + s * cfg.hidden_size);
        if (cfg.position_embedding_type == "learned") {
            const TensorView& pos = bundle_->tensor("model.embeddings.position_embeddings.weight");
            const int64_t position = position_offset + s;
            for (int64_t d = 0; d < cfg.hidden_size; ++d) {
                hidden[static_cast<size_t>(s * cfg.hidden_size + d)] +=
                    pos.data[position * cfg.hidden_size + d];
            }
        }
    }

    const int64_t q_dim = cfg.num_attention_heads * cfg.head_dim;
    const int64_t kv_dim = cfg.num_key_value_heads * cfg.head_dim;
    const int64_t total_len = position_offset + seq;
    std::vector<double> cos;
    std::vector<double> sin;
    rope_tables(seq, position_offset, cfg.head_dim, cfg.rope_theta, cos, sin);

    for (int64_t layer_idx = 0; layer_idx < cfg.num_hidden_layers; ++layer_idx) {
        LayerWeights& layer = layers_[static_cast<size_t>(layer_idx)];
        std::vector<double> normed = rmsnorm(
            hidden, seq, cfg.hidden_size, layer.input_norm, cfg.rms_norm_eps);
        std::vector<double> q_flat = linear(
            normed, seq, cfg.hidden_size, layer.q_proj_t, q_dim);
        std::vector<double> k_flat = linear(
            normed, seq, cfg.hidden_size, layer.k_proj_t, kv_dim);
        std::vector<double> v_flat = linear(
            normed, seq, cfg.hidden_size, layer.v_proj_t, kv_dim);

        std::vector<double> q_heads(static_cast<size_t>(cfg.num_attention_heads * seq * cfg.head_dim));
        std::vector<double> k_heads(static_cast<size_t>(cfg.num_key_value_heads * seq * cfg.head_dim));
        std::vector<double> v_heads(static_cast<size_t>(cfg.num_key_value_heads * seq * cfg.head_dim));
        for (int64_t s = 0; s < seq; ++s) {
            for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                std::copy_n(
                    q_flat.data() + s * q_dim + h * cfg.head_dim,
                    cfg.head_dim,
                    q_heads.data() + (h * seq + s) * cfg.head_dim);
            }
            for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                std::copy_n(
                    k_flat.data() + s * kv_dim + h * cfg.head_dim,
                    cfg.head_dim,
                    k_heads.data() + (h * seq + s) * cfg.head_dim);
                std::copy_n(
                    v_flat.data() + s * kv_dim + h * cfg.head_dim,
                    cfg.head_dim,
                    v_heads.data() + (h * seq + s) * cfg.head_dim);
            }
        }

        if (cfg.position_embedding_type == "rope") {
            std::vector<double> q_rope(q_heads.size());
            std::vector<double> k_rope(k_heads.size());
            checked_c_call(
                gptbridge_native_transformer_rope(
                    q_heads.data(), 1, cfg.num_attention_heads, seq, cfg.head_dim,
                    cos.data(), sin.data(), q_rope.data()),
                "rope-q");
            checked_c_call(
                gptbridge_native_transformer_rope(
                    k_heads.data(), 1, cfg.num_key_value_heads, seq, cfg.head_dim,
                    cos.data(), sin.data(), k_rope.data()),
                "rope-k");
            q_heads.swap(q_rope);
            k_heads.swap(k_rope);
        }

        if (append_cache) {
            const int64_t layer_stride = cfg.max_position_embeddings * kv_dim;
            double* k_cache = kv_k_.data() + layer_idx * layer_stride;
            double* v_cache = kv_v_.data() + layer_idx * layer_stride;
            for (int64_t s = 0; s < seq; ++s) {
                const int64_t position = position_offset + s;
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    std::copy_n(
                        k_heads.data() + (h * seq + s) * cfg.head_dim,
                        cfg.head_dim,
                        k_cache + position * kv_dim + h * cfg.head_dim);
                    std::copy_n(
                        v_heads.data() + (h * seq + s) * cfg.head_dim,
                        cfg.head_dim,
                        v_cache + position * kv_dim + h * cfg.head_dim);
                }
            }
        }

        std::vector<double> attn_flat(static_cast<size_t>(seq * q_dim), 0.0);
        const int64_t head_ratio = cfg.num_attention_heads / cfg.num_key_value_heads;
        for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
            const int64_t kv_head = h / head_ratio;
            std::vector<double> k_all(static_cast<size_t>(total_len * cfg.head_dim));
            std::vector<double> v_all(static_cast<size_t>(total_len * cfg.head_dim));
            for (int64_t t = 0; t < total_len; ++t) {
                const double* k_src = nullptr;
                const double* v_src = nullptr;
                if (t < position_offset) {
                    const int64_t layer_stride = cfg.max_position_embeddings * kv_dim;
                    k_src = kv_k_.data() + layer_idx * layer_stride + t * kv_dim + kv_head * cfg.head_dim;
                    v_src = kv_v_.data() + layer_idx * layer_stride + t * kv_dim + kv_head * cfg.head_dim;
                } else {
                    const int64_t s = t - position_offset;
                    k_src = k_heads.data() + (kv_head * seq + s) * cfg.head_dim;
                    v_src = v_heads.data() + (kv_head * seq + s) * cfg.head_dim;
                }
                std::copy_n(k_src, cfg.head_dim, k_all.data() + t * cfg.head_dim);
                std::copy_n(v_src, cfg.head_dim, v_all.data() + t * cfg.head_dim);
            }
            std::vector<double> k_t(static_cast<size_t>(cfg.head_dim * total_len));
            for (int64_t d = 0; d < cfg.head_dim; ++d) {
                for (int64_t t = 0; t < total_len; ++t) {
                    k_t[static_cast<size_t>(d * total_len + t)] = k_all[static_cast<size_t>(t * cfg.head_dim + d)];
                }
            }
            const double* q_head = q_heads.data() + h * seq * cfg.head_dim;
            std::vector<double> scores = matmul(q_head, seq, cfg.head_dim, k_t.data(), total_len);
            const double scale = 1.0 / std::sqrt(static_cast<double>(cfg.head_dim));
            for (int64_t s = 0; s < seq; ++s) {
                for (int64_t t = 0; t < total_len; ++t) {
                    double& value = scores[static_cast<size_t>(s * total_len + t)];
                    value = (t <= position_offset + s)
                        ? value * scale
                        : -std::numeric_limits<double>::infinity();
                }
            }
            checked_c_call(
                gptbridge_native_transformer_softmax(
                    scores.data(), seq, total_len, scores.data()),
                "attention-softmax");
            std::vector<double> head_out = matmul(
                scores.data(), seq, total_len, v_all.data(), cfg.head_dim);
            for (int64_t s = 0; s < seq; ++s) {
                std::copy_n(
                    head_out.data() + s * cfg.head_dim,
                    cfg.head_dim,
                    attn_flat.data() + s * q_dim + h * cfg.head_dim);
            }
        }

        std::vector<double> attn_out = linear(
            attn_flat, seq, q_dim, layer.o_proj_t, cfg.hidden_size);
        for (size_t i = 0; i < hidden.size(); ++i) hidden[i] += attn_out[i];

        normed = rmsnorm(hidden, seq, cfg.hidden_size, layer.post_norm, cfg.rms_norm_eps);
        std::vector<double> gate = linear(
            normed, seq, cfg.hidden_size, layer.gate_proj_t, cfg.intermediate_size);
        std::vector<double> up = linear(
            normed, seq, cfg.hidden_size, layer.up_proj_t, cfg.intermediate_size);
        std::vector<double> mlp_in(static_cast<size_t>(seq * cfg.intermediate_size));
        for (size_t i = 0; i < mlp_in.size(); ++i) {
            const double g = gate[i];
            mlp_in[i] = (g / (1.0 + std::exp(-g))) * up[i];
        }
        std::vector<double> mlp_out = linear(
            mlp_in, seq, cfg.intermediate_size, layer.down_proj_t, cfg.hidden_size);
        for (size_t i = 0; i < hidden.size(); ++i) hidden[i] += mlp_out[i];
    }

    if (append_cache) {
        kv_len_ = total_len;
    }
    return rmsnorm(hidden, seq, cfg.hidden_size, final_norm_, cfg.rms_norm_eps);
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

    std::vector<double> next_logits = forward_last_logits(prompt_ids, 0, true);
    for (int64_t step = 0; step < max_new_tokens; ++step) {
        const int64_t token = sample_next(next_logits, sequence_, sampling, rng_state);
        generated.push_back(token);
        sequence_.push_back(token);
        if (token == cfg.eos_token_id || step + 1 >= max_new_tokens) break;
        next_logits = forward_last_logits({token}, kv_len_, true);
    }
    return generated;
}

std::string NativeInferenceEngine::generate_text(
    const std::string& prompt,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    return decode(generate(encode(prompt), max_new_tokens, sampling));
}

int64_t NativeInferenceEngine::kv_memory_bytes() const {
    return static_cast<int64_t>((kv_k_.size() + kv_v_.size()) * sizeof(double));
}

int64_t NativeInferenceEngine::memory_bytes() const {
    return (bundle_ ? bundle_->weights_bytes() : 0) + kv_memory_bytes();
}

void NativeInferenceEngine::set_kv_memory_limit(int64_t bytes) {
    kv_limit_bytes_ = std::max<int64_t>(0, bytes);
    if (loaded() && kv_limit_bytes_ > 0 && kv_memory_bytes() > kv_limit_bytes_) {
        throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
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
        << "\"kv_memory_bytes\":" << kv_memory_bytes() << "}";
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
