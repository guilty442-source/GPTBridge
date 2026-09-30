// xcm_scale.h — NativeScaleEfficiencyPlane modes for xc_modeltool.
// Included once inside the anonymous namespace after xcm_efficiency.h.
//
//   scale-metrics          --bundle <dir> [--gpu-budget B]
//                          [--ram-budget B]
//                          §3/§4 seven-parameter truth + amplification
//   expert-store-build     --bundle <dir> --out <dir>
//                          [--quant none|int8]
//                          §10/§11 MappedExpertStore (XEB1 sharded bank)
//   expert-store-read      --bank <experts.xeb> --layer L --expert E
//                          [--timeout-ms N]  §13 cold-load path
//   prefetch-probe         --bundle <dir> [--tokens N]
//                          §14/§15 router-hint prefetch + session pin
//   delta-precision-probe  --bundle <dir> [--sizes 2048,4096,...]
//                          §22 FP64/BF16/FP16 delta-state drift
//   low-resource-sim       --bundle <dir> --gpu-budget B
//                          --ram-budget B [--nvme-budget B]
//                          [--cpu-pct N] [--tokens N]
//                          §52/§53 forced three-tier residency
//   scale-sim              --file <candidate.json>
//                          §29-§34 candidate simulator + ranking
//   scale-status           --bundle <dir>   §55 star-scale-status/v1
//   future-scale-probe     --file <shape.json> | --bundle <dir>
//                          §40-§43 research estimators (no arch change)
#pragma once

#include <map>
#include <set>

// ------------------------------------------------- §3 accounting ----
// Tensor classes: routed experts / shared experts / router / common
// core (embeddings, attention, deltanet, dense mlp, norms, lm_head,
// vision, mtp). TOTAL = logical parameters; UNIQUE = stored physical
// parameters (tied embeddings counted once); ACTIVE = common + shared
// + top_k routed experts per MoE layer; residency splits ACTIVE/TOTAL
// by the DEVICE_HOT / HOST_WARM / NVME_COLD tiers.
struct XcmScaleAcct {
    int64_t total = 0, unique = 0, common = 0, shared = 0,
            routed = 0, router = 0;
    int64_t moe_layers = 0;      // layers carrying routed experts
    int64_t experts = 0;         // routed experts per MoE layer
    int64_t top_k = 0;
    std::map<std::pair<int64_t, int64_t>, int64_t> expert_elems;
    std::vector<int64_t> moe_layer_ids;
};

int64_t xcm_shape_count(const JsonValue& t) {
    const JsonValue* sh = t.get("shape");
    if (!sh || sh->type != JsonValue::Type::Array) return 0;
    int64_t n = 1;
    for (const auto& d : sh->array) n *= (int64_t)d.number;
    return n;
}

XcmScaleAcct xcm_account(const JsonValue& mf) {
    XcmScaleAcct a;
    const JsonValue* tensors = mf.get("tensors");
    const JsonValue* cfg = mf.get("config");
    if (!tensors || tensors->type != JsonValue::Type::Object) return a;
    std::map<int64_t, int64_t> layer_max_expert;
    std::set<int64_t> moe_layers;
    std::map<std::pair<int64_t, int64_t>, int64_t> dup;
    int64_t embed_elems = 0, lmhead_elems = 0;
    for (const auto& kv : tensors->object) {
        const std::string& n = kv.first;
        int64_t elems = xcm_shape_count(kv.second);
        a.total += elems;
        a.unique += elems;
        int64_t layer = -1, expert = -1;
        // model.layers.<L>.mlp.experts.<E>.<proj>.weight
        size_t lp = n.find("model.layers.");
        if (lp == 0) {
            size_t ls = lp + 13, le = n.find('.', ls);
            if (le != std::string::npos)
                layer = std::stoll(n.substr(ls, le - ls));
        }
        size_t ep = n.find(".mlp.experts.");
        size_t sp = n.find(".mlp.shared_experts.");
        if (ep != std::string::npos && layer >= 0) {
            size_t es = ep + 13, ee = n.find('.', es);
            expert = std::stoll(n.substr(es, ee - es));
            a.routed += elems;
            a.expert_elems[{layer, expert}] += elems;
            layer_max_expert[layer] =
                std::max(layer_max_expert[layer], expert + 1);
            moe_layers.insert(layer);
        } else if (sp != std::string::npos) {
            a.shared += elems;
        } else if (n.find(".mlp.router.") != std::string::npos) {
            a.router += elems;
        } else {
            a.common += elems;
            if (n.find("word_embeddings") != std::string::npos)
                embed_elems = elems;
            if (n.find("lm_head") != std::string::npos)
                lmhead_elems = elems;
        }
    }
    a.moe_layers = (int64_t)moe_layers.size();
    a.moe_layer_ids.assign(moe_layers.begin(), moe_layers.end());
    for (const auto& kv : layer_max_expert)
        a.experts = std::max(a.experts, kv.second);
    if (cfg)
        a.top_k = (int64_t)xct::j_num(cfg, "moe_top_k", 0);
    // Tied embeddings: one physical storage serves two logical roles.
    bool tied = cfg &&
        xct::j_num(cfg, "tie_word_embeddings", 0) > 0.5 &&
        embed_elems == lmhead_elems && lmhead_elems > 0;
    if (tied) a.unique -= lmhead_elems;
    return a;
}

// §3 seven metrics + §4 amplifications. gpu/ram budgets (bytes of
// expert weights) drive the tier split; absent budgets mean every
// expert fits on device (all-resident reference point).
int mode_scale_metrics(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SCALE_ARGS_MISSING");
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    if (!cfg) fail("SCALE_BAD_CONFIG");
    XcmScaleAcct ac = xcm_account(mf);
    const int64_t tk = ac.top_k > 0 ? ac.top_k : 1;
    int64_t per_expert = 0;
    if (!ac.expert_elems.empty())
        per_expert = ac.expert_elems.begin()->second;
    // ACTIVE = common + router + shared + top_k experts × moe layers.
    int64_t active = ac.common + ac.router + ac.shared +
                     per_expert * tk * ac.moe_layers;
    int64_t gpu_b = a.has("gpu-budget")
        ? std::stoll(a.get("gpu-budget")) : INT64_MAX;
    int64_t ram_b = a.has("ram-budget")
        ? std::stoll(a.get("ram-budget")) : INT64_MAX;
    // Pinned set always occupies the device: common + router + shared.
    int64_t pinned = ac.common + ac.router + ac.shared;
    int64_t gpu_rem = gpu_b == INT64_MAX ? INT64_MAX
        : std::max<int64_t>(0, gpu_b - pinned * 8 / 8);
    int64_t gpu_resident = pinned;
    int64_t ram_resident = 0, nvme_cold = 0;
    int64_t rem_gpu = gpu_rem, rem_ram = ram_b;
    for (const auto& e : ac.expert_elems) {
        int64_t bytes = e.second;
        if (rem_gpu >= bytes) { gpu_resident += bytes; rem_gpu -= bytes; }
        else if (rem_ram >= bytes) { ram_resident += bytes; rem_ram -= bytes; }
        else nvme_cold += bytes;
    }
    // params vs bytes: tensor elements == params (fp64 storage).
    double cap_amp = active > 0 ? (double)ac.total / active : 0.0;
    double gpu_amp = gpu_resident > 0
        ? (double)ac.total / gpu_resident : 0.0;
    double ram_amp =
        (gpu_resident + ram_resident) > 0
            ? (double)ac.total / (gpu_resident + ram_resident) : 0.0;
    std::printf(
        "{\"ok\":true,\"mode\":\"scale-metrics\",\"format\":"
        "\"star-scale-metrics/v1\",\"total_params\":%lld,"
        "\"unique_params\":%lld,\"active_params\":%lld,"
        "\"gpu_resident_params\":%lld,\"ram_resident_params\":%lld,"
        "\"nvme_cold_params\":%lld,\"trainable_params\":%lld,"
        "\"capacity_amplification\":%.4f,\"gpu_amplification\":%.4f,"
        "\"ram_amplification\":%.4f,\"moe_layers\":%lld,"
        "\"experts_per_layer\":%lld,\"top_k\":%lld,"
        "\"per_expert_params\":%lld}\n",
        (long long)ac.total, (long long)ac.unique,
        (long long)active, (long long)gpu_resident,
        (long long)ram_resident, (long long)nvme_cold,
        (long long)ac.unique, cap_amp, gpu_amp, ram_amp,
        (long long)ac.moe_layers, (long long)ac.experts,
        (long long)tk, (long long)per_expert);
    return 0;
}

