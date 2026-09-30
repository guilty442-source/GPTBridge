// xct_ckpt.h — B94 fragment of xingcheng_trainer.cpp (checkpoint io).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

// ------------------------------------------------------------ checkpoint --

static void u32(std::ofstream& f, uint32_t x) { f.write((char*)&x, 4); }
static void u64(std::ofstream& f, uint64_t x) { f.write((char*)&x, 8); }
static uint32_t r32(std::ifstream& f) { uint32_t x; f.read((char*)&x, 4); return x; }
static uint64_t r64(std::ifstream& f) { uint64_t x; f.read((char*)&x, 8); return x; }

static bool ckpt_save(const Params& p, const ModelConfig& c,
                      const std::string& path, bool overwrite) {
    std::ifstream chk(path, std::ios::binary);
    if (chk && !overwrite) return false;   // never silently overwrite weights
    chk.close();
    std::string tmp = path + ".tmp";
    std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    // XCN2 = XCN1 header + three u32 MoE dimension fields (expert inter,
    // shared experts, shared inter) appended before the tensor table.
    // XCN3 = XCN2 + hybrid-attention block: full_attention_interval, flag
    // bits (attn_output_gate | qk_norm | shared_expert_gate), partial
    // rotary fraction, linear-attention geometry (k heads/dim, v
    // heads/dim, conv kernel).
    // XCN4 = XCN3 + vision early-fusion block: use_vision, vision
    // patch_dim, vision max_patches.
    // XCN5 = XCN4 + Gemma A4B block: global_attention_interval,
    // sliding_window, num_global_kv_heads, flag bits (k_eq_v_global |
    // post_attn_norm | post_ffw_norm | ffn_act), local/global rope
    // proportions and base frequencies, final_logit_softcap.
    // XCN6 = XCN5 + fused-router flag: moe_router_sigmoid (u32 bool).
    // XCN7 = XCN6 + DeepSeek V4-Pro block: MLA dims (kv_lora_rank,
    // q_lora_rank, qk_nope/qk_rope head dims), aux-free balance flag +
    // bias rate, MTP depth + loss weight.
    // XCN8 = XCN7 + Qwen3-Coder YaRN block: extension factor, original
    // context length, beta_fast/beta_slow band bounds, attention factor.
    // XCN9 = XCN8 + Gemma4 header block (marker u32, dims, rope/softcap
    // floats, layer_types + hidden_act strings) — gemma4 only.
    // v1..v8 checkpoints still load: absent fields default to the
    // Qwen-style fused behaviour.
    const uint32_t ver = c.is_gemma4() ? 9 : 8;
    f.write("XCN1", 4); u32(f, ver);
    u32(f, (uint32_t)c.vocab); u32(f, (uint32_t)c.hidden);
    u32(f, (uint32_t)c.inter); u32(f, (uint32_t)c.layers);
    u32(f, (uint32_t)c.heads); u32(f, (uint32_t)c.kv_heads);
    u32(f, (uint32_t)c.max_pos); u32(f, (uint32_t)c.moe_experts);
    u32(f, (uint32_t)c.moe_top_k); u32(f, (uint32_t)c.moe_layer_interval);
    f.write((char*)&c.rope_theta, 4); f.write((char*)&c.rms_eps, 4);
    f.write((char*)&c.moe_aux_w, 4);
    u32(f, (uint32_t)c.moe_expert_inter);
    u32(f, (uint32_t)c.moe_shared_experts);
    u32(f, (uint32_t)c.moe_shared_inter);
    u32(f, (uint32_t)c.full_attention_interval);
    u32(f, (c.attn_output_gate ? 1u : 0u) | (c.qk_norm ? 2u : 0u) |
           (c.shared_expert_gate ? 4u : 0u));
    f.write((char*)&c.partial_rotary, 4);
    u32(f, (uint32_t)c.lin_key_heads); u32(f, (uint32_t)c.lin_key_dim);
    u32(f, (uint32_t)c.lin_value_heads); u32(f, (uint32_t)c.lin_value_dim);
    u32(f, (uint32_t)c.lin_conv_kernel);
    u32(f, c.use_vision ? 1u : 0u);
    u32(f, (uint32_t)c.vision_patch_dim);
    u32(f, (uint32_t)c.vision_max_patches);
    // XCN5 A4B block — written unconditionally (defaults are zeros).
    u32(f, (uint32_t)c.global_attn_interval);
    u32(f, (uint32_t)c.sliding_window);
    u32(f, (uint32_t)c.num_global_kv_heads);
    u32(f, (c.k_eq_v_global ? 1u : 0u) | (c.post_attn_norm ? 2u : 0u) |
           (c.post_ffw_norm ? 4u : 0u) | (c.ffn_act ? 8u : 0u));
    f.write((char*)&c.local_rope_proportion, 4);
    f.write((char*)&c.global_rope_proportion, 4);
    f.write((char*)&c.rope_theta_local, 4);
    f.write((char*)&c.rope_theta_global, 4);
    f.write((char*)&c.final_logit_softcap, 4);
    u32(f, c.moe_router_sigmoid ? 1u : 0u);
    u32(f, (uint32_t)c.kv_lora_rank); u32(f, (uint32_t)c.q_lora_rank);
    u32(f, (uint32_t)c.qk_nope_head_dim);
    u32(f, (uint32_t)c.qk_rope_head_dim);
    u32(f, c.moe_auxfree_balance ? 1u : 0u);
    f.write((char*)&c.moe_lb_bias_rate, 4);
    u32(f, (uint32_t)c.mtp_num_layers);
    f.write((char*)&c.mtp_loss_weight, 4);
    f.write((char*)&c.yarn_factor, 4);
    u32(f, (uint32_t)c.yarn_orig_pos);
    f.write((char*)&c.yarn_beta_fast, 4);
    f.write((char*)&c.yarn_beta_slow, 4);
    f.write((char*)&c.yarn_attn_factor, 4);
    // XCN9 Gemma4 block — only for gemma4 configs.
    if (ver >= 9) {
        u32(f, 1);                                   // gemma4 marker
        u32(f, (uint32_t)c.head_dim);
        u32(f, (uint32_t)c.global_head_dim);
        u32(f, (uint32_t)c.sliding_window);
        u32(f, (uint32_t)c.num_kv_shared_layers);
        u32(f, (uint32_t)c.ple_hidden);
        u32(f, (uint32_t)c.ple_vocab);
        u32(f, (c.use_double_wide_mlp ? 1u : 0u) |
               (c.tie_embed ? 2u : 0u));
        f.write((char*)&c.rope_theta_full, 4);
        f.write((char*)&c.rope_partial_full, 4);
        f.write((char*)&c.final_logit_softcapping, 4);
        f.write((char*)&c.attention_scale, 4);
        u32(f, (uint32_t)c.layer_types.size());
        for (const auto& t : c.layer_types) {
            u32(f, (uint32_t)t.size());
            f.write(t.data(), (std::streamsize)t.size());
        }
        u32(f, (uint32_t)c.hidden_act.size());
        f.write(c.hidden_act.data(), (std::streamsize)c.hidden_act.size());
    }
    u32(f, (uint32_t)p.order.size());
    for (auto& n : p.order) {
        const Tensor& t = p.w.at(n);
        u32(f, (uint32_t)n.size()); f.write(n.data(), n.size());
        u32(f, (uint32_t)t.shape.size());
        for (auto s : t.shape) u64(f, (uint64_t)s);
        u64(f, (uint64_t)t.d.size());
        f.write((char*)t.d.data(), (std::streamsize)t.d.size() * 4);
    }
    f.close();
    if (!f) return false;
    std::remove(path.c_str());
    return std::rename(tmp.c_str(), path.c_str()) == 0;
}

