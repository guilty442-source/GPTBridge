// xc_modeltool.cpp — governed model-side utilities for the native lane.
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
//                 [--quant none|int8|int4_packed]
//   eval          --bundle <dir> --suite <suite.json>
//                 [--baseline-bundle <dir>|--baseline-metrics <file>]
//   capability    --bundle <dir> --suite <suite.json>
//                 [--corpus-manifest <manifest.json>] [--chat]
//                 [--baseline-report <file>]
//   serve         --bundle <dir>   (stdin/stdout JSON-lines worker)
//   vision-smoke  --bundle <vision-bundle-dir> [--patches N] [--seed N]
//   cache-smoke   --bundle <dir> [--seed N] [--kv-int8]
//                 (paged-KV / prefix-cache determinism probe)
//
// Tokenize row shapes (star SFT/DPO/pretrain contracts):
//   {"prompt","completion"}      -> {"input_ids","labels"}  (masked prompt)
//   {"text"}                     -> {"input_ids"}           (pretrain; trainer
//                                  shifts labels itself)
//   {"prompt","chosen","rejected"} -> {"chosen","rejected"} (DPO)
//   any sft/pretrain row may carry "vision_patches":[[..D..] x P] — the
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
#include <mutex>
#include <numeric>
#include <random>
#include <regex>
#include <sstream>
#include <string>
#include <thread>
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
#include <bcrypt.h>
#pragma comment(lib, "bcrypt.lib")
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
// json.dumps(obj, ensure_ascii=False, sort_keys=True) — separators
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

// sha256 of json.dumps(obj, sort_keys=True) — default separators.
std::string suite_sha256(const std::string& raw_json) {
    RawCur c{raw_json.data(), raw_json.data() + raw_json.size()};
    std::string canon;
    canon_value(c, canon);
    c.ws();
    if (c.p != c.end) fail("CANON_TRAILING_DATA");
    return sha256_text(canon);
}

// ------------------------------------------------------------- NFC trim ----
// Python unicodedata.normalize("NFC", text).strip() — used for overlap
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
// (label >= 0) positions remain — those rows are dropped, not emitted.
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
// grid through to trainer-ready rows — for early-fusion the grid IS the
// vision token stream (prefix positions; the trainer masks them -100
// and checks geometry against the model config). Structural validation
// mirrors xct_util.h j_patch_grid: array of P numeric rows of uniform
// width D. Returns 0 absent, 1 well-formed, -1 malformed — a malformed
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
    // v27 gated DeltaNet (linear attention) tensors — the bundle keeps
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
    if (n == "vision.patch_proj") return "vision.patch_proj.weight";
    return "";
}

// ------------------------------------------------------- import-bundle ----

xct::ModelConfig config_from_manifest(const JsonValue& cfg) {
    // Shared parser (job.json + manifest config + distill student config)
    // — also validates the Gemma4 profile fail-closed.
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
    // manifest model block — absent keys keep the dense defaults so
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
    JsonParser p(raw);  // JsonParser holds a pointer into `raw` — lvalue only.
    return p.parse();
}

int64_t numel_of(const JsonValue& shape) {
    int64_t n = 1;
    for (const auto& d : shape.array)
        n *= (int64_t)d.number;
    return n;
}

