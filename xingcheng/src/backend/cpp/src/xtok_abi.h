// xtok_abi.h — run-time binding to the Rust tokenizer (xtok/v1 C ABI in
// xcorpus.dll). B81: tokenizer is Rust-owned; the C++ engine reaches it
// only through this stable C boundary — resolved via LoadLibrary, never
// import-linked, exactly like the CUDA lane. Fail-closed: when the dll
// or any symbol is missing, available() is false and every call rc<0.
//
// ABI contract (mirrors xcorpus/src/abi.rs):
//   probe form: out==NULL or cap==0 -> returns required count, no write
//   rc >= 0   element/byte count
//   rc == -1  invalid argument / null handle
//   rc == -2  capacity insufficient (use probe form)
//   rc == -3  internal failure (panic caught at the boundary)
#pragma once

#include <cstddef>
#include <cstdint>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace xtok {

struct Api {
    HMODULE dll = nullptr;
    uint32_t (*abi_version)() = nullptr;
    void* (*load)(const uint8_t*, size_t) = nullptr;
    void (*free)(void*) = nullptr;
    int64_t (*vocab_size)(const void*) = nullptr;
    int64_t (*encode)(const void*, const uint8_t*, size_t, int32_t,
                      int32_t, int64_t, int64_t*, size_t) = nullptr;
    int64_t (*decode)(const void*, const int64_t*, size_t, int32_t,
                      uint8_t*, size_t) = nullptr;
};

inline const Api& api() {
    static Api a = [] {
        Api r;
        // exe-directory/PATH search — the governed build places
        // xcorpus.dll next to the consuming exe.
        r.dll = LoadLibraryA("xcorpus.dll");
        if (r.dll == nullptr) return r;
        auto sym = [](HMODULE d, const char* n) {
            return reinterpret_cast<void*>(GetProcAddress(d, n));
        };
        // All symbols required; one missing export = whole ABI absent.
        bool ok = true;
        ok &= (r.abi_version = reinterpret_cast<uint32_t (*)()>(
                   sym(r.dll, "xtok_abi_version"))) != nullptr;
        ok &= (r.load = reinterpret_cast<void* (*)(const uint8_t*, size_t)>(
                   sym(r.dll, "xtok_load"))) != nullptr;
        ok &= (r.free = reinterpret_cast<void (*)(void*)>(
                   sym(r.dll, "xtok_free"))) != nullptr;
        ok &= (r.vocab_size = reinterpret_cast<int64_t (*)(const void*)>(
                   sym(r.dll, "xtok_vocab_size"))) != nullptr;
        ok &= (r.encode = reinterpret_cast<int64_t (*)(
                   const void*, const uint8_t*, size_t, int32_t, int32_t,
                   int64_t, int64_t*, size_t)>(
                   sym(r.dll, "xtok_encode"))) != nullptr;
        ok &= (r.decode = reinterpret_cast<int64_t (*)(
                   const void*, const int64_t*, size_t, int32_t, uint8_t*,
                   size_t)>(sym(r.dll, "xtok_decode"))) != nullptr;
        if (!ok || (r.abi_version != nullptr && r.abi_version() != 1)) {
            for (void** p :
                 {reinterpret_cast<void**>(&r.abi_version),
                  reinterpret_cast<void**>(&r.load),
                  reinterpret_cast<void**>(&r.free),
                  reinterpret_cast<void**>(&r.vocab_size),
                  reinterpret_cast<void**>(&r.encode),
                  reinterpret_cast<void**>(&r.decode)}) {
                *p = nullptr;
            }
            FreeLibrary(r.dll);
            r.dll = nullptr;
        }
        return r;
    }();
    return a;
}

inline bool available() { return api().dll != nullptr; }

} // namespace xtok