static bool ckpt_read_g4(std::ifstream& f, ModelConfig& c) {
    const uint32_t marker = r32(f);
    if (!f || marker != 1) return false;
    c.model_type = "gemma4_text";
    c.head_dim = (int)r32(f);
    c.global_head_dim = (int)r32(f);
    c.sliding_window = (int)r32(f);
    c.num_kv_shared_layers = (int)r32(f);
    c.ple_hidden = (int)r32(f);
    c.ple_vocab = (int)r32(f);
    const uint32_t flags = r32(f);
    c.use_double_wide_mlp = (flags & 1u) != 0;
    c.tie_embed = (flags & 2u) != 0;
    f.read((char*)&c.rope_theta_full, 4);
    f.read((char*)&c.rope_partial_full, 4);
    f.read((char*)&c.final_logit_softcapping, 4);
    f.read((char*)&c.attention_scale, 4);
    const uint32_t nt = r32(f);
    c.layer_types.clear();
    for (uint32_t i = 0; i < nt; ++i) {
        const uint32_t nl = r32(f);
        std::string s(nl, '\0');
        f.read(s.data(), nl);
        c.layer_types.push_back(std::move(s));
    }
    const uint32_t al = r32(f);
    c.hidden_act.assign(al, '\0');
    f.read(c.hidden_act.data(), al);
    return (bool)f;
}