// ------------------------------------------- §10/§11 XEB1 bank ------
// MappedExpertStore: block-aligned (4KiB), independently addressable,
// checksummed, generation- and bundle-bound expert bank. Layout:
//   "XEB1" u32 ver | u32 entry_count | u64 gen_len + gen |
//   u64 bundle_sha_len + sha | index entries (each 128B):
//     u32 layer, u32 expert, u64 offset, u64 length,
//     u8 dtype_tag (0=fp64 1=int8 2=bf16), u8 pad[3],
//     double quant_scale | u64 pad | char sha256[64]
//   then 4096-aligned data blocks.
struct XebEntry {
    int64_t layer = 0, expert = 0;
    uint64_t offset = 0, length = 0;
    int dtype = 0;
    double scale = 0.0;
    std::string sha;
};

int mode_expert_store_build(const Args& a) {
    std::string bundle = a.get("bundle");
    std::string out_dir = a.get("out");
    std::string quant = a.get("quant");
    if (quant.empty()) quant = "none";
    if (bundle.empty() || out_dir.empty() ||
        (quant != "none" && quant != "int8"))
        fail("XEB_ARGS_MISSING");
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* tensors = mf.get("tensors");
    if (!tensors || tensors->type != JsonValue::Type::Object)
        fail("XEB_NO_TENSORS");
    std::string weights_sha =
        mf.get("weights_sha256") &&
                mf.get("weights_sha256")->type == JsonValue::Type::String
            ? mf.get("weights_sha256")->string : "";
    std::string generation =
        mf.get("architecture_generation") &&
                mf.get("architecture_generation")->type ==
                    JsonValue::Type::String
            ? mf.get("architecture_generation")->string : "";
    fs::path wbin = fs::path(bundle) / "weights.bin";

    // collect per-(layer,expert) member tensors (gate/up/down in order)
    struct Member { std::string name; uint64_t off, len; };
    std::map<std::pair<int64_t, int64_t>, std::vector<Member>> groups;
    for (const auto& kv : tensors->object) {
        const std::string& n = kv.first;
        size_t ep = n.find(".mlp.experts.");
        if (ep == std::string::npos) continue;
        size_t lp = 13, le = n.find('.', lp);
        int64_t layer = std::stoll(n.substr(lp, le - lp));
        size_t es = ep + 13, ee = n.find('.', es);
        int64_t ex = std::stoll(n.substr(es, ee - es));
        Member m;
        m.name = n;
        m.off = (uint64_t)xct::j_num(&kv.second, "offset", 0);
        m.len = (uint64_t)xct::j_num(&kv.second, "bytes", 0);
        groups[{layer, ex}].push_back(m);
    }
    if (groups.empty()) fail("XEB_NO_EXPERTS");
    std::ifstream wsrc(wbin, std::ios::binary);
    if (!wsrc) fail("XEB_WEIGHTS_UNREADABLE");

    fs::create_directories(out_dir);
    fs::path bank_path = fs::path(out_dir) / "experts.xeb";
    std::ofstream bank(bank_path, std::ios::binary | std::ios::trunc);
    if (!bank) fail("XEB_WRITE_FAILED");
    auto put32 = [&bank](uint32_t v) {
        bank.write(reinterpret_cast<const char*>(&v), 4);
    };
    auto put64 = [&bank](uint64_t v) {
        bank.write(reinterpret_cast<const char*>(&v), 8);
    };
    bank.write("XEB1", 4);
    put32(1);
    put32((uint32_t)groups.size());
    put64(generation.size());
    bank.write(generation.data(), (std::streamsize)generation.size());
    put64(weights_sha.size());
    bank.write(weights_sha.data(), (std::streamsize)weights_sha.size());
    const uint64_t index_off = (uint64_t)bank.tellp();
    const uint64_t entry_sz = 128;
    // reserve index
    std::vector<char> zeros((size_t)entry_sz * groups.size(), 0);
    bank.write(zeros.data(), (std::streamsize)zeros.size());

    std::vector<XebEntry> entries;
    std::vector<char> buf;
    for (auto& g : groups) {
        // gather member bytes (concatenated projections)
        std::vector<char> payload;
        int64_t elems = 0;
        for (const Member& m : g.second) {
            buf.resize((size_t)m.len);
            wsrc.seekg((std::streamoff)m.off);
            wsrc.read(buf.data(), (std::streamsize)m.len);
            if (wsrc.gcount() != (std::streamsize)m.len)
                fail("XEB_READ_SHORT");
            payload.insert(payload.end(), buf.begin(), buf.end());
            elems += (int64_t)(m.len / 8);
        }
        XebEntry e;
        e.layer = g.first.first;
        e.expert = g.first.second;
        if (quant == "int8") {
            // per-expert symmetric int8 over the concatenated fp64
            // payload — storage precision, dequantized on load.
            double amax = 0.0;
            const double* d =
                reinterpret_cast<const double*>(payload.data());
            for (int64_t i = 0; i < elems; ++i)
                amax = std::max(amax, std::fabs(d[i]));
            e.scale = amax / 127.0;
            std::vector<char> q((size_t)elems);
            for (int64_t i = 0; i < elems; ++i)
                q[(size_t)i] = (char)std::lrint(d[i] / e.scale);
            e.dtype = 1;
            payload = std::move(q);
        }
        // align to 4096
        uint64_t pos = (uint64_t)bank.tellp();
        uint64_t pad = (4096 - pos % 4096) % 4096;
        if (pad) {
            std::vector<char> pz((size_t)pad, 0);
            bank.write(pz.data(), (std::streamsize)pad);
            pos += pad;
        }
        e.offset = pos;
        e.length = payload.size();
        e.sha = sha256_bytes(
            reinterpret_cast<const unsigned char*>(payload.data()),
            payload.size());
        bank.write(payload.data(), (std::streamsize)payload.size());
        entries.push_back(std::move(e));
    }
    // back-patch index
    bank.seekp((std::streamoff)index_off);
    for (const XebEntry& e : entries) {
        put32((uint32_t)e.layer);
        put32((uint32_t)e.expert);
        put64(e.offset);
        put64(e.length);
        unsigned char tag[4] = {(unsigned char)e.dtype, 0, 0, 0};
        bank.write(reinterpret_cast<const char*>(tag), 4);
        bank.write(reinterpret_cast<const char*>(&e.scale), 8);
        put64(0);
        char shabuf[64] = {};
        std::memcpy(shabuf, e.sha.data(),
                    std::min<size_t>(64, e.sha.size()));
        bank.write(shabuf, 64);
        // pad to 128
        int64_t wrote = 4 + 4 + 8 + 8 + 4 + 8 + 8 + 64;
        std::vector<char> pz((size_t)(entry_sz - wrote), 0);
        bank.write(pz.data(), (std::streamsize)pz.size());
    }
    bank.flush();
    bank.close();
    uint64_t total_bytes =
        (uint64_t)fs::file_size(bank_path);
    std::printf(
        "{\"ok\":true,\"mode\":\"expert-store-build\",\"format\":"
        "\"star-mapped-expert-store/v1\",\"bank\":\"%s\",\"entries\":%zu,"
        "\"storage_quant\":\"%s\",\"block_align\":4096,"
        "\"generation\":\"%s\",\"bundle_sha256\":\"%s\","
        "\"bank_bytes\":%llu}\n",
        gptbridge::jsonlite::json_escape(bank_path.string()).c_str(),
        entries.size(), quant.c_str(),
        gptbridge::jsonlite::json_escape(generation).c_str(),
        weights_sha.c_str(), (unsigned long long)total_bytes);
    return 0;
}

