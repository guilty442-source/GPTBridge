// xc_modeltool.cpp ??governed model-side utilities for the native lane.
//
// Modes (single JSON object on stdout; non-zero exit on failure):
//   tokenize      --tokenizer <tokenizer.json|dir> --in rows.jsonl
//                 --out rows.jsonl [--max-length N] [--chat]
//   corpus        --registry <corpus-registry.json> --root <repo-root>
//                 --tokenizer <tokenizer.json|dir> --out <dir>
//                 [--max-len N] [--val-ratio PCT] [--max-docs N]
//                 [--max-doc-chars N] [--max-tokens N]
//   import-bundle --bundle <dir> --out <ckpt.xcn>
//   distill-init  --teacher <bundle-dir|ckpt.xcn> --config <model.json>
//                 --out <student.xcn> [--seed N] [--overwrite]
//   export-bundle --ckpt <file> --out <dir>
//                 --config-from <manifest.json> --tokenizer <tokenizer.json>
//                 [--quant none|int8|int4_packed|bf16]
//   precision     --ref <bundle> --candidate <bundle> [--tol F] [--seed N] [--len N]
//                 (precision-candidate validation: logits/NLL/greedy/memory)
//   eval          --bundle <dir> --suite <suite.json>
//                 [--baseline-bundle <dir>|--baseline-metrics <file>]
//   capability    --bundle <dir> --suite <suite.json>
//                 [--corpus-manifest <manifest.json>] [--chat]
//                 [--baseline-report <file>]
//   serve         --bundle <dir>   (stdin/stdout JSON-lines worker)
//   vision-smoke  --bundle <vision-bundle-dir> [--patches N] [--seed N]
//   cache-smoke   --bundle <dir> [--seed N] [--kv-int8]
//                 (paged-KV / prefix-cache determinism probe)
//   mtp-draft-probe --bundle <dir> --prompt <text> [--max-new N]
//                 (draft-length-1 acceptance evidence vs the exported
//                  MTP head; SPECULATIVE_DECODER_DISABLED stays in
//                  effect ??no production dispatch is bound)
//
// Tokenize row shapes (star SFT/DPO/pretrain contracts):
//   {"prompt","completion"}      -> {"input_ids","labels"}  (masked prompt)
//   {"text"}                     -> {"input_ids"}           (pretrain; trainer
//                                  shifts labels itself)
//   {"prompt","chosen","rejected"} -> {"chosen","rejected"} (DPO)
//   any sft/pretrain row may carry "vision_patches":[[..D..] x P] ??the
//   early-fusion vision grid is passed through verbatim after structural
//   validation (dpo+vision is unsupported and drops); optional
//   --vision-patch-dim N / --vision-max-patches N pin the geometry.
//
// Labels are emitted next-token-aligned (labels[t] = ids[t+1]) because
// xct's ce_loss consumes aligned labels directly and only shifts for the
// pretrain task/format.
//
// Bundles are self-contained (manifest.json + weights.bin fp64 or
// int8/int4_packed quantized tensors + tokenizer.json); checkpoints are
// the XCN2 fp32 format emitted by xingcheng_trainer. import/export
// translate tensor names bidirectionally so trained weights become
// evaluable/inferable through the C++ engine.
//
// Data residency: this tool only reads/writes paths given on the command
// line; the C# orchestrator owns the tool-root confinement checks.

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <limits>
#include <map>
#include <mutex>
#include <numeric>
#include <set>
#include <random>
#include <regex>
#include <sstream>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#if defined(_M_X64) || defined(__x86_64__)
#include <immintrin.h>
#include <intrin.h>
#endif

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#include <bcrypt.h>
#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "psapi.lib")
#pragma comment(lib, "Normaliz.lib")

#include "jsonlite.h"
#include "xingcheng_inference.hpp"

using gptbridge::jsonlite::JsonParser;
using gptbridge::jsonlite::JsonValue;

namespace xct {
#include "xct_util.h"
#include "xct_tpu.h"
#include "xct_math.h"
#include "xct_gemma4.h"
#include "xct_backward.h"
#include "xct_mtp.h"
#include "xct_ckpt.h"
}  // namespace xct

// batch-2 probe infrastructure (蝳?/蝳?0/蝳?2/蝳?3/蝳?7/蝳?9): xcm2 namespace ??
// research/probe surfaces only, never wired into production dispatch.
#include "xcm_batch2.h"

namespace {

namespace fs = std::filesystem;
using xingcheng::inference::ByteLevelBPETokenizer;
using xingcheng::inference::NativeInferenceEngine;
using xingcheng::inference::SamplingConfig;

// ---------------------------------------------------------------- args ----

struct Args {
    std::unordered_map<std::string, std::string> kv;
    std::unordered_set<std::string> flags;
    std::string get(const char* k, const std::string& d = "") const {
        auto it = kv.find(k);
        return it == kv.end() ? d : it->second;
    }
    bool has(const char* k) const {
        return kv.count(k) || flags.count(k);
    }
    // Numeric arg lookup; accepts both "key" and "--key" spellings.
    int64_t num_arg(const char* k, int64_t d = 0) const {
        std::string key = k;
        while (key.rfind("--", 0) == 0) key = key.substr(2);
        auto it = kv.find(key);
        if (it == kv.end()) return d;
        try {
            return std::stoll(it->second);
        } catch (...) {
            return d;
        }
    }
};

Args parse_args(int argc, char** argv) {
    Args a;
    for (int i = 2; i < argc; ++i) {
        std::string s = argv[i];
        if (s.rfind("--", 0) != 0) continue;
        std::string key = s.substr(2);
        if (i + 1 < argc && std::string(argv[i + 1]).rfind("--", 0) != 0) {
            a.kv[key] = argv[++i];
        } else {
            a.flags.insert(key);
        }
    }
    return a;
}

[[noreturn]] void fail(const std::string& code) {
    std::printf("{\"ok\":false,\"error\":\"%s\"}\n",
                gptbridge::jsonlite::json_escape(code).c_str());
    std::exit(1);
}

void emit_line(const std::string& line) { std::printf("%s\n", line.c_str()); }

// Minimal flat-object JSON writer for certification/probe reports.
struct JsonWriter {
    std::ostringstream o;
    bool first = true;
    JsonWriter& begin() { o << "{"; first = true; return *this; }
    JsonWriter& end() { o << "}"; return *this; }
    JsonWriter& kv(const char* k, const std::string& v) {
        if (!first) o << ",";
        first = false;
        o << "\"" << k << "\":\"" << gptbridge::jsonlite::json_escape(v) << "\"";
        return *this;
    }
    JsonWriter& kv(const char* k, const char* v) { return kv(k, std::string(v)); }
    JsonWriter& kv(const char* k, bool v) {
        if (!first) o << ",";
        first = false;
        o << "\"" << k << "\":" << (v ? "true" : "false");
        return *this;
    }
    template <typename I, typename = std::enable_if_t<std::is_integral_v<I>>>
    JsonWriter& kv(const char* k, I v) {
        if (!first) o << ",";
        first = false;
        o << "\"" << k << "\":" << (long long)v;
        return *this;
    }
    JsonWriter& kv(const char* k, double v) {
        if (!first) o << ",";
        first = false;
        char buf[64];
        auto r = std::to_chars(buf, buf + sizeof(buf), v);
        o << "\"" << k << "\":" << std::string(buf, r.ptr);
        return *this;
    }
    std::string str() const { return o.str(); }
};

std::string slurp(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) fail("FILE_UNREADABLE:" + path);
    std::ostringstream ss;
    ss << f.rdbuf();
    std::string s = ss.str();
    // UTF-8 BOM is presentation-layer noise; strip it so BOM'd fixtures
    // hash/parse identically to BOM-free ones.
    if (s.size() >= 3 && (unsigned char)s[0] == 0xEF &&
        (unsigned char)s[1] == 0xBB && (unsigned char)s[2] == 0xBF) {
        s.erase(0, 3);
    }
    return s;
}

// -------------------------------------------------------------- sha256 ----

std::string sha256_bytes(const unsigned char* data, size_t size) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    if (BCryptOpenAlgorithmProvider(
            &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0) {
        fail("SHA256_PROVIDER_UNAVAILABLE");
    }
    BCRYPT_HASH_HANDLE hash = nullptr;
    unsigned char digest[32];
    std::string out;
    if (BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) == 0 &&
        BCryptHashData(hash, const_cast<unsigned char*>(data),
                       (ULONG)size, 0) == 0 &&
        BCryptFinishHash(hash, digest, sizeof(digest), 0) == 0) {
        static const char* hexd = "0123456789abcdef";
        out.reserve(64);
        for (unsigned char b : digest) {
            out += hexd[b >> 4];
            out += hexd[b & 15];
        }
    }
    if (hash) BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (out.empty()) fail("SHA256_HASH_FAILED");
    return out;
}

std::string sha256_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) fail("FILE_UNREADABLE:" + path);
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    if (BCryptOpenAlgorithmProvider(
            &algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0) {
        fail("SHA256_PROVIDER_UNAVAILABLE");
    }
    BCRYPT_HASH_HANDLE hash = nullptr;
    unsigned char digest[32];
    std::vector<char> buf(1 << 20);
    bool ok = BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) == 0;
    while (ok && f) {
        f.read(buf.data(), (std::streamsize)buf.size());
        std::streamsize n = f.gcount();
        if (n > 0) {
            ok = BCryptHashData(hash, (unsigned char*)buf.data(),
                                (ULONG)n, 0) == 0;
        }
    }
    std::string out;
    if (ok && BCryptFinishHash(hash, digest, sizeof(digest), 0) == 0) {
        static const char* hexd = "0123456789abcdef";
        out.reserve(64);
        for (unsigned char b : digest) {
            out += hexd[b >> 4];
            out += hexd[b & 15];
        }
    }
    if (hash) BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (out.empty()) fail("SHA256_HASH_FAILED");
    return out;
}

std::string sha256_text(const std::string& s) {
    return sha256_bytes((const unsigned char*)s.data(), s.size());
}

// ------------------------------------------------- Python-parity canon ----
// Canonical form used for suite_sha256 / payload hashes:
// json.dumps(obj, ensure_ascii=False, sort_keys=True) ??separators
// (", ", ": "). Works on raw JSON text so integer vs float lexemes are
// preserved (3000 -> "3000"; 3000.0 -> "3000.0"), matching Python digests
// for the existing suite files byte-exactly.

struct RawCur {
    const char* p;
    const char* end;
    void ws() { while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p; }
    char peek() { return p < end ? *p : '\0'; }
    char take() { return p < end ? *p++ : '\0'; }
};

void raw_utf8_append(std::string& out, uint32_t cp) {
    if (cp < 0x80) out += (char)cp;
    else if (cp < 0x800) {
        out += (char)(0xC0 | (cp >> 6));
        out += (char)(0x80 | (cp & 63));
    } else if (cp < 0x10000) {
        out += (char)(0xE0 | (cp >> 12));
        out += (char)(0x80 | ((cp >> 6) & 63));
        out += (char)(0x80 | (cp & 63));
    } else {
        out += (char)(0xF0 | (cp >> 18));
        out += (char)(0x80 | ((cp >> 12) & 63));
        out += (char)(0x80 | ((cp >> 6) & 63));
        out += (char)(0x80 | (cp & 63));
    }
}

// Decode a JSON string literal (cursor inside quotes) to raw UTF-8.
std::string raw_string_decode(RawCur& c) {
    std::string out;
    while (c.p < c.end) {
        char ch = c.take();
        if (ch == '"') return out;
        if (ch != '\\') { out += ch; continue; }
        char e = c.take();
        switch (e) {
            case '"': out += '"'; break;
            case '\\': out += '\\'; break;
            case '/': out += '/'; break;
            case 'b': out += '\b'; break;
            case 'f': out += '\f'; break;
            case 'n': out += '\n'; break;
            case 'r': out += '\r'; break;
            case 't': out += '\t'; break;
            case 'u': {
                uint32_t cp = 0;
                for (int i = 0; i < 4 && c.p < c.end; ++i) {
                    char h = c.take();
                    cp <<= 4;
                    cp |= (h >= '0' && h <= '9') ? (uint32_t)(h - '0')
                          : (h >= 'a' && h <= 'f') ? (uint32_t)(h - 'a' + 10)
                          : (h >= 'A' && h <= 'F') ? (uint32_t)(h - 'A' + 10) : 0;
                }
                if (cp >= 0xD800 && cp <= 0xDBFF && c.peek() == '\\') {
                    RawCur save = c;
                    c.take();
                    if (c.peek() == 'u') {
                        c.take();
                        uint32_t lo = 0;
                        bool ok = true;
                        for (int i = 0; i < 4 && c.p < c.end; ++i) {
                            char h = c.take();
                            if (!isxdigit((unsigned char)h)) { ok = false; break; }
                            lo <<= 4;
                            lo |= (h >= '0' && h <= '9') ? (uint32_t)(h - '0')
                                  : (h >= 'a' && h <= 'f') ? (uint32_t)(h - 'a' + 10)
                                  : (uint32_t)(h - 'A' + 10);
                        }
                        if (ok && lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        } else {
                            c = save;
                        }
                    } else {
                        c = save;
                    }
                }
                raw_utf8_append(out, cp);
                break;
            }
            default: fail("CANON_BAD_ESCAPE");
        }
    }
    fail("CANON_UNTERMINATED_STRING");
}

// Python ensure_ascii=False string rendering.
std::string py_escape(const std::string& raw) {
    std::string out = "\"";
    for (size_t i = 0; i < raw.size(); ++i) {
        unsigned char ch = (unsigned char)raw[i];
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", ch);
                    out += buf;
                } else {
                    out += (char)ch;
                }
        }
    }
    out += '"';
    return out;
}

// Python repr() of a float: shortest round-trip, trailing ".0" when integral.
std::string py_float(double v) {
    if (!std::isfinite(v)) fail("CANON_NONFINITE_NUMBER");
    char buf[64];
    auto res = std::to_chars(buf, buf + sizeof(buf), v);
    std::string s(buf, res.ptr);
    // Python pads exponents to >=2 digits ("1e-05"); MSVC to_chars does too.
    if (s.find_first_of(".eE") == std::string::npos) s += ".0";
    // Python uses 'e-05'/'e+300' (lower e, explicit sign); to_chars matches.
    return s;
}

void canon_value(RawCur& c, std::string& out);

void canon_number(RawCur& c, std::string& out) {
    const char* start = c.p;
    bool is_float = false;
    while (c.p < c.end &&
           (isdigit((unsigned char)c.peek()) || c.peek() == '-' ||
            c.peek() == '+' || c.peek() == '.' || c.peek() == 'e' ||
            c.peek() == 'E')) {
        if (c.peek() == '.' || c.peek() == 'e' || c.peek() == 'E') is_float = true;
        c.take();
    }
    std::string lex(start, c.p);
    if (!is_float) { out += lex; return; }
    // Normalize float lexeme through double (Python parse->repr semantics).
    out += py_float(std::strtod(lex.c_str(), nullptr));
}

void canon_value(RawCur& c, std::string& out) {
    c.ws();
    char ch = c.peek();
    if (ch == '{') {
        c.take();
        std::vector<std::pair<std::string, std::string>> members;
        c.ws();
        if (c.peek() == '}') { c.take(); out += "{}"; return; }
        while (true) {
            c.ws();
            if (c.take() != '"') fail("CANON_BAD_OBJECT_KEY");
            std::string key_raw = raw_string_decode(c);
            c.ws();
            if (c.take() != ':') fail("CANON_BAD_OBJECT");
            std::string val;
            canon_value(c, val);
            members.emplace_back(std::move(key_raw), std::move(val));
            c.ws();
            char sep = c.take();
            if (sep == '}') break;
            if (sep != ',') fail("CANON_BAD_OBJECT");
        }
        std::sort(members.begin(), members.end(),
                  [](const auto& a, const auto& b) { return a.first < b.first; });
        out += '{';
        for (size_t i = 0; i < members.size(); ++i) {
            if (i) out += ", ";
            out += py_escape(members[i].first);
            out += ": ";
            out += members[i].second;
        }
        out += '}';
        return;
    }
    if (ch == '[') {
        c.take();
        std::vector<std::string> items;
        c.ws();
        if (c.peek() == ']') { c.take(); out += "[]"; return; }
        while (true) {
            std::string v;
            canon_value(c, v);
            items.push_back(std::move(v));
            c.ws();
            char sep = c.take();
            if (sep == ']') break;
            if (sep != ',') fail("CANON_BAD_ARRAY");
        }
        out += '[';
        for (size_t i = 0; i < items.size(); ++i) {
            if (i) out += ", ";
            out += items[i];
        }
        out += ']';
        return;
    }
    if (ch == '"') {
        c.take();
        out += py_escape(raw_string_decode(c));
        return;
    }
    if (ch == 't' && c.end - c.p >= 4 && std::strncmp(c.p, "true", 4) == 0) {
        c.p += 4; out += "true"; return;
    }
    if (ch == 'f' && c.end - c.p >= 5 && std::strncmp(c.p, "false", 5) == 0) {
        c.p += 5; out += "false"; return;
    }
    if (ch == 'n' && c.end - c.p >= 4 && std::strncmp(c.p, "null", 4) == 0) {
        c.p += 4; out += "null"; return;
    }
    canon_number(c, out);
}

// sha256 of json.dumps(obj, sort_keys=True) ??default separators.
std::string suite_sha256(const std::string& raw_json) {
    RawCur c{raw_json.data(), raw_json.data() + raw_json.size()};
    std::string canon;
    canon_value(c, canon);
    c.ws();
    if (c.p != c.end) fail("CANON_TRAILING_DATA");
    return sha256_text(canon);
}

// ------------------------------------------------------------- NFC trim ----
// Python unicodedata.normalize("NFC", text).strip() ??used for overlap
// hashing. Windows NormalizeString handles NFC; trim strips ASCII/Unicode
// whitespace sentinels at both ends.

std::string nfc(const std::string& s) {
    if (s.empty()) return s;
    int wlen = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(),
                                   nullptr, 0);
    if (wlen <= 0) fail("NFC_DECODE_FAILED");
    std::wstring w((size_t)wlen, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), wlen);
    int nlen = NormalizeString(NormalizationC, w.data(), wlen, nullptr, 0);
    if (nlen <= 0) return s;  // already normalized or empty
    std::wstring n((size_t)nlen, L'\0');
    int got = NormalizeString(NormalizationC, w.data(), wlen, n.data(), nlen);
    if (got <= 0) fail("NFC_NORMALIZE_FAILED");
    n.resize((size_t)got);
    int ulen = WideCharToMultiByte(CP_UTF8, 0, n.data(), got, nullptr, 0,
                                   nullptr, nullptr);
    if (ulen <= 0) fail("NFC_ENCODE_FAILED");
    std::string out((size_t)ulen, '\0');
    WideCharToMultiByte(CP_UTF8, 0, n.data(), got, out.data(), ulen,
                        nullptr, nullptr);
    return out;
}

bool unicode_space(uint32_t cp) {
    switch (cp) {
        case 0x09: case 0x0A: case 0x0B: case 0x0C: case 0x0D: case 0x20:
        case 0x85: case 0xA0: case 0x1680:
        case 0x2000: case 0x2001: case 0x2002: case 0x2003: case 0x2004:
        case 0x2005: case 0x2006: case 0x2007: case 0x2008: case 0x2009:
        case 0x200A: case 0x2028: case 0x2029: case 0x202F: case 0x205F:
        case 0x3000: case 0x1C: case 0x1D: case 0x1E: case 0x1F:
            return true;
        default: return false;
    }
}

std::string py_strip(const std::string& s) {
    size_t b = 0, e = s.size();
    auto next_cp = [&](size_t i, uint32_t& cp, size_t& len) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x80) { cp = c; len = 1; }
        else if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; len = 2; }
        else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; len = 3; }
        else { cp = c & 0x07; len = 4; }
        for (size_t k = 1; k < len && i + k < s.size(); ++k)
            cp = (cp << 6) | ((unsigned char)s[i + k] & 0x3F);
    };
    while (b < e) { uint32_t cp; size_t l; next_cp(b, cp, l); if (!unicode_space(cp)) break; b += l; }
    while (e > b) {
        // decode the last codepoint
        size_t i = e - 1;
        while (i > b && ((unsigned char)s[i] & 0xC0) == 0x80) --i;
        uint32_t cp; size_t l; next_cp(i, cp, l);
        if (!unicode_space(cp) || i + l != e) break;
        e = i;
    }
    return s.substr(b, e - b);
}

std::string norm_text_sha(const std::string& text) {
    return sha256_text(py_strip(nfc(text)));
}

// ------------------------------------------------------------ jsonl io ----

std::vector<JsonValue> read_jsonl(const std::string& path) {
    std::vector<JsonValue> rows;
    std::ifstream f(path, std::ios::binary);
    if (!f) fail("FILE_UNREADABLE:" + path);
    std::string line;
    bool first = true;
    while (std::getline(f, line)) {
        if (first && line.size() >= 3 &&
            (unsigned char)line[0] == 0xEF && (unsigned char)line[1] == 0xBB &&
            (unsigned char)line[2] == 0xBF) {
            line.erase(0, 3);  // UTF-8 BOM
        }
        first = false;
        if (line.empty() || line.find_first_not_of(" \t\r") == std::string::npos)
            continue;
        JsonParser p(line);
        rows.push_back(p.parse());
    }
    return rows;
}

std::string jget_str(const JsonValue& o, const char* k) {
    const JsonValue* v = o.get(k);
    return (v && v->type == JsonValue::Type::String) ? v->string : "";
}

// ------------------------------------------------------------- tokenize ---

std::string resolve_tokenizer_path(const std::string& path_or_dir) {
    fs::path p(path_or_dir);
    if (fs::is_directory(p)) p /= "tokenizer.json";
    return p.string();
}

struct Ids { std::vector<int64_t> ids; std::vector<int64_t> labels; };

Ids sft_encode(const ByteLevelBPETokenizer& tk, const std::string& prompt,
               const std::string& completion, bool chat, int64_t eos_id) {
    std::string pside, cside;
    if (chat) {
        // encode_conversation parity: mask everything through the
        // assistant header; supervise completion + <|eot|>.
        pside = "<|user|>\n" + py_strip(prompt) + "\n<|eot|>\n<|assistant|>\n";
        cside = py_strip(completion) + "\n<|eot|>";
    } else {
        pside = py_strip(prompt) + "\n\n";   // sft_text boundary
        cside = py_strip(completion);
    }
    std::vector<int64_t> ids = tk.encode(pside, true, false);
    std::vector<int64_t> cids = tk.encode(cside, false, false);
    size_t mask = ids.size();
    ids.insert(ids.end(), cids.begin(), cids.end());
    if (!chat && eos_id >= 0) ids.push_back(eos_id);
    Ids r;
    r.ids = ids;
    r.labels.assign(ids.size(), -100);
    for (size_t i = mask; i + 1 <= ids.size(); ++i) {
        // labels[i-1] supervises ids[i]
        if (i - 1 < r.labels.size()) r.labels[i - 1] = ids[i];
    }
    return r;
}

void ids_json(std::string& out, const char* key,
              const std::vector<int64_t>& v) {
    out += '"';
    out += key;
    out += "\":[";
    for (size_t i = 0; i < v.size(); ++i) {
        if (i) out += ',';
        out += std::to_string(v[i]);
    }
    out += ']';
}

