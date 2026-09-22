// Suite: native model baseline — 真實 cpp bundle 載入、完整性、
// 確定性與反退化生成（§10.60 baseline；取代 BLOCKED_MODEL_RUNTIME）。
#include "suite_model_common.hpp"
#include "harness.hpp"

namespace {

const char* SUITE = "STAR_BASELINE_SUITE";
suite_model::xc::NativeInferenceEngine g_engine;
std::string g_detail;

bool ensure_loaded() {
    if (g_engine.loaded()) return true;
    const auto root = suite_model::find_repo_root();
    const auto bundle = suite_model::find_bundle_dir(root);
    if (bundle.empty()) return false;
    g_engine.load(bundle.string());
    return g_engine.loaded();
}

} // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "bundle_discovered_and_loads") {
        const auto root = suite_model::find_repo_root();
        NT_CHECK(!root.empty(), "repo root with xingcheng dir");
        const auto bundle = suite_model::find_bundle_dir(root);
        NT_CHECK(!bundle.empty(),
                 "cpp-bundles/* manifest+weights+tokenizer");
        NT_CHECK(ensure_loaded(), "engine.load");
        NT_CHECK(g_engine.memory_bytes() > 0, "weights resident");
        NT_CHECK(g_engine.kv_memory_bytes() >= 0,
                 "kv pool accounted");
    }
    NT_END_TEST(SUITE, "bundle_discovered_and_loads");

    NT_TEST(SUITE, "tokenizer_encode_decode_round_trip") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        const std::string text = "星澄是 GPTBridge 的原生模型";
        const auto ids = g_engine.encode(text, true, false, 0);
        NT_CHECK(ids.size() >= 2, "encode produced tokens");
        const std::string back = g_engine.decode(ids, true);
        NT_CHECK(back.find("星澄") != std::string::npos,
                 "decode round-trips content");
        NT_CHECK(g_engine.encode(text, true, false, 0) == ids,
                 "encode deterministic");
    }
    NT_END_TEST(SUITE, "tokenizer_encode_decode_round_trip");

    NT_TEST(SUITE, "logits_finite_over_vocab") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        const auto ids = g_engine.encode("星澄", true, false, 0);
        const auto lg = g_engine.logits(ids);
        NT_CHECK(lg.size() > 1000, "logits cover vocab");
        bool finite = true;
        double mx = -1e300;
        for (const double x : lg) {
            finite = finite && std::isfinite(x);
            mx = std::max(mx, x);
        }
        NT_CHECK(finite, "all logits finite");
        NT_CHECK(std::isfinite(mx), "argmax source finite");
    }
    NT_END_TEST(SUITE, "logits_finite_over_vocab");

    NT_TEST(SUITE, "greedy_generation_deterministic") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        const auto prompt = g_engine.encode("星澄是", true, false, 0);
        suite_model::xc::SamplingConfig greedy;
        greedy.do_sample = false;
        const auto a = g_engine.generate(prompt, 16, greedy);
        const auto b = g_engine.generate(prompt, 16, greedy);
        NT_CHECK(a == b, "greedy run-to-run identical");
        NT_CHECK(!a.empty(), "greedy produced tokens");
    }
    NT_END_TEST(SUITE, "greedy_generation_deterministic");

    NT_TEST(SUITE, "unseen_prompt_non_degenerate") {
        /* 反背誦／反退化：未見提示生成須足量、多樣。 */
        NT_CHECK(ensure_loaded(), "engine loaded");
        const auto prompt = g_engine.encode(
            "請說明維護視窗的用途", true, false, 0);
        suite_model::xc::SamplingConfig greedy;
        greedy.do_sample = false;
        const auto out = g_engine.generate(prompt, 24, greedy);
        NT_CHECK(out.size() >= 8, "sufficient tokens");
        std::set<int64_t> distinct(out.begin(), out.end());
        NT_CHECK(distinct.size() >= 3, "diverse tokens");
        int64_t max_run = 1, run = 1;
        for (size_t i = 1; i < out.size(); ++i) {
            run = (out[i] == out[i - 1]) ? run + 1 : 1;
            max_run = std::max(max_run, run);
        }
        NT_CHECK(max_run < static_cast<int64_t>(out.size()) * 4 / 5,
                 "no degenerate repeat loop");
    }
    NT_END_TEST(SUITE, "unseen_prompt_non_degenerate");

    NT_TEST(SUITE, "sequence_nll_scored_and_finite") {
        NT_CHECK(ensure_loaded(), "engine loaded");
        const auto ids = g_engine.encode(
            "星澄助理提供系統自動更新總開關與結果。", true, false, 0);
        const auto [nll, count] = g_engine.sequence_nll(ids);
        NT_CHECK(count >= 3, "scored tokens");
        NT_CHECK(std::isfinite(nll) && nll > 0.0, "nll finite");
        const double ppl = std::exp(nll / static_cast<double>(count));
        NT_CHECK(std::isfinite(ppl) && ppl > 1.0,
                 "perplexity sane");
    }
    NT_END_TEST(SUITE, "sequence_nll_scored_and_finite");

    return native_tests::report("baseline_suite.json");
}
