/*
 * gptbridge_native.h — sole public C header and application-facing native
 * contract (A221/E186).
 *
 * Declares the canonical platform and compute ABI. Platform functions are
 * implemented by native/bridge/gptbridge_native.c; compute functions are
 * implemented by the pure-C cores in native/core/. Python bindings consume
 * this header only and never include private core headers (A220/E185).
 */
#ifndef GPTBRIDGE_NATIVE_H
#define GPTBRIDGE_NATIVE_H

#include <stdint.h>
#include <wchar.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Whether the current platform is Windows. */
int gptbridge_native_is_windows(void);

/* High-resolution monotonic clock in fractional seconds. */
double gptbridge_native_monotonic_seconds(void);

/* Process working-set size in bytes, or -1 on error / unsupported. */
int64_t gptbridge_native_working_set_bytes(void);

/* Process private (non-shared) memory usage in bytes, or -1 on error. */
int64_t gptbridge_native_private_bytes(void);

/* Empty the process working set; returns 1 on success, 0 otherwise. */
int gptbridge_native_release_working_set(void);

/* P24 psutil-convergence primitives — process/system queries for the
   resident Python surfaces that today call psutil.  All fail closed:
   -1/0 on error or unsupported platform. */

/* Total physical RAM in bytes, or -1. */
int64_t gptbridge_native_system_memory_total_bytes(void);

/* Available physical RAM in bytes, or -1. */
int64_t gptbridge_native_system_memory_available_bytes(void);

/* Logical CPU count, or 0. */
int gptbridge_native_cpu_count(void);

/* 1 when pid exists and is not a zombie/exited stub, else 0. */
int gptbridge_native_process_alive(int64_t pid);

/* Process image base name into buf; returns bytes written (no NUL) or -1. */
int64_t gptbridge_native_process_name(int64_t pid, char* buf, int64_t buf_len);

/* Working-set / private bytes of an arbitrary pid, or -1. */
int64_t gptbridge_native_process_working_set_bytes_for(int64_t pid);
int64_t gptbridge_native_process_private_bytes_for(int64_t pid);

/* Kernel+user times of pid in 100ns units; returns 1 on success, 0 else.
   Callers compute cpu_percent from two samples — same contract as
   psutil.Process.cpu_percent(interval=None). */
int gptbridge_native_process_cpu_times_100ns(
    int64_t pid, int64_t* kernel_100ns, int64_t* user_100ns);

/* Enumerate live pids into caller buffer; returns count or -1. */
int gptbridge_native_process_list(int64_t* pids_out, int64_t max_count);

/* Enumerate all descendants of root_pid (recursive, BFS over a single
   snapshot); returns count written or -1. */
int gptbridge_native_process_children(
    int64_t root_pid, int64_t* pids_out, int64_t max_count);

/* Full image path of pid into buf (UTF-8); bytes written or -1. */
int64_t gptbridge_native_process_exe(int64_t pid, char* buf, int64_t buf_len);

/* Command line of pid into buf (UTF-8); bytes written or -1.  Reads the
   target PEB on Windows; fails closed on any access error. */
int64_t gptbridge_native_process_cmdline(
    int64_t pid, char* buf, int64_t buf_len);

/* Thread count of pid, or -1. */
int gptbridge_native_process_num_threads(int64_t pid);

/* Open handle count of pid (Windows), or -1. */
int gptbridge_native_process_num_handles(int64_t pid);

/* Parent pid of pid, or -1. */
int64_t gptbridge_native_process_parent(int64_t pid);

/* Process creation time as Unix-epoch milliseconds (FILETIME → epoch),
   or -1.  Identity anchor for pid-reuse detection. */
int64_t gptbridge_native_process_create_time_ms(int64_t pid);

/* I/O counters of pid: read_bytes/write_bytes out; 1 on success, 0 else. */
int gptbridge_native_process_io_counters(
    int64_t pid, int64_t* read_bytes, int64_t* write_bytes);

/* DOMAIN\user of pid's token into buf (UTF-8); bytes written or -1. */
int64_t gptbridge_native_process_username(
    int64_t pid, char* buf, int64_t buf_len);

/* Set priority class (nice): NORMAL=0x20, IDLE=0x40, BELOW_NORMAL=0x4000,
   ABOVE_NORMAL=0x8000, HIGH=0x80, REALTIME=0x100.  1 on success, 0 else. */
int gptbridge_native_process_set_priority(int64_t pid, int32_t win_class);

/* Get priority class value, or -1. */
int64_t gptbridge_native_process_get_priority(int64_t pid);

