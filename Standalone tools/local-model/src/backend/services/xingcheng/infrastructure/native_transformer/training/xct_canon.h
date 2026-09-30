// xct_canon.h — B94 fragment of xingcheng_trainer.cpp (xc-fused-1 probe).
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_csa.h.
//
// --canoncheck: the canonical-generation contract (convergence phase).
// "xc-fused-1" is the single canonical architecture profile: every
// mechanism axis is pinned by the generation name, so a job that
// declares it resolves to exactly one ModelConfig — never a silent
// divergence. The probe asserts:
//   pins      — interval-4 deltanet interleave, gated+qk_norm+partial-
//               rotary full attention, sigmoid MoE top-2 with shared
//               expert + aux 0.001, vision early fusion, YaRN >=2,
//               MTP stack depth>=1 weight>=0.1
//   exclusion — CSA, MLA, Gemma4, aux-free lb_bias and kv-sharing are
//               canonically absent (fields forced off, not advisory)
//   unique    — two parses of the same manifest are field-identical;
//               conflicting job fields are overridden, not merged
//   execute   — synthetic forward+backward produce finite
//               logits/gradients through every pinned axis
//   ckpt      — saves land as XCN10 with gemma4 marker 0, round-trip
//               bit-identical, and the file bytes are deterministic
#pragma once

static bool canon_cfg_eq(const ModelConfig& a, const ModelConfig& b) {
    return a.vocab == b.vocab && a.hidden == b.hidden &&
           a.inter == b.inter && a.layers == b.layers &&
           a.heads == b.heads && a.kv_heads == b.kv_heads &&
           a.max_pos == b.max_pos &&
           a.full_attention_interval == b.full_attention_interval &&
           a.attn_output_gate == b.attn_output_gate &&
           a.qk_norm == b.qk_norm &&
           a.partial_rotary == b.partial_rotary &&
           a.lin_key_heads == b.lin_key_heads &&
           a.lin_key_dim == b.lin_key_dim &&
           a.lin_value_heads == b.lin_value_heads &&
           a.lin_value_dim == b.lin_value_dim &&
           a.lin_conv_kernel == b.lin_conv_kernel &&
           a.moe_experts == b.moe_experts &&
           a.moe_top_k == b.moe_top_k &&
           a.moe_layer_interval == b.moe_layer_interval &&
           a.moe_router_sigmoid == b.moe_router_sigmoid &&
           a.moe_shared_experts == b.moe_shared_experts &&
           a.shared_expert_gate == b.shared_expert_gate &&
           a.moe_aux_w == b.moe_aux_w &&
           a.moe_auxfree_balance == b.moe_auxfree_balance &&
           a.moe_zloss_w == b.moe_zloss_w &&
           a.kv_lora_rank == b.kv_lora_rank &&
           a.q_lora_rank == b.q_lora_rank &&
           a.mtp_depth == b.mtp_depth && a.mtp_loss_w == b.mtp_loss_w &&
           a.mtp_num_layers == b.mtp_num_layers &&
           a.yarn_factor == b.yarn_factor &&
           a.yarn_orig_pos == b.yarn_orig_pos &&
           a.yarn_beta_fast == b.yarn_beta_fast &&
           a.yarn_beta_slow == b.yarn_beta_slow &&
           a.yarn_attn_factor == b.yarn_attn_factor &&
           a.use_vision == b.use_vision &&
           a.vision_patch_dim == b.vision_patch_dim &&
           a.vision_max_patches == b.vision_max_patches &&
           a.csa_ratio == b.csa_ratio && a.csa_topk == b.csa_topk &&
           a.csa_window == b.csa_window &&
           a.global_attn_interval == b.global_attn_interval &&
           a.sliding_window == b.sliding_window &&
           a.num_global_kv_heads == b.num_global_kv_heads &&
           a.k_eq_v_global == b.k_eq_v_global &&
           a.num_kv_shared_layers == b.num_kv_shared_layers &&
           a.model_type == b.model_type &&
           a.layer_types == b.layer_types;
}

