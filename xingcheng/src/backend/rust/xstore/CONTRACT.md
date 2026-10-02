# xstore — Rust data/safety lane (xstore/v1)

> Language architecture: AGENTS.md「星澄 Language Architecture」—
> Rust owns storage, data, file formats, tokenization, validators and
> every untrusted-input boundary. This crate is the first component of
> that lane.

## Scope

`xstore` is the untrusted-input boundary for local-model artifacts.
Anything that parses bytes nobody wrote — checkpoints, manifests,
network/corpus data, large binaries — belongs here, not in C++/C#,
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
xstore metadata-put --store <d> --op <op> [--params <json|@file>]
                                      governed metadata mutation (see below)
xstore metadata-transition --store <d> --type <t> --id <id> --to <s>
   [--expected-revision <n>]
xstore metadata-get --store <d> --type <t> --id <id>
xstore metadata-query --store <d> --type <t> [--where <json>] [--limit <n>]
xstore metadata-snapshot --store <d>
xstore metadata-verify --store <d>
xstore metadata-rebuild-index --store <d>
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
`xstore-*/v1`); parse/IO failure → `XCN_PARSE` / `XCN_VERIFY` /
`XSTORE_FAILED` on stderr, exit code 2. Never panics on hostile input;
never allocates based on unchecked length fields.

## Store layout (store.rs)

```
<store>/objects/<sha2>/<sha256>.bin   immutable content-addressed bodies
<store>/tmp/*.tmp                     write-tmp spill (fsync → rename)
<store>/store-index.jsonl             append-only receipt chain
                                      (star-store-index/v1, `prev` links)
```

- `put` verifies first (`--kind xcn1` runs full `xcn1::verify`), hashes,
  spills to tmp, fsyncs, re-reads and re-hashes, then renames — no
  object is ever overwritten; a same-hash put is an idempotent receipt.
- `get` re-hashes resident bytes before export; corruption → error.
- `verify-store` re-hashes every object AND walks the receipt chain —
  truncated, reordered or forged index lines flip `ok`/`index_chain_ok`.

## Delta candidates (diff.rs)

`ckpt-diff` compares base vs candidate: XCN verify on both, full
`CkptConfig` field parity, tensor name/shape/count alignment, then
byte-exact payload compare with lane-level stats (lanes_changed,
max_abs_diff, NaN counts) for differing tensors. `compatible:false`
is fail-closed — callers deny promotion on structural divergence.

## Immutable audit log (audit.rs)

`star-audit-log/v1` — append-only JSONL where each entry's `prev` is
sha256 of the previous line's **raw bytes** (genesis = 64 zeros):
tampering with any byte of entry k breaks the link stored in k+1;
reordering, forged appends and seq jumps all fail verify. Append is
O(1) via backward tail-seek; `f.sync_all` before receipt. Inherent
limit: lone logs cannot detect tail truncation — callers anchor `head`
externally (store index, DB row) to close it.

## Dataset snapshots (snapshot.rs)

`snapshot` pins a directory: every file is `put` (idempotent —
unchanged files are receipts, not copies), then a
`star-dataset-snapshot/v1` manifest {name, files:[{path,sha256,size}]}
is itself content-addressed into objects/ AND written to
`snapshots/<sha>.json`, then an audit entry lands in
`store-audit.jsonl`. `snapshot-verify` re-checks manifest
self-addressing (lookup sha == recomputed content hash) plus every
listed object's presence + hash — tampered manifests, missing or
corrupt objects all flip `ok:false`. Re-snapshotting a mutated
directory yields a new manifest sha; old generations stay retrievable.

## Failure pool (failpool.rs)

`star-capability-failure-pool/v1` — persisted-form port of C#
`FailurePool.cs` (§29/§55): `pool-<class>.jsonl` per capability,
`input_fingerprint` = sha256(input)[:16], `norm_fingerprint` =
sha256 of lowercase+whitespace-folded+punctuation/symbol-stripped
input[:16] (unicode_categories). Exact-or-normalized repeats bump
`seen_count`/`last_seen` in place instead of appending; `high`
severity upgrades stick. Pools bounded at 4096 (newest kept).
`fail-mark` drives §55 state transitions (OPEN→TRAINED/RESOLVED/
REGRESSED). Records serialize as canonical sorted-key JSON so Rust-
and C#-written lines are interchangeable in one pool dir.

## Metadata plane (meta_*.rs)

xstore carries a native structured-metadata lane — an append-only,
hash-chained event log with derived, rebuildable projections. There is
no embedded SQL and no second store; the canonical truth lives in
`metadata/events/events.jsonl` and everything else under `metadata/`
is a cache of it.

```
<store>/metadata/events/events.jsonl    canonical hash-chained log
                                        (star-xstore-metadata-event/v1)
<store>/metadata/audit/receipts.jsonl   mutation receipts, chained
                                        (star-xstore-metadata-receipt/v1)
<store>/metadata/index/head.json        commit checkpoint
<store>/metadata/index/records.json     materialized records (derived)
<store>/metadata/index/operations.json  idempotency map (derived)
<store>/metadata/snapshots/<sha>.json   restart-acceleration manifests
                                        (star-xstore-metadata-snapshot/v1)
<store>/metadata/leases/writer.lock     single-writer lease
<store>/metadata/leases/epoch.json      monotonic writer epoch (ABA)
<store>/metadata/schema.json            schema identity pin
```

