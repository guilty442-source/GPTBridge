//! corpus.rs — the star-pretrain-corpus/v1 pipeline, a faithful port of
//! `mode_corpus` in xcm_corpus.h. Same stage order, same field order in
//! every emitted line, same accounting — outputs are byte-comparable
//! against the C++ lane given identical inputs.
//!
//!   xcorpus corpus --registry <json> --root <dir> --tokenizer <path-or-dir>
//!                  --out <dir> [--max-len N=1024] [--val-ratio PCT=5]
//!                  [--max-docs N=20000] [--max-doc-chars N=1000000]
//!                  [--max-tokens N=50000000] [--jobs N=min(8,hw)]

use crate::scan;
use crate::textutil;
use crate::tokenizer::ByteLevelBpeTokenizer;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

pub struct Args {
    pub registry: String,
    pub root: String,
    pub tokenizer: String,
    pub out: String,
    pub max_len: i64,
    pub val_pct: i64,
    pub max_docs: i64,
    pub max_doc_chars: i64,
    pub max_tokens: i64,
    pub jobs: i64,
}

struct CorpusSource {
    id: String,
    path: String,
    #[allow(dead_code)]
    owner: String,
    #[allow(dead_code)]
    license: String,
    #[allow(dead_code)]
    language: String,
    #[allow(dead_code)]
    sensitivity: String,
}

#[derive(Clone, Default)]
struct CorpusDoc {
    source_id: String,
    relpath: String,
    sha_raw: String,
    sha_nfc: String,
    overlap_sha: String,
    language: String,
    split: String,
    nfc_changed: bool,
    ids: Vec<i64>,
}

#[derive(Clone, Default)]
struct FileCacheRec {
    present: bool,
    skip: bool,
    size: i64,
    mtime: i64,
    doc: CorpusDoc,
    band_keys: Vec<u64>,
}

fn jstr<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("")
}

fn jnum(v: &Value, k: &str, dflt: i64) -> i64 {
    v.get(k).and_then(|x| x.as_i64()).unwrap_or(dflt)
}

fn lower(s: &str) -> String {
    s.chars()
        .map(|c| if c.is_ascii_uppercase() { c.to_ascii_lowercase() } else { c })
        .collect()
}

/// std::filesystem::file_time_type ticks (100ns since 1601-01-01) so
/// the cache format round-trips the same numbers the C++ lane writes.
fn filetime_ticks(md: &std::fs::Metadata) -> i64 {
    match md.modified() {
        Ok(t) => match t.duration_since(std::time::UNIX_EPOCH) {
            Ok(d) => {
                (d.as_secs() as i64) * 10_000_000
                    + (d.subsec_nanos() as i64) / 100
                    + 116_444_736_000_000_000i64
            }
            Err(_) => 0,
        },
        Err(_) => 0,
    }
}

fn load_cache(out_dir: &Path) -> HashMap<String, FileCacheRec> {
    let mut fcache = HashMap::new();
    let path = out_dir.join("corpus-cache.jsonl");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return fcache;
    };
    for line in text.lines() {
        if line.is_empty() {
            continue;
        }
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if v.get("format").and_then(|x| x.as_str())
            != Some("star-corpus-file-cache/v1")
        {
            continue;
        }
        let rel = match v.get("rel").and_then(|x| x.as_str()) {
            Some(r) => r.to_string(),
            None => continue,
        };
        let mut r = FileCacheRec {
            present: true,
            size: jnum(&v, "size", -1),
            ..Default::default()
        };
        r.mtime = v
            .get("mtime")
            .and_then(|x| {
                x.as_str()
                    .and_then(|s| s.parse::<i64>().ok())
                    .or_else(|| x.as_i64())
            })
            .unwrap_or(0);
        r.skip = v.get("status").and_then(|x| x.as_str()) == Some("skip");
        if !r.skip {
            r.doc.source_id = jstr(&v, "source_id").to_string();
            r.doc.relpath = rel.clone();
            r.doc.sha_raw = jstr(&v, "sha_raw").to_string();
            r.doc.sha_nfc = jstr(&v, "sha_nfc").to_string();
            r.doc.overlap_sha = jstr(&v, "overlap_sha").to_string();
            r.doc.language = jstr(&v, "language").to_string();
            r.doc.split = jstr(&v, "split").to_string();
            r.doc.nfc_changed =
                v.get("nfc_changed").and_then(|x| x.as_bool()).unwrap_or(false);
            if let Some(bk) = v.get("band_keys").and_then(|x| x.as_array()) {
                for h in bk {
                    if let Some(s) = h.as_str() {
                        if let Ok(u) = u64::from_str_radix(s, 16) {
                            r.band_keys.push(u);
                        }
                    }
                }
            }
            if let Some(ids) = v.get("ids").and_then(|x| x.as_array()) {
                r.doc.ids = ids
                    .iter()
                    .filter_map(|t| t.as_i64())
                    .collect();
            }
        }
        fcache.insert(rel, r);
    }
    fcache
}