// Truncate post-alignment to max_len; returns false when no supervised
// (label >= 0) positions remain ??those rows are dropped, not emitted.
bool clip_ids(Ids& e, int64_t max_len) {
    if (max_len <= 0 || (int64_t)e.ids.size() <= max_len) {
        return std::any_of(e.labels.begin(), e.labels.end(),
                           [](int64_t y) { return y >= 0; });
    }
    e.ids.resize((size_t)max_len);
    e.labels.resize((size_t)max_len);
    return std::any_of(e.labels.begin(), e.labels.end(),
                       [](int64_t y) { return y >= 0; });
}

// Fusion tokenization (v1): a `vision_patches` field carries the patch
// grid through to trainer-ready rows ??for early-fusion the grid IS the
// vision token stream (prefix positions; the trainer masks them -100
// and checks geometry against the model config). Structural validation
// mirrors xct_util.h j_patch_grid: array of P numeric rows of uniform
// width D. Returns 0 absent, 1 well-formed, -1 malformed ??a malformed
// grid drops the whole row; never emit a text-only copy of a multimodal
// sample.
int vision_grid_state(const JsonValue* v, int64_t& patches, int64_t& dim) {
    if (!v) return 0;
    if (v->type != JsonValue::Type::Array) return -1;
    patches = (int64_t)v->array.size();
    dim = -1;
    for (const auto& pr : v->array) {
        if (pr.type != JsonValue::Type::Array) return -1;
        if (dim < 0) dim = (int64_t)pr.array.size();
        if ((int64_t)pr.array.size() != dim || dim <= 0) return -1;
        for (const auto& x : pr.array)
            if (x.type != JsonValue::Type::Number) return -1;
    }
    return (patches > 0 && dim > 0) ? 1 : -1;
}

int mode_tokenize(const Args& a) {
    std::string tk_path = resolve_tokenizer_path(a.get("tokenizer"));
    ByteLevelBPETokenizer tk = ByteLevelBPETokenizer::load(tk_path);
    int64_t max_len = a.has("max-length")
                          ? std::stoll(a.get("max-length")) : 0;
    bool chat = a.has("chat");
    // Optional geometry pins for the vision grid; absent = structural
    // validation only (the trainer re-checks against the model config).
    int64_t vision_pdim = 0, vision_pmax = 0;
    if (a.has("vision-patch-dim")) {
        try { vision_pdim = std::stoll(a.get("vision-patch-dim")); }
        catch (...) { fail("TOKENIZE_VISION_ARGS"); }
    }
    if (a.has("vision-max-patches")) {
        try { vision_pmax = std::stoll(a.get("vision-max-patches")); }
        catch (...) { fail("TOKENIZE_VISION_ARGS"); }
    }
    // eos: encode("", add_eos) returns {eos_id}; -1 when undetectable.
    std::vector<int64_t> eos_probe = tk.encode("", false, true);
    int64_t eos_id = eos_probe.empty() ? -1 : eos_probe.back();

    std::string in_path = a.get("in"), out_path = a.get("out");
    if (in_path.empty() || out_path.empty()) fail("TOKENIZE_ARGS_MISSING");
    std::vector<JsonValue> rows = read_jsonl(in_path);
    std::ofstream out(out_path, std::ios::binary | std::ios::trunc);
    if (!out) fail("TOKENIZE_OUT_UNWRITABLE");
    int64_t n_in = 0, n_out = 0, n_dropped = 0, n_vision = 0;
    for (const JsonValue& row : rows) {
        ++n_in;
        std::string prompt = jget_str(row, "prompt");
        const JsonValue* chosen = row.get("chosen");
        const JsonValue* rejected = row.get("rejected");
        const JsonValue* text = row.get("text");
        const JsonValue* vp = row.get("vision_patches");
        int64_t v_p = 0, v_d = 0;
        int v_state = vision_grid_state(vp, v_p, v_d);
        if (v_state < 0 ||
            (v_state > 0 && ((vision_pdim > 0 && v_d != vision_pdim) ||
                             (vision_pmax > 0 && v_p > vision_pmax)))) {
            ++n_dropped;
            continue;
        }
        std::string line;
        if (chosen && rejected) {
            if (v_state > 0) { ++n_dropped; continue; }
            std::string ch = chosen->type == JsonValue::Type::String
                                 ? chosen->string : jget_str(row, "chosen_text");
            std::string rj = rejected->type == JsonValue::Type::String
                                 ? rejected->string : jget_str(row, "rejected_text");
            Ids c = sft_encode(tk, prompt, ch, chat, eos_id);
            Ids r = sft_encode(tk, prompt, rj, chat, eos_id);
            if (!clip_ids(c, max_len) || !clip_ids(r, max_len)) {
                ++n_dropped;
                continue;
            }
            line = "{\"chosen\":{";
            ids_json(line, "input_ids", c.ids);
            line += ',';
            ids_json(line, "labels", c.labels);
            line += "},\"rejected\":{";
            ids_json(line, "input_ids", r.ids);
            line += ',';
            ids_json(line, "labels", r.labels);
            line += "}}";
        } else if (text) {
            std::vector<int64_t> ids = tk.encode(text->string, true, true);
            if (max_len > 0 && (int64_t)ids.size() > max_len)
                ids.resize((size_t)max_len);
            if (ids.size() < 2) { ++n_dropped; continue; }
            line = "{";
            ids_json(line, "input_ids", ids);
            line += '}';
        } else {
            std::string completion = jget_str(row, "completion");
            if (completion.empty()) completion = jget_str(row, "target_text");
            if (prompt.empty() || completion.empty()) { ++n_dropped; continue; }
            Ids e = sft_encode(tk, prompt, completion, chat, eos_id);
            if (!clip_ids(e, max_len)) { ++n_dropped; continue; }
            line = "{";
            ids_json(line, "input_ids", e.ids);
            line += ',';
            ids_json(line, "labels", e.labels);
            line += '}';
        }
        if (v_state > 0) {
            line.resize(line.size() - 1);              // drop '}'
            line += ",\"vision_patches\":";
            line += gptbridge::jsonlite::json_serialize(*vp);
            line += '}';
            ++n_vision;
        }
        out << line << '\n';
        ++n_out;
    }
    out.close();
    std::printf("{\"ok\":true,\"mode\":\"tokenize\",\"rows_in\":%lld,"
                "\"rows_out\":%lld,\"rows_dropped\":%lld,"
                "\"vision_rows\":%lld,\"eos_id\":%lld,"
                "\"tokenizer_sha256\":\"%s\"}\n",
                (long long)n_in, (long long)n_out, (long long)n_dropped,
                (long long)n_vision,
                (long long)eos_id, sha256_file(tk_path).c_str());
    return 0;
}

// ----------------------------------------------------- name translation ---

// `g4` selects the Gemma4 tensor contract: post_attention_norm is the
// post-attention norm (xct `norm_attn`) and the pre-FFN norm lives at
// pre_feedforward_norm (`norm2`); generic bundles keep the legacy
// two-norm reading where post_attention_norm feeds the FFN (`norm2`).
std::string bundle_to_xct(const std::string& b, bool g4 = false) {
    static const std::unordered_map<std::string, std::string> fixed = {
        {"model.embeddings.word_embeddings.weight", "embed"},
        {"lm_head.weight", "lm_head"},
        {"model.final_norm.weight", "norm_f"},
        {"model.embed_tokens_per_layer.weight", "embed_ple"},
        {"model.per_layer_model_projection.weight", "ple_model_proj"},
        {"model.per_layer_projection_norm.weight", "ple_proj_norm"},
    };
    auto it = fixed.find(b);
    if (it != fixed.end()) return it->second;
    std::smatch m;
    std::string l, e, s;
    std::string base = "layers.";
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.input_norm\.weight$)")))
        return base + m[1].str() + ".norm1";
    if (g4) {
        if (std::regex_match(b, m,
                std::regex(R"(^model\.layers\.(\d+)\.attention\.(q|k)_norm\.weight$)")))
            return base + m[1].str() + "." + m[2].str() + "_norm";
        if (std::regex_match(b, m,
                std::regex(R"(^model\.layers\.(\d+)\.post_attention_norm\.weight$)")))
            return base + m[1].str() + ".norm_attn";
        if (std::regex_match(b, m,
                std::regex(R"(^model\.layers\.(\d+)\.pre_feedforward_norm\.weight$)")))
            return base + m[1].str() + ".norm2";
        if (std::regex_match(b, m,
                std::regex(R"(^model\.layers\.(\d+)\.post_feedforward_norm\.weight$)")))
            return base + m[1].str() + ".norm_ffn";
        if (std::regex_match(b, m,
                std::regex(R"(^model\.layers\.(\d+)\.per_layer_input_gate\.weight$)")))
            return base + m[1].str() + ".ple_gate";
        if (std::regex_match(b, m,
                std::regex(R"(^model\.layers\.(\d+)\.per_layer_projection\.weight$)")))
            return base + m[1].str() + ".ple_proj";
        if (std::regex_match(b, m,
                std::regex(R"(^model\.layers\.(\d+)\.post_per_layer_input_norm\.weight$)")))
            return base + m[1].str() + ".ple_post";
    }
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.post_attention_norm\.weight$)")))
        return base + m[1].str() + (g4 ? ".norm_attn" : ".norm2");
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.attention\.(q|k|v|o)_proj\.weight$)"))) {
        char w = m[2].str()[0];
        return base + m[1].str() + ".w" + w;
    }
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.attention\.(q|k)_norm\.weight$)")))
        return base + m[1].str() + "." + m[2].str() + "_norm";
    // v27 fused hybrid: gated DeltaNet linear-attention tensors keep the
    // trainer's lin.* layout under the HF-style linear_attn.* namespace.
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.linear_attn\.(in_proj_qkv|in_proj_z|in_proj_a|in_proj_b|conv1d|A_log|dt_bias|norm|out_proj)\.weight$)")))
        return base + m[1].str() + ".lin." + m[2].str();
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.mlp\.shared_expert_gate\.weight$)")))
        return base + m[1].str() + ".shared_gate";
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.mlp\.router\.weight$)")))
        return base + m[1].str() + ".gate";
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.mlp\.experts\.(\d+)\.(gate|up|down)_proj\.weight$)"))) {
        const char* w = m[3].str() == "gate" ? "w1"
                       : m[3].str() == "up" ? "w3" : "w2";
        return base + m[1].str() + ".experts." + m[2].str() + "." + w;
    }
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.mlp\.shared_experts\.(\d+)\.(gate|up|down)_proj\.weight$)"))) {
        const char* w = m[3].str() == "gate" ? "w1"
                       : m[3].str() == "up" ? "w3" : "w2";
        return base + m[1].str() + ".shared." + m[2].str() + "." + w;
    }
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.mlp\.(gate|up|down)_proj\.weight$)"))) {
        const char* w = m[2].str() == "gate" ? "w1"
                       : m[2].str() == "up" ? "w3" : "w2";
        return base + m[1].str() + "." + w;
    }
    // MTP import: model.mtp.<name>.weight round-trips back to the
    // trainer's mtp.<name> namespace so checkpoint->bundle->params stays
    // lossless for the speculative head.
    if (std::regex_match(b, m,
            std::regex(R"(^model\.mtp\.([A-Za-z0-9_.]+)\.weight$)")))
        return "mtp." + m[1].str();
    if (b == "vision.patch_proj.weight") return "vision.patch_proj";
    return "";
}

std::string xct_to_bundle(const std::string& n, bool g4 = false) {
    static const std::unordered_map<std::string, std::string> fixed = {
        {"embed", "model.embeddings.word_embeddings.weight"},
        {"lm_head", "lm_head.weight"},
        {"norm_f", "model.final_norm.weight"},
        {"embed_ple", "model.embed_tokens_per_layer.weight"},
        {"ple_model_proj", "model.per_layer_model_projection.weight"},
        {"ple_proj_norm", "model.per_layer_projection_norm.weight"},
    };
    auto it = fixed.find(n);
    if (it != fixed.end()) return it->second;
    std::smatch m;
    std::string base = "model.layers.";
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.norm1$)")))
        return base + m[1].str() + ".input_norm.weight";
    if (g4) {
        if (std::regex_match(n, m,
                std::regex(R"(^layers\.(\d+)\.(q|k)_norm$)")))
            return base + m[1].str() + ".attention." + m[2].str() +
                   "_norm.weight";
        if (std::regex_match(n, m,
                std::regex(R"(^layers\.(\d+)\.norm_attn$)")))
            return base + m[1].str() + ".post_attention_norm.weight";
        if (std::regex_match(n, m,
                std::regex(R"(^layers\.(\d+)\.norm2$)")))
            return base + m[1].str() + ".pre_feedforward_norm.weight";
        if (std::regex_match(n, m,
                std::regex(R"(^layers\.(\d+)\.norm_ffn$)")))
            return base + m[1].str() + ".post_feedforward_norm.weight";
        if (std::regex_match(n, m,
                std::regex(R"(^layers\.(\d+)\.ple_gate$)")))
            return base + m[1].str() + ".per_layer_input_gate.weight";
        if (std::regex_match(n, m,
                std::regex(R"(^layers\.(\d+)\.ple_proj$)")))
            return base + m[1].str() + ".per_layer_projection.weight";
        if (std::regex_match(n, m,
                std::regex(R"(^layers\.(\d+)\.ple_post$)")))
            return base + m[1].str() +
                   ".post_per_layer_input_norm.weight";
    }
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.norm2$)")))
        return base + m[1].str() + (g4 ? ".pre_feedforward_norm.weight"
                                       : ".post_attention_norm.weight");
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.w([qkvo])$)"))) {
        char w = m[2].str()[0];
        return base + m[1].str() + ".attention." + w + "_proj.weight";
    }
    if (std::regex_match(n, m,
            std::regex(R"(^layers\.(\d+)\.(q|k)_norm$)")))
        return base + m[1].str() + ".attention." + m[2].str() +
               "_norm.weight";
    // v27 gated DeltaNet (linear attention) tensors ??the bundle keeps
    // the trainer's lin.* naming under linear_attn.* so the engine loads
    // the same projections verbatim.
    if (std::regex_match(n, m,
            std::regex(R"(^layers\.(\d+)\.lin\.(in_proj_qkv|in_proj_z|in_proj_a|in_proj_b|conv1d|A_log|dt_bias|norm|out_proj)$)")))
        return base + m[1].str() + ".linear_attn." + m[2].str() +
               ".weight";
    if (std::regex_match(n, m,
            std::regex(R"(^layers\.(\d+)\.shared_gate$)")))
        return base + m[1].str() + ".mlp.shared_expert_gate.weight";
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.gate$)")))
        return base + m[1].str() + ".mlp.router.weight";
    if (std::regex_match(n, m,
            std::regex(R"(^layers\.(\d+)\.experts\.(\d+)\.w([123])$)"))) {
        const char* w = m[3].str() == "1" ? "gate"
                       : m[3].str() == "2" ? "down" : "up";
        return base + m[1].str() + ".mlp.experts." + m[2].str() + "." +
               w + "_proj.weight";
    }
    if (std::regex_match(n, m,
            std::regex(R"(^layers\.(\d+)\.shared\.(\d+)\.w([123])$)"))) {
        const char* w = m[3].str() == "1" ? "gate"
                       : m[3].str() == "2" ? "down" : "up";
        return base + m[1].str() + ".mlp.shared_experts." + m[2].str() + "." +
               w + "_proj.weight";
    }
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.w([123])$)"))) {
        const char* w = m[2].str() == "1" ? "gate"
                       : m[2].str() == "2" ? "down" : "up";
        return base + m[1].str() + ".mlp." + w + "_proj.weight";
    }
    // MTP export: the speculative head is part of the XCN10 contract ??
    // mtp.* tensors ride the bundle verbatim under model.mtp.* so a
    // runtime drafter can bind them; silently dropping them orphans the
    // trained draft head.
    if (std::regex_match(n, m,
            std::regex(R"(^mtp\.([A-Za-z0-9_.]+)$)")))
        return "model.mtp." + m[1].str() + ".weight";
    if (n == "vision.patch_proj") return "vision.patch_proj.weight";
    return "";
}

// ------------------------------------------------------- import-bundle ----

xct::ModelConfig config_from_manifest(const JsonValue& cfg) {
    // Shared parser (job.json + manifest config + distill student config)
    // ??also validates the Gemma4 profile fail-closed.
    xct::ModelConfig c;
    try {
        c = xct::parse_model(&cfg);
    } catch (const char* e) {
        fail(std::string("CONFIG_INVALID:") + e);
    }
    const bool use_moe = xct::j_bool(&cfg, "use_moe", false);
    if (!use_moe) {
        c.moe_experts = 0;
        c.moe_shared_experts = 0;
        c.moe_expert_inter = 0;
        c.moe_shared_inter = 0;
    }
    if (c.is_gemma4() &&
        (use_moe || xct::j_bool(&cfg, "enable_moe_block", false)))
        fail("CONFIG_GEMMA4_MOE_UNSUPPORTED");
    bool use_vision = false;
    const JsonValue* uv = cfg.get("use_vision");
    if (uv && uv->type == JsonValue::Type::Bool) use_vision = uv->boolean;
    if (use_vision) {
        c.use_vision = true;
        c.vision_patch_dim = (int)xct::j_num(&cfg, "vision_patch_dim", 0);
        c.vision_max_patches = (int)xct::j_num(&cfg, "vision_max_patches", 0);
        if (c.vision_patch_dim <= 0 || c.vision_max_patches <= 0)
            fail("IMPORT_VISION_GEOMETRY");
    }
    // v27 fused hybrid (XCN3 fields): same names/semantics as the job
    // manifest model block ??absent keys keep the dense defaults so
    // pre-v27 bundles import unchanged.
    c.full_attention_interval =
        (int)xct::j_num(&cfg, "full_attention_interval",
                        c.full_attention_interval);
    c.attn_output_gate =
        xct::j_bool(&cfg, "attn_output_gate", c.attn_output_gate);
    c.qk_norm = xct::j_bool(&cfg, "qk_norm", c.qk_norm);
    c.shared_expert_gate =
        xct::j_bool(&cfg, "shared_expert_gate", c.shared_expert_gate);
    c.moe_router_sigmoid =
        xct::j_bool(&cfg, "moe_router_sigmoid", c.moe_router_sigmoid);
    c.partial_rotary =
        (float)xct::j_num(&cfg, "partial_rotary_factor", c.partial_rotary);
    c.lin_key_heads =
        (int)xct::j_num(&cfg, "linear_num_key_heads", c.lin_key_heads);
    c.lin_key_dim =
        (int)xct::j_num(&cfg, "linear_key_head_dim", c.lin_key_dim);
    c.lin_value_heads =
        (int)xct::j_num(&cfg, "linear_num_value_heads", c.lin_value_heads);
    c.lin_value_dim =
        (int)xct::j_num(&cfg, "linear_value_head_dim", c.lin_value_dim);
    c.lin_conv_kernel =
        (int)xct::j_num(&cfg, "linear_conv_kernel_dim", c.lin_conv_kernel);
    if (c.full_attention_interval > 0 &&
        (c.lin_key_heads <= 0 || c.lin_key_dim <= 0 ||
         c.lin_value_heads <= 0 || c.lin_value_dim <= 0 ||
         c.lin_value_heads % c.lin_key_heads != 0))
        fail("IMPORT_LINEAR_ATTN_GEOMETRY");
    return c;
}

JsonValue parse_json_file(const std::string& path) {
    std::string raw = slurp(path);
    JsonParser p(raw);  // JsonParser holds a pointer into `raw` ??lvalue only.
    return p.parse();
}

int64_t numel_of(const JsonValue& shape) {
    int64_t n = 1;
    for (const auto& d : shape.array)
        n *= (int64_t)d.number;
    return n;
}

// Loads a bundle directory into xct Params (fp32). Fails closed on any
// dtype/shape/contract violation ??shared by import-bundle and
// distill-init. Returns the number of filled tensors.
size_t load_bundle_params(const fs::path& bundle, xct::ModelConfig& c,
                          xct::Params& p) {
    JsonValue manifest = parse_json_file((bundle / "manifest.json").string());
    const JsonValue* cfg = manifest.get("config");
    const JsonValue* tensors = manifest.get("tensors");
    if (!cfg || !tensors || tensors->type != JsonValue::Type::Object)
        fail("IMPORT_MANIFEST_INVALID");
    c = config_from_manifest(*cfg);
    xct::init_params(p, c, 0);

    std::ifstream bin((bundle / "weights.bin").string(), std::ios::binary);
    if (!bin) fail("IMPORT_WEIGHTS_UNREADABLE");
    std::unordered_set<std::string> filled;
    std::vector<std::string> unmapped;
    for (const auto& [name, info] : tensors->object) {
        std::string xname = bundle_to_xct(name, c.is_gemma4());
        if (xname.empty()) { unmapped.push_back(name); continue; }
        auto wIt = p.w.find(xname);
        if (wIt == p.w.end()) {
            // Bundle carries an MTP head the manifest config does not
            // declare ??bundle/trunk contract mismatch, fail closed.
            if (xname.compare(0, 4, "mtp.") == 0)
                fail("MTP_BUNDLE_MISMATCH:" + name);
            fail("IMPORT_CONFIG_SHAPE_MISMATCH:" + xname);
        }
        const JsonValue* off = info.get("offset");
        const JsonValue* shape = info.get("shape");
        const JsonValue* dtype = info.get("dtype");
        if (!off || !shape) fail("IMPORT_TENSOR_INFO_INVALID:" + name);
        if (dtype && dtype->string != "float64")
            fail("IMPORT_UNSUPPORTED_DTYPE:" + name);
        int64_t n = numel_of(*shape);
        if ((int64_t)wIt->second.numel() != n) {
            if (xname.compare(0, 4, "mtp.") == 0)
                fail("MTP_BUNDLE_MISMATCH:" + name);
            fail("IMPORT_TENSOR_NUMEL_MISMATCH:" + name);
        }
        bin.seekg((std::streamoff)(int64_t)off->number);
        std::vector<double> tmp((size_t)n);
        bin.read((char*)tmp.data(), (std::streamsize)n * 8);
        if (!bin) fail("IMPORT_WEIGHTS_SHORT:" + name);
        for (int64_t i = 0; i < n; ++i) wIt->second.d[(size_t)i] = (float)tmp[(size_t)i];
        filled.insert(xname);
    }
    if (!unmapped.empty()) {
        std::string list;
        for (auto& n : unmapped) { if (!list.empty()) list += ','; list += n; }
        fail("IMPORT_UNMAPPED_TENSORS:" + list);
    }
    for (const auto& n : p.order) {
        // lb_bias is a train-time routing buffer that never exports ??
        // absence from a bundle is not a contract violation. mtp.* must
        // be present when the config declares an MTP head.
        if (n.size() >= 7 &&
            n.compare(n.size() - 7, 7, "lb_bias") == 0) continue;
        if (!filled.count(n)) {
            // The manifest config declares an MTP head but the bundle
            // does not carry it ??the bundle is mismatched against the
            // trunk contract, not merely short a tensor.
            if (n.compare(0, 4, "mtp.") == 0)
                fail("MTP_BUNDLE_MISMATCH:bundle lacks " + n);
            fail("IMPORT_MISSING_TENSOR:" + n);
        }
    }
    return filled.size();
}

int mode_import_bundle(const Args& a) {
    fs::path bundle = a.get("bundle");
    std::string out = a.get("out");
    if (bundle.empty() || out.empty()) fail("IMPORT_ARGS_MISSING");
    xct::ModelConfig c;
    xct::Params p;
    const size_t filled = load_bundle_params(bundle, c, p);
    if (!xct::ckpt_save(p, c, out, /*overwrite=*/false))
        fail("IMPORT_CKPT_WRITE_FAILED:" + out);
    std::printf("{\"ok\":true,\"mode\":\"import-bundle\",\"out\":\"%s\","
                "\"ckpt_sha256\":\"%s\",\"tensors\":%zu}\n",
                gptbridge::jsonlite::json_escape(out).c_str(),
                sha256_file(out).c_str(), filled);
    return 0;
}

