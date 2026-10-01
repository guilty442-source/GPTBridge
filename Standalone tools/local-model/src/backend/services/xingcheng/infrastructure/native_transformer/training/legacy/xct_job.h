// xct_job.h — B94 fragment of xingcheng_trainer.cpp (job/load_data/run_job/smoke).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

// ------------------------------------------------------------------- job --

struct Example {
    std::vector<int> ids, labels;              // sft/pretrain
    std::vector<int> rej_ids, rej_labels;      // dpo
    std::vector<float> vision;                 // early-fusion patches (flat P*D)
    int vision_patches = 0;
    int vision_dim = 0;
};

static std::vector<Example> load_data(const JsonValue* d, const std::string& fmt,
                                      int max_rows, int max_len) {
    std::vector<Example> out;
    std::string path = j_str(d, "path", "");
    std::ifstream f(path);
    if (!f) throw "data: path unreadable";
    std::string line;
    while ((int)out.size() < max_rows && std::getline(f, line)) {
        if (line.empty()) continue;
        JsonValue row;
        try { row = JsonParser(line).parse(); } catch (...) { continue; }
        Example e;
        if (fmt == "dpo") {
            const JsonValue* ch = row.get("chosen");
            const JsonValue* rj = row.get("rejected");
            if (!ch || !rj) continue;
            if (row.get("vision_patches") || ch->get("vision_patches") ||
                rj->get("vision_patches"))
                throw "data: vision unsupported for dpo";
            e.ids = j_ids(ch, "input_ids");
            e.labels = j_ids(ch, "labels");
            if (e.labels.empty()) e.labels = e.ids;
            e.rej_ids = j_ids(rj, "input_ids");
            e.rej_labels = j_ids(rj, "labels");
            if (e.rej_labels.empty()) e.rej_labels = e.rej_ids;
        } else if (fmt == "grpo") {
            // {"prompt_ids": [...], "completion_ids": [...]} — the
            // completion is the verifiable target the sampled rollouts
            // are rewarded against.
            e.ids = j_ids(&row, "prompt_ids");
            e.labels = j_ids(&row, "completion_ids");
            if (!e.ids.empty() && !e.labels.empty()) {
                out.push_back(std::move(e));
            }
            continue;
        } else {
            e.ids = j_ids(&row, "input_ids");
            if (fmt == "sft") {
                e.labels = j_ids(&row, "labels");
                if (e.labels.empty()) e.labels = e.ids;
            } else {
                e.labels = e.ids;              // pretrain: shifted CE
            }
            // Optional early-fusion patches; malformed grids throw inside
            // j_patch_grid (never silently partial).
            if (j_patch_grid(&row, "vision_patches", e.vision,
                             e.vision_patches, e.vision_dim) &&
                (int)e.vision.size() != e.vision_patches * e.vision_dim)
                throw "data: VISION_DATA_SIZE";
        }
        if ((int)e.ids.size() > max_len) { e.ids.resize(max_len); e.labels.resize(max_len); }
        if ((int)e.rej_ids.size() > max_len) { e.rej_ids.resize(max_len); e.rej_labels.resize(max_len); }
        if (e.ids.size() >= 2) out.push_back(std::move(e));
    }
    return out;
}

struct TrainCfg {
    float lr = 3e-4f, wd = 0.01f, clip = 1.0f, beta = 0.1f;
    int warmup = 0, max_steps = 100, log_every = 10, ckpt_every = 0;
    uint64_t seed = 42;
    double deadline_s = 0.0;                   // 0 = unbounded (bounded by steps)
    std::string init_ckpt, emit_ckpt, decay = "cosine";
    bool overwrite = false;
    // Native Thinking RL (grpo): on-policy group rollouts, verifiable
    // reward, group-normalized advantage, KL-to-reference penalty.
    int group_size = 4;                        // parallel hypotheses/prompt
    int max_new = 8;                           // rollout completion length
    float temperature = 1.0f;                  // rollout sampling temp
    float kl_coef = 0.02f;                     // KL(policy||ref) weight
    std::string reward = "exact";              // exact|prefix
    // TPU-cluster lanes: threads 0 = auto (min(8, hw), cap 16), 1 = serial;
    // simd toggles the runtime AVX2/FMA dispatch (scalar fallback).
    int threads = 0;
    bool simd = true;
};

