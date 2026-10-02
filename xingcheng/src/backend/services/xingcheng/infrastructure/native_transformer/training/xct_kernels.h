// xct_kernels.h —star-kernel-registry + star-kernel-policy.
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_util.h and xct_tpu.h (needs ModelConfig, g_tpu, tpu_threads,
// tpu_has_avx2_fma) and before xct_job.h (run_job calls
// kernel_policy_enforce).
//
// The registry is the governed inventory of every compute kernel the
// trainer lane owns: name, category, model family gate, determinism
// contract, implementation variants and the dispatch rule that selects
// between them. Phase 1 keeps dispatch where it is (the lanes in
// xct_tpu.h/xct_math.h/xct_backward.h); the registry emits the resolved
// variant per kernel and enforces star-kernel-policy pins
// fail-closed at job start. Phase 2 promotes the table to the single
// dispatch authority.
//
// Policy file (star-kernel-policy, runtime/settings/kernel-policy.json):
//   {"format":"star-kernel-policy","enabled":true,
//    "force_serial":false,"max_threads":0,
//    "deny_variants":["simd"|"tile4"|"cuda"],
//    "deny_kernels":["<registry name>", ...]}
// Resolution order: --kernel-policy <path> arg, else XCT_KERNEL_POLICY
// env, else no policy. A referenced-but-unreadable or malformed policy
// is a hard fail —never silently ignored. A policy whose format tag or
// enabled flag is wrong is likewise rejected. deny_kernels refuses any
// job whose model/task activates the denied kernel's family;
// deny_variants pin the affected lanes off; force_serial/max_threads
// bound the lane pool.

#if defined(XINGCHENG_CUDA)
extern "C" int xcuda_adamw_probe();
#else
static int xcuda_adamw_probe() { return 0; }
#endif

struct KernelVariant {
    const char* id;
    const char* req;  // space-separated caps: avx2 | cuda | ""
    const char* parity;    // bitwise | order-fixed | device-cert-pending
};

struct KernelEntry {
    const char* name;
    const char* category;
    const char* family;       // core|deltanet|attn|mla|csa|moe|mtp|
                              // vision|gemma4|dpo|grpo
    const char* determinism;  // bitwise | order-fixed
    const char* dispatch;     // rule summary (audit text)
    std::vector<KernelVariant> variants;
};

// ---------------------------------------------------------------- table --
static const KernelEntry kKernelRegistry[] = {
    // ---- scheduling substrate ----
    {"lane_dispatch", "SCHED", "core", "lane-count invariant",
     "n>=64 or n*cost>=32K joins the TpuPool; serial otherwise",
     {
         {"serial", "", "bitwise"},
         {"tpu-pool", "", "bitwise"}}},

    // ---- GEMM ----
    {"linear_fwd", "GEMM", "core", "bitwise",
     "tile4 at T>=16 && T*O<16M; tile2 at large-out; legacy small-T "
     "(XCT_TPU_TILE4=0 pins legacy for A/B)",
     {{"scalar-legacy", "", "bitwise-oracle"},
                                {"avx2-tile4", "avx2", "bitwise"},
                                {"avx2-tile2", "avx2", "bitwise"}}},
    {"linear_bwd", "GEMM", "core", "bitwise",
     "tile4 dW/dx lanes at T>=16; legacy row partitions otherwise",
     {{"scalar-legacy", "", "bitwise-oracle"},
                                {"avx2-tile4", "avx2", "bitwise"}}},// ---- normalization / position ----
    {"rmsnorm_fwd", "NORM", "core", "bitwise",
     "row lanes over T*rows",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"rmsnorm_bwd", "NORM", "core", "bitwise",
     "two-phase: row lanes for dss, column lanes for dw",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"l2norm_fwd", "NORM", "core", "bitwise",
     "row lanes; deltanet q/k normalize",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"rope", "POS", "core", "bitwise",
     "(t,h) lanes; rope_hf_partial for decoupled rope dims",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},// ---- attention ----
    {"attn_fwd", "ATTN", "attn", "bitwise",
     "per-head lanes over blocked t-quads (tpu_attn_fwd); serial rows "
     "when heads*work below floor",
     {{"scalar-rows", "", "bitwise-oracle"},
                                {"avx2-blocked4", "avx2", "bitwise"}}},
    {"attn_bwd", "ATTN", "attn", "bitwise",
     "kv-head-group lanes (dk/dv slices disjoint); 4-row blocked "
     "tpu_attn_bwd; CSA layers keep nsel extras",
     {{"scalar-rows", "", "bitwise-oracle"},
                                {"avx2-blocked4", "avx2", "bitwise"}}},// ---- deltanet ----
    {"conv1d_causal_fwd", "CONV", "deltanet", "bitwise",
     "channel lanes over depthwise causal conv",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"conv1d_causal_bwd", "CONV", "deltanet", "bitwise",
     "channel lanes; dx/dw channel-disjoint",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"deltanet_scan_fwd", "SCAN", "deltanet", "bitwise",
     "v-head lanes; per-head fp32 recurrent state stays lane-private",
     {{"serial-scan", "", "bitwise-oracle"},
                                {"head-lane", "", "bitwise"}}},
    {"deltanet_scan_bwd", "SCAN", "deltanet", "bitwise",
     "v-head lanes; reverse recurrent carry per head",
     {{"serial-scan", "", "bitwise-oracle"},
                                {"head-lane", "", "bitwise"}}},// ---- elementwise / loss / embed ----
    {"elementwise", "ELEMENTWISE", "core", "bitwise",
     "flat index lanes (tpu_elementwise)",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"ce_loss", "LOSS", "core", "bitwise",
     "row lanes for logits grad; loss merged serially in row order",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"seq_logprob", "LOSS", "core", "bitwise",
     "row lanes; per-token logprob identical to serial",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"embed_lookup", "EMBED", "core", "bitwise",
     "row lanes gather token rows",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"embed_scatter", "EMBED", "core", "bitwise",
     "H-column lanes; per-element i-ascending accumulation",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},// ---- optimizer ----
    {"adamw_step", "OPTIM", "core", "bitwise",
     "AVX2 fused lane; CUDA batch pipeline only when "
     "XINGCHENG_TRAINER_CUDA_OPT is set and the probe passes "
     "(cert-pending); scalar fallback per tensor",
     {{"scalar", "", "bitwise-oracle"},
                                {"avx2", "avx2", "bitwise"},
                                {"cuda-fused", "cuda", "device-cert-pending"}}},
    {"grad_clip", "REDUCE", "core", "order-fixed",
     "fixed-partition chunked sumsq; associativity fixed at build",
     {{"scalar", "", "bitwise-oracle"},
                                {"avx2-chunked", "avx2", "order-fixed"}}},// ---- MoE ----
    {"moe_route", "EXPERT", "moe", "bitwise",
     "token lanes for gate probs + top-k; counts merged serially",
     {{"serial-topk", "", "bitwise"},
                                {"token-lane", "", "bitwise"}}},
    {"moe_expert_mlp", "EXPERT", "moe", "bitwise",
     "grouped expert GEMM via linear lanes; slot caches per example",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},// ---- MLA / CSA ----
    {"mla_attn_fwd", "ATTN", "mla", "bitwise",
     "nope+rope split score4 into shared blocked core; per-head lanes",
     {{"scalar-rows", "", "bitwise-oracle"},
                                {"avx2-blocked4", "avx2", "bitwise"}}},
    {"mla_attn_bwd", "ATTN", "mla", "bitwise",
     "64-chunk lanes with private dkr buffers, ordered merge "
     "(rope-key gradient is head-shared)",
     {{"serial-rows", "", "bitwise-oracle"},
                                {"chunk-lane-merge", "", "bitwise"}}},
    {"csa_indexer", "INDEX", "csa", "bitwise",
     "compressed-KV latent indexer; nc lanes",
     {{"serial", "", "bitwise"},
                                {"nc-lane", "", "bitwise"}}},
    {"csa_attn_bwd", "ATTN", "csa", "bitwise",
     "kv-group lanes; compressed-KV grads sliced per kv head",
     {{"serial-rows", "", "bitwise-oracle"},
                                {"kvgroup-lane", "", "bitwise"}}},// ---- MTP stack ----
    {"mtp_stack_fwd", "STACK", "mtp", "bitwise",
     "per-depth MTP stack; head lanes for stack attention",
     {{"serial", "", "bitwise-oracle"},
                                {"head-lane", "", "bitwise"}}},
    {"mtp_stack_bwd", "STACK", "mtp", "bitwise",
     "kv-head-group lanes (dk/dv disjoint per group owner)",
     {{"serial", "", "bitwise-oracle"},
                                {"kvgroup-lane", "", "bitwise"}}},// ---- vision / gemma4 / rl tasks ----
    {"vision_embed", "EMBED", "vision", "bitwise",
     "patch embed projection via linear lanes",
     {{"scalar", "", "bitwise"},
                                {"avx2", "avx2", "bitwise"}}},
    {"g4_hybrid_attn", "ATTN", "gemma4", "bitwise",
     "local/global mix with shared-KV owner lanes; kv-group bwd",
     {{"serial-rows", "", "bitwise-oracle"},
                                {"lane-parallel", "", "bitwise"}}},
    {"dpo_soft_grad", "LOSS", "dpo", "bitwise",
     "row lanes for policy/ref logprob grads",
     {{"scalar", "", "bitwise"},
                                {"row-lane", "", "bitwise"}}},
    {"grpo_rollout", "STACK", "grpo", "bitwise",
     "per-rollout autoregressive forward; seeded sampling",
     {{"serial", "", "bitwise"}}},
};

// ------------------------------------------------------------- policy --

struct KernelPolicy {
    bool loaded = false;
    bool enabled = true;
    bool force_serial = false;
    int max_threads = 0;
    // Accelerator-plane pins (optional; 0 = built-in default stands):
    // dev_min_flops raises the device-GEMM work floor, vram_reserve_mb
    // raises the live-VRAM headroom every offload must preserve.
    int64_t dev_min_flops = 0;
    int64_t vram_reserve_mb = 0;
    std::vector<std::string> deny_variants;
    std::vector<std::string> deny_kernels;
    std::string source;
};

// CLI/env-selected policy path. main() may set --kernel-policy; the env
// var XCT_KERNEL_POLICY overrides nothing —the explicit arg wins.
static std::string g_kernel_policy_arg;

static std::string kernel_policy_path() {
    if (!g_kernel_policy_arg.empty()) return g_kernel_policy_arg;
    const char* e = std::getenv("XCT_KERNEL_POLICY");
    return e ? std::string(e) : std::string();
}

static KernelPolicy kernel_policy_load(const std::string& path) {
    KernelPolicy pol;
    pol.source = path;
    if (path.empty()) return pol;
    {
        std::ifstream probe(path, std::ios::binary);
        if (!probe)
            throw "kernel-policy: file unreadable (fail-closed)";
    }
    std::string text = slurp(path);
    JsonValue root;
    try {
        root = JsonParser(text).parse();
    } catch (...) {
        throw "kernel-policy: JSON_PARSE (fail-closed)";
    }
    const std::string fmt = j_str(&root, "format", "");
    if (fmt != "star-kernel-policy")
        throw "kernel-policy: FORMAT_MISMATCH (fail-closed)";
    pol.loaded = true;
    pol.enabled = j_bool(&root, "enabled", true);
    pol.force_serial = j_bool(&root, "force_serial", false);
    pol.max_threads = j_int(&root, "max_threads", 0);
    pol.dev_min_flops = (int64_t)j_num(&root, "dev_min_flops", 0);
    pol.vram_reserve_mb = (int64_t)j_num(&root, "vram_reserve_mb", 0);
    if (const JsonValue* dv = root.get("deny_variants"))
        if (dv->type == JsonValue::Type::Array)
            for (const auto& e : dv->array)
                if (e.type == JsonValue::Type::String)
                    pol.deny_variants.push_back(e.string);
    if (const JsonValue* dk = root.get("deny_kernels"))
        if (dk->type == JsonValue::Type::Array)
            for (const auto& e : dk->array)
                if (e.type == JsonValue::Type::String)
                    pol.deny_kernels.push_back(e.string);
    return pol;
}

// The kernel families a (config, task) pair will actually dispatch.
// deny_kernels is meaningful only against this set —denying a family
// the job never reaches is recorded but inert.
static std::unordered_set<std::string>
kernel_active_families(const ModelConfig& c, const std::string& task) {
    std::unordered_set<std::string> f = {"core", "attn"};
    bool deltanet = false, mla = false, csa = false, attn = false;
    for (int l = 0; l < c.layers; ++l) {
        if (c.is_linear(l)) { deltanet = true; continue; }
        attn = true;
        if (c.is_mla(l)) mla = true;
        if (c.use_csa(l)) csa = true;
    }
    if (!attn) f.erase("attn");
    if (deltanet) f.insert("deltanet");
    if (mla) f.insert("mla");
    if (csa) f.insert("csa");
    if (c.moe_experts > 0) f.insert("moe");
    if (c.mtp_depth > 0) f.insert("mtp");
    if (c.use_vision) f.insert("vision");
    if (c.is_gemma4()) f.insert("gemma4");
    if (task == "dpo") f.insert("dpo");
    if (task == "grpo") f.insert("grpo");
    return f;
}

static bool kernel_denied(const KernelPolicy& pol, const char* name) {
    for (const auto& d : pol.deny_kernels)
        if (d == name) return true;
    return false;
}

// Fail-closed gate executed inside run_job after lane config lands.
// Variant pins apply first (they only narrow dispatch); a denied kernel
// whose family the job activates is a hard refusal.
static void kernel_policy_enforce(const ModelConfig& c,
                                  const std::string& task) {
    KernelPolicy pol = kernel_policy_load(kernel_policy_path());
    if (!pol.loaded || !pol.enabled) return;
    for (const auto& v : pol.deny_variants) {
        if (v == "simd") g_tpu.simd = false;
        else if (v == "tile4") g_tpu.tile4 = false;
        else if (v == "cuda") g_accel.cuda_denied = true;
        // "cuda" is also re-checked live where the device lane probes
        // (adamw_step checks kernel_policy_cuda_denied(), the GEMM lane
        // binds it inside accel_detect).
    }
    if (pol.force_serial) g_tpu.threads = 1;
    else if (pol.max_threads > 0 &&
             (g_tpu.threads <= 0 || g_tpu.threads > pol.max_threads))
        g_tpu.threads = pol.max_threads;
    // Accelerator-plane pins (optional; apply before the first dispatch).
    if (pol.dev_min_flops > 0) g_accel.dev_min_flops = pol.dev_min_flops;
    if (pol.vram_reserve_mb > 0)
        g_accel.vram_reserve_mb = pol.vram_reserve_mb;
    const auto fams = kernel_active_families(c, task);
    for (const auto& e : kKernelRegistry) {
        if (!kernel_denied(pol, e.name)) continue;
        if (fams.count(e.family) == 0) continue;
        static thread_local std::string msg;
        msg = std::string("KERNEL_POLICY_DENIED: ") + e.name;
        throw msg.c_str();
    }
}

static bool kernel_policy_cuda_denied() {
    static const bool denied = [] {
        KernelPolicy pol;
        try {
            pol = kernel_policy_load(kernel_policy_path());
        } catch (...) {
            return true;  // unreadable policy fails the device lane closed
        }
        if (!pol.loaded || !pol.enabled) return false;
        for (const auto& v : pol.deny_variants)
            if (v == "cuda") return true;
        return kernel_denied(pol, "adamw_step");
    }();
    return denied;
}

// -------------------------------------------------------------- emit --

static bool kernel_variant_eligible(const KernelVariant& v) {
    const std::string req = v.req;
    if (req.empty()) return true;
    if (req == "avx2") return tpu_has_avx2_fma();
    if (req == "cuda")
        return std::getenv("XINGCHENG_TRAINER_CUDA_OPT") != nullptr &&
               xcuda_adamw_probe() != 0 &&
               !kernel_policy_cuda_denied();
    return false;
}

// deny_variants pins applied to eligibility so "active" reports the
// variant that would actually dispatch under policy: "simd" kills every
// req=avx2 lane, "tile4" kills the register-tiled GEMM, "cuda" kills
// every req=cuda lane (also enforced live by kernel_policy_cuda_denied).
static bool kernel_variant_pinned(const KernelPolicy& pol,
                                  const KernelVariant& v) {
    if (!pol.loaded || !pol.enabled) return false;
    for (const auto& d : pol.deny_variants) {
        if (d == "simd" && std::string(v.req) == "avx2") return true;
        if (d == "tile4" && std::string(v.id).find("tile4") !=
                               std::string::npos)
            return true;
        if (d == "cuda" && std::string(v.req) == "cuda") return true;
    }
    return false;
}

static std::string kernel_active_variant(const KernelEntry& e,
                                         const KernelPolicy& pol) {
    std::string active = e.variants[0].id;
    for (int i = 1; i < (int)e.variants.size(); ++i)
        if (kernel_variant_eligible(e.variants[i]) &&
            !kernel_variant_pinned(pol, e.variants[i]))
            active = e.variants[i].id;
    return active;
}

static std::string jesc(const std::string& s) {
    std::string o;
    for (char ch : s) {
        if (ch == '\\' || ch == '"') { o += '\\'; o += ch; }
        else if (ch == '\n') o += "\\n";
        else o += ch;
    }
    return o;
}

static int kernel_registry_emit() {
    KernelPolicy pol;
    std::string pol_err;
    try {
        pol = kernel_policy_load(kernel_policy_path());
    } catch (const char* e) {
        pol_err = e;
    }
    const bool simd = tpu_has_avx2_fma();
    const bool cuda = std::getenv("XINGCHENG_TRAINER_CUDA_OPT") !=
                          nullptr && xcuda_adamw_probe() != 0;
    std::ostringstream o;
    o << "{\"format\":\"star-kernel-registry\",\"lane\":\"trainer\","
      << "\"count\":"
      << (int)(sizeof(kKernelRegistry) / sizeof(kKernelRegistry[0]))
      << ",\"caps\":{\"avx2_fma\":" << (simd ? "true" : "false")
      << ",\"cuda_adamw\":" << (cuda ? "true" : "false")
      << ",\"threads\":" << tpu_threads()
      << ",\"simd_enabled\":" << (g_tpu.simd ? "true" : "false")
      << ",\"tile4_enabled\":" << (g_tpu.tile4 ? "true" : "false")
      << "},\"policy\":{"
      << "\"source\":\"" << jesc(kernel_policy_path()) << "\","
      << "\"loaded\":" << (pol.loaded ? "true" : "false")
      << ",\"enabled\":" << (pol.enabled ? "true" : "false")
      << ",\"force_serial\":" << (pol.force_serial ? "true" : "false")
      << ",\"max_threads\":" << pol.max_threads
      << ",\"deny_variants\":[";
    bool first = true;
    for (const auto& v : pol.deny_variants) {
        o << (first ? "" : ",") << "\"" << jesc(v) << "\"";
        first = false;
    }
    o << "],\"deny_kernels\":[";
    first = true;
    for (const auto& v : pol.deny_kernels) {
        o << (first ? "" : ",") << "\"" << jesc(v) << "\"";
        first = false;
    }
    o << "],\"error\":"
      << (pol_err.empty() ? "null" : "\"" + jesc(pol_err) + "\"")
      << "},\"accel\":{";
    // The accel plane is part of the same contract: caps are detected
    // once, the resolved lane reflects policy pins, and the counters
    // stay at zero (emit runs before any dispatch). The deny flag is
    // resolved before detect — unreadable policy fails the lane closed,
    // same posture as kernel_policy_cuda_denied.
    if (!pol_err.empty() ||
        (pol.loaded && pol.enabled &&
         std::find(pol.deny_variants.begin(), pol.deny_variants.end(),
                   "cuda") != pol.deny_variants.end()))
        g_accel.cuda_denied = true;
    accel_detect();
    if (pol.loaded && pol.enabled) {
        if (pol.dev_min_flops > 0)
            g_accel.dev_min_flops = pol.dev_min_flops;
        if (pol.vram_reserve_mb > 0)
            g_accel.vram_reserve_mb = pol.vram_reserve_mb;
    }
    accel_refresh_mem();
    o << "\"cuda_opt\":" << (g_accel.cuda_opt ? "true" : "false")
      << ",\"cuda_dev\":" << (g_accel.cuda_dev ? "true" : "false")
      << ",\"cuda_denied\":" << (g_accel.cuda_denied ? "true" : "false")
      << ",\"cuda_lane\":" << (g_accel.cuda ? "true" : "false")
      << ",\"cc\":\"" << g_accel.cc_major << "." << g_accel.cc_minor
      << "\",\"sm_count\":" << g_accel.sm_count
      << ",\"dev_min_flops\":" << g_accel.dev_min_flops
      << ",\"vram_reserve_mb\":" << g_accel.vram_reserve_mb
      << ",\"ram_total_mb\":" << g_accel.ram_total_mb
      << ",\"ram_free_mb\":" << g_accel.ram_free_mb
      << ",\"vram_total_mb\":" << g_accel.vram_total_mb
      << ",\"vram_free_mb\":" << g_accel.vram_free_mb
      << "},\"kernels\":[";
    first = true;
    for (const auto& e : kKernelRegistry) {
        o << (first ? "" : ",")
          << "{\"name\":\"" << e.name << "\",\"category\":\""
          << e.category << "\",\"family\":\"" << e.family
          << "\",\"determinism\":\"" << e.determinism
          << "\",\"dispatch\":\"" << e.dispatch
          << "\",\"active\":\""
          << (pol.loaded && pol.enabled && kernel_denied(pol, e.name)
                  ? "denied" : kernel_active_variant(e, pol))
          << "\",\"variants\":[";
        for (int i = 0; i < (int)e.variants.size(); ++i) {
            const auto& v = e.variants[i];
            o << (i ? "," : "") << "{\"id\":\"" << v.id
              << "\",\"requires\":\"" << v.req
              << "\",\"parity\":\"" << v.parity << "\"}";
        }
        o << "]}";
        first = false;
    }
    o << "]}\n";
    std::printf("%s", o.str().c_str());
    return 0;
}

// --accel-plane: emit star-accel-plane — the resolved single dynamic
// accelerator surface for this host. Detection is live (caps + memory
// now), the resolved block is what dispatch will honour under the
// active policy, and counters start at zero until a job runs.
static int accel_plane_emit() {
    KernelPolicy pol;
    std::string pol_err;
    try {
        pol = kernel_policy_load(kernel_policy_path());
    } catch (const char* e) {
        pol_err = e;
    }
    // Deny flag resolves before detect — same fail-closed posture as
    // the job path (unreadable policy denies the device lane).
    if (!pol_err.empty() ||
        (pol.loaded && pol.enabled &&
         std::find(pol.deny_variants.begin(), pol.deny_variants.end(),
                   "cuda") != pol.deny_variants.end()))
        g_accel.cuda_denied = true;
    accel_detect();
    if (pol.loaded && pol.enabled) {
        if (pol.dev_min_flops > 0)
            g_accel.dev_min_flops = pol.dev_min_flops;
        if (pol.vram_reserve_mb > 0)
            g_accel.vram_reserve_mb = pol.vram_reserve_mb;
    }
    accel_refresh_mem();
    std::ostringstream o;
    o << "{\"ok\":" << (pol_err.empty() ? "true" : "false")
      << ",\"format\":\"star-accel-plane\",\"lane\":\"trainer\","
      << "\"cpu\":{\"cores\":" << (int)std::thread::hardware_concurrency()
      << ",\"threads\":" << tpu_threads()
      << ",\"simd\":\"" << tpu_simd_name() << "\""
      << ",\"tile4\":" << (g_tpu.tile4 ? "true" : "false")
      << ",\"ram_total_mb\":" << g_accel.ram_total_mb
      << ",\"ram_free_mb\":" << g_accel.ram_free_mb
      << "},\"gpu\":{\"available\":"
      << (g_accel.cuda_dev ? "true" : "false")
      << ",\"cc\":\"" << g_accel.cc_major << "." << g_accel.cc_minor
      << "\""
      << ",\"sm_count\":" << g_accel.sm_count
      << ",\"vram_total_mb\":" << g_accel.vram_total_mb
      << ",\"vram_free_mb\":" << g_accel.vram_free_mb
      << "},\"resolved\":{\"cuda_lane\":"
      << (g_accel.cuda ? "true" : "false")
      << ",\"cuda_opt_in\":" << (g_accel.cuda_opt ? "true" : "false")
      << ",\"cuda_policy_denied\":"
      << (g_accel.cuda_denied ? "true" : "false")
      << ",\"dev_min_flops\":" << g_accel.dev_min_flops
      << ",\"vram_reserve_mb\":" << g_accel.vram_reserve_mb
      << ",\"force_serial\":" << (pol.force_serial ? "true" : "false")
      << ",\"max_threads\":" << pol.max_threads
      << "},\"counters\":{\"dev_calls\":" << g_accel.dev_calls
      << ",\"denied_off\":" << g_accel.dev_denied_off
      << ",\"denied_work\":" << g_accel.dev_denied_work
      << ",\"denied_vram\":" << g_accel.dev_denied_vram
      << "},\"policy\":{\"source\":\"" << jesc(kernel_policy_path()) << "\""
      << ",\"loaded\":" << (pol.loaded ? "true" : "false")
      << ",\"error\":"
      << (pol_err.empty() ? "null" : "\"" + jesc(pol_err) + "\"")
      << "}}\n";
    std::printf("%s", o.str().c_str());
    return 0;
}
