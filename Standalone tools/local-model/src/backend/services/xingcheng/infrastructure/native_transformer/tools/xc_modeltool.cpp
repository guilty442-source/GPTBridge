// xc_modeltool.cpp — governed model-side utilities for the native lane.
//
// Modes (single JSON object on stdout; non-zero exit on failure):
//   tokenize      --tokenizer <tokenizer.json|dir> --in rows.jsonl
//                 --out rows.jsonl [--max-length N] [--chat]
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
//
// Tokenize row shapes (star SFT/DPO/pretrain contracts):
//   {"prompt","completion"}      -> {"input_ids","labels"}  (masked prompt)
//   {"text"}                     -> {"input_ids"}           (pretrain; trainer
//                                  shifts labels itself)
//   {"prompt","chosen","rejected"} -> {"chosen","rejected"} (DPO)
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
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numeric>
#include <random>
#include <regex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

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
#include "xct_ckpt.h"
}  // namespace xct

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
    std::printf("{\"ok\":false,\"error\":\"%s\"}\n", code.c_str());
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

int mode_tokenize(const Args& a) {
    std::string tk_path = resolve_tokenizer_path(a.get("tokenizer"));
    ByteLevelBPETokenizer tk = ByteLevelBPETokenizer::load(tk_path);
    int64_t max_len = a.has("max-length")
                          ? std::stoll(a.get("max-length")) : 0;
    bool chat = a.has("chat");
    // eos: encode("", add_eos) returns {eos_id}; -1 when undetectable.
    std::vector<int64_t> eos_probe = tk.encode("", false, true);
    int64_t eos_id = eos_probe.empty() ? -1 : eos_probe.back();

    std::string in_path = a.get("in"), out_path = a.get("out");
    if (in_path.empty() || out_path.empty()) fail("TOKENIZE_ARGS_MISSING");
    std::vector<JsonValue> rows = read_jsonl(in_path);
    std::ofstream out(out_path, std::ios::binary | std::ios::trunc);
    if (!out) fail("TOKENIZE_OUT_UNWRITABLE");
    int64_t n_in = 0, n_out = 0, n_dropped = 0;
    for (const JsonValue& row : rows) {
        ++n_in;
        std::string prompt = jget_str(row, "prompt");
        const JsonValue* chosen = row.get("chosen");
        const JsonValue* rejected = row.get("rejected");
        const JsonValue* text = row.get("text");
        std::string line;
        if (chosen && rejected) {
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
        out << line << '\n';
        ++n_out;
    }
    out.close();
    std::printf("{\"ok\":true,\"mode\":\"tokenize\",\"rows_in\":%lld,"
                "\"rows_out\":%lld,\"rows_dropped\":%lld,\"eos_id\":%lld,"
                "\"tokenizer_sha256\":\"%s\"}\n",
                (long long)n_in, (long long)n_out, (long long)n_dropped,
                (long long)eos_id, sha256_file(tk_path).c_str());
    return 0;
}

// ----------------------------------------------------- name translation ---

std::string bundle_to_xct(const std::string& b) {
    static const std::unordered_map<std::string, std::string> fixed = {
        {"model.embeddings.word_embeddings.weight", "embed"},
        {"lm_head.weight", "lm_head"},
        {"model.final_norm.weight", "norm_f"},
    };
    auto it = fixed.find(b);
    if (it != fixed.end()) return it->second;
    std::smatch m;
    std::string l, e, s;
    std::string base = "layers.";
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.input_norm\.weight$)")))
        return base + m[1].str() + ".norm1";
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.post_attention_norm\.weight$)")))
        return base + m[1].str() + ".norm2";
    if (std::regex_match(b, m,
            std::regex(R"(^model\.layers\.(\d+)\.attention\.(q|k|v|o)_proj\.weight$)"))) {
        char w = m[2].str()[0];
        return base + m[1].str() + ".w" + w;
    }
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
    return "";
}

std::string xct_to_bundle(const std::string& n) {
    static const std::unordered_map<std::string, std::string> fixed = {
        {"embed", "model.embeddings.word_embeddings.weight"},
        {"lm_head", "lm_head.weight"},
        {"norm_f", "model.final_norm.weight"},
    };
    auto it = fixed.find(n);
    if (it != fixed.end()) return it->second;
    std::smatch m;
    std::string base = "model.layers.";
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.norm1$)")))
        return base + m[1].str() + ".input_norm.weight";
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.norm2$)")))
        return base + m[1].str() + ".post_attention_norm.weight";
    if (std::regex_match(n, m, std::regex(R"(^layers\.(\d+)\.w([qkvo])$)"))) {
        char w = m[2].str()[0];
        return base + m[1].str() + ".attention." + w + "_proj.weight";
    }
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
    return "";
}

// ------------------------------------------------------- import-bundle ----

