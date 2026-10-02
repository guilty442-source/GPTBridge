# xstore ??Rust data/safety lane (xstore/v1)

> Language architecture: AGENTS.md??瞉?Language Architecture??> Rust owns storage, data, file formats, tokenization, validators and
> every untrusted-input boundary. This crate is the first component of
> that lane.

## Scope

`xstore` is the untrusted-input boundary for local-model artifacts.
Anything that parses bytes nobody wrote ??checkpoints, manifests,
network/corpus data, large binaries ??belongs here, not in C++/C#,
because the ownership model removes the bug classes that matter most
on this surface: use-after-free, buffer overflow, iterator
invalidation, double free, and data races.

## Commands

```
xstore ckpt-info   <file>             XCN1..XCN10 header + tensor table (JSON)
xstore ckpt-verify <file> [--hash-payloads]
                                      structural verify + file sha256
xstore ckpt-diff   <base> <cand>      delta-candidate parity report
xstore hash        <file>             sha256 of a file
xstore put  --store <dir> --file <f> [--kind xcn1|blob]
xstore get  --store <dir> --sha256 <hex> --out <path>
xstore verify-store --store <dir>     re-hash all objects + index chain
xstore audit-append --log <f> --data <json>
                                      hash-chained JSONL entry
xstore audit-verify --log <f>         verify the whole chain
xstore snapshot --store <d> --src <d> [--name <id>]
                                      pin a directory into the store
xstore snapshot-verify --store <d> --manifest <sha256|path>
xstore fail-record --pool-dir <d> --class <c> --input <s>
   --generation <g> --expected <s> --actual <s> --evidence <s>
   [--severity <l>] [--reason <s>] [--model-version <v>]
   [--runtime-version <v>] [--provenance <s>] [--reproducible 0|1]
xstore fail-list  --pool-dir <d> --class <c>
xstore fail-mark  --pool-dir <d> --class <c> --fingerprint <fp>
   --state <OPEN|TRAINED|RESOLVED|REGRESSED>
xstore fail-status --pool-dir <d>
xstore kernel-registry [--policy <json>]
                                      star-kernel-registry inventory
```

Every command also accepts `--policy <json>` (or env
`XCT_KERNEL_POLICY`) naming a `star-kernel-policy` file. When the
policy is loaded and `enabled`, a `deny_kernels` entry matching the
kernel a command dispatches to fails closed as
`XSTORE_FAILED: KERNEL_POLICY_DENIED: <kernel>` (exit 2); a referenced
but unreadable/malformed/misformat policy is likewise a hard failure.
`kernel-registry` emits the lane inventory (`star-kernel-registry`)
with the resolved policy echoed in the `policy` block and denied
kernels marked `"active":"denied"`.

Output contract: one JSON object on stdout (`format` tagged,
`xstore-*/v1`); parse/IO failure ??`XCN_PARSE` / `XCN_VERIFY` /
`XSTORE_FAILED` on stderr, exit code 2. Never panics on hostile input;
never allocates based on unchecked length fields.

## Store layout (store.rs)

```
<store>/objects/<sha2>/<sha256>.bin   immutable content-addressed bodies
<store>/tmp/*.tmp                     write-tmp spill (fsync ??rename)
<store>/store-index.jsonl             append-only receipt chain
                                      (star-store-index/v1, `prev` links)
```

- `put` verifies first (`--kind xcn1` runs full `xcn1::verify`), hashes,
  spills to tmp, fsyncs, re-reads and re-hashes, then renames ??no
  object is ever overwritten; a same-hash put is an idempotent receipt.
- `get` re-hashes resident bytes before export; corruption ??error.
- `verify-store` re-hashes every object AND walks the receipt chain ??  truncated, reordered or forged index lines flip `ok`/`index_chain_ok`.

## Delta candidates (diff.rs)