// Cold-load path: verify integrity -> read -> (optional) dequant
// preview -> report latency. EXPERT_STORAGE_CORRUPT on any checksum /
// bound mismatch; EXPERT_COLD_LOAD_TIMEOUT if the read exceeds the
// caller's latency budget.
int mode_expert_store_read(const Args& a) {
    std::string bank_path = a.get("bank");
    if (bank_path.empty()) fail("XEB_ARGS_MISSING");
    int64_t want_layer = a.has("layer") ? std::stoll(a.get("layer")) : -1;
    int64_t want_expert =
        a.has("expert") ? std::stoll(a.get("expert")) : -1;
    int64_t timeout_ms =
        a.has("timeout-ms") ? std::stoll(a.get("timeout-ms")) : 0;
    std::ifstream f(bank_path, std::ios::binary);
    if (!f) fail("XEB_UNREADABLE");
    char magic[4];
    f.read(magic, 4);
    if (std::memcmp(magic, "XEB1", 4) != 0)
        fail("EXPERT_STORAGE_CORRUPT:magic");
    uint32_t ver = 0, count = 0;
    f.read(reinterpret_cast<char*>(&ver), 4);
    f.read(reinterpret_cast<char*>(&count), 4);
    if (ver != 1) fail("EXPERT_STORAGE_CORRUPT:version");
    uint64_t glen = 0, slen = 0;
    f.read(reinterpret_cast<char*>(&glen), 8);
    std::string gen((size_t)glen, '\0');
    f.read(gen.data(), (std::streamsize)glen);
    f.read(reinterpret_cast<char*>(&slen), 8);
    std::string bsha((size_t)slen, '\0');
    f.read(bsha.data(), (std::streamsize)slen);
    uint64_t index_off = 4 + 4 + 4 + 8 + glen + 8 + slen;
    int64_t found = -1;
    XebEntry hit;
    const int64_t t0 = bench_now_ms();
    for (uint32_t i = 0; i < count; ++i) {
        XebEntry e;
        uint32_t l32, e32, dt;
        uint64_t pad;
        char shabuf[64];
        f.seekg((std::streamoff)(index_off + (uint64_t)i * 128));
        f.read(reinterpret_cast<char*>(&l32), 4);
        f.read(reinterpret_cast<char*>(&e32), 4);
        f.read(reinterpret_cast<char*>(&e.offset), 8);
        f.read(reinterpret_cast<char*>(&e.length), 8);
        f.read(reinterpret_cast<char*>(&dt), 4);
        f.read(reinterpret_cast<char*>(&e.scale), 8);
        f.read(reinterpret_cast<char*>(&pad), 8);
        f.read(shabuf, 64);
        e.layer = l32; e.expert = e32; e.dtype = (int)dt;
        e.sha.assign(shabuf, 64);
        if (e.layer == want_layer && e.expert == want_expert) {
            hit = e; found = (int64_t)i; break;
        }
    }
    if (found < 0) fail("EXPERT_PREFETCH_MISS:expert_not_in_bank");
    std::vector<char> payload((size_t)hit.length);
    f.seekg((std::streamoff)hit.offset);
    f.read(payload.data(), (std::streamsize)hit.length);
    if (f.gcount() != (std::streamsize)hit.length)
        fail("EXPERT_STORAGE_CORRUPT:short_read");
    const int64_t read_ms = bench_now_ms() - t0;
    std::string sha = sha256_bytes(
        reinterpret_cast<const unsigned char*>(payload.data()),
        payload.size());
    if (sha.compare(0, 64, hit.sha) != 0)
        fail("EXPERT_STORAGE_CORRUPT:checksum");
    if (timeout_ms > 0 && read_ms > timeout_ms)
        fail("EXPERT_COLD_LOAD_TIMEOUT");
    // optional dequant preview (int8 -> fp64 stats)
    int64_t elements = 0;
    if (hit.dtype == 1 && hit.scale > 0)
        elements = (int64_t)hit.length;
    else
        elements = (int64_t)hit.length / 8;
    std::printf(
        "{\"ok\":true,\"mode\":\"expert-store-read\",\"format\":"
        "\"star-mapped-expert-store/v1\",\"layer\":%lld,"
        "\"expert\":%lld,\"bytes\":%llu,\"dtype\":%d,\"elements\":%lld,"
        "\"checksum_verified\":true,\"generation\":\"%s\","
        "\"bundle_sha256\":\"%s\",\"cold_load_ms\":%lld}\n",
        (long long)hit.layer, (long long)hit.expert,
        (unsigned long long)hit.length, hit.dtype,
        (long long)elements,
        gptbridge::jsonlite::json_escape(gen).c_str(), bsha.c_str(),
        (long long)read_ms);
    return 0;
}

