// _binding: pybind11 binding for the canonical native interface (A221/E186).
//
// Binding-only (A220/E185): wraps the C functions declared in
// native/include/gptbridge_native.h and implemented in
// native/bridge/gptbridge_native.c.  Does not duplicate bridge logic.
//
// Built by build_native.py into _sovereign_native.pyd placed next to this
// source so the Python adapters can import it with a relative import.
// The extension is optional; Python adapters provide graceful fallback
// if the .pyd is not present (A219/E184).

#include <pybind11/pybind11.h>

#include "gptbridge_native.h"

namespace py = pybind11;

PYBIND11_MODULE(_sovereign_native, m) {
    m.doc() = "GPTBridge native kernel (canonical C interface via native/bridge).";
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
}