// ---------------------------------------------------------- scanning --

struct Cand {
    abs: PathBuf,
    rel: String,
    src_idx: usize,
}

fn collect_candidates(
    root: &Path,
    allowed: &[CorpusSource],
) -> (Vec<Cand>, i64) {
    let mut cands = Vec::new();
    let mut denied_paths = 0i64;
    for (si, src) in allowed.iter().enumerate() {
        let base = root.join(&src.path);
        let mut stack = vec![base];
        while let Some(dir) = stack.pop() {
            let rd = match std::fs::read_dir(&dir) {
                Ok(r) => r,
                Err(_) => continue,
            };
            for ent in rd.flatten() {
                let p = ent.path();
                let rel = match p.strip_prefix(root) {
                    Ok(r) => r.to_string_lossy().replace('\\', "/"),
                    Err(_) => continue,
                };
                let low = lower(&rel);
                let ft = match ent.file_type() {
                    Ok(f) => f,
                    Err(_) => continue,
                };
                if ft.is_dir() {
                    if scan::corpus_denied(&format!("{low}/")) {
                        denied_paths += 1;
                        continue; // prune
                    }
                    stack.push(p);
                    continue;
                }
                if !ft.is_file() {
                    continue;
                }
                if scan::corpus_denied(&low) {
                    denied_paths += 1;
                    continue;
                }
                let ext = lower(
                    &p.extension()
                        .map(|e| format!(".{}", e.to_string_lossy()))
                        .unwrap_or_default(),
                );
                if !scan::corpus_text_ext(&ext) {
                    continue;
                }
                cands.push(Cand { abs: p, rel, src_idx: si });
            }
        }
    }
    cands.sort_by(|a, b| a.rel.cmp(&b.rel));
    (cands, denied_paths)
}

// --------------------------------------------------------- pipeline --

enum Kind {
    Pending,
    Cached,
    Doc,
    Skip,
}

struct ScanResult {
    kind: Kind,
    doc: CorpusDoc,
    band_keys: Vec<u64>,
    rec: FileCacheRec,
    cache_hit: bool,
    cache_drop: bool,
    skip_why: i32, // -1 none, 0 unreadable, 1 empty, 2 binary
}

impl Default for ScanResult {
    fn default() -> Self {
        Self {
            kind: Kind::Pending,
            doc: CorpusDoc::default(),
            band_keys: Vec::new(),
            rec: FileCacheRec::default(),
            cache_hit: false,
            cache_drop: false,
            skip_why: -1,
        }
    }
}

