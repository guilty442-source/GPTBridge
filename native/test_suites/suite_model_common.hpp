// Shared helpers for model-runtime suites (baseline / eval / dialogue).
// §10.60：三套件自 BLOCKED_MODEL_RUNTIME 解鎖——載入真實 cpp bundle
// （star-native-inference-bundle/v1）並跑 star-native-eval-suite/v1
// 規格語義。零 Python、僅主系統測試套件入口。
#ifndef SUITE_MODEL_COMMON_HPP
#define SUITE_MODEL_COMMON_HPP

#include "xingcheng_inference.hpp"
#include "jsonlite.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <set>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace suite_model {

namespace jl = gptbridge::jsonlite;
namespace xc = xingcheng::inference;
namespace fs = std::filesystem;

/* ---- 小 JSON 建構工具（與各 suite 同慣例） ---- */

inline jl::JsonValue jstr(const std::string& s) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::String; v.string = s;
    return v;
}
inline jl::JsonValue jnum(double n) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Number; v.number = n;
    return v;
}
inline jl::JsonValue jbool(bool b) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Bool; v.boolean = b;
    return v;
}
inline jl::JsonValue jobj(std::initializer_list<
                   std::pair<std::string, jl::JsonValue>> items) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Object;
    v.object.assign(items.begin(), items.end());
    return v;
}

/* ---- 路徑發現 ---- */

/* 自 cwd 向上找含 "Standalone tools/local-model/xingcheng" 的 repo 根；
   找不到回傳空 path（呼叫端 fail-closed）。 */
inline fs::path find_repo_root() {
    for (fs::path p = fs::current_path(); !p.empty(); p = p.parent_path()) {
        std::error_code ec;
        const fs::path marker =
            p / "Standalone tools" / "local-model" / "xingcheng";
        if (fs::is_directory(marker, ec)) return p;
        if (p == p.root_path()) break;
    }
    return fs::path();
}

/* bundle 目錄：env XINGCHENG_BUNDLE_DIR 優先；否則掃
   <repo>/Standalone tools/local-model/xingcheng/runtime/models/
   cpp-bundles/*，取第一個含 manifest.json+weights.bin+tokenizer.json
   者（名稱排序，決定性）。 */
inline fs::path find_bundle_dir(const fs::path& root) {
    if (const char* env = std::getenv("XINGCHENG_BUNDLE_DIR")) {
        if (*env) return fs::path(env);
    }
    const fs::path base = root / "Standalone tools" / "local-model" /
                          "xingcheng" / "runtime" / "models" /
                          "cpp-bundles";
    std::error_code ec;
    std::vector<fs::path> dirs;
    for (const auto& e : fs::directory_iterator(base, ec)) {
        if (!e.is_directory()) continue;
        const fs::path d = e.path();
        if (fs::exists(d / "manifest.json", ec) &&
            fs::exists(d / "weights.bin", ec) &&
            fs::exists(d / "tokenizer.json", ec))
            dirs.push_back(d);
    }
    std::sort(dirs.begin(), dirs.end());
    return dirs.empty() ? fs::path() : dirs.front();
}

/* ---- eval suite 規格（star-native-eval-suite/v1） ---- */

struct EvalSpec {
    std::string suite_id;
    std::string eval_text;
    std::string sanity_prompt;
    int64_t sanity_max_new = 16;
    int64_t seed = 42;
    bool require_generation = true;
    double min_tps = 0.0;
    double max_ppl_regression_pct = 0.0;
    bool has_baseline_metrics = false;
};

inline std::string read_text_file(const fs::path& p) {
    std::ifstream in(p, std::ios::binary);
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

inline bool spec_number(const jl::JsonValue& obj, const char* key,
                        double* out) {
    const jl::JsonValue* v = obj.get(key);
    if (!v || v->type != jl::JsonValue::Type::Number) return false;
    *out = v->number;
    return true;
}

inline bool load_eval_spec(const fs::path& path, EvalSpec* spec,
                           std::string* err) {
    jl::JsonValue doc;
    try {
        doc = jl::JsonParser(read_text_file(path)).parse();
    } catch (...) {
        if (err) *err = "spec unreadable";
        return false;
    }
    const jl::JsonValue* fv = doc.get("format_version");
    if (!fv || fv->type != jl::JsonValue::Type::String ||
        fv->string != "star-native-eval-suite/v1") {
        if (err) *err = "format_version mismatch";
        return false;
    }
    const jl::JsonValue* sid = doc.get("suite_id");
    const jl::JsonValue* et = doc.get("eval_text");
    const jl::JsonValue* gates = doc.get("quality_gates");
    if (!sid || sid->type != jl::JsonValue::Type::String ||
        !et || et->type != jl::JsonValue::Type::String ||
        et->string.empty() ||
        !gates || gates->type != jl::JsonValue::Type::Object) {
        if (err) *err = "missing suite_id/eval_text/quality_gates";
        return false;
    }
    spec->suite_id = sid->string;
    spec->eval_text = et->string;
    if (const jl::JsonValue* sp = doc.get("sanity_prompt"))
        if (sp->type == jl::JsonValue::Type::String)
            spec->sanity_prompt = sp->string;
    double n = 0.0;
    if (spec_number(doc, "sanity_max_new_tokens", &n))
        spec->sanity_max_new = static_cast<int64_t>(n);
    if (spec_number(doc, "seed", &n)) spec->seed = static_cast<int64_t>(n);
    double req = 1.0;
    if (const jl::JsonValue* rg = gates->get("require_generation"))
        if (rg->type == jl::JsonValue::Type::Bool)
            spec->require_generation = rg->boolean;
    (void)req;
    spec_number(*gates, "min_tokens_per_second", &spec->min_tps);
    spec_number(*gates, "max_perplexity_regression_pct",
                &spec->max_ppl_regression_pct);
    const jl::JsonValue* bm = doc.get("baseline_metrics");
    spec->has_baseline_metrics =
        bm && bm->type == jl::JsonValue::Type::Object &&
        !bm->object.empty();
    return true;
}

/* eval_text → tokens（決定性截斷 cap；與 Python 全量語義同構，
   差異只在步數上限，截斷量記錄於 detail）。 */
inline std::vector<int64_t> eval_tokens(xc::NativeInferenceEngine& eng,
                                        const std::string& text,
                                        int64_t cap) {
    std::vector<int64_t> ids = eng.encode(text, true, false, 0);
    if (cap > 0 && static_cast<int64_t>(ids.size()) > cap)
        ids.resize(static_cast<size_t>(cap));
    return ids;
}

} // namespace suite_model

#endif /* SUITE_MODEL_COMMON_HPP */