// -------------------------------------------------------- distill-init ----
// Sequence-level distillation companion: builds the student checkpoint by
// transplanting every teacher tensor whose shape matches the smaller
// architecture (embeddings, attention, norms, dense MLPs) so SFT starts
// from the teacher's language competence instead of a random init.
// Teacher MoE layers contribute only attention/norms; student dense MLPs
// are filled round-robin from the teacher's own dense layers.
//
//   xc_modeltool distill-init --teacher <bundle-dir|ckpt.xcn>
//       --config <student-model.json> --out <student.xcn>
//       [--seed N] [--overwrite]

static bool copy_if_same_shape(xct::Params& dst, const std::string& dname,
                               const xct::Params& src,
                               const std::string& sname,
                               int64_t& copied_params) {
    auto s = src.w.find(sname);
    auto d = dst.w.find(dname);
    if (s == src.w.end() || d == dst.w.end()) return false;
    if (s->second.shape != d->second.shape) return false;
    d->second.d = s->second.d;
    copied_params += d->second.numel();
    return true;
}

int mode_distill_init(const Args& a) {
    std::string teacher_path = a.get("teacher");
    std::string cfg_path = a.get("config");
    std::string out = a.get("out");
    if (teacher_path.empty() || cfg_path.empty() || out.empty())
        fail("DISTILL_ARGS_MISSING");
    uint64_t seed = a.has("seed") ? (uint64_t)std::stoull(a.get("seed")) : 42;

    // -- teacher weights (bundle dir or XCN checkpoint).
    xct::ModelConfig tc;
    xct::Params tp;
    if (fs::is_directory(teacher_path)) {
        load_bundle_params(teacher_path, tc, tp);
    } else {
        if (!xct::ckpt_peek_config(teacher_path, tc))
            fail("DISTILL_TEACHER_UNREADABLE");
        xct::init_params(tp, tc, 0);
        if (!xct::ckpt_load(tp, tc, teacher_path))
            fail("DISTILL_TEACHER_LOAD_FAILED");
    }

    // -- student architecture from a model-config JSON (same field names
    //    as job.json's model block / bundle manifest config).
    JsonValue cfgroot = parse_json_file(cfg_path);
    const JsonValue* scfg = cfgroot.get("model");
    if (!scfg) scfg = cfgroot.get("config");
    if (!scfg) scfg = &cfgroot;
    xct::ModelConfig sc = config_from_manifest(*scfg);
    if (sc.vocab != tc.vocab || sc.hidden != tc.hidden ||
        sc.heads != tc.heads || sc.kv_heads != tc.kv_heads)
        fail("DISTILL_DIM_MISMATCH: student vocab/hidden/heads must equal "
             "teacher for weight transplant");
    xct::Params sp;
    xct::init_params(sp, sc, seed);

    int64_t copied_params = 0, copied_tensors = 0;
    auto copy = [&](const std::string& d, const std::string& s) {
        if (copy_if_same_shape(sp, d, tp, s, copied_params))
            ++copied_tensors;
    };
    copy("embed", "embed");
    copy("lm_head", "lm_head");
    copy("norm_f", "norm_f");
    copy("embed_ple", "embed_ple");
    copy("ple_model_proj", "ple_model_proj");
    copy("ple_proj_norm", "ple_proj_norm");

    // Teacher dense-MLP pool (layers carrying layers.N.w1).
    std::vector<int> dense_layers;
    for (int l = 0; l < tc.layers; ++l)
        if (tp.w.count(xct::ln(l, "w1"))) dense_layers.push_back(l);
    if (dense_layers.empty())
        fail("DISTILL_NO_DENSE_MLP: teacher has no dense MLP layer");
    auto nearest_dense = [&](int tl) {
        int best = dense_layers[0];
        for (int d : dense_layers)
            if (std::abs(d - tl) < std::abs(best - tl)) best = d;
        return best;
    };
    int dense_cursor = 0;
    std::ostringstream layer_map;
    layer_map << '[';
    for (int l = 0; l < sc.layers; ++l) {
        // Positional transplant: student layer l inherits teacher layer l
        // when in range (no representational drift); extra student depth
        // reuses whole teacher dense layers (attn+MLP stay coherent).
        int tl = l < tc.layers ? l
                               : dense_layers[(size_t)dense_cursor++ %
                                              dense_layers.size()];
        int ml = tp.w.count(xct::ln(tl, "w1")) ? tl : nearest_dense(tl);
        for (const char* t :
             {"norm1", "wq", "wk", "wv", "wo", "norm2", "q_norm",
              "k_norm", "norm_attn", "norm_ffn", "ple_gate", "ple_proj",
              "ple_post"})
            copy(xct::ln(l, t), xct::ln(tl, t));
        for (const char* t : {"w1", "w2", "w3"})
            copy(xct::ln(l, t), xct::ln(ml, t));
        if (l) layer_map << ',';
        layer_map << "{\"student\":" << l << ",\"teacher_attn\":" << tl
                  << ",\"teacher_mlp\":" << ml << '}';
    }
    layer_map << ']';

    int64_t total = 0;
    for (const auto& n : sp.order) total += sp.w[n].numel();
    if (!xct::ckpt_save(sp, sc, out, a.has("overwrite")))
        fail("DISTILL_CKPT_WRITE_FAILED:" + out);
    std::printf(
        "{\"ok\":true,\"mode\":\"distill-init\",\"out\":\"%s\","
        "\"ckpt_sha256\":\"%s\",\"student_params\":%lld,"
        "\"teacher_params\":%lld,\"copied_tensors\":%lld,"
        "\"copied_params\":%lld,\"fresh_params\":%lld,"
        "\"layer_map\":%s}\n",
        gptbridge::jsonlite::json_escape(out).c_str(),
        sha256_file(out).c_str(), (long long)total,
        (long long)std::accumulate(
            tp.order.begin(), tp.order.end(), (int64_t)0,
            [&](int64_t s, const std::string& n) {
                return s + tp.w[n].numel();
            }),
        (long long)copied_tensors, (long long)copied_params,
        (long long)(total - copied_params), layer_map.str().c_str());
    return 0;
}

// ----------------------------------------------- vision-smoke (v1) ----

// vision-smoke --bundle <vision-bundle-dir> [--patches N] [--seed N]
// End-to-end proof that the native vision early-fusion path is live:
// text-only vs vision-prefixed last-logits on the same engine, determinism
// across runs, and fail-closed shape/count gates. Single JSON on stdout.
int mode_vision_smoke(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("VISION_SMOKE_ARGS_MISSING");
    int64_t want_patches = 3;
    if (a.has("patches")) {
        try { want_patches = std::stoll(a.get("patches")); }
        catch (...) { fail("VISION_SMOKE_BAD_PATCHES"); }
    }
    uint64_t seed = 11;
    if (a.has("seed")) {
        try { seed = (uint64_t)std::stoull(a.get("seed")); }
        catch (...) { fail("VISION_SMOKE_BAD_SEED"); }
    }
    NativeInferenceEngine engine;
    try {
        engine.load(bundle);
    } catch (const std::exception& e) {
        fail(std::string("VISION_SMOKE_LOAD_FAILED:") + e.what());
    }
    JsonValue manifest = parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    if (!mcfg) fail("VISION_SMOKE_MANIFEST_INVALID");
    bool use_vision = false;
    if (const JsonValue* uv = mcfg->get("use_vision"))
        if (uv->type == JsonValue::Type::Bool) use_vision = uv->boolean;
    if (!use_vision) fail("VISION_SMOKE_NOT_VISION_BUNDLE");
    int64_t vocab = (int64_t)xct::j_num(mcfg, "vocab_size", 0);
    int64_t pdim = (int64_t)xct::j_num(mcfg, "vision_patch_dim", 0);
    int64_t pmax = (int64_t)xct::j_num(mcfg, "vision_max_patches", 0);
    if (vocab < 16 || pdim <= 0 || pmax <= 0) fail("VISION_SMOKE_BAD_CONFIG");
    int64_t P = std::min<int64_t>(want_patches <= 0 ? 1 : want_patches, pmax);
    std::vector<int64_t> ids;
    for (int64_t i = 0; i < 8; ++i) ids.push_back(3 + (i * 7) % (vocab - 4));
    std::vector<double> patches((size_t)P * (size_t)pdim);
    {
        std::mt19937_64 rng(seed);
        std::uniform_real_distribution<double> ud(-0.5, 0.5);
        for (auto& x : patches) x = ud(rng);
    }
    auto finite = [](const std::vector<double>& v) {
        for (double x : v) if (!std::isfinite(x)) return false;
        return !v.empty();
    };
    std::vector<double> base, v1, v2;
    try {
        base = engine.logits(ids);
        v1 = engine.forward_vision_logits(ids, patches, P);
        v2 = engine.forward_vision_logits(ids, patches, P);
    } catch (const std::exception& e) {
        fail(std::string("VISION_SMOKE_FORWARD_FAILED:") + e.what());
    }
    bool text_finite = finite(base);
    bool vision_finite = finite(v1) && finite(v2);
    bool deterministic = v1.size() == v2.size() &&
        std::equal(v1.begin(), v1.end(), v2.begin());
    double fusion_diff = 0.0;
    if (base.size() == v1.size())
        for (size_t i = 0; i < base.size(); ++i)
            fusion_diff = std::max(fusion_diff, std::fabs(v1[i] - base[i]));
    bool fusion_live = fusion_diff > 1e-9;
    // Fail-closed gates: ragged patch data and over-count must throw.
    bool fc_shape = false, fc_count = false;
    {
        std::vector<double> bad((size_t)P * (size_t)pdim + 1, 0.0);
        try {
            engine.forward_vision_logits(ids, bad, P);
        } catch (const std::exception& e) {
            fc_shape = std::string(e.what()).find("VISION_SHAPE_MISMATCH") !=
                       std::string::npos;
        }
    }
    {
        std::vector<double> many((size_t)(pmax + 1) * (size_t)pdim, 0.0);
        try {
            engine.forward_vision_logits(ids, many, pmax + 1);
        } catch (const std::exception& e) {
            fc_count = std::string(e.what()).find("VISION_TOO_MANY_PATCHES") !=
                       std::string::npos;
        }
    }
    bool ok = text_finite && vision_finite && deterministic && fusion_live &&
              fc_shape && fc_count;
    std::printf(
        "{\"ok\":%s,\"mode\":\"vision-smoke\",\"bundle\":\"%s\","
        "\"patches\":%lld,\"patch_dim\":%lld,\"vocab\":%lld,"
        "\"text_finite\":%s,\"vision_finite\":%s,\"deterministic\":%s,"
        "\"fusion_max_abs_diff\":%.6g,\"fusion_live\":%s,"
        "\"fail_closed_shape\":%s,\"fail_closed_count\":%s}\n",
        ok ? "true" : "false",
        gptbridge::jsonlite::json_escape(bundle).c_str(), (long long)P,
        (long long)pdim, (long long)vocab, text_finite ? "true" : "false",
        vision_finite ? "true" : "false",
        deterministic ? "true" : "false", fusion_diff,
        fusion_live ? "true" : "false", fc_shape ? "true" : "false",
        fc_count ? "true" : "false");
    return ok ? 0 : 1;
}

// --------------------------------------------------------- cache-smoke ----
//
// Long-context memory & cache probe for a native bundle (public engine
// API only ??no internal surface):
//   paged KV   ??generate() drives the append-cache decode path; a
//                repeated generate over the same prompt restores the
//                cached prefix (prefix_cache_hits++) and must reproduce
//                identical tokens ??the "restored KV is bit-identical to
//                recompute" contract made executable;
//   determinism ??uncached logits() calls are bitwise stable;
//   kv-int8 (opt-in via --kv-int8) ??a second engine loaded under
//                XINGCHENG_CPP_KV_INT8 keeps the same prefix-cache
//                behavior and deterministic generation on a ~8x smaller
//                KV footprint; logit drift vs fp64 is reported and must
//                stay bounded.
namespace cache_smoke_detail {

int64_t prefix_hits(NativeInferenceEngine& e) {
    const std::string d = e.describe();
    const std::string key = "\"prefix_cache_hits\":";
    size_t p = d.find(key);
    if (p == std::string::npos) return -1;
    return std::atoll(d.c_str() + p + key.size());
}

bool all_finite(const std::vector<double>& v) {
    for (double x : v) if (!std::isfinite(x)) return false;
    return !v.empty();
}

bool vec_eq(const std::vector<double>& x, const std::vector<double>& y) {
    return x.size() == y.size() &&
           std::memcmp(x.data(), y.data(), x.size() * sizeof(double)) == 0;
}

struct Run {
    bool logits_finite = false;
    bool logits_deterministic = false;
    bool prefix_hit = false;
    bool partial_prefix_hit = false;
    bool gen_nonempty = false;
    bool gen_identical = false;
    std::vector<double> ref_logits;
};

Run probe_run(const std::string& bundle,
              const std::vector<int64_t>& ids) {
    NativeInferenceEngine e;
    e.load(bundle);
    Run r;
    r.ref_logits = e.logits(ids);
    r.logits_finite = all_finite(r.ref_logits);
    r.logits_deterministic = vec_eq(e.logits(ids), r.ref_logits);
    SamplingConfig sc;                    // do_sample=false ??argmax
    std::vector<int64_t> prompt(ids.begin(), ids.begin() + 16);
    std::vector<int64_t> g1 =
        e.generate(prompt, 6, sc);        // miss ??stores prefix entry
    const int64_t h1 = prefix_hits(e);
    std::vector<int64_t> g2 = e.generate(prompt, 6, sc);
    const int64_t h2 = prefix_hits(e);
    r.prefix_hit = (h2 > h1);
    r.gen_nonempty = !g1.empty();
    // Restored-KV decode must reproduce the recomputed output exactly.
    r.gen_identical = !g1.empty() && g1 == g2;
    // Same prefix, longer prompt: a partial-prefix hit exercises the
    // longest-match restore path as well.
    std::vector<int64_t> prompt2(ids.begin(), ids.begin() + 24);
    std::vector<int64_t> g3 = e.generate(prompt2, 4, sc);
    const int64_t h3 = prefix_hits(e);
    r.partial_prefix_hit = (h3 > h2) && !g3.empty();
    return r;
}

}  // namespace cache_smoke_detail

int mode_cache_smoke(const Args& a) {
    using namespace cache_smoke_detail;
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("CACHE_SMOKE_ARGS_MISSING");
    uint64_t seed = 11;
    if (a.has("seed")) {
        try { seed = (uint64_t)std::stoull(a.get("seed")); }
        catch (...) { fail("CACHE_SMOKE_BAD_SEED"); }
    }
    bool want_int8 = a.has("kv-int8");
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    if (!mcfg) fail("CACHE_SMOKE_MANIFEST_INVALID");
    int64_t vocab = (int64_t)xct::j_num(mcfg, "vocab_size", 0);
    if (vocab < 32) fail("CACHE_SMOKE_BAD_CONFIG");
    // v27+ fused hybrid: the engine's hybrid prefix path restores
    // attention KV *and* DeltaNet recurrent state (see
    // hybrid-prefix-smoke / delta-prefix-restore), so prefix hits are
    // now REQUIRED on hybrid bundles too ??greedy output must stay
    // bit-identical either way.
    const bool hybrid =
        xct::j_num(mcfg, "full_attention_interval", 0) > 0 &&
        xct::j_num(mcfg, "linear_num_key_heads", 0) > 0;
    std::vector<int64_t> ids;
    {
        std::mt19937_64 rng(seed);
        std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
        for (int i = 0; i < 32; ++i) ids.push_back(tok(rng));
    }
    Run fp;
    try {
        fp = probe_run(bundle, ids);
    } catch (const std::exception& e) {
        fail(std::string("CACHE_SMOKE_FORWARD_FAILED:") + e.what());
    }
    const bool prefix_ok = fp.prefix_hit && fp.partial_prefix_hit;
    bool ok = fp.logits_finite && fp.logits_deterministic && prefix_ok &&
              fp.gen_nonempty && fp.gen_identical;
    // kv-int8: logits() never touches the KV pool, so the meaningful
    // evidence is the cached path ??prefix hits still fire, the
    // restore??搪quantize round-trip keeps greedy output identical, and
    // generation stays non-degenerate. fp64-vs-int8 token equality is
    // reported but not gated (quantization may legitimately nudge
    // argmax).
    bool int8_ok = true, int8_finite = false, int8_hit = false,
         int8_partial = false, int8_gen_id = false, int8_vs_fp64 = false,
         int8_nonempty = false;
    if (want_int8) {
#ifdef _WIN32
        _putenv_s("XINGCHENG_CPP_KV_INT8", "1");
#else
        setenv("XINGCHENG_CPP_KV_INT8", "1", 1);
#endif
        try {
            Run q8 = probe_run(bundle, ids);
            int8_finite = q8.logits_finite;
            int8_hit = q8.prefix_hit;
            int8_partial = q8.partial_prefix_hit;
            int8_gen_id = q8.gen_identical;
            int8_nonempty = q8.gen_nonempty;
            int8_vs_fp64 = vec_eq(q8.ref_logits, fp.ref_logits);
            int8_ok = int8_finite && int8_gen_id &&
                      int8_hit && int8_partial;
        } catch (...) {
            int8_ok = false;
        }
        ok = ok && int8_ok;
    }
    std::printf(
        "{\"ok\":%s,\"mode\":\"cache-smoke\",\"bundle\":\"%s\","
        "\"vocab\":%lld,\"hybrid\":%s,\"logits_finite\":%s,"
        "\"logits_deterministic\":%s,"
        "\"prefix_hit\":%s,\"partial_prefix_hit\":%s,"
        "\"gen_identical\":%s,"
        "\"kv_int8\":{\"requested\":%s,\"ok\":%s,\"finite\":%s,"
        "\"prefix_hit\":%s,\"partial_prefix_hit\":%s,"
        "\"gen_identical\":%s,\"gen_nonempty\":%s,"
        "\"vs_fp64_logits_identical\":%s}}\n",
        ok ? "true" : "false",
        gptbridge::jsonlite::json_escape(bundle).c_str(),
        (long long)vocab,
        hybrid ? "true" : "false",
        fp.logits_finite ? "true" : "false",
        fp.logits_deterministic ? "true" : "false",
        fp.prefix_hit ? "true" : "false",
        fp.partial_prefix_hit ? "true" : "false",
        fp.gen_identical ? "true" : "false",
        want_int8 ? "true" : "false", int8_ok ? "true" : "false",
        int8_finite ? "true" : "false", int8_hit ? "true" : "false",
        int8_partial ? "true" : "false", int8_gen_id ? "true" : "false",
        int8_nonempty ? "true" : "false",
        int8_vs_fp64 ? "true" : "false");
    return ok ? 0 : 1;
}

// ------------------------------------------------------- export-bundle ----

// ------------------------------------------------------------- parity ----
// v27 fused-hybrid gate: numeric parity between the trainer checkpoint
// (fp32 xct::fwd) and the exported bundle run through the C++ engine
// (fp64) on identical token ids. Compares summed next-token NLL,
// last-position logits and greedy argmax ??this is the end-to-end check
// that export + engine execution reproduce trained weights exactly,
// including DeltaNet recurrence, causal-conv tails, q/k norm, gated
// attention out and partial RoPE.
int mode_parity(const Args& a) {
    std::string ckpt = a.get("ckpt"), bundle = a.get("bundle");
    if (ckpt.empty() || bundle.empty()) fail("PARITY_ARGS_MISSING");
    double tol_nll = 0.02, tol_logit = 0.10;
    if (a.has("tol")) {
        try { tol_nll = std::stod(a.get("tol")); }
        catch (...) { fail("PARITY_BAD_TOL"); }
    }
    if (a.has("tol-logit")) {
        try { tol_logit = std::stod(a.get("tol-logit")); }
        catch (...) { fail("PARITY_BAD_TOL"); }
    }

    xct::ModelConfig c;
    if (!xct::ckpt_peek_config(ckpt, c)) fail("PARITY_CKPT_UNREADABLE");

    // ids: explicit CSV via --ids, else seeded probe ids like cache-smoke.
    std::vector<int64_t> ids;
    if (a.has("ids")) {
        const std::string s = a.get("ids");
        size_t pos = 0;
        while (pos <= s.size()) {
            size_t comma = s.find(',', pos);
            std::string tok =
                s.substr(pos, comma == std::string::npos
                                 ? std::string::npos : comma - pos);
            if (tok.empty()) fail("PARITY_BAD_IDS");
            try { ids.push_back(std::stoll(tok)); }
            catch (...) { fail("PARITY_BAD_IDS"); }
            if (comma == std::string::npos) break;
            pos = comma + 1;
        }
    } else {
        int64_t len = a.has("len")
                          ? std::stoll(a.get("len")) : 32;
        uint64_t seed = a.has("seed")
                            ? (uint64_t)std::stoull(a.get("seed")) : 11;
        std::mt19937_64 rng(seed);
        std::uniform_int_distribution<int64_t> tok(3, c.vocab - 1);
        for (int64_t i = 0; i < len; ++i) ids.push_back(tok(rng));
    }
    if (ids.size() < 4) fail("PARITY_IDS_TOO_SHORT");
    for (int64_t id : ids)
        if (id < 0 || id >= c.vocab) fail("PARITY_ID_RANGE");

    xct::Params p;
    xct::init_params(p, c, 0);
    if (!xct::ckpt_load(p, c, ckpt)) fail("PARITY_CKPT_LOAD_FAILED");
    std::vector<int> tids;
    tids.reserve(ids.size());
    for (int64_t id : ids) tids.push_back((int)id);
    xct::Fwd o;
    try {
        xct::fwd(p, c, tids, o);
    } catch (const char* e) {
        fail(std::string("PARITY_TRAINER_FWD:") + e);
    } catch (const std::exception& e) {
        fail(std::string("PARITY_TRAINER_FWD:") + e.what());
    } catch (...) {
        fail("PARITY_TRAINER_FWD");
    }

    const int T = (int)tids.size(), V = c.vocab;
    if ((int64_t)o.logits.size() != (int64_t)T * V)
        fail("PARITY_TRAINER_SHAPE");
    double nll_t = 0.0;
    int64_t cnt = 0;
    for (int i = 0; i + 1 < T; ++i) {
        const float* row = o.logits.data() + (size_t)i * V;
        double mx = (double)row[0];
        for (int v = 1; v < V; ++v)
            if ((double)row[v] > mx) mx = (double)row[v];
        double se = 0.0;
        for (int v = 0; v < V; ++v)
            se += std::exp((double)row[v] - mx);
        const int tgt = tids[(size_t)i + 1];
        nll_t += mx + std::log(se) - (double)row[tgt];
        ++cnt;
    }
    const float* last_t = o.logits.data() + (size_t)(T - 1) * V;
    int argmax_t = 0;
    for (int v = 1; v < V; ++v)
        if (last_t[v] > last_t[argmax_t]) argmax_t = v;

    NativeInferenceEngine e;
    try {
        e.load(bundle);
    } catch (const std::exception& ex) {
        fail(std::string("PARITY_ENGINE_LOAD:") + ex.what());
    }
    std::pair<double, int64_t> nll_e;
    std::vector<double> lg;
    try {
        nll_e = e.sequence_nll(ids);
        lg = e.logits(ids);
    } catch (const std::exception& ex) {
        fail(std::string("PARITY_ENGINE_FWD:") + ex.what());
    }
    if ((int64_t)lg.size() != V) fail("PARITY_ENGINE_SHAPE");
    int argmax_e = 0;
    double max_logit_d = 0.0;
    for (int v = 0; v < V; ++v) {
        if (lg[(size_t)v] > lg[(size_t)argmax_e]) argmax_e = v;
        double d = std::fabs(lg[(size_t)v] - (double)last_t[v]);
        if (d > max_logit_d) max_logit_d = d;
    }
    const double mean_t = nll_t / (double)cnt;
    const double mean_e =
        nll_e.second > 0 ? nll_e.first / (double)nll_e.second : -1.0;
    const double mean_d = std::fabs(mean_t - mean_e);
    int linear_layers = 0;
    for (int l = 0; l < c.layers; ++l)
        if (c.is_linear(l)) ++linear_layers;
    const bool ok = nll_e.second == cnt && argmax_t == argmax_e &&
                    mean_d <= tol_nll && max_logit_d <= tol_logit &&
                    std::isfinite(mean_t) && std::isfinite(mean_e);
    std::printf(
        "{\"ok\":%s,\"mode\":\"parity\",\"ckpt\":\"%s\",\"bundle\":\"%s\","
        "\"tokens\":%lld,\"scored\":%lld,\"linear_layers\":%d,"
        "\"nll_trainer\":%.6f,\"nll_engine\":%.6f,"
        "\"mean_nll_trainer\":%.6f,\"mean_nll_engine\":%.6f,"
        "\"mean_nll_diff\":%.6f,\"logit_max_abs_diff\":%.6f,"
        "\"argmax_trainer\":%d,\"argmax_engine\":%d,"
        "\"argmax_match\":%s}\n",
        ok ? "true" : "false",
        gptbridge::jsonlite::json_escape(ckpt).c_str(),
        gptbridge::jsonlite::json_escape(bundle).c_str(),
        (long long)ids.size(), (long long)nll_e.second, linear_layers,
        nll_t, nll_e.first, mean_t, mean_e, mean_d, max_logit_d,
        argmax_t, argmax_e, argmax_t == argmax_e ? "true" : "false");
    return ok ? 0 : 1;
}

