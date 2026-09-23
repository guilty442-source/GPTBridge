/* xingcheng_engine_c.h — flat C ABI for the XingCheng native inference
 * engine (P11/MS6: hot path without a Python mediator).
 *
 * The symbols are exported from the same image as the pybind11 module
 * (a .pyd IS a DLL), so C# P/Invoke or the C++ ToolHost can host the
 * engine in-process without the Python service on the infer hot path.
 *
 * Ownership: caller owns only the opaque handle; text is returned via
 * caller-provided buffers (no shared allocations, no free-ABI needed).
 * Error convention: 0 = ok, 1 = failure (err buffer filled when given),
 * 2 = output buffer too small (*out_len receives the required size).
 */
#ifndef XINGCHENG_ENGINE_C_H
#define XINGCHENG_ENGINE_C_H

#include <stddef.h>
#include <stdint.h>

#if defined(_MSC_VER)
#define XC_API __declspec(dllexport)
#else
#define XC_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct xc_engine xc_engine_t;

XC_API xc_engine_t* xc_engine_create(void);
XC_API void xc_engine_destroy(xc_engine_t* engine);

/* Load a model bundle directory (manifest + weights + tokenizer).
 * Returns 0 on success; 1 on failure with err filled (UTF-8). */
XC_API int xc_engine_load(xc_engine_t* engine, const char* bundle_dir,
                          char* err, size_t err_cap);

XC_API int xc_engine_loaded(const xc_engine_t* engine);

XC_API void xc_engine_unload(xc_engine_t* engine);

/* prompt (UTF-8, NUL-terminated) -> generated text (UTF-8).
 * out/out_len: caller buffer; on return *out_len is bytes written
 * (excluding NUL). If too small: returns 2, *out_len = required size,
 * nothing written. do_sample!=0 enables sampling with temperature. */
XC_API int xc_engine_generate_text(xc_engine_t* engine,
                                   const char* prompt_utf8,
                                   int64_t max_new_tokens,
                                   double temperature,
                                   int do_sample,
                                   char* out, size_t* out_len,
                                   char* err, size_t err_cap);

/* Human/diagnostic description of the loaded engine (UTF-8). Same
 * buffer contract as generate_text. */
XC_API int xc_engine_describe(xc_engine_t* engine, char* out,
                              size_t* out_len, char* err, size_t err_cap);

#ifdef __cplusplus
}
#endif

#endif /* XINGCHENG_ENGINE_C_H */
