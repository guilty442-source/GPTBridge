/*
 * gptbridge_native.c — sole C ABI thunk (A221/E186).
 *
 * Validates arguments, translates status/handles, delegates immediately.
 * Contains no algorithm/business/workflow/state authority (A220/E185).
 * Resource monitoring is a platform API call, not compute — it lives in
 * the bridge, not in native/core/.
 */
#include "gptbridge_native.h"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <psapi.h>
#else
#include <time.h>
#endif

int gptbridge_native_is_windows(void) {
#ifdef _WIN32
    return 1;
#else
    return 0;
#endif
}

double gptbridge_native_monotonic_seconds(void) {
#ifdef _WIN32
    static LARGE_INTEGER frequency = {0};
    if (frequency.QuadPart == 0) {
        QueryPerformanceFrequency(&frequency);
    }
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    if (frequency.QuadPart == 0) {
        return 0.0;
    }
    return (double)counter.QuadPart / (double)frequency.QuadPart;
#else
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0.0;
    }
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
#endif
}

int64_t gptbridge_native_working_set_bytes(void) {
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS counters;
    ZeroMemory(&counters, sizeof(counters));
    if (GetProcessMemoryInfo(
            GetCurrentProcess(), &counters, sizeof(counters)) == 0) {
        return -1;
    }
    return (int64_t)counters.WorkingSetSize;
#else
    return -1;
#endif
}

int64_t gptbridge_native_private_bytes(void) {
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS_EX counters;
    ZeroMemory(&counters, sizeof(counters));
    counters.cb = sizeof(counters);
    if (GetProcessMemoryInfo(
            GetCurrentProcess(),
            (PPROCESS_MEMORY_COUNTERS)&counters,
            sizeof(counters)) == 0) {
        return -1;
    }
    return (int64_t)counters.PrivateUsage;
#else
    return -1;
#endif
}

int gptbridge_native_release_working_set(void) {
#ifdef _WIN32
    return EmptyWorkingSet(GetCurrentProcess()) != 0 ? 1 : 0;
#else
    return 0;
#endif
}
