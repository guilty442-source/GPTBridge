// Suite: M1 system_rescue C prototype (platform_packager.py semantics).
// Covers the _verify_tool_package verdict ladder, packager error-code
// normalization, verify_all aggregation, archive checksum verdicts,
// SHA-256 parity vectors, and the _main CLI dispatch order.
#include "harness.hpp"

#include <cstring>
#include <string>

extern "C" {
#include "system_rescue.h"
}

namespace {
const char* SUITE = "SYSTEM_RESCUE_SUITE";

std::string sha256_of(const char* text) {
    char hex[65];
    if (!gptbridge_sr_sha256_hex(
            reinterpret_cast<const uint8_t*>(text),
            std::strlen(text), hex)) {
        return std::string("<sha256-failed>");
    }
    return std::string(hex);
}
}

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "verify_tool_package_verdict_ladder") {
        NT_CHECK(gptbridge_sr_verify_tool_package(0, 0, 0, 0, 0, 0)
                     == GPTBRIDGE_SR_PKG_MISSING, "dir missing first");
        NT_CHECK(gptbridge_sr_verify_tool_package(1, 0, 0, 0, 0, 0)
                     == GPTBRIDGE_SR_PKG_METADATA_MISSING, "metadata missing");
        NT_CHECK(gptbridge_sr_verify_tool_package(1, 1, 0, 0, 0, 0)
                     == GPTBRIDGE_SR_PKG_METADATA_INVALID, "invalid metadata");
        NT_CHECK(gptbridge_sr_verify_tool_package(1, 1, 1, 1, 200, 100)
                     == GPTBRIDGE_SR_PKG_STALE, "source newer -> stale");
        NT_CHECK(gptbridge_sr_verify_tool_package(1, 1, 1, 1, 100, 100)
                     == GPTBRIDGE_SR_PKG_OK, "equal mtime not stale");
        NT_CHECK(gptbridge_sr_verify_tool_package(1, 1, 1, 1, 50, 100)
                     == GPTBRIDGE_SR_PKG_OK, "source older ok");
        NT_CHECK(gptbridge_sr_verify_tool_package(1, 1, 1, 0, 0, 0)
                     == GPTBRIDGE_SR_PKG_OK, "no manifest ok");
        NT_CHECK(std::strcmp(
                     gptbridge_sr_pkg_verdict_code(GPTBRIDGE_SR_PKG_STALE),
                     "STALE_PACKAGE") == 0, "stale code name");
    }
    NT_END_TEST(SUITE, "verify_tool_package_verdict_ladder");

    NT_TEST(SUITE, "normalize_packager_error") {
        NT_CHECK(std::strcmp(
                     gptbridge_sr_normalize_packager_error("PROCESS_TIMEOUT"),
                     "PACKAGER_TIMEOUT") == 0, "timeout mapping");
        NT_CHECK(std::strcmp(gptbridge_sr_normalize_packager_error(
                                 "PROCESS_LAUNCH_FAILED"),
                             "PACKAGER_LAUNCH_FAILED") == 0, "launch mapping");
        NT_CHECK(std::strcmp(gptbridge_sr_normalize_packager_error(
                                 "PROCESS_OUTPUT_INVALID"),
                             "PACKAGER_OUTPUT_INVALID") == 0, "output mapping");
        NT_CHECK(std::strcmp(gptbridge_sr_normalize_packager_error("OTHER"),
                             "OTHER") == 0, "passthrough");
        NT_CHECK(gptbridge_sr_normalize_packager_error(nullptr) == nullptr,
                 "null -> null");
        NT_CHECK(gptbridge_sr_normalize_packager_error("") == nullptr,
                 "empty -> null");
    }
    NT_END_TEST(SUITE, "normalize_packager_error");

    NT_TEST(SUITE, "verify_all_aggregation") {
        int32_t oks_all[3] = {1, 1, 1};
        int32_t oks_bad[3] = {1, 0, 1};
        NT_CHECK(gptbridge_sr_all_ok(oks_all, 3) == 1, "all ok");
        NT_CHECK(gptbridge_sr_all_ok(oks_bad, 3) == 0, "one fail");
        NT_CHECK(gptbridge_sr_all_ok(nullptr, 0) == 1,
                 "empty -> ok (no packaged tools)");
        NT_CHECK(gptbridge_sr_all_ok(nullptr, -1) == 1, "negative -> ok");
    }
    NT_END_TEST(SUITE, "verify_all_aggregation");

    NT_TEST(SUITE, "verify_archive_verdicts") {
        NT_CHECK(gptbridge_sr_verify_archive(0, 0, nullptr, nullptr)
                     == GPTBRIDGE_SR_ARC_NOT_FOUND, "missing file");
        NT_CHECK(gptbridge_sr_verify_archive(1, 0, nullptr, nullptr)
                     == GPTBRIDGE_SR_ARC_OK, "no sidecar ok");
        NT_CHECK(gptbridge_sr_verify_archive(1, 1, "abcdef", "abcdef")
                     == GPTBRIDGE_SR_ARC_OK, "hex match");
        NT_CHECK(gptbridge_sr_verify_archive(1, 1, "ABCDEF", "abcdef")
                     == GPTBRIDGE_SR_ARC_OK, "case-insensitive match");
        NT_CHECK(gptbridge_sr_verify_archive(1, 1, "abcdef", "abcdeg")
                     == GPTBRIDGE_SR_ARC_CHECKSUM_MISMATCH, "mismatch");
        NT_CHECK(gptbridge_sr_verify_archive(1, 1, nullptr, "abcdef")
                     == GPTBRIDGE_SR_ARC_CHECKSUM_UNREADABLE,
                 "empty/unparseable sidecar -> UNREADABLE");
        NT_CHECK(gptbridge_sr_verify_archive(1, 1, "abcdef", nullptr)
                     == GPTBRIDGE_SR_ARC_CHECKSUM_MISMATCH,
                 "null actual mismatches");
        NT_CHECK(std::strcmp(
                     gptbridge_sr_arc_verdict_code(
                         GPTBRIDGE_SR_ARC_CHECKSUM_MISMATCH),
                     "PACKAGE_CHECKSUM_MISMATCH") == 0, "mismatch code name");
    }
    NT_END_TEST(SUITE, "verify_archive_verdicts");

    NT_TEST(SUITE, "sha256_known_vectors") {
        NT_CHECK(sha256_of("") ==
                     "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934"
                     "ca495991b7852b855",
                 "empty digest");
        NT_CHECK(sha256_of("abc") ==
                     "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb"
                     "410ff61f20015ad",
                 "abc digest");
        /* 56-byte message crosses the padding boundary -> two blocks */
        NT_CHECK(sha256_of("abcdbcdecdefdefgefghfghighijhijkijkljklmkl"
                           "mnlmnomnopnopq") ==
                     "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6e"
                     "cedd419db06c1",
                 "multi-block digest");
    }
    NT_END_TEST(SUITE, "sha256_known_vectors");

    NT_TEST(SUITE, "cli_dispatch_order") {
        NT_CHECK(gptbridge_sr_cli_dispatch(1, nullptr, 1, 0, 0)
                     == GPTBRIDGE_SR_OP_VERIFY_ALL, "--all --verify");
        NT_CHECK(gptbridge_sr_cli_dispatch(1, "tool", 1, 0, 0)
                     == GPTBRIDGE_SR_OP_VERIFY_ALL,
                 "--all --verify wins over tool");
        NT_CHECK(gptbridge_sr_cli_dispatch(0, "tool", 1, 1, 0)
                     == GPTBRIDGE_SR_OP_DEEP_VERIFY_TOOL, "deep before plain");
        NT_CHECK(gptbridge_sr_cli_dispatch(0, "tool", 1, 0, 0)
                     == GPTBRIDGE_SR_OP_VERIFY_TOOL, "--tool --verify");
        NT_CHECK(gptbridge_sr_cli_dispatch(0, "tool", 0, 0, 1)
                     == GPTBRIDGE_SR_OP_PACKAGE_TOOL, "--tool --package");
        NT_CHECK(gptbridge_sr_cli_dispatch(1, nullptr, 0, 0, 1)
                     == GPTBRIDGE_SR_OP_PACKAGE_ALL, "--all --package");
        NT_CHECK(gptbridge_sr_cli_dispatch(1, nullptr, 0, 0, 0)
                     == GPTBRIDGE_SR_OP_VERIFY_ALL, "bare --all");
        NT_CHECK(gptbridge_sr_cli_dispatch(0, nullptr, 1, 0, 0)
                     == GPTBRIDGE_SR_OP_INVALID, "no selector");
        NT_CHECK(gptbridge_sr_cli_dispatch(0, "tool", 0, 0, 0)
                     == GPTBRIDGE_SR_OP_INVALID, "tool alone invalid");
        NT_CHECK(gptbridge_sr_cli_dispatch(0, "", 1, 0, 0)
                     == GPTBRIDGE_SR_OP_INVALID, "empty tool id");
        NT_CHECK(std::strcmp(
                     gptbridge_sr_cli_op_name(GPTBRIDGE_SR_OP_PACKAGE_ALL),
                     "package_all") == 0, "op name");
    }
    NT_END_TEST(SUITE, "cli_dispatch_order");

    return native_tests::report("system_rescue_suite.json");
}
