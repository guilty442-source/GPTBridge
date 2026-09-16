// _binding: pybind11 binding for the canonical native interface (A221/E186).
//
// Binding-only (A220/E185): wraps the C functions declared in
// native/include/gptbridge_native.h and implemented in
// native/bridge/gptbridge_native.c, plus the compute cores in
// native/core/{parser,vector,transformer}.cpp.  Does not duplicate
// bridge or core logic.
//
// Built by build_native.py into _sovereign_native.pyd placed next to this
// source so the Python adapters can import it with a relative import.
// The extension is optional; Python adapters provide graceful fallback
// if the .pyd is not present (A219/E184).
//
// Safety rules (A221/E186):
//   - Python owns all memory; C++ borrows raw pointers + length.
//   - No C++ exceptions cross the ABI boundary (noexcept + try/catch).
//   - No unbounded allocation; no per-request thread pool.
//   - No cross-runtime free (never free Python memory from C++).
//   - GIL released only during pure compute (never during conversion).

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>

#include "gptbridge_native.h"
#include "parser.hpp"
#include "vector.hpp"
#include "transformer.hpp"

#include <stdexcept>
#include <vector>

namespace py = pybind11;

// --- Parser wrappers ---

static py::object parser_token_estimate(py::str text) {
    Py_ssize_t len = 0;
    const char* data = PyUnicode_AsUTF8AndSize(text.ptr(), &len);
    if (data == nullptr) {
        throw py::error_already_set();
    }
    int64_t count = gptbridge_native_parser::token_estimate(data, static_cast<int64_t>(len));
    return py::cast(count);
}

static py::object parser_batch_token_estimate(py::list texts) {
    Py_ssize_t n = PyList_Size(texts.ptr());
    if (n < 0) {
        throw py::error_already_set();
    }
    if (n == 0) {
        return py::list();
    }

    // Collect C-string pointers and lengths (Python owns the memory).
    std::vector<const char*> ptrs;
    std::vector<int64_t> lens;
    ptrs.reserve(static_cast<size_t>(n));
    lens.reserve(static_cast<size_t>(n));

    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* item = PyList_GetItem(texts.ptr(), i);
        if (item == nullptr) {
            throw py::error_already_set();
        }
        Py_ssize_t item_len = 0;
        const char* data = PyUnicode_AsUTF8AndSize(item, &item_len);
        if (data == nullptr) {
            throw py::error_already_set();
        }
        ptrs.push_back(data);
        lens.push_back(static_cast<int64_t>(item_len));
    }

    // Allocate result array (C++ owns this; freed before return).
    std::vector<int64_t> results(static_cast<size_t>(n), 0);

    // Release GIL during pure compute.
    {
        py::gil_scoped_release release;
        gptbridge_native_parser::batch_token_estimate(
            ptrs.data(), lens.data(), static_cast<int64_t>(n), results.data());
    }

    py::list out;
    for (size_t i = 0; i < results.size(); ++i) {
        out.append(py::cast(results[i]));
    }
    return out;
}

// --- Vector wrappers (numpy array input) ---

static py::object vector_dot(py::array_t<double> a, py::array_t<double> b) {
    auto a_buf = a.request();
    auto b_buf = b.request();
    if (a_buf.ndim != 1 || b_buf.ndim != 1) {
        throw std::invalid_argument("dot requires 1-D arrays");
    }
    if (a_buf.shape[0] != b_buf.shape[0]) {
        throw std::invalid_argument("dot requires same-length arrays");
    }
    int64_t dim = static_cast<int64_t>(a_buf.shape[0]);
    const double* a_ptr = static_cast<const double*>(a_buf.ptr);
    const double* b_ptr = static_cast<const double*>(b_buf.ptr);

    double result;
    {
        py::gil_scoped_release release;
        result = gptbridge_native_vector::dot(a_ptr, b_ptr, dim);
    }
    return py::cast(result);
}