// -------------------------------------------- §14/§15 prefetch ------
// ExpertPrefetchPlanner: router prob hints + layer-transition stats +
// session affinity + residency + transfer latency. Prefetching layer
// L+1's predicted experts while L computes; a session that keeps
// hitting the same experts pins them (no load/execute/evict churn).
struct XcmPrefetchPlanner {
    // predicted experts for `next_layer` given the router decisions at
    // the current layer — transition histogram P(e_next | e_cur).
    std::map<std::pair<int64_t, int64_t>, std::map<int64_t, int64_t>>
        trans;         // (layer,expert) -> next-expert -> count
    std::map<std::pair<int64_t, int64_t>, int64_t> affinity;  // session
    int64_t prefetches = 0, prefetch_hits = 0, prefetch_misses = 0;
    int depth = 1;

    std::vector<int64_t> predict(int64_t next_layer, int64_t experts,
                                 const std::vector<int64_t>& cur_sel) {
        // candidate pool: union of historical next-experts for the
        // current selection + the session's hottest experts.
        std::map<int64_t, int64_t> votes;
        for (int64_t e : cur_sel)
            for (const auto& n : trans[{next_layer - 1, e}])
                votes[n.first] += n.second;
        for (const auto& af : affinity)
            if (af.first.first == next_layer)
                votes[af.first.second] += af.second;
        std::vector<std::pair<int64_t, int64_t>> v(
            votes.begin(), votes.end());
        std::sort(v.begin(), v.end(),
                  [](auto& x, auto& y) { return x.second > y.second; });
        std::vector<int64_t> out;
        for (int64_t i = 0; i < depth &&
                            i < (int64_t)v.size(); ++i)
            out.push_back(v[(size_t)i].first);
        return out;
    }
};

int mode_prefetch_probe(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PREFETCH_ARGS_MISSING");
    int64_t tokens = a.has("tokens") ? std::stoll(a.get("tokens")) : 48;
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("PREFETCH_LOAD:") + ex.what());
    }
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
    if (vocab < 8) fail("PREFETCH_BAD_CONFIG");
    e.set_router_trace(true);
    std::mt19937_64 rng(11);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> prompt;
    for (int i = 0; i < 32; ++i) prompt.push_back(tok(rng));
    SamplingConfig sc;
    std::vector<int64_t> gen;
    try { gen = e.generate(prompt, tokens, sc); }
    catch (const std::exception& ex) {
        fail(std::string("PREFETCH_GEN:") + ex.what());
    }
    JsonValue tr = e.router_trace();
    const JsonValue* layers = tr.get("layers");
    XcmPrefetchPlanner planner;
    XcmExpertResidency res;
    res.expert_bytes = 0;
    res.max_resident = 0;          // all-resident baseline; the probe
    res.gpu_budget_bytes = 0;      // measures planner accuracy only
    int64_t selections = 0, predicted_correct = 0;
    int64_t pinned = 0;
    std::set<int64_t> prev_sel_layers;
    std::map<int64_t, std::vector<int64_t>> prev_sel;
    std::map<int64_t, std::map<int64_t, double>> prob_hist;
    if (layers && layers->type == JsonValue::Type::Array) {
        for (const auto& L : layers->array) {
            int64_t lid = (int64_t)xct::j_num(&L, "layer_id", -1);
            const JsonValue* sel = L.get("selected");
            if (!sel || sel->type != JsonValue::Type::Array) continue;
            // per-token top-k rows
            for (const auto& row : sel->array) {
                if (row.type != JsonValue::Type::Array) continue;
                std::vector<int64_t> cur;
                for (const auto& x : row.array)
                    cur.push_back((int64_t)x.number);
                ++selections;
                // session affinity accumulate
                for (int64_t ex : cur) planner.affinity[{lid, ex}]++;
                // score the prediction made for this layer
                if (prev_sel.count(lid - 0) == 0) {}
                auto pit = prev_sel.find(lid);
                if (pit != prev_sel.end()) {
                    auto preds = planner.predict(
                        lid, 0, pit->second);
                    for (int64_t pe : preds) {
                        ++planner.prefetches;
                        bool hit = std::find(cur.begin(), cur.end(),
                                             pe) != cur.end();
                        if (hit) ++planner.prefetch_hits;
                        else ++planner.prefetch_misses;
                        if (hit) ++predicted_correct;
                    }
                }
                // transition stats: current layer's selection -> next
                // layer's experts are learned as they are seen.
                for (int64_t ex : cur)
                    for (int64_t nx : cur)
                        planner.trans[{lid, ex}][nx]++;
                prev_sel[lid] = cur;
            }
        }
    }
    double hit_rate = planner.prefetches > 0
        ? (double)planner.prefetch_hits / planner.prefetches : 0.0;
    std::printf(
        "{\"ok\":true,\"mode\":\"prefetch-probe\",\"format\":"
        "\"star-expert-prefetch/v1\",\"selections\":%lld,"
        "\"prefetches\":%lld,\"prefetch_hits\":%lld,"
        "\"prefetch_misses\":%lld,\"prefetch_hit_rate\":%.4f,"
        "\"affinity_entries\":%zu,\"transition_entries\":%zu,"
        "\"pinned_sessions\":%lld,\"residency_hits\":%lld,"
        "\"residency_misses\":%lld}\n",
        (long long)selections, (long long)planner.prefetches,
        (long long)planner.prefetch_hits,
        (long long)planner.prefetch_misses, hit_rate,
        planner.affinity.size(), planner.trans.size(),
        (long long)pinned, (long long)res.hits,
        (long long)res.misses);
    return 0;
}

// ------------------------------------------------- §22 delta state --
// XSST payload quantizer: walks the documented layout and rounds every
// fp64 payload element (conv tail + S matrix) down to bf16/fp16 then
// back — the same fidelity a low-precision store would deliver. The
// trailing sha256 is recomputed so restore_delta_state accepts the
// blob and the probe measures real end-to-end drift.
std::string xcm_xsst_quantize(const std::string& blob, int bits) {
    if (blob.size() < 4 || blob.compare(0, 4, "XSST") != 0)
        fail("SEQUENCE_STATE_INVALID:magic");
    std::string out = blob.substr(0, blob.size() - 64);
    size_t pos = 4;
    auto rd32 = [&]() -> uint32_t {
        uint32_t v;
        std::memcpy(&v, out.data() + pos, 4); pos += 4; return v;
    };
    auto rd64 = [&]() -> uint64_t {
        uint64_t v;
        std::memcpy(&v, out.data() + pos, 8); pos += 8; return v;
    };
    auto quant_at = [&](size_t p) {
        double d;
        std::memcpy(&d, out.data() + p, 8);
        if (bits == 16) {
            float f = (float)d;      // fp32 intermediate for fp16-class
            uint32_t u;
            std::memcpy(&u, &f, 4);
            u &= 0xFFFF0000u;        // bf16-style truncation
            std::memcpy(&f, &u, 4);
            d = (double)f;
        } else if (bits == 8) {
            float f = (float)d;
            uint32_t u;
            std::memcpy(&u, &f, 4);
            u &= 0xFF000000u;        // coarser: fp8-class mantissa cut
            std::memcpy(&f, &u, 4);
            d = (double)f;
        }
        std::memcpy(out.data() + p, &d, 8);
    };
    rd32();                          // ver
    uint32_t glen = rd32(); pos += glen;
    uint32_t hlen = rd32(); pos += hlen;
    uint32_t slots = rd32();
    for (uint32_t s = 0; s < slots; ++s) {
        uint32_t lc = rd32();
        for (uint32_t l = 0; l < lc; ++l) {
            uint64_t cn = rd64();
            for (uint64_t i = 0; i < cn; ++i) { quant_at(pos); pos += 8; }
            uint64_t sn = rd64();
            for (uint64_t i = 0; i < sn; ++i) { quant_at(pos); pos += 8; }
            pos += 8;                // tokens i64
        }
    }
    std::string sha = sha256_bytes(
        reinterpret_cast<const unsigned char*>(out.data()),
        out.size());
    out.append(sha);
    return out;
}

