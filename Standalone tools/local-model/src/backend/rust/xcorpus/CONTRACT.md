# xcorpus — Rust corpus pipeline (star-pretrain-corpus/v1 port)

> Language architecture: AGENTS.md「星澄 Language Architecture」—
> Rust owns the data layer. This crate ports `xcm_corpus.h`
> (`xc_modeltool corpus`) into the Rust lane: untrusted text + file
> trees in, governed packed datasets out.

## Commands

```
xcorpus corpus --registry <json> --root <dir> --tokenizer <path-or-dir>
               --out <dir> [--max-len N=1024] [--val-ratio PCT=5]
               [--max-docs N=20000] [--max-doc-chars N=1000000]
               [--max-tokens N=50000000] [--jobs N=min(8,hw)]
```

Identical CLI surface to `xc_modeltool corpus`; stdout emits the same
single JSON summary; failures print the C108 error code and exit 2.

## Byte-parity contract

Given the same registry/root/tokenizer the emitted artifacts are
byte-identical to the C++ lane:

- `train-ids.jsonl` / `valid-ids.jsonl` — EOS-separated packing,
  one shared pack cursor across docs/splits (matching the C++ emit
  order, including the cross-split flush behaviour).
- `documents.jsonl` — field order fixed; `dataset_version` =
  sha256 of this file, so parity is provable by the digest.
- `train-records.jsonl` / `valid-records.jsonl` — overlap-gate records
  (`norm_text_sha` = sha256(py_strip(nfc(text)))).
- `corpus-cache.jsonl` — `star-corpus-file-cache/v1`; unchanged files
  (size + FILETIME-tick mtime match) reuse derived records.
- `manifest.json` — identical schema/counters; `created_at`, `root`,
  `registry` are the only run-specific fields.

Pipeline elements (C108): registry gate (enabled + license +
sensitivity allowlist), deny-fragment pruning before descent,
extension allowlist, NUL-byte binary rejection, `max_doc_chars`
truncation, sha256 raw + NFC + overlap hashes, deterministic language
tag, BPE encode + bos/eos, hash-split train/valid, exact-dup by
`sha_nfc` + MinHash 16-band near-dup, dedup losers uncached, integrity
manifest with per-file sha256.

## Module map

| Module | Ports |
| --- | --- |
| `tokenizer.rs` | `ByteLevelBPETokenizer` (byte-level BPE, merges by lowest rank, specials, bos=1/eos=2) |
| `textutil.rs` | `nfc` (unicode-normalization), `py_strip` (C++ unicode_space table), `sha256_text`, `json_escape` |
| `scan.rs` | deny list, text/code extension sets, `corpus_lang`, FNV + splitmix64 mix, 64-lane MinHash, 16 band keys |
| `corpus.rs` | registry gate → scan → cache gate → parallel parse → dedup merge → emit → manifest |

## Known deltas vs C++ (safe, documented)

- NFC: C++ uses Windows `NormalizeString`; Rust uses
  `unicode-normalization` — same Unicode NFC mapping; invalid UTF-8 is
  lossy-decoded in both lanes (U+FFFD).
- `mtime` stored as Windows FILETIME ticks (100ns since 1601) to keep
  `corpus-cache.jsonl` interoperable with C++-written caches.
- Worker pool is `std::thread::scope` — tokenizer tables are read-only
  after load; result slots are per-candidate `Mutex`s.