static py::object vector_l2_norm(py::array_t<double> a) {
    auto a_buf = a.request();
    if (a_buf.ndim != 1) {
        throw std::invalid_argument("l2_norm requires a 1-D array");
    }
    int64_t dim = static_cast<int64_t>(a_buf.shape[0]);
    const double* a_ptr = static_cast<const double*>(a_buf.ptr);

    double result;
    {
        py::gil_scoped_release release;
        result = gptbridge_native_vector::l2_norm(a_ptr, dim);
    }
    return py::cast(result);
}

static py::object vector_cosine_similarity(py::array_t<double> a, py::array_t<double> b) {
    auto a_buf = a.request();
    auto b_buf = b.request();
    if (a_buf.ndim != 1 || b_buf.ndim != 1) {
        throw std::invalid_argument("cosine_similarity requires 1-D arrays");
    }
    if (a_buf.shape[0] != b_buf.shape[0]) {
        throw std::invalid_argument("cosine_similarity requires same-length arrays");
    }
    int64_t dim = static_cast<int64_t>(a_buf.shape[0]);
    const double* a_ptr = static_cast<const double*>(a_buf.ptr);
    const double* b_ptr = static_cast<const double*>(b_buf.ptr);

    double result;
    {
        py::gil_scoped_release release;
        result = gptbridge_native_vector::cosine_similarity(a_ptr, b_ptr, dim);
    }
    return py::cast(result);
}

// --- Transformer wrappers (numpy array input) ---

static py::object transformer_matmul(
        py::array_t<double> a, py::array_t<double> b) {
    auto a_buf = a.request();
    auto b_buf = b.request();
    if (a_buf.ndim != 2 || b_buf.ndim != 2) {
        throw std::invalid_argument("matmul requires 2-D arrays");
    }
    int64_t m = static_cast<int64_t>(a_buf.shape[0]);
    int64_t k = static_cast<int64_t>(a_buf.shape[1]);
    int64_t k_in = static_cast<int64_t>(b_buf.shape[0]);
    int64_t n = static_cast<int64_t>(b_buf.shape[1]);
    if (k != k_in) {
        throw std::invalid_argument("matmul: inner dimensions must match");
    }

    const double* a_ptr = static_cast<const double*>(a_buf.ptr);
    const double* b_ptr = static_cast<const double*>(b_buf.ptr);

    py::array_t<double> c({static_cast<py::ssize_t>(m), static_cast<py::ssize_t>(n)});
    auto c_buf = c.request();
    double* c_ptr = static_cast<double*>(c_buf.ptr);

    int rc;
    {
        py::gil_scoped_release release;
        rc = gptbridge_native_transformer::matmul(a_ptr, m, k, b_ptr, k_in, n, c_ptr);
    }
    if (rc != 0) {
        throw std::runtime_error("matmul failed");
    }
    return c;
}

static py::object transformer_softmax(py::array_t<double> input) {
    auto in_buf = input.request();
    if (in_buf.ndim != 2) {
        throw std::invalid_argument("softmax requires a 2-D array");
    }
    int64_t rows = static_cast<int64_t>(in_buf.shape[0]);
    int64_t cols = static_cast<int64_t>(in_buf.shape[1]);
    const double* in_ptr = static_cast<const double*>(in_buf.ptr);

    py::array_t<double> output({static_cast<py::ssize_t>(rows), static_cast<py::ssize_t>(cols)});
    auto out_buf = output.request();
    double* out_ptr = static_cast<double*>(out_buf.ptr);

    int rc;
    {
        py::gil_scoped_release release;
        rc = gptbridge_native_transformer::softmax(in_ptr, rows, cols, out_ptr);
    }
    if (rc != 0) {
        throw std::runtime_error("softmax failed");
    }
    return output;
}