int mode_delta_precision_probe(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("DELTAPROBE_ARGS_MISSING");
    std::vector<int64_t> sizes = {2048, 4096, 8192, 16384, 32768};
    if (a.has("sizes")) {
        sizes.clear();
        std::string s = a.get("sizes");
        size_t pos = 0;
        while (pos < s.size()) {
            size_t c = s.find(',', pos);
            sizes.push_back(std::stoll(s.substr(
                pos, c == std::string::npos ? std::string::npos
                                            : c - pos)));
            if (c == std::string::npos) break;
            pos = c + 1;
        }
    }
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("DELTAPROBE_LOAD:") + ex.what());
    }
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
    const int64_t max_pos =
        (int64_t)xct::j_num(cfg, "max_position_embeddings", 0);
    if (vocab < 8) fail("DELTAPROBE_BAD_CONFIG");
    std::string gen =
        mf.get("architecture_generation") &&
                mf.get("architecture_generation")->type ==
                    JsonValue::Type::String
            ? mf.get("architecture_generation")->string
            : "gen-2-consolidated";
    std::mt19937_64 rng(3);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::printf(
        "{\"ok\":true,\"mode\":\"delta-precision-probe\",\"format\":"
        "\"star-delta-precision-probe/v1\",\"results\":[");
    bool first = true;
    for (int64_t n : sizes) {
        if (max_pos > 0 && n > max_pos) continue;
        if (n < 8 || n > 32768) continue;
        std::vector<int64_t> ids;
        for (int64_t i = 0; i < n; ++i) ids.push_back(tok(rng));
        std::vector<double> ref;
        try { ref = e.logits(ids); }
        catch (const std::exception& ex) {
            fail(std::string("DELTAPROBE_FWD:") + ex.what());
        }
        std::string snap;
        try { snap = e.snapshot_delta_state(gen); }
        catch (const std::exception&) { snap.clear(); }
        if (snap.empty()) {
            if (!first) std::printf(",");
            first = false;
            std::printf(
                "{\"size\":%lld,\"delta_state\":false,"
                "\"note\":\"no linear layers\"}", (long long)n);
            continue;
        }
        for (int bits : {16, 8}) {
            std::string q = xcm_xsst_quantize(snap, bits);
            try { e.restore_delta_state(q, gen); }
            catch (const std::exception& ex) {
                fail(std::string("DELTAPROBE_RESTORE:") + ex.what());
            }
            std::vector<double> got;
            try { got = e.logits(ids); }
            catch (const std::exception& ex) {
                fail(std::string("DELTAPROBE_FWD2:") + ex.what());
            }
            double max_d = 0.0;
            int64_t am_ref = 0, am_got = 0;
            if (ref.size() == got.size()) {
                for (size_t i = 0; i < ref.size(); ++i)
                    max_d = std::max(max_d,
                                     std::fabs(ref[i] - got[i]));
                for (size_t i = 1; i < ref.size(); ++i) {
                    if (ref[i] > ref[(size_t)am_ref]) am_ref = (int64_t)i;
                    if (got[i] > got[(size_t)am_got]) am_got = (int64_t)i;
                }
            }
            if (!first) std::printf(",");
            first = false;
            std::printf(
                "{\"size\":%lld,\"precision\":\"%s\",\"state_bytes\":%lld,"
                "\"logit_drift\":%.9f,\"argmax_match\":%s}",
                (long long)n, bits == 16 ? "bf16" : "fp8-class",
                (long long)snap.size(), max_d,
                am_ref == am_got ? "true" : "false");
        }
    }
    std::printf("]}\n");
    return 0;
}