xct::ModelConfig config_from_manifest(const JsonValue& cfg) {
    xct::ModelConfig c;
    c.vocab = (int)xct::j_num(&cfg, "vocab_size", c.vocab);
    c.hidden = (int)xct::j_num(&cfg, "hidden_size", c.hidden);
    c.inter = (int)xct::j_num(&cfg, "intermediate_size", c.inter);
    c.layers = (int)xct::j_num(&cfg, "num_hidden_layers", c.layers);
    c.heads = (int)xct::j_num(&cfg, "num_attention_heads", c.heads);
    c.kv_heads = (int)xct::j_num(&cfg, "num_key_value_heads", c.heads);
    c.max_pos = (int)xct::j_num(&cfg, "max_position_embeddings", c.max_pos);
    c.rope_theta = (float)xct::j_num(&cfg, "rope_theta", c.rope_theta);
    c.rms_eps = (float)xct::j_num(&cfg, "rms_norm_eps", c.rms_eps);
    c.moe_aux_w = (float)xct::j_num(&cfg, "moe_aux_loss_weight", c.moe_aux_w);
    bool use_moe = false;
    const JsonValue* um = cfg.get("use_moe");
    if (um && um->type == JsonValue::Type::Bool) use_moe = um->boolean;
    if (use_moe) {
        c.moe_experts = (int)xct::j_num(&cfg, "moe_num_experts", 0);
        c.moe_top_k = (int)xct::j_num(&cfg, "moe_top_k", c.moe_top_k);
        c.moe_layer_interval =
            (int)xct::j_num(&cfg, "moe_layer_interval", c.moe_layer_interval);
        c.moe_expert_inter =
            (int)xct::j_num(&cfg, "moe_expert_intermediate_size", 0);
        c.moe_shared_experts =
            (int)xct::j_num(&cfg, "moe_num_shared_experts", 0);
        c.moe_shared_inter =
            (int)xct::j_num(&cfg, "moe_shared_intermediate_size", 0);
    } else {
        c.moe_experts = 0;
        c.moe_shared_experts = 0;
    }
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
// distill-init.
void load_bundle_params(const fs::path& bundle, xct::ModelConfig& c,
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
        std::string xname = bundle_to_xct(name);
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
}

int mode_import_bundle(const Args& a) {
    fs::path bundle = a.get("bundle");
    std::string out = a.get("out");
    if (bundle.empty() || out.empty()) fail("IMPORT_ARGS_MISSING");
    xct::ModelConfig c;
    xct::Params p;
    load_bundle_params(bundle, c, p);
    if (!xct::ckpt_save(p, c, out, /*overwrite=*/false))
        fail("IMPORT_CKPT_WRITE_FAILED:" + out);
    std::printf("{\"ok\":true,\"mode\":\"import-bundle\",\"out\":\"%s\","
                "\"ckpt_sha256\":\"%s\",\"tensors\":%zu}\n",
                gptbridge::jsonlite::json_escape(out).c_str(),
                sha256_file(out).c_str(), p.order.size());
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
             {"norm1", "wq", "wk", "wv", "wo", "norm2"})
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

// ------------------------------------------------------- export-bundle ----

int mode_export_bundle(const Args& a) {
    std::string ckpt = a.get("ckpt");
    fs::path out_dir = a.get("out");
    std::string cfg_from = a.get("config-from");
    std::string tokenizer = a.get("tokenizer");
    if (ckpt.empty() || out_dir.empty() || cfg_from.empty() || tokenizer.empty())
        fail("EXPORT_ARGS_MISSING");

    // Pass 1: read XCN header -> config; then allocate + load.
    xct::ModelConfig c;
    if (!xct::ckpt_peek_config(ckpt, c)) fail("EXPORT_CKPT_UNREADABLE");
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

    fs::create_directories(out_dir);
    // Deterministic tensor order: sorted bundle names.
    std::vector<std::pair<std::string, std::string>> pairs;
    pairs.reserve(p.order.size());
    for (const auto& n : p.order) {
        std::string b = xct_to_bundle(n);
        if (b.empty()) fail("EXPORT_UNMAPPED_TENSOR:" + n);
        pairs.emplace_back(b, n);
    }
    std::sort(pairs.begin(), pairs.end());

    // --quant none|int8|int4_packed: weight-only per-tensor symmetric
    // quantization of 2-D matrices (mirrors kernels/quant.py; the engine
    // dequantizes to fp64 at load). 1-D tensors (norms) stay fp64.
    std::string quant = a.get("quant");
    if (quant.empty()) quant = "none";
    if (quant != "none" && quant != "int8" && quant != "int4_packed")
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
    std::ostringstream mf;
    mf << "{\"checkpoint_sha256\":\"" << ckpt_sha << "\""
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

double serve_num(const JsonValue& o, const char* k, double d) {
    const JsonValue* v = o.get(k);
    return (v && v->type == JsonValue::Type::Number) ? v->number : d;
}

bool serve_bool(const JsonValue& o, const char* k, bool d) {
    const JsonValue* v = o.get(k);
    return (v && v->type == JsonValue::Type::Bool) ? v->boolean : d;
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

                std::ostringstream o;
                o << "{\"ok\":true,\"text\":\""
                  << gptbridge::jsonlite::json_escape(text) << "\"";
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

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr,
            "xc_modeltool <tokenize|import-bundle|distill-init|export-bundle|eval|"
            "capability|serve> [args]\n");
        return 2;
    }
    std::string mode = argv[1];
    Args a = parse_args(argc, argv);
    try {
        if (mode == "tokenize") return mode_tokenize(a);
        if (mode == "import-bundle") return mode_import_bundle(a);
        if (mode == "distill-init") return mode_distill_init(a);
        if (mode == "export-bundle") return mode_export_bundle(a);
        if (mode == "eval") return mode_eval(a);
        if (mode == "capability") return mode_capability(a);
        if (mode == "serve") return mode_serve(a);
        if (mode == "probe-cuda") return mode_probe_cuda();
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
