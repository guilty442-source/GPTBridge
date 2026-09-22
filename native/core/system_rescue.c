/* system_rescue.c — M1：system-rescue 決策自由執行語義
 *
 * 實作對齊 platform_packager.py；I/O 由呼叫方負責，本檔純語義。
 * SHA-256 為 FIPS 180-4 標準實作（與 hashlib.sha256 等值）。
 */
#include "system_rescue.h"

#include <string.h>
#include <ctype.h>

const char* gptbridge_sr_pkg_verdict_code(gptbridge_sr_pkg_verdict_t v) {
    switch (v) {
    case GPTBRIDGE_SR_PKG_OK: return "OK";
    case GPTBRIDGE_SR_PKG_MISSING: return "PACKAGE_MISSING";
    case GPTBRIDGE_SR_PKG_METADATA_MISSING:
        return "PACKAGE_METADATA_MISSING";
    case GPTBRIDGE_SR_PKG_METADATA_INVALID:
        return "PACKAGE_METADATA_INVALID";
    case GPTBRIDGE_SR_PKG_STALE: return "STALE_PACKAGE";
    default: return "UNKNOWN";
    }
}

const char* gptbridge_sr_arc_verdict_code(gptbridge_sr_arc_verdict_t v) {
    switch (v) {
    case GPTBRIDGE_SR_ARC_OK: return "OK";
    case GPTBRIDGE_SR_ARC_NOT_FOUND: return "PACKAGE_NOT_FOUND";
    case GPTBRIDGE_SR_ARC_CHECKSUM_MISMATCH:
        return "PACKAGE_CHECKSUM_MISMATCH";
    case GPTBRIDGE_SR_ARC_CHECKSUM_UNREADABLE:
        return "PACKAGE_CHECKSUM_UNREADABLE";
    default: return "UNKNOWN";
    }
}

const char* gptbridge_sr_cli_op_name(gptbridge_sr_cli_op_t op) {
    switch (op) {
    case GPTBRIDGE_SR_OP_VERIFY_ALL: return "verify_all";
    case GPTBRIDGE_SR_OP_DEEP_VERIFY_TOOL: return "deep_verify_tool";
    case GPTBRIDGE_SR_OP_VERIFY_TOOL: return "verify_tool";
    case GPTBRIDGE_SR_OP_PACKAGE_TOOL: return "package_tool";
    case GPTBRIDGE_SR_OP_PACKAGE_ALL: return "package_all";
    default: return "invalid_args";
    }
}

gptbridge_sr_pkg_verdict_t gptbridge_sr_verify_tool_package(
    int32_t release_dir_exists,
    int32_t metadata_exists,
    int32_t metadata_valid,
    int32_t has_source_manifest,
    int64_t source_mtime,
    int64_t package_mtime) {
    if (!release_dir_exists) {
        return GPTBRIDGE_SR_PKG_MISSING;
    }
    if (!metadata_exists) {
        return GPTBRIDGE_SR_PKG_METADATA_MISSING;
    }
    if (!metadata_valid) {
        return GPTBRIDGE_SR_PKG_METADATA_INVALID;
    }
    /* Python: source_mtime > pkg_mtime → STALE_PACKAGE */
    if (has_source_manifest && source_mtime > package_mtime) {
        return GPTBRIDGE_SR_PKG_STALE;
    }
    return GPTBRIDGE_SR_PKG_OK;
}

const char* gptbridge_sr_normalize_packager_error(const char* error_code) {
    if (error_code == NULL || error_code[0] == '\0') {
        return NULL;
    }
    if (strcmp(error_code, "PROCESS_TIMEOUT") == 0) {
        return "PACKAGER_TIMEOUT";
    }
    if (strcmp(error_code, "PROCESS_LAUNCH_FAILED") == 0) {
        return "PACKAGER_LAUNCH_FAILED";
    }
    if (strcmp(error_code, "PROCESS_OUTPUT_INVALID") == 0) {
        return "PACKAGER_OUTPUT_INVALID";
    }
    return error_code;
}

int gptbridge_sr_all_ok(const int32_t* oks, int32_t count) {
    int32_t i;
    if (count <= 0) {
        return 1;
    }
    for (i = 0; i < count; ++i) {
        if (!oks[i]) {
            return 0;
        }
    }
    return 1;
}

static int _hex_equal_ci(const char* a, const char* b) {
    if (a == NULL || b == NULL) {
        return 0;
    }
    while (*a != '\0' && *b != '\0') {
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) {
            return 0;
        }
        ++a;
        ++b;
    }
    return *a == '\0' && *b == '\0';
}

gptbridge_sr_arc_verdict_t gptbridge_sr_verify_archive(
    int32_t file_exists,
    int32_t sidecar_exists,
    const char* expected_hex,
    const char* actual_hex) {
    if (!file_exists) {
        return GPTBRIDGE_SR_ARC_NOT_FOUND;
    }
    if (sidecar_exists && expected_hex == NULL) {
        return GPTBRIDGE_SR_ARC_CHECKSUM_UNREADABLE;
    }
    if (sidecar_exists && !_hex_equal_ci(expected_hex, actual_hex)) {
        return GPTBRIDGE_SR_ARC_CHECKSUM_MISMATCH;
    }
    return GPTBRIDGE_SR_ARC_OK;
}

