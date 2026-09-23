// Suite: engine-level KV pool coverage (G95) — real gptbridge_kv_pool C
// ABI and real NativeInferenceEngine KV paths: config, release, reuse,
// position/length tracking, GQA/RoPE via prefill-decode parity, overflow
// fail-closed, reset isolation, prefix reuse, INT8 parity, memory bounds.
#include "suite_model_common.hpp"
#include "harness.hpp"

#include <cstdlib>
#include <cstring>

namespace {

const char* SUITE = "KV_ENGINE_SUITE";

suite_model::xc::NativeInferenceEngine g_engine;

bool ensure_loaded() {
    if (g_engine.loaded()) return true;
    const auto root = suite_model::find_repo_root();
    const auto bundle = suite_model::find_bundle_dir(root);
    if (bundle.empty()) return false;
    g_engine.load(bundle.string());
    return g_engine.loaded();
}

int64_t describe_number(const std::string& describe, const char* key) {
    const std::string tag = std::string("\"") + key + "\":";
    const size_t at = describe.find(tag);
    if (at == std::string::npos) return -1;
    return std::strtoll(describe.c_str() + at + tag.size(), nullptr, 10);
}

suite_model::xc::SamplingConfig greedy_cfg() {
    suite_model::xc::SamplingConfig cfg;
    cfg.do_sample = false;
    return cfg;
}

int64_t argmax(const std::vector<double>& values) {
    return static_cast<int64_t>(std::distance(
        values.begin(), std::max_element(values.begin(), values.end())));
}

bool throws_code(void (*fn)(), const char* code) {
    try {
        fn();
    } catch (const std::exception& error) {
        return std::string(error.what()).find(code) != std::string::npos;
    }
    return false;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "pool_alloc_release_reuse_zeroed") {
        gptbridge_kv_pool* pool = gptbridge_kv_pool_create(64, 0);
        NT_CHECK(pool != nullptr, "pool create");
        const int32_t first = gptbridge_kv_pool_alloc(pool);
        NT_CHECK(first >= 0, "alloc");
        double* key = gptbridge_kv_pool_data(pool, first, 1);
        double* value = gptbridge_kv_pool_data(pool, first, 0);
        NT_CHECK(key != nullptr && value != nullptr, "data pointers");
        key[0] = 7.5;
        value[0] = -3.25;
        gptbridge_kv_pool_release(pool, first);
        NT_CHECK(gptbridge_kv_pool_data(pool, first, 1) == nullptr,
                 "inactive block data denied");
        const int32_t reused = gptbridge_kv_pool_alloc(pool);
        NT_CHECK(reused == first, "free id reused (LIFO)");
        const double* rekey = gptbridge_kv_pool_data(pool, reused, 1);
        const double* revalue = gptbridge_kv_pool_data(pool, reused, 0);
        NT_CHECK(rekey[0] == 0.0 && revalue[0] == 0.0,
                 "reused block zeroed — no cross-owner leakage");
        gptbridge_kv_pool_destroy(pool);
    }
    NT_END_TEST(SUITE, "pool_alloc_release_reuse_zeroed");

    NT_TEST(SUITE, "pool_limit_and_accounting_failclosed") {
        // stride 64 → 64*2*sizeof(double) = 1024 bytes per block.
        gptbridge_kv_pool* pool = gptbridge_kv_pool_create(64, 2048);
        NT_CHECK(pool != nullptr, "pool create");
        NT_CHECK(gptbridge_kv_pool_alloc(pool) >= 0, "block 1");
        NT_CHECK(gptbridge_kv_pool_alloc(pool) >= 0, "block 2");
        NT_CHECK(gptbridge_kv_pool_memory_bytes(pool) == 2048,
                 "memory accounting");
        NT_CHECK(gptbridge_kv_pool_alloc(pool) == -1,
                 "overflow alloc fails closed");
        NT_CHECK(gptbridge_kv_pool_set_limit(pool, 1024) == 0,
                 "shrink below live usage rejected");
        NT_CHECK(gptbridge_kv_pool_set_limit(pool, 4096) == 1,
                 "limit raise accepted");
        NT_CHECK(gptbridge_kv_pool_alloc(pool) >= 0,
                 "alloc resumes after raise");
        gptbridge_kv_pool_destroy(pool);
    }
    NT_END_TEST(SUITE, "pool_limit_and_accounting_failclosed");

    NT_TEST(SUITE, "pool_invalid_access_failclosed") {
        gptbridge_kv_pool* pool = gptbridge_kv_pool_create(16, 0);
        NT_CHECK(pool != nullptr, "pool create");
        NT_CHECK(gptbridge_kv_pool_alloc(nullptr) == -1,
                 "null pool alloc denied");
        NT_CHECK(gptbridge_kv_pool_data(pool, -1, 1) == nullptr,
                 "negative id denied");
        NT_CHECK(gptbridge_kv_pool_data(pool, 9999, 1) == nullptr,
                 "out-of-range id denied");
        const int32_t id = gptbridge_kv_pool_alloc(pool);
        NT_CHECK(id >= 0, "alloc");
        NT_CHECK(gptbridge_kv_pool_data(pool, id, 1) !=
                     gptbridge_kv_pool_data(pool, id, 0),
                 "key and value buffers isolated");
        gptbridge_kv_pool_destroy(pool);
        NT_CHECK(gptbridge_kv_pool_memory_bytes(nullptr) == 0,
                 "null pool memory zero");
    }
    NT_END_TEST(SUITE, "pool_invalid_access_failclosed");