static bool ckpt_peek_config(const std::string& path, ModelConfig& c) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    char magic[4]; f.read(magic, 4);
    if (std::memcmp(magic, "XCN1", 4) != 0) return false;
    const uint32_t ver = r32(f);
    if (ver < 1 || ver > 9) return false;
    c.vocab = (int)r32(f); c.hidden = (int)r32(f); c.inter = (int)r32(f);
    c.layers = (int)r32(f); c.heads = (int)r32(f); c.kv_heads = (int)r32(f);
    c.max_pos = (int)r32(f); c.moe_experts = (int)r32(f);
    c.moe_top_k = (int)r32(f); c.moe_layer_interval = (int)r32(f);
    f.read((char*)&c.rope_theta, 4); f.read((char*)&c.rms_eps, 4);
    f.read((char*)&c.moe_aux_w, 4);
    if (ver >= 2) {
        c.moe_expert_inter = (int)r32(f);
        c.moe_shared_experts = (int)r32(f);
        c.moe_shared_inter = (int)r32(f);
    }
    if (ver >= 3) {
        c.full_attention_interval = (int)r32(f);
        uint32_t fl = r32(f);
        c.attn_output_gate = (fl & 1u) != 0;
        c.qk_norm = (fl & 2u) != 0;
        c.shared_expert_gate = (fl & 4u) != 0;
        f.read((char*)&c.partial_rotary, 4);
        c.lin_key_heads = (int)r32(f); c.lin_key_dim = (int)r32(f);
        c.lin_value_heads = (int)r32(f); c.lin_value_dim = (int)r32(f);
        c.lin_conv_kernel = (int)r32(f);
    }
    if (ver >= 4) {
        c.use_vision = r32(f) != 0;
        c.vision_patch_dim = (int)r32(f);
        c.vision_max_patches = (int)r32(f);
    }
    if (ver >= 5) {
        c.global_attn_interval = (int)r32(f);
        c.sliding_window = (int)r32(f);
        c.num_global_kv_heads = (int)r32(f);
        uint32_t fl = r32(f);
        c.k_eq_v_global = (fl & 1u) != 0;
        c.post_attn_norm = (fl & 2u) != 0;
        c.post_ffw_norm = (fl & 4u) != 0;
        c.ffn_act = (fl & 8u) != 0 ? 1 : 0;
        f.read((char*)&c.local_rope_proportion, 4);
        f.read((char*)&c.global_rope_proportion, 4);
        f.read((char*)&c.rope_theta_local, 4);
        f.read((char*)&c.rope_theta_global, 4);
        f.read((char*)&c.final_logit_softcap, 4);
    }
    if (ver >= 6) c.moe_router_sigmoid = r32(f) != 0;
    if (ver >= 7) {
        c.kv_lora_rank = (int)r32(f); c.q_lora_rank = (int)r32(f);
        c.qk_nope_head_dim = (int)r32(f);
        c.qk_rope_head_dim = (int)r32(f);
        c.moe_auxfree_balance = r32(f) != 0;
        f.read((char*)&c.moe_lb_bias_rate, 4);
        c.mtp_num_layers = (int)r32(f);
        f.read((char*)&c.mtp_loss_weight, 4);
    }
    if (ver >= 8) {
        f.read((char*)&c.yarn_factor, 4);
        c.yarn_orig_pos = (int)r32(f);
        f.read((char*)&c.yarn_beta_fast, 4);
        f.read((char*)&c.yarn_beta_slow, 4);
        f.read((char*)&c.yarn_attn_factor, 4);
    }
    if (ver >= 9 && !ckpt_read_g4(f, c)) return false;
    return (bool)f;
}

