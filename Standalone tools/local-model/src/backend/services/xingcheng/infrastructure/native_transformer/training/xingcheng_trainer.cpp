// xingcheng_trainer.cpp — MODEL_TRAINING native engine (C++23, no external deps).
//
// Codex contract (project_architecture_directory: MODEL_TRAINING):
//   pretrain | sft | dpo | corpus | checkpoint-emission; on-demand governed
//   jobs; fail-closed job chain; weights are never silently overwritten.
// Python/PyTorch/JAX are retired (B4/C116): this is the sole training executor.
//
// Bounded execution: one governed job per process, bounded data admission
//   (data.max_rows), step/time deadlines, reject-on-unknown-field envelope.
//
//   xingcheng_trainer.exe --job <job.json> --report <report.json>
//   xingcheng_trainer.exe --smoke | --gradcheck | --maskcheck | --headcheck | --rulecheck | --depthcheck | --poscheck | --inputcheck | --gemmacheck | --mixcheck | --mtpcheck | --routecheck | --dsvcheck | --yarncheck | --csacheck | --canoncheck
//
// Masked self-attention (causal contract): position t may only read tokens
//   <= t. Full attention scores/gradients iterate s<=t (upper triangle stays
//   zero), the deltanet scan carries state forward only, the depthwise conv
//   reads x[t-j], and pretrain labels are shifted so logits[t] predicts
//   token t+1 (labels <0 are ignored). --maskcheck proves it: perturbing a
//   token leaves all earlier-position logits bitwise identical.
//
// Multi-head attention (GQA contract): every q-head reads only its own q
//   slice and its kv group's k/v — heads are isolated lanes, kv heads are
//   shared by h/group. --headcheck proves it: per-head weight
//   perturbations move exactly the expected heads' probs/attn_out.
//
// Network parts (canonical): structure = weight/activity topology,
//   activation rule = short-timescale dynamics (sigmoid/silu/softplus,
//   softmax, gates, norms), learning rule = long-timescale weight update
//   (AdamW: depends on target, activities, current weights).
//   --rulecheck probes all three executably.
//
// Multi-layer stack (depth contract): hybrid layers interleave by
//   full_attention_interval and MoE FFNs by moe_layer_interval; every
//   layer must move the residual stream, receive nonzero gradient at
//   depth, and stay learnable. --depthcheck probes an 8-layer fused
//   stack executably.
//
// Positional encoding (order contract): with no sequential recurrence the
//   net fuses position through RoPE on q/k (exact t·theta^{-2j/d} pair
//   rotations; scores depend only on t−s) and through the deltanet scan /
//   causal conv's strict forward order. --poscheck proves both channels
//   executably.
//
// Input layer (parallel gather + positional code): token ids are looked
//   up in the embed table in one shot — no sequential input — and order
//   reaches the mixers through rotary position coding (full attention)
//   and the forward recurrent state (deltanet/conv). --inputcheck proves
//   the lookup rows are bitwise, the rope score field is relative, and
//   reordered inputs change the outputs.
//
// Gemma 4 26B A4B axis (config-gated, off by default): local sliding-
//   window attention alternating with global attention
//   (global_attention_interval + sliding_window_size), unified K==V and
//   fewer kv heads on global layers (k_eq_v_global/num_global_kv_heads),
//   per-type RoPE (local_rope_proportion/global_rope_proportion +
//   local/global_base_frequency), post attention/FFW sandwich norms
//   (use_post_attn_norm/use_post_ffw_norm), GeGLU FFN
//   (ffn_activation="gelu_tanh") and final_logit_softcap. --gemmacheck
//   probes all of them plus a finite-diff pass over the new backward
//   paths.
//
// MoE mixing (dense/sparse contract): each FFN layer is either dense
//   (one shared SwiGLU) or sparse (token-choice top-K router over E
//   experts plus always-on shared experts) by moe_layer_interval.
//   --mixcheck proves topology split, top-K routing, renormalized expert
//   mixing, unrouted-expert isolation and per-token choice executably.
//   The fused router (Qwen3-A3B × Qwen3.5) keeps that shared top-k +
//   renorm contract while moe_router_sigmoid swaps the scoring function:
//   softmax (A3B denominator) or per-expert sigmoid (Qwen3.5 — scale
//   -robust under many fine-grained experts). --routecheck proves the
//   scoring modes, selection, rerouting and router grads executably.
//
// DeepSeek V4-Pro axis (config-gated, off by default): MLA multi-head
//   latent attention on non-linear layers (kv_lora_rank latent KV ->
//   per-head up-projections, q_lora_rank low-rank q, qk_nope/qk_rope
//   head dims with a shared decoupled rope key), aux-loss-free MoE
//   balancing (moe_auxfree_balance — per-expert lb_bias ranks selection
//   s+b while weights stay s; sign-rule updates, never AdamW) and a
//   depth-1 MTP module (num_nextn_predict_layers + mtp_loss_weight —
//   predicts t+2 through a conditioned decoder block sharing embed /
//   lm_head). --dsvcheck probes all three plus an XCN7 round-trip.
//
// Qwen3-Coder-480B axis (config-gated, off by default): YaRN context
//   extension on top of every rope path — yarn_factor /
//   yarn_original_max_position_embeddings enable per-channel blending
//   of raw and factor-interpolated inv-freqs between the beta_fast /
//   beta_slow band boundaries, plus the attention-factor mscale.
//   --yarncheck probes the blended table, the extension regime
//   (positions > orig_pos), causality and the XCN8 round-trip.
//
// job.json (star-native-train-job/v1):
//   task:  "pretrain" | "sft" | "dpo"
//   model: { vocab_size, hidden_size, intermediate_size, num_hidden_layers,
//            num_attention_heads, num_key_value_heads, max_position_embeddings,
//            rope_theta, rms_norm_eps, moe_num_experts, moe_top_k,
//            moe_layer_interval, moe_aux_loss_weight, moe_router_sigmoid }
//   train: { lr, weight_decay, max_steps, grad_clip, warmup_steps, lr_decay,
//            seed, beta(dpo), deadline_s, log_every, checkpoint_every,
//            init_checkpoint, emit_checkpoint,
//            threads(0=auto|1=serial|N), simd(avx2+fma dispatch) }
//   env:   XCT_TPU_THREADS / XCT_TPU_SIMD=0 override the job fields.
//   lanes: disjoint-output partitions keep results identical for any
//          thread count; SIMD keeps one fixed order per build.
//   data:  { path, format(sft|pretrain|dpo), max_rows }
//
// data rows:  sft      {"input_ids":[...],"labels":[...]}   (-100 = masked)
//             pretrain {"input_ids":[...]}                  (labels = shifted)
//             dpo      {"chosen":{"input_ids":[...],"labels":[...]},
//                       "rejected":{"input_ids":[...],"labels":[...]}}
//
// checkpoint: star-native-ckpt/v1 binary — magic, config, then name/shape/f32
//             tensors in deterministic order. emit_checkpoint is written to a
//             temp path and atomically renamed (weights are never silently
//             overwritten: existing target is refused unless overwrite=true).