/* --- SHA-256 (FIPS 180-4) ------------------------------------------------ */

static const uint32_t _k256[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u,
    0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
    0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u,
    0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u,
    0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
};

static uint32_t _rotr32(uint32_t x, int n) {
    return (x >> n) | (x << (32 - n));
}

static void _sha256_block(uint32_t h[8], const uint8_t* block) {
    uint32_t w[64];
    uint32_t a, b, c, d, e, f, g, hh;
    int i;
    for (i = 0; i < 16; ++i) {
        w[i] = ((uint32_t)block[i * 4] << 24)
             | ((uint32_t)block[i * 4 + 1] << 16)
             | ((uint32_t)block[i * 4 + 2] << 8)
             | ((uint32_t)block[i * 4 + 3]);
    }
    for (i = 16; i < 64; ++i) {
        const uint32_t s0 = _rotr32(w[i - 15], 7) ^ _rotr32(w[i - 15], 18)
                          ^ (w[i - 15] >> 3);
        const uint32_t s1 = _rotr32(w[i - 2], 17) ^ _rotr32(w[i - 2], 19)
                          ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = h[0]; b = h[1]; c = h[2]; d = h[3];
    e = h[4]; f = h[5]; g = h[6]; hh = h[7];
    for (i = 0; i < 64; ++i) {
        const uint32_t s1 = _rotr32(e, 6) ^ _rotr32(e, 11) ^ _rotr32(e, 25);
        const uint32_t ch = (e & f) ^ (~e & g);
        const uint32_t t1 = hh + s1 + ch + _k256[i] + w[i];
        const uint32_t s0 = _rotr32(a, 2) ^ _rotr32(a, 13) ^ _rotr32(a, 22);
        const uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        const uint32_t t2 = s0 + maj;
        hh = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d;
    h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
}

int gptbridge_sr_sha256_hex(const uint8_t* data, size_t len, char* out_hex) {
    static const char* hexd = "0123456789abcdef";
    uint32_t h[8] = {
        0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
        0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u
    };
    uint8_t block[64];
    size_t pos = 0;
    uint64_t bit_len = (uint64_t)len * 8u;
    int i;
    if (out_hex == NULL) {
        return 0;
    }
    if (data == NULL) {
        data = (const uint8_t*)"";
        len = 0;
    }
    while (len - pos >= 64) {
        _sha256_block(h, data + pos);
        pos += 64;
    }
    /* padding: 0x80, zeros, 64-bit big-endian length */
    memset(block, 0, sizeof(block));
    memcpy(block, data + pos, len - pos);
    block[len - pos] = 0x80;
    if (len - pos >= 56) {
        _sha256_block(h, block);
        memset(block, 0, sizeof(block));
    }
    for (i = 0; i < 8; ++i) {
        block[63 - i] = (uint8_t)(bit_len >> (i * 8));
    }
    _sha256_block(h, block);
    for (i = 0; i < 8; ++i) {
        out_hex[i * 8 + 0] = hexd[(h[i] >> 28) & 0xf];
        out_hex[i * 8 + 1] = hexd[(h[i] >> 24) & 0xf];
        out_hex[i * 8 + 2] = hexd[(h[i] >> 20) & 0xf];
        out_hex[i * 8 + 3] = hexd[(h[i] >> 16) & 0xf];
        out_hex[i * 8 + 4] = hexd[(h[i] >> 12) & 0xf];
        out_hex[i * 8 + 5] = hexd[(h[i] >> 8) & 0xf];
        out_hex[i * 8 + 6] = hexd[(h[i] >> 4) & 0xf];
        out_hex[i * 8 + 7] = hexd[h[i] & 0xf];
    }
    out_hex[64] = '\0';
    return 1;
}

gptbridge_sr_cli_op_t gptbridge_sr_cli_dispatch(
    int32_t all_flag,
    const char* tool_id,
    int32_t verify_flag,
    int32_t deep_flag,
    int32_t package_flag) {
    const int32_t has_tool = tool_id != NULL && tool_id[0] != '\0';
    if (all_flag && verify_flag) {
        return GPTBRIDGE_SR_OP_VERIFY_ALL;
    }
    if (has_tool && verify_flag && deep_flag) {
        return GPTBRIDGE_SR_OP_DEEP_VERIFY_TOOL;
    }
    if (has_tool && verify_flag) {
        return GPTBRIDGE_SR_OP_VERIFY_TOOL;
    }
    if (has_tool && package_flag) {
        return GPTBRIDGE_SR_OP_PACKAGE_TOOL;
    }
    if (all_flag && package_flag) {
        return GPTBRIDGE_SR_OP_PACKAGE_ALL;
    }
    if (all_flag) {
        return GPTBRIDGE_SR_OP_VERIFY_ALL;
    }
    return GPTBRIDGE_SR_OP_INVALID;
}