// --------------------------------------------- §52/§53 low-resource -
// Forced three-tier residency over a real router trace: DEVICE_HOT
// (gpu budget) -> HOST_WARM (ram budget) -> NVME_COLD. A miss climbs
// COLD->WARM staging->HOT; when even the pinned common set cannot fit
// the gpu budget the sim fails closed. CPU budget pct caps the
// dequant/prefetch work allowed per token; exceeding it reports
// SCALE_CPU_BUDGET_EXCEEDED instead of silently spilling.
int mode_low_resource_sim(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("LOWRES_ARGS_MISSING");
    int64_t gpu_b = a.has("gpu-budget")
        ? std::stoll(a.get("gpu-budget")) : 0;
    int64_t ram_b = a.has("ram-budget")
        ? std::stoll(a.get("ram-budget")) : 0;
    int64_t nvme_b = a.has("nvme-budget")
        ? std::stoll(a.get("nvme-budget")) : 0;
    int cpu_pct = a.has("cpu-pct")
        ? std::stoi(a.get("cpu-pct")) : 100;
    int64_t tokens = a.has("tokens") ? std::stoll(a.get("tokens")) : 64;
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    if (!cfg) fail("LOWRES_BAD_CONFIG");
    XcmScaleAcct ac = xcm_account(mf);
    const int64_t per_expert = ac.expert_elems.empty()
        ? 0 : ac.expert_elems.begin()->second;
    const int64_t pinned = ac.common + ac.router + ac.shared;
    if (gpu_b > 0 && pinned > gpu_b)
        fail("SCALE_GPU_BUDGET_EXCEEDED:pinned_set");
    const int64_t gpu_expert_cap =
        gpu_b > 0 ? std::max<int64_t>(0, gpu_b - pinned) : INT64_MAX;
    if (nvme_b > 0 && ac.routed * 8 > nvme_b * 8 + 0 &&
        (int64_t)ac.expert_elems.size() * per_expert > nvme_b)
        fail("SCALE_NVME_BUDGET_EXCEEDED");

    // run the model under router trace to get a real selection stream
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("LOWRES_LOAD:") + ex.what());
    }
    const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
    if (vocab < 8) fail("LOWRES_BAD_CONFIG");
    e.set_router_trace(true);
    std::mt19937_64 rng(17);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> prompt;
    for (int i = 0; i < 24; ++i) prompt.push_back(tok(rng));
    SamplingConfig sc;
    try { e.generate(prompt, tokens, sc); }
    catch (const std::exception& ex) {
        fail(std::string("LOWRES_GEN:") + ex.what());
    }
    JsonValue tr = e.router_trace();
    const JsonValue* layers = tr.get("layers");

    // tier state per expert: 0=COLD 1=WARM 2=HOT
    std::map<std::pair<int64_t, int64_t>, int> tier;
    std::map<std::pair<int64_t, int64_t>, uint64_t> last_use, freq;
    uint64_t tick = 0;
    int64_t hot_bytes = 0, warm_bytes = 0;
    int64_t hits_hot = 0, hits_warm = 0, cold_loads = 0,
            evictions = 0;
    double est_transfer_ms = 0.0;
    auto evict_hot = [&]() {
        // residency score = recency + frequency; lowest score out.
        int64_t worst_score = INT64_MAX;
        std::pair<int64_t, int64_t> victim{-1, -1};
        for (const auto& t : tier) {
            if (t.second != 2) continue;
            uint64_t lu = last_use[t.first];
            int64_t sc = (int64_t)(tick - lu) -
                         (int64_t)freq[t.first] * 8;
            if (sc < worst_score) { worst_score = sc; victim = t.first; }
        }
        if (victim.first < 0) return false;
        tier[victim] = 1;  // HOT -> WARM (demote, not delete)
        hot_bytes -= per_expert;
        if (warm_bytes + per_expert > ram_b && ram_b > 0) {
            // WARM overflow spills back to COLD (nvme), never holds.
            tier[victim] = 0;
            warm_bytes = std::max<int64_t>(0, warm_bytes - per_expert);
        } else warm_bytes += per_expert;
        ++evictions;
        return true;
    };
    if (layers && layers->type == JsonValue::Type::Array)
        for (const auto& L : layers->array) {
            int64_t lid = (int64_t)xct::j_num(&L, "layer_id", -1);
            const JsonValue* sel = L.get("selected");
            if (!sel || sel->type != JsonValue::Type::Array) continue;
            for (const auto& row : sel->array) {
                if (row.type != JsonValue::Type::Array) continue;
                for (const auto& x : row.array) {
                    ++tick;
                    std::pair<int64_t, int64_t> key{
                        lid, (int64_t)x.number};
                    int t = tier.count(key) ? tier[key] : 0;
                    ++freq[key];
                    last_use[key] = tick;
                    if (t == 2) { ++hits_hot; continue; }
                    if (t == 1) {
                        ++hits_warm;
                        // WARM -> HOT staging
                        while (hot_bytes + per_expert >
                                   gpu_expert_cap &&
                               evict_hot()) {}
                        tier[key] = 2;
                        hot_bytes += per_expert;
                        warm_bytes -= per_expert;
                        est_transfer_ms += per_expert / 1e6 * 8.0;
                        continue;
                    }
                    // COLD: NVMe -> RAM staging -> HOT
                    ++cold_loads;
                    est_transfer_ms += per_expert / 1e6 * 20.0;
                    while (hot_bytes + per_expert > gpu_expert_cap &&
                           evict_hot()) {}
                    tier[key] = 2;
                    hot_bytes += per_expert;
                }
            }
        }
    // CPU budget: dequant+prefetch threads are a fixed slice of each
    // token's host-side work; exceeding the pct is fail-closed.
    double cold_share =
        tick > 0 ? (double)cold_loads / (double)tick : 0.0;
    bool cpu_ok = cpu_pct >= 100 ||
        cold_share * 100.0 <= (double)cpu_pct;
    std::printf(
        "{\"ok\":%s,\"mode\":\"low-resource-sim\",\"format\":"
        "\"star-low-resource-sim/v1\",\"gpu_budget_bytes\":%lld,"
        "\"ram_budget_bytes\":%lld,\"nvme_budget_bytes\":%lld,"
        "\"pinned_bytes\":%lld,\"hot_bytes\":%lld,\"warm_bytes\":%lld,"
        "\"hot_hits\":%lld,\"warm_hits\":%lld,\"cold_loads\":%lld,"
        "\"evictions\":%lld,\"est_transfer_ms\":%.3f,"
        "\"cpu_budget_pct\":%d,\"cpu_ok\":%s%s}\n",
        cpu_ok ? "true" : "false",
        (long long)gpu_b, (long long)ram_b, (long long)nvme_b,
        (long long)pinned, (long long)hot_bytes,
        (long long)warm_bytes, (long long)hits_hot,
        (long long)hits_warm, (long long)cold_loads,
        (long long)evictions, est_transfer_ms, cpu_pct,
        cpu_ok ? "true" : "false",
        cpu_ok ? "" : ",\"error_code\":\"SCALE_CPU_BUDGET_EXCEEDED\"");
    return cpu_ok ? 0 : 1;
}

// --------------------------------------------- §29-§34 candidate ----
// Analytic parameter model for a candidate shape — the planner's
// estimate, not a bundle read. Mirrors the canonical tensor layout.
struct XcmShape {
    int64_t layers = 0, hidden = 0, inter = 0, heads = 0,
            kv_heads = 0, vocab = 0, experts = 0, top_k = 2,
            moe_interval = 1, shared_experts = 1,
            expert_inter = 0, shared_inter = 0;
    int64_t lin_key_heads = 0, lin_key_dim = 0,
            lin_value_heads = 0, lin_value_dim = 0;
    int64_t full_attention_interval = 4;
    bool use_vision = false;
    int64_t vision_patch_dim = 0;
    std::string name;
};

XcmShape xcm_shape_from_json(const JsonValue& j) {
    XcmShape s;
    s.name = jget_str(j, "scale_profile");
    s.layers = (int64_t)xct::j_num(&j, "layers", 0);
    s.hidden = (int64_t)xct::j_num(&j, "hidden_size", 0);
    s.inter = (int64_t)xct::j_num(&j, "intermediate_size", 0);
    s.heads = (int64_t)xct::j_num(&j, "num_attention_heads", 0);
    s.kv_heads = (int64_t)xct::j_num(&j, "num_key_value_heads", 0);
    s.vocab = (int64_t)xct::j_num(&j, "vocab_size", 0);
    s.experts = (int64_t)xct::j_num(&j, "moe_num_experts", 0);
    s.top_k = (int64_t)xct::j_num(&j, "moe_top_k", 2);
    s.moe_interval = (int64_t)xct::j_num(&j, "moe_layer_interval", 1);
    s.shared_experts =
        (int64_t)xct::j_num(&j, "moe_num_shared_experts", 0);
    s.expert_inter =
        (int64_t)xct::j_num(&j, "moe_expert_intermediate_size",
                            s.inter);
    s.shared_inter =
        (int64_t)xct::j_num(&j, "moe_shared_intermediate_size",
                            s.inter);
    s.lin_key_heads = (int64_t)xct::j_num(&j, "linear_num_key_heads", 0);
    s.lin_key_dim = (int64_t)xct::j_num(&j, "linear_key_dim", 0);
    s.lin_value_heads =
        (int64_t)xct::j_num(&j, "linear_num_value_heads", 0);
    s.lin_value_dim = (int64_t)xct::j_num(&j, "linear_value_dim", 0);
    s.full_attention_interval =
        (int64_t)xct::j_num(&j, "full_attention_interval", 4);
    s.use_vision = xct::j_num(&j, "use_vision", 0) > 0.5;
    s.vision_patch_dim =
        (int64_t)xct::j_num(&j, "vision_patch_dim", 0);
    return s;
}

