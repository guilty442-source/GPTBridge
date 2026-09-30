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
};

static double now_s() {
    return std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
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
    int max_rows = j_int(dj, "max_rows", 10000);
    int max_len = j_int(dj, "max_len", c.max_pos);

    Params p;
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
    int64_t grpo_rollouts = 0;
    double grpo_reward_sum = 0.0, grpo_kl_sum = 0.0;

    while (step < tc.max_steps) {
        for (auto& ex : data) {
            if (step >= tc.max_steps) break;
            if (tc.deadline_s > 0 && now_s() - t0 > tc.deadline_s) {
                deadline_hit = true; break;
            }
            p.zero_grad();
            float loss = 0.0f;
            if (task == "dpo") {
                // policy chosen
                fw.layers.clear(); fw.moe_aux = 0.0f;
                fwd(p, c, ex.ids, fw);
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
                        fw.layers.clear(); fw.moe_aux = 0.0f;
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
                    fw.layers.clear(); fw.moe_aux = 0.0f;
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
                fw.layers.clear(); fw.moe_aux = 0.0f;
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
                    loss = ce_loss(fw.logits, vlab, T, c.vocab, dlogits)
                           + fw.moe_aux;
                    bwd(p, c, ex.ids, fw, dlogits, 1.0f, &ex.vision);
                } else {
                    fwd(p, c, ex.ids, fw);
                    loss = ce_loss(fw.logits, lab, (int)ex.ids.size(), c.vocab, dlogits)
                           + fw.moe_aux;
                    bwd(p, c, ex.ids, fw, dlogits, 1.0f);
                }
            }
            // grad clip (global norm)
            double gnorm = 0.0f;
            for (auto& n : p.order)
                for (float x : p.g[n].d) gnorm += (double)x * x;
            gnorm = std::sqrt(gnorm);
            float gscale = (tc.clip > 0 && gnorm > tc.clip) ? tc.clip / (float)gnorm : 1.0f;
            // adamw
            float lr_t = tc.lr;
            if (tc.warmup > 0 && step < tc.warmup) lr_t *= (float)(step + 1) / tc.warmup;
            else if (tc.decay == "cosine" && tc.max_steps > tc.warmup) {
                float pr = (float)(step - tc.warmup) / (tc.max_steps - tc.warmup);
                lr_t *= 0.5f * (1.0f + std::cos(3.14159265f * std::min(1.0f, pr)));
            }
            float b1 = 0.9f, b2 = 0.999f, eps = 1e-8f;
            float bc1 = 1.0f - std::pow(b1, step + 1), bc2 = 1.0f - std::pow(b2, step + 1);
            for (auto& n : p.order) {
                Tensor& w = p.w[n]; Tensor& g = p.g[n];
                Tensor& m = p.m[n]; Tensor& v = p.v[n];
                for (size_t i = 0; i < w.d.size(); ++i) {
                    float gi = g.d[i] * gscale;
                    m.d[i] = b1 * m.d[i] + (1 - b1) * gi;
                    v.d[i] = b2 * v.d[i] + (1 - b2) * gi * gi;
                    float mh = m.d[i] / bc1, vh = v.d[i] / bc2;
                    w.d[i] -= lr_t * (mh / (std::sqrt(vh) + eps) + tc.wd * w.d[i]);
                }
            }
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
                               dl) + fw.moe_aux;
    };
    p.zero_grad();
    Fwd fw;
    fwd(p, c, ids, fw, &vpatches, VP);
    std::vector<float> dl;
    double loss0 = ce_loss(fw.logits, vlabels, VT, c.vocab, dl)
                   + fw.moe_aux;
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