static double now_s() {
    return std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// AdamW update (learning rule): decoupled weight decay + bias-corrected
// first/second moments. Depends on activities (via g), the supervised
// target (via ce_loss dlogits -> g) and the current weights (wd term).

// NativeCudaTrainingPlane §26: when XINGCHENG_TRAINER_CUDA_OPT is set
// and the CUDA lane probes live, each tensor's update runs as one fused
// device kernel with w/m/v resident — the host only ships the gradient
// and reads back w (host forward still needs it this phase). Any miss
// falls through to the scalar path per tensor — never a partial tensor.
#if defined(XINGCHENG_CUDA)
extern "C" int xcuda_adamw_probe();
extern "C" int xcuda_adamw_bind(const float*, const float*, const float*,
                              long long);
extern "C" int xcuda_adamw_step_dev(const float*, const void*, float,
                                    float, float, int);
extern "C" int xcuda_adamw_sync(const void*, float*, float*, float*);
#else
static int xcuda_adamw_probe() { return 0; }
static int xcuda_adamw_bind(const float*, const float*, const float*,
                            long long) { return 3; }
static int xcuda_adamw_step_dev(const float*, const void*, float, float,
                                float, int) { return 3; }
static int xcuda_adamw_sync(const void*, float*, float*, float*) {
    return 3;
}
#endif

static void adamw_step(Params& p, float gscale, float lr_t, float wd,
                       int step) {
    const float b1 = 0.9f, b2 = 0.999f, eps = 1e-8f;
    float bc1 = 1.0f - std::pow(b1, step + 1),
          bc2 = 1.0f - std::pow(b2, step + 1);
    // Env-gated per run: certification precedent is the harness's
    // bit-parity check; the flag keeps production runs opt-in until the
    // bf16-training-cert gate formally promotes the device optimizer.
    static const bool cuda_opt =
        std::getenv("XINGCHENG_TRAINER_CUDA_OPT") != nullptr;
    static const bool cuda_ok =
        cuda_opt && xcuda_adamw_probe() != 0;
    for (auto& n : p.order) {
        // DeepSeek aux-free lb_bias is a routing-time buffer updated by
        // the sign rule (lb_bias_step), never by the optimizer — without
        // this guard decoupled weight decay would pull it to zero.
        if (n.size() >= 7 && n.compare(n.size() - 7, 7, "lb_bias") == 0)
            continue;
        // §41 ParameterFreezeMap: frozen params own no Adam moments
        // (sparse optimizer) and are never updated.
        if (p.frozen.count(n)) continue;
        Tensor& w = p.w[n]; Tensor& g = p.g[n];
        // §44 gradient sparsity, scoped to routed experts: an expert no
        // token selected this step (backward never marked it touched)
        // gets no gradient update AND no optimizer update — decoupled
        // weight decay would otherwise silently shrink dormant experts.
        // Dense params keep standard AdamW semantics (wd applies at g=0).
        if (n.find(".experts.") != std::string::npos &&
            !p.touched.count(n))
            continue;
        Tensor& m = p.m[n]; Tensor& v = p.v[n];
        if (cuda_ok) {
            const int64_t cnt = static_cast<int64_t>(w.d.size());
            if (xcuda_adamw_bind(w.d.data(), m.d.data(), v.d.data(),
                                 cnt) == 0) {
                if (xcuda_adamw_step_dev(g.d.data(), w.d.data(), gscale,
                                         lr_t, wd, step) != 0 ||
                    xcuda_adamw_sync(w.d.data(), w.d.data(), nullptr,
                                     nullptr) != 0) {
                    // Device holds the newest m/v — pull them back so
                    // the scalar fallback resumes from the last good
                    // optimizer state rather than stale host copies.
                    xcuda_adamw_sync(w.d.data(), w.d.data(),
                                     m.d.data(), v.d.data());
                } else {
                    // consumed: the fused-zero contract clears the host
                    // gradient so the next step's backward starts clean.
                    std::fill(g.d.begin(), g.d.end(), 0.0f);
                    continue;
                }
            }
        }
        tpu_elementwise((int64_t)w.d.size(), [&](int64_t i) {
            float gi = g.d[(size_t)i] * gscale;
            g.d[(size_t)i] = 0.0f;   // consumed: fused zero_grad
            m.d[(size_t)i] = b1 * m.d[(size_t)i] + (1 - b1) * gi;
            v.d[(size_t)i] = b2 * v.d[(size_t)i] + (1 - b2) * gi * gi;
            float mh = m.d[(size_t)i] / bc1, vh = v.d[(size_t)i] / bc2;
            w.d[(size_t)i] -=
                lr_t * (mh / (std::sqrt(vh) + eps) + wd * w.d[(size_t)i]);
        });
    }
    p.touched.clear();
}

// DeepSeek V3 aux-loss-free load balancing: per-expert bias b_e ranks
// selection (s+b) while combination weights stay s; after each forward
// the batch's assignment counts nudge b_e toward under-served experts —
// b_e += u * sign(mean_load - load_e). Piecewise-constant by design.
static void lb_bias_step(Params& p, const ModelConfig& c, const Fwd& o) {
    if (!c.moe_auxfree_balance || c.moe_lb_bias_rate <= 0.0f) return;
    const float u = c.moe_lb_bias_rate;
    for (int l = 0; l < c.layers; ++l) {
        if (!(c.moe_experts > 0 && (l % c.moe_layer_interval == 0)))
            continue;
        const LayerCache& L = o.layers[l];
        const int E = c.moe_experts, K = c.moe_top_k;
        const size_t slots = L.moe_idx.size();
        if (!slots) continue;
        const int T = (int)(slots / K);
        std::vector<float> cnt((size_t)E, 0.0f);
        for (int e : L.moe_idx) cnt[(size_t)e] += 1.0f;
        const float mean = (float)(T * K) / (float)E;
        Tensor& b = p.w[ln(l, "lb_bias")];
        for (int e = 0; e < E; ++e) {
            float err = mean - cnt[(size_t)e];
            b.d[(size_t)e] += u * (err > 0.0f ? 1.0f : err < 0.0f ? -1.0f
                                                              : 0.0f);
        }
    }
}

// cross-entropy with next-token shift inside a row
static void shift_labels(std::vector<int>& lab) {
    if (lab.size() < 2) return;
    for (size_t i = 0; i + 1 < lab.size(); ++i) lab[i] = lab[i + 1];
    lab.back() = -100;
}

static JsonValue run_job(const JsonValue& job) {
    const JsonValue* mj = job.get("model");
    const JsonValue* tj = job.get("train");
    const JsonValue* dj = job.get("data");
    std::string task = j_str(&job, "task", "sft");

    ModelConfig c = parse_model(mj);
    TrainCfg tc;
    tc.lr = (float)j_num(tj, "lr", tc.lr);
    tc.wd = (float)j_num(tj, "weight_decay", tc.wd);
    tc.clip = (float)j_num(tj, "grad_clip", tc.clip);
    tc.beta = (float)j_num(tj, "beta", tc.beta);
    tc.warmup = j_int(tj, "warmup_steps", 0);
    tc.max_steps = j_int(tj, "max_steps", tc.max_steps);
    tc.log_every = j_int(tj, "log_every", tc.log_every);
    tc.ckpt_every = j_int(tj, "checkpoint_every", 0);
    tc.seed = (uint64_t)j_num(tj, "seed", tc.seed);
    tc.deadline_s = j_num(tj, "deadline_s", 0);
    tc.decay = j_str(tj, "lr_decay", tc.decay);
    tc.group_size = j_int(tj, "group_size", tc.group_size);
    tc.max_new = j_int(tj, "max_new_tokens", tc.max_new);
    tc.temperature = (float)j_num(tj, "temperature", tc.temperature);
    tc.kl_coef = (float)j_num(tj, "kl_coef", tc.kl_coef);
    tc.reward = j_str(tj, "reward", tc.reward);
    if (task == "grpo") {
        if (tc.group_size < 2 || tc.group_size > 8)
            throw "train: group_size must be in [2,8]";
        if (tc.max_new < 1 || tc.max_new > 64)
            throw "train: max_new_tokens must be in [1,64]";
        if (tc.reward != "exact" && tc.reward != "prefix")
            throw "train: unsupported grpo reward";
    }
    tc.init_ckpt = j_str(tj, "init_checkpoint", "");
    tc.emit_ckpt = j_str(tj, "emit_checkpoint", "");
    tc.overwrite = j_bool(tj, "overwrite", false);
    tc.threads = j_int(tj, "threads", tc.threads);
    tc.simd = j_bool(tj, "simd", tc.simd);
    // Operator env overrides win over the job fields (bounded either way).
    if (const char* e = std::getenv("XCT_TPU_THREADS"))
        tc.threads = std::atoi(e);
    if (const char* e = std::getenv("XCT_TPU_SIMD"))
        tc.simd = !(e[0] == '0' && e[1] == '\0');
    g_tpu.threads = tc.threads;
    g_tpu.simd = tc.simd;
    int max_rows = j_int(dj, "max_rows", 10000);
    int max_len = j_int(dj, "max_len", c.max_pos);

    Params p;
    // §41 ParameterFreezeMap: train.freeze = ["layers.*.experts.",
    // "embed", ...] — resolved at alloc_adam inside init_params, so
    // frozen params never allocate Adam moments (§43 sparse optimizer).
    if (const JsonValue* fj = tj ? tj->get("freeze") : nullptr)
        if (fj->type == JsonValue::Type::Array)
            for (const auto& v : fj->array)
                if (v.type == JsonValue::Type::String)
                    p.freeze_patterns.push_back(v.string);
    init_params(p, c, tc.seed);
    ModelConfig file_cfg = c;
    if (!tc.init_ckpt.empty()) {
        if (!ckpt_load(p, file_cfg, tc.init_ckpt))
            throw "init_checkpoint: unreadable or shape mismatch";
        if (file_cfg.use_vision != c.use_vision ||
            file_cfg.vision_patch_dim != c.vision_patch_dim)
            throw "init_checkpoint: vision config mismatch";
    }
    // DPO/GRPO reference: frozen copy of the initial weights
    Params ref;
    if (task == "dpo" || task == "grpo") { ref = p; }

    std::vector<Example> data = load_data(dj, j_str(dj, "format", task), max_rows, max_len);
    if (data.empty()) throw "data: no usable rows";

    std::mt19937 rng((uint32_t)tc.seed);
    std::shuffle(data.begin(), data.end(), rng);

    std::string log;
    std::vector<float> losses;
    double t0 = now_s();
    int step = 0;
    bool deadline_hit = false;
    Fwd fw;
    std::vector<float> dlogits;
    float mtp_last = 0.0f;
    float mtp_stack_last = 0.0f;
    int64_t grpo_rollouts = 0;
    double grpo_reward_sum = 0.0, grpo_kl_sum = 0.0;

    while (step < tc.max_steps) {
        for (auto& ex : data) {
            if (step >= tc.max_steps) break;
            if (tc.deadline_s > 0 && now_s() - t0 > tc.deadline_s) {
                deadline_hit = true; break;
            }
            // Gradients self-clear: adamw_step zeroes each buffer as it
            // consumes it (fused zero_grad) — params skipped by the
            // §44/§41 guards always hold zero already.
            float loss = 0.0f;
            if (task == "dpo") {
                // policy chosen
                fw.layers.clear(); fw.moe_aux = 0.0f; fw.moe_zloss = 0.0f; fw.csa_idx = 0.0f;
                fwd(p, c, ex.ids, fw);
                lb_bias_step(p, c, fw);
                float lp_c = seq_logprob(fw.logits, ex.labels, (int)ex.ids.size(), c.vocab);
                Fwd fc; fwd(ref, c, ex.ids, fc);
                float rp_c = seq_logprob(fc.logits, ex.labels, (int)ex.ids.size(), c.vocab);
                Fwd fr; fwd(p, c, ex.rej_ids, fr);
                float lp_r = seq_logprob(fr.logits, ex.rej_labels, (int)ex.rej_ids.size(), c.vocab);
                Fwd frr; fwd(ref, c, ex.rej_ids, frr);
                float rp_r = seq_logprob(frr.logits, ex.rej_labels, (int)ex.rej_ids.size(), c.vocab);
                float margin = (lp_c - rp_c) - (lp_r - rp_r);
                float sig = 1.0f / (1.0f + std::exp(-tc.beta * margin));
                loss = -std::log(sig + 1e-9f);
                // dL/dlp_chosen = -beta*sigma(-beta*margin) = -beta*(1-sig);
                // dL/dlp_rejected = +beta*(1-sig). soft_grad emits
                // scale*(p - 1[y]) = scale*d(-lp)/dz, so scale = beta*(1-sig).
                float s = tc.beta * (1.0f - sig);
                std::vector<float> dl_c(fw.logits.size(), 0.0f), dl_r(fr.logits.size(), 0.0f);
                auto soft_grad = [&](const std::vector<float>& lg,
                                     const std::vector<int>& lab, int T,
                                     float scale, std::vector<float>& dl) {
                    for (int t = 0; t < T; ++t) {
                        int y = lab[t];
                        if (y < 0 || y >= c.vocab) continue;
                        const float* lr = lg.data() + (size_t)t * c.vocab;
                        float mx = *std::max_element(lr, lr + c.vocab), sum = 0.0f;
                        for (int i = 0; i < c.vocab; ++i) sum += std::exp(lr[i] - mx);
                        float* d = dl.data() + (size_t)t * c.vocab;
                        for (int i = 0; i < c.vocab; ++i) d[i] = scale * std::exp(lr[i] - mx) / sum;
                        d[y] -= scale;
                    }
                };
                soft_grad(fw.logits, ex.labels, (int)ex.ids.size(), s, dl_c);
                soft_grad(fr.logits, ex.rej_labels, (int)ex.rej_ids.size(), -s, dl_r);
                bwd(p, c, ex.ids, fw, dl_c, 0.0f);
                Fwd fr2 = std::move(fr);        // reuse caches for rej backward
                bwd(p, c, ex.rej_ids, fr2, dl_r, 0.0f);
            } else if (task == "grpo") {
                // Native Thinking RL: sample G parallel rollouts from the
                // current policy, score them with a verifiable reward,
                // group-normalize into advantages, then take one policy-
                // gradient step with a KL pull toward the reference.
                const int G = tc.group_size, M = tc.max_new;
                const int P = (int)ex.ids.size();
                struct Rollout {
                    std::vector<int> toks;
                    float reward = 0.0f, adv = 0.0f;
                };
                std::vector<Rollout> ro((size_t)G);
                float rsum = 0.0f;
                for (auto& r : ro) {
                    std::vector<int> seq = ex.ids;
                    for (int m = 0; m < M; ++m) {
                        fw.layers.clear(); fw.moe_aux = 0.0f; fw.moe_zloss = 0.0f; fw.csa_idx = 0.0f;
                        fwd(p, c, seq, fw);
                        const float* lr = fw.logits.data() +
                            ((size_t)seq.size() - 1) * c.vocab;
                        seq.push_back(
                            sample_cat(lr, c.vocab, tc.temperature, rng));
                    }
                    r.toks.assign(seq.begin() + P, seq.end());
                    // Verifiable reward against the gold completion.
                    if (tc.reward == "exact") {
                        r.reward = r.toks == ex.labels ? 1.0f : 0.0f;
                    } else {  // prefix: matched-prefix fraction
                        int m = 0;
                        while (m < (int)r.toks.size() &&
                               m < (int)ex.labels.size() &&
                               r.toks[(size_t)m] == ex.labels[(size_t)m]) ++m;
                        r.reward = ex.labels.empty()
                            ? 0.0f : (float)m / (float)ex.labels.size();
                    }
                    rsum += r.reward;
                    ++grpo_rollouts;
                }
                const float mean = rsum / (float)G;
                float var = 0.0f;
                for (auto& r : ro) var += (r.reward - mean) * (r.reward - mean);
                const float std_dev = std::sqrt(var / (float)G);
                for (auto& r : ro) {
                    r.adv = std_dev > 1e-4f ? (r.reward - mean) / std_dev : 0.0f;
                }
                grpo_reward_sum += rsum;
                float step_loss = 0.0f;
                for (auto& r : ro) {
                    if (r.adv == 0.0f && tc.kl_coef <= 0.0f) continue;
                    std::vector<int> full = ex.ids;
                    full.insert(full.end(), r.toks.begin(), r.toks.end());
                    const int T = (int)full.size();
                    // Completion labels: logits[t] predicts full[t+1] over
                    // positions P-1..T-2; prompt positions stay -100.
                    std::vector<int> lab((size_t)T - 1, -100);
                    for (int t = P - 1; t < T - 1; ++t) {
                        lab[(size_t)t] = full[(size_t)t + 1];
                    }
                    fw.layers.clear(); fw.moe_aux = 0.0f; fw.moe_zloss = 0.0f; fw.csa_idx = 0.0f;
                    fwd(p, c, std::vector<int>(full.begin(), full.end() - 1),
                        fw);
                    Fwd rf;
                    fwd(ref, c, std::vector<int>(full.begin(), full.end() - 1),
                        rf);
                    const float lp_p = seq_logprob(
                        fw.logits, lab, T - 1, c.vocab);
                    const float lp_r = seq_logprob(
                        rf.logits, lab, T - 1, c.vocab);
                    const int ntok = T - P;  // completion positions scored
                    const float kl = (lp_p - lp_r) / (float)ntok;
                    step_loss +=
                        (-r.adv * lp_p + tc.kl_coef * (lp_p - lp_r)) /
                            (float)ntok;
                    grpo_kl_sum += kl;
                    // dL/dz = (A - kl_c)/ntok * (softmax - 1[y]) over the
                    // completion positions only.
                    std::vector<float> dl(fw.logits.size(), 0.0f);
                    const float scale = (r.adv - tc.kl_coef) / (float)ntok;
                    for (int t = P - 1; t < T - 1; ++t) {
                        const int y = full[(size_t)t + 1];
                        if (y < 0 || y >= c.vocab) continue;
                        soft_grad_row(
                            fw.logits.data() + (size_t)t * c.vocab,
                            c.vocab, y, scale,
                            dl.data() + (size_t)t * c.vocab);
                    }
                    bwd(p, c,
                        std::vector<int>(full.begin(), full.end() - 1),
                        fw, dl, 0.0f);
                }
                loss = step_loss / (float)G;
            } else {
                std::vector<int> lab = ex.labels;
                if (task == "pretrain" || j_str(dj, "format", task) == "pretrain")
                    shift_labels(lab);
                fw.layers.clear(); fw.moe_aux = 0.0f; fw.moe_zloss = 0.0f; fw.csa_idx = 0.0f;
                if (!ex.vision.empty()) {
                    // Vision early-fusion: prefix rows carry -100 labels
                    // (ce_loss skips them; loss normalizes over text only).
                    if (!c.use_vision)
                        throw "data: vision patches but model use_vision=false";
                    if (ex.vision_dim != c.vision_patch_dim)
                        throw "data: vision dim mismatch";
                    if (ex.vision_patches > c.vision_max_patches)
                        throw "data: too many vision patches";
                    std::vector<int> vlab((size_t)ex.vision_patches, -100);
                    vlab.insert(vlab.end(), lab.begin(), lab.end());
                    const int T = ex.vision_patches + (int)ex.ids.size();
                    fwd(p, c, ex.ids, fw, &ex.vision, ex.vision_patches);
                    lb_bias_step(p, c, fw);
                    loss = ce_loss(fw.logits, vlab, T, c.vocab, dlogits)
                           + fw.moe_aux + fw.moe_zloss + fw.csa_idx
                           + fw.mtp.loss;
                    mtp_last = fw.mtp.loss;
                    std::vector<std::vector<float>> dmtp;
                    loss += mtp_stack_last =
                        mtp_stack_aux_loss(c, ex.ids, fw, dmtp);
                    bwd(p, c, ex.ids, fw, dlogits, 1.0f, &ex.vision, &dmtp);
                } else {
                    fwd(p, c, ex.ids, fw);
                    lb_bias_step(p, c, fw);
                    loss = ce_loss(fw.logits, lab, (int)ex.ids.size(), c.vocab, dlogits)
                           + fw.moe_aux + fw.moe_zloss + fw.csa_idx
                           + fw.mtp.loss;
                    mtp_last = fw.mtp.loss;
                    std::vector<std::vector<float>> dmtp;
                    loss += mtp_stack_last =
                        mtp_stack_aux_loss(c, ex.ids, fw, dmtp);
                    bwd(p, c, ex.ids, fw, dlogits, 1.0f, nullptr, &dmtp);
                }
                if (std::getenv("XCT_DUMP_G")) {
                    uint64_t h = 1469598103934665603ull;
                    for (auto& n : p.order) {
                        auto it = p.g.find(n);
                        if (it == p.g.end()) continue;
                        for (float x : it->second.d) {
                            uint32_t u; std::memcpy(&u, &x, 4);
                            h ^= u; h *= 1099511628211ull;
                        }
                    }
                    std::fprintf(stderr, "GS %d %016llx\n", (int)step,
                               (unsigned long long)h);
                }
            }
            // grad clip (global norm over trainable params only —
            // frozen grads would inflate the norm and shrink the
            // effective scale for the params actually being updated)
            double gnorm = 0.0f;
            for (auto& n : p.order) {
                if (p.frozen.count(n)) continue;
                for (float x : p.g[n].d) gnorm += (double)x * x;
            }
            gnorm = std::sqrt(gnorm);
            float gscale = (tc.clip > 0 && gnorm > tc.clip) ? tc.clip / (float)gnorm : 1.0f;
            // adamw
            float lr_t = tc.lr;
            if (tc.warmup > 0 && step < tc.warmup) lr_t *= (float)(step + 1) / tc.warmup;
            else if (tc.decay == "cosine" && tc.max_steps > tc.warmup) {
                float pr = (float)(step - tc.warmup) / (tc.max_steps - tc.warmup);
                lr_t *= 0.5f * (1.0f + std::cos(3.14159265f * std::min(1.0f, pr)));
            }
            adamw_step(p, gscale, lr_t, tc.wd, step);
            losses.push_back(loss);
            ++step;
            if (tc.ckpt_every > 0 && step % tc.ckpt_every == 0 && !tc.emit_ckpt.empty())
                ckpt_save(p, c, tc.emit_ckpt, /*overwrite*/true);
        }
        if (deadline_hit) break;
    }

    bool finite = true;
    for (auto& n : p.order)
        for (float x : p.w[n].d)
            if (!std::isfinite(x)) finite = false;

    bool emitted = false;
    if (!tc.emit_ckpt.empty()) emitted = ckpt_save(p, c, tc.emit_ckpt, tc.overwrite);

    JsonValue r; r.type = JsonValue::Type::Object;
    auto put = [&](const char* k, JsonValue v) { r.object.emplace_back(k, std::move(v)); };
    auto num = [](double x) { JsonValue v; v.type = JsonValue::Type::Number; v.number = x; return v; };
    auto str = [](const char* s) { JsonValue v; v.type = JsonValue::Type::String; v.string = s; return v; };
    auto bol = [](bool b) { JsonValue v; v.type = JsonValue::Type::Bool; v.boolean = b; return v; };
    put("schema", str("star-native-train-report/v1"));
    put("task", str(task.c_str()));
    {
        const std::string gen = j_str(mj, "generation", "");
        if (!gen.empty()) put("generation", str(gen.c_str()));
    }
    {
        JsonValue tpu; tpu.type = JsonValue::Type::Object;
        tpu.object.emplace_back("threads", num((double)tpu_threads()));
        tpu.object.emplace_back("simd", str(tpu_simd_name()));
        put("tpu_cluster", tpu);
    }
    put("steps", num(step));
    put("examples", num((double)data.size()));
    put("deadline_hit", bol(deadline_hit));
    put("params_finite", bol(finite));
    put("checkpoint_emitted", bol(emitted));
    put("checkpoint_path", str(tc.emit_ckpt.c_str()));
    if (!losses.empty()) {
        put("loss_first", num(losses.front()));
        put("loss_last", num(losses.back()));
        float mn = *std::min_element(losses.begin(), losses.end());
        put("loss_min", num(mn));
        JsonValue tail; tail.type = JsonValue::Type::Array;
        size_t st = losses.size() > 10 ? losses.size() - 10 : 0;
        for (size_t i = st; i < losses.size(); ++i) tail.array.push_back(num(losses[i]));
        put("loss_tail", tail);
    }
    // Router-health observation (B139): last forward's accumulated
    // load-balancing aux — ≈moe_aux_w×layers at perfect balance.
    if (c.moe_experts > 0) {
        put("moe_aux_last", num(fw.moe_aux));
        put("moe_zlast", num(fw.moe_zloss));
    }
    // CSA2 indexer health: last forward's alignment CE (~0 once index
    // scores track the main attention mass).
    if (c.csa_ratio >= 2 && c.csa_indexer)
        put("csa_idx_last", num(fw.csa_idx));
    // DeepSeek MTP observability: weighted aux CE of the last example.
    if (c.mtp_num_layers > 0) put("mtp_loss_last", num(mtp_last));
    // v29 MTP stack observability: weighted aux CE of the last example.
    if (c.mtp_depth > 0) put("mtp_stack_loss_last", num(mtp_stack_last));
    // §45 parameter-efficiency metrics: trainable vs frozen counts and
    // gain-per-million — the capability loop's comparison currency.
    {
        const int64_t trainable = p.trainable_params();
        put("trainable_params", num((double)trainable));
        put("frozen_params", num((double)p.frozen_params()));
        if (!p.freeze_patterns.empty()) {
            JsonValue fp; fp.type = JsonValue::Type::Array;
            for (auto& s : p.freeze_patterns)
                fp.array.push_back(str(s.c_str()));
            put("freeze_patterns", fp);
        }
        if (!losses.empty() && trainable > 0)
            put("gain_per_million_trainable_params",
                num((losses.front() - losses.back()) /
                    (trainable / 1e6)));
        double el = now_s() - t0;
        int64_t toks = 0;
        for (auto& ex : data) toks += (int64_t)ex.ids.size();
        if (el > 0 && step > 0)
            put("tokens_per_sec", num(toks * (double)step /
                (double)data.size() / el));
    }
    if (task == "grpo") {
        put("rollouts", num((double)grpo_rollouts));
        const double seen = grpo_rollouts > 0 ? (double)grpo_rollouts : 1.0;
        put("reward_mean", num(grpo_reward_sum / seen));
        put("kl_mean", num(grpo_kl_sum / seen));
    }
    put("elapsed_s", num(now_s() - t0));
    return r;
}

// ------------------------------------------------------------- gradcheck --

// Central finite-difference check of analytic gradients on a tiny hybrid
// (deltanet + gated attention + gated-MoE) model. fp32 limits accuracy, so
// the pass bar is a loose relative tolerance — this catches sign/order
// bugs, not last-ulp drift.
static int gradcheck() {
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.full_attention_interval = 2;          // layer 0 deltanet, layer 1 attn
    c.attn_output_gate = true;
    c.qk_norm = true;
    c.partial_rotary = 0.5f;
    c.lin_key_heads = 1; c.lin_key_dim = 32;
    c.lin_value_heads = 2; c.lin_value_dim = 32;
    c.lin_conv_kernel = 4;
    c.moe_experts = 2; c.moe_top_k = 1; c.moe_layer_interval = 1;
    c.moe_expert_inter = 24; c.moe_shared_experts = 1;
    c.moe_shared_inter = 24; c.shared_expert_gate = true;
    if (const char* e = std::getenv("XCT_GC_E")) c.moe_experts = std::atoi(e);
    if (const char* k = std::getenv("XCT_GC_K")) c.moe_top_k = std::atoi(k);
    if (const char* s = std::getenv("XCT_GC_SHARED"))
        c.moe_shared_experts = std::atoi(s);
    if (const char* i = std::getenv("XCT_GC_INT"))
        c.full_attention_interval = std::atoi(i);
    if (const char* z = std::getenv("XCT_GC_PLAIN")) {
        c.attn_output_gate = false; c.qk_norm = false;
        c.partial_rotary = 1.0f;
    }
    // XCT_GC_CSA=1: enable the CSA2 lane on the full-attention layer
    // (r=2, K=2, window 4). XCT_GC_CSA_GROUP=2 additionally turns the
    // probe model all-attention so layer 0 produces the shared compressed
    // stream and layer 1 exercises the Reuse path + cross-layer grads.
    if (const char* e = std::getenv("XCT_GC_CSA")) {
        if (std::atoi(e) != 0) {
            c.global_attn_interval = 1;   // every attn layer is global
            c.csa_ratio = 2; c.csa_topk = 2; c.csa_window = 4;
            c.csa_rope_theta = 40000.0f;
            if (const char* g = std::getenv("XCT_GC_CSA_GROUP")) {
                c.full_attention_interval = 0;
                c.csa_group = std::atoi(g);
            }
        }
    }
    Params p;
    // Vision leg: the same numeric sweep also covers vision.patch_proj
    // and the prefix path (deterministic synthetic patches; prefix labels
    // masked). Text-only behaviour is pinned by --smoke instead.
    c.use_vision = true; c.vision_patch_dim = 8; c.vision_max_patches = 4;
    init_params(p, c, 7);
    std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31};
    std::vector<int> labels = {-100, 5, 7, 11, 13, 17, 19, 23, 29, 31};
    const int VP = 3, VD = 8;
    std::vector<float> vpatches((size_t)VP * VD);
    {
        std::mt19937 vrng(11);
        std::uniform_real_distribution<float> vd(-0.5f, 0.5f);
        for (auto& x : vpatches) x = vd(vrng);
    }
    std::vector<int> vlabels((size_t)VP, -100);
    vlabels.insert(vlabels.end(), labels.begin(), labels.end());
    const int VT = VP + (int)ids.size();
    auto loss_of = [&]() {
        Fwd fw;
        fwd(p, c, ids, fw, &vpatches, VP);
        std::vector<float> dl;
        return (double)ce_loss(fw.logits, vlabels, VT, c.vocab,
                               dl) + fw.moe_aux + fw.csa_idx;
    };
    p.zero_grad();
    Fwd fw;
    fwd(p, c, ids, fw, &vpatches, VP);
    std::vector<float> dl;
    double loss0 = ce_loss(fw.logits, vlabels, VT, c.vocab, dl)
                   + fw.moe_aux + fw.csa_idx;
    bwd(p, c, ids, fw, dl, 1.0f, &vpatches);
    const double eps = 4e-3;   // lift true signal above fp32 ulp noise in loss
    double worst_rel = 0.0, worst_abs = 0.0;
    std::string worst_name;
    int checked = 0, failed = 0;
    for (const auto& n : p.order) {
        Tensor& w = p.w[n];
        Tensor& g = p.g[n];
        // stride to keep the check bounded but cover every tensor kind
        size_t total = w.d.size();
        size_t stride = total > 8 ? total / 8 : 1;
        for (size_t i = 0; i < total; i += stride) {
            // Sparse top-k routing makes the loss piecewise in router
            // weights: finite differences straddle selection flips and
            // measure the jump, not the gradient. Skip ".gate" tensors.
            if (n.size() >= 5 && n.compare(n.size() - 5, 5, ".gate") == 0)
                continue;
            float orig = w.d[i];
            w.d[i] = orig + (float)eps; double lp = loss_of();
            w.d[i] = orig - (float)eps; double lm = loss_of();
            w.d[i] = orig;
            double num = (lp - lm) / (2.0 * eps);
            double ana = g.d[i];
            double abs_err = std::fabs(num - ana);
            double rel = abs_err / std::max(1e-4, std::fabs(num));
            // multi-step probe on suspicious elements: slope stable across
            // step sizes => real gradient mismatch, jittery => fp32 noise
            if (rel > 0.05 && abs_err > 1e-3) {
                std::printf("  probe %s[%zu]: ana=%.6f |", n.c_str(), i, ana);
                for (double e2 : {1e-3, 4e-3, 1.6e-2, 6.4e-2}) {
                    w.d[i] = orig + (float)e2; double lp2 = loss_of();
                    w.d[i] = orig - (float)e2; double lm2 = loss_of();
                    w.d[i] = orig;
                    std::printf(" e=%.3f:%.6f", e2, (lp2 - lm2) / (2.0 * e2));
                }
                std::printf("\n");
            }
            ++checked;
            if (rel > 0.10 && abs_err > 3e-3) {
                ++failed;
                std::printf("  FAIL %s[%zu]: ana=%.6f num=%.6f rel=%.3f abs=%.6f\n",
                            n.c_str(), i, ana, num, rel, abs_err);
                if (rel > worst_rel) {
                    worst_rel = rel; worst_abs = abs_err;
                    worst_name = n + "[" + std::to_string(i) + "]";
                }
            }
        }
    }
    bool ok = failed == 0;
    std::printf("gradcheck: loss=%.5f checked=%d failed=%d worst=%s "
                "rel=%.4f abs=%.6f -> %s\n", loss0, checked, failed,
                worst_name.c_str(), worst_rel, worst_abs,
                ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// ------------------------------------------------------------------ smoke --

static int smoke() {
    // L0-L2 maturity probe: structure init finite, fwd/bwd finite,
    // optimizer steps decrease loss on a fixed 8-sample set.
    std::string job = R"({
        "task":"sft",
        "model":{"vocab_size":64,"hidden_size":32,"intermediate_size":64,
                 "num_hidden_layers":2,"num_attention_heads":4,
                 "num_key_value_heads":2,"max_position_embeddings":32},
        "train":{"lr":0.05,"max_steps":30,"grad_clip":1.0,"warmup_steps":0,
                 "lr_decay":"constant","seed":7,"log_every":5},
        "data":{"path":"","format":"sft","max_rows":8,"max_len":12}
    })";
    // synthesize 8 samples in-memory: patch data path with a temp file
    std::string tmp = "_xct_smoke_data.jsonl";
    {
        std::ofstream f(tmp, std::ios::trunc);
        std::mt19937 rng(7);
        std::uniform_int_distribution<int> tok(3, 63);
        for (int i = 0; i < 8; ++i) {
            f << "{\"input_ids\":[";
            for (int t = 0; t < 12; ++t) f << (t ? "," : "") << tok(rng);
            f << "]}\n";
        }
    }
    std::string::size_type pos = job.find("\"path\":\"\"");
    job.replace(pos, 9, "\"path\":\"" + tmp + "\"");
    JsonValue j = JsonParser(job).parse();
    JsonValue r = run_job(j);
    std::remove(tmp.c_str());
    double l0 = r.get("loss_first")->number, l1 = r.get("loss_last")->number;
    bool ok = r.get("params_finite")->boolean && std::isfinite(l0) &&
              std::isfinite(l1) && l1 < l0;
    std::printf("smoke: loss_first=%.4f loss_last=%.4f finite=%d -> %s\n",
                l0, l1, (int)r.get("params_finite")->boolean, ok ? "PASS" : "FAIL");

    // Gemma4-mini leg: hybrid layers (sliding/full), 1 shared tail layer
    // (kv-owner = layer 1), PLE, dual rope, gelu-tanh, softcap, tied emb.
    std::string job4 = R"({
        "task":"sft",
        "model":{"model_type":"gemma4_text","vocab_size":64,"hidden_size":32,
                 "intermediate_size":64,"num_hidden_layers":4,
                 "num_attention_heads":4,"num_key_value_heads":2,
                 "head_dim":8,"global_head_dim":16,"sliding_window":4,
                 "num_kv_shared_layers":1,"hidden_size_per_layer_input":8,
                 "rope_theta":10000.0,
                 "rope_parameters":{"full_attention":{"rope_theta":1000000.0,
                                    "partial_rotary_factor":0.25}},
                 "final_logit_softcapping":30.0,
                 "hidden_activation":"gelu_pytorch_tanh",
                 "tie_word_embeddings":true,
                 "layer_types":["sliding_attention","full_attention",
                                "sliding_attention","full_attention"],
                 "max_position_embeddings":32},
        "train":{"lr":0.05,"max_steps":30,"grad_clip":1.0,"warmup_steps":0,
                 "lr_decay":"constant","seed":7,"log_every":5},
        "data":{"path":"","format":"sft","max_rows":8,"max_len":12}
    })";
    {
        std::ofstream f(tmp, std::ios::trunc);
        std::mt19937 rng(7);
        std::uniform_int_distribution<int> tok(3, 63);
        for (int i = 0; i < 8; ++i) {
            f << "{\"input_ids\":[";
            for (int t = 0; t < 12; ++t) f << (t ? "," : "") << tok(rng);
            f << "]}\n";
        }
    }
    std::string::size_type p4 = job4.find("\"path\":\"\"");
    job4.replace(p4, 9, "\"path\":\"" + tmp + "\"");
    JsonValue j4 = JsonParser(job4).parse();
    JsonValue r4 = run_job(j4);
    std::remove(tmp.c_str());
    double g0 = r4.get("loss_first")->number, g1 = r4.get("loss_last")->number;
    bool ok4 = r4.get("params_finite")->boolean && std::isfinite(g0) &&
               std::isfinite(g1) && g1 < g0;
    std::printf("smoke-gemma4: loss_first=%.4f loss_last=%.4f finite=%d -> %s\n",
                g0, g1, (int)r4.get("params_finite")->boolean,
                ok4 ? "PASS" : "FAIL");
    ok = ok && ok4;

    // GRPO leg (Native Thinking RL): 4 prompts with gold completions,
    // group_size=4 on-policy rollouts, prefix reward, KL-to-ref step.
    // Asserts the lane runs end-to-end with finite params/loss and a
    // recorded reward signal.
    std::string jobg = R"({
        "task":"grpo",
        "model":{"vocab_size":64,"hidden_size":32,"intermediate_size":64,
                 "num_hidden_layers":2,"num_attention_heads":4,
                 "num_key_value_heads":2,"max_position_embeddings":48},
        "train":{"lr":0.01,"max_steps":6,"grad_clip":1.0,"seed":7,
                 "lr_decay":"constant","group_size":4,"max_new_tokens":6,
                 "temperature":1.2,"kl_coef":0.02,"reward":"prefix"},
        "data":{"path":"","format":"grpo","max_rows":4,"max_len":16}
    })";
    {
        std::ofstream f(tmp, std::ios::trunc);
        std::mt19937 rng(11);
        std::uniform_int_distribution<int> tok(3, 63);
        for (int i = 0; i < 4; ++i) {
            f << "{\"prompt_ids\":[";
            for (int t = 0; t < 4; ++t) f << (t ? "," : "") << tok(rng);
            f << "],\"completion_ids\":[";
            for (int t = 0; t < 6; ++t) f << (t ? "," : "") << tok(rng);
            f << "]}\n";
        }
    }
    std::string::size_type pg = jobg.find("\"path\":\"\"");
    jobg.replace(pg, 9, "\"path\":\"" + tmp + "\"");
    JsonValue jg = JsonParser(jobg).parse();
    JsonValue rg = run_job(jg);
    std::remove(tmp.c_str());
    const bool okg = rg.get("params_finite")->boolean &&
        rg.get("rollouts")->number > 0 &&
        std::isfinite(rg.get("reward_mean")->number) &&
        std::isfinite(rg.get("loss_last")->number);
    std::printf("smoke-grpo: rollouts=%.0f reward_mean=%.4f loss_last=%.4f "
                "finite=%d -> %s\n",
                rg.get("rollouts")->number,
                rg.get("reward_mean")->number,
                rg.get("loss_last")->number,
                (int)rg.get("params_finite")->boolean, okg ? "PASS" : "FAIL");
    ok = ok && okg;
    std::fputs(gptbridge::jsonlite::json_serialize(r).c_str(), stdout);
    std::fputc('\n', stdout);
    return ok ? 0 : 1;
}

