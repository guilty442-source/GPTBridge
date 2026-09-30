// engine_util.h — B94 fragment of engine.cpp (file+sha256+matrix utilities).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

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
    // One copy: read directly into the string's buffer instead of
    // vector -> string (whole-file copy, e.g. multi-MB tokenizer.json).
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) {
        throw InferenceError("BUNDLE_FILE_UNREADABLE:" + path.string());
    }
    const std::streamoff size = input.tellg();
    if (size < 0 || size > max_bytes) {
        throw InferenceError("BUNDLE_FILE_TOO_LARGE");
    }
    std::string data(static_cast<size_t>(size), '\0');
    input.seekg(0, std::ios::beg);
    if (size > 0 && !input.read(data.data(), size)) {
        throw InferenceError("BUNDLE_FILE_READ_FAILED");
    }
    return data;
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

// Column-concatenation of transposed [k x n_part] weights: one fused GEMM
// produces [part0|part1|...] per input row. Each output column keeps the
// identical k-length dot product it had in the unfused weights; the win is
// fewer GEMM dispatches (and, on the CUDA path, fewer host→device round
// trips) per forward pass.
std::vector<double> hcat_weights(
    const std::vector<std::pair<const std::vector<double>*, int64_t>>& parts,
    int64_t k) {
    int64_t n_total = 0;
    for (const auto& part : parts) n_total += part.second;
    std::vector<double> out(static_cast<size_t>(k * n_total));
    for (int64_t r = 0; r < k; ++r) {
        double* dst = out.data() + static_cast<size_t>(r * n_total);
        for (const auto& part : parts) {
            std::copy_n(
                part.first->data() + static_cast<size_t>(r * part.second),
                part.second, dst);
            dst += part.second;
        }
    }
    return out;
}

// Inverse of hcat_weights on an activation matrix [rows x n_total]:
// each column block is copied into its own [rows x n_part] buffer.
void split_columns(
    const std::vector<double>& fused,
    int64_t rows,
    const std::vector<std::pair<int64_t, std::vector<double>*>>& parts) {
    int64_t n_total = 0;
    for (const auto& part : parts) {
        n_total += part.first;
        // resize (not zero-assign): the copy below overwrites every
        // element, and persistent scratch keeps its capacity.
        part.second->resize(static_cast<size_t>(rows * part.first));
    }
    for (int64_t r = 0; r < rows; ++r) {
        const double* src = fused.data() + static_cast<size_t>(r * n_total);
        for (const auto& part : parts) {
            std::copy_n(
                src, part.first,
                part.second->data() + static_cast<size_t>(r * part.first));
            src += part.first;
        }
    }
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
// P1-1③ residual: fp8 (e4m3) weight-storage GEMM (kernels/matmul_fp8.cu).
extern "C" int xcuda_fp8_available();
extern "C" int xcuda_matmul_fp8(
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