#include <algorithm>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <functional>
#include <mutex>
#include <numeric>
#include <random>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#if defined(_M_X64) || defined(__x86_64__)
#include <immintrin.h>
#include <intrin.h>
#include <io.h>
#include <sstream>
#endif

#include "jsonlite.h"
#include "xcb_batch.h"

using gptbridge::jsonlite::JsonParser;
using gptbridge::jsonlite::JsonValue;

namespace xct {

#include "xct_util.h"
#include "xct_tpu.h"
#include "xct_kernels.h"
#include "xct_math.h"
#include "xct_gemma4.h"
#include "xct_backward.h"
#include "xct_mtp.h"
#include "xct_ckpt.h"
#include "xct_job.h"
#include "xct_depth.h"
#include "xct_pos.h"
#include "xct_mix.h"
#include "xct_route.h"
#include "xct_yarn.h"
#include "xct_csa.h"
#include "xct_canon.h"

} // namespace xct

int main(int argc, char** argv) {
    std::string job_path, report_path;
    bool do_smoke = false, do_kernel_registry = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--job" && i + 1 < argc) job_path = argv[++i];
        else if (a == "--report" && i + 1 < argc) report_path = argv[++i];
        else if (a == "--smoke") do_smoke = true;
        else if (a == "--gradcheck") return xct::gradcheck();
        else if (a == "--maskcheck") return xct::maskcheck();
        else if (a == "--headcheck") return xct::headcheck();
        else if (a == "--rulecheck") return xct::rulecheck();
        else if (a == "--depthcheck") return xct::depthcheck();
        else if (a == "--poscheck") return xct::poscheck();
        else if (a == "--inputcheck") return xct::inputcheck();
        else if (a == "--gemmacheck") return xct::gemmacheck();
        else if (a == "--mixcheck") return xct::mixcheck();
        else if (a == "--routecheck") return xct::routecheck();
        else if (a == "--dsvcheck") return xct::dsvcheck();
        else if (a == "--yarncheck") return xct::yarncheck();
        else if (a == "--csacheck") return xct::csacheck();
        else if (a == "--mtpcheck") return xct::mtpcheck();
        else if (a == "--canoncheck") return xct::canoncheck();
        else if (a == "--freezecheck") return xct::freezecheck();
        else if (a == "--gemmbench") return xct::gemmbench();
        else if (a == "--kernel-registry") do_kernel_registry = true;
        else if (a == "--kernel-policy" && i + 1 < argc)
            xct::g_kernel_policy_arg = argv[++i];
        else if (a == "--canonical-materialize")
            return xct::canonical_materialize();
        else if (a == "--probe-all") return xct::probe_all();
    }
    if (do_kernel_registry) return xct::kernel_registry_emit();
    if (do_smoke) return xct::smoke();
    if (job_path.empty()) {
        std::fprintf(stderr, "usage: xingcheng_trainer --job <job.json> [--report <out.json>] | --smoke | --gradcheck | --maskcheck | --headcheck | --rulecheck | --depthcheck | --poscheck | --inputcheck | --mixcheck | --mtpcheck | --routecheck | --gemmacheck | --dsvcheck | --yarncheck | --csacheck | --canoncheck\n");
        return 2;
    }
    try {
        JsonValue job = JsonParser(xct::slurp(job_path)).parse();
        JsonValue rep = xct::run_job(job);
        std::string out = gptbridge::jsonlite::json_serialize(rep);
        if (!report_path.empty()) {
            std::ofstream f(report_path, std::ios::trunc);
            f << out << "\n";
        } else {
            std::puts(out.c_str());
        }
        return 0;
    } catch (const char* e) {
        std::fprintf(stderr, "train-job error: %s\n", e);
        return 1;
    } catch (const gptbridge::jsonlite::JsonError&) {
        std::fprintf(stderr, "train-job error: JSON_PARSE\n");
        return 1;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "train-job error: %s\n", e.what());
        return 1;
    } catch (...) {
        std::fprintf(stderr, "train-job error: malformed job or unrecoverable state\n");
        return 1;
    }
}