// 蝳?5 precision-candidate validation: the same weights exported at two
// precisions (REFERENCE_FP64 vs PRODUCTION_BF16) are compared through
// the real engine ??last-position logit drift, argmax agreement,
// sequence NLL, greedy continuation identity, load time and resident
// memory. Reports evidence; promotion stays a lifecycle decision.
int mode_precision(const Args& a) {
    std::string ref_path = a.get("ref"), cand_path = a.get("candidate");
    if (ref_path.empty() || cand_path.empty()) fail("PRECISION_ARGS_MISSING");
    double tol = 0.5;
    if (a.has("tol")) {
        try { tol = std::stod(a.get("tol")); }
        catch (...) { fail("PRECISION_BAD_TOL"); }
    }
    uint64_t seed = 11;
    int64_t len = 32;
    if (a.has("seed")) {
        try { seed = (uint64_t)std::stoull(a.get("seed")); }
        catch (...) { fail("PRECISION_BAD_SEED"); }
    }
    if (a.has("len")) {
        try { len = std::stoll(a.get("len")); }
        catch (...) { fail("PRECISION_BAD_LEN"); }
    }
    if (len < 4) fail("PRECISION_LEN_TOO_SHORT");

    NativeInferenceEngine ref, cand;
    auto now_ms = [] {
        return std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
    };
    const int64_t t_load0 = now_ms();
    try { ref.load(ref_path); }
    catch (const std::exception& ex) {
        fail(std::string("PRECISION_REF_LOAD:") + ex.what());
    }
    const int64_t t_load1 = now_ms();
    try { cand.load(cand_path); }
    catch (const std::exception& ex) {
        fail(std::string("PRECISION_CAND_LOAD:") + ex.what());
    }
    const int64_t t_load2 = now_ms();

    std::vector<int64_t> ids;
    {
        // Seeded probe ids; bound by the reference manifest vocab.
        JsonValue mf = parse_json_file(
            (fs::path(ref_path) / "manifest.json").string());
        const JsonValue* cfg = mf.get("config");
        const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
        if (vocab < 8) fail("PRECISION_BAD_CONFIG");
        std::mt19937_64 rng(seed);
        std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
        for (int64_t i = 0; i < len; ++i) ids.push_back(tok(rng));
    }

    std::vector<double> lg_ref, lg_cand;
    std::pair<double, int64_t> nll_ref, nll_cand;
    try {
        lg_ref = ref.logits(ids);
        nll_ref = ref.sequence_nll(ids);
        lg_cand = cand.logits(ids);
        nll_cand = cand.sequence_nll(ids);
    } catch (const std::exception& ex) {
        fail(std::string("PRECISION_FWD:") + ex.what());
    }
    if (lg_ref.size() != lg_cand.size()) fail("PRECISION_SHAPE");
    bool finite = cache_smoke_detail::all_finite(lg_ref) &&
                  cache_smoke_detail::all_finite(lg_cand);
    double max_d = 0.0;
    int argmax_r = 0, argmax_c = 0;
    for (size_t v = 0; v < lg_ref.size(); ++v) {
        max_d = std::max(max_d,
                         std::fabs(lg_ref[v] - lg_cand[v]));
        if (lg_ref[v] > lg_ref[(size_t)argmax_r]) argmax_r = (int)v;
        if (lg_cand[v] > lg_cand[(size_t)argmax_c]) argmax_c = (int)v;
    }
    // Greedy continuation identity + decode throughput on the candidate.
    SamplingConfig sc;
    std::vector<int64_t> prompt(ids.begin(), ids.begin() + len / 2);
    std::vector<int64_t> g_ref, g_cand;
    const int64_t t_gen0 = now_ms();
    try {
        g_ref = ref.generate(prompt, 8, sc);
        g_cand = cand.generate(prompt, 8, sc);
    } catch (const std::exception& ex) {
        fail(std::string("PRECISION_GEN:") + ex.what());
    }
    const int64_t t_gen1 = now_ms();
    const bool gen_identical = g_ref == g_cand && !g_cand.empty();
    const double gen_ms = (double)std::max<int64_t>(1, t_gen1 - t_gen0);
    const double tps = 16.0 * 1000.0 / gen_ms;   // both runs, 8 each
    const bool ok = finite && argmax_r == argmax_c &&
                    gen_identical && max_d <= tol;
    std::printf(
        "{\"ok\":%s,\"mode\":\"precision\",\"ref\":\"%s\","
        "\"candidate\":\"%s\",\"tokens\":%lld,"
        "\"finite\":%s,\"logit_max_abs_diff\":%.6f,"
        "\"argmax_match\":%s,\"nll_ref\":%.6f,\"nll_cand\":%.6f,"
        "\"nll_diff\":%.6f,\"gen_identical\":%s,"
        "\"load_ms_ref\":%lld,\"load_ms_cand\":%lld,"
        "\"memory_bytes_ref\":%lld,\"memory_bytes_cand\":%lld,"
        "\"tps_approx\":%.2f,\"tol\":%.6f%s}\n",
        ok ? "true" : "false",
        gptbridge::jsonlite::json_escape(ref_path).c_str(),
        gptbridge::jsonlite::json_escape(cand_path).c_str(),
        (long long)ids.size(),
        finite ? "true" : "false", max_d,
        argmax_r == argmax_c ? "true" : "false",
        nll_ref.first, nll_cand.first,
        std::fabs(nll_ref.first - nll_cand.first),
        gen_identical ? "true" : "false",
        (long long)(t_load1 - t_load0), (long long)(t_load2 - t_load1),
        (long long)ref.memory_bytes(), (long long)cand.memory_bytes(),
        tps, tol,
        ok ? "" : ",\"error_code\":\"PRECISION_PARITY_FAILED\"");
    return ok ? 0 : 1;
}

int mode_export_bundle(const Args& a) {
    std::string ckpt = a.get("ckpt");
    fs::path out_dir = a.get("out");
    std::string cfg_from = a.get("config-from");
    std::string tokenizer = a.get("tokenizer");
    if (ckpt.empty() || out_dir.empty() || cfg_from.empty() || tokenizer.empty())
        fail("EXPORT_ARGS_MISSING");

    // Pass 1: read XCN header -> config; then allocate + load.
    xct::ModelConfig c;
    if (!xct::ckpt_peek_config(ckpt, c))
        fail("EXPORT_CKPT_UNREADABLE:" + ckpt);
    xct::Params p;
    xct::init_params(p, c, 0);
    if (!xct::ckpt_load(p, c, ckpt)) fail("EXPORT_CKPT_LOAD_FAILED");

    JsonValue src_manifest = parse_json_file(cfg_from);
    const JsonValue* cfg = src_manifest.get("config");
    if (!cfg) fail("EXPORT_CONFIG_FROM_MISSING_CONFIG");
    // Fail-closed dimensional parity between the exported ckpt and the
    // manifest config we are about to ship.
    if ((int)xct::j_num(cfg, "vocab_size", -1) != c.vocab ||
        (int)xct::j_num(cfg, "hidden_size", -1) != c.hidden ||
        (int)xct::j_num(cfg, "num_hidden_layers", -1) != c.layers)
        fail("EXPORT_CONFIG_MISMATCH");
    {
        bool cfg_vision = false;
        const JsonValue* uv = cfg->get("use_vision");
        if (uv && uv->type == JsonValue::Type::Bool) cfg_vision = uv->boolean;
        if (cfg_vision != c.use_vision ||
            (c.use_vision &&
             ((int)xct::j_num(cfg, "vision_patch_dim", -1) != c.vision_patch_dim ||
              (int)xct::j_num(cfg, "vision_max_patches", -1) != c.vision_max_patches)))
            fail("EXPORT_VISION_MISMATCH");
    }
    // v27 fused-hybrid parity (XCN3 fields): a non-default checkpoint
    // field must be present verbatim in the shipped manifest; a declared
    // field must equal the checkpoint's value. Otherwise the engine
    // would run the bundle under silently-wrong layer semantics.
    {
        struct NumParity { const char* key; int64_t ckpt; bool required; };
        const bool lin_required = c.full_attention_interval > 0;
        for (const NumParity& np : {
                 NumParity{"full_attention_interval",
                           (int64_t)c.full_attention_interval, lin_required},
                 {"linear_num_key_heads", (int64_t)c.lin_key_heads, lin_required},
                 {"linear_key_head_dim", (int64_t)c.lin_key_dim, lin_required},
                 {"linear_num_value_heads", (int64_t)c.lin_value_heads,
                  lin_required},
                 {"linear_value_head_dim", (int64_t)c.lin_value_dim,
                  lin_required},
                 {"linear_conv_kernel_dim", (int64_t)c.lin_conv_kernel,
                  lin_required}}) {
            const JsonValue* v = cfg->get(np.key);
            if (v != nullptr && v->type != JsonValue::Type::Number)
                fail("EXPORT_HYBRID_MISMATCH");
            if (np.required && v == nullptr)
                fail("EXPORT_HYBRID_MISMATCH");
            if (v != nullptr && (int64_t)v->number != np.ckpt)
                fail("EXPORT_HYBRID_MISMATCH");
        }
        const JsonValue* pr = cfg->get("partial_rotary_factor");
        if (pr != nullptr && pr->type != JsonValue::Type::Number)
            fail("EXPORT_HYBRID_MISMATCH");
        if (c.partial_rotary != 1.0f && pr == nullptr)
            fail("EXPORT_HYBRID_MISMATCH");
        if (pr != nullptr &&
            std::fabs(pr->number - (double)c.partial_rotary) > 1e-6)
            fail("EXPORT_HYBRID_MISMATCH");
        // MTP declaration parity (XCN10): a trained head must be
        // declared verbatim in the shipped config, and a config that
        // declares a head the checkpoint does not carry mismatches the
        // bundle contract ??both fail closed with the canonical code.
        for (const NumParity& np : {
                 NumParity{"num_nextn_predict_layers",
                           (int64_t)c.mtp_num_layers,
                           c.mtp_num_layers > 0},
                 {"mtp_stack_depth", (int64_t)c.mtp_depth,
                  c.mtp_depth > 0}}) {
            const JsonValue* v = cfg->get(np.key);
            if (v != nullptr && v->type != JsonValue::Type::Number)
                fail("MTP_BUNDLE_MISMATCH:" + std::string(np.key));
            if (np.required && v == nullptr)
                fail("MTP_BUNDLE_MISMATCH:config lacks " +
                     std::string(np.key));
            if (v != nullptr && (int64_t)v->number != np.ckpt)
                fail("MTP_BUNDLE_MISMATCH:" + std::string(np.key));
        }
        struct BoolParity { const char* key; bool ckpt; };
        for (const BoolParity& bp : {
                 BoolParity{"attn_output_gate", c.attn_output_gate},
                 {"qk_norm", c.qk_norm},
                 {"shared_expert_gate", c.shared_expert_gate},
                 {"moe_router_sigmoid", c.moe_router_sigmoid}}) {
            const JsonValue* v = cfg->get(bp.key);
            if (v != nullptr && v->type != JsonValue::Type::Bool)
                fail("EXPORT_HYBRID_MISMATCH");
            if (bp.ckpt && (v == nullptr || !v->boolean))
                fail("EXPORT_HYBRID_MISMATCH");
            if (v != nullptr && v->boolean != bp.ckpt)
                fail("EXPORT_HYBRID_MISMATCH");
        }
    }

    fs::create_directories(out_dir);
    // Deterministic tensor order: sorted bundle names.
    std::vector<std::pair<std::string, std::string>> pairs;
    pairs.reserve(p.order.size());
    int64_t mtp_exported = 0;
    for (const auto& n : p.order) {
        // MTP export: mtp.* tensors are part of the XCN10 contract and
        // ride the bundle under model.mtp.* ??the trained draft head is
        // preserved for the runtime drafter.
        // aux-free lb_bias is a routing-time buffer updated by the sign
        // rule (never by the optimizer); the serving engine has no
        // lb_bias consumer, so it stays out of bundles.
        if (n.size() >= 7 &&
            n.compare(n.size() - 7, 7, "lb_bias") == 0) continue;
        std::string b = xct_to_bundle(n, c.is_gemma4());
        if (b.empty()) fail("EXPORT_UNMAPPED_TENSOR:" + n);
        if (n.compare(0, 4, "mtp.") == 0) ++mtp_exported;
        pairs.emplace_back(b, n);
    }
    // Fail-closed: a checkpoint whose config declares an MTP head but
    // carries no mtp.* tensors cannot produce a canonical bundle ??the
    // draft head would be silently lost.
    if ((c.mtp_num_layers > 0 || c.mtp_depth > 0) && mtp_exported == 0)
        fail("MTP_HEAD_MISSING:config declares mtp but no mtp.* tensors");
    std::sort(pairs.begin(), pairs.end());

    // --quant none|int8|int4_packed|bf16: 2-D matrices are stored
    // compressed (per-tensor symmetric scale for int8/int4; bf16 keeps
    // fp32's exponent with a truncated mantissa ??PRODUCTION_BF16
    // candidate). The engine widens to fp64 at load; 1-D tensors
    // (norms) stay fp64.
    std::string quant = a.get("quant");
    if (quant.empty()) quant = "none";
    if (quant != "none" && quant != "int8" && quant != "int4_packed" &&
        quant != "bf16")
        fail("EXPORT_QUANT_UNSUPPORTED:" + quant);

    std::string bin_path = (out_dir / "weights.bin").string();
    std::string bin_tmp = bin_path + ".tmp";
    std::ofstream bin(bin_tmp, std::ios::binary | std::ios::trunc);
    if (!bin) fail("EXPORT_WEIGHTS_UNWRITABLE");
    std::ostringstream tensors_json;
    tensors_json << '{';
    int64_t offset = 0;
    int64_t param_count = 0, quantized_tensors = 0;
    for (size_t i = 0; i < pairs.size(); ++i) {
        const xct::Tensor& t = p.w.at(pairs[i].second);
        int64_t n = t.numel();
        param_count += n;
        bool q = quant != "none" && t.shape.size() == 2;
        int64_t bytes;
        std::string dtype;
        double scale = 0.0;
        if (q && quant == "bf16") {
            // bf16: top 16 bits of fp32, round-to-nearest-even on the
            // dropped mantissa (x + 0x7FFF + lsb). decode is bits<<16.
            std::vector<uint16_t> qd((size_t)n);
            for (int64_t k = 0; k < n; ++k) {
                uint32_t bits;
                const float fv = t.d[(size_t)k];
                std::memcpy(&bits, &fv, 4);
                bits += 0x7FFFu + ((bits >> 16) & 1u);
                qd[(size_t)k] = (uint16_t)(bits >> 16);
            }
            bin.write((char*)qd.data(), (std::streamsize)(n * 2));
            bytes = n * 2;
            dtype = "bf16";
        } else if (q && quant == "int8") {
            double mx = 0.0;
            for (int64_t k = 0; k < n; ++k)
                mx = std::max(mx, (double)std::fabs(t.d[(size_t)k]));
            scale = mx > 0.0 ? mx / 127.0 : 1.0;
            std::vector<int8_t> qd((size_t)n);
            for (int64_t k = 0; k < n; ++k) {
                double v = std::lround((double)t.d[(size_t)k] / scale);
                qd[(size_t)k] = (int8_t)std::clamp(v, -127.0, 127.0);
            }
            bin.write((char*)qd.data(), (std::streamsize)n);
            bytes = n;
            dtype = "int8";
        } else if (q && quant == "int4_packed") {
            double mx = 0.0;
            for (int64_t k = 0; k < n; ++k)
                mx = std::max(mx, (double)std::fabs(t.d[(size_t)k]));
            scale = mx > 0.0 ? mx / 8.0 : 1.0;
            const int64_t last = t.shape.back();
            const int64_t rows = n / last, packed_row = (last + 1) / 2;
            std::vector<unsigned char> qd((size_t)(rows * packed_row), 0);
            for (int64_t r = 0; r < rows; ++r) {
                for (int64_t col = 0; col < last; ++col) {
                    double v = std::lround(
                        (double)t.d[(size_t)(r * last + col)] / scale);
                    int nib = (int)std::clamp(v, -8.0, 7.0) + 8;
                    unsigned char& byte = qd[(size_t)(r * packed_row + col / 2)];
                    if (col % 2 == 0) byte |= (unsigned char)nib;
                    else byte |= (unsigned char)(nib << 4);
                }
            }
            bin.write((char*)qd.data(), (std::streamsize)qd.size());
            bytes = (int64_t)qd.size();
            dtype = "int4_packed";
        } else {
            std::vector<double> tmp((size_t)n);
            for (int64_t k = 0; k < n; ++k)
                tmp[(size_t)k] = (double)t.d[(size_t)k];
            bin.write((char*)tmp.data(), (std::streamsize)n * 8);
            bytes = n * 8;
            dtype = "float64";
        }
        if (q) ++quantized_tensors;
        if (i) tensors_json << ',';
        tensors_json << '"' << pairs[i].first
                     << "\":{\"bytes\":" << bytes
                     << ",\"dtype\":\"" << dtype
                     << "\",\"endianness\":\"little\","
                     << "\"offset\":" << offset;
        if (q) tensors_json << ",\"scale\":" << scale;
        tensors_json << ",\"shape\":[";
        for (size_t d = 0; d < t.shape.size(); ++d) {
            if (d) tensors_json << ',';
            tensors_json << t.shape[d];
        }
        tensors_json << "]}";
        offset += bytes;
    }
    tensors_json << '}';
    bin.close();
    if (!bin) fail("EXPORT_WEIGHTS_WRITE_FAILED");
    std::string weights_sha = sha256_file(bin_tmp);
    std::remove(bin_path.c_str());
    if (std::rename(bin_tmp.c_str(), bin_path.c_str()) != 0)
        fail("EXPORT_WEIGHTS_RENAME_FAILED");

    // manifest.json ??config copied verbatim from --config-from so every
    // engine-side field (backend preference, rope, kv quant, ?? survives.
    std::string cfg_canon;
    {
        // Serialize config through the JsonValue tree (engine only reads
        // fields; whitespace is irrelevant for manifest consumers). Patch
        // the engine-visible quantization marker to match the shipped
        // tensor dtypes.
        JsonValue cfg_copy = *cfg;
        bool found = false;
        for (auto& kv : cfg_copy.object)
            if (kv.first == "quantization") { kv.second.string = quant;
                kv.second.type = JsonValue::Type::String; found = true; }
        if (!found) {
            JsonValue qv; qv.type = JsonValue::Type::String;
            qv.string = quant;
            cfg_copy.object.emplace_back("quantization", qv);
        }
        cfg_canon = gptbridge::jsonlite::json_serialize(cfg_copy);
    }
    std::string ckpt_sha = sha256_file(ckpt);
    // Checkpoint contract version read straight from the XCN1 header so
    // the bundle manifest carries the same contract identity as the
    // source artifact (unified 蝳?0 checkpoint/bundle contract).
    uint32_t ckpt_ver = 0;
    {
        std::ifstream ch(ckpt, std::ios::binary);
        char hdr[8] = {};
        if (ch.read(hdr, 8) &&
            hdr[0] == 'X' && hdr[1] == 'C' && hdr[2] == 'N' && hdr[3] == '1')
            std::memcpy(&ckpt_ver, hdr + 4, 4);
    }
    // Architecture generation declared by the shipped config (present
    // for canonical-generation jobs such as xc-fused-1).
    std::string arch_gen;
    {
        const JsonValue* g = cfg->get("generation");
        if (g && g->type == JsonValue::Type::String) arch_gen = g->string;
        if (arch_gen.empty()) {
            const JsonValue* g2 = src_manifest.get("architecture_generation");
            if (g2 && g2->type == JsonValue::Type::String)
                arch_gen = g2->string;
        }
    }
    int64_t now = (int64_t)std::chrono::duration_cast<std::chrono::seconds>(
                      std::chrono::system_clock::now().time_since_epoch())
                      .count();
    // 蝳?5 bundle provenance: resolve + hash the tokenizer up-front so the
    // manifest can ship the full evidence block (hash -> signature ->
    // generation -> architecture -> checkpoint -> shape -> runtime
    // compatibility -> load; provenance-check enforces it fail-closed).
    fs::path tk_src = resolve_tokenizer_path(tokenizer);
    const std::string tk_sha = sha256_file(tk_src.string());
    std::ostringstream mf;
    mf << "{\"checkpoint_sha256\":\"" << ckpt_sha << "\""
       << ",\"checkpoint_version\":" << ckpt_ver;
    if (!arch_gen.empty())
        mf << ",\"architecture_generation\":\""
           << gptbridge::jsonlite::json_escape(arch_gen) << "\"";
    mf << ",\"config\":" << cfg_canon
       << ",\"created_by\":\"xc-modeltool\""
       << ",\"created_at\":" << now
       << ",\"schema_version\":\"star-native-inference-bundle/v1\""
       << ",\"source_checkpoint\":\""
       << gptbridge::jsonlite::json_escape(ckpt) << "\""
       << ",\"source_ckpt_sha256\":\"" << ckpt_sha << "\""
       << ",\"lineage\":{\"source_checkpoint\":\""
       << gptbridge::jsonlite::json_escape(ckpt)
       << "\",\"source_ckpt_sha256\":\"" << ckpt_sha
       << "\",\"checkpoint_version\":" << ckpt_ver << "}"
       << ",\"tensors\":" << tensors_json.str()
       << ",\"weights_file\":\"weights.bin\""
       << ",\"weights_sha256\":\"" << weights_sha << "\""
       << ",\"param_count\":" << param_count
       << ",\"quantized_tensors\":" << quantized_tensors
       << ",\"quantization\":\"" << quant << "\""
       << ",\"tokenizer_sha256\":\"" << tk_sha << "\""
       << ",\"provenance\":{\"format\":\"star-bundle-provenance/v1\","
       << "\"build_id\":\"xc-modeltool/" << __DATE__ << "\","
       << "\"runtime_compatibility\":"
          "\"star-native-inference-engine/v1\","
       << "\"generation\":\""
       << gptbridge::jsonlite::json_escape(
              arch_gen.empty() ? std::string("unversioned") : arch_gen)
       << "\",\"xcn_version\":" << ckpt_ver
       << ",\"manifest_core_sha256\":\"";
    // manifest_core_sha256 covers everything before the provenance
    // block ??the hash is computed over the mf prefix already streamed.
    std::string core_sha = sha256_bytes(
        reinterpret_cast<const unsigned char*>(mf.str().data()),
        mf.str().size());
    mf << core_sha << "\"}}";
    std::string mf_path = (out_dir / "manifest.json").string();
    std::string mf_tmp = mf_path + ".tmp";
    {
        std::ofstream f(mf_tmp, std::ios::binary | std::ios::trunc);
        f << mf.str();
    }
    std::remove(mf_path.c_str());
    if (std::rename(mf_tmp.c_str(), mf_path.c_str()) != 0)
        fail("EXPORT_MANIFEST_RENAME_FAILED");

    fs::copy_file(tk_src, out_dir / "tokenizer.json",
                  fs::copy_options::overwrite_existing);

    std::printf("{\"ok\":true,\"mode\":\"export-bundle\",\"out\":\"%s\","
                "\"weights_sha256\":\"%s\",\"checkpoint_sha256\":\"%s\","
                "\"tensors\":%zu,\"param_count\":%lld,"
                "\"quantization\":\"%s\",\"weights_bytes\":%lld}\n",
                gptbridge::jsonlite::json_escape(out_dir.string()).c_str(),
                weights_sha.c_str(), ckpt_sha.c_str(), pairs.size(),
                (long long)param_count, quant.c_str(), (long long)offset);
    return 0;
}

