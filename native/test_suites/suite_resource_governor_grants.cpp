// Suite: resource-governor Grant ?降撅歹?star-resource-request/grant/v1嚗?//
// ??銝?撘??? native-only 閬 禮4??1??8??9???嗅??A/B/E/F/G嚗?
//  - ??閰??芣? DENIED/DEFERRED/PARTIAL/GRANTED/REVOKED嚗?//  - quota ?批憿???GRANTED嚗artial ??PARTIAL 銝?鈭?< preferred嚗?//  - 憿?怠?/?園?憿?雿 minimum ??DEFERRED嚗?仿???gpu_required 雿?//    璅∪??? ??DENIED嚗mergency ??DENIED 銝摮?grant ??REVOKED嚗?//  - ???亙? request ??cpu_threads 蝮賢? <= class quota嚗?頞?嚗?
//  - VRAM嚗?蝯??潑?min(grant)嚗??1 ?勗恥?嗥垢 percent 閫??嚗?//  - grant 瑼閬 禮8 ?券甈?嚗enew 撱嗅? valid_until嚗elease 皜?嚗?//  - grant ?? ??REVOKED嚗equest 瘨仃 ??orphan-drop??// 瑼??望?皜祈岫?芸? temp dir嚗撠??臭??具?#include "harness.hpp"

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

jl::JsonValue request_json(const std::string& overrides) {
    std::string text =
        "{\"format\":\"star-resource-request/v1\","
        "\"request_id\":\"rr-1\",\"workload_id\":\"wl-1\","
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

gr::ResourceRequest req(const std::string& overrides = "") {
    auto parsed = gr::parse_request(request_json(overrides));
    return parsed.value_or(gr::ResourceRequest{});
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

void write_request_file(const fs::path& state_dir, const std::string& id,
                        const std::string& body) {
    std::ofstream out(state_dir / "resource-requests" / (id + ".request.json"),
                      std::ios::binary | std::ios::trunc);
    out << body;
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
        "{\"defaults\":{},"
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
        auto no_id = gr::parse_request(request_json(
            "\"request_id\":\"\""));
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
        NT_CHECK(d.cpu_threads_max == 8, "granted 8 not 12 (spec 禮16)");
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
        auto g = gr::adjudicate(
            req("\"gpu_required\":true,\"gpu_optional\":false"), c);
        NT_CHECK(g.response == gr::GrantResponse::Denied &&
                     g.reason == "gpu-required-but-disabled-by-mode",
                 "gpu_required denied when gpu off");
        auto soft = gr::adjudicate(req(), c); /* gpu_optional */
        NT_CHECK(soft.response == gr::GrantResponse::Partial &&
                     !soft.gpu_allowed && soft.vram_bytes_max == 0,
                 "gpu_optional falls back CPU (spec 禮18)");
    }
    NT_END_TEST(SUITE, "denied_unknown_class_and_gpu_required_off");

    NT_TEST(SUITE, "emergency_denies_new_requests") {
        auto c = ctx(12);
        c.emergency = true;
        auto d = gr::adjudicate(req(), c);
        NT_CHECK(d.response == gr::GrantResponse::Denied &&
                     d.pressure_state == "EMERGENCY",
                 "emergency denied (spec 禮23)");
    }
    NT_END_TEST(SUITE, "emergency_denies_new_requests");

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
        NT_CHECK(gr::response_name(d.response).size() > 0, "response named");
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
        write_request_file(dir, "rr-x", jl::json_serialize(request_json("")));
        gov::Snapshot snap = snap_with_budget(8, false);
        auto stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1000.0);
        NT_CHECK(stats.granted == 1 || stats.partial == 1, "granted cycle");
        const fs::path grant_file =
            dir / "resource-grants" / "rr-x.json";
        NT_CHECK(fs::exists(grant_file), "grant file written");
        jl::JsonValue doc = jl::JsonParser(read_text(grant_file)).parse();
        NT_CHECK(doc.get("grant")->get("cpu_threads_max")->number == 8.0,
                 "grant clamped to quota 8");
        /* expiry ??REVOKED tombstone */
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
        write_request_file(dir, "rr-a", jl::json_serialize(request_json("")));
        write_request_file(dir, "rr-b", jl::json_serialize(request_json("")));
        gov::Snapshot snap = snap_with_budget(8, false);
        auto stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1000.0);
        const int granted_total =
            stats.granted + stats.partial;
        NT_CHECK(granted_total == 2, "both lanes get decisions");
        jl::JsonValue a = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-a.json")).parse();
        jl::JsonValue b = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-b.json")).parse();
        const int ta = a.get("grant") ? (int)a.get("grant")->get(
                        "cpu_threads_max")->number : 0;
        const int tb = b.get("grant") ? (int)b.get("grant")->get(
                        "cpu_threads_max")->number : 0;
        NT_CHECK(ta + tb <= 8, "sum <= quota (spec 禮33)");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "grant_cycle_multi_lane_never_oversubscribes");

    NT_TEST(SUITE, "grant_cycle_emergency_revokes_live_grants") {
        const fs::path dir = temp_state_dir();
        write_request_file(dir, "rr-e", jl::json_serialize(request_json("")));
        gov::Snapshot snap = snap_with_budget(8, false);
        gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1000.0);
        snap.disabled = true; /* kill-switch ??EMERGENCY */
        auto stats = gr::run_grant_cycle(dir, snap, rules_gpu_on(), 1010.0);
        NT_CHECK(stats.revoked >= 1, "emergency revokes");
        jl::JsonValue doc = jl::JsonParser(
            read_text(dir / "resource-grants" / "rr-e.json")).parse();
        NT_CHECK(doc.get("response")->string == "REVOKED",
                 "grant file tombstoned (spec 禮58)");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "grant_cycle_emergency_revokes_live_grants");

    NT_TEST(SUITE, "renew_extends_valid_until") {
        const fs::path dir = temp_state_dir();
        write_request_file(dir, "rr-r", jl::json_serialize(request_json("")));
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
        const double until =
            doc.get("grant")->get("valid_until_s")->number;
        NT_CHECK(until > 1050.0, "valid_until extended (spec 禮59)");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
    NT_END_TEST(SUITE, "renew_extends_valid_until");

    return native_tests::report("resource_governor_grants_suite.json");
}
