// Device-resident KV cache + online-softmax attention kernel for the
// Xingcheng C++ inference engine (P1-1②). The host paged KV pool stays the
// source of truth — every committed write is mirrored here (write-through),
// so the device copy carries the same fp64 values and attention over the
// cache never round-trips the host during decode.
//
// Device layout: k[layer][kv_head][position][head_dim] (fp64), allocated as
// one contiguous buffer per K/V at engine load. Only the single-sequence
// slot (slot 0) is mirrored; batched spans keep the host path.
//
// The kernel mirrors the host online-softmax loop in engine.cpp exactly:
// causal bound = position_offset + s, tile-scanned K/V with a running
// max/sum/accumulator — the seq x total_len scores matrix is never
// materialized. fp64 throughout, so parity with the host path is tight.
//
// Governance: engine gates this behind XINGCHENG_CPP_CUDA_KV=1 set by the
// governed Python layer; requesting it without kernels/device or with the
// int8 KV format fails closed at load (CUDA_KV_UNAVAILABLE /
// CUDA_KV_UNSUPPORTED_CONFIG) — never a silent host fallback.

#include <cuda_runtime.h>

#include <cstdint>
#include <limits>

namespace {

constexpr double kNegInf = -std::numeric_limits<double>::infinity();

constexpr int kTile = 128;          // K/V positions per online-softmax tile
constexpr int kThreads = 128;       // one block per (head, query row)
constexpr int64_t kMaxHeadDim = 256;

double* g_k = nullptr;
double* g_v = nullptr;
double* g_qbuf = nullptr;   // q scratch, grown on demand
double* g_obuf = nullptr;   // attention-out scratch, grown on demand
int64_t g_layers = 0;
int64_t g_kv_heads = 0;
int64_t g_head_dim = 0;
int64_t g_max_len = 0;
size_t g_qcap = 0;
size_t g_ocap = 0;

// One block per (head, query row). Shared layout:
//   scores[kTile] | acc[head_dim] | red[32] | scal[5]
// scal: [0]=running max m, [1]=running sum l, [2]=tile rescale r,
//       [3]=tile merged max m_new, [4]=final inv_l.
__global__ void kv_attention_kernel(
    const double* __restrict__ q,       // [heads][seq][head_dim]
    const double* __restrict__ kbuf,    // [kv_heads][max_len][head_dim]
    const double* __restrict__ vbuf,
    int64_t heads, int64_t seq, int64_t kv_heads, int64_t head_dim,
    int64_t max_len, int64_t position_offset,
    double* __restrict__ out,           // [seq][out_stride] strided
    int64_t out_stride) {
    const int64_t s = blockIdx.x % seq;
    const int64_t h = blockIdx.x / seq;
    const int64_t kv_head = h / (heads / kv_heads);
    const int64_t last = position_offset + s;  // inclusive causal bound
    const double* q_row = q + (h * seq + s) * head_dim;
    const double* kbase = kbuf + kv_head * max_len * head_dim;
    const double* vbase = vbuf + kv_head * max_len * head_dim;
    const double scale = 1.0 / sqrt(static_cast<double>(head_dim));

    extern __shared__ double sm[];
    double* scores = sm;
    double* acc = sm + kTile;
    double* red = acc + head_dim;
    double* scal = red + 32;

    const int tid = threadIdx.x;
    const int warps = (blockDim.x + 31) / 32;
    if (tid == 0) {
        scal[0] = kNegInf;
        scal[1] = 0.0;
    }
    for (int64_t d = tid; d < head_dim; d += blockDim.x) acc[d] = 0.0;
    __syncthreads();

    for (int64_t t0 = 0; t0 <= last; t0 += kTile) {
        const int64_t tn = min(static_cast<int64_t>(kTile), last - t0 + 1);

        // Tile scores: score_j = dot(q_row, k_j) * scale.
        for (int64_t j = tid; j < tn; j += blockDim.x) {
            const double* krow = kbase + (t0 + j) * head_dim;
            double dot = 0.0;
            for (int64_t d = 0; d < head_dim; ++d) dot += q_row[d] * krow[d];
            scores[j] = dot * scale;
        }
        __syncthreads();

        // Tile max → block reduce.
        double tmax = kNegInf;
        for (int64_t j = tid; j < tn; j += blockDim.x) {
            tmax = fmax(tmax, scores[j]);
        }
        for (int off = 16; off > 0; off >>= 1) {
            tmax = fmax(tmax, __shfl_down_sync(0xffffffffu, tmax, off));
        }
        if ((tid & 31) == 0) red[tid >> 5] = tmax;
        __syncthreads();
        if (tid == 0) {
            double m_tile = red[0];
            for (int w = 1; w < warps; ++w) m_tile = fmax(m_tile, red[w]);
            const double m_new = fmax(scal[0], m_tile);
            scal[2] = exp(scal[0] - m_new);  // exp(-inf)=0 on first tile
            scal[3] = m_new;
        }
        __syncthreads();
        const double r = scal[2];
        const double m_new = scal[3];

        // Weights w_j = exp(score_j - m_new), stored back over scores;
        // then the tile sum reduces into the running denominator.
        for (int64_t j = tid; j < tn; j += blockDim.x) {
            scores[j] = exp(scores[j] - m_new);
        }
        __syncthreads();
        double tsum = 0.0;
        for (int64_t j = tid; j < tn; j += blockDim.x) tsum += scores[j];
        for (int off = 16; off > 0; off >>= 1) {
            tsum += __shfl_down_sync(0xffffffffu, tsum, off);
        }
        if ((tid & 31) == 0) red[tid >> 5] = tsum;
        __syncthreads();
        if (tid == 0) {
            double l_tile = red[0];
            for (int w = 1; w < warps; ++w) l_tile += red[w];
            scal[1] = scal[1] * r + l_tile;
            scal[0] = m_new;
        }

        // Accumulator: acc_d = acc_d * r + Σ_j w_j * V[t0+j][d].
        for (int64_t d = tid; d < head_dim; d += blockDim.x) {
            double a = acc[d] * r;
            for (int64_t j = 0; j < tn; ++j) {
                a += scores[j] * vbase[(t0 + j) * head_dim + d];
            }
            acc[d] = a;
        }
        __syncthreads();
    }

    if (tid == 0) scal[4] = 1.0 / scal[1];
    __syncthreads();
    const double inv_l = scal[4];
    double* orow = out + s * out_stride + h * head_dim;
    for (int64_t d = tid; d < head_dim; d += blockDim.x) {
        orow[d] = acc[d] * inv_l;
    }
}

bool grow_scratch(double** buf, size_t* cap, size_t need) {
    if (*cap >= need) return true;
    if (*buf != nullptr) cudaFree(*buf);
    *buf = nullptr;
    *cap = 0;
    if (cudaMalloc(buf, need * sizeof(double)) != cudaSuccess) return false;
    *cap = need;
    return true;
}

}  // namespace