static int canoncheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };

    // ---- pins: the manifest declares only the generation + sizes ----
    JsonValue mj = JsonParser(
        "{\"generation\":\"xc-fused-1\",\"vocab_size\":96,"
        "\"hidden_size\":32,\"intermediate_size\":48,"
        "\"num_hidden_layers\":8,\"num_attention_heads\":4,"
        "\"num_key_value_heads\":2,\"max_position_embeddings\":64,"
        // conflicting non-canonical axes — must be overridden
        "\"moe_router_sigmoid\":false,\"attn_output_gate\":false,"
        "\"qk_norm\":false,\"partial_rotary_factor\":1.0,"
        "\"use_vision\":false,\"moe_num_experts\":0,"
        "\"kv_lora_rank\":16,\"csa_compress_ratio\":4,"
        "\"moe_auxfree_balance\":true,"
        "\"num_kv_shared_layers\":2,\"k_eq_v_global\":true}").parse();
    ModelConfig c;
    try {
        c = parse_model(&mj);
    } catch (const char* e) {
        std::printf("  FAIL parse: %s\n", e);
        return 1;
    } catch (...) {
        fail("parse: threw");
        return 1;
    }
    if (c.full_attention_interval != 4) fail("pins: interval != 4");
    if (c.lin_key_heads <= 0 || c.lin_key_dim <= 0 ||
        c.lin_value_heads <= 0 || c.lin_value_dim <= 0)
        fail("pins: deltanet geometry unset");
    {
        int lin = 0, attn = 0;
        for (int l = 0; l < c.layers; ++l)
            (c.is_linear(l) ? lin : attn)++;
        if (lin != 6 || attn != 2) fail("pins: layer split != 6/2");
    }
    if (!c.attn_output_gate || !c.qk_norm ||
        c.partial_rotary != 0.5f)
        fail("pins: gated+normed+partial attention");
    if (!c.use_yarn() || c.yarn_factor < 2.0f)
        fail("pins: yarn factor < 2");
    if (!c.moe_router_sigmoid || c.moe_top_k != 2 ||
        c.moe_experts < 8 || c.moe_layer_interval != 1 ||
        c.moe_shared_experts < 1 || !c.shared_expert_gate ||
        c.moe_aux_w != 0.001f)
        fail("pins: MoE contract");
    if (!c.use_vision || c.vision_patch_dim < 16 ||
        c.vision_max_patches < 64)
        fail("pins: vision contract");
    if (c.mtp_depth < 1 || c.mtp_loss_w < 0.1f)
        fail("pins: mtp stack");
    // ---- exclusion: canonical-absent axes stay off ----
    if (c.csa_ratio != 0 || c.csa_topk != 0 || c.csa_window != 0)
        fail("exclusion: csa");
    if (c.kv_lora_rank != 0 || c.q_lora_rank != 0 ||
        c.qk_nope_head_dim != 0 || c.qk_rope_head_dim != 0)
        fail("exclusion: mla");
    if (c.is_gemma4() || !c.layer_types.empty() ||
        c.num_kv_shared_layers != 0)
        fail("exclusion: gemma4/kv-sharing");
    if (c.moe_auxfree_balance || c.moe_lb_bias_rate != 0.0f)
        fail("exclusion: aux-free balance");
    if (c.k_eq_v_global || c.num_global_kv_heads != 0)
        fail("exclusion: k_eq_v/global kv");

    // ---- unique: re-parse is field-identical ----
    try {
        ModelConfig c2 = parse_model(&mj);
        if (!canon_cfg_eq(c, c2)) fail("unique: re-parse differs");
    } catch (...) { fail("unique: re-parse threw"); }

    // ---- execute: fwd/bwd through every pinned axis ---------------
    Params p;
    init_params(p, c, 7);
    const int T = 12, VP = 4;
    std::vector<int> ids(T);
    for (int t = 0; t < T; ++t) ids[t] = 3 + (t * 7) % (c.vocab - 4);
    std::vector<float> vp((size_t)VP * c.vision_patch_dim);
    for (size_t i = 0; i < vp.size(); ++i)
        vp[i] = 0.01f * (float)((int)(i % 13) - 6);
    Fwd o;
    try {
        fwd(p, c, ids, o, &vp, VP);
    } catch (...) { fail("exec: fwd threw"); }
    bool fin = true;
    for (float x : o.logits) if (!std::isfinite(x)) fin = false;
    if (!fin) fail("exec: non-finite logits");
    if (o.mtp_stack.empty()) fail("exec: mtp stack empty");
    {
        // one backward: grads must be finite on canonical params
        const int VT = VP + T;
        std::vector<int> vlab((size_t)VT, -100);
        for (int t = 0; t < T - 1; ++t) vlab[VP + t] = ids[t + 1];
        std::vector<float> dl;
        ce_loss(o.logits, vlab, VT, c.vocab, dl);
        std::vector<std::vector<float>> dmtp;
        mtp_stack_aux_loss(c, ids, o, dmtp);
        p.zero_grad();
        try {
            bwd(p, c, ids, o, dl, 1.0f, &vp, &dmtp);
        } catch (...) { fail("exec: bwd threw"); }
        for (auto& n : p.order) {
            const Tensor& g = p.g[n];
            for (float x : g.d)
                if (!std::isfinite(x)) { fin = false; break; }
            if (!fin) { fail("exec: non-finite grad"); break; }
        }
        // the pinned MTP stack must own trainable params with signal
        const std::string m0 = "mtp.0.eh";
        if (!p.w.count(m0)) fail("exec: mtp param missing");
    }

    // ---- ckpt: XCN10 unconditional + deterministic bytes -----------
    {
        const char* t1 = "xct_canon_a.tmp", *t2 = "xct_canon_b.tmp";
        if (!ckpt_save(p, c, t1, true) || !ckpt_save(p, c, t2, true))
            fail("ckpt: save");
        if (slurp(t1) != slurp(t2)) fail("ckpt: non-deterministic bytes");
        ModelConfig pk;
        if (!ckpt_peek_config(t1, pk)) fail("ckpt: peek");
        else {
            if (pk.is_gemma4()) fail("ckpt: gemma4 marker not 0");
            if (!canon_cfg_eq(c, pk)) fail("ckpt: config drift");
        }
        Params p2;
        init_params(p2, pk, 0);
        if (!ckpt_load(p2, pk, t1)) fail("ckpt: load");
        else {
            for (auto& n : p.order) {
                const Tensor& a = p.w.at(n), &b = p2.w.at(n);
                if (a.d != b.d) { fail("ckpt: tensor drift"); break; }
            }
        }
        std::remove(t1); std::remove(t2);
    }

    bool ok = failures == 0;
    std::printf("canoncheck: xc-fused-1 failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}