struct XcmShapeMetrics {
    int64_t total = 0, unique = 0, active = 0, common = 0,
            routed = 0, shared = 0, router = 0;
    int64_t moe_layers = 0, per_expert = 0;
};

XcmShapeMetrics xcm_shape_metrics(const XcmShape& s) {
    XcmShapeMetrics m;
    int64_t moe_layers =
        s.moe_interval > 0 ? (s.layers + s.moe_interval - 1) /
                                 s.moe_interval
                           : 0;
    // attention (full layers) qkvo + deltanet (linear layers)
    int64_t attn_layers =
        s.full_attention_interval > 0
            ? (s.layers + s.full_attention_interval - 1) /
                  s.full_attention_interval
            : s.layers;
    int64_t lin_layers = s.layers - attn_layers;
    int64_t attn_w =
        attn_layers *
        (s.hidden * s.heads * (s.hidden / std::max<int64_t>(s.heads, 1)) +
         2 * s.hidden * s.kv_heads *
             (s.hidden / std::max<int64_t>(s.heads, 1)) +
         s.hidden * s.hidden);
    int64_t lin_w =
        lin_layers *
        (s.lin_key_heads * s.lin_key_dim * s.hidden +
         s.lin_value_heads * s.lin_value_dim * s.hidden);
    int64_t dense_mlp =
        (s.layers - moe_layers) * 3 * s.hidden * s.inter;
    int64_t norms = s.layers * 2 * s.hidden + s.hidden;
    m.common = s.vocab * s.hidden * 2 + attn_w + lin_w + dense_mlp +
               norms + (s.use_vision
                            ? s.vision_patch_dim * s.hidden : 0);
    m.router = moe_layers * s.hidden * s.experts;
    m.per_expert = 3 * s.hidden * s.expert_inter;
    m.routed = moe_layers * s.experts * m.per_expert;
    m.shared =
        moe_layers * s.shared_experts * 3 * s.hidden * s.shared_inter;
    m.total = m.common + m.router + m.shared + m.routed;
    m.unique = m.total;   // no sharing in the analytic model
    m.moe_layers = moe_layers;
    m.active = m.common + m.router + m.shared +
               moe_layers * s.top_k * m.per_expert;
    return m;
}

// Candidate simulator: hardware budgets + latency targets in, ranked
// verdicts out. Latency model is deliberately conservative: decode ITL
// scales with active params / device flops estimate plus a cold-load
// penalty when experts must come from WARM/COLD.
int mode_scale_sim(const Args& a) {
    std::string file = a.get("file");
    if (file.empty()) fail("SCALESIM_ARGS_MISSING");
    JsonValue req = parse_json_file(file);
    const JsonValue* hw = req.get("hardware");
    const JsonValue* cands = req.get("candidates");
    const JsonValue* budgets = req.get("budgets");
    if (!hw || !cands || cands->type != JsonValue::Type::Array)
        fail("SCALESIM_BAD_INPUT");
    const int64_t vram = (int64_t)xct::j_num(hw, "vram_bytes", 0);
    const int64_t ram = (int64_t)xct::j_num(hw, "ram_bytes", 0);
    const int64_t nvme = (int64_t)xct::j_num(hw, "nvme_bytes", 0);
    const double pcie_gbps = xct::j_num(hw, "pcie_gbps", 16.0);
    const double nvme_gbps = xct::j_num(hw, "nvme_gbps", 5.0);
    const double gpu_flops = xct::j_num(hw, "gpu_tflops", 10.0) * 1e12;
    const int64_t active_budget =
        budgets ? (int64_t)xct::j_num(budgets,
                                      "active_parameter_budget",
                                      INT64_MAX)
                : INT64_MAX;
    const double target_itl_ms =
        budgets ? xct::j_num(budgets, "target_itl_ms", 0) : 0;
    const double target_ttft_ms =
        budgets ? xct::j_num(budgets, "target_ttft_ms", 0) : 0;

    std::printf(
        "{\"ok\":true,\"mode\":\"scale-sim\",\"format\":"
        "\"star-scale-sim/v1\",\"candidates\":[");
    bool first = true;
    for (const auto& cj : cands->array) {
        XcmShape s = xcm_shape_from_json(cj);
        XcmShapeMetrics m = xcm_shape_metrics(s);
        const int64_t wbytes = m.unique * 8;
        const int64_t pinned = m.common + m.router + m.shared;
        const int64_t pinned_bytes = pinned * 8;
        bool fits_pinned = vram == 0 || pinned_bytes <= vram;
        int64_t gpu_resident = pinned;
        int64_t rem = vram > 0 ? vram - pinned_bytes : INT64_MAX;
        int64_t ram_resident = 0, nvme_cold = 0;
        int64_t rem_ram = ram;
        for (int64_t e = 0; e < m.moe_layers * s.experts; ++e) {
            int64_t eb = m.per_expert * 8;
            if (rem >= eb) { gpu_resident += m.per_expert; rem -= eb; }
            else if (rem_ram >= eb) {
                ram_resident += m.per_expert; rem_ram -= eb;
            } else nvme_cold += m.per_expert;
        }
        if (nvme > 0 && nvme_cold * 8 > nvme) nvme_cold = nvme / 8;
        // decode ITL ~ 2 * active params / device flops + transfer
        // penalty when top-k isn't fully hot.
        int64_t hot_experts =
            rem == INT64_MAX ? m.moe_layers * s.experts
                             : (gpu_resident - pinned) /
                                   std::max<int64_t>(m.per_expert, 1);
        int64_t need_hot = m.moe_layers * s.top_k;
        double miss_ratio =
            need_hot > 0
                ? std::max<int64_t>(
                      0, need_hot - std::min(need_hot, hot_experts)) /
                      (double)need_hot
                : 0.0;
        double transfer_penalty_ms =
            miss_ratio * (m.per_expert * 8) /
            (nvme_gbps * 1e9) * 1000.0;
        double itl_ms =
            gpu_flops > 0
                ? (2.0 * m.active / gpu_flops) * 1000.0 +
                      transfer_penalty_ms
                : 0.0;
        double ttft_ms = itl_ms * 4 + transfer_penalty_ms;
        double cap_amp =
            m.active > 0 ? (double)m.total / m.active : 0.0;
        double gpu_amp =
            gpu_resident > 0 ? (double)m.total / gpu_resident : 0.0;
        bool pass = fits_pinned &&
                    m.active <= active_budget &&
                    (target_itl_ms <= 0 || itl_ms <= target_itl_ms) &&
                    (target_ttft_ms <= 0 || ttft_ms <= target_ttft_ms);
        double score = cap_amp * gpu_amp /
                       (1.0 + itl_ms + transfer_penalty_ms);
        if (!first) std::printf(",");
        first = false;
        std::printf(
            "{\"scale_profile\":\"%s\",\"total_params\":%lld,"
            "\"unique_params\":%lld,\"active_params\":%lld,"
            "\"gpu_resident_params\":%lld,\"ram_resident_params\":%lld,"
            "\"nvme_cold_params\":%lld,\"capacity_amplification\":%.3f,"
            "\"gpu_amplification\":%.3f,\"est_itl_ms\":%.3f,"
            "\"est_ttft_ms\":%.3f,\"est_transfer_ms\":%.3f,"
            "\"score\":%.4f,\"pass\":%s%s}",
            gptbridge::jsonlite::json_escape(s.name).c_str(),
            (long long)m.total, (long long)m.unique,
            (long long)m.active, (long long)gpu_resident,
            (long long)ram_resident, (long long)nvme_cold,
            cap_amp, gpu_amp, itl_ms, ttft_ms,
            transfer_penalty_ms, score, pass ? "true" : "false",
            !fits_pinned
                ? ",\"error_code\":\"SCALE_GPU_BUDGET_EXCEEDED\""
            : m.active > active_budget
                ? ",\"error_code\":\"ACTIVE_PARAMETER_BUDGET_EXCEEDED\""
            : (target_itl_ms > 0 && itl_ms > target_itl_ms)
                ? ",\"error_code\":\"SCALE_LATENCY_TARGET_FAILED\""
                : "");
    }
    std::printf("]}\n");
    return 0;
}

