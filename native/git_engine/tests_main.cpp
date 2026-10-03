// git-engine-native M0 self-contained tests (own main; no suite edits).
// Fail-closed assertions: any unexpected probe outcome is a failure.

#include "git_engine.h"

#include <stdio.h>

static int g_failures = 0;

#define GE_CHECK(cond, label)                                                     \
    do {                                                                          \
        if (!(cond)) {                                                            \
            ++g_failures;                                                         \
            printf("FAIL %s (line %d)\n", label, __LINE__);                        \
        } else {                                                                  \
            printf("PASS %s\n", label);                                           \
        }                                                                         \
    } while (0)

int main() {
    GE_CHECK(ge_version() == GE_VERSION_M0, "version-is-m0");

    ge_probe_t probe{};
    // The repo root is a live worktree; the probe must succeed with sane counts.
    const ge_status_t st = ge_probe_worktree(L"E:\\GPTBridge", &probe, 30000);
    GE_CHECK(st == GE_OK, "probe-repo-root-ok");
    GE_CHECK(probe.dirty >= 0 && probe.staged >= 0 && probe.untracked >= 0, "probe-counts-sane");
    GE_CHECK(probe.staged <= probe.dirty, "probe-staged-subset-of-dirty");
    printf("INFO root dirty=%lld staged=%lld untracked=%lld clean=%d truncated=%d\n",
           (long long)probe.dirty, (long long)probe.staged, (long long)probe.untracked,
           probe.clean, probe.truncated);

    // Fail-closed on non-directories.
    ge_probe_t bad{};
    GE_CHECK(ge_probe_worktree(L"E:\\GPTBridge\\does-not-exist-xyz", &bad, 5000) == GE_BAD_PATH,
             "probe-missing-path-fail-closed");
    GE_CHECK(ge_probe_worktree(nullptr, &bad, 5000) == GE_BAD_PATH, "probe-null-dir-fail-closed");
    GE_CHECK(ge_probe_worktree(L"E:\\GPTBridge", nullptr, 5000) == GE_FAILED,
             "probe-null-out-fail-closed");

    if (g_failures == 0) {
        printf("ALL-TESTS-PASS\n");
        return 0;
    }
    printf("FAILURES=%d\n", g_failures);
    return 1;
}
