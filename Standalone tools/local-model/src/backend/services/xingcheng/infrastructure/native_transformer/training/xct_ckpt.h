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
    // XCN3 = XCN2 + Gemma4 header block (flag u32, dims, rope/softcap
    // floats, layer_types + hidden_act strings). v1/v2 still load.
    const uint32_t ver = c.is_gemma4() ? 3 : 2;
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
    if (ver >= 3) {
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
    if (ver < 1 || ver > 3) return false;
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
    if (ver >= 3 && !ckpt_read_g4(f, c)) return false;
    return (bool)f;
}

static bool ckpt_load(Params& p, ModelConfig& c, const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    char magic[4]; f.read(magic, 4);
    if (std::memcmp(magic, "XCN1", 4) != 0) return false;
    const uint32_t ver = r32(f);
    if (ver < 1 || ver > 3) return false;
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
    if (ver >= 3 && !ckpt_read_g4(f, c)) return false;
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
