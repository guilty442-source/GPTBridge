// Suite stubs: model-runtime dependent suites (baseline / eval / dialogue).
// These require the native model runtime and checkpoint; they are reported as
// BLOCKED_MODEL_RUNTIME (never PASS) until the native runtime lands.
#include "harness.hpp"

int main(int argc, char** argv) {
    const char* suite = argc > 1 ? argv[1] : "MODEL_RUNTIME_SUITE";
    native_tests::record_blocked(suite, "requires_native_model_runtime",
                                 "BLOCKED_MODEL_RUNTIME: checkpoint and native runtime required");
    const std::string out = std::string(suite) + ".json";
    return native_tests::report(out.c_str());
}
