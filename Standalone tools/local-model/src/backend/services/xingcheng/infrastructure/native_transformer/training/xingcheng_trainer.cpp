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
//   xingcheng_trainer.exe --smoke | --gradcheck | --maskcheck | --headcheck | --rulecheck | --depthcheck | --inputcheck
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
// Input layer (parallel gather + positional code): token ids are looked
//   up in the embed table in one shot — no sequential input — and order
//   reaches the mixers through rotary position coding (full attention)
//   and the forward recurrent state (deltanet/conv). --inputcheck proves
//   the lookup rows are bitwise, the rope score field is relative, and
//   reordered inputs change the outputs.
//
// job.json (star-native-train-job/v1):
//   task:  "pretrain" | "sft" | "dpo"
//   model: { vocab_size, hidden_size, intermediate_size, num_hidden_layers,
//            num_attention_heads, num_key_value_heads, max_position_embeddings,
//            rope_theta, rms_norm_eps, moe_num_experts, moe_top_k,
//            moe_layer_interval, moe_aux_loss_weight }
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
#include <vector>
#if defined(_M_X64) || defined(__x86_64__)
#include <immintrin.h>
#include <intrin.h>
#endif

#include "jsonlite.h"

using gptbridge::jsonlite::JsonParser;
using gptbridge::jsonlite::JsonValue;

namespace xct {

#include "xct_util.h"
#include "xct_tpu.h"
#include "xct_math.h"
#include "xct_backward.h"
#include "xct_ckpt.h"
#include "xct_job.h"
#include "xct_depth.h"

} // namespace xct

int main(int argc, char** argv) {
    std::string job_path, report_path;
    bool do_smoke = false;
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
        else if (a == "--inputcheck") return xct::inputcheck();
    }
    if (do_smoke) return xct::smoke();
    if (job_path.empty()) {
        std::fprintf(stderr, "usage: xingcheng_trainer --job <job.json> [--report <out.json>] | --smoke | --gradcheck | --maskcheck | --headcheck | --rulecheck | --depthcheck | --inputcheck\n");
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
    } catch (...) {
        std::fprintf(stderr, "train-job error: malformed job or unrecoverable state\n");
        return 1;
    }
}