`ckpt-diff` compares base vs candidate: XCN verify on both, full
`CkptConfig` field parity, tensor name/shape/count alignment, then
byte-exact payload compare with lane-level stats (lanes_changed,
max_abs_diff, NaN counts) for differing tensors. `compatible:false`
is fail-closed ??callers deny promotion on structural divergence.

## Immutable audit log (audit.rs)

`star-audit-log/v1` ??append-only JSONL where each entry's `prev` is
sha256 of the previous line's **raw bytes** (genesis = 64 zeros):
tampering with any byte of entry k breaks the link stored in k+1;
reordering, forged appends and seq jumps all fail verify. Append is
O(1) via backward tail-seek; `f.sync_all` before receipt. Inherent
limit: lone logs cannot detect tail truncation ??callers anchor `head`
externally (store index, DB row) to close it.

## Dataset snapshots (snapshot.rs)

`snapshot` pins a directory: every file is `put` (idempotent ??unchanged files are receipts, not copies), then a
`star-dataset-snapshot/v1` manifest {name, files:[{path,sha256,size}]}
is itself content-addressed into objects/ AND written to
`snapshots/<sha>.json`, then an audit entry lands in
`store-audit.jsonl`. `snapshot-verify` re-checks manifest
self-addressing (lookup sha == recomputed content hash) plus every
listed object's presence + hash ??tampered manifests, missing or
corrupt objects all flip `ok:false`. Re-snapshotting a mutated
directory yields a new manifest sha; old generations stay retrievable.

## Failure pool (failpool.rs)

`star-capability-failure-pool/v1` ??persisted-form port of C#
`FailurePool.cs` (禮29/禮55): `pool-<class>.jsonl` per capability,
`input_fingerprint` = sha256(input)[:16], `norm_fingerprint` =
sha256 of lowercase+whitespace-folded+punctuation/symbol-stripped
input[:16] (unicode_categories). Exact-or-normalized repeats bump
`seen_count`/`last_seen` in place instead of appending; `high`
severity upgrades stick. Pools bounded at 4096 (newest kept).
`fail-mark` drives 禮55 state transitions (OPEN?RAINED/RESOLVED/
REGRESSED). Records serialize as canonical sorted-key JSON so Rust-
and C#-written lines are interchangeable in one pool dir.

## Guarantees (xcn1.rs)

- All reads bounds-checked; every declared extent (`count * 4`, name
  length, dims, g4 string lengths) passes a hard cap before use.
- No allocation sized by attacker-controlled fields.
- `count * 4` is overflow-checked (`u64` mul, then bounded by remaining
  file bytes).
- gemma4 marker is validated (only 0/1 legal); version range 1..=10.
- `verify` walks the full tensor table and reports `trailing_bytes`
  instead of silently accepting them.
- Input is a read-only `memmap2` mapping ??multi-GB checkpoints verify
  without staging the payload into memory.

## Roadmap (in priority order)

1. **xcn1 parser/verifier** ??landed.
2. Hashing surface (`hash.rs`) ??landed.
3. **Checkpoint store + delta candidates** ??landed (`store.rs`,
   `diff.rs`): content-addressed objects, tmp+fsync+rename atomicity,
   hash-chained receipt index, structural parity gate.
4. Dataset reader/writer + sequence packing + MinHash/dedup ??landed in
   `xcorpus` (port of `xcm_corpus.h`'s C108 pipeline, byte-parity).
5. Immutable audit-log append/verify ??landed (`audit.rs`:
   `star-audit-log/v1` hash-chained JSONL, O(1) append, full-chain
   verify). C# `AuditEvent` JSONL and failure-pool records can adopt
   this format as their persisted form.
6. Tokenizer ??landed in `xcorpus` (port of `engine_tokenizer.h`);
   a C ABI shim for the inference engine is still open.
7. Binary-format registry: one parser per governed artifact kind.

C++ keeps the model core (forward/backward/kernels); C# keeps
governance, lifecycle and scheduling. This lane must not grow a model
runtime.