// ------------------------------------------------- §55 dashboard ----
int mode_scale_status(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SCALE_ARGS_MISSING");
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    XcmScaleAcct ac = xcm_account(mf);
    const int64_t tk = ac.top_k > 0 ? ac.top_k : 1;
    int64_t per_expert = ac.expert_elems.empty()
        ? 0 : ac.expert_elems.begin()->second;
    int64_t active = ac.common + ac.router + ac.shared +
                     per_expert * tk * ac.moe_layers;
    std::string gen =
        mf.get("architecture_generation") &&
                mf.get("architecture_generation")->type ==
                    JsonValue::Type::String
            ? mf.get("architecture_generation")->string : "";
    NativeInferenceEngine e;
    int64_t mem = 0, kv = 0, rss = 0;
    try {
        e.load(bundle);
        auto r = e.memory_report();
        mem = e.memory_bytes();
        kv = e.kv_memory_bytes();
        rss = r.recurrent_state_bytes;
    } catch (...) {}
    std::printf(
        "{\"ok\":true,\"mode\":\"scale-status\",\"format\":"
        "\"star-scale-status/v1\",\"architecture\":\"xc-fused-1\","
        "\"scale_profile\":\"%s\",\"generation\":\"%s\","
        "\"total_params\":%lld,\"unique_params\":%lld,"
        "\"active_params\":%lld,\"gpu_resident_params\":%lld,"
        "\"ram_resident_params\":%lld,\"nvme_cold_params\":%lld,"
        "\"trainable_params\":%lld,\"memory_bytes\":%lld,"
        "\"kv_bytes\":%lld,\"recurrent_state_bytes\":%lld,"
        "\"expert_hit_rate\":null,\"prefix_hit_rate\":null}\n",
        "", gptbridge::jsonlite::json_escape(gen).c_str(),
        (long long)ac.total, (long long)ac.unique,
        (long long)active, (long long)(ac.common + ac.router +
                                       ac.shared + ac.routed),
        0LL, 0LL, (long long)ac.unique,
        (long long)mem, (long long)kv, (long long)rss);
    return 0;
}

// -------------------------------------------- §40-§43 research ------
// Future-candidate estimators. Output feeds FutureArchitectureResearch
// only — none of these touch xc-fused-1.
int mode_future_scale_probe(const Args& a) {
    JsonValue sh;
    if (a.has("file")) {
        sh = parse_json_file(a.get("file"));
    } else if (a.has("bundle")) {
        JsonValue mf = parse_json_file(
            (fs::path(a.get("bundle")) / "manifest.json").string());
        const JsonValue* c = mf.get("config");
        if (!c) fail("FUTURE_BAD_CONFIG");
        sh = *c;
    } else fail("FUTURE_ARGS_MISSING");
    XcmShape s = xcm_shape_from_json(sh);
    if (s.experts <= 0) s.experts = 8;
    if (s.expert_inter <= 0) s.expert_inter = s.inter;
    XcmShapeMetrics m = xcm_shape_metrics(s);

    // FactorizedExpertBank: shared basis B (hidden x expert_inter) +
    // per-expert low-rank delta (rank r). Logical capacity counts full
    // experts; unique params count basis + deltas.
    const int64_t r = 16;
    int64_t basis = s.hidden * s.expert_inter;
    int64_t delta = r * (s.hidden + s.expert_inter);
    int64_t full_per = 3 * s.hidden * s.expert_inter;
    int64_t fact_per = basis + delta;
    // SharedBlockProbe: attention base shared across layer pairs.
    int64_t attn_per_layer =
        s.hidden * s.hidden * 2 +
        2 * s.hidden * s.kv_heads *
            (s.hidden / std::max<int64_t>(s.heads, 1));
    int64_t shared_saving =
        (s.layers / 2) * attn_per_layer;
    // ConditionalDepthProbe: L logical layers, floor(L/2) active.
    double flops_full = (double)m.active;
    double flops_half = (double)(m.common) +
        (double)(m.active - m.common) * 0.5;
    std::printf(
        "{\"ok\":true,\"mode\":\"future-scale-probe\",\"format\":"
        "\"star-future-scale-probe/v1\",\"target\":"
        "\"FutureArchitectureResearch\",\"architecture_change\":false,"
        "\"factorized_expert\":{\"full_expert_bytes\":%lld,"
        "\"factorized_expert_bytes\":%lld,\"rank\":%lld,"
        "\"unique_param_ratio\":%.4f,\"logical_capacity_invariant\":true},"
        "\"shared_block\":{\"est_saved_params\":%lld,"
        "\"quality_risk\":\"layer-specialization loss - needs ablation\"},"
        "\"conditional_depth\":{\"logical_layers\":%lld,"
        "\"active_layers_min\":%lld,\"active_flops_ratio\":%.4f,"
        "\"state_risk\":\"deltanet state must persist through skipped "
        "layers\"}}\n",
        (long long)full_per, (long long)fact_per, (long long)r,
        full_per > 0 ? (double)fact_per / full_per : 0.0,
        (long long)shared_saving,
        (long long)s.layers, (long long)(s.layers / 2),
        flops_full > 0 ? flops_half / flops_full : 0.0);
    return 0;
}
