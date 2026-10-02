// Suite: resource-governor Grant 協議層（star-resource-request/grant/v1）。
//
// 鎖定不變式（星澄 native-only 規格 §4–§11、§58–§59、驗收場景 A/B/E/F/G）：
//  - 回覆詞彙只有 DENIED/DEFERRED/PARTIAL/GRANTED/REVOKED；
//  - quota 內全額 → GRANTED；partial → PARTIAL 且授予值 < preferred；
//  - 類別暫停/零配額/低於 minimum → DEFERRED；未知類別/gpu_required 但
//    模式關閉 → DENIED；emergency → DENIED 且現存 grant → REVOKED；
//  - 同類別多 request 的 cpu_threads 總和 <= class quota（不超額）；
//  - VRAM：有絕對值→min(grant)；無→-1 由客戶端 percent 解析；
//  - grant 檔含規格 §8 全部欄位；renew 延展 valid_until；release 清檔；
//  - grant 過期 → REVOKED；request 消失 → orphan-drop。
// 檔案週期測試只動 temp dir，零專案副作用。
#include "harness.hpp"

#include "../resource_governor/governor_grants.h"
#include "governor_fake_engine.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

namespace {

using namespace governor_suite;
namespace gr = gptbridge::governor::grants;
namespace jl = gptbridge::jsonlite;
const char* SUITE = "RESOURCE_GOVERNOR_GRANTS_SUITE";

jl::JsonValue request_json(const std::string& overrides,
                           const std::string& id = "rr-1") {
    std::string text =
        "{\"format\":\"star-resource-request/v1\","
        "\"request_id\":\"" + id + "\",\"workload_id\":\"wl-1\","
        "\"candidate_id\":\"cand-1\",\"capability\":\"math\","
        "\"workload_class\":\"training\",\"priority\":5,"
        "\"minimum_cpu_threads\":2,\"preferred_cpu_threads\":12,"
        "\"minimum_ram_bytes\":1073741824,"
        "\"preferred_ram_bytes\":4294967296,"
        "\"gpu_optional\":true,\"gpu_required\":false,"
        "\"minimum_vram_bytes\":1073741824,"
        "\"preferred_vram_bytes\":4294967296,"
        "\"io_read_budget\":104857600,\"io_write_budget\":52428800,"
        "\"expected_duration_s\":600,"
        "\"checkpointable\":true,\"preemptible\":true";
    if (!overrides.empty()) text += "," + overrides;
    text += "}";
    return jl::JsonParser(text).parse();
}

std::string request_text(const std::string& id) {
    return jl::json_serialize(request_json("", id));
}

gr::ResourceRequest req(const std::string& overrides = "") {
    auto parsed = gr::parse_request(request_json(overrides));
    return parsed.value_or(gr::ResourceRequest{});
}

gr::ResourceRequest req_class(const std::string& cls) {
    gr::ResourceRequest r = req();
    r.workload_class = cls;
    return r;
}

gr::ResourceRequest req_gpu_required() {
    auto parsed = gr::parse_request(request_json(
        "\"gpu_optional\":true"));
    gr::ResourceRequest r = parsed.value_or(gr::ResourceRequest{});
    r.gpu_optional = false;
    r.gpu_required = true;
    return r;
}

gr::GrantContext ctx(int quota, bool paused = false) {
    gr::GrantContext c;
    c.class_known = true;
    c.class_quota = quota;
    c.class_paused = paused;
    c.gpu_enabled = true;
    c.vram_budget_percent = 50.0;
    c.vram_total_bytes = 8LL << 30;
    c.ram_available_bytes = 32LL << 30;
    c.now_unix = 1000.0;
    return c;
}

fs::path temp_state_dir() {
    const fs::path base = fs::temp_directory_path() /
        ("rg-grants-" + std::to_string(
             std::chrono::steady_clock::now().time_since_epoch().count() %
             1000000000));
    std::error_code ec;
    fs::create_directories(base / "resource-requests", ec);
    return base;
}

void write_request_file(const fs::path& state_dir, const std::string& id) {
    std::ofstream out(state_dir / "resource-requests" / (id + ".request.json"),
                      std::ios::binary | std::ios::trunc);
    out << request_text(id);
}

std::string read_text(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

gov::Snapshot snap_with_budget(int training_quota, bool paused) {
    gov::Snapshot snap;
    snap.has_mode = true;
    snap.mode = "high";
    snap.mem_avail_mb = 32768.0;
    gov::ConcurrencyBudget b;
    b.logical_cores = 16;
    b.total_quota = 12;
    b.pressure = gov::PressureTier::None;
    b.generation = 7;
    b.classes[static_cast<int>(gov::WorkClass::Training)] =
        {training_quota, training_quota, paused};
    snap.concurrency_budget = b;
    return snap;
}

gov::RulesDoc rules_gpu_on() {
    auto parsed = gov::parse_rules(
        "{\"mode\":\"high\",\"defaults\":{},"
        "\"modes\":{\"high\":{\"gpu_enabled\":true,"
        "\"vram_budget_percent\":50}}}");
    gov::RulesDoc rules = parsed.value_or(gov::RulesDoc{});
    rules.mode = "high";
    rules.has_mode = true;
    return rules;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "request_parse_requires_format_and_ids") {
        auto good = gr::parse_request(request_json(""));
        NT_CHECK(good.has_value(), "valid request parses");
        NT_CHECK(good->request_id == "rr-1" &&
                     good->preferred_cpu_threads == 12,
                 "fields populated");
        auto bad_fmt = gr::parse_request(
            jl::JsonParser("{\"format\":\"nope\"}").parse());
        NT_CHECK(!bad_fmt.has_value(), "wrong format rejected");
        auto no_id = gr::parse_request(request_json("", ""));
        NT_CHECK(!no_id.has_value(), "empty request_id rejected");
    }
    NT_END_TEST(SUITE, "request_parse_requires_format_and_ids");

    NT_TEST(SUITE, "scenario_a_full_grant_within_quota") {
        auto d = gr::adjudicate(req(), ctx(12));
        NT_CHECK(d.response == gr::GrantResponse::Granted, "GRANTED");
        NT_CHECK(d.cpu_threads_max == 12, "threads = preferred");
        NT_CHECK(d.gpu_allowed, "gpu granted");
        NT_CHECK(d.vram_bytes_max == (8LL << 30) / 2, "vram 50% of 8GB");
        NT_CHECK(d.pressure_state == "NORMAL", "pressure normal");
        NT_CHECK(d.valid_until_s > 1000.0, "valid_until future");
    }
    NT_END_TEST(SUITE, "scenario_a_full_grant_within_quota");

    NT_TEST(SUITE, "partial_grant_cpu_clamped_to_quota") {
        auto d = gr::adjudicate(req(), ctx(8));
        NT_CHECK(d.response == gr::GrantResponse::Partial, "PARTIAL");
        NT_CHECK(d.cpu_threads_max == 8, "granted 8 not 12 (spec §16)");
    }
    NT_END_TEST(SUITE, "partial_grant_cpu_clamped_to_quota");

    NT_TEST(SUITE, "deferred_when_below_minimum_or_paused") {
        auto d = gr::adjudicate(req(), ctx(1));
        NT_CHECK(d.response == gr::GrantResponse::Deferred &&
                     d.reason == "below-minimum-cpu-threads",
                 "below minimum defers");
        auto p = gr::adjudicate(req(), ctx(12, /*paused=*/true));
        NT_CHECK(p.response == gr::GrantResponse::Deferred &&
                     p.reason == "class-paused-or-zero-quota",
                 "paused defers");
    }
    NT_END_TEST(SUITE, "deferred_when_below_minimum_or_paused");

    NT_TEST(SUITE, "denied_unknown_class_and_gpu_required_off") {
        auto c = ctx(12);
        c.class_known = false;
        auto d = gr::adjudicate(req(), c);
        NT_CHECK(d.response == gr::GrantResponse::Denied &&
                     d.reason.rfind("unknown-workload-class", 0) == 0,
                 "unknown class denied");
        c = ctx(12);
        c.gpu_enabled = false;
        auto g = gr::adjudicate(req_gpu_required(), c);
        NT_CHECK(g.response == gr::GrantResponse::Denied &&
                     g.reason == "gpu-required-but-disabled-by-mode",
                 "gpu_required denied when gpu off");
        auto soft = gr::adjudicate(req(), c); /* gpu_optional */
        NT_CHECK(soft.response == gr::GrantResponse::Partial &&
                     !soft.gpu_allowed && soft.vram_bytes_max == 0,
                 "gpu_optional falls back CPU (spec §18)");
    }
    NT_END_TEST(SUITE, "denied_unknown_class_and_gpu_required_off");

    NT_TEST(SUITE, "emergency_denies_new_requests") {
        auto c = ctx(12);
        c.emergency = true;
        auto d = gr::adjudicate(req(), c);
        NT_CHECK(d.response == gr::GrantResponse::Denied &&
                     d.pressure_state == "EMERGENCY",
                 "emergency denied (spec §23)");
    }
    NT_END_TEST(SUITE, "emergency_denies_new_requests");

    /* §21/§22 + A598：PRE → shed 首位（training）DEFERRED；
     * ACTIVE → shed 前段（training…maintenance）DEFERRED；
     * serving 類（model/rag/interactive）不受壓力削讓。 */
    NT_TEST(SUITE, "pressure_defers_shed_first_classes") {
        gr::GrantContext c = ctx(12);
        c.pressure = gov::PressureTier::Pre;
        auto tr = gr::adjudicate(req_class("training"), c);
        NT_CHECK(tr.response == gr::GrantResponse::Deferred &&
                     tr.reason == "pre-pressure-shed",
                 "PRE defers training (new-lane ban)");
        auto md = gr::adjudicate(req_class("model"), c);
        NT_CHECK(md.response == gr::GrantResponse::Granted,
                 "model still granted at PRE");
        c.pressure = gov::PressureTier::Active;
        tr = gr::adjudicate(req_class("training"), c);
        NT_CHECK(tr.response == gr::GrantResponse::Deferred &&
                     tr.reason == "active-pressure-shed",
                 "ACTIVE defers training");
        auto bt = gr::adjudicate(req_class("batch"), c);
        NT_CHECK(bt.response == gr::GrantResponse::Deferred,
                 "ACTIVE defers batch");
        md = gr::adjudicate(req_class("model"), c);
        NT_CHECK(md.response == gr::GrantResponse::Granted,
                 "model still granted at ACTIVE");
        std::ignore = md;
    }
    NT_END_TEST(SUITE, "pressure_defers_shed_first_classes");

    NT_TEST(SUITE, "vram_negative_means_client_resolves_percent") {
        auto c = ctx(12);
        c.vram_total_bytes = 0;
        auto d = gr::adjudicate(req(), c);
        NT_CHECK(d.gpu_allowed && d.vram_bytes_max == -1 &&
                     d.vram_budget_percent == 50.0,
                 "percent-only VRAM grant");
    }
    NT_END_TEST(SUITE, "vram_negative_means_client_resolves_percent");

    NT_TEST(SUITE, "grant_json_has_all_spec_fields") {
        auto r = req();
        auto d = gr::adjudicate(r, ctx(12));
        auto doc = gr::decision_to_json(r, d);
        const jl::JsonValue* g = doc.get("grant");
        NT_CHECK(g != nullptr, "grant object present");
        for (const char* key :
             {"grant_id", "cpu_threads_max", "ram_bytes_max",
              "pinned_ram_bytes_max", "gpu_allowed", "vram_bytes_max",
              "gpu_compute_share", "io_read_limit", "io_write_limit",
              "background_threads_max", "valid_until_s",
              "pressure_state"}) {
            NT_CHECK(g->get(key) != nullptr,
                     (std::string("field ") + key).c_str());
        }
    }
    NT_END_TEST(SUITE, "grant_json_has_all_spec_fields");

    NT_TEST(SUITE, "grant_cycle_grants_then_revokes_on_expiry") {
        const fs::path dir = temp_state_dir();
        write_request_file(dir, "rr-x");
        gov::Snapshot snap = snap_with_budget(8, false);
        auto stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1000.0);
        NT_CHECK(stats.granted == 1 || stats.partial == 1,
                 "granted cycle");
        const fs::path grant_file =
            dir / "resource-grants" / "rr-x.json";
        NT_CHECK(fs::exists(grant_file), "grant file written");
        jl::JsonValue doc =
            jl::JsonParser(read_text(grant_file)).parse();
        const jl::JsonValue* g1 = doc.get("grant");
        NT_CHECK(g1 != nullptr &&
                     g1->type == jl::JsonValue::Type::Object &&
                     g1->get("cpu_threads_max")->number == 8.0,
                 "grant clamped to quota 8");
        stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 100000.0);
        NT_CHECK(stats.revoked == 1, "expired grant revoked");
        doc = jl::JsonParser(read_text(grant_file)).parse();
        NT_CHECK(doc.get("response")->string == "REVOKED", "tombstone");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "grant_cycle_grants_then_revokes_on_expiry");

    NT_TEST(SUITE, "grant_cycle_multi_lane_never_oversubscribes") {
        const fs::path dir = temp_state_dir();
        write_request_file(dir, "rr-a");
        write_request_file(dir, "rr-b");
        gov::Snapshot snap = snap_with_budget(8, false);
        auto stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1000.0);
        /* quota 8 < preferred 12：第一條 PARTIAL 拿滿 8，第二條剩餘 0
         * < minimum → DEFERRED（不超額是最終硬規則，§33）。 */
        NT_CHECK(stats.granted + stats.partial == 1 &&
                     stats.deferred == 1,
                 "first lane partial, second deferred");
        jl::JsonValue a = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-a.json")).parse();
        jl::JsonValue b = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-b.json")).parse();
        const auto grant_obj = [](const jl::JsonValue& doc) {
            const jl::JsonValue* g = doc.get("grant");
            return g != nullptr &&
                           g->type == jl::JsonValue::Type::Object
                       ? (int)g->get("cpu_threads_max")->number
                       : 0;
        };
        const int ta = grant_obj(a);
        const int tb = grant_obj(b);
        NT_CHECK(ta + tb <= 8, "sum <= quota (spec §33)");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "grant_cycle_multi_lane_never_oversubscribes");

    NT_TEST(SUITE, "grant_cycle_emergency_revokes_live_grants") {
        const fs::path dir = temp_state_dir();
        write_request_file(dir, "rr-e");
        gov::Snapshot snap = snap_with_budget(8, false);
        gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1000.0);
        snap.disabled = true; /* kill-switch → EMERGENCY */
        auto stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1010.0);
        NT_CHECK(stats.revoked >= 1, "emergency revokes");
        jl::JsonValue doc = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-e.json")).parse();
        NT_CHECK(doc.get("response")->string == "REVOKED",
                 "grant file tombstoned (spec §58)");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "grant_cycle_emergency_revokes_live_grants");

    NT_TEST(SUITE, "renew_extends_valid_until") {
        const fs::path dir = temp_state_dir();
        write_request_file(dir, "rr-r");
        gov::Snapshot snap = snap_with_budget(8, false);
        gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1000.0);
        {
            std::ofstream marker(dir / "resource-requests" /
                                     "rr-r.renew.json",
                                 std::ios::binary | std::ios::trunc);
            marker << "{}";
        }
        auto stats =
            gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1050.0);
        NT_CHECK(stats.renewed == 1, "renew consumed");
        jl::JsonValue doc = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-r.json")).parse();
        const jl::JsonValue* g = doc.get("grant");
        NT_CHECK(g != nullptr &&
                     g->type == jl::JsonValue::Type::Object,
                 "grant object present after renew");
        const jl::JsonValue* vu = g->get("valid_until_s");
        NT_CHECK(vu != nullptr, "valid_until_s present");
        NT_CHECK(vu->number > 1050.0,
                 "valid_until extended (spec §59)");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "renew_extends_valid_until");

    /* 場景 B（§56 dynamic resize）：quota 12→4，既有 grant 必須被重寫
     * 為更小上限（signature 變動 → 覆寫＋稽核），threads 不得維持 8。 */
    NT_TEST(SUITE, "grant_cycle_resize_shrinks_existing_grant") {
        const fs::path dir = temp_state_dir();
        write_request_file(dir, "rr-b");
        gov::Snapshot snap = snap_with_budget(12, false);
        auto stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(),
                                         1000.0);
        NT_CHECK(stats.granted + stats.partial == 1, "granted");
        /* 第二輪：同一 request，類別配額縮到 4。 */
        snap = snap_with_budget(4, false);
        stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1010.0);
        jl::JsonValue doc = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-b.json")).parse();
        const jl::JsonValue* g = doc.get("grant");
        NT_CHECK(g != nullptr &&
                     g->type == jl::JsonValue::Type::Object,
                 "grant still live after resize");
        NT_CHECK(g->get("cpu_threads_max")->number <= 4.0,
                 "resized down to quota (spec §56)");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "grant_cycle_resize_shrinks_existing_grant");

    return native_tests::report("resource_governor_grants_suite.json");
}