static py::object transformer_scaled_dot_product_attention(
        py::array_t<double> q,
        py::array_t<double> k,
        py::array_t<double> v) {
    auto q_buf = q.request();
    auto k_buf = k.request();
    auto v_buf = v.request();
    if (q_buf.ndim != 2 || k_buf.ndim != 2 || v_buf.ndim != 2) {
        throw std::invalid_argument("attention requires 2-D arrays");
    }
    int64_t q_rows = static_cast<int64_t>(q_buf.shape[0]);
    int64_t d_k = static_cast<int64_t>(q_buf.shape[1]);
    int64_t k_rows = static_cast<int64_t>(k_buf.shape[0]);
    int64_t d_k_in = static_cast<int64_t>(k_buf.shape[1]);
    int64_t v_rows = static_cast<int64_t>(v_buf.shape[0]);
    int64_t d_v = static_cast<int64_t>(v_buf.shape[1]);
    if (d_k != d_k_in) {
        throw std::invalid_argument("attention: Q and K dims must match");
    }
    if (k_rows != v_rows) {
        throw std::invalid_argument("attention: K and V rows must match");
    }

    const double* q_ptr = static_cast<const double*>(q_buf.ptr);
    const double* k_ptr = static_cast<const double*>(k_buf.ptr);
    const double* v_ptr = static_cast<const double*>(v_buf.ptr);

    py::array_t<double> output({static_cast<py::ssize_t>(q_rows), static_cast<py::ssize_t>(d_v)});
    auto out_buf = output.request();
    double* out_ptr = static_cast<double*>(out_buf.ptr);

    // Temporary scores buffer [q_rows x k_rows] — C++ owns this.
    std::vector<double> scores_temp(static_cast<size_t>(q_rows * k_rows), 0.0);

    int rc;
    {
        py::gil_scoped_release release;
        rc = gptbridge_native_transformer::scaled_dot_product_attention(
            q_ptr, q_rows, d_k,
            k_ptr, k_rows, d_k_in,
            v_ptr, v_rows, d_v,
            out_ptr, scores_temp.data());
    }
    if (rc != 0) {
        throw std::runtime_error("scaled_dot_product_attention failed");
    }
    return output;
}

// --- Module ---

PYBIND11_MODULE(_sovereign_native, m) {
    m.doc() = "GPTBridge native kernel (canonical C interface via native/bridge).";

    // Platform functions (existing)
    m.def("is_windows", &gptbridge_native_is_windows,
          "Whether the current platform is Windows.");
    m.def("monotonic_seconds", &gptbridge_native_monotonic_seconds,
          "High-resolution monotonic clock in fractional seconds.");
    m.def("working_set_bytes", &gptbridge_native_working_set_bytes,
          "Process working-set size in bytes, or -1 on error.");
    m.def("private_bytes", &gptbridge_native_private_bytes,
          "Process private memory usage in bytes, or -1 on error.");
    m.def("release_working_set", &gptbridge_native_release_working_set,
          "Empty the process working set; returns success flag.");

    // Parser compute (A221)
    m.def("parser_token_estimate", &parser_token_estimate,
          "Estimate token count (words + punctuation) in a string.");
    m.def("parser_batch_token_estimate", &parser_batch_token_estimate,
          "Batch token estimation for a list of strings.");

    // Vector compute (A221)
    m.def("vector_dot", &vector_dot,
          "Dot product of two 1-D float arrays.");
    m.def("vector_l2_norm", &vector_l2_norm,
          "L2 norm of a 1-D float array.");
    m.def("vector_cosine_similarity", &vector_cosine_similarity,
          "Cosine similarity of two 1-D float arrays.");

    // Transformer compute (A221)
    m.def("transformer_matmul", &transformer_matmul,
          "Matrix multiply: C = A * B (2-D float arrays).");
    m.def("transformer_softmax", &transformer_softmax,
          "Softmax over the last dimension of a 2-D float array.");
    m.def("transformer_scaled_dot_product_attention",
          &transformer_scaled_dot_product_attention,
          "Scaled dot-product attention: Q, K, V -> output.");
}