pub fn run(a: &Args) -> Result<String, String> {
    if a.registry.is_empty() || a.root.is_empty()
        || a.tokenizer.is_empty() || a.out.is_empty()
    {
        return Err("CORPUS_ARGS_MISSING".into());
    }
    if a.max_len <= 0 || a.val_pct < 0 || a.val_pct >= 50 {
        return Err("CORPUS_BAD_ARGS".into());
    }

    // ---- registry gate ----
    let reg_text = std::fs::read_to_string(&a.registry)
        .map_err(|e| format!("CORPUS_REGISTRY_INVALID: {e}"))?;
    let reg: Value = serde_json::from_str(&reg_text)
        .map_err(|e| format!("CORPUS_REGISTRY_INVALID: {e}"))?;
    let sources = reg
        .get("sources")
        .and_then(|s| s.as_array())
        .ok_or("CORPUS_REGISTRY_INVALID")?;
    let mut allowed: Vec<CorpusSource> = Vec::new();
    let mut rejected_sources: Vec<String> = Vec::new();
    for s in sources {
        let enabled =
            s.get("enabled").and_then(|x| x.as_bool()).unwrap_or(false);
        let license = jstr(s, "license");
        let sens = jstr(s, "sensitivity");
        let sens_ok = sens == "internal" || sens == "public";
        if !enabled || license.is_empty() || !sens_ok {
            rejected_sources.push(jstr(s, "source_id").to_string());
            continue;
        }
        let cs = CorpusSource {
            id: jstr(s, "source_id").to_string(),
            path: jstr(s, "path").to_string(),
            owner: jstr(s, "owner").to_string(),
            license: license.to_string(),
            language: jstr(s, "language").to_string(),
            sensitivity: sens.to_string(),
        };
        if cs.id.is_empty() || cs.path.is_empty() {
            rejected_sources
                .push(if cs.id.is_empty() { "?".into() } else { cs.id });
            continue;
        }
        allowed.push(cs);
    }
    if allowed.is_empty() {
        return Err("CORPUS_NO_SOURCES".into());
    }

    let mut tk_path = PathBuf::from(&a.tokenizer);
    if tk_path.is_dir() {
        tk_path = tk_path.join("tokenizer.json");
    }
    let tk = ByteLevelBpeTokenizer::load(&tk_path)?;
    let eos_probe = tk.encode(b"", false, true);
    let eos_id = eos_probe.last().copied().unwrap_or(-1);

    // ---- scan ----
    let root = PathBuf::from(&a.root);
    let (cands, denied_paths) = collect_candidates(&root, &allowed);
    let scanned_files = cands.len() as i64;

    let fcache = load_cache(Path::new(&a.out));

    let mut jobs = a.jobs;
    if jobs <= 0 {
        jobs = (std::thread::available_parallelism()
            .map(|n| n.get() as i64)
            .unwrap_or(1))
        .min(8);
    }
    if jobs < 1 {
        jobs = 1;
    }

    let mut results: Vec<Mutex<ScanResult>> =
        (0..cands.len()).map(|_| Mutex::new(ScanResult::default())).collect();
    let mut cache_hits = 0i64;

    // Stage 1: metadata gate — unchanged files reuse the cached record.
    let mut work: Vec<usize> = Vec::new();
    for i in 0..cands.len() {
        let md = match std::fs::metadata(&cands[i].abs) {
            Ok(m) => m,
            Err(_) => {
                results[i].get_mut().unwrap().kind = Kind::Skip;
                continue;
            }
        };
        let sz = md.len() as i64;
        let mt = filetime_ticks(&md);
        if let Some(rec) = fcache.get(&cands[i].rel) {
            if rec.present && rec.size == sz && rec.mtime == mt {
                let r = results[i].get_mut().unwrap();
                r.kind = if rec.skip { Kind::Skip } else { Kind::Cached };
                r.doc = rec.doc.clone();
                r.band_keys = rec.band_keys.clone();
                r.cache_hit = true;
                cache_hits += 1;
                continue;
            }
        }
        work.push(i);
    }
    let reparsed = work.len() as i64;

    // Stage 2: bounded parallel parse of new/changed files.
    {
        let next = AtomicUsize::new(0);
        let next = &next;
        let work_ref = &work;
        let cands_ref = &cands;
        let results_ref = &results;
        let tk_ref = &tk;
        let allowed_ref = &allowed;
        let max_doc_chars = a.max_doc_chars;
        let val_pct = a.val_pct;
        std::thread::scope(|scope| {
            for _ in 0..jobs {
                scope.spawn(move || loop {
                    let w = next.fetch_add(1, Ordering::Relaxed);
                    if w >= work_ref.len() {
                        break;
                    }
                    let i = work_ref[w];
                    let c = &cands_ref[i];
                    let mut r = results_ref[i].lock().unwrap();
                    if let Ok(md) = std::fs::metadata(&c.abs) {
                        r.rec.size = md.len() as i64;
                        r.rec.mtime = filetime_ticks(&md);
                    }
                    let raw = match std::fs::read(&c.abs) {
                        Ok(b) => b,
                        Err(_) => {
                            r.kind = Kind::Skip;
                            r.skip_why = 0;
                            r.rec.skip = true;
                            continue;
                        }
                    };
                    if raw.is_empty() {
                        r.kind = Kind::Skip;
                        r.skip_why = 1;
                        r.rec.skip = true;
                        continue;
                    }
                    let raw = &raw[..raw.len().min(max_doc_chars as usize)];
                    if raw.contains(&0) {
                        r.kind = Kind::Skip;
                        r.skip_why = 2;
                        r.rec.skip = true;
                        continue;
                    }
                    let mut d = CorpusDoc {
                        source_id: allowed_ref[c.src_idx].id.clone(),
                        relpath: c.rel.clone(),
                        ..Default::default()
                    };
                    d.sha_raw = textutil::sha256_bytes(raw);
                    let raw_text = String::from_utf8_lossy(raw);
                    let norm = textutil::nfc(&raw_text);
                    d.nfc_changed = norm.as_bytes() != raw;
                    d.sha_nfc = textutil::sha256_text(&norm);
                    let ext = lower(
                        &Path::new(&c.rel)
                            .extension()
                            .map(|e| format!(".{}", e.to_string_lossy()))
                            .unwrap_or_default(),
                    );
                    d.language = scan::corpus_lang(norm.as_bytes(), &ext)
                        .to_string();
                    d.overlap_sha = textutil::norm_text_sha(&norm);
                    d.ids = tk_ref.encode(norm.as_bytes(), true, false);
                    if eos_id >= 0 {
                        d.ids.push(eos_id);
                    }
                    if d.ids.is_empty() {
                        r.kind = Kind::Skip;
                        r.skip_why = 1;
                        r.rec.skip = true;
                        continue;
                    }
                    let key = format!("{}:{}", d.source_id, d.relpath);
                    let kh = scan::corpus_fnv(key.as_bytes());
                    d.split = if kh % 1000 < (val_pct * 10) as u64 {
                        "valid"
                    } else {
                        "train"
                    }
                    .to_string();
                    let sig = scan::corpus_sig(norm.as_bytes());
                    r.band_keys = scan::band_keys(&sig).to_vec();
                    r.rec.doc = d.clone();
                    r.rec.band_keys = r.band_keys.clone();
                    r.doc = d;
                    r.kind = Kind::Doc;
                });
            }
        });
    }

    // Stage 3: deterministic merge — exact + MinHash dedup in sorted
    // relpath order.
    let mut docs: Vec<CorpusDoc> = Vec::new();
    let mut seen_sha: HashSet<String> = HashSet::new();
    let mut bands: HashSet<u64> = HashSet::new();
    let (mut exact_dup, mut near_dup, mut empty_docs, mut unreadable) =
        (0i64, 0i64, 0i64, 0i64);
    let mut total_tokens = 0i64;
    let mut lang_counts: HashMap<String, i64> = HashMap::new();
    for i in 0..cands.len() {
        let mut r = results[i].lock().unwrap();
        if matches!(r.kind, Kind::Skip) {
            if r.skip_why == 0 {
                unreadable += 1;
            } else if r.skip_why == 1 {
                empty_docs += 1;
            }
            continue;
        }
        if docs.len() as i64 >= a.max_docs || total_tokens >= a.max_tokens {
            break;
        }
        if !seen_sha.insert(r.doc.sha_nfc.clone()) {
            exact_dup += 1;
            r.kind = Kind::Skip;
            r.cache_drop = true;
            continue;
        }
        if r.band_keys.iter().any(|k| bands.contains(k)) {
            near_dup += 1;
            r.kind = Kind::Skip;
            r.cache_drop = true;
            continue;
        }
        for &k in &r.band_keys {
            bands.insert(k);
        }
        total_tokens += r.doc.ids.len() as i64;
        *lang_counts.entry(r.doc.language.clone()).or_insert(0) += 1;
        if r.cache_hit {
            r.rec = fcache.get(&cands[i].rel).cloned().unwrap_or_default();
        }
        docs.push(std::mem::take(&mut r.doc));
    }

    // ---- emit ----
    let out_dir = PathBuf::from(&a.out);
    std::fs::create_dir_all(&out_dir)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    // Binary-hot-data rule: packed token ids ship as XCB1 containers
    // (train-ids.xcb / valid-ids.xcb); documents/records JSONL remain —
    // provenance manifests, not token batches.
    let train_path = out_dir.join("train-ids.xcb");
    let valid_path = out_dir.join("valid-ids.xcb");
    let docs_path = out_dir.join("documents.jsonl");
    let trec_path = out_dir.join("train-records.jsonl");
    let vrec_path = out_dir.join("valid-records.jsonl");
    let manifest_path = out_dir.join("manifest.json");
    let train = std::fs::File::create(&train_path)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    let valid = std::fs::File::create(&valid_path)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    let mut drec = std::fs::File::create(&docs_path)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    let mut trec = std::fs::File::create(&trec_path)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    let mut vrec = std::fs::File::create(&vrec_path)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    let tk_sha = textutil::sha256_file(&tk_path).unwrap_or_default();
    let ids_meta = format!(
        "{{\"format\":\"star-token-batch/v1\",\"producer\":\"xcorpus\",\"packing_max_len\":{},\"tokenizer_sha256\":\"{}\"}}",
        a.max_len, tk_sha);
    let mut xw_train = crate::xcb::Writer::new(train, &ids_meta)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    let mut xw_valid = crate::xcb::Writer::new(valid, &ids_meta)
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;

    let (mut train_rows, mut valid_rows, mut train_toks, mut valid_toks) =
        (0i64, 0i64, 0i64, 0i64);
    let mut pack: Vec<i64> = Vec::new();
    macro_rules! flush {
        ($out:expr, $rows:expr, $toks:expr) => {{
            if !pack.is_empty() {
                let mut ids: Vec<i32> = Vec::with_capacity(pack.len());
                for &t in pack.iter() {
                    if t < i32::MIN as i64 || t > i32::MAX as i64 {
                        return Err("CORPUS_TOKEN_RANGE".to_string());
                    }
                    ids.push(t as i32);
                }
                $out.add_ids(&ids)
                    .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
                $toks += pack.len() as i64;
                $rows += 1;
                pack.clear();
            }
        }};
    }

    for d in &docs {
        let is_valid = d.split == "valid";
        let mut off = 0usize;
        while off < d.ids.len() {
            let room = a.max_len as usize - pack.len();
            let take = room.min(d.ids.len() - off);
            pack.extend_from_slice(&d.ids[off..off + take]);
            off += take;
            if pack.len() as i64 >= a.max_len {
                if is_valid {
                    flush!(xw_valid, valid_rows, valid_toks);
                } else {
                    flush!(xw_train, train_rows, train_toks);
                }
            }
        }
        writeln!(
            drec,
            "{{\"source_id\":\"{}\",\"path\":\"{}\",\"sha256_raw\":\"{}\",\"sha256_nfc\":\"{}\",\"nfc_changed\":{},\"language\":\"{}\",\"tokens\":{},\"split\":\"{}\"}}",
            textutil::json_escape(&d.source_id),
            textutil::json_escape(&d.relpath),
            d.sha_raw,
            d.sha_nfc,
            d.nfc_changed,
            d.language,
            d.ids.len(),
            d.split
        )
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
        let ovl = if is_valid { &mut vrec } else { &mut trec };
        writeln!(
            ovl,
            "{{\"path\":\"{}\",\"sha256\":\"{}\"}}",
            textutil::json_escape(&d.relpath),
            d.overlap_sha
        )
        .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    }
    flush!(xw_train, train_rows, train_toks);
    flush!(xw_valid, valid_rows, valid_toks);
    xw_train.finish().map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    xw_valid.finish().map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
    drop(drec);
    drop(trec);
    drop(vrec);

    // Persist the incremental file cache.
    {
        let mut cc = std::fs::File::create(out_dir.join("corpus-cache.jsonl"))
            .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
        for i in 0..cands.len() {
            let r = results[i].lock().unwrap();
            if r.cache_drop {
                continue;
            }
            if matches!(r.kind, Kind::Skip) && !r.rec.skip {
                continue;
            }
            if !matches!(r.kind, Kind::Doc | Kind::Cached | Kind::Skip) {
                continue;
            }
            let rec = &r.rec;
            let mut line = format!(
                "{{\"format\":\"star-corpus-file-cache/v1\",\"rel\":\"{}\",\"source_id\":\"{}\",\"size\":{},\"mtime\":\"{}\"",
                textutil::json_escape(&cands[i].rel),
                textutil::json_escape(&rec.doc.source_id),
                rec.size,
                rec.mtime
            );
            if matches!(r.kind, Kind::Skip) {
                line.push_str(",\"status\":\"skip\"}\n");
                cc.write_all(line.as_bytes())
                    .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
                continue;
            }
            line.push_str(&format!(
                ",\"status\":\"doc\",\"sha_raw\":\"{}\",\"sha_nfc\":\"{}\",\"overlap_sha\":\"{}\",\"language\":\"{}\",\"split\":\"{}\",\"nfc_changed\":{},\"band_keys\":[",
                rec.doc.sha_raw,
                rec.doc.sha_nfc,
                rec.doc.overlap_sha,
                rec.doc.language,
                rec.doc.split,
                rec.doc.nfc_changed
            ));
            for (b, k) in rec.band_keys.iter().enumerate() {
                if b > 0 {
                    line.push(',');
                }
                line.push_str(&format!("\"{k:016x}\""));
            }
            line.push_str("],\"ids\":[");
            for (t, id) in rec.doc.ids.iter().enumerate() {
                if t > 0 {
                    line.push(',');
                }
                line.push_str(&id.to_string());
            }
            line.push_str("]}\n");
            cc.write_all(line.as_bytes())
                .map_err(|e| format!("CORPUS_OUT_UNWRITABLE: {e}"))?;
        }
    }

    let docs_sha = textutil::sha256_file(&docs_path)?;
    let mut lv: Vec<(String, i64)> = lang_counts.into_iter().collect();
    lv.sort();
    let mut mf = String::new();
    mf.push_str(&format!(
        "{{\"schema_version\":\"star-pretrain-corpus/v1\",\"dataset_version\":\"sha256:{}\",\"created_at\":{},\"root\":\"{}\",\"registry\":\"{}\",\"tokenizer_sha256\":\"{}\",\"counts\":{{\"sources_allowed\":{},\"sources_rejected\":{},\"files_scanned\":{},\"cache_hits\":{},\"files_reparsed\":{},\"scan_jobs\":{},\"paths_denied\":{},\"docs_kept\":{},\"exact_duplicates\":{},\"near_duplicates\":{},\"empty_skipped\":{},\"unreadable_skipped\":{},\"tokens_total\":{}}},\"languages\":{{",
        docs_sha,
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0),
        textutil::json_escape(
            // std::fs::canonicalize on Windows returns the verbatim
            // "\\?\C:\" form; strip the prefix to match the C++ lane's
            // std::filesystem::absolute output byte-for-byte.
            &std::fs::canonicalize(&root)
                .map(|p| p.to_string_lossy().into_owned())
                .map(|s| s.strip_prefix(r"\\?\").unwrap_or(&s).to_string())
                .unwrap_or_else(|_| a.root.clone())
        ),
        textutil::json_escape(&a.registry),
        textutil::sha256_file(&tk_path)?,
        allowed.len(),
        rejected_sources.len(),
        scanned_files,
        cache_hits,
        reparsed,
        jobs,
        denied_paths,
        docs.len(),
        exact_dup,
        near_dup,
        empty_docs,
        unreadable,
        total_tokens
    ));
    for (i, (k, n)) in lv.iter().enumerate() {
        if i > 0 {
            mf.push(',');
        }
        mf.push_str(&format!("\"{k}\":{n}"));
    }
    mf.push_str("},\"rejected_sources\":[");
    for (i, s) in rejected_sources.iter().enumerate() {
        if i > 0 {
            mf.push(',');
        }
        mf.push_str(&format!("\"{}\"", textutil::json_escape(s)));
    }
    mf.push_str(&format!(
        "],\"train\":{{\"path\":\"train-records.jsonl\"}},\"val\":{{\"path\":\"valid-records.jsonl\"}},\"split\":{{\"val_ratio_pct\":{},\"train_rows\":{},\"valid_rows\":{},\"train_tokens\":{},\"valid_tokens\":{}}},\"packing\":{{\"max_len\":{},\"eos_separator\":{}}},\"integrity\":{{\"documents_jsonl_sha256\":\"{}\",\"train_ids_sha256\":\"{}\",\"valid_ids_sha256\":\"{}\",\"train_records_sha256\":\"{}\",\"valid_records_sha256\":\"{}\"}}}}\n",
        a.val_pct,
        train_rows,
        valid_rows,
        train_toks,
        valid_toks,
        a.max_len,
        eos_id,
        docs_sha,
        textutil::sha256_file(&train_path)?,
        textutil::sha256_file(&valid_path)?,
        textutil::sha256_file(&trec_path)?,
        textutil::sha256_file(&vrec_path)?
    ));
    std::fs::write(&manifest_path, mf)
        .map_err(|e| format!("CORPUS_MANIFEST_UNWRITABLE: {e}"))?;

    Ok(format!(
        "{{\"ok\":true,\"mode\":\"corpus\",\"out\":\"{}\",\"dataset_version\":\"sha256:{}\",\"docs_kept\":{},\"paths_denied\":{},\"exact_duplicates\":{},\"near_duplicates\":{},\"tokens_total\":{},\"train_rows\":{},\"valid_rows\":{}}}",
        textutil::json_escape(&a.out),
        docs_sha,
        docs.len(),
        denied_paths,
        exact_dup,
        near_dup,
        total_tokens,
        train_rows,
        valid_rows
    ))
}
