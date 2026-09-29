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

#include "_binding_wrappers.h"
#include "_binding_surfaces_a.h"
#include "_binding_surfaces_b.h"
#include "_binding_adapters.h"

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
          py::call_guard<py::gil_scoped_release>(),
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
        .def("collect_due", &NativeScheduler::collect_due,
             py::arg("now_ms"), py::arg("paused") = false)
        .def("record", &NativeScheduler::record,
             py::arg("name"), py::arg("started_ms"),
             py::arg("duration_ms"), py::arg("error") = false)
        .def("min_due_ms", &NativeScheduler::min_due_ms)
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
        .def("requeue", &NativeMaintenance::requeue)
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
        .def("set_status_rc", &NativeIpcRegistry::set_status_rc,
             py::arg("request_id"), py::arg("status"), py::arg("now_ms") = 0)
        .def("merge_status", &NativeIpcRegistry::merge_status,
             py::arg("request_id"), py::arg("status"), py::arg("now_ms") = 0)
        .def("request_cancel", &NativeIpcRegistry::request_cancel,
             py::arg("request_id"))
        .def("set_backend", &NativeIpcRegistry::set_backend,
             py::arg("request_id"), py::arg("backend_id"))
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
        .def("restore", &NativeRuntimeStateRegistry::restore)
        .def("modules", &NativeRuntimeStateRegistry::modules)
        .def("count", &NativeRuntimeStateRegistry::count);

    py::class_<NativeActivationBroker>(m, "NativeActivationBroker")
        .def(py::init<double, double, double>())
        .def("ensure", &NativeActivationBroker::ensure)
        .def("on_start_result", &NativeActivationBroker::on_start_result)
        .def("on_release_result", &NativeActivationBroker::on_release_result)
        .def("note_explicit_stop", &NativeActivationBroker::note_explicit_stop)
        .def("restore", &NativeActivationBroker::restore)
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