    NT_TEST(SUITE, "engine_kv_initialized_gqa_rope_config") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        const std::string d = g_engine.describe();
        NT_CHECK(describe_number(d, "kv_heads") > 0, "kv_heads reported");
        NT_CHECK(describe_number(d, "heads") >=
                     describe_number(d, "kv_heads"),
                 "GQA: kv heads <= q heads");
        NT_CHECK(g_engine.kv_memory_bytes() >= 0, "kv pool accounted");
    }
    NT_END_TEST(SUITE, "engine_kv_initialized_gqa_rope_config");

    NT_TEST(SUITE, "prefill_decode_matches_full_forward") {
        // Full-forward (logits(), no KV writes) vs prefill+decode KV path:
        // position/RoPE/length tracking must produce identical argmax.
        NT_CHECK(ensure_loaded(), "engine loaded");
        const auto ids = g_engine.encode("星澄模型管理本地推論", true, false, 0);
        NT_CHECK(ids.size() >= 3, "prompt tokens");
        const auto full = g_engine.logits(ids);
        const int64_t expect = argmax(full);
        const auto out = g_engine.generate(ids, 1, greedy_cfg());
        NT_CHECK(out.size() == 1, "one token generated");
        NT_CHECK(out[0] == expect,
                 "KV prefill+decode argmax == full-forward argmax");
    }
    NT_END_TEST(SUITE, "prefill_decode_matches_full_forward");

    NT_TEST(SUITE, "reset_isolation_and_position_tracking") {
        // reset_cache per generate: repeated runs identical; an unrelated
        // prompt between runs must not contaminate the KV window.
        NT_CHECK(ensure_loaded(), "engine loaded");
        const auto a = g_engine.encode("系統狀態查詢", true, false, 0);
        const auto b = g_engine.encode("今天天氣如何", true, false, 0);
        const auto first = g_engine.generate(a, 8, greedy_cfg());
        g_engine.generate(b, 8, greedy_cfg());
        const auto second = g_engine.generate(a, 8, greedy_cfg());
        NT_CHECK(first == second,
                 "generate(a) stable across unrelated generate(b)");
        NT_CHECK(!first.empty(), "tokens produced");
    }
    NT_END_TEST(SUITE, "reset_isolation_and_position_tracking");

    NT_TEST(SUITE, "prefix_reuse_bit_identical") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        const std::string shared =
            "星澄模型的治理架構包含多層契約與受管更新流程，";
        const auto p1 = g_engine.encode(shared + "請摘要第一點", true, false, 0);
        const auto p2 = g_engine.encode(shared + "請摘要第二點", true, false, 0);
        // Cache disabled baseline.
        g_engine.set_prefix_cache_limit(0, 0);
        const auto base1 = g_engine.generate(p1, 8, greedy_cfg());
        const auto base2 = g_engine.generate(p2, 8, greedy_cfg());
        // Cache enabled: second shared-prefix prompt must hit and stay
        // bit-identical to the uncached run.
        g_engine.set_prefix_cache_limit(8, 256LL * 1024 * 1024);
        const auto cached1 = g_engine.generate(p1, 8, greedy_cfg());
        const auto cached2 = g_engine.generate(p2, 8, greedy_cfg());
        const std::string d = g_engine.describe();
        NT_CHECK(describe_number(d, "prefix_cache_hits") >= 1,
                 "shared prefix hit recorded");
        NT_CHECK(cached1 == base1 && cached2 == base2,
                 "prefix-restored KV produces identical tokens");
    }
    NT_END_TEST(SUITE, "prefix_reuse_bit_identical");

    NT_TEST(SUITE, "kv_memory_limit_failclosed") {
        const auto root = suite_model::find_repo_root();
        const auto bundle = suite_model::find_bundle_dir(root);
        NT_CHECK(!bundle.empty(), "bundle found");
        // Worst-case footprint check at load: a limit below the configured
        // KV footprint must refuse the load, never degrade silently.
        suite_model::xc::NativeInferenceEngine limited;
        limited.set_kv_memory_limit(1);
        bool refused = false;
        try {
            limited.load(bundle.string());
        } catch (const std::exception& error) {
            refused = std::string(error.what()).find(
                          "KV_MEMORY_LIMIT_EXCEEDED") != std::string::npos;
        }
        NT_CHECK(refused, "load under KV limit throws KV_MEMORY_LIMIT_EXCEEDED");
        NT_CHECK(!limited.loaded(), "refused engine not loaded");
    }
    NT_END_TEST(SUITE, "kv_memory_limit_failclosed");

    NT_TEST(SUITE, "sequence_overflow_failclosed") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        // max_position_embeddings from the bundle manifest: prompt longer
        // than the context window must fail closed, not wrap KV slots.
        const auto root = suite_model::find_repo_root();
        const auto bundle = suite_model::find_bundle_dir(root);
        std::string text = suite_model::read_text_file(bundle / "manifest.json");
        const std::string tag = "\"max_position_embeddings\"";
        const size_t at = text.find(tag);
        NT_CHECK(at != std::string::npos, "manifest max_position_embeddings");
        const size_t colon = text.find(':', at + tag.size());
        const int64_t max_pos =
            std::strtoll(text.c_str() + colon + 1, nullptr, 10);
        NT_CHECK(max_pos > 0, "positive context window");
        std::vector<int64_t> overflow(static_cast<size_t>(max_pos + 1), 1);
        bool refused = false;
        try {
            g_engine.generate(overflow, 1, greedy_cfg());
        } catch (const std::exception& error) {
            refused = std::string(error.what()).find(
                          "SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS") !=
                      std::string::npos;
        }
        NT_CHECK(refused, "overflow prompt fails closed");
    }
    NT_END_TEST(SUITE, "sequence_overflow_failclosed");

    NT_TEST(SUITE, "int8_kv_prefill_agreement_and_memory_saving") {
        // A567: INT8 quantization error is measured separately — the gate
        // is that packed KV must not flip the first decode step (prefill
        // read path sanity floor) and must actually shrink the pool.
        NT_CHECK(ensure_loaded(), "engine loaded");
        const std::vector<std::string> texts = {
            "星澄模型的推論管線", "系統維護視窗的用途", "資料庫備援策略",
        };
        std::vector<std::vector<int64_t>> prompts;
        for (const auto& t : texts)
            prompts.push_back(g_engine.encode(t, true, false, 0));
        std::vector<int64_t> fp_first;
        for (const auto& p : prompts) {
            const auto out = g_engine.generate(p, 1, greedy_cfg());
            NT_CHECK(out.size() == 1, "fp64 token generated");
            fp_first.push_back(out[0]);
        }

        // Fresh engines for a symmetric footprint comparison: the pool
        // recycles released blocks, so deltas on a warm engine are zero.
        suite_model::xc::NativeInferenceEngine fp64_fresh;
        const auto root = suite_model::find_repo_root();
        fp64_fresh.load(suite_model::find_bundle_dir(root).string());
        NT_CHECK(fp64_fresh.loaded(), "fp64 reference engine loaded");
        fp64_fresh.generate(prompts[0], 1, greedy_cfg());
        const int64_t fp_mem = fp64_fresh.kv_memory_bytes();

        _putenv("XINGCHENG_CPP_KV_INT8=1");
        suite_model::xc::NativeInferenceEngine int8_engine;
        int8_engine.load(suite_model::find_bundle_dir(root).string());
        _putenv("XINGCHENG_CPP_KV_INT8=");
        NT_CHECK(int8_engine.loaded(), "int8 engine loaded");
        int64_t agreed = 0;
        for (size_t i = 0; i < prompts.size(); ++i) {
            const auto out = int8_engine.generate(prompts[i], 1, greedy_cfg());
            NT_CHECK(out.size() == 1, "int8 token generated");
            if (out[0] == fp_first[i]) ++agreed;
        }
        const int64_t q_mem = int8_engine.kv_memory_bytes();
        nt_detail = "first_token_agreement=" + std::to_string(agreed) +
                    "/" + std::to_string(prompts.size()) +
                    " fp64_kv_bytes=" + std::to_string(fp_mem) +
                    " int8_kv_bytes=" + std::to_string(q_mem);
        NT_CHECK(agreed == static_cast<int64_t>(prompts.size()),
                 nt_detail.c_str());
        NT_CHECK(q_mem > 0 && fp_mem > 0 && q_mem < fp_mem,
                 nt_detail.c_str());
    }
    NT_END_TEST(SUITE, "int8_kv_prefill_agreement_and_memory_saving");

    NT_TEST(SUITE, "batch_releases_finished_kv") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        const std::vector<std::vector<int64_t>> prompts = {
            g_engine.encode("說明索引用途", true, false, 0),
            g_engine.encode("說明備援策略", true, false, 0),
        };
        const int64_t before = g_engine.kv_memory_bytes();
        const auto outs = g_engine.generate_batch(prompts, 6, greedy_cfg());
        NT_CHECK(outs.size() == 2, "batch results");
        const int64_t after = g_engine.kv_memory_bytes();
        NT_CHECK(after - before <
                     static_cast<int64_t>(256) * 1024 * 1024,
                 "KV footprint stays bounded across batch");
        // A following single-sequence run reuses the pool cleanly.
        const auto solo = g_engine.encode("狀態確認", true, false, 0);
        const auto a = g_engine.generate(solo, 6, greedy_cfg());
        const auto b = g_engine.generate(solo, 6, greedy_cfg());
        NT_CHECK(a == b, "post-batch single generate deterministic");
    }
    NT_END_TEST(SUITE, "batch_releases_finished_kv");

    return native_tests::report("kv_engine_suite.json");
}
