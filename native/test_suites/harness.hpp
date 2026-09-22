// Native test-suite harness (C++17, no external dependencies).
#pragma once

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace native_tests {

struct CaseResult {
    std::string suite;
    std::string name;
    bool passed;
    bool blocked;
    std::string detail;
    double ms;
};

inline std::vector<CaseResult>& results() {
    static std::vector<CaseResult> store;
    return store;
}

inline double now_ms() {
    using clock = std::chrono::steady_clock;
    static const auto start = clock::now();
    return std::chrono::duration<double, std::milli>(clock::now() - start).count();
}

class Suite {
public:
    explicit Suite(const char* name) : name_(name) {}
    const std::string& name() const { return name_; }
private:
    std::string name_;
};

inline void record(const std::string& suite, const std::string& name, bool passed, const std::string& detail, double ms) {
    results().push_back({suite, name, passed, false, detail, ms});
}

inline void record_blocked(const std::string& suite, const std::string& name, const std::string& detail) {
    results().push_back({suite, name, false, true, detail, 0.0});
}

#define NT_SUITE(NAME) static native_tests::Suite nt_suite_##__LINE__(NAME)

#define NT_TEST(SUITE, NAME)                                                   \
    do {                                                                       \
        const double nt_t0 = native_tests::now_ms();                           \
        bool nt_ok = true;                                                     \
        std::string nt_detail;                                                 \
        [&]() {                                                                \
            try {

#define NT_END_TEST(SUITE, NAME)                                               \
            } catch (const std::exception& nt_error) {                         \
                nt_ok = false;                                                 \
                nt_detail = nt_error.what();                                   \
            }                                                                  \
        }();                                                                   \
        native_tests::record(SUITE, NAME, nt_ok, nt_detail,                    \
                             native_tests::now_ms() - nt_t0);                  \
    } while (false)

#define NT_CHECK(COND, MSG)                                                    \
    if (!(COND)) {                                                             \
        nt_ok = false;                                                         \
        nt_detail = MSG;                                                       \
        return;                                                                \
    }

#define NT_CHECK_NEAR(A, B, TOL, MSG)                                          \
    if (std::fabs((A) - (B)) > (TOL)) {                                        \
        nt_ok = false;                                                         \
        nt_detail = std::string(MSG) + " |a-b|=" + std::to_string(std::fabs((A) - (B))); \
        return;                                                                \
    }

inline int report(const char* path) {
    FILE* out = std::fopen(path, "w");
    if (!out) out = stdout;
    std::fprintf(out, "{\"harness\":\"native-test-suite/v1\",\"cases\":[");
    int passed = 0;
    int failed = 0;
    int blocked = 0;
    for (std::size_t i = 0; i < results().size(); ++i) {
        const auto& item = results()[i];
        const char* status = item.blocked ? "BLOCKED" : (item.passed ? "PASS" : "FAIL");
        if (item.blocked) ++blocked;
        else if (item.passed) ++passed;
        else ++failed;
        std::fprintf(out,
                     "%s{\"suite\":\"%s\",\"name\":\"%s\",\"status\":\"%s\",\"ms\":%.3f,\"detail\":\"%s\"}",
                     i ? "," : "", item.suite.c_str(), item.name.c_str(), status, item.ms, item.detail.c_str());
    }
    std::fprintf(out, "],\"passed\":%d,\"failed\":%d,\"blocked\":%d,\"total\":%d}", passed, failed, blocked,
                 static_cast<int>(results().size()));
    if (out != stdout) std::fclose(out);
    return failed == 0 ? 0 : 1;
}

}  // namespace native_tests