- **Canonical serialization**: `serde_json::Value` maps are
  BTreeMap-ordered, so `to_string` yields sorted-key compact bytes —
  that is the only canonical encoding. C# callers never rebuild hash
  bytes; they ship payloads and Rust re-canonicalizes before hashing.
- **Event identity**: each event carries `seq`, `transaction_id`,
  `operation_id`, `record_type`, `record_id`, `revision`,
  `previous_hash` (raw-line chain), `previous_revision_hash`
  (per-record chain), `payload_hash`, `writer_epoch`, `event_hash`.
- **Torn tail**: a line is committed iff it parses, hash-verifies,
  chains and seq-increments. An unparseable trailing line is ignored
  on read and trimmed before the next append; mid-file corruption is
  `XSTORE_RECOVERY_REQUIRED` — never auto-repaired.
- **Crash order**: validate → append events + fsync (commit point) →
  append receipt + fsync → refresh derived index → release lease. A
  crash after commit but before the index refresh is detected on the
  next open (`head.last_event_hash` != log tail hash) and rebuilt.
- **Writer model**: single writer + multi reader. `writer.lock` is an
  O_EXCL lease with expiry; stale locks are taken over and the epoch
  is bumped — a superseded writer committing with an old epoch fails
  `XSTORE_STALE_WRITER`. Contention on a live lock fails
  `XSTORE_WRITER_BUSY` after a bounded wait.
- **Optimistic concurrency**: mutations may pass `expected_revision`;
  a mismatch fails `XSTORE_REVISION_CONFLICT` before any byte is
  written. `operation_id` makes retried mutations idempotent — a
  replay returns `result:"duplicate"` with the original receipt data.
- **Domain ops** replicate the PostgreSQL repository semantics
  verbatim (datasets are `UNIQUE(content_sha256,snapshot_sha256)` +
  immutable; job FSM `queued→preflight→training→validating→completed`
  with `failed`/`cancelled` sides fail-closed; `claim_job` is the
  serial-lane advisory-lock port; candidates carry unique
  `artifact_sha256` with at most one `active`; `activate`/`rollback`/
  `retire` move the runtime singleton in the same atomic bundle;
  `automatic_weight_replacement` is pinned `false` and re-checked at
  every materialize). Release actions are exactly
  `stage|activate|rollback|retire`; evaluations are
  `UNIQUE(adapter_id,suite_sha256)`.
- **Startup**: `load_state` = verify schema → scan canonical log →
  use in-step index fast path, else snapshot-accelerated replay
  (`metadata-snapshot` bodies are content-addressed objects; a
  manifest is honored only when its `last_event_hash` is a real chain
  position). Cross-record invariants are re-enforced on every fold:
  revision chains, append-only types, single active candidate, one
  runtime-state singleton, legal statuses — violations are
  `XSTORE_RECOVERY_REQUIRED`, never guessed-at repair.
- **Snapshots** accelerate restart only; `metadata-verify` reports
  event/record counts, receipts-chain head, index freshness, snapshot
  inventory and the writer epoch.

## Guarantees (xcn1.rs)

- All reads bounds-checked; every declared extent (`count * 4`, name
  length, dims, g4 string lengths) passes a hard cap before use.
- No allocation sized by attacker-controlled fields.
- `count * 4` is overflow-checked (`u64` mul, then bounded by remaining
  file bytes).
- gemma4 marker is validated (only 0/1 legal); version range 1..=10.
- `verify` walks the full tensor table and reports `trailing_bytes`
  instead of silently accepting them.
- Input is a read-only `memmap2` mapping — multi-GB checkpoints verify
  without staging the payload into memory.

## Roadmap (in priority order)

1. **xcn1 parser/verifier** — landed.
2. Hashing surface (`hash.rs`) — landed.
3. **Checkpoint store + delta candidates** — landed (`store.rs`,
   `diff.rs`): content-addressed objects, tmp+fsync+rename atomicity,
   hash-chained receipt index, structural parity gate.
4. Dataset reader/writer + sequence packing + MinHash/dedup — landed in
   `xcorpus` (port of `xcm_corpus.h`'s C108 pipeline, byte-parity).
5. Immutable audit-log append/verify — landed (`audit.rs`:
   `star-audit-log/v1` hash-chained JSONL, O(1) append, full-chain
   verify). C# `AuditEvent` JSONL and failure-pool records can adopt
   this format as their persisted form.
6. Tokenizer — landed in `xcorpus` (port of `engine_tokenizer.h`);
   a C ABI shim for the inference engine is still open.
7. Binary-format registry: one parser per governed artifact kind.
8. **Metadata plane** — landed (`meta_*.rs`): append-only hash-chained
   event log, writer lease/epoch, derived index + snapshot restart,
   `star-xstore-metadata-receipt/v1` per mutation, and the full
   PostgreSQL-repository semantic port (datasets, examples, job FSM,
   serial claim, candidates, evaluations, release actions, runtime
   singleton). This is the substrate for the PostgreSQL → xstore
   metadata-authority migration; the authority flip itself is a
   governed transition outside this crate.

C++ keeps the model core (forward/backward/kernels); C# keeps
governance, lifecycle and scheduling. This lane must not grow a model
runtime.
