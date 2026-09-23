// _binding: pybind11 binding for the canonical native interface (A221/E186).
//
// Binding-only (A220/E185): wraps the C functions declared in the sole public
// header native/include/gptbridge_native.h. Platform functions are implemented
// by native/bridge/gptbridge_native.c; compute functions are implemented by the
// pure-C cores in native/core/{parser,vector,transformer}.c. Does not duplicate
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
#include <pybind11/stl.h>

#include "gptbridge_native.h"
#include "watchdog.h"
#include "scheduler.h"
#include "outbox.h"
#include "maintenance.h"
#include "ipc_registry.h"
#include "runtime_state.h"
#include "activation_broker.h"
#include "system_rescue.h"
#include "governed_tool.h"

#include <chrono>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <unordered_map>
#include <vector>

namespace py = pybind11;

// --- Parser wrappers ---

static py::object parser_token_estimate(py::str text) {
    Py_ssize_t len = 0;
    const char* data = PyUnicode_AsUTF8AndSize(text.ptr(), &len);
    if (data == nullptr) {
        throw py::error_already_set();
    }
    int64_t count = gptbridge_native_parser_token_estimate(data, static_cast<int64_t>(len));
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
        gptbridge_native_parser_batch_token_estimate(
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
        result = gptbridge_native_vector_dot(a_ptr, b_ptr, dim);
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
        result = gptbridge_native_vector_l2_norm(a_ptr, dim);
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
        result = gptbridge_native_vector_cosine_similarity(a_ptr, b_ptr, dim);
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
        rc = gptbridge_native_transformer_matmul(a_ptr, m, k, b_ptr, k_in, n, c_ptr);
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
        rc = gptbridge_native_transformer_softmax(in_ptr, rows, cols, out_ptr);
    }
    if (rc != 0) {
        throw std::runtime_error("softmax failed");
    }
    return output;
}

static py::object transformer_rmsnorm(
        py::array_t<double> input,
        py::array_t<double> weight,
        double eps) {
    auto in_buf = input.request();
    auto w_buf = weight.request();
    if (in_buf.ndim != 2 || w_buf.ndim != 1) {
        throw std::invalid_argument("rmsnorm requires a 2-D input and 1-D weight");
    }
    int64_t rows = static_cast<int64_t>(in_buf.shape[0]);
    int64_t cols = static_cast<int64_t>(in_buf.shape[1]);
    if (w_buf.shape[0] != cols) {
        throw std::invalid_argument("rmsnorm weight length must match input columns");
    }

    py::array_t<double> output({static_cast<py::ssize_t>(rows), static_cast<py::ssize_t>(cols)});
    auto out_buf = output.request();
    int rc;
    {
        py::gil_scoped_release release;
        rc = gptbridge_native_transformer_rmsnorm(
            static_cast<const double*>(in_buf.ptr), rows, cols,
            static_cast<const double*>(w_buf.ptr), eps,
            static_cast<double*>(out_buf.ptr));
    }
    if (rc != 0) {
        throw std::runtime_error("rmsnorm failed");
    }
    return output;
}