// ------------------------------------------------------------------ eval --

JsonValue suite_load(const std::string& path, const char* fmt) {
    std::string raw = slurp(path);
    JsonParser p(raw);
    JsonValue suite = p.parse();
    const JsonValue* fv = suite.get("format_version");
    std::string got = fv && fv->type == JsonValue::Type::String
                          ? fv->string : std::string(fmt);
    if (got != fmt) fail("SUITE_FORMAT_MISMATCH:" + got);
    return suite;
}

std::string manifest_field(const fs::path& bundle, const char* key) {
    JsonValue m = parse_json_file((bundle / "manifest.json").string());
    const JsonValue* v = m.get(key);
    return v && v->type == JsonValue::Type::String ? v->string : "";
}

JsonValue eval_one(const std::string& bundle_dir, const JsonValue& suite) {
    NativeInferenceEngine engine;
    try {
        engine.load(bundle_dir);
    } catch (const std::exception& e) {
        fail(std::string("EVAL_ENGINE_LOAD_FAILED:") + e.what());
    }
    int64_t cap = (int64_t)xct::j_num(&suite, "eval_token_cap", 128);
    if (cap < 32) cap = 32;
    if (cap > 2048) cap = 2048;
    std::string eval_text = jget_str(suite, "eval_text");
    std::vector<int64_t> ids = engine.encode(eval_text, false, false);
    if ((int64_t)ids.size() > cap) ids.resize((size_t)cap);
    double ppl;
    if (ids.size() < 2) {
        ppl = std::numeric_limits<double>::quiet_NaN();
    } else {
        auto nll = engine.sequence_nll(ids);
        ppl = std::exp(nll.first / (double)std::max<int64_t>(1, nll.second));
    }

    std::string prompt = jget_str(suite, "sanity_prompt");
    if (prompt.empty()) prompt = "def main():";
    int64_t max_new = (int64_t)xct::j_num(&suite, "sanity_max_new_tokens", 16);
    uint64_t seed = (uint64_t)xct::j_num(&suite, "seed", 42);
    SamplingConfig sc;
    sc.do_sample = false;
    sc.seed = seed;
    std::vector<int64_t> pids = engine.encode(prompt, true, false);
    auto t0 = std::chrono::steady_clock::now();
    std::vector<int64_t> out;
    try {
        out = engine.generate(pids, max_new, sc);
    } catch (const std::exception& e) {
        engine.unload();
        fail(std::string("EVAL_GENERATION_FAILED:") + e.what());
    }
    double elapsed = std::chrono::duration<double>(
                         std::chrono::steady_clock::now() - t0).count();
    int64_t gen_tokens = (int64_t)out.size();
    if (gen_tokens > (int64_t)pids.size() &&
        std::equal(pids.begin(), pids.end(), out.begin())) {
        gen_tokens -= (int64_t)pids.size();  // engine returns prompt+new
    }
    double tps = elapsed > 0 ? gen_tokens / elapsed : 0.0;
    double latency_ms = gen_tokens > 0 ? elapsed * 1000.0 / gen_tokens : 0.0;
    engine.unload();

    JsonValue r;
    r.type = JsonValue::Type::Object;
    auto put = [&](const char* k, const JsonValue& v) {
        r.object.emplace_back(k, v);
    };
    auto num = [](double v) { JsonValue x; x.type = JsonValue::Type::Number; x.number = v; return x; };
    auto str = [](const std::string& v) { JsonValue x; x.type = JsonValue::Type::String; x.string = v; return x; };
    auto boolean = [](bool v) { JsonValue x; x.type = JsonValue::Type::Bool; x.boolean = v; return x; };
    if (std::isfinite(ppl)) put("perplexity", num(ppl));
    else { JsonValue n; n.type = JsonValue::Type::Null; put("perplexity", n); }
    put("generation_ok", boolean(gen_tokens > 0));
    put("generated_tokens", num((double)gen_tokens));
    put("tokens_per_second", num(tps));
    put("latency_ms_mean", num(latency_ms));
    put("eval_tokens", num((double)ids.size()));
    put("quantization", str(manifest_field(bundle_dir, "quantization").empty()
                                ? "none"
                                : manifest_field(bundle_dir, "quantization")));
    put("checkpoint_sha256", str(manifest_field(bundle_dir, "checkpoint_sha256")));
    put("throughput_engine", str("cpp"));
    return r;
}

// compare_metrics port (star-native-eval-suite/v1 quality gates).
std::pair<std::string, bool> compare_metrics(const JsonValue& baseline,
                                             const JsonValue& candidate,
                                             const JsonValue& gates) {
    double max_regression = xct::j_num(&gates, "max_perplexity_regression_pct", 5.0);
    bool require_generation = true;
    const JsonValue* rg = gates.get("require_generation");
    if (rg && rg->type == JsonValue::Type::Bool) require_generation = rg->boolean;
    double min_tps = xct::j_num(&gates, "min_tokens_per_second", 0.0);
    double tps_ratio = xct::j_num(&gates, "min_tps_baseline_ratio", 0.0);

    auto num_or = [](const JsonValue& o, const char* k, bool& present) {
        const JsonValue* v = o.get(k);
        if (v && v->type == JsonValue::Type::Number) { present = true; return v->number; }
        present = false; return 0.0;
    };
    bool bp, cp, bt;
    double base_ppl = num_or(baseline, "perplexity", bp);
    double cand_ppl = num_or(candidate, "perplexity", cp);
    double ppl_delta = 0.0;
    bool ppl_ok = true;
    bool delta_ok = false;
    if (bp && cp && base_ppl > 0) {
        ppl_delta = (cand_ppl - base_ppl) / base_ppl * 100.0;
        ppl_ok = ppl_delta <= max_regression;
        delta_ok = true;
    }
    bool generation_ok = true;
    const JsonValue* g = candidate.get("generation_ok");
    if (require_generation)
        generation_ok = g && g->type == JsonValue::Type::Bool && g->boolean;
    bool _bt;
    double cand_tps = num_or(candidate, "tokens_per_second", _bt);
    double base_tps = num_or(baseline, "tokens_per_second", bt);
    bool tps_ok = min_tps > 0 ? cand_tps >= min_tps : true;
    bool tps_ratio_ok = true;
    if (tps_ratio > 0)
        tps_ratio_ok = bt && base_tps > 0 && cand_tps >= base_tps * tps_ratio;
    tps_ok = tps_ok && tps_ratio_ok;
    bool passed = ppl_ok && generation_ok && tps_ok;

    std::ostringstream o;
    o << "{\"perplexity_delta_pct\":";
    if (delta_ok) o << ppl_delta; else o << "null";
    o << ",\"perplexity_ok\":" << (ppl_ok ? "true" : "false")
      << ",\"generation_ok\":" << (generation_ok ? "true" : "false")
      << ",\"tokens_per_second_ok\":" << (tps_ok ? "true" : "false")
      << ",\"tokens_per_second_ratio_ok\":"
      << (tps_ratio_ok ? "true" : "false")
      << ",\"baseline_tokens_per_second\":";
    if (bt) o << base_tps; else o << "null";
    o << '}';
    return {o.str(), passed};
}

int mode_eval(const Args& a) {
    std::string bundle = a.get("bundle");
    std::string suite_path = a.get("suite");
    if (bundle.empty() || suite_path.empty()) fail("EVAL_ARGS_MISSING");
    std::string raw = slurp(suite_path);
    JsonValue suite = suite_load(suite_path, "star-native-eval-suite/v1");
    std::string suite_sha = suite_sha256(raw);
    const JsonValue* gates = suite.get("quality_gates");
    if (!gates) fail("EVAL_SUITE_MISSING_GATES");

    JsonValue cand = eval_one(bundle, suite);

    JsonValue baseline;
    baseline.type = JsonValue::Type::Object;
    if (a.has("baseline-bundle")) {
        baseline = eval_one(a.get("baseline-bundle"), suite);
    } else if (a.has("baseline-metrics")) {
        baseline = parse_json_file(a.get("baseline-metrics"));
    } else {
        const JsonValue* bm = suite.get("baseline_metrics");
        if (bm) baseline = *bm;
    }

    std::string cmp_json = "{}";
    bool passed = false;
    if (baseline.type == JsonValue::Type::Object && !baseline.object.empty()) {
        auto cmp = compare_metrics(baseline, cand, *gates);
        cmp_json = cmp.first;
        passed = cmp.second;
    } else {
        // No baseline: absolute gates only (generation + min_tps).
        JsonValue empty;
        empty.type = JsonValue::Type::Object;
        auto cmp = compare_metrics(empty, cand, *gates);
        cmp_json = cmp.first;
        passed = cmp.second;
    }
    std::printf("{\"ok\":true,\"mode\":\"eval\",\"suite_id\":\"%s\","
                "\"suite_sha256\":\"%s\",\"passed\":%s,"
                "\"candidate\":%s,\"baseline\":%s,\"comparison\":%s}\n",
                gptbridge::jsonlite::json_escape(jget_str(suite, "suite_id")).c_str(),
                suite_sha.c_str(), passed ? "true" : "false",
                gptbridge::jsonlite::json_serialize(cand).c_str(),
                gptbridge::jsonlite::json_serialize(baseline).c_str(),
                cmp_json.c_str());
    return passed ? 0 : 2;
}

// ------------------------------------------------------------- capability --

std::string generate_reply(NativeInferenceEngine& engine,
                           const std::string& prompt, int64_t max_new,
                           uint64_t seed) {
    SamplingConfig sc;
    sc.do_sample = false;
    sc.seed = seed;
    std::vector<int64_t> ids = engine.encode(prompt, true, false);
    std::vector<int64_t> out = engine.generate(ids, max_new, sc);
    if (out.size() > ids.size() &&
        std::equal(ids.begin(), ids.end(), out.begin())) {
        out.erase(out.begin(), out.begin() + (ptrdiff_t)ids.size());
    }
    return engine.decode(out, true);
}

double block_perplexity(NativeInferenceEngine& engine,
                        const std::string& text) {
    std::vector<int64_t> ids = engine.encode(text, false, false);
    if (ids.size() < 9) return std::numeric_limits<double>::quiet_NaN();
    int64_t block = std::min<int64_t>(64, (int64_t)ids.size() - 1);
    double total = 0.0;
    int64_t batches = 0;
    // range(0, len(ids) - block, block) ??non-overlapping windows.
    for (size_t start = 0; start + (size_t)block < ids.size();
         start += (size_t)block) {
        std::vector<int64_t> w(ids.begin() + (ptrdiff_t)start,
                               ids.begin() + (ptrdiff_t)(start + block));
        auto nll = engine.sequence_nll(w);
        if (nll.second > 0) {
            total += nll.first / (double)nll.second;
            ++batches;
        }
    }
    if (!batches) return std::numeric_limits<double>::quiet_NaN();
    return std::exp(std::min(20.0, total / (double)batches));
}

bool split_tool_call_name(const std::string& reply,
                          const std::string& want) {
    size_t open = reply.find("<tool_call>");
    if (open == std::string::npos) return false;
    size_t close = reply.find("</tool_call>", open);
    if (close == std::string::npos) return false;
    std::string body =
        reply.substr(open + 11, close - open - 11);
    try {
        JsonParser p(body);
        JsonValue v = p.parse();
        const JsonValue* name = v.get("name");
        return name && name->type == JsonValue::Type::String &&
               name->string == want;
    } catch (...) {
        return false;
    }
}

int64_t first_int(const std::string& s, bool& found) {
    std::smatch m;
    static const std::regex re("-?\\d+");
    if (std::regex_search(s, m, re)) {
        found = true;
        return std::stoll(m.str());
    }
    found = false;
    return 0;
}

int64_t last_int(const std::string& s, bool& found) {
    static const std::regex re("-?\\d+");
    auto b = std::sregex_iterator(s.begin(), s.end(), re);
    auto e = std::sregex_iterator();
    found = b != e;
    int64_t v = 0;
    for (auto it = b; it != e; ++it) v = std::stoll(it->str());
    return v;
}

int mode_capability(const Args& a) {
    std::string bundle = a.get("bundle");
    std::string suite_path = a.get("suite");
    if (bundle.empty() || suite_path.empty()) fail("CAPABILITY_ARGS_MISSING");
    std::string raw = slurp(suite_path);
    JsonValue suite = suite_load(suite_path, "star-capability-suite/v1");
    const JsonValue* items = suite.get("items");
    if (!items || items->type != JsonValue::Type::Array)
        fail("CAPABILITY_SUITE_ITEMS_MISSING");
    std::string suite_sha = suite_sha256(raw);
    bool chat = a.has("chat");

    // Fail-closed overlap check against the training corpus manifest.
    std::unordered_set<std::string> corpus_hashes;
    bool overlap_free = true;
    std::string overlap_error;
    if (a.has("corpus-manifest")) {
        fs::path mpath = fs::absolute(a.get("corpus-manifest"));
        JsonValue manifest = parse_json_file(mpath.string());
        try {
            for (const char* split : {"train", "val"}) {
                const JsonValue* entry = manifest.get(split);
                if (!entry) continue;
                std::string rel = jget_str(*entry, "path");
                if (rel.empty()) continue;
                for (const JsonValue& rec :
                     read_jsonl((mpath.parent_path() / rel).string())) {
                    std::string h = jget_str(rec, "sha256");
                    if (!h.empty()) corpus_hashes.insert(h);
                    std::string t = jget_str(rec, "text");
                    if (!t.empty()) corpus_hashes.insert(norm_text_sha(t));
                }
            }
        } catch (...) {
            overlap_free = false;
            overlap_error = "corpus jsonl unreadable";
        }
    }
    std::unordered_set<std::string> rejected_ids;
    for (const auto& item : items->array) {
        for (const char* field : {"prompt", "eval_text", "expected"}) {
            const JsonValue* v = item.get(field);
            std::string t;
            if (v) {
                t = v->type == JsonValue::Type::String
                        ? v->string
                        : gptbridge::jsonlite::json_serialize(*v);
            }
            if (!t.empty() && corpus_hashes.count(norm_text_sha(t))) {
                rejected_ids.insert(jget_str(item, "id"));
                break;
            }
        }
    }
    if (!rejected_ids.empty()) overlap_free = false;

    NativeInferenceEngine engine;
    try {
        engine.load(bundle);
    } catch (const std::exception& e) {
        fail(std::string("CAPABILITY_ENGINE_LOAD_FAILED:") + e.what());
    }
    uint64_t seed = (uint64_t)xct::j_num(&suite, "seed", 42);

    std::unordered_map<std::string, std::vector<std::string>> cat_items;
    std::vector<std::string> cat_order;
    auto track = [&](const std::string& cat, const std::string& detail) {
        if (!cat_items.count(cat)) cat_order.push_back(cat);
        cat_items[cat].push_back(detail);
    };
    for (const auto& item : items->array) {
        std::string id = jget_str(item, "id");
        std::string cat = jget_str(item, "category");
        std::string kind = jget_str(item, "check");
        std::ostringstream d;
        d << "{\"id\":\"" << gptbridge::jsonlite::json_escape(id)
          << "\",\"check\":\"" << gptbridge::jsonlite::json_escape(kind) << "\"";
        if (rejected_ids.count(id)) {
            d << ",\"passed\":false,\"skipped\":\"train-eval-overlap\"}";
            track(cat, d.str());
            continue;
        }
        try {
            if (kind == "router_health") {
                // The C++ engine exposes no router-metrics surface ??
                // same outcome as the Python lane on a metrics-less
                // checkpoint: skipped, excluded from pass_rate.
                d << ",\"passed\":false,\"skipped\":\"no-moe-metrics-surface\"}";
                track(cat, d.str());
                continue;
            }
            if (kind == "ppl_max") {
                double ppl = block_perplexity(engine, jget_str(item, "eval_text"));
                double max = xct::j_num(&item, "max", 0);
                bool pass = std::isfinite(ppl) && ppl <= max;
                d << ",\"perplexity\":" << ppl
                  << ",\"passed\":" << (pass ? "true" : "false") << '}';
                track(cat, d.str());
                continue;
            }
            std::string prompt = jget_str(item, "prompt");
            if (chat) {
                prompt = "<|user|>\n" + py_strip(prompt) +
                         "\n<|eot|>\n<|assistant|>\n";
            }
            int64_t max_new = (int64_t)xct::j_num(&item, "max_new_tokens", 32);
            std::string reply = generate_reply(engine, prompt, max_new, seed);
            std::string reply_shown = reply.substr(0, 200);
            d << ",\"reply\":\""
              << gptbridge::jsonlite::json_escape(reply_shown) << "\"";
            bool pass = false;
            if (kind == "contains") {
                pass = reply.find(jget_str(item, "expected")) != std::string::npos;
            } else if (kind == "choice") {
                const JsonValue* allowed = item.get("allowed");
                if (allowed && allowed->type == JsonValue::Type::Array &&
                    !allowed->array.empty()) {
                    for (const auto& al : allowed->array)
                        if (al.type == JsonValue::Type::String &&
                            reply.find(al.string) != std::string::npos)
                            pass = true;
                } else {
                    pass = reply.find(jget_str(item, "expected")) !=
                           std::string::npos;
                }
            } else if (kind == "first_int") {
                bool found;
                int64_t v = first_int(reply, found);
                pass = found &&
                       v == (int64_t)xct::j_num(&item, "expected", 0);
            } else if (kind == "last_int") {
                size_t eot = reply.find("<|eot|>");
                std::string seg =
                    eot == std::string::npos ? reply : reply.substr(0, eot);
                bool found;
                int64_t v = last_int(seg, found);
                pass = found && v == (int64_t)xct::j_num(&item, "expected", 0);
            } else if (kind == "regex") {
                pass = std::regex_search(reply,
                                         std::regex(jget_str(item, "pattern")));
            } else if (kind == "regex_all") {
                // instruction-recovery: every pattern in `patterns` must
                // match the same generation (composite constraints).
                pass = true;
                const JsonValue* pats = item.get("patterns");
                if (pats && pats->type == JsonValue::Type::Array) {
                    for (const auto& p : pats->array) {
                        if (p.type != JsonValue::Type::String ||
                            !std::regex_search(reply, std::regex(p.string))) {
                            pass = false; break;
                        }
                    }
                } else pass = false;
            } else if (kind == "not_contains") {
                // negative-constraint check: `expected` and every entry of
                // `forbidden` must be absent from the reply.
                pass = reply.find(jget_str(item, "expected")) ==
                       std::string::npos;
                const JsonValue* fb = item.get("forbidden");
                if (fb && fb->type == JsonValue::Type::Array)
                    for (const auto& f : fb->array)
                        if (f.type == JsonValue::Type::String &&
                            reply.find(f.string) != std::string::npos)
                            pass = false;
            } else if (kind == "count_lines") {
                // count non-empty lines matching optional `line_pattern`;
                // pass iff count == expected (exact-count instructions).
                int64_t want = (int64_t)xct::j_num(&item, "expected", -1);
                int64_t totalWant =
                    (int64_t)xct::j_num(&item, "total_lines", -1);
                std::string lp = jget_str(item, "line_pattern");
                int64_t cnt = 0, total = 0;
                std::istringstream iss(reply);
                std::string line;
                while (std::getline(iss, line)) {
                    std::string t = py_strip(line);
                    if (t.empty()) continue;
                    ++total;
                    if (!lp.empty() &&
                        !std::regex_search(t, std::regex(lp))) continue;
                    ++cnt;
                }
                pass = want >= 0 && cnt == want &&
                       (totalWant < 0 || total == totalWant);
            } else if (kind == "json_valid") {
                // structural JSON gate: reply (optionally fenced) must parse
                // as an object carrying `required_fields`; `exact_fields`
                // forbids extra keys; `field_order` enforces key order.
                std::string t = py_strip(reply);
                size_t eot = t.find("<|eot|>");
                if (eot != std::string::npos) t = py_strip(t.substr(0, eot));
                if (t.rfind("```", 0) == 0) {
                    size_t nl = t.find('\n');
                    size_t end = t.rfind("```");
                    if (nl != std::string::npos && end > nl)
                        t = py_strip(t.substr(nl + 1, end - nl - 1));
                }
                bool parsed = false;
                try {
                    JsonValue j = JsonParser(t).parse();
                    if (j.type == JsonValue::Type::Object) {
                        parsed = true;
                        const JsonValue* rf = item.get("required_fields");
                        if (rf && rf->type == JsonValue::Type::Array) {
                            std::vector<std::string> order;
                            for (const auto& kv : j.object)
                                order.push_back(kv.first);
                            for (const auto& f : rf->array)
                                if (f.type == JsonValue::Type::String &&
                                    !j.get(f.string))
                                    parsed = false;
                            if (parsed &&
                                xct::j_num(&item, "exact_fields", 0) > 0.5 &&
                                order.size() != (size_t)rf->array.size())
                                parsed = false;
                            const JsonValue* fo = item.get("field_order");
                            if (parsed && fo &&
                                fo->type == JsonValue::Type::Array) {
                                std::vector<std::string> want;
                                for (const auto& f : fo->array)
                                    if (f.type == JsonValue::Type::String)
                                        want.push_back(f.string);
                                std::vector<std::string> got;
                                for (auto& k : order)
                                    if (std::find(want.begin(), want.end(), k)
                                        != want.end())
                                        got.push_back(k);
                                if (got != want) parsed = false;
                            }
                            // structured-output lane: `field_types`
                            // {name: "string"|"number"|"boolean"|
                            // "array"|"object"|"null"} verifies the JSON
                            // VALUE kind, not just key presence — a
                            // quoted number fails a "number" field.
                            const JsonValue* ft = item.get("field_types");
                            if (parsed && ft &&
                                ft->type == JsonValue::Type::Object) {
                                for (const auto& kv : ft->object) {
                                    const JsonValue* fv =
                                        j.get(kv.first);
                                    if (!fv) { parsed = false; break; }
                                    const std::string& want =
                                        kv.second.string;
                                    bool ok =
                                        (want == "string" &&
                                         fv->type == JsonValue::Type::String) ||
                                        (want == "number" &&
                                         fv->type == JsonValue::Type::Number) ||
                                        (want == "boolean" &&
                                         fv->type == JsonValue::Type::Bool) ||
                                        (want == "array" &&
                                         fv->type == JsonValue::Type::Array) ||
                                        (want == "object" &&
                                         fv->type == JsonValue::Type::Object) ||
                                        (want == "null" &&
                                         fv->type == JsonValue::Type::Null);
                                    if (!ok) { parsed = false; break; }
                                }
                            }
                            // `field_values` {name: [allowed scalars]} —
                            // enum membership on the field's value.
                            const JsonValue* fvals =
                                item.get("field_values");
                            if (parsed && fvals &&
                                fvals->type == JsonValue::Type::Object) {
                                for (const auto& kv : fvals->object) {
                                    const JsonValue* fv =
                                        j.get(kv.first);
                                    if (!fv ||
                                        kv.second.type !=
                                            JsonValue::Type::Array) {
                                        parsed = false; break;
                                    }
                                    bool ok = false;
                                    for (const auto& av : kv.second.array) {
                                        if (fv->type == av.type &&
                                            ((av.type ==
                                                JsonValue::Type::String &&
                                              fv->string == av.string) ||
                                             (av.type ==
                                                JsonValue::Type::Number &&
                                              fv->number == av.number) ||
                                             (av.type ==
                                                JsonValue::Type::Bool &&
                                              fv->boolean == av.boolean)))
                                            ok = true;
                                    }
                                    if (!ok) { parsed = false; break; }
                                }
                            }
                        }
                    }
                } catch (...) { parsed = false; }
                pass = parsed;
            } else if (kind == "tool_call") {
                pass = split_tool_call_name(reply, jget_str(item, "tool_name"));
            } else {
                d << ",\"passed\":false,\"error\":\"unknown-check\"}";
                track(cat, d.str());
                continue;
            }
            d << ",\"passed\":" << (pass ? "true" : "false") << '}';
            track(cat, d.str());
        } catch (const std::exception& e) {
            std::string msg = e.what();
            if (msg.size() > 200) msg.resize(200);
            d << ",\"passed\":false,\"error\":\""
              << gptbridge::jsonlite::json_escape(msg) << "\"}";
            track(cat, d.str());
        }
    }
    engine.unload();

    // categories rollup ??same shape as star-capability-eval/v1.
    std::ostringstream cats;
    cats << '{';
    bool first_cat = true;
    for (const auto& cat : cat_order) {
        auto& list = cat_items[cat];
        int64_t evaluated = 0, passed = 0;
        for (auto& d : list) {
            bool skipped = d.find("\"skipped\"") != std::string::npos;
            bool ok = d.find("\"passed\":true") != std::string::npos;
            if (!skipped) ++evaluated;
            if (ok && !skipped) ++passed;
        }
        if (!first_cat) cats << ',';
        first_cat = false;
        cats << '"' << cat << "\":{\"items\":" << list.size()
             << ",\"evaluated\":" << evaluated << ",\"passed\":" << passed
             << ",\"pass_rate\":";
        if (evaluated) cats << (double)passed / (double)evaluated;
        else cats << "null";
        cats << '}';
    }
    cats << '}';
    // items object (cat -> [detail,...]).
    std::ostringstream items_obj;
    items_obj << '{';
    bool first = true;
    for (const auto& cat : cat_order) {
        if (!first) items_obj << ',';
        first = false;
        items_obj << '"' << cat << "\":[";
        for (size_t i = 0; i < cat_items[cat].size(); ++i) {
            if (i) items_obj << ',';
            items_obj << cat_items[cat][i];
        }
        items_obj << ']';
    }
    items_obj << '}';

    std::string ckpt_sha = manifest_field(bundle, "checkpoint_sha256");
    std::string tk_sha = sha256_file(
        (fs::path(bundle) / "tokenizer.json").string());
    std::string suite_id = jget_str(suite, "suite_id");
    int64_t now = (int64_t)std::chrono::duration_cast<std::chrono::seconds>(
                      std::chrono::system_clock::now().time_since_epoch())
                      .count();
    std::ostringstream report;
    report << "{\"format_version\":\"star-capability-eval/v1\""
           << ",\"suite_id\":\"" << gptbridge::jsonlite::json_escape(suite_id)
           << "\",\"suite_sha256\":\"" << suite_sha << "\""
           << ",\"model_id\":\"xingcheng-native-transformer\""
           << ",\"model_version\":\"" << ckpt_sha.substr(0, 16) << "\""
           << ",\"checkpoint_sha256\":\"" << ckpt_sha << "\""
           << ",\"tokenizer_sha256\":\"" << tk_sha << "\""
           << ",\"dataset_version\":\"" << suite_id << '@'
           << suite_sha.substr(0, 16) << "\""
           << ",\"inference_backend\":\"cpu/fp64\""
           << ",\"prompt_mode\":\"" << (chat ? "chat" : "verbatim") << "\""
           << ",\"quantization\":\"none\""
           << ",\"categories\":" << cats.str()
           << ",\"items\":" << items_obj.str()
           << ",\"overlap\":{\"overlap_free\":"
           << (overlap_free ? "true" : "false")
           << ",\"rejected_items\":[";
    {
        bool fr = true;
        for (auto& r : rejected_ids) {
            if (!fr) report << ',';
            fr = false;
            report << '"' << r << '"';
        }
    }
    report << "],\"corpus_documents_checked\":" << corpus_hashes.size();
    if (!overlap_error.empty())
        report << ",\"error\":\"" << overlap_error << "\"";
    report << "},\"third_party_used\":false"
           << ",\"recorded_at\":\"" << now << "\"}";

    // Optional regression comparison against a baseline report file.
    if (a.has("baseline-report")) {
        JsonValue base = parse_json_file(a.get("baseline-report"));
        const JsonValue* bcats = base.get("categories");
        std::ostringstream cmp;
        std::vector<std::string> regressions;
        if (bcats && bcats->type == JsonValue::Type::Object) {
            std::string cats_raw = cats.str();
            JsonParser cp(cats_raw);
            JsonValue ccats = cp.parse();
            for (const auto& [cat, binfo] : bcats->object) {
                const JsonValue* brate = binfo.get("pass_rate");
                const JsonValue* cinfo = ccats.get(cat);
                const JsonValue* crate = cinfo ? cinfo->get("pass_rate") : nullptr;
                if (brate && brate->type == JsonValue::Type::Number && crate &&
                    crate->type == JsonValue::Type::Number &&
                    crate->number < brate->number - 1e-9) {
                    std::ostringstream r;
                    r << "{\"category\":\"" << cat
                      << "\",\"baseline\":" << brate->number
                      << ",\"candidate\":" << crate->number << '}';
                    regressions.push_back(r.str());
                }
            }
        }
        cmp << "{\"passed\":" << (regressions.empty() ? "true" : "false")
            << ",\"regressions\":[";
        for (size_t i = 0; i < regressions.size(); ++i) {
            if (i) cmp << ',';
            cmp << regressions[i];
        }
        cmp << "],\"baseline_suite\":\""
            << gptbridge::jsonlite::json_escape(jget_str(base, "suite_id"))
            << "\",\"candidate_suite\":\""
            << gptbridge::jsonlite::json_escape(suite_id) << "\"}";
        std::printf("{\"ok\":true,\"mode\":\"capability\",\"passed\":%s,"
                    "\"report\":%s,\"comparison\":%s}\n",
                    regressions.empty() ? "true" : "false",
                    report.str().c_str(), cmp.str().c_str());
        return regressions.empty() ? 0 : 2;
    }
    std::printf("{\"ok\":true,\"mode\":\"capability\",\"report\":%s}\n",
                report.str().c_str());
    return 0;
}

