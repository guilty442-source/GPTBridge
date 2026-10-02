// xcm_corpus.h — C108 star-pretrain-corpus/v1: governed unsupervised
// pretraining corpus pipeline (native lane). Included once by
// xc_modeltool.cpp inside the anonymous namespace, after the shared
// helpers (slurp/nfc/sha256/JsonValue) it uses.
//
//   corpus --registry <corpus-registry.json> --root <repo-root>
//          --tokenizer <path-or-dir> --out <dir>
//          [--max-len N=1024] [--val-ratio PCT=5] [--max-docs N=20000]
//          [--max-doc-chars N=1000000] [--max-tokens N=50000000]
//          [--jobs N=min(8,hw)]
//
// The eleven mandated elements land as: registry-gated source list
// (corpus registry), content-addressed dataset_version, per-document
// language classification, source_id+relpath source tracking on every
// kept document, license+sensitivity validation before any read, NFC
// with raw-vs-normalized hash mapping, exact + MinHash-band near
// duplicate detection, EOS-separated sequence packing into train/valid
// id rows, deterministic hash train/validation split, per-document and
// total token counts, and a dataset-integrity manifest carrying per-file
// SHA256 plus the accounting counters.
//
// Fail-closed (C108 exclusion rule): confidential codex content (the
// codex tree incl. the zh-TW mirror parts and the codex database),
// permission data, 星澄 auxiliary-channel data, runtime state, audit
// journals and unlicensed/unauthorized sources are denied by path
// fragment or registry gate BEFORE any read; denied paths are counted,
// never sampled.
#pragma once

char corpus_lc(char c) {
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

struct CorpusSource {
    std::string id, path, owner, license, language, sensitivity;
};

struct CorpusDoc {
    std::string source_id, relpath, sha_raw, sha_nfc, overlap_sha,
        language, split;
    bool nfc_changed = false;
    std::vector<int64_t> ids;
};

// Deny-list on lowercase '/'-separated repo-relative paths. Mirrors the
// C108 confidential classes; deliberately fragment-based so renames
// cannot route around it.
bool corpus_denied(const std::string& rel) {
    static const char* kDeny[] = {
        "governance_rule/codex",   // codex + zh-TW mirrors + codex db
        "governance_codex",        // stray codex artifacts anywhere
        "permission_directory",    // permission data
        "permission-directory",
        "/audit/",                 // audit journals
        "git_audit_chain",
        "codex_amendment_audit",
        "codex_read_audit",
        "/runtime/",               // runtime state
        ".git/",
        ".worktrees",
        "node_modules",
        ".backups",
        "/obj/",
        "/bin/",
        "/.vs/",
        "/.vscode/",
        "/.devin/",
        "/.kilo/",
        "/.smallcode/",
        "/.tools/",
    };
    // Anchor-fragments like "/runtime/" must still deny a top-level
    // "runtime/" dir, so match against a '/'-padded haystack.
    std::string hay = "/" + rel;
    if (rel.empty() || rel.back() != '/') hay += '/';
    for (const char* d : kDeny)
        if (hay.find(d) != std::string::npos) return true;
    return false;
}

bool corpus_text_ext(const std::string& ext) {
    static const char* kExt[] = {
        ".md",  ".txt",  ".json", ".py",   ".cs",  ".cpp", ".h",
        ".hpp", ".c",    ".cc",   ".rs",   ".ts",  ".tsx", ".js",
        ".mjs", ".go",   ".sql",  ".toml", ".yaml", ".yml", ".xml",
        ".html", ".css",  ".ps1",  ".bat",  ".sh",  ".cmake",
    };
    for (const char* e : kExt) if (ext == e) return true;
    return false;
}

// Deterministic per-document language tag: script statistics first
// (CJK → zh-tw), then extension for programming languages, then digit
// density for math, else en/other.
std::string corpus_lang(const std::string& text, const std::string& ext) {
    const size_t span = std::min<size_t>(text.size(), 64 * 1024);
    int64_t cjk = 0, alpha = 0, digit = 0, total = 0;
    for (size_t i = 0; i < span;) {
        unsigned char c = (unsigned char)text[i];
        uint32_t cp = c; size_t len = 1;
        if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; len = 2; }
        else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; len = 3; }
        else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; len = 4; }
        for (size_t k = 1; k < len && i + k < span; ++k)
            cp = (cp << 6) | ((unsigned char)text[i + k] & 0x3F);
        i += len;
        ++total;
        if ((cp >= 0x4E00 && cp <= 0x9FFF) ||
            (cp >= 0x3400 && cp <= 0x4DBF) ||
            (cp >= 0xF900 && cp <= 0xFAFF))
            ++cjk;
        else if ((cp >= 'a' && cp <= 'z') || (cp >= 'A' && cp <= 'Z'))
            ++alpha;
        else if (cp >= '0' && cp <= '9')
            ++digit;
    }
    if (total > 0 && cjk * 20 > total) return "zh-tw";
    static const char* kCodeExt[] = {
        ".cs", ".cpp", ".h", ".hpp", ".c", ".cc", ".rs", ".ts",
        ".tsx", ".js", ".mjs", ".py", ".go", ".sql", ".ps1", ".bat",
        ".sh", ".cmake",
    };
    for (const char* e : kCodeExt) if (ext == e) return "code";
    if (total > 0 && digit * 4 > alpha) return "math";
    return alpha ? "en" : "other";
}