static py::object transformer_rope(
        py::array_t<double> input,
        py::array_t<double> cos_table,
        py::array_t<double> sin_table) {
    auto in_buf = input.request();
    auto cos_buf = cos_table.request();
    auto sin_buf = sin_table.request();
    if (in_buf.ndim != 4 || cos_buf.ndim != 3 || sin_buf.ndim != 3) {
        throw std::invalid_argument("rope requires [B,H,S,D] input and [B,S,D] tables");
    }
    int64_t batch = static_cast<int64_t>(in_buf.shape[0]);
    int64_t heads = static_cast<int64_t>(in_buf.shape[1]);
    int64_t seq_len = static_cast<int64_t>(in_buf.shape[2]);
    int64_t head_dim = static_cast<int64_t>(in_buf.shape[3]);
    if (head_dim <= 0 || head_dim % 2 != 0) {
        throw std::invalid_argument("rope head_dim must be positive and even");
    }
    if (cos_buf.shape[0] != batch || cos_buf.shape[1] != seq_len || cos_buf.shape[2] != head_dim ||
        sin_buf.shape[0] != batch || sin_buf.shape[1] != seq_len || sin_buf.shape[2] != head_dim) {
        throw std::invalid_argument("rope cos/sin tables must have shape [B,S,D]");
    }

    py::array_t<double> output({
        static_cast<py::ssize_t>(batch), static_cast<py::ssize_t>(heads),
        static_cast<py::ssize_t>(seq_len), static_cast<py::ssize_t>(head_dim)});
    auto out_buf = output.request();
    int rc;
    {
        py::gil_scoped_release release;
        rc = gptbridge_native_transformer_rope(
            static_cast<const double*>(in_buf.ptr), batch, heads, seq_len, head_dim,
            static_cast<const double*>(cos_buf.ptr),
            static_cast<const double*>(sin_buf.ptr),
            static_cast<double*>(out_buf.ptr));
    }
    if (rc != 0) {
        throw std::runtime_error("rope failed");
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
        rc = gptbridge_native_transformer_scaled_dot_product_attention(
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

static py::object transformer_attention_online(
        py::array_t<double> q,
        py::array_t<double> k,
        py::array_t<double> v,
        int64_t block_k) {
    auto q_buf = q.request();
    auto k_buf = k.request();
    auto v_buf = v.request();
    if (q_buf.ndim != 2 || k_buf.ndim != 2 || v_buf.ndim != 2) {
        throw std::invalid_argument("attention_online requires 2-D arrays");
    }
    int64_t q_rows = static_cast<int64_t>(q_buf.shape[0]);
    int64_t d_k = static_cast<int64_t>(q_buf.shape[1]);
    int64_t k_rows = static_cast<int64_t>(k_buf.shape[0]);
    int64_t d_k_in = static_cast<int64_t>(k_buf.shape[1]);
    int64_t v_rows = static_cast<int64_t>(v_buf.shape[0]);
    int64_t d_v = static_cast<int64_t>(v_buf.shape[1]);
    if (d_k != d_k_in) {
        throw std::invalid_argument("attention_online: Q and K dims must match");
    }
    if (k_rows != v_rows) {
        throw std::invalid_argument("attention_online: K and V rows must match");
    }
    if (block_k <= 0) {
        throw std::invalid_argument("attention_online: block_k must be positive");
    }

    const double* q_ptr = static_cast<const double*>(q_buf.ptr);
    const double* k_ptr = static_cast<const double*>(k_buf.ptr);
    const double* v_ptr = static_cast<const double*>(v_buf.ptr);

    py::array_t<double> output({static_cast<py::ssize_t>(q_rows), static_cast<py::ssize_t>(d_v)});
    auto out_buf = output.request();
    double* out_ptr = static_cast<double*>(out_buf.ptr);

    // Bounded scores workspace: ONE K-block, not [q_rows x k_rows].
    const int64_t blk = block_k < k_rows ? block_k : k_rows;
    std::vector<double> block_scores(static_cast<size_t>(blk), 0.0);

    int rc;
    {
        py::gil_scoped_release release;
        rc = gptbridge_native_transformer_attention_online(
            q_ptr, q_rows, d_k,
            k_ptr, k_rows, d_k_in,
            v_ptr, v_rows, d_v,
            block_k,
            out_ptr, block_scores.data());
    }
    if (rc != 0) {
        throw std::runtime_error("attention_online failed");
    }
    return output;
}

static py::object transformer_matmul_grouped(
        py::array_t<double> a,
        py::array_t<int64_t> group_rows,
        py::list b_list) {
    auto a_buf = a.request();
    auto r_buf = group_rows.request();
    if (a_buf.ndim != 2 || r_buf.ndim != 1) {
        throw std::invalid_argument(
            "matmul_grouped requires 2-D a and 1-D group_rows");
    }
    const int64_t k = static_cast<int64_t>(a_buf.shape[1]);
    const int64_t groups = static_cast<int64_t>(r_buf.shape[0]);
    if (groups <= 0 || static_cast<int64_t>(b_list.size()) != groups) {
        throw std::invalid_argument(
            "matmul_grouped: b_list length must equal group_rows length");
    }

    std::vector<py::array_t<double>> holders;
    std::vector<const double*> b_ptrs;
    holders.reserve(static_cast<size_t>(groups));
    b_ptrs.reserve(static_cast<size_t>(groups));
    int64_t n = -1;
    for (int64_t g = 0; g < groups; ++g) {
        holders.push_back(b_list[g].cast<py::array_t<double>>());
        auto info = holders.back().request();
        if (info.ndim != 2 || info.shape[0] != k ||
            (n >= 0 && info.shape[1] != n)) {
            throw std::invalid_argument(
                "matmul_grouped: each b must be [k x n] with matching dims");
        }
        if (n < 0) n = static_cast<int64_t>(info.shape[1]);
        b_ptrs.push_back(static_cast<const double*>(info.ptr));
    }

    const int64_t* rows_ptr = static_cast<const int64_t*>(r_buf.ptr);
    int64_t total = 0;
    for (int64_t g = 0; g < groups; ++g) {
        if (rows_ptr[g] < 0) {
            throw std::invalid_argument(
                "matmul_grouped: group_rows entries must be non-negative");
        }
        total += rows_ptr[g];
    }
    if (a_buf.shape[0] != total) {
        throw std::invalid_argument(
            "matmul_grouped: a rows must equal sum(group_rows)");
    }

    py::array_t<double> c({static_cast<py::ssize_t>(total),
                           static_cast<py::ssize_t>(n)});
    auto c_buf = c.request();
    int rc;
    {
        py::gil_scoped_release release;
        rc = gptbridge_native_transformer_matmul_grouped(
            static_cast<const double*>(a_buf.ptr), rows_ptr, groups,
            b_ptrs.data(), k, n,
            static_cast<double*>(c_buf.ptr));
    }
    if (rc != 0) {
        throw std::runtime_error("matmul_grouped failed");
    }
    return c;
}

// --- E1 execution-surface prototypes (§10.65 shadow) ---
// Thin holders over the pure-C state machines in native/core/. Python owns
// the structs by value; the C layer performs no I/O and no authority calls.

static const char* WD_STATES[] = {
    "unknown", "connected", "degraded", "disconnected", "starting"};

class NativeWatchdog {
public:
    NativeWatchdog(int64_t min_interval_ms, int64_t max_interval_ms,
                   int32_t dead_threshold, int32_t retry_grace) {
        if (!gptbridge_wd_init(&wd_, min_interval_ms, max_interval_ms,
                               dead_threshold, retry_grace)) {
            throw std::invalid_argument("watchdog init failed");
        }
    }
    py::object probe(bool backend_process_alive, bool backend_http_healthy,
                     bool frontend_connected, int64_t now_ms) {
        gptbridge_wd_probe_t p{backend_process_alive ? 1 : 0,
                               backend_http_healthy ? 1 : 0,
                               frontend_connected ? 1 : 0};
        gptbridge_wd_event_t ev{};
        int repair = gptbridge_wd_probe(&wd_, &p, now_ms, &ev);
        if (ev.from_state == ev.to_state && !repair) {
            return py::none();
        }
        py::dict out;
        out["from_state"] = WD_STATES[ev.from_state];
        out["to_state"] = WD_STATES[ev.to_state];
        out["trigger_repair"] = static_cast<bool>(ev.trigger_repair);
        out["at_ms"] = ev.at_ms;
        out["repair_fired"] = static_cast<bool>(repair);
        return out;
    }
    int64_t next_interval_ms() { return gptbridge_wd_next_interval_ms(&wd_); }
    std::string state() const { return WD_STATES[wd_.state]; }
    int32_t consecutive_dead() const { return wd_.consecutive_dead; }
    int64_t probe_count() const { return wd_.probe_count; }

private:
    gptbridge_wd_t wd_{};
};

static void sched_count_tick(void* ctx) {
    ++(*static_cast<int64_t*>(ctx));
}

class NativeScheduler {
public:
    NativeScheduler() {
        if (!gptbridge_sched_init(&sched_)) {
            throw std::runtime_error("scheduler init failed");
        }
    }
    bool register_job(const std::string& name, int64_t interval_ms,
                      int64_t timeout_ms, int64_t now_ms,
                      bool run_immediately, bool pausable) {
        if (sched_.count >= GPTBRIDGE_SCHED_MAX_JOBS) return false;
        counters_[sched_.count] = 0;
        counter_index_[name] = sched_.count;
        return gptbridge_sched_register(
                   &sched_, name.c_str(), interval_ms, timeout_ms, now_ms,
                   run_immediately ? 1 : 0, pausable ? 1 : 0,
                   &sched_count_tick, &counters_[sched_.count]) != 0;
    }
    bool unregister_job(const std::string& name) {
        auto it = counter_index_.find(name);
        if (it == counter_index_.end()) return false;
        int32_t slot = it->second;
        if (gptbridge_sched_unregister(&sched_, name.c_str()) == 0) {
            return false;
        }
        counters_[slot] = 0;
        /* shift-remove moved later jobs down one slot — rebind ctx pointers
           and the name→slot index from the authoritative job order. */
        counter_index_.clear();
        for (int32_t i = 0; i < sched_.count; ++i) {
            sched_.jobs[i].ctx = &counters_[i];
            counter_index_[sched_.jobs[i].name] = i;
        }
        return true;
    }
    int tick(int64_t now_ms, bool paused) {
        return gptbridge_sched_tick(&sched_, now_ms, paused ? 1 : 0);
    }
    int job_count() const { return gptbridge_sched_job_count(&sched_); }
    py::object job_stats(const std::string& name) const {
        const gptbridge_sched_job_t* j =
            gptbridge_sched_find(&sched_, name.c_str());
        if (j == nullptr) return py::none();
        py::dict out;
        out["run_count"] = j->run_count;
        out["error_count"] = j->error_count;
        out["paused_count"] = j->paused_count;
        out["last_run_ms"] = j->last_run_ms;
        out["last_duration_ms"] = j->last_duration_ms;
        out["next_due_ms"] = j->next_due_ms;
        out["enabled"] = static_cast<bool>(j->enabled);
        return out;
    }

private:
    gptbridge_sched_t sched_{};
    int64_t counters_[GPTBRIDGE_SCHED_MAX_JOBS] = {};
    std::unordered_map<std::string, int32_t> counter_index_{};
};

class NativeOutbox {
public:
    NativeOutbox() {
        if (!gptbridge_ob_init(&reg_)) {
            throw std::runtime_error("outbox init failed");
        }
    }
    bool register_session(const std::string& sid) {
        return gptbridge_ob_register(&reg_, sid.c_str()) != 0;
    }
    bool unregister_session(const std::string& sid) {
        return gptbridge_ob_unregister(&reg_, sid.c_str()) != 0;
    }
    py::dict hello(const std::string& sid, int64_t cursor,
                   bool generation_matches, int64_t latest_sequence) {
        int32_t reset = 0;
        int64_t effective = gptbridge_ob_hello(
            &reg_, sid.c_str(), cursor, generation_matches ? 1 : 0,
            latest_sequence, &reset);
        py::dict out;
        out["effective_cursor"] = effective;
        out["reset"] = static_cast<bool>(reset);
        return out;
    }
    bool ack(const std::string& sid, int64_t cursor) {
        return gptbridge_ob_ack(&reg_, sid.c_str(), cursor) != 0;
    }
    bool resync(const std::string& sid, int64_t cursor) {
        return gptbridge_ob_resync(&reg_, sid.c_str(), cursor) != 0;
    }
    py::dict drain_plan(const std::string& sid, int64_t now_ms,
                        int64_t retry_ms) {
        int64_t start_after = 0, limit = 0;
        int rc = gptbridge_ob_drain_plan(&reg_, sid.c_str(), now_ms, retry_ms,
                                       &start_after, &limit);
        py::dict out;
        out["has_work"] = rc != 0;
        out["start_after"] = start_after;
        out["limit"] = limit;
        return out;
    }
    bool mark_sent(const std::string& sid, int64_t seq, int64_t now_ms) {
        return gptbridge_ob_mark_sent(&reg_, sid.c_str(), seq, now_ms) != 0;
    }
    int64_t next_retry_deadline(int64_t retry_ms) const {
        int64_t deadline = 0;
        gptbridge_ob_next_retry_deadline(&reg_, retry_ms, &deadline);
        return deadline;
    }
    int64_t prune_floor(int64_t latest_sequence) const {
        return gptbridge_ob_prune_floor(&reg_, latest_sequence);
    }
    py::object session(const std::string& sid) const {
        const gptbridge_ob_session_t* s =
            gptbridge_ob_find(&reg_, sid.c_str());
        if (s == nullptr) return py::none();
        py::dict out;
        out["acked"] = s->acked;
        out["sent_upto"] = s->sent_upto;
        out["last_attempt_ms"] = s->last_attempt_ms;
        return out;
    }

private:
    gptbridge_ob_registry_t reg_{};
};

class NativeMaintenance {
public:
    NativeMaintenance(int64_t tick_interval_ms, int64_t max_job_age_ms,
                      int32_t max_retry_attempts, int64_t retry_backoff_ms,
                      int64_t current_generation) {
        if (!gptbridge_mt_init(&mt_, tick_interval_ms, max_job_age_ms,
                               max_retry_attempts, retry_backoff_ms,
                               current_generation)) {
            throw std::invalid_argument("maintenance init failed");
        }
    }
    bool admit(const std::string& job_id, const std::string& action_id,
               int risk_class, int priority, int64_t generation,
               bool system_idle, bool authorized, int64_t now_ms,
               bool system_blocked) {
        gptbridge_mt_job_t job{};
        std::snprintf(job.job_id, GPTBRIDGE_MT_ID_MAX, "%s", job_id.c_str());
        std::snprintf(job.action_id, GPTBRIDGE_MT_ID_MAX, "%s",
                      action_id.c_str());
        job.risk_class = static_cast<gptbridge_mt_class_t>(risk_class);
        job.priority = priority;
        job.status = GPTBRIDGE_MT_PLANNED;
        job.generation = generation;
        job.scheduled_at_ms = now_ms;
        job.next_attempt_ms = now_ms;
        return gptbridge_mt_admit(&mt_, &job, system_idle ? 1 : 0,
                                  authorized ? 1 : 0,
                                  system_blocked ? 1 : 0, now_ms) != 0;
    }
    py::object next_due(int64_t now_ms) {
        gptbridge_mt_job_t* j = gptbridge_mt_next_due(&mt_, now_ms);
        if (j == nullptr) return py::none();
        py::dict out;
        out["job_id"] = j->job_id;
        out["action_id"] = j->action_id;
        out["status"] = static_cast<int>(j->status);
        out["attempt_count"] = j->attempt_count;
        return out;
    }
    bool complete(const std::string& job_id) {
        return gptbridge_mt_complete(&mt_, job_id.c_str()) != 0;
    }
    bool fail(const std::string& job_id, int64_t now_ms) {
        return gptbridge_mt_fail(&mt_, job_id.c_str(), now_ms) != 0;
    }
    bool cancel(const std::string& job_id) {
        return gptbridge_mt_cancel(&mt_, job_id.c_str()) != 0;
    }
    int job_count() const { return mt_.count; }
    /* Non-terminal slots (QUEUED/DEFERRED/RUNNING) — mirrors the Python
       scheduler's len(_queue)+len(_running).  job_count() is the raw table
       size: terminal slots linger until recycled, so it overstates live
       work (P4 shadow desynced-queue metric was counting terminal slots). */
    int live_count() const {
        int n = 0;
        for (int32_t i = 0; i < mt_.count; ++i) {
            const gptbridge_mt_status_t st = mt_.jobs[i].status;
            if (st == GPTBRIDGE_MT_QUEUED || st == GPTBRIDGE_MT_DEFERRED ||
                st == GPTBRIDGE_MT_RUNNING)
                ++n;
        }
        return n;
    }
    void set_generation(int64_t generation) {
        mt_.current_generation = generation;
    }
    /* TTL probe cache (same-tick shared probe result, failure cached too).
       get returns None on miss/expiry, else the stored ok flag. */
    py::object cache_get(int64_t now_ms, int64_t ttl_ms) {
        int32_t ok = 0;
        if (!gptbridge_mt_cache_get(&cache_, now_ms, ttl_ms,
                                    nullptr, &ok)) {
            return py::none();
        }
        return py::bool_(ok != 0);
    }
    void cache_set(int64_t now_ms, bool probe_ok) {
        gptbridge_mt_cache_set(&cache_, now_ms, probe_ok ? 1 : 0, nullptr);
    }

private:
    gptbridge_mt_t mt_{};
    gptbridge_mt_cache_t cache_{};
};

// --- E2 transport/registration-surface prototype (§10.65 shadow) ---
// Thin holder over the pure-C request registry in native/core/ipc_registry.c.
// Python owns the struct by value; the C layer performs no I/O and no
// authority calls (governance gates stay on the Python path).

static const char* REQ_STATES[] = {
    "CREATED", "QUEUED", "RUNNING", "COMPLETED",
    "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"};

// Caller-supplied ms timestamps keep the C core deterministic; a 0 means
// "stamp now" so bindings stay ergonomic for callers without a clock.
static int64_t now_or_host(int64_t now_ms) {
    if (now_ms > 0) return now_ms;
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

class NativeIpcRegistry {
public:
    NativeIpcRegistry() {
        if (!gptbridge_ipc_registry_init(&reg_)) {
            throw std::runtime_error("ipc_registry init failed");
        }
    }
    bool create(const std::string& request_id, int32_t generation,
                int64_t now_ms) {
        return gptbridge_ipc_registry_create(
                   &reg_, request_id.c_str(), generation,
                   now_or_host(now_ms)) != 0;
    }
    bool set_status(const std::string& request_id, int status,
                    int64_t now_ms) {
        return gptbridge_ipc_registry_set_status(
                   &reg_, request_id.c_str(),
                   static_cast<gptbridge_req_status_t>(status),
                   now_or_host(now_ms)) != 0;
    }
    bool cancel(const std::string& request_id, int64_t now_ms) {
        return gptbridge_ipc_registry_cancel(
                   &reg_, request_id.c_str(), now_or_host(now_ms)) != 0;
    }
    bool set_timeout(const std::string& request_id, int64_t timeout_ms) {
        return gptbridge_ipc_registry_set_timeout(
                   &reg_, request_id.c_str(), timeout_ms) != 0;
    }
    py::object find(const std::string& request_id) const {
        const gptbridge_ipc_request_t* r =
            gptbridge_ipc_registry_find(&reg_, request_id.c_str());
        if (r == nullptr) return py::none();
        py::dict out;
        out["request_id"] = r->request_id;
        out["backend_id"] = r->backend_id;
        out["backend_generation"] = r->backend_generation;
        out["status"] = REQ_STATES[r->status];
        out["cancelled"] = static_cast<bool>(r->cancelled);
        out["created_at_ms"] = r->created_at_ms;
        out["started_at_ms"] = r->started_at_ms;
        out["completed_at_ms"] = r->completed_at_ms;
        out["timeout_ms"] = r->timeout_ms;
        out["deadline_ms"] =
            gptbridge_ipc_registry_deadline_ms(&reg_, request_id.c_str());
        return out;
    }
    int count() const { return gptbridge_ipc_registry_count(&reg_); }
    bool transport_send(const std::string& payload, int64_t seq) {
        gptbridge_ipc_transport_msg_t msg{};
        std::snprintf(msg.payload, sizeof(msg.payload), "%s",
                      payload.c_str());
        msg.seq = static_cast<uint64_t>(seq);
        return gptbridge_ipc_transport_send(&msg) != 0;
    }
    py::object transport_recv() {
        gptbridge_ipc_transport_msg_t msg{};
        if (!gptbridge_ipc_transport_recv(&msg)) return py::none();
        py::dict out;
        out["payload"] = msg.payload;
        out["seq"] = static_cast<int64_t>(msg.seq);
        return out;
    }

private:
    gptbridge_ipc_registry_t reg_{};
};

// --- E3 startup/activation-surface prototypes (§10.65 shadow) ---
// Thin holders over the pure-C state machines in native/core/.  The C
// layer performs no I/O, no SQL and no process spawning — persistence,
// transport probes and toolbox calls stay on the Python governed path.

class NativeRuntimeStateRegistry {
public:
    NativeRuntimeStateRegistry() { gptbridge_rs_init(&reg_); }
    bool set_runtime_state(const std::string& module_id,
                           const std::string& state,
                           const py::object& health,
                           const py::object& release_id,
                           const py::object& error,
                           const std::string& now_str) {
        const uint8_t st = gptbridge_rs_runtime_from_name(state.c_str());
        if (st == GPTBRIDGE_RT_UNKNOWN) return false;
        std::string h, r, e;
        const char* ph = nullptr;
        const char* pr = nullptr;
        const char* pe = nullptr;
        if (!health.is_none()) { h = py::cast<std::string>(health); ph = h.c_str(); }
        if (!release_id.is_none()) { r = py::cast<std::string>(release_id); pr = r.c_str(); }
        if (!error.is_none()) { e = py::cast<std::string>(error); pe = e.c_str(); }
        return gptbridge_rs_set_runtime(
                   &reg_, module_id.c_str(), st, ph, pr, pe,
                   now_str.c_str()) != 0;
    }
    bool set_capability_state(const std::string& module_id,
                              const std::string& state,
                              const std::string& now_str) {
        const uint8_t st = gptbridge_rs_capability_from_name(state.c_str());
        if (st == GPTBRIDGE_CAP_UNKNOWN) return false;
        return gptbridge_rs_set_capability(
                   &reg_, module_id.c_str(), st, now_str.c_str()) != 0;
    }
    bool heartbeat(const std::string& module_id,
                   const std::string& now_str, int64_t now_ms) {
        return gptbridge_rs_heartbeat(
                   &reg_, module_id.c_str(), now_str.c_str(),
                   now_or_host(now_ms)) != 0;
    }
    int is_stale(const std::string& module_id, int64_t now_ms,
                 int64_t stale_after_ms) {
        return gptbridge_rs_is_stale(
            &reg_, module_id.c_str(), now_or_host(now_ms),
            stale_after_ms);
    }
    bool record_error(const std::string& module_id,
                      const std::string& error,
                      const std::string& now_str) {
        return gptbridge_rs_record_error(
                   &reg_, module_id.c_str(), error.c_str(),
                   now_str.c_str()) != 0;
    }
    py::object get(const std::string& module_id) const {
        const gptbridge_rs_record_t* r =
            gptbridge_rs_find(&reg_, module_id.c_str());
        if (r == nullptr) return py::none();
        py::dict out;
        out["module_id"] = r->module_id;
        out["runtime_state"] = gptbridge_rs_runtime_name(r->runtime_state);
        out["capability_state"] =
            gptbridge_rs_capability_name(r->capability_state);
        out["health"] = r->health;
        out["release_id"] = r->release_id;
        out["last_heartbeat"] = r->last_heartbeat;
        out["last_error"] = r->last_error;
        out["recovery_attempts"] = r->recovery_attempts;
        out["updated_at"] = r->updated_at;
        return out;
    }
    py::dict aggregate() const {
        int32_t by_rt[8], by_cap[5], failed_n = 0;
        char failed[GPTBRIDGE_RS_MAX_MODULES][GPTBRIDGE_RS_ID_MAX];
        const int32_t total = gptbridge_rs_aggregate(
            &reg_, by_rt, by_cap, failed, GPTBRIDGE_RS_MAX_MODULES,
            &failed_n);
        py::dict rt, cap;
        for (uint8_t s = 1; s <= 7; ++s) {
            if (by_rt[s] > 0) rt[gptbridge_rs_runtime_name(s)] = by_rt[s];
        }
        for (uint8_t s = 1; s <= 4; ++s) {
            if (by_cap[s] > 0) {
                cap[gptbridge_rs_capability_name(s)] = by_cap[s];
            }
        }
        py::list fl;
        const int32_t shown = failed_n < GPTBRIDGE_RS_MAX_MODULES
                                  ? failed_n : GPTBRIDGE_RS_MAX_MODULES;
        for (int32_t i = 0; i < shown; ++i) fl.append(failed[i]);
        py::dict out;
        out["schema"] = "star-runtime-state/v1";
        out["module_count"] = total;
        out["by_runtime_state"] = rt;
        out["by_capability_state"] = cap;
        out["failed_modules"] = fl;
        return out;
    }
    int count() const { return reg_.count; }

private:
    gptbridge_rs_registry_t reg_{};
};

class NativeActivationBroker {
public:
    NativeActivationBroker(double cooldown_s, double min_backoff_s,
                           double max_backoff_s) {
        if (!gptbridge_act_init(&broker_, cooldown_s, min_backoff_s,
                                max_backoff_s)) {
            throw std::runtime_error("activation broker init failed");
        }
    }
    std::string ensure(const py::dict& inputs) {
        gptbridge_act_inputs_t in{};
        in.pending = _flag(inputs, "pending");
        in.maintenance_ready = _flag(inputs, "maintenance_ready");
        in.shutting_down = _flag(inputs, "shutting_down");
        in.admission_hold = _flag(inputs, "admission_hold");
        in.liveness_known = _flag(inputs, "liveness_known");
        in.owner_active = _flag(inputs, "owner_active");
        in.regulation_active = _flag(inputs, "regulation_active");
        in.now_monotonic = inputs.contains("now_monotonic")
                               ? py::cast<double>(inputs["now_monotonic"])
                               : 0.0;
        return gptbridge_act_decision_name(
            gptbridge_act_ensure(&broker_, &in));
    }
    std::string on_start_result(bool ok, double now_monotonic) {
        return gptbridge_act_decision_name(gptbridge_act_on_start_result(
            &broker_, ok ? 1 : 0, now_monotonic));
    }
    std::string on_release_result(bool ok, double now_monotonic) {
        return gptbridge_act_decision_name(gptbridge_act_on_release_result(
            &broker_, ok ? 1 : 0, now_monotonic));
    }
    void note_explicit_stop(double now_monotonic, double wall_time) {
        gptbridge_act_note_explicit_stop(&broker_, now_monotonic, wall_time);
    }
    py::dict status() const {
        py::dict out;
        out["attempts"] = broker_.attempts;
        out["backoff_seconds"] = broker_.backoff_s;
        out["next_attempt_at"] = broker_.next_attempt_at;
        out["next_release_at"] = broker_.next_release_at;
        out["broker_started_owner"] =
            static_cast<bool>(broker_.broker_started_owner);
        out["explicit_stop_at"] = broker_.explicit_stop_at;
        return out;
    }
    static double poll_interval(bool pending, double idle_s,
                                double pending_s) {
        return gptbridge_act_poll_interval(pending ? 1 : 0, idle_s,
                                           pending_s);
    }
    static bool state_write_due(bool fingerprint_changed, double now,
                                double last_write_at, double heartbeat_s) {
        return gptbridge_act_state_write_due(
                   fingerprint_changed ? 1 : 0, now, last_write_at,
                   heartbeat_s) != 0;
    }

private:
    static int32_t _flag(const py::dict& d, const char* key) {
        return d.contains(key) && py::cast<bool>(d[key]) ? 1 : 0;
    }
    gptbridge_act_broker_t broker_{};
};

// --- M1 system-rescue prototype adapters (stateless) -------------------
// Thin boundary: facts are injected by the Python caller; this layer
// performs no I/O and no process spawning.

static py::str sr_verify_tool_package_name(bool release_dir_exists,
                                           bool metadata_exists,
                                           bool metadata_valid,
                                           bool has_source_manifest,
                                           int64_t source_mtime,
                                           int64_t package_mtime) {
    return py::str(gptbridge_sr_pkg_verdict_code(
        gptbridge_sr_verify_tool_package(
            release_dir_exists ? 1 : 0, metadata_exists ? 1 : 0,
            metadata_valid ? 1 : 0, has_source_manifest ? 1 : 0,
            source_mtime, package_mtime)));
}

static py::object sr_normalize_packager_error(const py::object& error_code) {
    if (error_code.is_none()) return py::none();
    const std::string code = py::cast<std::string>(error_code);
    const char* out = gptbridge_sr_normalize_packager_error(code.c_str());
    if (out == nullptr) return py::none();
    return py::str(out);
}

static bool sr_all_ok(const py::iterable& oks) {
    std::vector<int32_t> values;
    for (const auto& item : oks) {
        values.push_back(py::cast<bool>(item) ? 1 : 0);
    }
    return gptbridge_sr_all_ok(values.data(),
                               static_cast<int32_t>(values.size())) != 0;
}

static py::str sr_verify_archive_name(bool file_exists,
                                      bool sidecar_exists,
                                      const py::object& expected_hex,
                                      const py::object& actual_hex) {
    std::string expected, actual;
    const char* pe = nullptr;
    const char* pa = nullptr;
    if (!expected_hex.is_none()) {
        expected = py::cast<std::string>(expected_hex);
        pe = expected.c_str();
    }
    if (!actual_hex.is_none()) {
        actual = py::cast<std::string>(actual_hex);
        pa = actual.c_str();
    }
    return py::str(gptbridge_sr_arc_verdict_code(gptbridge_sr_verify_archive(
        file_exists ? 1 : 0, sidecar_exists ? 1 : 0, pe, pa)));
}

static py::str sr_sha256_hex(const py::bytes& data) {
    const std::string bytes = data.cast<std::string>();
    char hex[65];
    if (!gptbridge_sr_sha256_hex(
            reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size(),
            hex)) {
        throw std::runtime_error("sha256 failed");
    }
    return py::str(hex);
}

static py::str sr_cli_dispatch_name(bool all_flag,
                                    const py::object& tool_id,
                                    bool verify_flag,
                                    bool deep_flag,
                                    bool package_flag) {
    std::string tool;
    const char* pt = nullptr;
    if (!tool_id.is_none()) {
        tool = py::cast<std::string>(tool_id);
        pt = tool.c_str();
    }
    return py::str(gptbridge_sr_cli_op_name(gptbridge_sr_cli_dispatch(
        all_flag ? 1 : 0, pt, verify_flag ? 1 : 0, deep_flag ? 1 : 0,
        package_flag ? 1 : 0)));
}

// --- M1 governed-tool-runtime ABI adapters (star-governed-tool-runtime-abi/v1)
// Stateless decision subset; transport/token/network stay in Python.

static py::str gt_workspace_instance_id(const std::string& tool_id,
                                        int64_t port) {
    char out[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1];
    if (!gptbridge_gt_workspace_instance_id(tool_id.c_str(), port, out)) {
        throw std::runtime_error("workspace_instance_id failed");
    }
    return py::str(out);
}

static bool gt_env_gate(bool project_root_ok, bool tool_dir_ok,
                        const std::string& session_token, int64_t port,
                        bool bootstrap_ok) {
    return gptbridge_gt_env_gate(
               project_root_ok ? 1 : 0, tool_dir_ok ? 1 : 0,
               session_token.c_str(), port, bootstrap_ok ? 1 : 0) != 0;
}

static bool gt_shutdown_gate(const py::object& env_token,
                             const py::object& provided_token) {
    std::string env, provided;
    const char* pe = nullptr;
    const char* pp2 = nullptr;
    if (!env_token.is_none()) {
        env = py::cast<std::string>(env_token);
        pe = env.c_str();
    }
    if (!provided_token.is_none()) {
        provided = py::cast<std::string>(provided_token);
        pp2 = provided.c_str();
    }
    return gptbridge_gt_shutdown_gate(pe, pp2) != 0;
}

static bool gt_ws_gate(const std::string& session_token,
                       const std::string& provided_token,
                       const std::string& expected_instance,
                       const std::string& provided_instance) {
    return gptbridge_gt_ws_gate(session_token.c_str(),
                                provided_token.c_str(),
                                expected_instance.c_str(),
                                provided_instance.c_str()) != 0;
}

static bool gt_request_valid(const std::string& command,
                             bool payload_is_dict,
                             const std::string& request_id,
                             const py::object& payload_tool_id,
                             const std::string& self_tool_id) {
    std::string tool;
    const char* pt = nullptr;
    if (!payload_tool_id.is_none()) {
        tool = py::cast<std::string>(payload_tool_id);
        pt = tool.c_str();
    }
    return gptbridge_gt_request_valid(command.c_str(),
                                      payload_is_dict ? 1 : 0,
                                      request_id.c_str(), pt,
                                      self_tool_id.c_str()) != 0;
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
    m.def("system_memory_total_bytes",
          &gptbridge_native_system_memory_total_bytes,
          "Total physical RAM in bytes, or -1.");
    m.def("system_memory_available_bytes",
          &gptbridge_native_system_memory_available_bytes,
          "Available physical RAM in bytes, or -1.");
    m.def("cpu_count", &gptbridge_native_cpu_count,
          "Logical CPU count, or 0.");
    m.def("process_alive", &gptbridge_native_process_alive,
          "1 when pid exists and is running, else 0.");
    m.def("process_name",
          [](int64_t pid) -> py::object {
              char buf[512];
              int64_t n =
                  gptbridge_native_process_name(pid, buf, (int64_t)sizeof(buf));
              if (n < 0) return py::none();
              return py::str(buf, (size_t)n);
          },
          "Process image base name for pid, or None.");
    m.def("process_working_set_bytes",
          &gptbridge_native_process_working_set_bytes_for,
          "Working-set bytes of pid, or -1.");
    m.def("process_private_bytes",
          &gptbridge_native_process_private_bytes_for,
          "Private bytes of pid, or -1.");
    m.def("process_cpu_times",
          [](int64_t pid) -> py::object {
              int64_t kernel = 0, user = 0;
              if (!gptbridge_native_process_cpu_times_100ns(
                      pid, &kernel, &user))
                  return py::none();
              return py::make_tuple(kernel, user);
          },
          "Kernel+user 100ns times for pid, or None.");
    m.def("process_list",
          [](int64_t max_count) -> py::object {
              if (max_count <= 0 || max_count > 65536) return py::none();
              std::vector<int64_t> buf((size_t)max_count);
              int n = gptbridge_native_process_list(
                  buf.data(), max_count);
              if (n < 0) return py::none();
              buf.resize((size_t)n);
              return py::cast(std::move(buf));
          },
          "Enumerate live pids (bounded), or None.");
    m.def("process_terminate", &gptbridge_native_process_terminate,
          "Terminate pid; 1 on success, 0 otherwise.");
    m.def("process_children",
          [](int64_t root_pid, int64_t max_count) -> py::object {
              if (root_pid <= 0 || max_count <= 0 || max_count > 65536)
                  return py::none();
              std::vector<int64_t> buf((size_t)max_count);
              int n = gptbridge_native_process_children(
                  root_pid, buf.data(), max_count);
              if (n < 0) return py::none();
              buf.resize((size_t)n);
              return py::cast(std::move(buf));
          },
          "All descendant pids of root_pid (recursive), or None.");
    m.def("process_exe",
          [](int64_t pid) -> py::object {
              char buf[1024];
              int64_t n =
                  gptbridge_native_process_exe(pid, buf, (int64_t)sizeof(buf));
              if (n < 0) return py::none();
              return py::str(buf, (size_t)n);
          },
          "Full image path of pid, or None.");
    m.def("process_cmdline",
          [](int64_t pid) -> py::object {
              char buf[8192];
              int64_t n = gptbridge_native_process_cmdline(
                  pid, buf, (int64_t)sizeof(buf));
              if (n < 0) return py::none();
              return py::str(buf, (size_t)n);
          },
          "Command line of pid, or None.");
    m.def("tcp_listen_pid", &gptbridge_native_tcp_listen_pid,
          "Pid listening on a TCP port, or -1.");

    m.def("process_num_threads", &gptbridge_native_process_num_threads,
          "Thread count of pid, or -1.");
    m.def("process_num_handles", &gptbridge_native_process_num_handles,
          "Open handle count of pid, or -1.");
    m.def("process_parent", &gptbridge_native_process_parent,
          "Parent pid, or -1.");
    m.def("process_create_time_ms",
          &gptbridge_native_process_create_time_ms,
          "Process creation time as Unix-epoch ms, or -1.");
    m.def("process_io_counters",
          [](int64_t pid) -> py::object {
              int64_t rd = 0, wr = 0;
              if (!gptbridge_native_process_io_counters(pid, &rd, &wr))
                  return py::none();
              return py::make_tuple(rd, wr);
          },
          "(read_bytes, write_bytes) of pid, or None.");
    m.def("process_username",
          [](int64_t pid) -> py::object {
              char buf[512];
              int64_t n = gptbridge_native_process_username(
                  pid, buf, (int64_t)sizeof(buf));
              if (n < 0) return py::none();
              return py::str(buf, (size_t)n);
          },
          "DOMAIN\\user of pid, or None.");
    m.def("process_set_priority",
          &gptbridge_native_process_set_priority,
          "Set priority class; 1 on success.");
    m.def("process_get_priority",
          &gptbridge_native_process_get_priority,
          "Priority class value, or -1.");
    m.def("process_set_affinity",
          &gptbridge_native_process_set_affinity,
          "Set CPU affinity mask; 1 on success.");
    m.def("process_get_affinity",
          &gptbridge_native_process_get_affinity,
          "CPU affinity mask, or -1.");
    m.def("process_wait", &gptbridge_native_process_wait,
          "Wait for pid exit up to timeout_ms; 1 exited, 0 timeout.");
    m.def("system_cpu_times",
          []() -> py::object {
              int64_t idle = 0, kernel = 0, user = 0;
              if (!gptbridge_native_system_cpu_times_100ns(
                      &idle, &kernel, &user))
                  return py::none();
              return py::make_tuple(idle, kernel, user);
          },
          "System idle/kernel/user 100ns times, or None.");

    // Directory change notification (P14 event-driven invalidation).
    // Handles cross the boundary as opaque integers (0 = open failed).
    m.def("dirwatch_open",
          [](const std::wstring& path) -> uintptr_t {
              return reinterpret_cast<uintptr_t>(
                  gptbridge_native_dirwatch_open(path.c_str()));
          },
          "Open a recursive dir-change watch on path; handle or 0.");
    m.def("dirwatch_wait",
          [](uintptr_t handle, int64_t timeout_ms) -> int {
              return gptbridge_native_dirwatch_wait(
                  reinterpret_cast<void*>(handle), timeout_ms);
          },
          "Wait for a change: 1 changed (re-armed), 0 timeout, -1 err.");
    m.def("dirwatch_close",
          [](uintptr_t handle) {
              gptbridge_native_dirwatch_close(
                  reinterpret_cast<void*>(handle));
          },
          "Close a dirwatch handle; safe on 0.");

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
    m.def("transformer_rmsnorm", &transformer_rmsnorm,
          "RMSNorm over the last dimension of a 2-D float array.");
    m.def("transformer_rope", &transformer_rope,
          "RoPE over a [B,H,S,D] float array with [B,S,D] cos/sin tables.");
    m.def("transformer_matmul_grouped", &transformer_matmul_grouped,
          "Grouped matmul over concatenated row-blocks (R5 MoE grouped GEMM)");
    m.def("transformer_attention_online", &transformer_attention_online,
          "Online (blocked) scaled dot-product attention, bounded scores workspace");
    m.def("transformer_scaled_dot_product_attention",
          &transformer_scaled_dot_product_attention,
          "Scaled dot-product attention: Q, K, V -> output.");

    // E1 execution-surface prototypes (§10.65 shadow mode).
    py::class_<NativeWatchdog>(m, "NativeWatchdog")
        .def(py::init<int64_t, int64_t, int32_t, int32_t>())
        .def("probe", &NativeWatchdog::probe)
        .def("next_interval_ms", &NativeWatchdog::next_interval_ms)
        .def("state", &NativeWatchdog::state)
        .def("consecutive_dead", &NativeWatchdog::consecutive_dead)
        .def("probe_count", &NativeWatchdog::probe_count);

    py::class_<NativeScheduler>(m, "NativeScheduler")
        .def(py::init<>())
        .def("register_job", &NativeScheduler::register_job,
             py::arg("name"), py::arg("interval_ms"), py::arg("timeout_ms"),
             py::arg("now_ms") = 0, py::arg("run_immediately") = false,
             py::arg("pausable") = false)
        .def("unregister_job", &NativeScheduler::unregister_job)
        .def("tick", &NativeScheduler::tick,
             py::arg("now_ms"), py::arg("paused") = false)
        .def("job_count", &NativeScheduler::job_count)
        .def("job_stats", &NativeScheduler::job_stats);

    py::class_<NativeOutbox>(m, "NativeOutbox")
        .def(py::init<>())
        .def("register_session", &NativeOutbox::register_session)
        .def("unregister_session", &NativeOutbox::unregister_session)
        .def("hello", &NativeOutbox::hello)
        .def("ack", &NativeOutbox::ack)
        .def("resync", &NativeOutbox::resync)
        .def("drain_plan", &NativeOutbox::drain_plan)
        .def("mark_sent", &NativeOutbox::mark_sent)
        .def("next_retry_deadline", &NativeOutbox::next_retry_deadline)
        .def("prune_floor", &NativeOutbox::prune_floor)
        .def("session", &NativeOutbox::session);

    py::class_<NativeMaintenance>(m, "NativeMaintenance")
        .def(py::init<int64_t, int64_t, int32_t, int64_t, int64_t>())
        .def("admit", &NativeMaintenance::admit,
             py::arg("job_id"), py::arg("action_id"), py::arg("risk_class"),
             py::arg("priority"), py::arg("generation"),
             py::arg("system_idle"), py::arg("authorized"),
             py::arg("now_ms"), py::arg("system_blocked") = false)
        .def("next_due", &NativeMaintenance::next_due)
        .def("complete", &NativeMaintenance::complete)
        .def("fail", &NativeMaintenance::fail)
        .def("cancel", &NativeMaintenance::cancel)
        .def("job_count", &NativeMaintenance::job_count)
        .def("live_count", &NativeMaintenance::live_count)
        .def("set_generation", &NativeMaintenance::set_generation)
        .def("cache_get", &NativeMaintenance::cache_get)
        .def("cache_set", &NativeMaintenance::cache_set);

    // E2 transport/registration-surface prototype (§10.65 shadow mode).
    py::class_<NativeIpcRegistry>(m, "NativeIpcRegistry")
        .def(py::init<>())
        .def("create", &NativeIpcRegistry::create,
             py::arg("request_id"), py::arg("generation"),
             py::arg("now_ms") = 0)
        .def("set_status", &NativeIpcRegistry::set_status,
             py::arg("request_id"), py::arg("status"), py::arg("now_ms") = 0)
        .def("cancel", &NativeIpcRegistry::cancel,
             py::arg("request_id"), py::arg("now_ms") = 0)
        .def("set_timeout", &NativeIpcRegistry::set_timeout,
             py::arg("request_id"), py::arg("timeout_ms"))
        .def("find", &NativeIpcRegistry::find)
        .def("count", &NativeIpcRegistry::count)
        .def("transport_send", &NativeIpcRegistry::transport_send)
        .def("transport_recv", &NativeIpcRegistry::transport_recv);

    // E3 startup/activation-surface prototypes (§10.65 shadow mode).
    py::class_<NativeRuntimeStateRegistry>(m, "NativeRuntimeStateRegistry")
        .def(py::init<>())
        .def("set_runtime_state", &NativeRuntimeStateRegistry::set_runtime_state)
        .def("set_capability_state",
             &NativeRuntimeStateRegistry::set_capability_state)
        .def("heartbeat", &NativeRuntimeStateRegistry::heartbeat,
             py::arg("module_id"), py::arg("now_str"),
             py::arg("now_ms") = 0)
        .def("is_stale", &NativeRuntimeStateRegistry::is_stale,
             py::arg("module_id"), py::arg("now_ms"),
             py::arg("stale_after_ms"))
        .def("record_error", &NativeRuntimeStateRegistry::record_error)
        .def("get", &NativeRuntimeStateRegistry::get)
        .def("aggregate", &NativeRuntimeStateRegistry::aggregate)
        .def("count", &NativeRuntimeStateRegistry::count);

    py::class_<NativeActivationBroker>(m, "NativeActivationBroker")
        .def(py::init<double, double, double>())
        .def("ensure", &NativeActivationBroker::ensure)
        .def("on_start_result", &NativeActivationBroker::on_start_result)
        .def("on_release_result", &NativeActivationBroker::on_release_result)
        .def("note_explicit_stop", &NativeActivationBroker::note_explicit_stop)
        .def("status", &NativeActivationBroker::status)
        .def_static("poll_interval", &NativeActivationBroker::poll_interval)
        .def_static("state_write_due",
                    &NativeActivationBroker::state_write_due);

    // M1 system-rescue shadow prototype (module-language-migration-order).
    m.def("sr_verify_tool_package", &sr_verify_tool_package_name,
          "system-rescue _verify_tool_package verdict (code string).");
    m.def("sr_normalize_packager_error", &sr_normalize_packager_error,
          "Normalize a packager error_code; None for no error.");
    m.def("sr_all_ok", &sr_all_ok,
          "verify_all_packages aggregation (empty -> True).");
    m.def("sr_verify_archive", &sr_verify_archive_name,
          "verify_packaged_tool verdict (code string).");
    m.def("sr_sha256_hex", &sr_sha256_hex,
          "SHA-256 hex digest, equal to hashlib.sha256().hexdigest().");
    m.def("sr_cli_dispatch", &sr_cli_dispatch_name,
          "system-rescue CLI arg routing (operation name string).");

    // M1 governed-tool-runtime ABI decision subset (mode B shadow).
    m.def("gt_tool_id_valid", &gptbridge_gt_tool_id_valid,
          "tool_id regex ^[a-z0-9][a-z0-9_-]{1,63}$.");
    m.def("gt_session_token_valid", &gptbridge_gt_session_token_valid,
          "session token format ^[a-f0-9]{64}$.");
    m.def("gt_port_valid", &gptbridge_gt_port_valid,
          "IPC port range 1024-65535.");
    m.def("gt_env_gate", &gt_env_gate,
          "Conjunction of injected env/bootstrap facts (PERMISSION_DENIED).");
    m.def("gt_workspace_instance_id", &gt_workspace_instance_id,
          "sha256('{tool_id}:{port}')[:16] workspace instance id.");
    m.def("gt_shutdown_gate", &gt_shutdown_gate,
          "/shutdown token gate (hmac.compare_digest semantics).");
    m.def("gt_ws_gate", &gt_ws_gate,
          "WS upgrade gate (lowercased token + instance compare).");
    m.def("gt_request_valid", &gt_request_valid,
          "Command pre-validation (PERMISSION_DENIED form).");
    m.def("gt_idle_next_ms", &gptbridge_gt_idle_next_ms,
          "idle_poll evolution (notify/request -> 250, timeout x1.5 cap 500).");
    m.def("gt_wait_timeout_ms", &gptbridge_gt_wait_timeout_ms,
          "Wait timeout: notify pending -> 50, else max(idle_poll, 50).");
    m.def("gt_health_degraded", &gptbridge_gt_health_degraded,
          "channel_health degraded at >=3 consecutive failures.");
}