static bool ckpt_load(Params& p, ModelConfig& c, const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    char magic[4]; f.read(magic, 4);
    if (std::memcmp(magic, "XCN1", 4) != 0) return false;
    const uint32_t ver = r32(f);
    if (ver < 1 || ver > 9) return false;
    c.vocab = (int)r32(f); c.hidden = (int)r32(f); c.inter = (int)r32(f);
    c.layers = (int)r32(f); c.heads = (int)r32(f); c.kv_heads = (int)r32(f);
    c.max_pos = (int)r32(f); c.moe_experts = (int)r32(f);
    c.moe_top_k = (int)r32(f); c.moe_layer_interval = (int)r32(f);
    f.read((char*)&c.rope_theta, 4); f.read((char*)&c.rms_eps, 4);
    f.read((char*)&c.moe_aux_w, 4);
    if (ver >= 2) {
        c.moe_expert_inter = (int)r32(f);
        c.moe_shared_experts = (int)r32(f);
        c.moe_shared_inter = (int)r32(f);
    }
    if (ver >= 3) {
        c.full_attention_interval = (int)r32(f);
        uint32_t fl = r32(f);
        c.attn_output_gate = (fl & 1u) != 0;
        c.qk_norm = (fl & 2u) != 0;
        c.shared_expert_gate = (fl & 4u) != 0;
        f.read((char*)&c.partial_rotary, 4);
        c.lin_key_heads = (int)r32(f); c.lin_key_dim = (int)r32(f);
        c.lin_value_heads = (int)r32(f); c.lin_value_dim = (int)r32(f);
        c.lin_conv_kernel = (int)r32(f);
    }
    if (ver >= 4) {
        c.use_vision = r32(f) != 0;
        c.vision_patch_dim = (int)r32(f);
        c.vision_max_patches = (int)r32(f);
    }
    if (ver >= 5) {
        c.global_attn_interval = (int)r32(f);
        c.sliding_window = (int)r32(f);
        c.num_global_kv_heads = (int)r32(f);
        uint32_t fl = r32(f);
        c.k_eq_v_global = (fl & 1u) != 0;
        c.post_attn_norm = (fl & 2u) != 0;
        c.post_ffw_norm = (fl & 4u) != 0;
        c.ffn_act = (fl & 8u) != 0 ? 1 : 0;
        f.read((char*)&c.local_rope_proportion, 4);
        f.read((char*)&c.global_rope_proportion, 4);
        f.read((char*)&c.rope_theta_local, 4);
        f.read((char*)&c.rope_theta_global, 4);
        f.read((char*)&c.final_logit_softcap, 4);
    }
    if (ver >= 6) c.moe_router_sigmoid = r32(f) != 0;
    if (ver >= 7) {
        c.kv_lora_rank = (int)r32(f); c.q_lora_rank = (int)r32(f);
        c.qk_nope_head_dim = (int)r32(f);
        c.qk_rope_head_dim = (int)r32(f);
        c.moe_auxfree_balance = r32(f) != 0;
        f.read((char*)&c.moe_lb_bias_rate, 4);
        c.mtp_num_layers = (int)r32(f);
        f.read((char*)&c.mtp_loss_weight, 4);
    }
    if (ver >= 8) {
        f.read((char*)&c.yarn_factor, 4);
        c.yarn_orig_pos = (int)r32(f);
        f.read((char*)&c.yarn_beta_fast, 4);
        f.read((char*)&c.yarn_beta_slow, 4);
        f.read((char*)&c.yarn_attn_factor, 4);
    }
    if (ver >= 9 && !ckpt_read_g4(f, c)) return false;
    uint32_t nt = r32(f);
    for (uint32_t i = 0; i < nt; ++i) {
        uint32_t nl = r32(f);
        std::string n(nl, '\0'); f.read(n.data(), nl);
        uint32_t nd = r32(f);
        std::vector<int64_t> shp(nd);
        for (auto& s : shp) s = (int64_t)r64(f);
        uint64_t cnt = r64(f);
        if (p.w.count(n)) {
            Tensor& t = p.w[n];
            if ((uint64_t)t.numel() != cnt) return false;
            f.read((char*)t.d.data(), (std::streamsize)cnt * 4);
        } else {
            f.seekg((std::streamoff)cnt * 4, std::ios::cur);
        }
    }
    return (bool)f;
}