// -------------------------------------------------------- mtp-draft-probe --
// XCN10 MTP: real draft/verify against the exported MTP head. The
// trainer's mtp_fwd is re-derived here in fp64 against bundle tensors ??
// cin = [rmsnorm(h_t;norm_h) | rmsnorm(e_{t+1};norm_e)] -> w_proj ->
// norm1 -> full-rope(YaRN) causal attention over the MTP module's own
// K/V -> wo -> +res -> norm2 -> gated FFN -> +res -> norm_out -> shared
// lm_head. Greedy argmax verification: a draft is accepted iff it equals
// the trunk argmax ??emitted tokens are identical to plain greedy by
// construction, so `output_parity` is guaranteed, not claimed.
// draft_length=1 evidence only: the engine-side NativeMtpDrafter
// production dispatch is not bound; this mode reports measured
// acceptance, never a speedup claim, and SPECULATIVE_DECODER_DISABLED
// remains in effect for production.

namespace {

struct MtpDraft {
    const xingcheng::inference::WeightBundle* b = nullptr;
    const xingcheng::inference::ModelConfig* c = nullptr;
    const xingcheng::inference::TensorView* norm_h = nullptr;
    const xingcheng::inference::TensorView* norm_e = nullptr;
    const xingcheng::inference::TensorView* w_proj = nullptr;
    const xingcheng::inference::TensorView* norm1 = nullptr;
    const xingcheng::inference::TensorView* wq = nullptr;
    const xingcheng::inference::TensorView* wk = nullptr;
    const xingcheng::inference::TensorView* wv = nullptr;
    const xingcheng::inference::TensorView* wo = nullptr;
    const xingcheng::inference::TensorView* norm2 = nullptr;
    const xingcheng::inference::TensorView* w1 = nullptr;
    const xingcheng::inference::TensorView* w3 = nullptr;
    const xingcheng::inference::TensorView* w2 = nullptr;
    const xingcheng::inference::TensorView* norm_out = nullptr;
    const xingcheng::inference::TensorView* lm_head = nullptr;
    const xingcheng::inference::TensorView* embed = nullptr;
    int64_t positions = 0;
    std::vector<double> kv_k;   // [pos][kvh*hd]
    std::vector<double> kv_v;   // [pos][kvh*hd]

    static constexpr int kHeadCount = 13;
    // Canonical MTP head tensor set ??the full XCN10 contract a
    // production drafter would bind.
    static const char* const* head_names() {
        static const char* names[kHeadCount] = {
            "model.mtp.norm_h.weight", "model.mtp.norm_e.weight",
            "model.mtp.w_proj.weight", "model.mtp.norm1.weight",
            "model.mtp.wq.weight", "model.mtp.wk.weight",
            "model.mtp.wv.weight", "model.mtp.wo.weight",
            "model.mtp.norm2.weight", "model.mtp.w1.weight",
            "model.mtp.w3.weight", "model.mtp.w2.weight",
            "model.mtp.norm_out.weight"};
        return names;
    }

    // XCN10 stack naming (mtp_depth >= 1, xc-fused-1 canonical): the
    // depth-0 module is the draft head; role names differ from the
    // legacy nextn flat names only -- same tensors, same math.
    static const char* const* stack_names(int d) {
        static std::string buf[kHeadCount];
        static const char* ptrs[kHeadCount];
        static const char* role[kHeadCount] = {
            "eh", "et", "proj", "norm1", "wq", "wk", "wv", "wo",
            "norm2", "w1", "w3", "w2", "norm_o"};
        for (int i = 0; i < kHeadCount; ++i) {
            buf[i] = "model.mtp." + std::to_string(d) + "." +
                     role[i] + ".weight";
            ptrs[i] = buf[i].c_str();
        }
        return ptrs;
    }

    const char* bound_family = nullptr;  // "nextn" or "stack0"
    const char* const* bound_names_ = nullptr;

    // Returns the first missing/empty head tensor name, or nullptr.
    const char* bind_fail() {
        const xingcheng::inference::TensorView** dst[] = {
            &norm_h, &norm_e, &w_proj, &norm1, &wq, &wk, &wv, &wo,
            &norm2, &w1, &w3, &w2, &norm_out};
        // Prefer the legacy flat nextn set; fall back to the XCN10
        // depth-0 stack module -- never mix families.
        const char* const* names = head_names();
        bool flat_all = true;
        for (int i = 0; i < kHeadCount; ++i)
            if (!b->has_tensor(names[i]) ||
                b->tensor(names[i]).data == nullptr ||
                b->tensor(names[i]).size() <= 0) {
                flat_all = false;
                break;
            }
        if (!flat_all) names = stack_names(0);
        bound_names_ = names;
        bound_family = flat_all ? "nextn" : "stack0";
        const char* missing = nullptr;
        for (int i = 0; i < kHeadCount; ++i) {
            if (!b->has_tensor(names[i])) {
                if (!missing) missing = names[i];
                continue;
            }
            const xingcheng::inference::TensorView* tv =
                &b->tensor(names[i]);
            if (tv->data == nullptr || tv->size() <= 0) {
                if (!missing) missing = names[i];
                continue;
            }
            *dst[i] = tv;
        }
        if (missing) return missing;
        const char* lm = c->tie_word_embeddings
            ? "model.embeddings.word_embeddings.weight"
            : "lm_head.weight";
        if (!b->has_tensor(lm)) return lm;
        if (!b->has_tensor("model.embeddings.word_embeddings.weight"))
            return "model.embeddings.word_embeddings.weight";
        lm_head = &b->tensor(lm);
        embed = &b->tensor("model.embeddings.word_embeddings.weight");
        if (!lm_head->data) return lm;
        if (!embed->data)
            return "model.embeddings.word_embeddings.weight";
        return nullptr;
    }

    // Shape validation against the trunk config ??a bound head whose
    // tensors disagree with the bundle's declared geometry is an
    // MTP_BUNDLE_MISMATCH, not a usable draft head.
    const char* mismatch() const {
        const int64_t H = c->hidden_size, hd = c->head_dim;
        const int64_t nh = c->num_attention_heads,
                      kvh = c->num_key_value_heads;
        const int64_t I = c->intermediate_size, V = c->vocab_size;
        const char* const* N = bound_names_ ? bound_names_
                                            : head_names();
        // N order: 0 norm_h, 1 norm_e, 2 w_proj, 3 norm1, 4 wq, 5 wk,
        // 6 wv, 7 wo, 8 norm2, 9 w1, 10 w3, 11 w2, 12 norm_out.
        if (norm_h->size() != H) return N[0];
        if (norm_e->size() != H) return N[1];
        if (norm1->size() != H) return N[3];
        if (norm2->size() != H) return N[8];
        if (norm_out->size() != H) return N[12];
        if (w_proj->size() != H * 2 * H) return N[2];
        if (wq->size() != nh * hd * H) return N[4];
        if (wk->size() != kvh * hd * H) return N[5];
        if (wv->size() != kvh * hd * H) return N[6];
        if (wo->size() != H * nh * hd) return N[7];
        if (w1->size() != I * H) return N[9];
        if (w3->size() != I * H) return N[10];
        if (w2->size() != H * I) return N[11];
        if (lm_head->size() != V * H) return "lm_head.weight";
        if (embed->size() != V * H)
            return "model.embeddings.word_embeddings.weight";
        return nullptr;
    }

    static void rmsnorm(const double* x, const double* w, double* y,
                        int64_t n, double eps) {
        double ss = 0.0;
        for (int64_t i = 0; i < n; ++i) ss += x[i] * x[i];
        const double inv = 1.0 / std::sqrt(ss / (double)n + eps);
        for (int64_t i = 0; i < n; ++i) y[i] = x[i] * w[i] * inv;
    }
    static void matvec(const double* W, const double* x, double* y,
                       int64_t out, int64_t in) {
        for (int64_t o = 0; o < out; ++o) {
            const double* r = W + (size_t)o * in;
            double s = 0.0;
            for (int64_t i = 0; i < in; ++i) s += r[i] * x[i];
            y[o] = s;
        }
    }
    static double gate_act(double x, bool gelu) {
        if (!gelu) return x / (1.0 + std::exp(-x));
        const double c0 = 0.7978845608028654, c1 = 0.044715;
        double u = c0 * (x + c1 * x * x * x);
        return 0.5 * x * (1.0 + std::tanh(u));
    }
    // Trainer rope convention for the MTP block: interleaved pairs
    // (2p, 2p+1) over the FULL head_dim with the YaRN-blended table ??
    // deliberately not the trunk's rotate-half partial rope.
    void rope_pos(double* v, int64_t nh, int64_t pos) const {
        const int64_t hd = c->head_dim;
        const int64_t half = hd / 2;
        const double theta = c->rope_theta;
        const bool yarn = c->use_yarn();
        const double ms = !yarn ? 1.0 :
            (c->yarn_attention_factor > 0.0
                 ? c->yarn_attention_factor
                 : 0.1 * std::log(c->yarn_factor) + 1.0);
        const double logb = std::log(theta);
        auto blend = [&](int64_t p) {
            if (!yarn) return 1.0;
            auto corr = [&](double beta) {
                return (double)hd *
                       std::log((double)c->yarn_original_max_position_embeddings /
                                (beta * 6.28318530718)) / (2.0 * logb);
            };
            double lo = std::max(0.0, std::floor(corr(c->yarn_beta_fast)));
            double hi = std::min((double)(half - 1),
                                 std::ceil(corr(c->yarn_beta_slow)));
            if (hi == lo) hi = lo + 1e-3;
            double ext = 1.0 - std::min(1.0,
                std::max(0.0, ((double)p - lo) / (hi - lo)));
            return ext + (1.0 - ext) / c->yarn_factor;
        };
        for (int64_t h = 0; h < nh; ++h) {
            double* r = v + (size_t)h * hd;
            for (int64_t p = 0; p < half; ++p) {
                double fr = std::pow(theta, -2.0 * (double)p / (double)hd)
                            * blend(p);
                double co = std::cos((double)pos * fr) * ms;
                double si = std::sin((double)pos * fr) * ms;
                double a = r[2 * p], bv = r[2 * p + 1];
                r[2 * p] = a * co - bv * si;
                r[2 * p + 1] = a * si + bv * co;
            }
        }
    }

    // z_t = w_proj [rmsnorm(h) | rmsnorm(e_next)] ??the shared head of
    // every MTP position. Returns z; k/v appended separately.
    std::vector<double> z_of(const double* h_t, const double* e_next) const {
        const int64_t H = c->hidden_size;
        std::vector<double> cin((size_t)2 * H);
        rmsnorm(h_t, norm_h->data, cin.data(), H, c->rms_norm_eps);
        rmsnorm(e_next, norm_e->data, cin.data() + H, H, c->rms_norm_eps);
        std::vector<double> z((size_t)H);
        matvec(w_proj->data, cin.data(), z.data(), H, 2 * H);
        return z;
    }
    // Append position t's K/V (z must already be computed).
    void kv_append(const std::vector<double>& z, int64_t pos) {
        const int64_t H = c->hidden_size;
        const int64_t kvl = c->num_key_value_heads * c->head_dim;
        std::vector<double> n1((size_t)H);
        rmsnorm(z.data(), norm1->data, n1.data(), H, c->rms_norm_eps);
        std::vector<double> k((size_t)kvl), v((size_t)kvl);
        matvec(wk->data, n1.data(), k.data(), kvl, H);
        matvec(wv->data, n1.data(), v.data(), kvl, H);
        rope_pos(k.data(), c->num_key_value_heads, pos);
        kv_k.insert(kv_k.end(), k.begin(), k.end());
        kv_v.insert(kv_v.end(), v.begin(), v.end());
        ++positions;
    }
    // Full block at position t over accumulated KV -> draft logits argmax.
    int64_t draft(const std::vector<double>& z, int64_t pos) const {
        const int64_t H = c->hidden_size, hd = c->head_dim;
        const int64_t nh = c->num_attention_heads;
        const int64_t kvh = c->num_key_value_heads;
        const int64_t kvl = kvh * hd, Hq = nh * hd;
        const int64_t group = nh / kvh;
        std::vector<double> n1((size_t)H);
        rmsnorm(z.data(), norm1->data, n1.data(), H, c->rms_norm_eps);
        std::vector<double> q((size_t)Hq);
        matvec(wq->data, n1.data(), q.data(), Hq, H);
        rope_pos(q.data(), nh, pos);
        const double scale = 1.0 / std::sqrt((double)hd);
        std::vector<double> attn((size_t)Hq, 0.0);
        std::vector<double> scores((size_t)positions);
        for (int64_t h = 0; h < nh; ++h) {
            const int64_t kh2 = h / group;
            const double* qr = q.data() + (size_t)h * hd;
            double mx = -1e300;
            for (int64_t s = 0; s < positions; ++s) {
                const double* kr = kv_k.data() + (size_t)s * kvl +
                                   (size_t)kh2 * hd;
                double d = 0.0;
                for (int64_t i = 0; i < hd; ++i) d += qr[i] * kr[i];
                scores[(size_t)s] = d * scale;
                mx = std::max(mx, scores[(size_t)s]);
            }
            double sum = 0.0;
            for (int64_t s = 0; s < positions; ++s) {
                scores[(size_t)s] = std::exp(scores[(size_t)s] - mx);
                sum += scores[(size_t)s];
            }
            double* ao = attn.data() + (size_t)h * hd;
            for (int64_t s = 0; s < positions; ++s) {
                const double p = scores[(size_t)s] / sum;
                const double* vr = kv_v.data() + (size_t)s * kvl +
                                   (size_t)kh2 * hd;
                for (int64_t i = 0; i < hd; ++i) ao[i] += p * vr[i];
            }
        }
        std::vector<double> proj((size_t)H);
        matvec(wo->data, attn.data(), proj.data(), H, Hq);
        std::vector<double> xres((size_t)H);
        for (int64_t i = 0; i < H; ++i) xres[(size_t)i] = z[(size_t)i] + proj[(size_t)i];
        std::vector<double> n2((size_t)H);
        rmsnorm(xres.data(), norm2->data, n2.data(), H, c->rms_norm_eps);
        const bool gelu = c->hidden_act == "gelu" ||
                          c->hidden_act == "geglu" ||
                          c->hidden_act == "gelu_tanh";
        std::vector<double> fa((size_t)c->intermediate_size),
                            fb((size_t)c->intermediate_size),
                            fh((size_t)c->intermediate_size);
        matvec(w1->data, n2.data(), fa.data(), c->intermediate_size, H);
        matvec(w3->data, n2.data(), fb.data(), c->intermediate_size, H);
        for (size_t i = 0; i < fh.size(); ++i)
            fh[i] = gate_act(fa[i], gelu) * fb[i];
        matvec(w2->data, fh.data(), proj.data(), H, c->intermediate_size);
        for (int64_t i = 0; i < H; ++i) xres[(size_t)i] += proj[(size_t)i];
        std::vector<double> out((size_t)H);
        rmsnorm(xres.data(), norm_out->data, out.data(), H,
                c->rms_norm_eps);
        std::vector<double> lg((size_t)c->vocab_size);
        matvec(lm_head->data, out.data(), lg.data(), c->vocab_size, H);
        int64_t best = 0;
        for (int64_t i = 1; i < c->vocab_size; ++i)
            if (lg[(size_t)i] > lg[(size_t)best]) best = i;
        return best;
    }
};

}  // namespace

