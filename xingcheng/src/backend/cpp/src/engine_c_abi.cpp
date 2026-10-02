/* engine_c_abi.cpp — flat C ABI surface over NativeInferenceEngine.
 *
 * Thin translation layer only: exceptions become error codes, strings
 * become caller-owned buffers. No governance logic lives here — the
 * hosting process (C# orchestration / ToolHost) stays inside the same
 * governed envelope; this ABI merely removes the Python service hop on
 * the infer hot path. */
#include "xingcheng_engine_c.h"
#include "xingcheng_inference.hpp"

#include <cstring>
#include <exception>
#include <string>
#include <vector>

using xingcheng::inference::NativeInferenceEngine;
using xingcheng::inference::SamplingConfig;

struct xc_engine {
    NativeInferenceEngine impl;
};

namespace {

void copy_err(char* err, size_t cap, const std::string& what) {
    if (!err || cap == 0) return;
    const size_t n = what.size() < cap - 1 ? what.size() : cap - 1;
    std::memcpy(err, what.data(), n);
    err[n] = '\0';
}

/* Copy `text` into caller buffer; returns 0 or 2 (sets *out_len). */
int copy_out(const std::string& text, char* out, size_t* out_len) {
    const size_t need = text.size();
    if (!out || !out_len) {
        if (out_len) *out_len = need;
        return 2;
    }
    const size_t cap = *out_len;
    if (cap < need + 1) {
        *out_len = need;
        return 2;
    }
    std::memcpy(out, text.data(), need);
    out[need] = '\0';
    *out_len = need;
    return 0;
}

} // namespace

xc_engine_t* xc_engine_create(void) {
    return new (std::nothrow) xc_engine();
}

void xc_engine_destroy(xc_engine_t* engine) { delete engine; }

int xc_engine_load(xc_engine_t* engine, const char* bundle_dir,
                   char* err, size_t err_cap) {
    if (!engine || !bundle_dir) {
        copy_err(err, err_cap, "null argument");
        return 1;
    }
    try {
        engine->impl.load(bundle_dir);
        return 0;
    } catch (const std::exception& e) {
        copy_err(err, err_cap, e.what());
        return 1;
    } catch (...) {
        copy_err(err, err_cap, "unknown load failure");
        return 1;
    }
}

int xc_engine_loaded(const xc_engine_t* engine) {
    return (engine && engine->impl.loaded()) ? 1 : 0;
}

void xc_engine_unload(xc_engine_t* engine) {
    if (engine) engine->impl.unload();
}

int xc_engine_generate_text(xc_engine_t* engine, const char* prompt_utf8,
                            int64_t max_new_tokens, double temperature,
                            int do_sample, char* out, size_t* out_len,
                            char* err, size_t err_cap) {
    if (!engine || !engine->impl.loaded() || !prompt_utf8) {
        copy_err(err, err_cap, "engine not loaded or null prompt");
        return 1;
    }
    try {
        SamplingConfig sampling;
        sampling.do_sample = (do_sample != 0);
        sampling.temperature = temperature;
        const std::string text = engine->impl.generate_text(
            prompt_utf8, max_new_tokens, sampling);
        return copy_out(text, out, out_len);
    } catch (const std::exception& e) {
        copy_err(err, err_cap, e.what());
        return 1;
    } catch (...) {
        copy_err(err, err_cap, "unknown generate failure");
        return 1;
    }
}

int xc_engine_generate_ex(xc_engine_t* engine, const char* prompt_utf8,
                          int64_t max_new_tokens, double temperature,
                          int do_sample, char* out, size_t* out_len,
                          int64_t* ids, size_t* ids_len,
                          char* err, size_t err_cap) {
    if (!engine || !engine->impl.loaded() || !prompt_utf8) {
        copy_err(err, err_cap, "engine not loaded or null prompt");
        return 1;
    }
    try {
        SamplingConfig sampling;
        sampling.do_sample = (do_sample != 0);
        sampling.temperature = temperature;
        const std::vector<int64_t> gen = engine->impl.generate(
            engine->impl.encode(prompt_utf8), max_new_tokens, sampling);
        const std::string text = engine->impl.decode(gen);
        int rc = copy_out(text, out, out_len);
        if (ids_len != nullptr) {
            const size_t need = gen.size();
            const size_t cap = ids != nullptr ? *ids_len : 0;
            if (cap < need) {
                *ids_len = need;
                rc = 2;
            } else {
                if (need > 0)
                    std::memcpy(ids, gen.data(), need * sizeof(int64_t));
                *ids_len = need;
            }
        }
        return rc;
    } catch (const std::exception& e) {
        copy_err(err, err_cap, e.what());
        return 1;
    } catch (...) {
        copy_err(err, err_cap, "unknown generate failure");
        return 1;
    }
}

int xc_engine_describe(xc_engine_t* engine, char* out, size_t* out_len,
                       char* err, size_t err_cap) {
    if (!engine) {
        copy_err(err, err_cap, "null engine");
        return 1;
    }
    try {
        return copy_out(engine->impl.describe(), out, out_len);
    } catch (const std::exception& e) {
        copy_err(err, err_cap, e.what());
        return 1;
    }
}
