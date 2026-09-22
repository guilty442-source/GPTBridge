// Suite: native eval — star-native-eval-v1 規格語義（§10.60 eval；
// 取代 BLOCKED_MODEL_RUNTIME）：held-out ppl（決定性截斷）＋
// require_generation ＋ min_tokens_per_second 閘門。
#include "suite_model_common.hpp"
#include "harness.hpp"

namespace {

const char* SUITE = "STAR_EVAL_SUITE";
const char* SPEC_REL =
    "Standalone tools/local-model/xingcheng/eval/"
    "star-native-eval-v1.json";
/* 截斷上限由規格 eval_token_cap 提供（預設 128；§3.1 ~30s 預算） */

suite_model::xc::NativeInferenceEngine g_engine;
suite_model::EvalSpec g_spec;

bool ensure_loaded() {
    if (!g_engine.loaded()) {
        const auto bundle =
            suite_model::find_bundle_dir(suite_model::find_repo_root());
        if (bundle.empty()) return false;
        g_engine.load(bundle.string());
    }
    return g_engine.loaded();
}

} // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "eval_suite_spec_loads") {
        const auto root = suite_model::find_repo_root();
        NT_CHECK(!root.empty(), "repo root");
        std::string err;
        NT_CHECK(suite_model::load_eval_spec(root / SPEC_REL,
                                             &g_spec, &err),
                 "star-native-eval-v1 spec");
        NT_CHECK(g_spec.suite_id == "star-native-eval-v1",
                 "suite_id");
        NT_CHECK(!g_spec.eval_text.empty(), "eval_text");
    }
    NT_END_TEST(SUITE, "eval_suite_spec_loads");

    NT_TEST(SUITE, "heldout_perplexity_beats_uniform") {
        NT_CHECK(ensure_loaded(), "engine.load");
        const auto ids = suite_model::eval_tokens(
            g_engine, g_spec.eval_text, g_spec.eval_token_cap);
        NT_CHECK(ids.size() >= 32, "eval tokens truncated cap");
        const auto [nll, count] = g_engine.sequence_nll(ids);
        NT_CHECK(count >= 31, "scored tokens");
        NT_CHECK(std::isfinite(nll) && nll > 0.0, "nll finite");
        const double ppl =
            std::exp(nll / static_cast<double>(count));
        NT_CHECK(std::isfinite(ppl) && ppl > 1.0,
                 "perplexity finite");
        /* 誠實下界：held-out ppl 必須勝過 uniform（=vocab）。 */
        NT_CHECK(ppl < 8192.0, "perplexity beats uniform");
    }
    NT_END_TEST(SUITE, "heldout_perplexity_beats_uniform");

    NT_TEST(SUITE, "sanity_generation_and_tps_gate") {
        NT_CHECK(ensure_loaded(), "engine.load");
        const auto prompt = g_engine.encode(
            g_spec.sanity_prompt, true, false, 0);
        suite_model::xc::SamplingConfig greedy;
        greedy.do_sample = false;
        const auto t0 = std::chrono::steady_clock::now();
        const auto out = g_engine.generate(
            prompt, g_spec.sanity_max_new, greedy);
        const double secs = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        if (g_spec.require_generation)
            NT_CHECK(!out.empty(), "require_generation");
        const double tps = secs > 0 ? out.size() / secs : 0.0;
        NT_CHECK(tps >= g_spec.min_tps,
                 "min_tokens_per_second gate");
    }
    NT_END_TEST(SUITE, "sanity_generation_and_tps_gate");

    return native_tests::report("eval_suite.json");
}