extern "C" {

void xcuda_kv_free();

int xcuda_kv_kernel_probe() {
    int count = 0;
    if (cudaGetDeviceCount(&count) != cudaSuccess || count <= 0) return 0;
    return 1;
}

int xcuda_kv_alloc(
    int64_t layers, int64_t kv_heads, int64_t head_dim, int64_t max_len) {
    if (layers <= 0 || kv_heads <= 0 || head_dim <= 0 || max_len <= 0 ||
        head_dim > kMaxHeadDim) {
        return 1;
    }
    xcuda_kv_free();
    const size_t elems = static_cast<size_t>(layers) *
        static_cast<size_t>(kv_heads) * static_cast<size_t>(max_len) *
        static_cast<size_t>(head_dim);
    if (cudaMalloc(&g_k, elems * sizeof(double)) != cudaSuccess) return 2;
    if (cudaMalloc(&g_v, elems * sizeof(double)) != cudaSuccess) {
        cudaFree(g_k);
        g_k = nullptr;
        return 2;
    }
    g_layers = layers;
    g_kv_heads = kv_heads;
    g_head_dim = head_dim;
    g_max_len = max_len;
    return 0;
}

void xcuda_kv_free() {
    if (g_k != nullptr) cudaFree(g_k);
    if (g_v != nullptr) cudaFree(g_v);
    if (g_qbuf != nullptr) cudaFree(g_qbuf);
    if (g_obuf != nullptr) cudaFree(g_obuf);
    g_k = g_v = g_qbuf = g_obuf = nullptr;
    g_qcap = g_ocap = 0;
    g_layers = g_kv_heads = g_head_dim = g_max_len = 0;
}

// Write `rows` contiguous head_dim rows for one (layer, kv_head) starting at
// position pos0. src is a host pointer of rows*head_dim doubles.
int xcuda_kv_write_rows(
    int is_k, int64_t layer, int64_t head, int64_t pos0, int64_t rows,
    const double* src) {
    if (g_k == nullptr || src == nullptr || layer < 0 || layer >= g_layers ||
        head < 0 || head >= g_kv_heads || pos0 < 0 || rows <= 0 ||
        pos0 + rows > g_max_len) {
        return 1;
    }
    double* base = (is_k ? g_k : g_v) +
        ((layer * g_kv_heads + head) * g_max_len + pos0) * g_head_dim;
    if (cudaMemcpy(
            base, src,
            static_cast<size_t>(rows) * static_cast<size_t>(g_head_dim) *
                sizeof(double),
            cudaMemcpyHostToDevice) != cudaSuccess) {
        return 2;
    }
    return 0;
}

// Online-softmax attention over the device cache for one layer.
// q_host: [heads][seq][head_dim] (RoPE already applied).
// out_host: strided rows — element (s, h, d) → out[s*out_stride + h*head_dim + d].
// Causal bound per row: position_offset + s (inclusive).
int xcuda_kv_attention(
    int64_t layer, const double* q_host, int64_t heads, int64_t seq,
    int64_t kv_heads, int64_t head_dim, int64_t position_offset,
    double* out_host, int64_t out_stride) {
    if (g_k == nullptr || q_host == nullptr || out_host == nullptr ||
        layer < 0 || layer >= g_layers || heads <= 0 || seq <= 0 ||
        kv_heads <= 0 || heads % kv_heads != 0 || head_dim <= 0 ||
        head_dim != g_head_dim || head_dim > kMaxHeadDim ||
        kv_heads != g_kv_heads || position_offset < 0 ||
        position_offset + seq > g_max_len ||
        out_stride < heads * head_dim) {
        return 1;
    }
    const size_t q_elems = static_cast<size_t>(heads) *
        static_cast<size_t>(seq) * static_cast<size_t>(head_dim);
    const size_t o_elems = static_cast<size_t>(seq) *
        static_cast<size_t>(out_stride);
    if (!grow_scratch(&g_qbuf, &g_qcap, q_elems) ||
        !grow_scratch(&g_obuf, &g_ocap, o_elems)) {
        return 2;
    }
    if (cudaMemcpy(
            g_qbuf, q_host, q_elems * sizeof(double),
            cudaMemcpyHostToDevice) != cudaSuccess) {
        return 2;
    }
    const size_t smem = (static_cast<size_t>(kTile) +
                         static_cast<size_t>(head_dim) + 32 + 5) *
                        sizeof(double);
    const int blocks = static_cast<int>(heads * seq);
    kv_attention_kernel<<<blocks, kThreads, smem>>>(
        g_qbuf,
        g_k + layer * g_kv_heads * g_max_len * g_head_dim,
        g_v + layer * g_kv_heads * g_max_len * g_head_dim,
        heads, seq, kv_heads, head_dim, g_max_len, position_offset,
        g_obuf, out_stride);
    if (cudaGetLastError() != cudaSuccess) return 3;
    if (cudaMemcpy(
            out_host, g_obuf, o_elems * sizeof(double),
            cudaMemcpyDeviceToHost) != cudaSuccess) {
        return 3;
    }
    return 0;
}

}  // extern "C"
