// xcm_corpus.h — C108 star-pretrain-corpus/v1: governed unsupervised
// pretraining corpus pipeline (native lane). Included once by
// xc_modeltool.cpp inside the anonymous namespace, after the shared
// helpers (slurp/nfc/sha256/JsonValue) it uses.
//
//   corpus --registry <corpus-registry.json> --root <repo-root>
//          --tokenizer <path-or-dir> --out <dir>
//          [--max-len N=1024] [--val-ratio PCT=5] [--max-docs N=20000]
//          [--max-doc-chars N=1000000] [--max-tokens N=50000000]
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
    std::vector<CorpusDoc> docs;
    std::unordered_set<std::string> seen_sha;
    std::unordered_map<uint64_t, std::vector<int64_t>> bands;
    int64_t exact_dup = 0, near_dup = 0, empty_docs = 0,
            unreadable = 0, total_tokens = 0;
    std::unordered_map<std::string, int64_t> lang_counts;
    for (const Cand& c : cands) {
        if ((int64_t)docs.size() >= max_docs ||
            total_tokens >= max_tokens)
            break;
        std::ifstream f(c.abs, std::ios::binary);
        if (!f) { ++unreadable; continue; }
        std::ostringstream ss; ss << f.rdbuf();
        std::string raw = ss.str();
        if (raw.empty()) { ++empty_docs; continue; }
        if ((int64_t)raw.size() > max_doc_chars)
            raw.resize((size_t)max_doc_chars);
        if (raw.find('\0', 0) != std::string::npos) continue;  // binary
        CorpusDoc d;
        d.source_id = c.src->id;
        d.relpath = c.rel;
        d.sha_raw = sha256_text(raw);
        std::string norm = nfc(raw);
        d.nfc_changed = norm != raw;
        d.sha_nfc = sha256_text(norm);
        if (!seen_sha.insert(d.sha_nfc).second) { ++exact_dup; continue; }
        std::vector<uint64_t> sig = corpus_sig(norm);
        bool dup = false;
        for (int b = 0; b < 16 && !dup; ++b) {
            uint64_t key = corpus_mix(
                sig[(size_t)(b * 4)] ^ sig[(size_t)(b * 4 + 1)] ^
                (sig[(size_t)(b * 4 + 2)] << 1) ^
                (sig[(size_t)(b * 4 + 3)] >> 1) ^ (uint64_t)b);
            dup = bands.count(key) != 0;
        }
        if (dup) { ++near_dup; continue; }
        for (int b = 0; b < 16; ++b) {
            uint64_t key = corpus_mix(
                sig[(size_t)(b * 4)] ^ sig[(size_t)(b * 4 + 1)] ^
                (sig[(size_t)(b * 4 + 2)] << 1) ^
                (sig[(size_t)(b * 4 + 3)] >> 1) ^ (uint64_t)b);
            bands[key].push_back((int64_t)docs.size());
        }
        d.language = corpus_lang(norm, fs::path(c.rel).extension().string());
        const std::string overlap_sha = norm_text_sha(norm);
        d.overlap_sha = overlap_sha;
        d.ids = tk.encode(norm, true, false);
        if (eos_id >= 0) d.ids.push_back(eos_id);
        if (d.ids.empty()) { ++empty_docs; continue; }
        total_tokens += (int64_t)d.ids.size();
        uint64_t kh = corpus_fnv(d.source_id + ":" + d.relpath);
        d.split = (kh % 1000 < (uint64_t)(val_pct * 10)) ? "valid" : "train";
        lang_counts[d.language]++;
        docs.push_back(std::move(d));
    }

    // ---- emit (elements 2/8/9/11): packed ids + records + manifest ---
    fs::create_directories(out_dir);
    fs::path train_path = out_dir / "train-ids.jsonl";
    fs::path valid_path = out_dir / "valid-ids.jsonl";
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
    int64_t train_rows = 0, valid_rows = 0, train_toks = 0, valid_toks = 0;
    std::vector<int64_t> pack;
    auto flush = [&](std::ofstream& out, int64_t& rows, int64_t& toks) {
        if (pack.empty()) return;
        std::string line = "{\"input_ids\":[";
        for (size_t i = 0; i < pack.size(); ++i) {
            if (i) line += ',';
            line += std::to_string(pack[(size_t)i]);
        }
        line += "]}\n";
        out << line;
        toks += (int64_t)pack.size();
        ++rows;
        pack.clear();
    };
    for (const CorpusDoc& d : docs) {
        std::ofstream& dst = d.split == "valid" ? valid : train;
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
    flush(train, train_rows, train_toks);
    flush(valid, valid_rows, valid_toks);
    train.close(); valid.close(); drec.close();
    trec.close(); vrec.close();

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