/* Set CPU affinity mask (bit i = logical core i); 1 on success, 0 else. */
int gptbridge_native_process_set_affinity(int64_t pid, uint64_t mask);

/* Get CPU affinity mask, or -1. */
int64_t gptbridge_native_process_get_affinity(int64_t pid);

/* Wait for pid exit up to timeout_ms; returns 1 exited, 0 timeout/error. */
int gptbridge_native_process_wait(int64_t pid, int64_t timeout_ms);

/* Pid of the process listening on a TCP port, or -1. */
int64_t gptbridge_native_tcp_listen_pid(int64_t port);

/* System-wide CPU times in 100ns units (idle/kernel/user split out);
   returns 1 on success, 0 otherwise.  kernel includes idle — same
   contract as GetSystemTimes. */
int gptbridge_native_system_cpu_times_100ns(
    int64_t* idle_100ns, int64_t* kernel_100ns, int64_t* user_100ns);

/* Terminate pid (SIGKILL equivalent); 1 on success, 0 otherwise. */
int gptbridge_native_process_terminate(int64_t pid);

/* Count tokens (words + punctuation) in UTF-8 text. */
int64_t gptbridge_native_parser_token_estimate(
    const char* text, int64_t text_len);

/* Count tokens in a batch of UTF-8 texts; caller provides results. */
int gptbridge_native_parser_batch_token_estimate(
    const char* const* texts_ptr,
    const int64_t* text_lens,
    int64_t count,
    int64_t* results_ptr);

/* Dot product of two float64 vectors. */
double gptbridge_native_vector_dot(
    const double* a, const double* b, int64_t dim);

/* L2 norm of a float64 vector. */
double gptbridge_native_vector_l2_norm(const double* a, int64_t dim);

/* Cosine similarity of two float64 vectors. */
double gptbridge_native_vector_cosine_similarity(
    const double* a, const double* b, int64_t dim);

/* Batch dot product over `count` vector pairs; caller provides results. */
int gptbridge_native_vector_batch_dot(
    const double* const* a_ptrs,
    const double* const* b_ptrs,
    int64_t count,
    int64_t dim,
    double* results);

/* Matrix multiply: C[M x N] = A[M x K] * B[K x N], row-major. */
int gptbridge_native_transformer_matmul(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t k_in, int64_t n,
    double* c);

/* Grouped matrix multiply: for each group g, C_g = A_g[M_g x K] * B_g[K x N];
 * a is the concatenated row-blocks, group_rows[g] its row count, b_list[g]
 * its weight, c the concatenated outputs in group order. */
int gptbridge_native_transformer_matmul_grouped(
    const double* a, const int64_t* group_rows, int64_t groups,
    const double* const* b_list, int64_t k, int64_t n,
    double* c);

/* Softmax over each row of a [rows x cols] matrix. */
int gptbridge_native_transformer_softmax(
    const double* input, int64_t rows, int64_t cols,
    double* output);

/* RMSNorm over each row: x / sqrt(mean(x^2) + eps) * weight. */
int gptbridge_native_transformer_rmsnorm(
    const double* input, int64_t rows, int64_t cols,
    const double* weight, double eps,
    double* output);

/* RoPE over [batch x heads x seq x head_dim]; cos/sin are [batch x seq x dim]. */
int gptbridge_native_transformer_rope(
    const double* input,
    int64_t batch, int64_t heads, int64_t seq_len, int64_t head_dim,
    const double* cos_table, const double* sin_table,
    double* output);

/* Scaled dot-product attention; caller provides output and scores workspace. */
int gptbridge_native_transformer_scaled_dot_product_attention(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    double* output,
    double* scores_temp);

/* Online (blocked) scaled dot-product attention — same result as
 * gptbridge_native_transformer_scaled_dot_product_attention but the
 * scores workspace is bounded: block_scores must hold at least
 * min(block_k, k_rows) doubles (one K-block), independent of k_rows. */
int gptbridge_native_transformer_attention_online(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    int64_t block_k,
    double* output,
    double* block_scores);

/* Directory change notification (P14 event-driven invalidation).
 * Mechanical watcher only: reports "something under path changed";
 * changed paths are deliberately NOT reported — callers recapture
 * through git. One OS handle per open watch; caller closes.
 * open: opaque handle or NULL (unsupported platform / bad path).
 * wait: 1 = changed (handle re-armed), 0 = timeout, -1 = error.
 * close: releases the handle; safe on NULL. */
void* gptbridge_native_dirwatch_open(const wchar_t* path);
int gptbridge_native_dirwatch_wait(void* handle, int64_t timeout_ms);
void gptbridge_native_dirwatch_close(void* handle);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_NATIVE_H */
