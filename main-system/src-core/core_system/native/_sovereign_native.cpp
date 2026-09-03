// _sovereign_native: C++ native kernel for the System Sovereign hybrid
// architecture (Python orchestration + C++ resource/liveness primitives).
//
// Built by build_native.py into a _sovereign_native.pyd placed next to this
// source so the Python adapters can import it with a relative import.
//
// The sovereigns (RuntimeSovereign and MaintenanceSovereign) orchestrate in
// Python but delegate performance-/resource-critical primitives to this module:
//   * process working-set / private-memory statistics (Windows PSAPI)
//   * high-resolution monotonic timing
//   * working-set trimming (empty working set)
//   * process CPU usage (Windows)
//
// All imports are resolved through pybind11; the extension is optional and the
// Python adapters provide a graceful fallback if the .pyd is not present, so
// the platform keeps running without it.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <chrono>
#include <cstdint>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <psapi.h>
#else
#include <ctime>
#endif

namespace py = pybind11;

namespace gbsovereign {

// High-resolution monotonic clock in fractional seconds.
double monotonic_seconds() {
    using clock = std::chrono::steady_clock;
    const auto now = clock::now();
    const auto duration = now.time_since_epoch();
    return std::chrono::duration<double>(duration).count();
}

// Whether the current platform is Windows.
bool is_windows() {
#ifdef _WIN32
    return true;
#else
    return false;
#endif
}

// Process working-set size in bytes (number of pages physically touched).
// Returns -1 on error / unsupported platform.
std::int64_t working_set_bytes() {
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS counters{};
    if (GetProcessMemoryInfo(
            GetCurrentProcess(), &counters, sizeof(counters)) == 0) {
        return -1;
    }
    return static_cast<std::int64_t>(counters.WorkingSetSize);
#else
    return -1;
#endif
}

// Process private (non-shared) memory usage in bytes using the EX structure,
// which carries PrivateUsage. Returns -1 on error / unsupported platform.
std::int64_t private_bytes() {
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS_EX counters{};
    counters.cb = sizeof(counters);
    if (GetProcessMemoryInfo(
            GetCurrentProcess(),
            reinterpret_cast<PPROCESS_MEMORY_COUNTERS>(&counters),
            sizeof(counters)) == 0) {
        return -1;
    }
    return static_cast<std::int64_t>(counters.PrivateUsage);
#else
    return -1;
#endif
}

// Empty the process working set, returning the pages freed to the OS when
// possible. Returns true on success / false otherwise.
bool release_working_set() {
#ifdef _WIN32
    const HANDLE process = GetCurrentProcess();
    return EmptyWorkingSet(process) != 0;
#else
    return false;
#endif
}

}  // namespace gbsovereign

PYBIND11_MODULE(_sovereign_native, m) {
    m.doc() = "GPTBridge Sovereign native kernel (hybrid Python/C++ architecture).";
    m.def("monotonic_seconds", &gbsovereign::monotonic_seconds,
          "High-resolution monotonic clock in fractional seconds.");
    m.def("is_windows", &gbsovereign::is_windows,
          "Whether the current platform is Windows.");
    m.def("working_set_bytes", &gbsovereign::working_set_bytes,
          "Process working-set size in bytes, or -1 on error.");
    m.def("private_bytes", &gbsovereign::private_bytes,
          "Process private memory usage in bytes, or -1 on error.");
    m.def("release_working_set", &gbsovereign::release_working_set,
          "Empty the process working set; returns success flag.");
}