// -------------------------------------------------------------- maskcheck --

// Masked self-attention causality probe: corrupting the token at position j
// must leave logits[0..j) bitwise identical — a decoder may never read the
// future — while logits[j..] must move (non-vacuous perturbation). Identity
// is bitwise because every mixing op is causal-bounded: full attention
// scores rows s<=t only, the deltanet scan accumulates state strictly
// forward, and the depthwise conv reads x[t-j]. Sweeps the layer matrix
// all-attention / hybrid / all-linear so both mixers are exercised.
static int maskcheck() {
    int failures = 0;
    for (int interval : {0, 2, 100}) {
        ModelConfig c;
        c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
        c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
        c.full_attention_interval = interval;
        c.attn_output_gate = true;
        c.qk_norm = true;
        c.partial_rotary = 0.5f;
        c.lin_key_heads = 1; c.lin_key_dim = 32;
        c.lin_value_heads = 2; c.lin_value_dim = 32;
        c.lin_conv_kernel = 4;
        c.moe_experts = 2; c.moe_top_k = 1; c.moe_layer_interval = 1;
        c.moe_expert_inter = 24; c.moe_shared_experts = 1;
        c.moe_shared_inter = 24; c.shared_expert_gate = true;
        Params p;
        init_params(p, c, 13);
        std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
        const int T = (int)ids.size();
        Fwd fw0;
        fwd(p, c, ids, fw0);
        for (int j : {T - 1, T / 2}) {
            std::vector<int> ids2 = ids;
            ids2[j] = (ids2[j] + 13) % c.vocab;
            if (ids2[j] == ids[j]) ids2[j] = (ids2[j] + 1) % c.vocab;
            Fwd fw1;
            fwd(p, c, ids2, fw1);
            const size_t past = (size_t)j * c.vocab;
            bool sealed = std::memcmp(fw0.logits.data(), fw1.logits.data(),
                                      past * sizeof(float)) == 0;
            bool moved = std::memcmp(fw0.logits.data() + past,
                                     fw1.logits.data() + past,
                                     ((size_t)T * c.vocab - past) *
                                         sizeof(float)) != 0;
            if (!sealed || !moved) {
                ++failures;
                std::printf("  FAIL interval=%d j=%d sealed=%d moved=%d\n",
                            interval, j, (int)sealed, (int)moved);
            }
        }
    }
    bool ok = failures == 0;
    std::printf("maskcheck: causal-probe failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// -------------------------------------------------------------- headcheck --
//
// Multi-head attention probe: proves the fused decoder's full-attention
// layers really run `heads` independent views over GQA kv groups — the
// "analyze the sequence from multiple angles at once" contract:
//   1. normalized causal softmax: every (head,t) prob row sums to 1 on
//      s<=t and stays exactly 0 above the diagonal;
//   2. head isolation: perturbing head h's wq rows moves probs[h] and its
//      attn_out slice while every other head stays bitwise identical —
//      no cross-head leakage through the packed qkv buffers;
//   3. kv-group sharing: perturbing kv head g's wk/wv rows moves exactly
//      the q-heads {h | h/group == g} — the GQA map, and only it;
//   4. non-degeneracy: distinct heads produce distinct attention patterns
//      (a slicing bug that folds every head onto one view fails here).
// Sweeps GQA (4q/2kv), MHA (2q/2kv) and MQA (4q/1kv) geometries under the
// gated full-attention stack (attn_output_gate + qk_norm + partial
// rotary). The deltanet mixer's heads are covered by --maskcheck; this
// probe targets multi-head *attention*.
static int headcheck() {
    int failures = 0;
    const int geos[][2] = {{4, 2}, {2, 2}, {4, 1}};
    for (auto& ge : geos) {
        ModelConfig c;
        c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
        c.heads = ge[0]; c.kv_heads = ge[1]; c.max_pos = 64;
        c.full_attention_interval = 0;
        c.attn_output_gate = true;
        c.qk_norm = true;
        c.partial_rotary = 0.5f;
        Params p;
        init_params(p, c, 29);
        std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23};
        const int T = (int)ids.size();
        const int hd = c.hidden / c.heads;
        const int group = c.heads / c.kv_heads;
        const int qmul = c.attn_output_gate ? 2 : 1;
        Fwd fw0;
        fwd(p, c, ids, fw0);

        auto fail = [&](const char* what, int l, int h) {
            ++failures;
            std::printf("  FAIL heads=%d kv=%d %s layer=%d head=%d\n",
                        c.heads, c.kv_heads, what, l, h);
        };
        auto probs_eq = [&](const Fwd& a, const Fwd& b, int l, int h) {
            return std::memcmp(a.layers[l].probs.data() + (size_t)h * T * T,
                               b.layers[l].probs.data() + (size_t)h * T * T,
                               (size_t)T * T * sizeof(float)) == 0;
        };
        auto out_eq = [&](const Fwd& a, const Fwd& b, int l, int h) {
            for (int t = 0; t < T; ++t) {
                const float* ar = a.layers[l].attn_out.data() +
                                  ((size_t)t * c.heads + h) * hd;
                const float* br = b.layers[l].attn_out.data() +
                                  ((size_t)t * c.heads + h) * hd;
                if (std::memcmp(ar, br, (size_t)hd * sizeof(float)) != 0)
                    return false;
            }
            return true;
        };
        auto moved = [&](const Fwd& a, const Fwd& b) {
            return std::memcmp(a.logits.data(), b.logits.data(),
                               a.logits.size() * sizeof(float)) != 0;
        };
        auto perturb_rows = [&](Params& p2, int l, const char* wname,
                                int r0, int rn) {
            float* w = p2.w.at(ln(l, wname)).d.data();
            for (int r = r0; r < r0 + rn; ++r)
                for (int i = 0; i < c.hidden; ++i)
                    w[(size_t)r * c.hidden + i] += 0.05f;
        };

        for (int l = 0; l < c.layers; ++l) {
            const LayerCache& L = fw0.layers[l];
            for (int h = 0; h < c.heads; ++h)
                for (int t = 0; t < T; ++t) {
                    const float* pr =
                        L.probs.data() + ((size_t)h * T + t) * T;
                    float sum = 0.0f;
                    for (int s = 0; s <= t; ++s) sum += pr[s];
                    bool bad = std::fabs(sum - 1.0f) > 1e-5f;
                    for (int s = t + 1; s < T && !bad; ++s)
                        bad = pr[s] != 0.0f;
                    if (bad) fail("softmax", l, h);
                }
            for (int a = 0; a < c.heads; ++a)
                for (int b = a + 1; b < c.heads; ++b)
                    if (std::memcmp(L.probs.data() + (size_t)a * T * T,
                                    L.probs.data() + (size_t)b * T * T,
                                    (size_t)T * T * sizeof(float)) == 0)
                        fail("degenerate-heads", l, a);

            for (int hp = 0; hp < c.heads; ++hp) {
                Params p2 = p;
                perturb_rows(p2, l, "wq", hp * hd * qmul, hd);
                Fwd fw2;
                fwd(p2, c, ids, fw2);
                if (!moved(fw0, fw2)) fail("wq-vacuous", l, hp);
                for (int h = 0; h < c.heads; ++h) {
                    if (probs_eq(fw0, fw2, l, h) == (h == hp))
                        fail("wq-isolation", l, h);
                    if (out_eq(fw0, fw2, l, h) == (h == hp))
                        fail("wq-out-isolation", l, h);
                }
            }
            for (int g = 0; g < c.kv_heads; ++g)
                for (int which = 0; which < 2; ++which) {
                    Params p2 = p;
                    perturb_rows(p2, l, which ? "wk" : "wv", g * hd, hd);
                    Fwd fw2;
                    fwd(p2, c, ids, fw2);
                    if (!moved(fw0, fw2))
                        fail(which ? "wk-vacuous" : "wv-vacuous", l, g);
                    for (int h = 0; h < c.heads; ++h) {
                        bool member = (h / group) == g;
                        if (out_eq(fw0, fw2, l, h) != !member)
                            fail(which ? "wk-group" : "wv-group", l, h);
                        bool exp_probs_eq = (which == 0) || !member;
                        if (probs_eq(fw0, fw2, l, h) != exp_probs_eq)
                            fail(which ? "wk-probs" : "wv-probs", l, h);
                    }
                }
        }
    }
    bool ok = failures == 0;
    std::printf("headcheck: multi-head probe failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// -------------------------------------------------------------- rulecheck --

// The fused network's three canonical parts, each probed executably:
//   structure  — weights + activity topology: every declared weight has
//                matching grad/m/v state, expected per-layer params exist,
//                and one forward yields finite activities.
//   activation — short-timescale dynamics: scalar rules (sigmoid/silu/
//                softplus) obey their math, and cached activities satisfy
//                their invariants (softmax rows sum to 1, sigmoid gates in
//                (0,1), deltanet decay in (0,1], rms factors > 0).
//   learning   — long-timescale weight update: the rule depends on the
//                supervised target (different labels -> different grads),
//                on activities (all-masked labels -> zero CE signal), and
//                on current weights (pure decay scales w by 1-lr*wd);
//                a few AdamW steps reduce the loss.
static int rulecheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    // hybrid model: layer 0 deltanet, layer 1 gated full attention, MoE FFN
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.full_attention_interval = 2;
    c.attn_output_gate = true;
    c.qk_norm = true;
    c.partial_rotary = 0.5f;
    c.lin_key_heads = 1; c.lin_key_dim = 32;
    c.lin_value_heads = 2; c.lin_value_dim = 32;
    c.lin_conv_kernel = 4;
    c.moe_experts = 2; c.moe_top_k = 1; c.moe_layer_interval = 1;
    c.moe_expert_inter = 24; c.moe_shared_experts = 1;
    c.moe_shared_inter = 24; c.shared_expert_gate = true;
    Params p;
    init_params(p, c, 17);
    std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
    const int T = (int)ids.size();

    // ---- structure: variables and their topology -------------------------
    for (auto& n : p.order) {
        Tensor& w = p.w[n];
        if (!p.g.count(n) || !p.m.count(n) || !p.v.count(n))
            fail("structure: weight missing grad/m/v state");
        if (p.g[n].numel() != w.numel() || p.m[n].numel() != w.numel() ||
            p.v[n].numel() != w.numel())
            fail("structure: state shape mismatch");
        for (float x : w.d)
            if (!std::isfinite(x)) fail("structure: non-finite weight");
    }
    auto has = [&](const std::string& n) {
        if (!p.w.count(n)) fail((std::string("structure: missing ") + n).c_str());
    };
    has("embed"); has("lm_head"); has("norm_f");
    for (int l = 0; l < c.layers; ++l) {
        has(ln(l, "norm1")); has(ln(l, "norm2"));
        if (c.is_linear(l)) {
            for (const char* s : {"lin.in_proj_qkv", "lin.in_proj_z",
                                  "lin.in_proj_a", "lin.in_proj_b",
                                  "lin.conv1d", "lin.A_log", "lin.dt_bias",
                                  "lin.norm", "lin.out_proj"})
                has(ln(l, s));
        } else {
            for (const char* s : {"wq", "wk", "wv", "wo"})
                has(ln(l, s));
            if (c.qk_norm) { has(ln(l, "q_norm")); has(ln(l, "k_norm")); }
        }
        if (c.moe_experts > 0 && l % c.moe_layer_interval == 0) {
            has(ln(l, "gate"));
            for (int e = 0; e < c.moe_experts; ++e)
                for (const char* s : {"w1", "w3", "w2"})
                    has(ln(l, "experts.") + std::to_string(e) + "." + s);
            if (c.moe_shared_experts > 0)
                for (const char* s : {"w1", "w3", "w2"})
                    has(ln(l, "shared.0.") + s);
        }
    }
    Fwd fw;
    fwd(p, c, ids, fw);
    for (float x : fw.logits)
        if (!std::isfinite(x)) fail("structure: non-finite logits");
    for (float x : fw.hidden)
        if (!std::isfinite(x)) fail("structure: non-finite hidden");

    // ---- activation rule: short-timescale dynamics ------------------------
    if (sigmoid_f(0.0f) != 0.5f) fail("activation: sigmoid(0)");
    for (float x : {-1.f, 0.f, 1.f})
        if (!(sigmoid_f(x) > 0.0f && sigmoid_f(x) < 1.0f))
            fail("activation: sigmoid range");
    for (float x : {-30.f, 30.f})   // fp32 saturates exactly at the tails
        if (!(sigmoid_f(x) >= 0.0f && sigmoid_f(x) <= 1.0f))
            fail("activation: sigmoid tail range");
    if (std::fabs(silu_f(0.0f)) > 1e-7f ||
        std::fabs(silu_f(10.0f) - 10.0f) > 1e-2f)
        fail("activation: silu identity");
    if (std::fabs(softplus_f(0.0f) - 0.693147f) > 1e-4f)
        fail("activation: softplus(0)=ln2");
    for (float x : {-5.f, 0.f, 5.f})
        if (!(softplus_f(x) > 0.0f)) fail("activation: softplus range");
    for (int l = 0; l < c.layers; ++l) {
        const LayerCache& L = fw.layers[(size_t)l];
        if (c.is_linear(l)) {
            for (float x : L.lin_decay)
                if (!(x > 0.0f && x <= 1.0f))
                    fail("activation: deltanet decay range");
            for (float x : L.lin_orms)
                if (!(x > 0.0f && std::isfinite(x)))
                    fail("activation: lin rms factor");
        } else {
            // softmax rows: entries >=0, masked tail exactly 0, sums to 1
            for (int t = 0; t < T; ++t)
                for (int h = 0; h < c.heads; ++h) {
                    const float* pr =
                        L.probs.data() + ((size_t)h * T + t) * T;
                    float s = 0.0f;
                    for (int u = 0; u < T; ++u) {
                        if (!(pr[u] >= 0.0f))
                            fail("activation: prob<0");
                        if (u > t && pr[u] != 0.0f)
                            fail("activation: prob leak past mask");
                        if (u <= t) s += pr[u];
                    }
                    if (std::fabs(s - 1.0f) > 1e-4f)
                        fail("activation: prob row sum");
                }
            // output gate consistency: gated = out * sigmoid(gate)
            for (size_t i = 0; i < L.attn_gated.size(); ++i) {
                float expct =
                    L.attn_out[i] * sigmoid_f(L.attn_gate[i]);
                if (std::fabs(L.attn_gated[i] - expct) > 1e-5f)
                    fail("activation: attn gate");
            }
        }
        for (float x : L.rms1)
            if (!(x > 0.0f && std::isfinite(x))) fail("activation: rms1");
        for (float x : L.rms2)
            if (!(x > 0.0f && std::isfinite(x))) fail("activation: rms2");
        // MoE router softmax rows
        const int E = c.moe_experts;
        for (int t = 0; t < T; ++t) {
            float s = 0.0f;
            for (int e = 0; e < E; ++e) {
                float gp = L.gate_probs[(size_t)t * E + e];
                if (!(gp >= 0.0f && gp <= 1.0f))
                    fail("activation: router prob range");
                s += gp;
            }
            if (std::fabs(s - 1.0f) > 1e-4f)
                fail("activation: router row sum");
        }
    }

    // ---- learning rule: long-timescale weight update ----------------------
    // (a) all-masked labels -> zero supervised signal in dlogits
    {
        std::vector<int> nolab((size_t)T, -100);
        std::vector<float> dl;
        float l = ce_loss(fw.logits, nolab, T, c.vocab, dl);
        if (l != 0.0f) fail("learning: masked-label loss");
        for (float x : dl)
            if (x != 0.0f) fail("learning: masked-label dlogits");
    }
    // (b) supervised target dependence: different labels -> different grads
    {
        std::vector<int> lab = ids;
        shift_labels(lab);
        std::vector<float> dl;
        auto grads_of = [&](std::vector<int>& l, const char* key) {
            p.zero_grad();
            Fwd f;
            fwd(p, c, ids, f);
            ce_loss(f.logits, l, T, c.vocab, dl);
            bwd(p, c, ids, f, dl, 1.0f);
            return p.g[key].d;
        };
        std::vector<float> g1 = grads_of(lab, "lm_head");
        std::vector<int> lab2 = lab;
        lab2[0] = (lab2[0] + 1) % c.vocab;
        if (lab2[0] == lab[0]) lab2[0] = (lab2[0] + 1) % c.vocab;
        std::vector<float> g2 = grads_of(lab2, "lm_head");
        bool differ = false;
        for (size_t i = 0; i < g1.size(); ++i)
            if (g1[i] != g2[i]) { differ = true; break; }
        if (!differ) fail("learning: target independence");
    }
    // (c) weight dependence: fresh params (m=v=g=0) under adamw_step decay
    //     by exactly (1 - lr*wd)
    {
        Params p3;
        init_params(p3, c, 5);
        p3.zero_grad();
        const float lr = 0.01f, wd = 0.5f;
        std::vector<float> before = p3.w["lm_head"].d;
        adamw_step(p3, 1.0f, lr, wd, 0);
        const std::vector<float>& after = p3.w["lm_head"].d;
        for (size_t i = 0; i < before.size(); ++i) {
            float expct = before[i] * (1.0f - lr * wd);
            if (std::fabs(after[i] - expct) > 1e-6f)
                fail("learning: decoupled weight decay");
        }
    }
    // (d) AdamW steps on a fixed batch reduce the loss (rule actually learns)
    {
        std::vector<int> lab = ids;
        shift_labels(lab);
        float first = 0.0f, last = 0.0f;
        for (int s = 0; s < 4; ++s) {
            p.zero_grad();
            Fwd f;
            fwd(p, c, ids, f);
            std::vector<float> dl;
            float l = ce_loss(f.logits, lab, T, c.vocab, dl) + f.moe_aux + f.csa_idx;
            if (s == 0) first = l;
            last = l;
            bwd(p, c, ids, f, dl, 1.0f);
            adamw_step(p, 1.0f, 0.05f, 0.0f, s);
        }
        if (!(std::isfinite(last) && last < first))
            fail("learning: loss did not decrease");
    }

    bool ok = failures == 0;
    std::printf("rulecheck: structure+activation+learning failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// ------------------------------------------------------------- inputcheck --

// Input layer: token embedding (lookup table) + positional encoding.
// The architecture gathers all token rows in parallel — there is no
// RNN-style sequential input — so order information must be injected
// afterwards: rotary position coding on full-attention q/k, and the
// strictly forward recurrent state inside the deltanet/conv mixers.
// Probes: layer-0 residual input rows are bitwise the embed lookup rows
// (content only, no position); permuting ids permutes those rows; the
// rope score field is translation-invariant (relative positions) but
// non-degenerate across offsets; partial rotary leaves channels >= rd
// untouched; and reordered inputs produce different logits.
static int inputcheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.full_attention_interval = 2;
    c.attn_output_gate = true;
    c.qk_norm = true;
    c.partial_rotary = 0.5f;
    c.lin_key_heads = 1; c.lin_key_dim = 32;
    c.lin_value_heads = 2; c.lin_value_dim = 32;
    c.lin_conv_kernel = 4;
    Params p;
    init_params(p, c, 23);
    std::vector<int> ids = {3, 7, 11, 5, 7, 19, 23, 7, 29, 31, 37, 7};
    const int T = (int)ids.size();
    const int H = c.hidden;
    Fwd fw;
    fwd(p, c, ids, fw);

    // (1) token embedding is a lookup table: x_in[t] == embed[ids[t]]
    const std::vector<float>& xin = fw.layers[0].x_in;
    const float* emb = p.w.at("embed").d.data();
    for (int t = 0; t < T; ++t)
        if (std::memcmp(xin.data() + (size_t)t * H,
                        emb + (size_t)ids[t] * H,
                        (size_t)H * sizeof(float)) != 0)
            fail("input: embed lookup");
    // same token, different positions -> identical embedding rows
    for (int a = 0; a < T; ++a)
        for (int b = a + 1; b < T; ++b)
            if (ids[a] == ids[b] &&
                std::memcmp(xin.data() + (size_t)a * H,
                            xin.data() + (size_t)b * H,
                            (size_t)H * sizeof(float)) != 0)
                fail("input: repeat-token rows");
    // permuting ids permutes exactly those rows (content-only channel)
    {
        std::vector<int> sw = ids;
        std::swap(sw[0], sw[1]);
        Fwd fs;
        fwd(p, c, sw, fs);
        const std::vector<float>& xs = fs.layers[0].x_in;
        for (int t = 0; t < T; ++t)
            if (std::memcmp(xs.data() + (size_t)t * H,
                            emb + (size_t)sw[t] * H,
                            (size_t)H * sizeof(float)) != 0)
                fail("input: permuted lookup");
        if (std::memcmp(fw.logits.data(), fs.logits.data(),
                        (size_t)T * c.vocab * sizeof(float)) == 0)
            fail("input: order ignored (position not encoded)");
    }

    // (2) positional encoding: rotary carries relative order on q/k
    {
        const int TR = 16, hd = 16, nh = 1;
        const float theta = c.rope_theta;
        auto rot = [&](float* v) {
            rope(v, TR, nh, hd, theta, false);
        };
        auto score = [&](const float* q, const float* k, int t, int s) {
            return tpu_dot(q + (size_t)t * hd, k + (size_t)s * hd, hd);
        };
        // relative property: score(t,s) == score(t+d,s+d)
        {
            std::vector<float> q((size_t)TR * hd), k((size_t)TR * hd);
            for (int t = 0; t < TR; ++t)
                for (int i = 0; i < hd; ++i) {
                    q[(size_t)t * hd + i] =
                        std::sin(0.37f * i + 0.11f);
                    k[(size_t)t * hd + i] =
                        std::cos(0.29f * i + 0.23f);
                }
            rot(q.data()); rot(k.data());
            for (int d = -4; d <= 4; ++d)
                for (int t = 0; t < TR; ++t)
                    for (int s = 0; s < TR; ++s) {
                        int t2 = t + d, s2 = s + d;
                        if (t2 < 0 || s2 < 0 || t2 >= TR || s2 >= TR)
                            continue;
                        float a = score(q.data(), k.data(), t, s);
                        float b = score(q.data(), k.data(), t2, s2);
                        if (std::fabs(a - b) > 2e-4f)
                            fail("input: rope relativity");
                    }
            // non-degenerate: different offsets give different scores
            float s1 = score(q.data(), k.data(), 5, 5);
            float s2 = score(q.data(), k.data(), 5, 3);
            if (std::fabs(s1 - s2) < 1e-6f)
                fail("input: rope degenerate");
            // inverse rotation restores the raw vectors
            std::vector<float> qr((size_t)TR * hd);
            for (int t = 0; t < TR; ++t)
                for (int i = 0; i < hd; ++i)
                    qr[(size_t)t * hd + i] = 0.31f * i - 0.02f * t;
            std::vector<float> raw = qr;
            rope(qr.data(), TR, nh, hd, theta, false);
            rope(qr.data(), TR, nh, hd, theta, true);
            for (size_t i = 0; i < qr.size(); ++i)
                if (std::fabs(qr[i] - raw[i]) > 1e-5f)
                    fail("input: rope inverse");
        }
        // partial rotary: channels >= rd are untouched by the encoding
        {
            const int rd = 8;
            std::vector<float> v((size_t)TR * hd), raw;
            for (int t = 0; t < TR; ++t)
                for (int i = 0; i < hd; ++i)
                    v[(size_t)t * hd + i] = 0.13f * i + 0.07f * t;
            raw = v;
            rope_hf_partial(v.data(), TR, nh, hd, rd, theta, false);
            for (int t = 0; t < TR; ++t) {
                const float* r = v.data() + (size_t)t * hd;
                const float* w0 = raw.data() + (size_t)t * hd;
                for (int i = rd; i < hd; ++i)
                    if (r[i] != w0[i])
                        fail("input: partial rope tail");
                bool rotated = false;
                for (int i = 0; i < rd; ++i)
                    if (std::fabs(r[i] - w0[i]) > 1e-6f) rotated = true;
                if (t > 0 && !rotated)
                    fail("input: partial rope head");
            }
        }
    }

    // (3) parallel-input + position contract at model level: reversing
    // the sequence changes the outputs — order reaches the model through
    // the positional code, not through the embedding gather.
    {
        std::vector<int> rev = ids;
        std::reverse(rev.begin(), rev.end());
        Fwd fr;
        fwd(p, c, rev, fr);
        if (std::memcmp(fw.logits.data(), fr.logits.data(),
                        (size_t)T * c.vocab * sizeof(float)) == 0)
            fail("input: reversed order identical");
    }

    bool ok = failures == 0;
    std::printf("inputcheck: embed+position failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// ------------------------------------------------------------- gemmacheck --

// Gemma 4 26B A4B fusion probe. The config-gated signatures are verified
// executably on a miniature A4B topology: local sliding-window attention
// alternating with global attention (num_global_kv_heads + unified K==V),
// per-type RoPE (local full / global p-RoPE, independent base
// frequencies), post attention/FFW norms, GeGLU FFN, and the final logit
// softcap — plus a finite-difference spot check of the new backward
// paths (windowed score grads, wkv merge, post-norm, softcap chain).
static int gemmacheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 6;
    c.heads = 4; c.kv_heads = 2; c.max_pos = 64;
    c.qk_norm = true;
    c.moe_experts = 2; c.moe_top_k = 1; c.moe_layer_interval = 2;
    c.moe_expert_inter = 24; c.moe_shared_experts = 1;
    c.moe_shared_inter = 24; c.shared_expert_gate = true;
    // Gemma axis: (l+1)%3 != 0 -> local windowed; l=2,5 global.
    c.global_attn_interval = 3;
    c.sliding_window = 4;
    c.num_global_kv_heads = 1;
    c.k_eq_v_global = true;
    c.local_rope_proportion = 1.0f;
    c.global_rope_proportion = 0.25f;
    c.rope_theta_local = 10000.0f;
    c.rope_theta_global = 1000000.0f;
    c.final_logit_softcap = 30.0f;
    c.post_attn_norm = true;
    c.post_ffw_norm = true;
    c.ffn_act = 1;
    Params p;
    init_params(p, c, 31);
    std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
    const int T = (int)ids.size();
    const int H = c.hidden, hd = H / c.heads;
    Fwd fw;
    fwd(p, c, ids, fw);

    // (1) local layers: window mask — zero outside the last W, sum 1.
    //     global layers: reach beyond the window must be non-degenerate.
    for (int l = 0; l < c.layers; ++l) {
        const LayerCache& L = fw.layers[(size_t)l];
        const int W = c.sliding_window;
        if (c.is_local_attn(l)) {
            for (int h = 0; h < c.heads; ++h)
                for (int t = 0; t < T; ++t) {
                    const float* pr =
                        L.probs.data() + ((size_t)h * T + t) * T;
                    float s = 0.0f;
                    for (int u = 0; u < T; ++u) {
                        int lo = std::max(0, t - W + 1);
                        if ((u < lo || u > t) && pr[u] != 0.0f)
                            fail("gemma: local window leak");
                        if (u >= lo && u <= t) s += pr[u];
                    }
                    if (std::fabs(s - 1.0f) > 1e-4f)
                        fail("gemma: local prob row sum");
                }
        } else {
            // global reach: some row t>=W attends past the window
            bool reach = false;
            for (int h = 0; h < c.heads && !reach; ++h)
                for (int t = c.sliding_window; t < T && !reach; ++t) {
                    const float* pr =
                        L.probs.data() + ((size_t)h * T + t) * T;
                    for (int u = 0; u <= t - c.sliding_window; ++u)
                        if (pr[u] > 0.0f) { reach = true; break; }
                }
            if (!reach) fail("gemma: global reach degenerate");
        }
    }

    // (2) unified K==V on global layers; per-layer kv dims
    for (int l = 0; l < c.layers; ++l) {
        const LayerCache& L = fw.layers[(size_t)l];
        const int kvh = c.kv_heads_at(l);
        if (L.k.size() != (size_t)T * kvh * hd)
            fail("gemma: kv dim per layer");
        if (c.kv_unified(l)) {
            // one projection: v IS the raw shared tensor; k takes the
            // scoring transforms (qk_norm + rope) on top of the same
            // values — qk_kraw (pre-norm cache) must equal v bitwise.
            if (!L.qk_kraw.empty() &&
                std::memcmp(L.qk_kraw.data(), L.v.data(),
                            L.v.size() * sizeof(float)) != 0)
                fail("gemma: k_eq_v not unified");
            if (!p.w.count(ln(l, "wkv")) || p.w.count(ln(l, "wv")))
                fail("gemma: wkv params");
        } else {
            if (std::memcmp(L.k.data(), L.v.data(),
                            L.k.size() * sizeof(float)) == 0)
                fail("gemma: non-global kv unexpectedly unified");
        }
    }

    // (3) post norms: rmsnorm(attn_proj) feeds the residual; same for FFN
    for (int l = 0; l < c.layers; ++l) {
        const LayerCache& L = fw.layers[(size_t)l];
        if (L.post_attn.size() != (size_t)T * H ||
            L.post_attn_rms.size() != (size_t)T)
            fail("gemma: post_attn cache");
        for (float r : L.post_attn_rms)
            if (!(r > 0.0f && std::isfinite(r)))
                fail("gemma: post_attn rms");
        for (size_t i = 0; i < (size_t)T * H; ++i)
            if (L.x_res[i] != L.x_in[i] + L.post_attn[i])
                fail("gemma: post_attn sandwich");
        if (L.post_ffn.size() != (size_t)T * H)
            fail("gemma: post_ffn cache");
        if (l + 1 < c.layers) {
            const std::vector<float>& nx = fw.layers[(size_t)l + 1].x_in;
            for (size_t i = 0; i < (size_t)T * H; ++i)
                if (nx[i] != L.x_res[i] + L.post_ffn[i])
                    fail("gemma: post_ffn sandwich");
        }
    }

    // (4) final logit softcap bound
    for (float x : fw.logits)
        if (std::fabs(x) > c.final_logit_softcap + 1e-4f)
            fail("gemma: softcap bound");

    // (5) GeGLU: dense FFN (layer 1) and an MoE expert slot use gelu_tanh
    {
        const LayerCache& L1 = fw.layers[1];
        if (!L1.fh.empty())
            for (size_t i = 0; i < L1.fh.size(); ++i)
                if (std::fabs(L1.fh[i] -
                              gelu_tanh_f(L1.fa[i]) * L1.fb[i]) > 1e-5f)
                    fail("gemma: dense GeGLU");
        bool saw_slot = false;
        for (int l = 0; l < c.layers; ++l) {
            const LayerCache& L = fw.layers[(size_t)l];
            for (size_t s = 0; s < L.mfh.size() && !saw_slot; ++s)
                if (!L.mfh[s].empty()) {
                    saw_slot = true;
                    for (size_t i = 0; i < L.mfh[s].size(); ++i)
                        if (std::fabs(L.mfh[s][i] -
                                      gelu_tanh_f(L.mfa[s][i]) *
                                          L.mfb[s][i]) > 1e-5f)
                            fail("gemma: expert GeGLU");
                }
        }
        if (!saw_slot) fail("gemma: expert GeGLU coverage");
    }

    // (6) finite-difference spot check across the new backward paths:
    //     wkv merge, local windowed scores, post norms, softcap chain,
    //     GeGLU expert weights.
    {
        std::vector<int> lab = ids;
        shift_labels(lab);
        auto loss_of = [&]() {
            Fwd f;
            fwd(p, c, ids, f);
            std::vector<float> dl;
            return (double)ce_loss(f.logits, lab, T, c.vocab, dl) +
                   f.moe_aux + f.csa_idx;
        };
        p.zero_grad();
        Fwd f0;
        fwd(p, c, ids, f0);
        std::vector<float> dl;
        ce_loss(f0.logits, lab, T, c.vocab, dl);
        bwd(p, c, ids, f0, dl, 1.0f);
        const double eps = 4e-3;
        for (const char* key : {"layers.2.wkv", "layers.0.wk",
                                "layers.0.norm_attn_out",
                                "layers.5.norm_ffw_out",
                                "layers.0.experts.0.w1", "lm_head"}) {
            Tensor& w = p.w[key];
            Tensor& g = p.g[key];
            size_t stride = w.d.size() > 4 ? w.d.size() / 4 : 1;
            for (size_t i = 0; i < w.d.size(); i += stride) {
                float orig = w.d[i];
                w.d[i] = orig + (float)eps;
                double lp = loss_of();
                w.d[i] = orig - (float)eps;
                double lm = loss_of();
                w.d[i] = orig;
                double num = (lp - lm) / (2.0 * eps);
                double ana = g.d[i];
                double abs_err = std::fabs(num - ana);
                double rel = abs_err / std::max(1e-4, std::fabs(num));
                if (rel > 0.10 && abs_err > 3e-3) {
                    ++failures;
                    std::printf("  FAIL fdiff %s[%zu]: ana=%.6f num=%.6f\n",
                                key, i, ana, num);
                }
            }
        }
    }

    bool ok = failures == 0;
    std::printf("gemmacheck: gemma-a4b failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// -------------------------------------------------------------- dsvcheck --
//
// DeepSeek V4-Pro signatures — executable evidence:
//   MLA: kv latent reconstruction (normed c drives per-head up
//        projections bitwise), shared decoupled rope key (one w_kr
//        perturbation moves every head while a per-head w_uk slice
//        moves only its own), causal + sliding-window masks still hold;
//   aux-free LB: bias ranks selection (s+b) yet never enters the
//        weights — flipping a bias re-routes a token while its router
//        scores stay bitwise identical; the sign rule moves load toward
//        under-served experts; lb_bias is outside the optimizer;
//   MTP: depth-1 module predicts ids[i+2] via shared embed/lm_head and
//        the hidden states — labels, finite logits and gradients on the
//        shared tensors + a finite-difference sweep over the new
//        backward paths;
//   XCN7: checkpoint round-trip preserves the whole axis.
static int dsvcheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 6;
    c.heads = 4; c.kv_heads = 2; c.max_pos = 64;
    // MLA on every attention layer; Gemma local/global split gives
    // windowed-MLA layers (l0,1,3,4 local / l2,5 global).
    c.global_attn_interval = 3;
    c.sliding_window = 4;
    c.kv_lora_rank = 8;
    c.q_lora_rank = 8;
    c.qk_nope_head_dim = 8;
    c.qk_rope_head_dim = 8;
    // aux-free balance over the v28 sigmoid router (V3's combo).
    c.moe_experts = 4; c.moe_top_k = 2; c.moe_layer_interval = 3;
    c.moe_expert_inter = 24; c.moe_router_sigmoid = true;
    c.moe_auxfree_balance = true; c.moe_lb_bias_rate = 0.01f;
    c.mtp_num_layers = 1; c.mtp_loss_weight = 0.3f;
    Params p;
    init_params(p, c, 37);
    std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
    const int T = (int)ids.size();
    const int H = c.hidden, hd = H / c.heads;
    const int kn = c.qk_nope_head_dim, kr = c.qk_rope_head_dim;
    Fwd fw;
    fwd(p, c, ids, fw);

    // (1) MLA latent + masks
    for (int l = 0; l < c.layers; ++l) {
        const LayerCache& L = fw.layers[(size_t)l];
        if (L.mla_ckv.size() != (size_t)T * c.kv_lora_rank ||
            L.mla_kn.size() != (size_t)T * c.heads * kn ||
            L.mla_kr.size() != (size_t)T * kr ||
            L.mla_cq.size() != (size_t)T * c.q_lora_rank)
            fail("dsv: mla latent dims");
        // latent norm identity: rmsnorm(raw) == cached normed
        {
            std::vector<float> re((size_t)T * c.kv_lora_rank),
                rr((size_t)T);
            rmsnorm_fwd(L.mla_ckv_raw.data(),
                        p.w.at(ln(l, "norm_kvl")).d.data(), re.data(),
                        rr.data(), T, c.kv_lora_rank, c.rms_eps);
            if (std::memcmp(re.data(), L.mla_ckv.data(),
                            re.size() * sizeof(float)) != 0)
                fail("dsv: mla latent norm");
        }
        // up-projection reconstruction: W_uk @ ckv == kn bitwise
        {
            std::vector<float> re((size_t)T * c.heads * kn);
            linear_fwd(L.mla_ckv.data(), p.w.at(ln(l, "w_uk")), re.data(),
                       T, c.kv_lora_rank, c.heads * kn);
            if (std::memcmp(re.data(), L.mla_kn.data(),
                            re.size() * sizeof(float)) != 0)
                fail("dsv: mla up-proj");
        }
        const int W = c.sliding_window;
        for (int h = 0; h < c.heads; ++h)
            for (int t = 0; t < T; ++t) {
                const float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                float s = 0.0f;
                const int lo = c.is_local_attn(l)
                                   ? std::max(0, t - W + 1) : 0;
                for (int u = 0; u < T; ++u) {
                    if ((u < lo || u > t) && pr[u] != 0.0f)
                        fail("dsv: mla mask leak");
                    if (u >= lo && u <= t) s += pr[u];
                }
                if (std::fabs(s - 1.0f) > 1e-4f)
                    fail("dsv: mla prob row sum");
            }
    }
    // shared rope key: negating w_kr must move every head's probs;
    // negating one head's w_uk slice moves only that head.
    {
        Params p2 = p;
        for (auto& x : p2.w[ln(0, "w_kr")].d) x = -x;
        Fwd f2; fwd(p2, c, ids, f2);
        const LayerCache& a = fw.layers[0], &b = f2.layers[0];
        for (int h = 0; h < c.heads; ++h)
            if (std::memcmp(a.probs.data() + (size_t)h * T * T,
                            b.probs.data() + (size_t)h * T * T,
                            (size_t)T * T * sizeof(float)) == 0)
                fail("dsv: shared rope-k reach");
        Params p3 = p;
        Tensor& uk = p3.w[ln(0, "w_uk")];
        for (int i = 0; i < kn; ++i) uk.d[(size_t)i] = -uk.d[(size_t)i];
        Fwd f3; fwd(p3, c, ids, f3);
        const LayerCache& b3 = f3.layers[0];
        if (std::memcmp(a.probs.data(), b3.probs.data(),
                        (size_t)T * T * sizeof(float)) == 0)
            fail("dsv: head0 w_uk slice");
        for (int h = 1; h < c.heads; ++h)
            if (std::memcmp(a.probs.data() + (size_t)h * T * T,
                            b3.probs.data() + (size_t)h * T * T,
                            (size_t)T * T * sizeof(float)) != 0)
                fail("dsv: w_uk isolation");
    }

    // (2) aux-free balance: bias re-routes selection without touching
    //     router scores or combination weights.
    {
        const int ml = 0;                 // l0 is moe (interval 3)
        const LayerCache& L = fw.layers[ml];
        const int E = c.moe_experts, K = c.moe_top_k;
        int victim = -1, rival = -1;
        for (int e = 0; e < E; ++e) {
            bool sel = false;
            for (int s = 0; s < K; ++s)
                if (L.moe_idx[(size_t)s] == e) sel = true;
            if (!sel) { victim = e; break; }
        }
        for (int e = 0; e < E; ++e)
            if (e != victim && rival < 0) rival = e;
        if (victim >= 0) {
            Params p4 = p;
            p4.w[ln(ml, "lb_bias")].d[(size_t)victim] = 1e3f;
            Fwd f4; fwd(p4, c, ids, f4);
            const LayerCache& L4 = f4.layers[ml];
            bool routed = false;
            for (int s = 0; s < K; ++s)
                if (L4.moe_idx[(size_t)s] == victim) routed = true;
            if (!routed) fail("dsv: lb bias re-route");
            if (std::memcmp(L.gate_probs.data(), L4.gate_probs.data(),
                            L.gate_probs.size() * sizeof(float)) != 0)
                fail("dsv: lb bias touched scores");
            if (routed) {
                float s = 0.0f;
                for (int k = 0; k < K; ++k) s += L4.moe_w[(size_t)k];
                if (std::fabs(s - 1.0f) > 1e-4f)
                    fail("dsv: moe weight renorm");
            }
        } else fail("dsv: lb probe degenerate");
        // sign rule: bias must match u * sign(mean - count)
        Params p5 = p;
        lb_bias_step(p5, c, fw);
        const Tensor& bb = p5.w[ln(ml, "lb_bias")];
        std::vector<float> cnt((size_t)E, 0.0f);
        for (int e : L.moe_idx) cnt[(size_t)e] += 1.0f;
        const float mean = (float)(T * K) / (float)E;
        for (int e = 0; e < E; ++e) {
            float err = mean - cnt[(size_t)e];
            float want = c.moe_lb_bias_rate *
                         (err > 0.0f ? 1.0f : err < 0.0f ? -1.0f : 0.0f);
            if (bb.d[(size_t)e] != want)
                fail("dsv: lb sign rule");
        }
        // no aux gradient term under aux-free mode
        if (fw.moe_aux != 0.0f) fail("dsv: auxfree aux leak");
    }

    // (3) MTP: labels t+2, finite logits, shared-tensor grads.
    {
        const MtpCache& M = fw.mtp;
        if (!M.on || M.logits.size() != (size_t)T * c.vocab)
            fail("dsv: mtp shape");
        for (int i = 0; i < T; ++i) {
            int want = i + 2 < T ? ids[(size_t)i + 2] : -100;
            if (M.lab[(size_t)i] != want) fail("dsv: mtp labels");
        }
        for (float x : M.logits)
            if (!std::isfinite(x)) fail("dsv: mtp logits");
        if (!(M.loss > 0.0f && std::isfinite(M.loss)))
            fail("dsv: mtp loss");
        std::vector<int> lab = ids;
        shift_labels(lab);
        p.zero_grad();
        Fwd f0; fwd(p, c, ids, f0);
        std::vector<float> dl;
        ce_loss(f0.logits, lab, T, c.vocab, dl);
        bwd(p, c, ids, f0, dl, 1.0f);
        auto gnorm = [&](const char* k) {
            double s = 0.0;
            for (float x : p.g[k].d) s += (double)x * x;
            return s;
        };
        if (!(gnorm("mtp.w_proj") > 0.0)) fail("dsv: mtp w_proj grad");
        if (!(gnorm("mtp.norm_h") > 0.0)) fail("dsv: mtp norm_h grad");
        if (!(gnorm("embed") > 0.0)) fail("dsv: shared embed grad");
        if (!(gnorm("lm_head") > 0.0)) fail("dsv: shared lm_head grad");
        // lb_bias is outside the optimizer graph
        for (int l = 0; l < c.layers; ++l)
            if (c.moe_experts > 0 && (l % c.moe_layer_interval == 0))
                if (gnorm(ln(l, "lb_bias").c_str()) != 0.0)
                    fail("dsv: lb_bias grad leak");
    }

    // (4) finite-difference sweep over the new backward paths: MLA
    //     latent chain (both directions), shared rope key, low-rank q,
    //     MTP block + shared embed. lb_bias skipped — piecewise.
    {
        std::vector<int> lab = ids;
        shift_labels(lab);
        auto loss_of = [&]() {
            Fwd f;
            fwd(p, c, ids, f);
            std::vector<float> dl;
            return (double)ce_loss(f.logits, lab, T, c.vocab, dl) +
                   f.moe_aux + f.mtp.loss;
        };
        p.zero_grad();
        Fwd f0; fwd(p, c, ids, f0);
        std::vector<float> dl;
        ce_loss(f0.logits, lab, T, c.vocab, dl);
        bwd(p, c, ids, f0, dl, 1.0f);
        const double eps = 4e-3;
        for (const char* key : {"layers.0.w_dkv", "layers.0.w_uk",
                                "layers.0.w_kr", "layers.0.w_dq",
                                "layers.2.norm_kvl", "layers.2.w_uv",
                                "mtp.w_proj", "mtp.wq", "mtp.norm_h",
                                "embed", "lm_head"}) {
            Tensor& w = p.w[key];
            Tensor& g = p.g[key];
            size_t stride = w.d.size() > 4 ? w.d.size() / 4 : 1;
            for (size_t i = 0; i < w.d.size(); i += stride) {
                float orig = w.d[i];
                w.d[i] = orig + (float)eps;
                double lp = loss_of();
                w.d[i] = orig - (float)eps;
                double lm = loss_of();
                w.d[i] = orig;
                double num = (lp - lm) / (2.0 * eps);
                double ana = g.d[i];
                double abs_err = std::fabs(num - ana);
                double rel = abs_err / std::max(1e-4, std::fabs(num));
                if (rel > 0.10 && abs_err > 3e-3) {
                    ++failures;
                    std::printf("  FAIL fdiff %s[%zu]: ana=%.6f num=%.6f\n",
                                key, i, ana, num);
                }
            }
        }
    }

    // (5) XCN7 round-trip: config + every tensor (incl. lb_bias/mtp).
    {
        const char* tmp = "_dsvcheck_tmp.xcn";
        if (!ckpt_save(p, c, tmp, /*overwrite*/true))
            fail("dsv: ckpt save");
        ModelConfig c2;
        if (!ckpt_peek_config(tmp, c2))
            fail("dsv: ckpt peek");
        Params p2;
        init_params(p2, c2, 0);          // tensor table needs the names
        if (!ckpt_load(p2, c2, tmp))
            fail("dsv: ckpt load");
        if (c2.kv_lora_rank != c.kv_lora_rank ||
            c2.q_lora_rank != c.q_lora_rank ||
            c2.qk_nope_head_dim != c.qk_nope_head_dim ||
            c2.qk_rope_head_dim != c.qk_rope_head_dim ||
            c2.moe_auxfree_balance != c.moe_auxfree_balance ||
            c2.moe_lb_bias_rate != c.moe_lb_bias_rate ||
            c2.mtp_num_layers != c.mtp_num_layers ||
            c2.mtp_loss_weight != c.mtp_loss_weight)
            fail("dsv: ckpt config");
        for (auto& n : p.order)
            if (!p2.w.count(n) ||
                std::memcmp(p.w[n].d.data(), p2.w[n].d.data(),
                            p.w[n].d.size() * sizeof(float)) != 0)
                fail("dsv: ckpt tensor");
        std::remove(tmp);
    }

    bool ok = failures == 0;
    std::printf("dsvcheck: deepseek-v4 failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// ---------------------------------------------------------- freezecheck --
//
// ParameterFreezeMap probe (300M §41-§44): patterns resolve at
// alloc_adam, frozen params get no Adam moments, adamw_step leaves them
// byte-identical while trainable params move, dormant routed experts
// (zero grad) are skipped, and the run_job report exposes the §45
// parameter-efficiency fields.
static int freezecheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.full_attention_interval = 2;
    c.attn_output_gate = true; c.qk_norm = true; c.partial_rotary = 0.5f;
    c.lin_key_heads = 1; c.lin_key_dim = 32;
    c.lin_value_heads = 2; c.lin_value_dim = 32; c.lin_conv_kernel = 4;
    c.moe_experts = 4; c.moe_top_k = 1; c.moe_layer_interval = 1;
    c.moe_expert_inter = 24; c.moe_shared_experts = 1;
    c.moe_shared_inter = 24; c.shared_expert_gate = true;

    // ---- 1. resolution + sparse optimizer allocation ---------------------
    Params p;
    p.freeze_patterns = {"embed", "*.experts.*"};
    init_params(p, c, 31);
    if (p.frozen.empty()) fail("freeze set empty");
    if (!p.is_frozen("embed")) fail("exact name not frozen");
    if (!p.is_frozen(ln(0, "experts.0.w1")))
        fail("wildcard pattern not frozen");
    if (p.is_frozen("lm_head")) fail("non-matching name frozen");
    for (auto& n : p.order) {
        bool frz = p.frozen.count(n) != 0;
        if (frz != (p.m.count(n) == 0))
            fail("frozen/moment mismatch");
        if (frz && (p.m.count(n) || p.v.count(n)))
            fail("frozen param allocated Adam state");
        if (!frz && (!p.m.count(n) || !p.v.count(n)))
            fail("trainable param missing Adam state");
    }
    const int64_t total = p.trainable_params() + p.frozen_params();
    int64_t wsum = 0;
    for (auto& n : p.order) wsum += p.w[n].numel();
    if (total != wsum) fail("trainable+frozen != total");
    if (p.frozen_params() <= 0 || p.trainable_params() <= 0)
        fail("degenerate freeze split");

    // ---- 2. frozen weights byte-identical after real optimizer steps -----
    {
        std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
        std::vector<int> lab = ids; shift_labels(lab);
        std::unordered_map<std::string, std::vector<float>> snap;
        for (auto& n : p.order)
            if (p.frozen.count(n)) snap[n] = p.w[n].d;
        std::vector<float> trainable_before = p.w["lm_head"].d;
        for (int s = 0; s < 4; ++s) {
            p.zero_grad();
            Fwd f;
            fwd(p, c, ids, f);
            std::vector<float> dl;
            ce_loss(f.logits, lab, (int)ids.size(), c.vocab, dl);
            bwd(p, c, ids, f, dl, 1.0f);
            adamw_step(p, 1.0f, 0.05f, 0.01f, s);
        }
        for (auto& n : p.order) {
            if (!p.frozen.count(n)) continue;
            const std::vector<float>& a = p.w[n].d;
            const std::vector<float>& b = snap[n];
            if (a.size() != b.size() ||
                std::memcmp(a.data(), b.data(),
                            a.size() * sizeof(float)) != 0)
                fail("frozen weight changed");
        }
        bool moved = false;
        for (size_t i = 0; i < trainable_before.size(); ++i)
            if (p.w["lm_head"].d[i] != trainable_before[i]) moved = true;
        if (!moved) fail("trainable weight did not move");
    }

    // ---- 3. §44 dormant routed expert skipped (dense wd still applies) --
    {
        Params p3;
        init_params(p3, c, 37);
        p3.zero_grad();
        const std::string ex = ln(0, "experts.0.w1");
        if (p3.frozen.count(ex)) fail("unexpected freeze in leg 3");
        std::vector<float> exb = p3.w[ex].d;
        std::vector<float> lmb = p3.w["lm_head"].d;
        adamw_step(p3, 1.0f, 0.01f, 0.5f, 0);
        if (std::memcmp(p3.w[ex].d.data(), exb.data(),
                        exb.size() * sizeof(float)) != 0)
            fail("dormant expert updated");
        bool dec = false;
        for (size_t i = 0; i < lmb.size(); ++i)
            if (p3.w["lm_head"].d[i] != lmb[i]) dec = true;
        if (!dec) fail("dense decoupled wd lost");
    }

    // ---- 4. job plumbing + §45 report fields ------------------------------
    std::string tmp = "_xct_freeze_data.jsonl";
    {
        std::ofstream f(tmp, std::ios::trunc);
        std::mt19937 rng(11);
        std::uniform_int_distribution<int> tok(3, 63);
        for (int i = 0; i < 8; ++i) {
            f << "{\"input_ids\":[";
            for (int t = 0; t < 12; ++t) f << (t ? "," : "") << tok(rng);
            f << "]}\n";
        }
    }
    std::string job = std::string(R"({
        "task":"sft",
        "model":{"vocab_size":64,"hidden_size":32,"intermediate_size":64,
                 "num_hidden_layers":2,"num_attention_heads":2,
                 "num_key_value_heads":1,"max_position_embeddings":32,
                 "moe_num_experts":4,"moe_top_k":1,
                 "moe_expert_intermediate_size":24,
                 "moe_num_shared_experts":1,
                 "moe_shared_intermediate_size":24},
        "train":{"lr":0.05,"max_steps":8,"grad_clip":1.0,"warmup_steps":0,
                 "lr_decay":"constant","seed":7,
                 "freeze":["embed","*.experts.*"]},
        "data":{"path":")") + tmp + R"(","format":"sft","max_rows":8,"max_len":12}
    })";
    JsonValue r = run_job(JsonParser(job).parse());
    std::remove(tmp.c_str());
    if (!r.get("params_finite")->boolean) fail("job non-finite");
    if (!r.get("trainable_params") || !r.get("frozen_params"))
        fail("report missing efficiency fields");
    else {
        double tr = r.get("trainable_params")->number;
        double fr = r.get("frozen_params")->number;
        if (!(tr > 0.0) || !(fr > 0.0))
            fail("report efficiency fields degenerate");
    }
    if (!r.get("freeze_patterns"))
        fail("report missing freeze_patterns echo");

    bool ok = failures == 0;
    std::printf("freezecheck: ParameterFreezeMap failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}