int mode_mtp_draft_probe(const Args& a) {
    std::string bundle = a.get("bundle");
    std::string prompt = a.get("prompt");
    if (bundle.empty() || prompt.empty())
        fail("SPEC_ARGS_MISSING:bundle,prompt");
    int64_t max_new = a.has("max-new")
                          ? std::stoll(a.get("max-new")) : 24;
    if (max_new < 1 || max_new > 256) fail("SPEC_ARGS_RANGE:max-new");

    NativeInferenceEngine engine;
    try {
        engine.load(bundle);
    } catch (const std::exception& e) {
        fail(std::string("CAPABILITY_ENGINE_LOAD_FAILED:") + e.what());
    }
    MtpDraft mtp;
    mtp.b = engine.bundle();
    mtp.c = &mtp.b->config();
    const auto& c = *mtp.c;
    const char* missing = mtp.bind_fail();
    if (missing) fail(std::string("MTP_HEAD_MISSING:") + missing);
    const char* mm = mtp.mismatch();
    if (mm) fail(std::string("MTP_BUNDLE_MISMATCH:") + mm);

    const int64_t H = c.hidden_size, V = c.vocab_size;
    std::vector<int64_t> ids = engine.encode(prompt);
    if (ids.size() < 2) fail("SPEC_PROMPT_TOO_SHORT");
    if ((int64_t)ids.size() + max_new > c.max_position_embeddings)
        fail("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");

    auto embed_row = [&](int64_t id) -> const double* {
        return mtp.embed->data + (size_t)id * H;
    };
    auto lm_logits = [&](const double* hrow) -> std::vector<double> {
        std::vector<double> lg((size_t)V);
        MtpDraft::matvec(mtp.lm_head->data, hrow, lg.data(), V, H);
        return lg;
    };
    auto argmax = [](const std::vector<double>& v) {
        int64_t best = 0;
        for (size_t i = 1; i < v.size(); ++i)
            if (v[i] > v[(size_t)best]) best = (int64_t)i;
        return best;
    };

    // Prefill: trunk hidden for every prompt position, then MTP K/V for
    // positions 0..n-2 (their e_{j+1} are known prompt tokens).
    std::vector<double> hid = engine.forward_all_hidden(ids);
    for (size_t j = 0; j + 1 < ids.size(); ++j) {
        std::vector<double> z =
            mtp.z_of(hid.data() + j * H, embed_row(ids[j + 1]));
        mtp.kv_append(z, (int64_t)j);
    }

    int64_t proposed = 0, accepted = 0, emitted = 0;
    std::vector<int> accept_log;
    std::string turn_tail;
    while (emitted < max_new) {
        const int64_t t = (int64_t)ids.size() - 1;
        const double* h_t = hid.data() + (size_t)t * H;
        std::vector<double> lg = lm_logits(h_t);
        const int64_t tok = argmax(lg);
        // Draft token t+2 from (h_t, e_{t+1}): kv_append then full block.
        std::vector<double> z = mtp.z_of(h_t, embed_row(tok));
        mtp.kv_append(z, t);
        const int64_t draft_tok = mtp.draft(z, t);
        ids.push_back(tok);
        turn_tail += engine.decode({tok}, false);
        if (turn_tail.size() > 64)
            turn_tail.erase(0, turn_tail.size() - 64);
        ++emitted;
        if (tok == c.eos_token_id || emitted >= max_new ||
            (turn_tail.size() >= 7 &&
             turn_tail.compare(turn_tail.size() - 7, 7, "<|eot|>") == 0))
            break;
        // Verify: trunk argmax for position t+2.
        hid = engine.forward_all_hidden(ids);
        const int64_t t2 = (int64_t)ids.size() - 1;
        const int64_t verify =
            argmax(lm_logits(hid.data() + (size_t)t2 * H));
        ++proposed;
        accept_log.push_back(verify == draft_tok ? 1 : 0);
        if (verify == draft_tok) ++accepted;
    }
    engine.unload();

    const double rate =
        proposed ? (double)accepted / (double)proposed : 0.0;
    std::ostringstream log_arr;
    log_arr << '[';
    for (size_t i = 0; i < accept_log.size(); ++i) {
        if (i) log_arr << ',';
        log_arr << accept_log[i];
    }
    log_arr << ']';
    std::printf(
        "{\"ok\":true,\"mode\":\"mtp-draft-probe\","
        "\"format\":\"star-mtp-draft-probe/v1\","
        "\"mtp_family\":\"%s\","
        "\"draft_length\":1,"
        "\"verify_rule\":\"greedy_argmax\","
        "\"output_parity\":\"guaranteed_by_verification\","
        "\"proposed\":%lld,\"accepted\":%lld,"
        "\"acceptance_rate\":%.6f,"
        "\"est_tokens_per_forward_bound\":%.6f,"
        "\"mtp_kv_positions\":%lld,"
        "\"emitted_tokens\":%lld,"
        "\"accept_log\":%s,"
        "\"speculative_decoder\":\"INFRASTRUCTURE_EVIDENCE ??engine-side "
        "NativeMtpDrafter dispatch is not bound; SPECULATIVE_"
        "DECODER_DISABLED remains in effect for production\","
        "\"speedup\":null}\n",
        mtp.bound_family ? mtp.bound_family : "none",
        (long long)proposed, (long long)accepted, rate,
        1.0 + rate, (long long)mtp.positions, (long long)emitted,
        log_arr.str().c_str());
    return 0;
}

// ------------------------------------------------------------------ serve --
// Long-lived stdin/stdout JSON-lines inference worker. The C# tool host
// owns the loopback HTTP surface + descriptor; this mode keeps the
// engine resident and answers one request object per line.
//
//   xc_modeltool serve --bundle <dir>
//
// In : {"op":"status"|"load"|"infer"|"unload"|"quit", ...}
// Out: one JSON object per request line (flushed); EOF exits 0.
// infer: {"prompt": verbatim} or {"messages":[{role,content}...]} (chat
//        template applied here), plus optional sampling fields
//        max_new_tokens/temperature/top_k/top_p/repetition_penalty/
//        do_sample/seed.

const int64_t kServeLineCap = 8 * 1024 * 1024;

// Render the SFT chat template: <|user|>\n{c}\n<|eot|>\n<|assistant|>\n
// per turn; "system" content folds into the user lane (the bundle was
// trained on user/assistant only). A trailing assistant turn closes
// with <|eot|> and a fresh user header so generation always lands on an
// open assistant slot.
std::string serve_render_chat(const JsonValue& messages) {
    std::string p;
    bool open_assistant = false;
    for (const auto& m : messages.array) {
        std::string role = jget_str(m, "role");
        std::string content = py_strip(jget_str(m, "content"));
        if (content.empty()) continue;
        if (role == "user" || role == "system") {
            if (open_assistant) p += "<|eot|>\n";
            p += "<|user|>\n" + content + "\n<|eot|>\n<|assistant|>\n";
            open_assistant = true;
        } else if (role == "assistant" && open_assistant) {
            p += content + "\n<|eot|>\n";
            open_assistant = false;
        }
    }
    if (!open_assistant) {
        // Conversation ended on an assistant turn (or was empty): open a
        // fresh turn so generation has a well-formed slot.
        p += "<|user|>\n<|eot|>\n<|assistant|>\n";
    }
    return p;
}

// Governed GPU-admission probe: reports CUDA capability + free VRAM
// without loading the engine. The stub (non-CUDA build) reports
// available=0 ??capability lives in the CUDA TU, the decision above.
extern "C" int xcuda_probe(long long* free_bytes, long long* total_bytes,
                           int* cc_major, int* cc_minor);

int mode_probe_cuda() {
    long long fb = 0, tb = 0;
    int ccm = 0, ccn = 0;
    const int ok = xcuda_probe(&fb, &tb, &ccm, &ccn);
    std::printf(
        "{\"ok\":true,\"cuda\":{\"available\":%s,"
        "\"vram_free_mb\":%lld,\"vram_total_mb\":%lld,"
        "\"cc_major\":%d,\"cc_minor\":%d}}\n",
        ok ? "true" : "false", fb / (1024 * 1024), tb / (1024 * 1024),
        ccm, ccn);
    return 0;
}

double serve_num(const JsonValue& o, const char* k, double d) {
    const JsonValue* v = o.get(k);
    return (v && v->type == JsonValue::Type::Number) ? v->number : d;
}

bool serve_bool(const JsonValue& o, const char* k, bool d) {
    const JsonValue* v = o.get(k);
    return (v && v->type == JsonValue::Type::Bool) ? v->boolean : d;
}

// Runtime-capability integration headers: shared helpers first
// (xcm_runtime.h), probe modes second (xcm_integration.h consumes
// them) ??both serve ops below and main() dispatch use these.
#include "xcm_runtime.h"
#include "xcm_integration.h"

int mode_serve(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SERVE_ARGS_MISSING");

    // Unbuffered line protocol: every response must reach the host even
    // while a later request is still generating.
    std::setvbuf(stdout, nullptr, _IONBF, 0);

    NativeInferenceEngine engine;
    std::string ckpt_sha = manifest_field(bundle, "checkpoint_sha256");
    std::string model_version =
        ckpt_sha.size() > 16 ? ckpt_sha.substr(0, 16) : ckpt_sha;

    auto emit = [](const std::string& line) {
        std::fputs(line.c_str(), stdout);
        std::fputc('\n', stdout);
        std::fflush(stdout);
    };
    auto err_obj = [&](const std::string& code) {
        emit(std::string("{\"ok\":false,\"error\":\"") +
             gptbridge::jsonlite::json_escape(code) + "\"}");
    };

    std::fprintf(stderr, "[xc_modeltool serve] bundle=%s\n", bundle.c_str());

    std::string line;
    line.reserve(4096);
    while (true) {
        // Bounded line read ??a peer writing past the cap is a protocol
        // violation, not a reason to grow memory without bound.
        line.clear();
        int ch;
        while ((ch = std::fgetc(stdin)) != EOF && ch != '\n') {
            if ((int64_t)line.size() >= kServeLineCap) {
                err_obj("SERVE_REQUEST_TOO_LARGE");
                // drain to the line boundary so framing stays aligned
                while (ch != EOF && ch != '\n') ch = std::fgetc(stdin);
                line.clear();
                break;
            }
            line += (char)ch;
        }
        if (ch == EOF && line.empty()) break;  // stdin closed
        if (line.empty()) continue;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;

        JsonValue req;
        try {
            JsonParser p(line);
            req = p.parse();
        } catch (...) {
            err_obj("SERVE_REQUEST_INVALID_JSON");
            continue;
        }
        if (req.type != JsonValue::Type::Object) {
            err_obj("SERVE_REQUEST_NOT_OBJECT");
            continue;
        }
        std::string op = jget_str(req, "op");
        try {
            if (op == "quit") {
                emit("{\"ok\":true,\"quitting\":true}");
                break;
            }
            if (op == "status") {
                std::ostringstream o;
                o << "{\"ok\":true,\"service\":\"xc-model-service\","
                  << "\"decoder\":\"native-cpp\",\"cpp_runtime\":true,"
                  << "\"loaded\":" << (engine.loaded() ? "true" : "false")
                  << ",\"bundle_dir\":\""
                  << gptbridge::jsonlite::json_escape(bundle) << "\""
                  << ",\"model_id\":\"xingcheng-native-transformer\""
                  << ",\"model_version\":\""
                  << gptbridge::jsonlite::json_escape(model_version) << "\""
                  << ",\"pid\":" << (long long)GetCurrentProcessId();
                if (engine.loaded()) {
                    o << ",\"engine\":" << engine.describe()
                      << ",\"memory_bytes\":" << engine.memory_bytes()
                      << ",\"kv_memory_bytes\":" << engine.kv_memory_bytes()
                      << ",\"cuda_active\":"
                      << (engine.cuda_active() ? "true" : "false");
                }
                o << '}';
                emit(o.str());
                continue;
            }
            if (op == "load") {
                if (!engine.loaded()) {
                    auto t0 = std::chrono::steady_clock::now();
                    engine.load(bundle);
                    double ms = std::chrono::duration<double>(
                        std::chrono::steady_clock::now() - t0).count() * 1000.0;
                    std::ostringstream o;
                    o << "{\"ok\":true,\"loaded\":true,\"load_ms\":" << ms
                      << '}';
                    emit(o.str());
                } else {
                    emit("{\"ok\":true,\"loaded\":true,\"already\":true}");
                }
                continue;
            }
            if (op == "unload") {
                if (engine.loaded()) {
                    engine.unload();
                    emit("{\"ok\":true,\"released\":[\"engine\"],"
                         "\"loaded\":false}");
                } else {
                    emit("{\"ok\":true,\"released\":[],\"loaded\":false}");
                }
                continue;
            }
            if (op == "infer") {
                std::string prompt = jget_str(req, "prompt");
                const JsonValue* messages = req.get("messages");
                if (prompt.empty() && messages &&
                    messages->type == JsonValue::Type::Array) {
                    prompt = serve_render_chat(*messages);
                }
                if (prompt.empty()) {
                    err_obj("SERVE_INFER_PROMPT_REQUIRED");
                    continue;
                }
                if (!engine.loaded()) engine.load(bundle);
                SamplingConfig sc;
                // 蝳?4 sampling profile presets (creative mode surface):
                // a named profile seeds the knobs; explicit fields
                // always win. Profiles are style-only ??safety/tool/
                // data authority never rides on them.
                std::string profile = jget_str(req, "sampling_profile");
                double p_temp = 1.0, p_topp = 1.0, p_rep = 1.0;
                if (profile == "precise") {
                    p_temp = 0.2; p_topp = 0.9; p_rep = 1.05;
                } else if (profile == "balanced") {
                    p_temp = 0.7; p_topp = 0.95; p_rep = 1.1;
                } else if (profile == "creative") {
                    p_temp = 0.9; p_topp = 0.95; p_rep = 1.1;
                } else if (profile == "roleplay") {
                    p_temp = 0.85; p_topp = 0.95; p_rep = 1.1;
                } else if (profile == "story") {
                    p_temp = 0.9; p_topp = 0.95; p_rep = 1.15;
                } else if (profile == "brainstorm") {
                    p_temp = 1.0; p_topp = 0.99; p_rep = 1.1;
                } else if (!profile.empty() && profile != "neutral") {
                    err_obj("SAMPLING_PROFILE_INVALID");
                    continue;
                }
                sc.do_sample = serve_bool(req, "do_sample",
                                          !profile.empty());
                sc.temperature = serve_num(req, "temperature", p_temp);
                sc.top_k = (int64_t)serve_num(req, "top_k", 0);
                sc.top_p = serve_num(req, "top_p", p_topp);
                sc.repetition_penalty =
                    serve_num(req, "repetition_penalty", p_rep);
                sc.seed = (uint64_t)serve_num(req, "seed", 0);
                int64_t max_new = (int64_t)serve_num(
                    req, "max_new_tokens", 192);
                if (max_new <= 0) max_new = 1;
                if (max_new > 2048) max_new = 2048;

                // 蝳?4 persona/context envelope: bounded structured
                // state (persona fields + narrative facts + factuality
                // marker) materialised once as prompt prefix ??never a
                // raw transcript replay. FICTIONAL mode tags the
                // context so generated content can be tracked as
                // fictional by the C# claim/memory layer.
                {
                    std::string persona = jget_str(req, "persona");
                    std::string narrative = jget_str(req, "narrative");
                    std::string factuality =
                        jget_str(req, "factuality");
                    if (!persona.empty() || !narrative.empty() ||
                        !factuality.empty()) {
                        if (!factuality.empty() &&
                            factuality != "FACTUAL_STRICT" &&
                            factuality != "GROUNDED" &&
                            factuality != "GENERAL" &&
                            factuality != "FICTIONAL") {
                            err_obj("FACTUALITY_MODE_INVALID");
                            continue;
                        }
                        std::string env =
                            "<|context_envelope|>{\"factuality\":\"" +
                            (factuality.empty() ? "GENERAL" : factuality)
                            + "\"";
                        if (factuality == "FICTIONAL")
                            env += ",\"fictional_context\":true";
                        if (!persona.empty())
                            env += ",\"persona\":" +
                                   gptbridge::jsonlite::json_escape(
                                       persona);
                        if (!narrative.empty())
                            env += ",\"narrative_state\":" +
                                   gptbridge::jsonlite::json_escape(
                                       narrative);
                        env += "}<|end_context_envelope|>\n";
                        prompt = env + prompt;
                    }
                }

                // 蝳?2 prefix scope isolation: callers pass a scope id
                // (typically the RAG manifest hash); entries from other
                // scopes are never served to this request.
                std::string pscope = jget_str(req, "prefix_scope");
                if (!pscope.empty()) engine.set_prefix_scope(pscope);

                std::vector<int64_t> pids = engine.encode(prompt, true, false);
                // 蝳?6 opt-in two-level MoE trace: per-request router
                // evidence, record-and-analyse only.
                const bool want_trace =
                    serve_bool(req, "router_trace", false);
                engine.set_router_trace(want_trace);
                auto t0 = std::chrono::steady_clock::now();
                std::vector<int64_t> out =
                    engine.generate(pids, max_new, sc);
                double elapsed = std::chrono::duration<double>(
                    std::chrono::steady_clock::now() - t0).count();
                if (out.size() > pids.size() &&
                    std::equal(pids.begin(), pids.end(), out.begin())) {
                    out.erase(out.begin(),
                              out.begin() + (ptrdiff_t)pids.size());
                }
                std::string text = engine.decode(out, true);
                size_t eot = text.find("<|eot|>");
                if (eot != std::string::npos) text.erase(eot);

                // Surface the model-native tool call: a trained-in
                // <tool_call>{json}</tool_call> block is split per
                // star-inference-output/v1 ??cleaned text stays in
                // "text", the parsed call JSON rides in "tool_call".
                // A malformed/unclosed call degrades to a
                // tool_call_error field instead of failing the
                // inference (the generation itself is still valid).
                std::string tool_call_json;
                std::string tool_call_error;
                if (text.find("<tool_call>") != std::string::npos) {
                    try {
                        JsonValue parsed = JsonParser(
                            xingcheng::inference::parse_generated_output(
                                text, 0)).parse();
                        if (const JsonValue* t = parsed.get("text");
                            t && t->type == JsonValue::Type::String)
                            text = t->string;
                        if (const JsonValue* tc = parsed.get("tool_call");
                            tc && tc->type != JsonValue::Type::Null)
                            tool_call_json =
                                gptbridge::jsonlite::json_serialize(*tc);
                    } catch (const std::exception& parseEx) {
                        tool_call_error = parseEx.what();
                    }
                }

                std::ostringstream o;
                o << "{\"ok\":true,\"text\":\""
                  << gptbridge::jsonlite::json_escape(text) << "\"";
                o << ",\"tool_call\":"
                  << (tool_call_json.empty() ? "null" : tool_call_json);
                if (!tool_call_error.empty())
                    o << ",\"tool_call_error\":\""
                      << gptbridge::jsonlite::json_escape(tool_call_error)
                      << "\"";
                o << ",\"token_ids\":[";
                for (size_t i = 0; i < out.size(); ++i) {
                    if (i) o << ',';
                    o << out[i];
                }
                o << ']';
                if (want_trace) {
                    // router_layers[] ??the star-capability-trace/v1
                    // inner layer: unique selected neural experts per
                    // MoE layer plus shared-expert usage.
                    o << ",\"router_layers\":[";
                    bool first_l = true;
                    for (const auto& tr : engine.router_trace()) {
                        if (!first_l) o << ',';
                        first_l = false;
                        std::vector<int64_t> uniq(tr.expert_ids);
                        std::sort(uniq.begin(), uniq.end());
                        uniq.erase(
                            std::unique(uniq.begin(), uniq.end()),
                            uniq.end());
                        o << "{\"layer_id\":" << tr.layer_id
                          << ",\"router_type\":\"" << tr.router_type
                          << "\",\"selected_neural_experts\":[";
                        for (size_t ei = 0; ei < uniq.size(); ++ei)
                            o << (ei ? "," : "") << uniq[ei];
                        o << "],\"shared_expert_used\":"
                          << (tr.shared_experts > 0 ? "true" : "false")
                          << ",\"shared_expert_gated\":"
                          << (tr.shared_expert_gated ? "true" : "false")
                          << ",\"top_k\":" << tr.top_k
                          << ",\"token_count\":" << tr.token_count
                          << '}';
                    }
                    o << ']';
                    engine.set_router_trace(false);
                }
                // 蝳?4 claim-boundary metadata: opt-in sentence spans
                // the C# claim-extraction layer consumes verbatim ??
                // the engine marks boundaries, it never judges truth.
                if (serve_bool(req, "mark_claims", false)) {
                    o << ",\"claim_spans\":[";
                    bool first_span = true;
                    size_t begin = 0;
                    for (size_t i = 0; i < text.size(); ++i) {
                        unsigned char ch = (unsigned char)text[i];
                        bool boundary =
                            ch == '.' || ch == '!' || ch == '?' ||
                            ch == ';' || ch == '\n' ||
                            (ch == 0xE3 && i + 2 < text.size() &&
                             (unsigned char)text[i + 1] == 0x80 &&
                             (unsigned char)text[i + 2] == 0x82) ||  // ??
                            (ch == 0xEF && i + 2 < text.size() &&
                             (unsigned char)text[i + 1] == 0xBC &&
                             ((unsigned char)text[i + 2] == 0x81 ||
                              (unsigned char)text[i + 2] == 0x9F ||
                              (unsigned char)text[i + 2] == 0x9B)); // ?????
                        bool last = i + 1 == text.size();
                        if (!boundary && !last) continue;
                        size_t end = i + 1;
                        if (boundary && (ch == 0xE3 || ch == 0xEF))
                            end = i + 3;  // include the 3-byte punct
                        if (end > begin + 1) {
                            if (!first_span) o << ',';
                            first_span = false;
                            o << "{\"begin\":" << begin
                              << ",\"end\":" << end << '}';
                        }
                        begin = end;
                        if (ch == 0xE3 || ch == 0xEF) i += 2;
                    }
                    o << ']';
                }
                o << ",\"generated_tokens\":" << (int64_t)out.size()
                  << ",\"latency_ms\":" << elapsed * 1000.0
                  << ",\"model_id\":\"xingcheng-native-transformer\""
                  << ",\"model_version\":\""
                  << gptbridge::jsonlite::json_escape(model_version) << "\""
                  << ",\"decoder\":\"native-cpp\",\"cpp_runtime\":true}";
                emit(o.str());
                continue;
            }
            if (op == "think") {
                // Native Thinking: latent continuous-thought steps then
                // parallel hypothesis branches ranked by model confidence
                // (latent CoVe) ??no textual chain-of-thought is produced.
                std::string prompt = jget_str(req, "prompt");
                const JsonValue* messages = req.get("messages");
                if (prompt.empty() && messages &&
                    messages->type == JsonValue::Type::Array) {
                    prompt = serve_render_chat(*messages);
                }
                if (prompt.empty()) {
                    err_obj("SERVE_INFER_PROMPT_REQUIRED");
                    continue;
                }
                if (!engine.loaded()) engine.load(bundle);
                SamplingConfig sc;
                sc.do_sample = serve_bool(req, "do_sample", true);
                sc.temperature = serve_num(req, "temperature", 1.0);
                sc.top_k = (int64_t)serve_num(req, "top_k", 0);
                sc.top_p = serve_num(req, "top_p", 1.0);
                sc.repetition_penalty =
                    serve_num(req, "repetition_penalty", 1.0);
                sc.seed = (uint64_t)serve_num(req, "seed", 0);
                int64_t max_new = (int64_t)serve_num(
                    req, "max_new_tokens", 128);
                if (max_new <= 0) max_new = 1;
                if (max_new > 2048) max_new = 2048;
                int64_t think_steps = (int64_t)serve_num(
                    req, "think_steps", 4);
                int64_t branches = (int64_t)serve_num(
                    req, "branches", 4);

                std::vector<int64_t> pids =
                    engine.encode(prompt, true, false);
                auto t0 = std::chrono::steady_clock::now();
                auto res = engine.generate_thinking(
                    pids, think_steps, branches, max_new, sc);
                double elapsed = std::chrono::duration<double>(
                    std::chrono::steady_clock::now() - t0).count();
                std::string text = engine.decode(res.answer_ids, true);
                size_t eot = text.find("<|eot|>");
                if (eot != std::string::npos) text.erase(eot);

                std::ostringstream o;
                o << "{\"ok\":true,\"text\":\""
                  << gptbridge::jsonlite::json_escape(text) << "\"";
                o << ",\"token_ids\":[";
                for (size_t i = 0; i < res.answer_ids.size(); ++i) {
                    if (i) o << ',';
                    o << res.answer_ids[i];
                }
                o << ']';
                o << ",\"generated_tokens\":"
                  << (int64_t)res.answer_ids.size()
                  << ",\"thinking\":{\"think_steps\":" << res.think_steps
                  << ",\"branches\":" << branches
                  << ",\"chosen_branch\":" << res.chosen_branch
                  << ",\"verify\":\"confidence\""
                  << ",\"branch_scores\":[";
                for (size_t i = 0; i < res.branch_scores.size(); ++i) {
                    if (i) o << ',';
                    o << res.branch_scores[i];
                }
                o << "],\"branch_lengths\":[";
                for (size_t i = 0; i < res.branch_ids.size(); ++i) {
                    if (i) o << ',';
                    o << (int64_t)res.branch_ids[i].size();
                }
                o << "]}";
                o << ",\"latency_ms\":" << elapsed * 1000.0
                  << ",\"model_id\":\"xingcheng-native-transformer\""
                  << ",\"model_version\":\""
                  << gptbridge::jsonlite::json_escape(model_version) << "\""
                  << ",\"decoder\":\"native-cpp\",\"cpp_runtime\":true}";
                emit(o.str());
                continue;
            }
            if (op == "memplan") {
                // InferenceMemoryPlanner report: typed budget breakdown
                // + prefill/decode high-water marks.
                if (!engine.loaded()) engine.load(bundle);
                const auto r = engine.memory_report();
                std::ostringstream o;
                o << "{\"ok\":true,\"format\":"
                     "\"star-inference-memory-report/v1\""
                  << ",\"weight_bytes\":" << r.weight_bytes
                  << ",\"kv_bytes\":" << r.kv_bytes
                  << ",\"prefix_cache_bytes\":" << r.prefix_cache_bytes
                  << ",\"recurrent_state_bytes\":"
                  << r.recurrent_state_bytes
                  << ",\"vision_bytes\":" << r.vision_bytes
                  << ",\"workspace_bytes\":" << r.workspace_bytes
                  << ",\"prefill_peak_bytes\":" << r.prefill_peak_bytes
                  << ",\"decode_peak_bytes\":" << r.decode_peak_bytes
                  << ",\"kv_limit_bytes\":"
                  << "null,\"note\":\"budgets via "
                     "set_kv_memory_limit/prefix limits\"}";
                emit(o.str());
                continue;
            }
            if (op == "depth") {
                // Depth telemetry (Kimi-K3 lesson): per-layer residual
                // RMS + per-module norms + recurrent-state norms.
                if (!engine.loaded()) engine.load(bundle);
                std::string prompt = jget_str(req, "prompt");
                if (prompt.empty()) prompt = "1 2 3";
                std::vector<int64_t> ids =
                    engine.encode(prompt, true, false);
                std::vector<double> layer =
                    engine.layer_metrics(ids);
                std::vector<double> mod =
                    engine.module_metrics(ids);
                std::vector<double> rec =
                    engine.recurrent_state_norms();
                std::ostringstream o;
                o << "{\"ok\":true,\"format\":"
                     "\"star-depth-telemetry/v1\""
                  << ",\"layer_representation_norm\":[";
                for (size_t i = 0; i < layer.size(); ++i)
                    o << (i ? "," : "") << layer[i];
                o << "],\"module_output_norm\":[";
                for (size_t i = 0; i < mod.size(); ++i)
                    o << (i ? "," : "") << mod[i];
                o << "],\"recurrent_state_norm\":[";
                for (size_t i = 0; i < rec.size(); ++i)
                    o << (i ? "," : "") << rec[i];
                o << "]}";
                emit(o.str());
                continue;
            }
            if (op == "moe-analyze") {
                // MoERoutingAnalyzer: quantile routing analysis +
                // ROUTER_* diagnoses over a prompt forward.
                if (!engine.loaded()) engine.load(bundle);
                std::string prompt = jget_str(req, "prompt");
                if (prompt.empty()) prompt = "1 2 3";
                engine.set_router_trace(true);
                engine.logits(engine.encode(prompt, true, false));
                std::string rep =
                    xcm_moe_analyze_json(engine.router_trace());
                engine.set_router_trace(false);
                emit(std::string("{\"ok\":true,") +
                     "\"analysis\":" + rep + "}");
                continue;
            }
            if (op == "fim") {
                // star-fim/v1 runtime envelope ??control tokens are
                // literal text; tokenizer assets unchanged.
                std::string prefix = jget_str(req, "prefix");
                std::string suffix = jget_str(req, "suffix");
                if (!engine.loaded()) engine.load(bundle);
                SamplingConfig sc;
                sc.do_sample = serve_bool(req, "do_sample", false);
                sc.seed = (uint64_t)serve_num(req, "seed", 0);
                int64_t max_new = (int64_t)serve_num(
                    req, "max_new_tokens", 96);
                std::string enveloped =
                    xcm_fim_envelope(prefix, suffix);
                std::vector<int64_t> ids =
                    engine.encode(enveloped, true, false);
                std::vector<int64_t> out =
                    engine.generate(ids, max_new, sc);
                if (out.size() > ids.size() &&
                    std::equal(ids.begin(), ids.end(), out.begin()))
                    out.erase(out.begin(), out.begin() +
                              (ptrdiff_t)ids.size());
                std::string text = engine.decode(out, true);
                size_t eot = text.find("<|eot|>");
                if (eot != std::string::npos) text.erase(eot);
                std::ostringstream o;
                o << "{\"ok\":true,\"format\":\"star-fim/v1\","
                  << "\"envelope\":\"runtime-literal\","
                  << "\"insertion\":\""
                  << gptbridge::jsonlite::json_escape(text)
                  << "\",\"generated_tokens\":"
                  << (int64_t)out.size() << "}";
                emit(o.str());
                continue;
            }
            if (op == "state-save") {
                // DeltaStateSnapshot -> base64-free: writes raw blob to
                // the given path; reports sha256 + bytes.
                if (!engine.loaded()) engine.load(bundle);
                std::string path = jget_str(req, "path");
                std::string gen = jget_str(req, "generation");
                if (path.empty() || gen.empty()) {
                    err_obj("STATE_SNAPSHOT_ARGS_MISSING");
                    continue;
                }
                std::string blob = engine.snapshot_delta_state(gen);
                std::ofstream f(path, std::ios::binary | std::ios::trunc);
                f.write(blob.data(), (std::streamsize)blob.size());
                std::ostringstream o;
                o << "{\"ok\":true,\"format\":\"star-delta-state/v1\","
                  << "\"path\":\""
                  << gptbridge::jsonlite::json_escape(path) << "\","
                  << "\"bytes\":" << (int64_t)blob.size()
                  << ",\"sha256\":\""
                  << NativeInferenceEngine::delta_state_sha256(blob)
                  << "\"}";
                emit(o.str());
                continue;
            }
            if (op == "state-restore") {
                if (!engine.loaded()) engine.load(bundle);
                std::string path = jget_str(req, "path");
                std::string gen = jget_str(req, "generation");
                if (path.empty() || gen.empty()) {
                    err_obj("STATE_SNAPSHOT_ARGS_MISSING");
                    continue;
                }
                std::ifstream f(path, std::ios::binary | std::ios::ate);
                if (!f) { err_obj("SEQUENCE_STATE_INVALID:open");
                          continue; }
                std::string blob((size_t)f.tellg(), '\0');
                f.seekg(0);
                f.read(blob.data(), (std::streamsize)blob.size());
                engine.restore_delta_state(blob, gen);
                emit("{\"ok\":true,\"restored\":true}");
                continue;
            }
            // -------- inference efficiency plane ops ----------------
            if (op == "prefix-scope") {
                // 蝳?2: bind subsequent requests to an isolation scope.
                engine.set_prefix_scope(jget_str(req, "scope"));
                std::ostringstream o;
                o << "{\"ok\":true,\"prefix_scope\":\""
                  << gptbridge::jsonlite::json_escape(
                         engine.prefix_scope())
                  << "\"}";
                emit(o.str());
                continue;
            }
            if (op == "prefix-invalidate") {
                // 蝳?9: drop a scope's prefix entries (document revision
                // / chunk hash / index revision changed).
                int64_t removed = engine.invalidate_prefix_scope(
                    jget_str(req, "scope"));
                std::ostringstream o;
                o << "{\"ok\":true,\"invalidated\":" << removed << "}";
                emit(o.str());
                continue;
            }
            if (op == "prefill") {
                // 蝳?5/蝳?6 star-prefill-artifact/v1: run the PREFILL
                // role and write the binary handoff artifact.
                std::string prompt = jget_str(req, "prompt");
                std::string out_path = jget_str(req, "out");
                if (prompt.empty() || out_path.empty()) {
                    err_obj("PREFILL_ARTIFACT_INVALID:args");
                    continue;
                }
                if (!engine.loaded()) engine.load(bundle);
                std::string pscope = jget_str(req, "prefix_scope");
                if (!pscope.empty()) engine.set_prefix_scope(pscope);
                std::vector<int64_t> pids =
                    engine.encode(prompt, true, false);
                auto t0 = std::chrono::steady_clock::now();
                std::string artifact = engine.prefill_artifact(
                    pids, jget_str(req, "request_id"));
                double prefill_ms = std::chrono::duration<double>(
                    std::chrono::steady_clock::now() - t0).count()
                    * 1000.0;
                std::ofstream f(out_path,
                                std::ios::binary | std::ios::trunc);
                if (!f) { err_obj("PREFILL_ARTIFACT_INVALID:out");
                          continue; }
                f.write(artifact.data(),
                        (std::streamsize)artifact.size());
                std::ostringstream o;
                o << "{\"ok\":true,\"artifact\":\""
                  << gptbridge::jsonlite::json_escape(out_path) << "\","
                  << "\"format\":\"star-prefill-artifact/v1\","
                  << "\"bytes\":" << (int64_t)artifact.size()
                  << ",\"tokens\":" << (int64_t)pids.size()
                  << ",\"prefill_ms\":" << prefill_ms << "}";
                emit(o.str());
                continue;
            }
            if (op == "decode-artifact") {
                // 蝳?7: DECODE role ??verify bindings fail-closed,
                // restore state, decode. Prefill never re-runs.
                std::string path = jget_str(req, "artifact");
                if (path.empty()) {
                    err_obj("PREFILL_ARTIFACT_INVALID:args");
                    continue;
                }
                if (!engine.loaded()) engine.load(bundle);
                std::ifstream f(path, std::ios::binary | std::ios::ate);
                if (!f) { err_obj("PREFILL_ARTIFACT_INVALID:open");
                          continue; }
                std::string artifact((size_t)f.tellg(), '\0');
                f.seekg(0);
                f.read(artifact.data(),
                       (std::streamsize)artifact.size());
                int64_t max_new = (int64_t)serve_num(
                    req, "max_new_tokens", 192);
                if (max_new <= 0) max_new = 1;
                if (max_new > 2048) max_new = 2048;
                SamplingConfig sc;
                auto t0 = std::chrono::steady_clock::now();
                std::vector<int64_t> out =
                    engine.generate_from_artifact(
                        artifact, max_new, sc);
                double decode_s = std::chrono::duration<double>(
                    std::chrono::steady_clock::now() - t0).count();
                std::string text = engine.decode(out, true);
                size_t eot = text.find("<|eot|>");
                if (eot != std::string::npos) text.erase(eot);
                std::ostringstream o;
                o << "{\"ok\":true,\"text\":\""
                  << gptbridge::jsonlite::json_escape(text) << "\","
                  << "\"generated_tokens\":" << (int64_t)out.size()
                  << ",\"decode_s\":" << decode_s
                  << ",\"decoder\":\"native-cpp\",\"cpp_runtime\":true"
                  << ",\"role\":\"decode\"}";
                emit(o.str());
                continue;
            }
            err_obj("SERVE_UNKNOWN_OP");
        } catch (const std::exception& e) {
            std::string msg = e.what();
            if (msg.size() > 300) msg.resize(300);
            err_obj(std::string("SERVE_OP_FAILED:") + msg);
        } catch (...) {
            err_obj("SERVE_OP_FAILED");
        }
    }
    return 0;
}