uint64_t corpus_fnv(const std::string& s) {
    uint64_t h = 1469598103934665603ULL;
    for (unsigned char c : s) { h ^= c; h *= 1099511628211ULL; }
    return h;
}

uint64_t corpus_mix(uint64_t x) {
    x ^= x >> 30; x *= 0xBF58476D1CE4E5B9ULL;
    x ^= x >> 27; x *= 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

// 64-lane MinHash signature over whitespace word 5-grams; band width 4
// (16 bands). A shared band ≈ Jaccard ≥0.65 near-duplicate.
std::vector<uint64_t> corpus_sig(const std::string& text) {
    std::vector<uint64_t> sig(64, ~0ULL);
    std::vector<uint64_t> grams;
    grams.reserve(64);
    size_t i = 0;
    while (i < text.size()) {
        uint64_t g = 1469598103934665603ULL;
        int words = 0;
        while (i < text.size() && words < 5) {
            while (i < text.size() &&
                   (unsigned char)text[i] <= ' ') ++i;
            if (i >= text.size()) break;
            size_t b = i;
            while (i < text.size() &&
                   (unsigned char)text[i] > ' ') ++i;
            for (size_t k = b; k < i; ++k) {
                g ^= (unsigned char)text[k];
                g *= 1099511628211ULL;
            }
            g ^= 0xFF; g *= 1099511628211ULL;
            ++words;
        }
        if (words == 5) grams.push_back(g);
        if (grams.size() > 4000) break;  // bounded per doc
    }
    if (grams.empty()) grams.push_back(corpus_fnv(text));
    for (uint64_t g : grams) {
        for (int s = 0; s < 64; ++s) {
            uint64_t v = corpus_mix(g ^ (0x9E3779B97F4A7C15ULL * (s + 1)));
            if (v < sig[(size_t)s]) sig[(size_t)s] = v;
        }
    }
    return sig;
}

int mode_corpus(const Args& a) {
    std::string registry_path = a.get("registry");
    std::string root = a.get("root");
    std::string tk_path = resolve_tokenizer_path(a.get("tokenizer"));
    fs::path out_dir = a.get("out");
    if (registry_path.empty() || root.empty() || a.get("tokenizer").empty() ||
        out_dir.empty())
        fail("CORPUS_ARGS_MISSING");
    const int64_t max_len =
        a.has("max-len") ? std::stoll(a.get("max-len")) : 1024;
    const int64_t val_pct =
        a.has("val-ratio") ? std::stoll(a.get("val-ratio")) : 5;
    const int64_t max_docs =
        a.has("max-docs") ? std::stoll(a.get("max-docs")) : 20000;
    const int64_t max_doc_chars =
        a.has("max-doc-chars") ? std::stoll(a.get("max-doc-chars"))
                               : 1000000;
    const int64_t max_tokens =
        a.has("max-tokens") ? std::stoll(a.get("max-tokens")) : 50000000;
    if (max_len <= 0 || val_pct < 0 || val_pct >= 50)
        fail("CORPUS_BAD_ARGS");

    // ---- registry gate (elements 1/5): enabled + licensed + cleared ---
    JsonValue reg = parse_json_file(registry_path);
    const JsonValue* sources = reg.get("sources");
    if (!sources || sources->type != JsonValue::Type::Array)
        fail("CORPUS_REGISTRY_INVALID");
    std::vector<CorpusSource> allowed;
    std::vector<std::string> rejected_sources;
    for (const auto& s : sources->array) {
        bool enabled = false;
        if (const JsonValue* e = s.get("enabled"))
            enabled = e->type == JsonValue::Type::Bool && e->boolean;
        std::string license = jget_str(s, "license");
        std::string sens = jget_str(s, "sensitivity");
        bool sens_ok = sens == "internal" || sens == "public";
        if (!enabled || license.empty() || !sens_ok) {
            rejected_sources.push_back(jget_str(s, "source_id"));
            continue;
        }
        CorpusSource cs;
        cs.id = jget_str(s, "source_id");
        cs.path = jget_str(s, "path");
        cs.owner = jget_str(s, "owner");
        cs.license = license;
        cs.language = jget_str(s, "language");
        cs.sensitivity = sens;
        if (cs.id.empty() || cs.path.empty()) {
            rejected_sources.push_back(cs.id.empty() ? "?" : cs.id);
            continue;
        }
        allowed.push_back(std::move(cs));
    }
    if (allowed.empty()) fail("CORPUS_NO_SOURCES");

    ByteLevelBPETokenizer tk = ByteLevelBPETokenizer::load(tk_path);
    std::vector<int64_t> eos_probe = tk.encode("", false, true);
    const int64_t eos_id = eos_probe.empty() ? -1 : eos_probe.back();

    // ---- scan (elements 4/7/9/10/11 groundwork) ----------------------
    struct Cand { std::string abs, rel; CorpusSource* src; };
    std::vector<Cand> cands;
    int64_t denied_paths = 0, scanned_files = 0;
    for (auto& src : allowed) {
        fs::path base = fs::path(root) / src.path;
        std::error_code ec;
        fs::recursive_directory_iterator it(
            base, fs::directory_options::skip_permission_denied, ec);
        fs::recursive_directory_iterator end;
        for (; it != end; it.increment(ec)) {
            if (ec) { ec.clear(); continue; }
            fs::path p = it->path();
            std::string rel =
                fs::relative(p, fs::path(root), ec).generic_string();
            if (ec) { ec.clear(); continue; }
            std::string low = rel;
            for (auto& ch : low) ch = (char)corpus_lc(ch);
            if (it->is_directory(ec)) {
                // Prune denied subtrees before descent (.worktrees,
                // node_modules, runtime state, codex mirror dirs).
                if (corpus_denied(low + "/")) {
                    ++denied_paths;
                    it.disable_recursion_pending();
                }
                continue;
            }
            if (!it->is_regular_file(ec)) continue;
            if (corpus_denied(low)) { ++denied_paths; continue; }
            std::string ext = p.extension().string();
            for (auto& ch : ext) ch = (char)corpus_lc(ch);
            if (!corpus_text_ext(ext)) continue;
            cands.push_back({p.string(), rel, &src});
        }
    }
    std::sort(cands.begin(), cands.end(),
              [](const Cand& x, const Cand& y) { return x.rel < y.rel; });
    scanned_files = (int64_t)cands.size();

    // ---- per-document pipeline (elements 3/6/7/8/9/10) ---------------
    // §22 incremental scan: a content-hash file cache under out_dir
    // (corpus-cache.jsonl, star-corpus-file-cache/v1) lets unchanged
    // files skip re-parse entirely — stat size+mtime must match the
    // cached record AND the raw-content sha is reused verbatim. Changed
    // or uncached files go through the full pipeline on a bounded worker
    // pool (--jobs N, default min(8, hw)); results merge back in sorted
    // relpath order so output stays bit-deterministic.
    struct FileCacheRec {
        bool present = false, skip = false;
        int64_t size = 0, mtime = 0;
        CorpusDoc doc;
        std::vector<uint64_t> band_keys;
    };
    // Binary-hot-data rule: cached token ids are base64 little-endian
    // i32 — a binary payload inside the JSONL metadata envelope, never
    // a JSON number array. Encode failure (id out of i32 range) omits
    // the field so the next run re-parses; decode failure behaves as a
    // cache miss — both fail-safe, never silently wrong ids.
    static const char kB64[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    auto b64_enc_i32 = [](const std::vector<int64_t>& ids,
                          std::string& out) -> bool {
        std::string bytes;
        bytes.reserve(ids.size() * 4);
        for (int64_t t : ids) {
            if (t < std::numeric_limits<int32_t>::min() ||
                t > std::numeric_limits<int32_t>::max())
                return false;
            const int32_t v = (int32_t)t;
            bytes.append(reinterpret_cast<const char*>(&v), 4);
        }
        out.clear();
        out.reserve((bytes.size() + 2) / 3 * 4);
        const unsigned char* p =
            reinterpret_cast<const unsigned char*>(bytes.data());
        for (size_t i = 0; i < bytes.size(); i += 3) {
            const size_t rem = bytes.size() - i;
            const uint32_t n = ((uint32_t)p[i] << 16) |
                               (rem > 1 ? (uint32_t)p[i + 1] << 8 : 0) |
                               (rem > 2 ? (uint32_t)p[i + 2] : 0);
            out.push_back(kB64[(n >> 18) & 63]);
            out.push_back(kB64[(n >> 12) & 63]);
            out.push_back(rem > 1 ? kB64[(n >> 6) & 63] : '=');
            out.push_back(rem > 2 ? kB64[n & 63] : '=');
        }
        return true;
    };
    auto b64_dec_i32 = [](const std::string& s,
                          std::vector<int64_t>& out) -> bool {
        if (s.size() % 4 != 0) return false;
        auto val = [](char c) -> int {
            if (c >= 'A' && c <= 'Z') return c - 'A';
            if (c >= 'a' && c <= 'z') return c - 'a' + 26;
            if (c >= '0' && c <= '9') return c - '0' + 52;
            if (c == '+') return 62;
            if (c == '/') return 63;
            return -1;
        };
        std::string bytes;
        const size_t chunks = s.size() / 4;
        bytes.reserve(chunks * 3);
        for (size_t i = 0; i < chunks; ++i) {
            const bool last = i == chunks - 1;
            size_t pad = 0;
            if (last)
                for (size_t j = 0; j < 4 && s[i * 4 + 3 - j] == '='; ++j)
                    ++pad;
            if (pad > 2) return false;
            uint32_t n = 0;
            for (size_t j = 0; j < 4; ++j) {
                const char c = s[i * 4 + j];
                if (c == '=') {
                    if (!last || j < 4 - pad) return false;
                } else {
                    if (pad > 0 && j >= 4 - pad) return false;
                    const int v = val(c);
                    if (v < 0) return false;
                    n |= (uint32_t)v << (18 - 6 * j);
                }
            }
            bytes.push_back((char)(n >> 16));
            if (pad < 2) bytes.push_back((char)(n >> 8));
            if (pad < 1) bytes.push_back((char)n);
        }
        if (bytes.size() % 4 != 0) return false;
        out.clear();
        out.reserve(bytes.size() / 4);
        for (size_t i = 0; i + 4 <= bytes.size(); i += 4) {
            int32_t v;
            std::memcpy(&v, bytes.data() + i, 4);
            out.push_back((int64_t)v);
        }
        return true;
    };
    std::unordered_map<std::string, FileCacheRec> fcache;
    {
        const fs::path cp = out_dir / "corpus-cache.jsonl";
        std::ifstream cf(cp, std::ios::binary);
        if (cf) {
            std::string line;
            while (std::getline(cf, line)) {
                if (line.empty()) continue;
                JsonValue v;
                try { JsonParser p(line); v = p.parse(); }
                catch (...) { continue; }
                const JsonValue* fv = v.get("format");
                if (!fv || fv->type != JsonValue::Type::String ||
                    fv->string != "star-corpus-file-cache/v1")
                    continue;
                const JsonValue* rv = v.get("rel");
                if (!rv || rv->type != JsonValue::Type::String) continue;
                FileCacheRec r;
                r.present = true;
                r.size = (int64_t)xct::j_num(&v, "size", -1);
                // mtime is stored as a string: file-time ticks exceed
                // double precision and must round-trip exactly.
                const JsonValue* mv = v.get("mtime");
                if (mv && mv->type == JsonValue::Type::String)
                    r.mtime = std::atoll(mv->string.c_str());
                else if (mv && mv->type == JsonValue::Type::Number)
                    r.mtime = (int64_t)mv->number;
                const JsonValue* sv = v.get("status");
                r.skip = sv && sv->type == JsonValue::Type::String &&
                         sv->string == "skip";
                if (!r.skip) {
                    auto js = [&](const char* k) -> std::string {
                        const JsonValue* x = v.get(k);
                        return x && x->type == JsonValue::Type::String
                                   ? x->string : "";
                    };
                    r.doc.source_id = js("source_id");
                    r.doc.relpath = rv->string;
                    r.doc.sha_raw = js("sha_raw");
                    r.doc.sha_nfc = js("sha_nfc");
                    r.doc.overlap_sha = js("overlap_sha");
                    r.doc.language = js("language");
                    r.doc.split = js("split");
                    const JsonValue* nc = v.get("nfc_changed");
                    r.doc.nfc_changed =
                        nc && nc->type == JsonValue::Type::Bool &&
                        nc->boolean;
                    const JsonValue* bk = v.get("band_keys");
                    if (bk && bk->type == JsonValue::Type::Array) {
                        for (const auto& h : bk->array) {
                            if (h.type == JsonValue::Type::String) {
                                uint64_t u = 0;
                                std::from_chars(
                                    h.string.data(),
                                    h.string.data() + h.string.size(),
                                    u, 16);
                                r.band_keys.push_back(u);
                            }
                        }
                    }
                    // Binary-hot-data rule: ids_b64 (base64 LE-i32) is
                    // the governed field; the legacy `ids` JSON array
                    // remains readable so pre-XCB1 caches still hit.
                    const JsonValue* ib = v.get("ids_b64");
                    if (ib && ib->type == JsonValue::Type::String) {
                        if (!b64_dec_i32(ib->string, r.doc.ids))
                            r.present = false;  // malformed → cache miss
                    } else {
                        const JsonValue* iv = v.get("ids");
                        if (iv && iv->type == JsonValue::Type::Array) {
                            r.doc.ids.reserve(iv->array.size());
                            for (const auto& t : iv->array)
                                if (t.type == JsonValue::Type::Number)
                                    r.doc.ids.push_back(
                                        (int64_t)t.number);
                        }
                    }
                }
                fcache[rv->string] = std::move(r);
            }
        }
    }

    int64_t jobs = 0;
    {
        const std::string js = a.get("jobs");
        if (!js.empty()) jobs = std::atoll(js.c_str());
    }
    if (jobs <= 0)
        jobs = std::min<int64_t>(
            8, (int64_t)std::thread::hardware_concurrency());
    if (jobs < 1) jobs = 1;

    struct ScanResult {
        enum class Kind { Pending, Cached, Doc, Skip } kind =
            Kind::Pending;
        CorpusDoc doc;
        std::vector<uint64_t> band_keys;
        FileCacheRec rec;          // record to persist into new cache
        bool cache_hit = false;
        bool cache_drop = false;   // deduped — persist no cache record
        int skip_why = -1;         // 0 unreadable, 1 empty, 2 binary
    };
    std::vector<ScanResult> results(cands.size());
    int64_t cache_hits = 0, reparsed = 0;

    // Stage 1 (metadata gate): unchanged files reuse the cached derived
    // record — no read, no BPE, no NFC.
    std::vector<size_t> work;
    for (size_t i = 0; i < cands.size(); ++i) {
        std::error_code ec;
        const int64_t sz = (int64_t)fs::file_size(cands[i].abs, ec);
        if (ec) { results[i].kind = ScanResult::Kind::Skip; continue; }
        const int64_t mt = (int64_t)fs::last_write_time(cands[i].abs, ec)
                               .time_since_epoch()
                               .count();
        if (ec) { results[i].kind = ScanResult::Kind::Skip; continue; }
        auto it = fcache.find(cands[i].rel);
        if (it != fcache.end() && it->second.size == sz &&
            it->second.mtime == mt) {
            results[i].kind = it->second.skip ? ScanResult::Kind::Skip
                                              : ScanResult::Kind::Cached;
            results[i].doc = it->second.doc;
            results[i].band_keys = it->second.band_keys;
            results[i].cache_hit = true;
            ++cache_hits;
            continue;
        }
        work.push_back(i);
    }
    reparsed = (int64_t)work.size();

    // Stage 2 (bounded parallel): full parse for new/changed files.
    // Workers own disjoint result slots; tokenizers read immutable
    // tables so encode() is safe to share.
    {
        std::atomic<size_t> next{0};
        auto worker = [&]() {
            while (true) {
                const size_t w = next.fetch_add(1);
                if (w >= work.size()) break;
                const size_t i = work[w];
                const Cand& c = cands[i];
                ScanResult& R = results[i];
                std::error_code ec;
                R.rec.size = (int64_t)fs::file_size(c.abs, ec);
                R.rec.mtime = (int64_t)fs::last_write_time(c.abs, ec)
                                  .time_since_epoch()
                                  .count();
                std::ifstream f(c.abs, std::ios::binary);
                if (!f) { R.kind = ScanResult::Kind::Skip;
                          R.skip_why = 0; R.rec.skip = true; continue; }
                std::ostringstream ss; ss << f.rdbuf();
                std::string raw = ss.str();
                if (raw.empty()) { R.kind = ScanResult::Kind::Skip;
                                   R.skip_why = 1; R.rec.skip = true;
                                   continue; }
                if ((int64_t)raw.size() > max_doc_chars)
                    raw.resize((size_t)max_doc_chars);
                if (raw.find('\0', 0) != std::string::npos) {
                    R.kind = ScanResult::Kind::Skip;
                    R.skip_why = 2; R.rec.skip = true;
                    continue;
                }
                CorpusDoc d;
                d.source_id = c.src->id;
                d.relpath = c.rel;
                d.sha_raw = sha256_text(raw);
                std::string norm = nfc(raw);
                d.nfc_changed = norm != raw;
                d.sha_nfc = sha256_text(norm);
                d.language = corpus_lang(
                    norm, fs::path(c.rel).extension().string());
                d.overlap_sha = norm_text_sha(norm);
                d.ids = tk.encode(norm, true, false);
                if (eos_id >= 0) d.ids.push_back(eos_id);
                if (d.ids.empty()) {
                    R.kind = ScanResult::Kind::Skip;
                    R.skip_why = 1; R.rec.skip = true;
                    continue;
                }
                uint64_t kh = corpus_fnv(d.source_id + ":" + d.relpath);
                d.split = (kh % 1000 < (uint64_t)(val_pct * 10))
                              ? "valid" : "train";
                const std::vector<uint64_t> sig = corpus_sig(norm);
                for (int b = 0; b < 16; ++b) {
                    R.band_keys.push_back(corpus_mix(
                        sig[(size_t)(b * 4)] ^ sig[(size_t)(b * 4 + 1)] ^
                        (sig[(size_t)(b * 4 + 2)] << 1) ^
                        (sig[(size_t)(b * 4 + 3)] >> 1) ^ (uint64_t)b));
                }
                R.doc = std::move(d);
                R.rec.doc = R.doc;
                R.rec.band_keys = R.band_keys;
                R.kind = ScanResult::Kind::Doc;
            }
        };
        if (work.empty()) {
            // all cached
        } else if (jobs <= 1) {
            worker();
        } else {
            std::vector<std::thread> pool;
            for (int64_t t = 0; t < jobs; ++t) pool.emplace_back(worker);
            for (auto& t : pool) t.join();
        }
    }

    // Stage 3 (deterministic merge): exact + MinHash dedup in sorted
    // relpath order — identical to the serial pipeline's semantics.
    std::vector<CorpusDoc> docs;
    std::unordered_set<std::string> seen_sha;
    std::unordered_map<uint64_t, std::vector<int64_t>> bands;
    int64_t exact_dup = 0, near_dup = 0, empty_docs = 0,
            unreadable = 0, total_tokens = 0;
    std::unordered_map<std::string, int64_t> lang_counts;
    for (size_t i = 0; i < cands.size(); ++i) {
        ScanResult& R = results[i];
        if (R.kind == ScanResult::Kind::Skip) {
            if (R.skip_why == 0) ++unreadable;
            else if (R.skip_why == 1) ++empty_docs;
            continue;
        }
        if ((int64_t)docs.size() >= max_docs ||
            total_tokens >= max_tokens)
            break;
        if (!seen_sha.insert(R.doc.sha_nfc).second) {
            ++exact_dup;
            // Deduped losers get no cache record: if the winner is
            // removed later, this file must re-parse on the next run.
            R.kind = ScanResult::Kind::Skip;
            R.cache_drop = true;
            continue;
        }
        bool dup = false;
        for (uint64_t key : R.band_keys)
            if (bands.count(key)) { dup = true; break; }
        if (dup) {
            ++near_dup;
            R.kind = ScanResult::Kind::Skip;
            R.cache_drop = true;
            continue;
        }
        for (uint64_t key : R.band_keys)
            bands[key].push_back((int64_t)docs.size());
        total_tokens += (int64_t)R.doc.ids.size();
        lang_counts[R.doc.language]++;
        docs.push_back(std::move(R.doc));
        if (R.cache_hit) R.rec = fcache[cands[i].rel];  // keep verbatim
    }

    // ---- emit (elements 2/8/9/11): packed ids + records + manifest ---
    // Binary-hot-data rule: packed token ids ship as XCB1 containers
    // (train-ids.xcb / valid-ids.xcb); the *-records/documents JSONL
    // remain — they are provenance manifests, not token batches.
    fs::create_directories(out_dir);
    fs::path train_path = out_dir / "train-ids.xcb";
    fs::path valid_path = out_dir / "valid-ids.xcb";
    fs::path docs_path = out_dir / "documents.jsonl";
    fs::path trec_path = out_dir / "train-records.jsonl";
    fs::path vrec_path = out_dir / "valid-records.jsonl";
    fs::path manifest_path = out_dir / "manifest.json";
    std::ofstream train(train_path, std::ios::binary | std::ios::trunc);
    std::ofstream valid(valid_path, std::ios::binary | std::ios::trunc);
    std::ofstream drec(docs_path, std::ios::binary | std::ios::trunc);
    // Per-split hash records consumed by the capability eval's
    // fail-closed corpus-overlap gate (--corpus-manifest train/val).
    std::ofstream trec(trec_path, std::ios::binary | std::ios::trunc);
    std::ofstream vrec(vrec_path, std::ios::binary | std::ios::trunc);
    if (!train || !valid || !drec || !trec || !vrec)
        fail("CORPUS_OUT_UNWRITABLE");
    const std::string ids_meta =
        std::string("{\"format\":\"star-token-batch/v1\",")
        + "\"producer\":\"xcm_corpus\",\"packing_max_len\":"
        + std::to_string(max_len) + ",\"tokenizer_sha256\":\""
        + sha256_file(tk_path) + "\"}";
    xcb::Writer xw_train(train, ids_meta);
    xcb::Writer xw_valid(valid, ids_meta);
    int64_t train_rows = 0, valid_rows = 0, train_toks = 0, valid_toks = 0;
    std::vector<int64_t> pack;
    auto flush = [&](xcb::Writer& xw, int64_t& rows, int64_t& toks) {
        if (pack.empty()) return;
        xcb::Record rec;
        rec.kind = xcb::Kind::kPretrain;
        rec.ids.reserve(pack.size());
        for (int64_t t : pack) {
            if (t < std::numeric_limits<int32_t>::min() ||
                t > std::numeric_limits<int32_t>::max())
                fail("CORPUS_TOKEN_RANGE");
            rec.ids.push_back(static_cast<int32_t>(t));
        }
        xw.add(rec);
        toks += (int64_t)pack.size();
        ++rows;
        pack.clear();
    };
    for (const CorpusDoc& d : docs) {
        xcb::Writer& dst = d.split == "valid" ? xw_valid : xw_train;
        int64_t& rows = d.split == "valid" ? valid_rows : train_rows;
        int64_t& toks = d.split == "valid" ? valid_toks : train_toks;
        size_t off = 0;
        while (off < d.ids.size()) {
            size_t room = (size_t)max_len - pack.size();
            size_t take = std::min(room, d.ids.size() - off);
            pack.insert(pack.end(), d.ids.begin() + (ptrdiff_t)off,
                        d.ids.begin() + (ptrdiff_t)(off + take));
            off += take;
            if ((int64_t)pack.size() >= max_len) flush(dst, rows, toks);
        }
        drec << "{\"source_id\":\"" << gptbridge::jsonlite::json_escape(d.source_id)
             << "\",\"path\":\"" << gptbridge::jsonlite::json_escape(d.relpath)
             << "\",\"sha256_raw\":\"" << d.sha_raw
             << "\",\"sha256_nfc\":\"" << d.sha_nfc
             << "\",\"nfc_changed\":" << (d.nfc_changed ? "true" : "false")
             << ",\"language\":\"" << d.language
             << "\",\"tokens\":" << (int64_t)d.ids.size()
             << ",\"split\":\"" << d.split << "\"}\n";
        std::ofstream& ovl = d.split == "valid" ? vrec : trec;
        ovl << "{\"path\":\"" << gptbridge::jsonlite::json_escape(d.relpath)
            << "\",\"sha256\":\"" << d.overlap_sha << "\"}\n";
    }
    flush(xw_train, train_rows, train_toks);
    flush(xw_valid, valid_rows, valid_toks);
    xw_train.close();
    xw_valid.close();
    train.close(); valid.close(); drec.close();
    trec.close(); vrec.close();

    // Persist the incremental file cache (star-corpus-file-cache/v1):
    // one line per candidate — doc records carry the derived fields and
    // band keys so unchanged files need zero re-parse next run; skip
    // records carry only metadata. Deduped files are never recorded.
    {
        std::ofstream cc(out_dir / "corpus-cache.jsonl",
                         std::ios::binary | std::ios::trunc);
        if (!cc) fail("CORPUS_OUT_UNWRITABLE");
        for (size_t i = 0; i < cands.size(); ++i) {
            const ScanResult& R = results[i];
            if (R.cache_drop) continue;
            if (R.kind == ScanResult::Kind::Skip && !R.rec.skip)
                continue;  // transient stat failure — not cacheable
            if (R.kind != ScanResult::Kind::Doc &&
                R.kind != ScanResult::Kind::Cached &&
                R.kind != ScanResult::Kind::Skip)
                continue;
            const FileCacheRec& rec = R.rec;
            cc << "{\"format\":\"star-corpus-file-cache/v1\""
               << ",\"rel\":\""
               << gptbridge::jsonlite::json_escape(cands[i].rel)
               << "\",\"source_id\":\""
               << gptbridge::jsonlite::json_escape(rec.doc.source_id)
               << "\",\"size\":" << rec.size
               << ",\"mtime\":\"" << rec.mtime << "\"";
            if (R.kind == ScanResult::Kind::Skip) {
                cc << ",\"status\":\"skip\"}\n";
                continue;
            }
            cc << ",\"status\":\"doc\""
               << ",\"sha_raw\":\"" << rec.doc.sha_raw
               << "\",\"sha_nfc\":\"" << rec.doc.sha_nfc
               << "\",\"overlap_sha\":\"" << rec.doc.overlap_sha
               << "\",\"language\":\"" << rec.doc.language
               << "\",\"split\":\"" << rec.doc.split
               << "\",\"nfc_changed\":"
               << (rec.doc.nfc_changed ? "true" : "false")
               << ",\"band_keys\":[";
            for (size_t b = 0; b < rec.band_keys.size(); ++b) {
                if (b) cc << ',';
                char hx[17];
                std::snprintf(hx, sizeof(hx), "%016llx",
                              (unsigned long long)rec.band_keys[b]);
                cc << '"' << hx << '"';
            }
            std::string enc;
            if (b64_enc_i32(rec.doc.ids, enc))
                cc << "],\"ids_b64\":\"" << enc << "\"}\n";
            else
                cc << "]}\n";  // out-of-range id → uncacheable, reparse
        }
        if (!cc) fail("CORPUS_OUT_UNWRITABLE");
    }

    std::string docs_sha = sha256_file(docs_path.string());
    std::ostringstream mf;
    mf << "{\"schema_version\":\"star-pretrain-corpus/v1\""
       << ",\"dataset_version\":\"sha256:" << docs_sha << "\""
       << ",\"created_at\":" << (int64_t)std::chrono::duration_cast<
              std::chrono::seconds>(
              std::chrono::system_clock::now().time_since_epoch()).count()
       << ",\"root\":\"" << gptbridge::jsonlite::json_escape(fs::absolute(root).string())
       << "\",\"registry\":\"" << gptbridge::jsonlite::json_escape(registry_path)
       << "\",\"tokenizer_sha256\":\"" << sha256_file(tk_path) << "\""
       << ",\"counts\":{\"sources_allowed\":" << (int64_t)allowed.size()
       << ",\"sources_rejected\":" << (int64_t)rejected_sources.size()
       << ",\"files_scanned\":" << scanned_files
       << ",\"cache_hits\":" << cache_hits
       << ",\"files_reparsed\":" << reparsed
       << ",\"scan_jobs\":" << jobs
       << ",\"paths_denied\":" << denied_paths
       << ",\"docs_kept\":" << (int64_t)docs.size()
       << ",\"exact_duplicates\":" << exact_dup
       << ",\"near_duplicates\":" << near_dup
       << ",\"empty_skipped\":" << empty_docs
       << ",\"unreadable_skipped\":" << unreadable
       << ",\"tokens_total\":" << total_tokens << "}"
       << ",\"languages\":{";
    {
        std::vector<std::pair<std::string, int64_t>> lv(
            lang_counts.begin(), lang_counts.end());
        std::sort(lv.begin(), lv.end());
        for (size_t i = 0; i < lv.size(); ++i) {
            if (i) mf << ',';
            mf << '"' << lv[i].first << "\":" << lv[i].second;
        }
    }
    mf << "},\"rejected_sources\":[";
    for (size_t i = 0; i < rejected_sources.size(); ++i) {
        if (i) mf << ',';
        mf << '"' << gptbridge::jsonlite::json_escape(rejected_sources[i]) << '"';
    }
    mf << "],\"train\":{\"path\":\"train-records.jsonl\"}"
       << ",\"val\":{\"path\":\"valid-records.jsonl\"}"
       << ",\"split\":{\"val_ratio_pct\":" << val_pct
       << ",\"train_rows\":" << train_rows
       << ",\"valid_rows\":" << valid_rows
       << ",\"train_tokens\":" << train_toks
       << ",\"valid_tokens\":" << valid_toks << "}"
       << ",\"packing\":{\"max_len\":" << max_len
       << ",\"eos_separator\":" << eos_id << "}"
       << ",\"integrity\":{\"documents_jsonl_sha256\":\"" << docs_sha
       << "\",\"train_ids_sha256\":\"" << sha256_file(train_path.string())
       << "\",\"valid_ids_sha256\":\"" << sha256_file(valid_path.string())
       << "\",\"train_records_sha256\":\"" << sha256_file(trec_path.string())
       << "\",\"valid_records_sha256\":\"" << sha256_file(vrec_path.string())
       << "\"}}"
       << "\n";
    {
        std::ofstream mfo(manifest_path, std::ios::binary | std::ios::trunc);
        if (!mfo) fail("CORPUS_MANIFEST_UNWRITABLE");
        mfo << mf.str();
    }

    std::printf(
        "{\"ok\":true,\"mode\":\"corpus\",\"out\":\"%s\","
        "\"dataset_version\":\"sha256:%s\",\"docs_kept\":%lld,"
        "\"paths_denied\":%lld,\"exact_duplicates\":%lld,"
        "\"near_duplicates\":%lld,\"tokens_total\":%lld,"
        "\"train_rows\":%lld,\"valid_rows\":%lld}\n",
        gptbridge::jsonlite::json_escape(out_dir.string()).c_str(), docs_sha.c_str(),
        (long long)docs.size(), (long long)denied_paths,
        (long long)exact_dup, (long long)near_dup,
        (long long)total_tokens, (long long)train_rows,
        (long long)valid_rows);
    return 0;
}