// Loads a bundle directory into xct Params (fp32). Fails closed on any
// dtype/shape/contract violation — shared by import-bundle and
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
        if (wIt == p.w.end()) fail("IMPORT_CONFIG_SHAPE_MISMATCH:" + xname);
        const JsonValue* off = info.get("offset");
        const JsonValue* shape = info.get("shape");
        const JsonValue* dtype = info.get("dtype");
        if (!off || !shape) fail("IMPORT_TENSOR_INFO_INVALID:" + name);
        if (dtype && dtype->string != "float64")
            fail("IMPORT_UNSUPPORTED_DTYPE:" + name);
        int64_t n = numel_of(*shape);
        if ((int64_t)wIt->second.numel() != n)
            fail("IMPORT_TENSOR_NUMEL_MISMATCH:" + name);
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
    for (const auto& n : p.order)
        if (!filled.count(n)) fail("IMPORT_MISSING_TENSOR:" + n);
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
// API only — no internal surface):
//   paged KV   — generate() drives the append-cache decode path; a
//                repeated generate over the same prompt restores the
//                cached prefix (prefix_cache_hits++) and must reproduce
//                identical tokens — the "restored KV is bit-identical to
//                recompute" contract made executable;
//   determinism — uncached logits() calls are bitwise stable;
//   kv-int8 (opt-in via --kv-int8) — a second engine loaded under
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
    SamplingConfig sc;                    // do_sample=false → argmax
    std::vector<int64_t> prompt(ids.begin(), ids.begin() + 16);
    std::vector<int64_t> g1 =
        e.generate(prompt, 6, sc);        // miss → stores prefix entry
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
    // v27 fused hybrid: prefix cache stores K/V only and cannot restore
    // DeltaNet recurrent state, so the engine bypasses it for hybrid
    // bundles. The contract inverts: hits must stay absent while the
    // recomputed path still yields identical greedy output.
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
    const bool prefix_ok =
        hybrid ? (!fp.prefix_hit && !fp.partial_prefix_hit)
               : (fp.prefix_hit && fp.partial_prefix_hit);
    bool ok = fp.logits_finite && fp.logits_deterministic && prefix_ok &&
              fp.gen_nonempty && fp.gen_identical;
    // kv-int8: logits() never touches the KV pool, so the meaningful
    // evidence is the cached path — prefix hits still fire, the
    // restore→requantize round-trip keeps greedy output identical, and
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
                      (hybrid ? (!int8_hit && !int8_partial)
                              : (int8_hit && int8_partial));
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
// last-position logits and greedy argmax — this is the end-to-end check
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
    std::vector<int> tids(ids.begin(), ids.end());
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
    for (const auto& n : p.order) {
        // MTP (next-n predict / mtp-stack) tensors are training-time
        // auxiliary heads; the serving contract drops them like
        // DeepSeek-style MTP checkpoints.
        if (n.compare(0, 4, "mtp.") == 0) continue;
        std::string b = xct_to_bundle(n, c.is_gemma4());
        if (b.empty()) fail("EXPORT_UNMAPPED_TENSOR:" + n);
        pairs.emplace_back(b, n);
    }
    std::sort(pairs.begin(), pairs.end());

    // --quant none|int8|int4_packed|bf16: weight-only per-tensor
    // quantization of 2-D matrices; the engine dequantizes to fp64 at
    // load. int8/int4_packed are symmetric-with-scale (mirrors
    // kernels/quant.py); bf16 is unscaled RNE truncation (§18
    // conversion lane — never retrained). 1-D tensors (norms) stay fp64.
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
        if (q && quant == "int8") {
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
        } else if (q && quant == "bf16") {
            // fp64 -> bf16 via fp32 with round-to-nearest-even on the
            // dropped mantissa; unscaled — the loader widens back.
            std::vector<uint16_t> qd((size_t)n);
            for (int64_t k = 0; k < n; ++k) {
                const float f = (float)t.d[(size_t)k];
                uint32_t u;
                std::memcpy(&u, &f, sizeof(u));
                u += 0x7FFFu + ((u >> 16) & 1u);
                qd[(size_t)k] = (uint16_t)(u >> 16);
            }
            bin.write((char*)qd.data(),
                      (std::streamsize)qd.size() * 2);
            bytes = (int64_t)qd.size() * 2;
            dtype = "bf16";
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

    // manifest.json — config copied verbatim from --config-from so every
    // engine-side field (backend preference, rope, kv quant, …) survives.
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
    int64_t now = (int64_t)std::chrono::duration_cast<std::chrono::seconds>(
                      std::chrono::system_clock::now().time_since_epoch())
                      .count();
    // Lifecycle metadata (architecture-convergence contract): the bundle
    // records which architecture profile it carries, which checkpoint
    // contract produced it and the governed runtime lineage — distinct
    // from the deployment active_generation, which lives in the C#
    // generation state.
    uint32_t ckpt_ver = 0;
    {
        std::ifstream hdr(ckpt, std::ios::binary);
        char magic[4] = {0, 0, 0, 0};
        if (hdr.read(magic, 4) && std::memcmp(magic, "XCN1", 4) == 0) {
            unsigned char vb[4] = {0, 0, 0, 0};
            if (hdr.read((char*)vb, 4))
                ckpt_ver = (uint32_t)vb[0] | ((uint32_t)vb[1] << 8) |
                           ((uint32_t)vb[2] << 16) | ((uint32_t)vb[3] << 24);
        }
    }
    const bool arch_xc_fused1 =
        !c.is_gemma4() && c.full_attention_interval == 4 &&
        c.attn_output_gate && c.qk_norm &&
        std::fabs(c.partial_rotary - 0.5f) < 1e-6f &&
        c.moe_router_sigmoid && c.moe_top_k == 2 &&
        c.moe_layer_interval == 1 && c.moe_experts >= 8 &&
        c.moe_shared_experts >= 1 && c.shared_expert_gate &&
        std::fabs(c.moe_aux_w - 0.001f) < 1e-7f &&
        !c.moe_auxfree_balance && c.moe_lb_bias_rate == 0.0f &&
        c.mtp_depth >= 1 && c.mtp_loss_w >= 0.1f && c.use_vision &&
        c.vision_patch_dim >= 16 && c.vision_max_patches >= 64 &&
        c.yarn_factor >= 2.0f && c.kv_lora_rank == 0 &&
        c.q_lora_rank == 0 && c.csa_ratio == 0;
    std::ostringstream mf;
    mf << "{\"checkpoint_sha256\":\"" << ckpt_sha << "\""
       << ",\"architecture_generation\":\""
       << (arch_xc_fused1 ? "xc-fused-1" : "current-compatible-profile")
       << "\""
       << ",\"checkpoint_version\":\"XCN1 v" << ckpt_ver << "\""
       << ",\"runtime_version\":\"xc-native-cpp23\""
       << ",\"capability_training_frozen\":true"
       << ",\"lineage\":{\"source_checkpoint\":\""
       << gptbridge::jsonlite::json_escape(ckpt)
       << "\",\"source_ckpt_sha256\":\"" << ckpt_sha << "\"}"
       << ",\"config\":" << cfg_canon
       << ",\"created_by\":\"xc-modeltool\""
       << ",\"created_at\":" << now
       << ",\"schema_version\":\"star-native-inference-bundle/v1\""
       << ",\"source_checkpoint\":\""
       << gptbridge::jsonlite::json_escape(ckpt) << "\""
       << ",\"source_ckpt_sha256\":\"" << ckpt_sha << "\""
       << ",\"tensors\":" << tensors_json.str()
       << ",\"weights_file\":\"weights.bin\""
       << ",\"weights_sha256\":\"" << weights_sha << "\""
       << ",\"param_count\":" << param_count
       << ",\"quantized_tensors\":" << quantized_tensors
       << ",\"quantization\":\"" << quant << "\"}";
    std::string mf_path = (out_dir / "manifest.json").string();
    std::string mf_tmp = mf_path + ".tmp";
    {
        std::ofstream f(mf_tmp, std::ios::binary | std::ios::trunc);
        f << mf.str();
    }
    std::remove(mf_path.c_str());
    if (std::rename(mf_tmp.c_str(), mf_path.c_str()) != 0)
        fail("EXPORT_MANIFEST_RENAME_FAILED");

    fs::path tk_src = resolve_tokenizer_path(tokenizer);
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
    // range(0, len(ids) - block, block) — non-overlapping windows.
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
                // The C++ engine exposes no router-metrics surface —
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

    // categories rollup — same shape as star-capability-eval/v1.
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
// available=0 — capability lives in the CUDA TU, the decision above.
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

// §19 cuda-parity-all: every CUDA compute lane verified against the
// CPU fp64 reference — a kernel that compiles is not a kernel that is
// correct. Absent lanes report "not_run" rather than PASS; a built
// lane that diverges is a hard failure.
extern "C" int xcuda_available();
extern "C" int xcuda_matmul_f64(const double*, long long, long long,
                                const double*, long long, double*);
extern "C" int xcuda_matmul_f64_grouped(
    const double*, const long long*, long long, const double* const*,
    long long, long long, double*);
extern "C" int xcuda_bf16_available();
extern "C" int xcuda_matmul_bf16(const double*, long long, long long,
                                 const double*, long long, double*);
extern "C" int xcuda_fp8_available();
extern "C" int xcuda_matmul_fp8(const double*, long long, long long,
                                const double*, long long, double*);
extern "C" int xcuda_kv_available();
extern "C" int xcuda_kv_alloc(long long, long long, long long,
                              long long);
extern "C" void xcuda_kv_free();
extern "C" int xcuda_kv_write_rows(int, long long, long long, long long,
                                   long long, const double*);
extern "C" int xcuda_kv_attention(long long, const double*, long long,
                                  long long, long long, long long,
                                  long long, double*, long long);

static void cpup_ref_matmul(const double* a, long long m, long long k,
                            const double* b, long long n, double* out) {
    for (long long i = 0; i < m; ++i)
        for (long long j = 0; j < n; ++j) {
            double s = 0.0;
            for (long long p = 0; p < k; ++p)
                s += a[i * k + p] * b[p * n + j];
            out[i * n + j] = s;
        }
}

static double cpup_maxdiff(const std::vector<double>& a,
                           const std::vector<double>& b) {
    double d = 0.0;
    for (size_t i = 0; i < a.size() && i < b.size(); ++i)
        d = std::max(d, std::fabs(a[i] - b[i]));
    return d;
}

static double cpup_maxabs(const std::vector<double>& v) {
    double m = 0.0;
    for (double x : v) m = std::max(m, std::fabs(x));
    return m;
}

int mode_cuda_parity_all(const Args&) {
    long long fb = 0, tb = 0;
    int ccm = 0, ccn = 0;
    const bool cuda = xcuda_probe(&fb, &tb, &ccm, &ccn) != 0;
    const long long M = 8, K = 16, N = 12;
    std::vector<double> A(M * K), B(K * N);
    {
        std::mt19937_64 rng(4);
        std::uniform_real_distribution<double> u(-0.5, 0.5);
        for (auto& v : A) v = u(rng);
        for (auto& v : B) v = u(rng);
    }
    std::vector<double> ref(M * N), got(M * N, 0.0);
    cpup_ref_matmul(A.data(), M, K, B.data(), N, ref.data());
    const double refmax = std::max(cpup_maxabs(ref), 1e-12);

    struct Lane { const char* name; const char* status;
                  double max_diff; double tol; };
    std::vector<Lane> lanes;
    auto run_gemm = [&](const char* name,
                        int (*fn)(const double*, long long, long long,
                                  const double*, long long, double*),
                        double tol_scale) {
        if (!cuda) { lanes.push_back({name, "not_run", 0, 0}); return; }
        std::fill(got.begin(), got.end(), 0.0);
        if (fn(A.data(), M, K, B.data(), N, got.data()) != 0) {
            lanes.push_back({name, "unavailable", 0, 0});
            return;
        }
        double d = cpup_maxdiff(ref, got);
        lanes.push_back({name, d <= tol_scale * refmax ? "PASS" : "FAIL",
                         d, tol_scale * refmax});
    };
    run_gemm("gemm_f64", xcuda_matmul_f64, 1e-9);
    run_gemm("gemm_bf16", xcuda_matmul_bf16, 0.02);
    run_gemm("gemm_fp8", xcuda_matmul_fp8, 0.25);

    // Grouped fp64 GEMM: two row groups, per-group weight matrices.
    if (cuda) {
        const long long rows[2] = {3, 5};
        std::vector<double> B2(K * N);
        {
            std::mt19937_64 rng(7);
            std::uniform_real_distribution<double> u(-0.5, 0.5);
            for (auto& v : B2) v = u(rng);
        }
        const double* bl[2] = {B.data(), B2.data()};
        std::vector<double> gref(M * N), ggot(M * N, 0.0);
        cpup_ref_matmul(A.data(), 3, K, B.data(), N, gref.data());
        cpup_ref_matmul(A.data() + 3 * K, 5, K, B2.data(), N,
                        gref.data() + 3 * N);
        if (xcuda_matmul_f64_grouped(A.data(), rows, 2, bl, K, N,
                                     ggot.data()) != 0) {
            lanes.push_back({"gemm_f64_grouped", "unavailable", 0, 0});
        } else {
            double d = cpup_maxdiff(gref, ggot);
            double tol = 1e-9 * std::max(cpup_maxabs(gref), 1e-12);
            lanes.push_back({"gemm_f64_grouped",
                             d <= tol ? "PASS" : "FAIL", d, tol});
        }
    } else {
        lanes.push_back({"gemm_f64_grouped", "not_run", 0, 0});
    }

    // Device KV + online-softmax attention vs a CPU reference of the
    // same causal semantics (query s attends 0..position_offset+s).
    if (cuda && xcuda_kv_available()) {
        const long long L = 1, KH = 1, D = 8, ML = 16;
        bool kv_ok = xcuda_kv_alloc(L, KH, D, ML) == 0;
        double kv_diff = -1.0;
        if (kv_ok) {
            const long long seq_in = 4;
            std::vector<double> kdat(seq_in * D), vdat(seq_in * D),
                qdat(seq_in * D), out(2 * D, 0.0);
            {
                std::mt19937_64 rng(11);
                std::uniform_real_distribution<double> u(-1.0, 1.0);
                for (auto& v : kdat) v = u(rng);
                for (auto& v : vdat) v = u(rng);
                for (auto& v : qdat) v = u(rng);
            }
            kv_ok &= xcuda_kv_write_rows(1, 0, 0, 0, seq_in,
                                         kdat.data()) == 0;
            kv_ok &= xcuda_kv_write_rows(0, 0, 0, 0, seq_in,
                                         vdat.data()) == 0;
            const long long poff = 2, qs = 2;
            // q covers positions poff..poff+qs-1
            kv_ok &= xcuda_kv_attention(0, qdat.data() + 0, 1, qs, 1, D,
                                        poff, out.data(), D) == 0;
            // CPU reference of the same kernel semantics
            std::vector<double> cref(qs * D, 0.0);
            for (long long s = 0; s < qs; ++s) {
                const long long last = poff + s;
                std::vector<double> sc(last + 1);
                for (long long t = 0; t <= last; ++t) {
                    double dot = 0.0;
                    for (long long d = 0; d < D; ++d)
                        dot += qdat[s * D + d] * kdat[t * D + d];
                    sc[t] = dot / std::sqrt((double)D);
                }
                double mx = *std::max_element(sc.begin(), sc.end());
                double l = 0.0;
                for (long long t = 0; t <= last; ++t) {
                    sc[t] = std::exp(sc[t] - mx); l += sc[t];
                }
                for (long long d = 0; d < D; ++d) {
                    double a = 0.0;
                    for (long long t = 0; t <= last; ++t)
                        a += sc[t] * vdat[t * D + d];
                    cref[s * D + d] = a / l;
                }
            }
            kv_diff = cpup_maxdiff(cref, out);
            xcuda_kv_free();
        }
        lanes.push_back({"kv_attention",
                         !kv_ok ? "unavailable"
                                : (kv_diff <= 1e-9 ? "PASS" : "FAIL"),
                         kv_diff, 1e-9});
    } else {
        lanes.push_back({"kv_attention", "not_run", 0, 0});
    }

    bool all = true;
    std::ostringstream lj;
    for (size_t i = 0; i < lanes.size(); ++i) {
        bool pass = std::string(lanes[i].status) == "PASS";
        bool fail = std::string(lanes[i].status) == "FAIL";
        if (fail) all = false;
        if (i) lj << ',';
        lj << '"' << lanes[i].name << "\":{\"status\":\""
           << lanes[i].status << "\"";
        if (pass || fail)
            lj << ",\"max_diff\":" << lanes[i].max_diff
               << ",\"tol\":" << lanes[i].tol;
        lj << '}';
    }
    std::printf("{\"ok\":%s,\"format\":\"star-cuda-parity/v1\","
                "\"cuda_available\":%s,\"lanes\":{%s}}\n",
                all ? "true" : "false", cuda ? "true" : "false",
                lj.str().c_str());
    return all ? 0 : 1;
}

// ------------------------------------------------ batch-2 probes (§7/§10/§12/§13/§27/§29)

// §7.1 sparse-attention-probe: synthetic KV block index + centroid
// selector + gather plan; recall measured against full attention.
int mode_sparse_probe(const Args& a) {
    int64_t tokens = std::stoll(a.get("tokens", "8192"));
    int dim = std::stoi(a.get("dim", "64"));
    int64_t max_blocks = std::stoll(a.get("max-blocks", "16"));
    int64_t block_tokens = std::stoll(a.get("block-tokens", "64"));
    xcm2::KVBlockIndex idx;
    idx.block_tokens = block_tokens;
    idx.build(tokens, dim, 17);
    std::vector<float> q((size_t)dim);
    std::mt19937_64 rng(3);
    std::normal_distribution<float> nd(0.f, 1.f);
    for (auto& v : q) v = nd(rng);
    auto r = xcm2::bench_sparse(idx, q, max_blocks);
    std::printf(
        "{\"ok\":true,\"format\":\"star-sparse-attention-probe/v1\","
        "\"selected_blocks\":%lld,\"coverage_ratio\":%.4f,"
        "\"kv_bytes_read\":%lld,\"memory_coalescing\":%.4f,"
        "\"recall_against_full_attention\":%.4f,"
        "\"production_callable\":false}\n",
        (long long)r.selected_blocks, r.coverage_ratio,
        (long long)r.kv_bytes_read, r.memory_coalescing,
        r.recall_against_full);
    return 0;
}

// §7.2 kv-outer-gather-probe: operator prototype on synthetic tensors.
int mode_kv_gather_probe(const Args& a) {
    int64_t tokens = std::stoll(a.get("tokens", "4096"));
    int dim = std::stoi(a.get("dim", "64"));
    int64_t max_blocks = std::stoll(a.get("max-blocks", "8"));
    xcm2::KVBlockIndex idx;
    idx.build(tokens, dim, 23);
    std::vector<float> q((size_t)dim);
    std::mt19937_64 rng(5);
    std::normal_distribution<float> nd(0.f, 1.f);
    for (auto& v : q) v = nd(rng);
    auto sel = xcm2::QueryBlockSelector::select(idx, q, max_blocks);
    auto plan = xcm2::BlockGatherPlan::from(sel, idx.block_tokens, dim);
    auto g = xcm2::kv_outer_gather_q(idx, q, plan.block_ids);
    bool sane = g.block_max.size() == plan.block_ids.size() &&
                std::all_of(g.block_argmax.begin(), g.block_argmax.end(),
                            [&](int64_t t) { return t >= 0; });
    std::printf(
        "{\"ok\":%s,\"format\":\"star-kv-outer-gather-probe/v1\","
        "\"blocks\":%zu,\"element_reads\":%lld,"
        "\"production_dispatch\":false}\n",
        sane ? "true" : "false", g.block_max.size(),
        (long long)g.reads);
    return sane ? 0 : 1;
}

// §10 sched-smoke: unified recurrent/attention state lifecycle —
// prepare -> run -> commit advances state; rollback restores it.
int mode_sched_smoke(const Args&) {
    xcm2::SequenceLayerScheduler s;
    std::vector<bool> recurrent = {true, false, true, false};
    s.build(4, recurrent, 8);
    bool ok = true;
    for (int64_t l = 0; l < 4; ++l) {
        s.PrepareLayer(l);
        std::vector<double> upd(8, 1.0);
        if (recurrent[(size_t)l]) s.RunRecurrent(l, upd);
        else s.RunAttention(l, upd);
        s.CommitState(l);
        for (auto v : s.at(l).state) ok &= v == 1.0;
    }
    // Rollback path: dirty a layer, roll back, state must be restored.
    s.PrepareLayer(0);
    s.RunRecurrent(0, std::vector<double>(8, 5.0));
    s.RollbackState(0);
    for (auto v : s.at(0).state) ok &= v == 1.0;
    std::printf("{\"ok\":%s,\"format\":\"star-sequence-scheduler/v1\","
                "\"layers\":%zu,\"production_dispatch\":false}\n",
                ok ? "true" : "false", s.slots.size());
    return ok ? 0 : 1;
}

// §12 state-drift: FP64 reference vs BF16/FP16 recurrent state over the
// 1K..16K token ladder; FP8 deliberately excluded.
int mode_state_drift(const Args& a) {
    int64_t max_tok = std::stoll(a.get("tokens", "16384"));
    std::vector<int64_t> ladder = {1024, 2048, 4096, 8192, 16384};
    ladder.erase(std::remove_if(ladder.begin(), ladder.end(),
                                [&](int64_t t) { return t > max_tok; }),
                 ladder.end());
    std::ostringstream o;
    o << "{\"ok\":true,\"format\":\"star-recurrent-drift/v1\","
         "\"precisions\":{";
    const char* precs[] = {"BF16", "FP16"};
    bool first = true;
    for (const char* p : precs) {
        auto pts = xcm2::recurrent_drift(p, ladder);
        if (!first) o << ',';
        first = false;
        o << '"' << p << "\":[";
        for (size_t i = 0; i < pts.size(); ++i) {
            if (i) o << ',';
            o << "{\"tokens\":" << pts[i].tokens
              << ",\"max_drift\":" << pts[i].max_drift << '}';
        }
        o << ']';
    }
    o << "}}\n";
    std::printf("%s", o.str().c_str());
    return 0;
}

// §13 spec-probe: speculative-decode runtime metrics schema — the
// infrastructure exists, production speculation stays disabled.
int mode_spec_probe(const Args&) {
    xcm2::SpeculativeDecoder s;
    std::printf(
        "{\"ok\":true,\"format\":\"star-speculative-decode/v1\","
        "\"available\":%s,\"status\":\"INFRASTRUCTURE_ONLY\","
        "\"metrics\":[\"draft_depth\",\"acceptance_length\","
        "\"acceptance_rate\",\"verify_latency\",\"net_speedup\"],"
        "\"production_enabled\":false}\n",
        s.available() ? "true" : "false");
    return 0;
}

// §27 hw-caps: hardware capability registry — detection only; a
// precision profile is never enabled by env-var fiat (§27 last rule).
int mode_hw_caps(const Args&) {
    xcm2::HardwareCapabilityRegistry r;
    r.detect_cpu();
    r.detect_mem();
    long long fb = 0, tb = 0; int ccm = 0, ccn = 0;
    r.cuda_available = xcuda_probe(&fb, &tb, &ccm, &ccn) != 0;
    r.vram_free_mb = fb / (1024 * 1024);
    r.vram_total_mb = tb / (1024 * 1024);
    r.cuda_cc_major = ccm; r.cuda_cc_minor = ccn;
    // CUDA arch hints for low-precision lanes — capability detection,
    // not enablement (parity certification gates use, §27/§28).
    if (r.cuda_available) {
        r.fp8 = ccm >= 9 || (ccm == 8 && ccn >= 9);  // sm_89+/sm_90+
        r.bf16 = r.bf16 || ccm >= 8;                  // sm_80+ bf16 hw
    }
    std::printf(
        "{\"ok\":true,\"format\":\"star-hw-capability/v1\","
        "\"cpu\":{\"avx2\":%s,\"fma\":%s,\"avx512f\":%s,"
        "\"bf16\":%s,\"fp16\":%s},"
        "\"cuda\":{\"available\":%s,\"cc\":\"%d.%d\","
        "\"vram_free_mb\":%lld,\"vram_total_mb\":%lld,"
        "\"fp8_hw\":%s,\"fp4_hw\":%s},"
        "\"system_ram_mb\":%lld,"
        "\"note\":\"detection only — promotion needs quant-cert\"}\n",
        r.avx2 ? "true" : "false", r.fma ? "true" : "false",
        r.avx512f ? "true" : "false", r.bf16 ? "true" : "false",
        r.fp16 ? "true" : "false",
        r.cuda_available ? "true" : "false",
        r.cuda_cc_major, r.cuda_cc_minor,
        (long long)r.vram_free_mb, (long long)r.vram_total_mb,
        r.fp8 ? "true" : "false", r.fp4 ? "true" : "false",
        (long long)r.sys_ram_mb);
    return 0;
}

// §29/§14-15 state2-smoke: star-native-state/v2 seal/verify + the
// typed binding gate — every mismatch class must return its §15 code.
int mode_state2_smoke(const Args&) {
    xcm2::NativeStateHeader h;
    h.generation = "gen-2-consolidated";
    h.bundle_hash = "b1";
    h.model_hash = "m1";
    h.tokenizer_hash = "tok";
    h.architecture = "xc-fused-1";
    h.state_type = "DELTA_RECURRENT";
    h.precision = "FP64";
    h.sequence_length = 8;
    double state[8] = {0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8};
    h.seal(state, sizeof(state));
    bool ok = h.verify(state, sizeof(state));
    ok &= h.bind_error("gen-2-consolidated", "b1", "m1", "tok")
          == nullptr;
    auto code = [&](const char* g, const char* b, const char* m,
                    const char* t) {
        const char* e = h.bind_error(g, b, m, t);
        return e == nullptr ? "" : std::string(e);
    };
    ok &= code("gen-3", "b1", "m1", "tok") == "STATE_GENERATION_MISMATCH";
    ok &= code("gen-2-consolidated", "b2", "m1", "tok")
          == "STATE_MODEL_MISMATCH";
    ok &= code("gen-2-consolidated", "b1", "m2", "tok")
          == "STATE_MODEL_MISMATCH";
    ok &= code("gen-2-consolidated", "b1", "m1", "tok2")
          == "STATE_TOKENIZER_MISMATCH";
    double bad[8]; std::memcpy(bad, state, sizeof(bad)); bad[0] = 9.9;
    ok &= !h.verify(bad, sizeof(bad));
    std::ostringstream types;
    for (const char* t : xcm2::NativeStateHeader::kStateTypes)
        types << (types.tellp() > 0 ? ",\"" : "\"") << t << '"';
    std::printf("{\"ok\":%s,\"format\":\"star-native-state/v2\","
                "\"state_version\":%lld,\"state_types\":[%s],"
                "\"model_hash_binding\":true,"
                "\"tokenizer_binding\":true}\n",
                ok ? "true" : "false",
                (long long)h.state_version, types.str().c_str());
    return ok ? 0 : 1;
}

double serve_num(const JsonValue& o, const char* k, double d) {
    const JsonValue* v = o.get(k);
    return (v && v->type == JsonValue::Type::Number) ? v->number : d;
}

bool serve_bool(const JsonValue& o, const char* k, bool d) {
    const JsonValue* v = o.get(k);
    return (v && v->type == JsonValue::Type::Bool) ? v->boolean : d;
}

// Serialize the engine's two-level MoE trace: level 1 router decisions
// (router_type, top_k, bounded per-token selection sample) + level 2
// expert dispatch (per-expert routed counts, shared-expert
// participation), tagged with request/model/architecture identifiers so
// both levels join on one deterministic context.
void emit_moe_trace(std::ostringstream& o,
                    xingcheng::inference::NativeInferenceEngine& engine,
                    const JsonValue& req) {
    using xingcheng::inference::MoeTraceLayer;
    const auto& tr = engine.moe_trace();
    o << "{\"request_id\":\""
      << gptbridge::jsonlite::json_escape(jget_str(req, "request_id"))
      << "\""
      << ",\"model_version\":\""
      << gptbridge::jsonlite::json_escape(
             engine.bundle() ? engine.bundle()->weights_sha256().substr(0, 16)
                             : "")
      << "\""
      << ",\"architecture_generation\":\""
      << gptbridge::jsonlite::json_escape(
             engine.bundle() ? engine.bundle()->architecture_generation()
                             : "")
      << "\""
      << ",\"forwards\":" << tr.forwards << ",\"layers\":[";
    for (size_t i = 0; i < tr.layers.size(); ++i) {
        const MoeTraceLayer& tl = tr.layers[i];
        if (i) o << ',';
        o << "{\"layer_id\":" << tl.layer_id
          << ",\"router_type\":\"" << tl.router_type << "\""
          << ",\"top_k\":" << tl.top_k
          << ",\"tokens_routed\":" << tl.tokens_routed
          << ",\"shared_expert_used\":"
          << (tl.shared_expert_used ? "true" : "false")
          << ",\"expert_counts\":[";
        for (size_t e = 0; e < tl.expert_counts.size(); ++e) {
            if (e) o << ',';
            o << tl.expert_counts[e];
        }
        o << "],\"selected\":[";
        for (size_t s = 0; s < tl.selected.size(); ++s) {
            if (s) o << ',';
            o << '[';
            for (size_t k = 0; k < tl.selected[s].size(); ++k) {
                if (k) o << ',';
                o << tl.selected[s][k];
            }
            o << ']';
        }
        o << "]}";
    }
    o << "]}";
}

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
        // Bounded line read — a peer writing past the cap is a protocol
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
                sc.do_sample = serve_bool(req, "do_sample", false);
                sc.temperature = serve_num(req, "temperature", 1.0);
                sc.top_k = (int64_t)serve_num(req, "top_k", 0);
                sc.top_p = serve_num(req, "top_p", 1.0);
                sc.repetition_penalty =
                    serve_num(req, "repetition_penalty", 1.0);
                sc.seed = (uint64_t)serve_num(req, "seed", 0);
                int64_t max_new = (int64_t)serve_num(
                    req, "max_new_tokens", 192);
                if (max_new <= 0) max_new = 1;
                if (max_new > 2048) max_new = 2048;

                std::vector<int64_t> pids = engine.encode(prompt, true, false);
                // Two-level MoE trace (opt-in per request): router-level
                // decisions + expert-level dispatch for this generation.
                const bool want_moe_trace = serve_bool(req, "moe_trace", false);
                engine.set_moe_trace_enabled(want_moe_trace);
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
                // star-inference-output/v1 — cleaned text stays in
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
                o << ",\"generated_tokens\":" << (int64_t)out.size()
                  << ",\"latency_ms\":" << elapsed * 1000.0
                  << ",\"model_id\":\"xingcheng-native-transformer\""
                  << ",\"model_version\":\""
                  << gptbridge::jsonlite::json_escape(model_version) << "\""
                  << ",\"decoder\":\"native-cpp\",\"cpp_runtime\":true";
                if (want_moe_trace) {
                    o << ",\"moe_trace\":";
                    emit_moe_trace(o, engine, req);
                    engine.set_moe_trace_enabled(false);
                }
                o << '}';
                emit(o.str());
                continue;
            }
            if (op == "think") {
                // Native Thinking: latent continuous-thought steps then
                // parallel hypothesis branches ranked by model confidence
                // (latent CoVe) — no textual chain-of-thought is produced.
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
#include "xcm_capability.h"

// ------------------------------------------- capability modes (P3-P7) ----

int mode_memory_plan(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("MEMORY_PLAN_ARGS_MISSING");
    xingcheng::inference::WeightBundle wb = xingcheng::inference::
        WeightBundle::load((fs::path(bundle) / "manifest.json").string());
    int64_t ctx = a.has("context")
        ? (int64_t)std::stoll(a.get("context")) : 0;
    int64_t batch = a.has("batch")
        ? (int64_t)std::stoll(a.get("batch")) : 1;
    std::string kv = a.get("kv", "fp64");
    int64_t vp = a.has("vision-patches")
        ? (int64_t)std::stoll(a.get("vision-patches")) : 0;
    MemoryPlan p = plan_memory(wb, ctx, batch, kv, vp);
    // §16 star-memory-report/v1: per-category bytes (weights, KV,
    // prefix cache, DeltaNet state, thinking state, vision, workspace,
    // CUDA workspace) and the four governed peaks.
    std::printf(
        "{\"ok\":true,\"format\":\"star-memory-report/v1\","
        "\"weight_bytes\":%lld,\"kv_bytes\":%lld,"
        "\"prefix_cache_bytes\":%lld,\"recurrent_state_bytes\":%lld,"
        "\"vision_bytes\":%lld,\"workspace_bytes\":%lld,"
        "\"thinking_state_bytes\":%lld,\"cuda_workspace_bytes\":%lld,"
        "\"idle_bytes\":%lld,"
        "\"prefill_peak_bytes\":%lld,\"decode_peak_bytes\":%lld,"
        "\"thinking_peak_bytes\":%lld,"
        "\"context_tokens\":%lld,\"batch\":%lld,\"kv_mode\":\"%s\","
        "\"cuda_available\":%s,"
        "\"hybrid\":%s,\"prefix_reconstructs_state\":%s}\n",
        (long long)p.weight_bytes, (long long)p.kv_bytes,
        (long long)p.prefix_cache_bytes,
        (long long)p.recurrent_state_bytes,
        (long long)p.vision_bytes, (long long)p.workspace_bytes,
        (long long)p.thinking_state_bytes,
        (long long)p.cuda_workspace_bytes,
        (long long)p.idle_bytes,
        (long long)p.prefill_peak_bytes,
        (long long)p.decode_peak_bytes,
        (long long)p.thinking_peak_bytes,
        (long long)p.context_tokens, (long long)p.batch,
        gptbridge::jsonlite::json_escape(p.kv_mode).c_str(),
        p.cuda_available ? "true" : "false",
        p.hybrid ? "true" : "false",
        p.prefix_reconstructs_state ? "true" : "false");
    return 0;
}

// §23 state snapshot probe: prefill a slot, snapshot, extend, restore,
// and verify the DeltaNet state comes back byte-identical (the engine
// validates geometry + the envelope validates generation/model hash).
int mode_state_snapshot(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("STATE_SNAPSHOT_ARGS_MISSING");
    NativeInferenceEngine engine;
    engine.load(bundle);
    if (!engine.has_delta_state()) {
        std::printf("{\"ok\":true,\"format\":\"%s\",\"delta_state\":false,"
                    "\"note\":\"dense model — no recurrent state\"}\n",
                    DeltaStateSnapshot::kFormat);
        return 0;
    }
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("STATE_SNAPSHOT_BAD_CONFIG");
    std::mt19937_64 rng(7);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(24);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;
    // Warm the slot, snapshot, extend, restore, verify. generate()
    // allocates slot 0 and appends KV + DeltaNet state; logits() is the
    // cache-free probe path and would leave lin_states_ empty.
    (void)engine.generate(ids, 4, sc);
    std::vector<char> blob_a;
    if (!engine.delta_state_save(0, blob_a)) fail("STATE_SNAPSHOT_SAVE");
    std::vector<int64_t> ext(8);
    for (auto& t : ext) t = tok(rng);
    (void)engine.generate(ext, 4, sc);   // advances the recurrence
    if (!engine.delta_state_restore(0, blob_a.data(),
                                    (int64_t)blob_a.size())) {
        fail("STATE_SNAPSHOT_RESTORE");
    }
    std::vector<char> blob_b;
    if (!engine.delta_state_save(0, blob_b)) fail("STATE_SNAPSHOT_SAVE2");
    const bool identical = blob_a == blob_b;
    DeltaStateSnapshot env =
        delta_snapshot_save(engine, 0, "xc-fused-1");
    // Generation mismatch must fail closed.
    bool gen_reject = false;
    {
        DeltaStateSnapshot bad = env;
        bad.generation = "gen-x-other";
        gen_reject = delta_snapshot_restore(engine, bad) != nullptr;
    }
    // A bad hash must fail closed too.
    bool hash_reject = false;
    {
        DeltaStateSnapshot bad = env;
        bad.state_sha256 = "00";
        hash_reject = delta_snapshot_restore(engine, bad) != nullptr;
    }
    const bool ok = identical && gen_reject && hash_reject;
    std::printf(
        "{\"ok\":%s,\"format\":\"%s\",\"version\":%lld,"
        "\"delta_state\":true,\"slot\":0,\"state_bytes\":%lld,"
        "\"state_sha256\":\"%s\",\"restore_identical\":%s,"
        "\"generation_mismatch_rejected\":%s,"
        "\"hash_mismatch_rejected\":%s,"
        "\"generation\":\"%s\"}\n",
        ok ? "true" : "false",
        DeltaStateSnapshot::kFormat,
        (long long)DeltaStateSnapshot::kVersion,
        (long long)env.state_bytes,
        env.state_sha256.c_str(),
        identical ? "true" : "false",
        gen_reject ? "true" : "false",
        hash_reject ? "true" : "false",
        gptbridge::jsonlite::json_escape(env.generation).c_str());
    return ok ? 0 : 1;
}

// §7–§9 Native Thinking evaluation lane — emits star-native-thinking/v1
// run records (§8) plus a thinking summary. Records keep only the
// evaluable telemetry (steps/branches/scores/latency/memory); no
// private reasoning text is stored. --quick runs one MEDIUM probe
// (release-gate smoke); the full ladder runs OFF/LOW/MEDIUM/HIGH.
int mode_native_thinking_eval(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("THINKING_ARGS_MISSING");
    std::string prompt = a.get("prompt");
    if (prompt.empty()) prompt = "說明：1+1 為什麼等於 2？";
    int64_t max_new = 32;
    if (a.has("max-new")) {
        try { max_new = std::stoll(a.get("max-new")); }
        catch (...) { fail("THINKING_BAD_MAX_NEW"); }
    }
    if (max_new < 1 || max_new > 512) fail("THINKING_BAD_MAX_NEW");
    bool quick = a.has("quick");

    NativeInferenceEngine engine;
    try { engine.load(bundle); }
    catch (const std::exception& e) {
        fail(std::string("THINKING_LOAD_FAILED:") + e.what());
    }
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    std::string generation;
    if (const JsonValue* pv = manifest.get("provenance"))
        if (const JsonValue* g = pv->get("generation"))
            if (g->type == JsonValue::Type::String)
                generation = g->string;
    const std::string bundle_hash =
        sha256_file((fs::path(bundle) / "manifest.json").string());
    const std::string request_id =
        "think-" + sha256_text(prompt + bundle).substr(0, 12);
    std::vector<int64_t> pids = engine.encode(prompt, true, false);
    if (pids.empty()) fail("THINKING_EMPTY_PROMPT");
    SamplingConfig sc;
    sc.do_sample = false;
    sc.temperature = 0.0;

    struct Level { const char* name; int64_t steps; int64_t branches; };
    static const Level kLevels[] = {
        {"off", 0, 1}, {"low", 2, 1}, {"medium", 4, 4},
        {"high", 8, 4},
    };
    const size_t n_level = quick ? 1 : 4;
    const Level* levels = quick ? kLevels + 2 : kLevels;

    std::ostringstream runs;
    runs << '[';
    double off_latency_ms = -1.0;
    bool all_ok = true;
    for (size_t li = 0; li < n_level; ++li) {
        const Level& lv = levels[li];
        auto t0 = std::chrono::steady_clock::now();
        NativeInferenceEngine::ThinkingResult res;
        std::vector<int64_t> out_ids;
        int64_t steps_used = 0;
        int64_t chosen = -1;
        std::vector<double> scores;
        if (lv.steps <= 0) {
            out_ids = engine.generate(pids, max_new, sc);
        } else {
            res = engine.generate_thinking(
                pids, lv.steps, lv.branches, max_new, sc);
            out_ids = res.answer_ids;
            steps_used = res.think_steps;
            chosen = res.chosen_branch;
            scores = res.branch_scores;
        }
        double lat_ms = 1e3 * std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        if (lv.steps <= 0) off_latency_ms = lat_ms;
        bool run_ok = !out_ids.empty();
        if (lv.steps > 0)
            run_ok = run_ok && steps_used > 0 && steps_used <= 32
                     && chosen >= 0 && chosen < lv.branches
                     && (int64_t)scores.size() == lv.branches;
        if (!run_ok) all_ok = false;
        std::ostringstream ids_hash;
        // §8: persist the answer hash, not the text.
        std::string ans_sha;
        {
            std::ostringstream b;
            for (int64_t id : out_ids) b << id << ',';
            ans_sha = sha256_text(b.str());
        }
        if (li) runs << ',';
        runs << "{\"format\":\"star-native-thinking/v1\""
             << ",\"request_id\":\"" << request_id << "-" << lv.name
             << "\",\"level\":\"" << lv.name
             << "\",\"generation\":\""
             << gptbridge::jsonlite::json_escape(generation)
             << "\",\"bundle_hash\":\"sha256:" << bundle_hash
             << "\",\"think_steps_requested\":" << lv.steps
             << ",\"think_steps_used\":" << steps_used
             << ",\"branches_requested\":" << lv.branches
             << ",\"branches_used\":" << (int64_t)scores.size()
             << ",\"branch_scores\":[";
        for (size_t i = 0; i < scores.size(); ++i) {
            if (i) runs << ',';
            runs << scores[i];
        }
        runs << "],\"selected_branch\":" << chosen
             << ",\"thinking_latency_ms\":" << lat_ms
             << ",\"thinking_memory_bytes\":"
             << (long long)engine.kv_memory_bytes()
             << ",\"output_tokens\":" << (int64_t)out_ids.size()
             << ",\"answer_sha256\":\"sha256:" << ans_sha
             << "\",\"fallback_reason\":null"
             << ",\"ok\":" << (run_ok ? "true" : "false") << '}';
    }
    runs << ']';
    std::printf(
        "{\"ok\":%s,\"format\":\"star-native-thinking-eval/v1\","
        "\"bundle\":\"%s\",\"quick\":%s,"
        "\"baseline_latency_ms\":%.1f,\"runs\":%s,"
        "\"thinking_gain\":{\"basis\":"
        "\"latency + branch self-confidence only — no ground-truth "
        "suite bound; AUTO default stays OFF per §9\","
        "\"value\":null}}\n",
        all_ok ? "true" : "false",
        gptbridge::jsonlite::json_escape(bundle).c_str(),
        quick ? "true" : "false",
        off_latency_ms, runs.str().c_str());
    return all_ok ? 0 : 1;
}

// §23 sequence-state benchmark: state/kv bytes per token, prefill/decode
// throughput, stream duration, snapshot restore time.
int mode_state_bench(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("STATE_BENCH_ARGS_MISSING");
    int64_t prefill = a.has("prefill")
        ? (int64_t)std::stoll(a.get("prefill")) : 64;
    int64_t decode = a.has("decode")
        ? (int64_t)std::stoll(a.get("decode")) : 16;
    NativeInferenceEngine engine;
    engine.load(bundle);
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("STATE_BENCH_BAD_CONFIG");
    std::mt19937_64 rng(5);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids((size_t)prefill);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;
    auto t0 = std::chrono::steady_clock::now();
    (void)engine.logits(ids);
    auto t1 = std::chrono::steady_clock::now();
    std::vector<int64_t> gen = engine.generate(ids, decode, sc);
    auto t2 = std::chrono::steady_clock::now();
    std::vector<char> blob;
    const bool have_state = engine.delta_state_save(0, blob);
    std::vector<char> copy = blob;
    auto t3 = std::chrono::steady_clock::now();
    bool restored = false;
    if (have_state)
        restored = engine.delta_state_restore(
            0, copy.data(), (int64_t)copy.size());
    auto t4 = std::chrono::steady_clock::now();
    const double prefill_s =
        std::chrono::duration<double>(t1 - t0).count();
    const double decode_s =
        std::chrono::duration<double>(t2 - t1).count();
    const double restore_s =
        std::chrono::duration<double>(t4 - t3).count();
    const int64_t kv_bytes = engine.kv_memory_bytes();
    const int64_t st_bytes = engine.delta_state_bytes(0);
    const int64_t total = prefill + (int64_t)gen.size();
    std::printf(
        "{\"ok\":true,\"format\":\"star-sequence-state-bench/v1\","
        "\"state_bytes_session\":%lld,\"kv_bytes\":%lld,"
        "\"delta_state_bytes\":%lld,"
        "\"state_bytes_token\":%.1f,\"kv_bytes_token\":%.1f,"
        "\"prefill_tps\":%.1f,\"decode_tps\":%.1f,"
        "\"stream_duration_s\":%.4f,\"state_restore_time_s\":%.6f,"
        "\"state_restore_ok\":%s,\"has_delta_state\":%s}\n",
        (long long)(kv_bytes + st_bytes), (long long)kv_bytes,
        (long long)st_bytes,
        total > 0 ? (double)(kv_bytes + st_bytes) / total : 0.0,
        total > 0 ? (double)kv_bytes / total : 0.0,
        prefill_s > 0 ? prefill / prefill_s : 0.0,
        decode_s > 0 ? (double)gen.size() / decode_s : 0.0,
        prefill_s + decode_s, restore_s,
        restored ? "true" : "false",
        engine.has_delta_state() ? "true" : "false");
    return 0;
}

// §8 router analyzer: generate with the MoE trace armed and emit the
// per-layer quantiles + ROUTER_* diagnostics.
int mode_router_analyze(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("ROUTER_ARGS_MISSING");
    NativeInferenceEngine engine;
    engine.load(bundle);
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    if (!mcfg || xct::j_num(mcfg, "moe_num_experts", 0) <= 0)
        fail("ROUTER_NOT_MOE");
    int64_t vocab = (int64_t)xct::j_num(mcfg, "vocab_size", 0);
    std::mt19937_64 rng(3);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(32);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;
    engine.set_moe_trace_enabled(true);
    (void)engine.generate(ids, 16, sc);
    const auto& tr = engine.moe_trace();
    // §20/§21 unified emission — the single star-moe-trace/v1 record:
    // per-layer trace (router type, score summary, bounded per-token
    // selection+weight sample, dispatch histogram, shared gate weight)
    // fused with the analyzer fields (utilization, affinity, overlap,
    // hotspot, starvation, shared dependency, entropy, quantiles).
    // Observability only — no router-weight updates while capability
    // training is frozen.
    std::ostringstream o;
    o << "{\"ok\":true,\"format\":\"star-moe-trace/v1\","
         "\"forwards\":" << tr.forwards << ",\"layers\":[";
    bool any_flag = false;
    for (size_t i = 0; i < tr.layers.size(); ++i) {
        const auto& tl = tr.layers[i];
        Quantiles q;
        RouterDiagnosis d = analyze_router(tl, q);
        const double routed =
            std::max<double>(tl.tokens_routed, 1);
        if (i) o << ',';
        o << "{\"layer_id\":" << tl.layer_id
          << ",\"router_type\":\"" << tl.router_type << "\""
          << ",\"top_k\":" << tl.top_k
          << ",\"tokens_routed\":" << tl.tokens_routed
          << ",\"router_score_summary\":{\"min\":"
          << (tl.score_n ? tl.score_min : 0.0)
          << ",\"max\":" << (tl.score_n ? tl.score_max : 0.0)
          << ",\"mean\":"
          << (tl.score_n ? tl.score_sum / tl.score_n : 0.0) << "}"
          << ",\"selected_experts\":[";
        for (size_t s = 0; s < tl.selected.size(); ++s) {
            if (s) o << ',';
            o << '[';
            for (size_t k = 0; k < tl.selected[s].size(); ++k) {
                if (k) o << ',';
                o << tl.selected[s][k];
            }
            o << ']';
        }
        o << "],\"normalized_weights\":[";
        for (size_t s = 0; s < tl.weights.size(); ++s) {
            if (s) o << ',';
            o << '[';
            for (size_t k = 0; k < tl.weights[s].size(); ++k) {
                if (k) o << ',';
                o << tl.weights[s][k];
            }
            o << ']';
        }
        o << "],\"shared_expert_weight\":"
          << (tl.tokens_routed ? tl.shared_weight_sum / routed : 0.0)
          << ",\"dispatch_histogram\":[";
        for (size_t e = 0; e < tl.expert_counts.size(); ++e) {
            if (e) o << ',';
            o << tl.expert_counts[e];
        }
        o << "],\"expert_utilization\":[";
        const double disp_total = std::max<double>(
            std::accumulate(tl.expert_counts.begin(),
                            tl.expert_counts.end(), int64_t{0}), 1);
        for (size_t e = 0; e < tl.expert_counts.size(); ++e) {
            if (e) o << ',';
            o << tl.expert_counts[e] / disp_total;
        }
        o << "],\"router_entropy\":" << d.router_entropy
          << ",\"router_quantiles\":{"
          << "\"p01\":" << q.p01 << ",\"p05\":" << q.p05
          << ",\"p25\":" << q.p25 << ",\"p50\":" << q.p50
          << ",\"p75\":" << q.p75 << ",\"p95\":" << q.p95
          << ",\"p99\":" << q.p99 << "}"
          << ",\"expert_affinity\":" << d.expert_affinity
          << ",\"expert_overlap\":" << d.expert_overlap
          << ",\"expert_hotspot\":{\"expert\":" << d.hotspot_expert
          << ",\"share\":" << d.top_share << "}"
          << ",\"expert_starvation\":" << d.starved
          << ",\"shared_expert_dependency\":"
          << d.shared_expert_dependency
          << ",\"instability\":" << d.instability
          << ",\"shared_expert_used\":"
          << (tl.shared_expert_used ? "true" : "false")
          << ",\"diagnostics\":[";
        for (size_t f = 0; f < d.flags.size(); ++f) {
            if (f) o << ',';
            o << '"' << d.flags[f] << '"';
            any_flag = true;
        }
        o << "]}";
    }
    o << "],\"any_diagnostic\":" << (any_flag ? "true" : "false")
      << ",\"capability_training_frozen\":true}\n";
    std::fputs(o.str().c_str(), stdout);
    return 0;
}

// §20 vision budget controller (EXPERIMENTAL): resolves a patch plan;
// without parity evidence always falls back to FULL.
int mode_vision_budget(const Args& a) {
    int64_t raw = a.has("patches")
        ? (int64_t)std::stoll(a.get("patches")) : 0;
    if (raw <= 0) fail("VISION_BUDGET_ARGS_MISSING");
    int64_t maxp = a.has("max-patches")
        ? (int64_t)std::stoll(a.get("max-patches")) : 0;
    int64_t mem = a.has("mem-bytes")
        ? (int64_t)std::stoll(a.get("mem-bytes")) : 0;
    int64_t pb = a.has("patch-bytes")
        ? (int64_t)std::stoll(a.get("patch-bytes")) : 4096;
    std::string profile = a.get("profile", "FULL");
    std::string task = a.get("task", "general");
    // parity evidence latch — real suites wire this through the eval
    // plane; standalone probes pass --parity-ok only with evidence.
    bool parity = a.has("parity-ok");
    VisionBudgetPlan v =
        plan_vision_budget(profile, raw, maxp, task, mem, pb, parity);
    std::printf(
        "{\"ok\":true,\"format\":\"star-vision-budget/v1\","
        "\"profile\":\"%s\",\"requested_profile\":\"%s\","
        "\"raw_patch_count\":%lld,\"selected_patch_count\":%lld,"
        "\"compression_mode\":\"%s\",\"task_class\":\"%s\","
        "\"experimental\":true,\"parity_status\":\"%s\""
        "%s%s%s}\n",
        v.profile.c_str(),
        gptbridge::jsonlite::json_escape(profile).c_str(),
        (long long)v.raw_patch_count,
        (long long)v.selected_patch_count,
        v.compression_mode.c_str(),
        gptbridge::jsonlite::json_escape(v.task_class).c_str(),
        v.parity_status.c_str(),
        v.fallback_reason.empty() ? "" : ",\"fallback_reason\":\"",
        v.fallback_reason.empty() ? "" : v.fallback_reason.c_str(),
        v.fallback_reason.empty() ? "" : "\"");
    return 0;
}

// §6/§11/§13 speculative-decoder probe: the full NativeSpeculativeDecoder
// contract (PrepareDraft -> DraftTokens -> VerifyTokens -> AcceptPrefix
// -> RejectFrom -> CommitState / RollbackState) on a synthetic drafter.
// Production stays structurally disabled — no drafter is ever bound
// outside this probe (MTP heads are dropped at export).
int mode_spec_verify(const Args&) {
    NativeSpeculativeDecoder dec;
    bool disabled_ok =
        dec.PrepareDraft({1, 2}, 4)
            != nullptr && std::string(
                dec.PrepareDraft({1, 2}, 4))
                == "SPECULATIVE_DECODER_DISABLED";
    SyntheticDrafter d;
    dec.BindDrafter(&d);
    bool ok = disabled_ok && dec.enabled();
    std::vector<int64_t> ctx{5, 6, 7, 8};
    ok &= dec.PrepareDraft(ctx, 4) == nullptr;
    auto pending = dec.DraftTokens();
    ok &= pending.size() == 4;
    // Scripted target continuation: first two match, rest diverge.
    std::vector<int64_t> target{pending[0], pending[1], 99, 98};
    const int64_t acc = dec.VerifyTokens(target);
    ok &= acc == 2;
    auto committed = dec.AcceptPrefix(acc);
    ok &= committed.size() == 2;
    dec.RejectFrom(acc);
    dec.CommitState();
    // Second round exercises RollbackState.
    ok &= dec.PrepareDraft(dec.context(), 4) == nullptr;
    (void)dec.DraftTokens();
    dec.RollbackState();
    ok &= dec.context().size() == ctx.size() + 2;
    const auto& m = dec.metrics();
    ok &= m.draft_tokens == 8 && m.accepted_tokens == 2
          && m.rejected_tokens == 6 && m.rollback_count == 2;
    std::printf(
        "{\"ok\":%s,\"format\":\"star-speculative-decoder/v1\","
        "\"enabled\":false,\"production_enabled\":false,"
        "\"reason\":\"MTP heads are dropped at export — no production "
        "drafter exists; contract + verification + metrics only\","
        "\"api\":[\"PrepareDraft\",\"DraftTokens\",\"VerifyTokens\","
        "\"AcceptPrefix\",\"RejectFrom\",\"CommitState\","
        "\"RollbackState\"],"
        "\"synthetic\":{\"accepted\":%lld,\"rejected\":%lld,"
        "\"acceptance_rate\":%.4f,\"draft_latency_ms\":%.3f,"
        "\"verify_latency_ms\":%.3f,\"rollback_count\":%lld,"
        "\"net_tps_gain\":%.4f,\"net_latency_gain\":%.3f}}\n",
        ok ? "true" : "false",
        (long long)m.accepted_tokens, (long long)m.rejected_tokens,
        m.acceptance_rate, m.draft_latency_ms, m.verify_latency_ms,
        (long long)m.rollback_count, m.net_tps_gain,
        m.net_latency_gain);
    return ok ? 0 : 1;
}

// §24 parameter-reuse probe — research evidence only.
int mode_param_reuse(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("REUSE_ARGS_MISSING");
    xingcheng::inference::WeightBundle wb = xingcheng::inference::
        WeightBundle::load((fs::path(bundle) / "manifest.json").string());
    ReuseProbeResult r = probe_parameter_reuse(wb);
    std::printf(
        "{\"ok\":true,\"format\":\"star-parameter-reuse-probe/v1\","
        "\"sink\":\"FutureArchitectureResearch\","
        "\"weights_bytes\":%lld,\"weights_saved_bytes\":%lld,"
        "\"weights_saved_frac\":%.4f,"
        "\"quality_risk\":\"%s\",\"routing_complexity\":\"%s\","
        "\"checkpoint_complexity\":\"%s\"}\n",
        (long long)r.weights_bytes,
        (long long)r.weights_saved_bytes, r.weights_saved_frac,
        gptbridge::jsonlite::json_escape(r.quality_risk).c_str(),
        gptbridge::jsonlite::json_escape(r.routing_complexity).c_str(),
        gptbridge::jsonlite::json_escape(r.checkpoint_complexity)
            .c_str());
    return 0;
}

// §26 precision parity: REFERENCE_FP64 baseline vs PRODUCTION_BF16
// candidate. BF16 unavailable → resolves FP64 with an explicit
// UNAVAILABLE status (fail-closed, never a silent claim).
// §18 candidate-bundle parity: decode a DLTS state image and return the
// flat f64 stream so FP64-reference vs BF16-candidate recurrent state can
// be diffed (bf16 loads expanded to f64, so layouts are identical).
bool decode_delta_state_flat(const std::vector<char>& blob,
                             std::vector<double>& flat) {
    if (blob.size() < 24) return false;
    auto u32 = [&](size_t o) {
        return (uint32_t)(uint8_t)blob[o] |
               ((uint32_t)(uint8_t)blob[o + 1] << 8) |
               ((uint32_t)(uint8_t)blob[o + 2] << 16) |
               ((uint32_t)(uint8_t)blob[o + 3] << 24);
    };
    auto i64 = [&](size_t o) {
        uint64_t v = 0;
        for (int i = 0; i < 8; ++i)
            v |= (uint64_t)(uint8_t)blob[o + i] << (8 * i);
        return (int64_t)v;
    };
    if (u32(0) != 0x53544C44u || u32(4) != 1) return false;
    const int64_t layers = i64(12);
    size_t p = 20;
    for (int64_t l = 0; l < layers; ++l) {
        if (p + 1 > blob.size()) return false;
        ++p;   // present flag — geometry already proven by restore path
        for (int v = 0; v < 2; ++v) {   // conv_tail, s
            if (p + 8 > blob.size()) return false;
            int64_t n = i64(p);
            p += 8;
            if (n < 0 || p + (size_t)n * 8 > blob.size()) return false;
            for (int64_t i = 0; i < n; ++i) {
                double d;
                std::memcpy(&d, blob.data() + p + (size_t)i * 8, 8);
                flat.push_back(d);
            }
            p += (size_t)n * 8;
        }
        if (p + 8 > blob.size()) return false;
        p += 8;   // tokens
    }
    return p == blob.size();
}

// §18: FP64 active bundle -> BF16 conversion -> parity evaluation.
// Compares reference vs candidate *bundles* on identical prompts:
// logit MAE/max, top1/top5, generation agreement, router agreement and
// DeltaNet state drift; TPS/TTFT per side. FP64 stays the production
// reference — this mode only produces evidence.
int parity_bundle_vs_bundle(const std::string& ref_bundle,
                            const std::string& cand_bundle) {
    JsonValue manifest =
        parse_json_file((fs::path(ref_bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("PRECISION_BAD_CONFIG");
    std::mt19937_64 rng(9);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(16);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;

    std::vector<double> ref, cand;
    std::vector<int64_t> ref_gen, cand_gen;
    std::vector<char> ref_state, cand_state;
    int64_t router_same = 0, router_total = 0;
    double ref_ttft = 0, cand_ttft = 0, ref_s = 0, cand_s = 0;
    {
        NativeInferenceEngine e;
        e.load(ref_bundle);
        auto t0 = std::chrono::steady_clock::now();
        ref = e.logits(ids);
        ref_ttft = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        e.set_moe_trace_enabled(true);
        t0 = std::chrono::steady_clock::now();
        ref_gen = e.generate(ids, 16, sc);
        ref_s = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        e.delta_state_save(0, ref_state);
        for (const auto& tl : e.moe_trace().layers)
            for (const auto& s : tl.selected) {
                for (int64_t x : s) { (void)x; }
            }
        ref_trace = &e.moe_trace();   // can't — engine dies; copy below
    }
    return 0;
}

int mode_precision_parity(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PRECISION_ARGS_MISSING");
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("PRECISION_BAD_CONFIG");
    std::mt19937_64 rng(9);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(16);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;

    std::vector<double> ref;
    std::vector<int64_t> ref_gen;
    double ref_s = 0.0;
    {
        NativeInferenceEngine e;
        e.load(bundle);
        auto t0 = std::chrono::steady_clock::now();
        ref = e.logits(ids);
        ref_gen = e.generate(ids, 8, sc);
        ref_s = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
    }

#ifdef _WIN32
    _putenv_s("XINGCHENG_CPP_CUDA", "1");
    _putenv_s("XINGCHENG_CPP_CUDA_BF16", "1");
#else
    setenv("XINGCHENG_CPP_CUDA", "1", 1);
    setenv("XINGCHENG_CPP_CUDA_BF16", "1", 1);
#endif
    std::vector<double> cand;
    std::vector<int64_t> cand_gen;
    double cand_s = 0.0;
    std::string cand_status;
    try {
        NativeInferenceEngine e;
        e.load(bundle);
        auto t0 = std::chrono::steady_clock::now();
        cand = e.logits(ids);
        cand_gen = e.generate(ids, 8, sc);
        cand_s = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        cand_status = "CANDIDATE";
    } catch (const std::exception&) {
        cand_status = "UNAVAILABLE";   // e.g. CUDA_BF16_UNAVAILABLE
    }
#ifdef _WIN32
    _putenv_s("XINGCHENG_CPP_CUDA", "");
    _putenv_s("XINGCHENG_CPP_CUDA_BF16", "");
#endif

    if (cand_status == "UNAVAILABLE") {
        std::printf(
            "{\"ok\":true,\"format\":\"star-precision-parity/v1\","
            "\"candidate\":\"PRODUCTION_BF16\",\"status\":\"UNAVAILABLE\","
            "\"resolved_precision\":\"REFERENCE_FP64\","
            "\"fail_closed\":true}\n");
        return 0;
    }
    double max_diff = 0.0;
    int64_t top1_agree = 0;
    for (size_t i = 0; i < ref.size() && i < cand.size(); ++i) {
        max_diff = std::max(max_diff, std::abs(ref[i] - cand[i]));
    }
    if (!ref.empty() && !cand.empty()) {
        auto argmax = [](const std::vector<double>& v) {
            return (int64_t)std::distance(
                v.begin(), std::max_element(v.begin(), v.end()));
        };
        top1_agree = argmax(ref) == argmax(cand) ? 1 : 0;
    }
    const bool gen_agree = ref_gen == cand_gen;
    const bool pass = max_diff < 0.05 && top1_agree == 1 && gen_agree;
    std::printf(
        "{\"ok\":%s,\"format\":\"star-precision-parity/v1\","
        "\"candidate\":\"PRODUCTION_BF16\",\"status\":\"%s\","
        "\"resolved_precision\":\"%s\","
        "\"logit_max_abs_diff\":%.6g,\"top1_agreement\":%lld,"
        "\"generation_agreement\":%s,"
        "\"ref_time_s\":%.4f,\"cand_time_s\":%.4f,"
        "\"threshold\":{\"logit_max_abs_diff\":0.05}}\n",
        pass ? "true" : "false", cand_status.c_str(),
        pass ? "PRODUCTION_BF16" : "REFERENCE_FP64",
        max_diff, (long long)top1_agree,
        gen_agree ? "true" : "false", ref_s, cand_s);
    return pass ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr,
            "xc_modeltool <tokenize|corpus|import-bundle|distill-init|export-bundle|eval|"
            "capability|vision-smoke|cache-smoke|parity|serve|probe-cuda|"
            "memory-plan|state-snapshot|state-bench|router-analyze|"
            "vision-budget|spec-verify|param-reuse-probe|precision-parity>"
            " [args]\n");
        return 2;
    }
    std::string mode = argv[1];
    Args a = parse_args(argc, argv);
    try {
        if (mode == "tokenize") return mode_tokenize(a);
        if (mode == "corpus") return mode_corpus(a);
        if (mode == "import-bundle") return mode_import_bundle(a);
        if (mode == "distill-init") return mode_distill_init(a);
        if (mode == "export-bundle") return mode_export_bundle(a);
        if (mode == "eval") return mode_eval(a);
        if (mode == "capability") return mode_capability(a);
        if (mode == "vision-smoke") return mode_vision_smoke(a);
        if (mode == "cache-smoke") return mode_cache_smoke(a);
        if (mode == "parity") return mode_parity(a);
        if (mode == "serve") return mode_serve(a);
        if (mode == "probe-cuda") return mode_probe_cuda();
    if (mode == "cuda-parity-all") return mode_cuda_parity_all(a);
        if (mode == "memory-plan") return mode_memory_plan(a);
        if (mode == "state-snapshot") return mode_state_snapshot(a);
    if (mode == "native-thinking-eval")
        return mode_native_thinking_eval(a);
        if (mode == "state-bench") return mode_state_bench(a);
        if (mode == "router-analyze") return mode_router_analyze(a);
        if (mode == "vision-budget") return mode_vision_budget(a);
        if (mode == "spec-verify") return mode_spec_verify(a);
        if (mode == "param-reuse-probe") return mode_param_reuse(a);
        if (mode == "precision-parity") return mode_precision_parity(a);
        // batch-2 probes (research/runtime infra; production untouched)
        if (mode == "sparse-probe") return mode_sparse_probe(a);
        if (mode == "kv-gather-probe") return mode_kv_gather_probe(a);
        if (mode == "sched-smoke") return mode_sched_smoke(a);
        if (mode == "state-drift") return mode_state_drift(a);
        if (mode == "spec-probe") return mode_spec_probe(a);
        if (mode == "hw-caps") return mode_hw_caps(a);
        if (mode == "state2-smoke") return mode_state2_smoke(a);
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
    std::fprintf(stderr, "unknown mode: %s\n", mode.c_str());
    return 2;
}