#include "xcm_corpus.h"
#include "xcm_efficiency.h"
// NativeScaleEfficiencyPlane: scale metrics, mapped expert store,
// prefetch, delta-state precision, low-resource + candidate sims.
#include "xcm_scale.h"
// Laya/MiMo capability plane native lanes: System-1 decision head +
// MTP drafter benchmarks (contract plane lives in C#).
#include "xcm_system1.h"
// NativeMemoryCudaPlane: unified CUDA memory manager, pools, arenas,
// pressure ladder, telemetry.
#include "xcm_memplane.h"
// NativeSiliconEfficiencyPlane: runtime owner, artifact dedup, CPU
// topology/profile probes, silicon routing, expert granularity,
// parameter efficiency, NPU probe-first discovery.
#include "xcm_silicon.h"
// 蝳?8 BF16 production certification: FP64 CPU oracle vs cuBLAS-fp64
// and the NVRTC bf16 GEMM lane on deterministic shapes.
#include "xcm_bf16cert.h"
// 蝳?1-蝳?5 blockwise quantization certification: per-class precision
// policy (router FP32 / shared BF16 floor / routed aggressive),
// simulated-quant bundle vs fp64 oracle across the 蝳?5 battery.
#include "xcm_quantcert.h"
// Capability contracts (P3-P7): InferenceMemoryPlanner, DeltaStateSnapshot,
// NativeStateRef, VisionBudgetPlan, SpeculativeDrafter, MoERoutingAnalyzer,
// SequenceStateBenchmark, ParameterReuseProbe, PrecisionParity.
#include "xcm_capability.h"
// Runtime-gate modes converged from the devin lane (see header doc).
#include "xcm_rtgates.h"

}  // namespace

// ModelToolModeRegistry ??the governed dispatch table. Every mode
// belongs to exactly one category; unregistered names resolve to
// MODE_NOT_REGISTERED rather than a free-form error. New capability
// should prefer a subcommand/option on an existing mode over a new
// registry row (mode count is not a success metric).
struct ModeEntry {
    const char* name;
    const char* category;  // MODEL|TRAINING|STATE|CACHE|PRECISION|
                           // CUDA|EXPERT|SCALE|RAG|EVAL|PROVENANCE
    int (*fn)(const Args&);
};

static const ModeEntry kModeRegistry[] = {
    {"tokenize",              "MODEL",      mode_tokenize},
    {"corpus",                "TRAINING",   mode_corpus},
    {"import-bundle",         "MODEL",      mode_import_bundle},
    {"distill-init",          "TRAINING",   mode_distill_init},
    {"export-bundle",         "MODEL",      mode_export_bundle},
    {"eval",                  "EVAL",       mode_eval},
    {"capability",            "EVAL",       mode_capability},
    {"vision-smoke",          "EVAL",       mode_vision_smoke},
    {"cache-smoke",           "CACHE",      mode_cache_smoke},
    {"parity",                "PRECISION",  mode_parity},
    {"precision",             "PRECISION",  mode_precision},
    {"memplan",               "STATE",      mode_memplan},
    {"statebench",            "STATE",      mode_statebench},
    {"spec-probe",            "EVAL",       mode_spec_probe},
    {"vision-budget",         "EVAL",       mode_vision_budget},
    {"context-probe",         "CACHE",      mode_context_probe},
    {"reuse-probe",           "CACHE",      mode_reuse_probe},
    {"serve",                 "MODEL",      mode_serve},
    {"probe-cuda",            "CUDA",
        [](const Args&) -> int { return mode_probe_cuda(); }},
    {"provenance-check",      "PROVENANCE", mode_provenance_check},
    {"depth-probe",           "EVAL",       mode_depth_probe},
    {"moe-analyze",           "EXPERT",     mode_moe_analyze},
    // Native Inference Efficiency Plane
    {"expert-residency",      "EXPERT",     mode_expert_residency},
    {"expert-offload-bench",  "EXPERT",     mode_expert_offload_bench},
    {"hybrid-prefix-smoke",   "CACHE",      mode_hybrid_prefix_smoke},
    {"prefix-invalidation",   "CACHE",      mode_prefix_invalidation},
    {"rag-prefix-bench",      "RAG",        mode_rag_prefix_bench},
    {"pd-pipeline-bench",     "SCALE",      mode_pd_bench},
    {"pd-transfer-smoke",     "SCALE",      mode_pd_transfer_smoke},
    {"delta-prefix-restore",  "CACHE",      mode_delta_prefix_restore},
    {"expert-quant-parity",   "EXPERT",     mode_expert_quant_parity},
    // NativeScaleEfficiencyPlane
    {"scale-metrics",         "SCALE",      mode_scale_metrics},
    {"expert-store-build",    "EXPERT",     mode_expert_store_build},
    {"expert-store-read",     "EXPERT",     mode_expert_store_read},
    {"prefetch-probe",        "EXPERT",     mode_prefetch_probe},
    {"delta-precision-probe", "PRECISION",  mode_delta_precision_probe},
    {"low-resource-sim",      "SCALE",      mode_low_resource_sim},
    {"scale-sim",             "SCALE",      mode_scale_sim},
    {"scale-status",          "SCALE",      mode_scale_status},
    {"future-scale-probe",    "SCALE",      mode_future_scale_probe},
    // Laya/MiMo capability plane ??native fast path.
    {"system1-head",          "EVAL",       mode_system1_head},
    {"mtp-runtime",           "MODEL",      mode_mtp_runtime},
    {"mtp-speedup",           "EVAL",       mode_mtp_speedup},
    {"mtp-precision-parity",  "PRECISION",  mode_mtp_precision_parity},
    {"mtp-draft-probe",       "EVAL",       mode_mtp_draft_probe},
    // NativeMemoryCudaPlane ??unified memory manager probes.
    {"memplane-probe",        "STATE",      mode_memplane_probe},
    {"memplane-telemetry",    "STATE",      mode_memplane_telemetry},
    // NativeSiliconEfficiencyPlane ??measured surfaces.
    {"npu-discovery",         "SCALE",      mode_npu_discovery},
    {"cpu-affinity-probe",    "SCALE",      mode_cpu_affinity_probe},
    {"cpu-bf16-bench",        "PRECISION",  mode_cpu_bf16_bench},
    {"system-reuse-probe",    "SCALE",      mode_system_reuse_probe},
    {"single-runtime-owner",  "PROVENANCE", mode_single_runtime_owner},
    {"artifact-dedup",        "PROVENANCE", mode_artifact_dedup},
    {"shared-routed-isolation","EXPERT",    mode_shared_routed_isolation},
    {"expert-granularity-probe","EXPERT",   mode_expert_granularity_probe},
    {"parameter-freeze-probe","TRAINING",   mode_parameter_freeze_probe},
    {"parameter-efficiency-report","SCALE", mode_parameter_efficiency},
    {"silicon-routing-bench", "SCALE",      mode_silicon_routing_bench},
    {"npu-system1-bench",     "EVAL",
        [](const Args& a) { return mode_npu_bench("npu-system1-bench", a); }},
    {"npu-embedding-bench",   "EVAL",
        [](const Args& a) { return mode_npu_bench("npu-embedding-bench", a); }},
    {"npu-prefill-bench",     "EVAL",
        [](const Args& a) { return mode_npu_bench("npu-prefill-bench", a); }},
    {"sparse-optimizer-probe","TRAINING",   mode_parameter_freeze_probe},
    {"npu-ep-enum",           "SCALE",      mode_npu_ep_enum},
    {"npu-duplicate-cost",    "SCALE",      mode_npu_duplicate_cost},
    {"capacity-metrics",      "SCALE",      mode_capacity_metrics},
    // 蝳?8 BF16 production certification (FP64 oracle comparison).
    {"bf16-cert",             "PRECISION",  mode_bf16_cert},
    {"bf16-drift",            "PRECISION",  mode_bf16_drift},
{"decode-graph-parity",   "CUDA",       mode_graph_parity},
    // 蝳?1-蝳?5 blockwise quantization certification (蝳?7 probe name is
    // blockwise-quant-probe; both resolve to the same lane).
    {"quant-cert",            "PRECISION",  mode_quant_cert},
    {"blockwise-quant-probe", "PRECISION",  mode_quant_cert},
    // ---- devin-lane runtime gates (xcm_rtgates.h) ??canonical names.
    {"native-thinking-eval",  "EVAL",      mode_native_thinking_eval},
    {"precision-parity",      "PRECISION", mode_precision_parity},
    {"spec-verify",           "EVAL",      mode_spec_verify},
    {"memory-plan",           "STATE",     mode_memory_plan},
    {"state-snapshot",        "STATE",     mode_state_snapshot},
    {"state-bench",           "STATE",     mode_state_bench},
    {"state-drift",           "STATE",     mode_state_drift},
    {"hw-baseline",           "SCALE",     mode_hw_baseline},
    {"state2-smoke",          "STATE",     mode_state2_smoke},
    {"sched-smoke",           "STATE",     mode_sched_smoke},
    {"router-analyze",        "EXPERT",    mode_router_analyze},
    {"cuda-parity-all",       "CUDA",      mode_cuda_parity_all},
    {"param-reuse-probe",     "SCALE",     mode_param_reuse},
    {"sparse-probe",          "CACHE",     mode_sparse_probe},
    {"kv-gather-probe",       "CACHE",     mode_kv_gather_probe},
    {"hw-caps",               "SCALE",     mode_hw_caps},
    // Checkpoint-format convergence onto the canonical writer
    // (byte-exact tensor table, zeroed MTP-stack block).
    {"ckpt-converge",         "MODEL",     mode_ckpt_converge},
};

static int mode_registry_emit() {
    std::ostringstream o;
    o << "{\"ok\":true,\"format\":\"star-mode-registry/v1\","
      << "\"count\":"
      << (sizeof(kModeRegistry) / sizeof(kModeRegistry[0]))
      << ",\"modes\":[";
    bool first = true;
    for (const auto& e : kModeRegistry) {
        o << (first ? "" : ",") << "{\"name\":\"" << e.name
          << "\",\"category\":\"" << e.category << "\"}";
        first = false;
    }
    o << "]}\n";
    std::printf("%s", o.str().c_str());
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr,
            "xc_modeltool <mode> [args] ??'mode-registry' lists the "
            "governed mode table (star-mode-registry/v1)\n");
        return 2;
    }
    std::string mode = argv[1];
    Args a = parse_args(argc, argv);
    if (mode == "mode-registry") return mode_registry_emit();
    try {
        for (const auto& e : kModeRegistry)
            if (mode == e.name) return e.fn(a);
    } catch (const std::exception& e) {
        std::string msg = e.what();
        std::fprintf(stderr, "xc_modeltool error: %s\n", msg.c_str());
        std::printf("{\"ok\":false,\"error\":\"%s\"}\n",
                    gptbridge::jsonlite::json_escape(msg).c_str());
        return 1;
    } catch (...) {
        std::fprintf(stderr, "xc_modeltool error: unhandled\n");
        std::printf("{\"ok\":false,\"error\":\"UNHANDLED\"}\n");
        return 1;
    }
    std::fprintf(stderr, "MODE_NOT_REGISTERED: %s\n", mode.c_str());
    std::printf("{\"ok\":false,\"error\":\"MODE_NOT_REGISTERED\","
                "\"mode\":\"%s\"}\n",
                gptbridge::jsonlite::json_escape(mode).c_str());
    return 2;
}
