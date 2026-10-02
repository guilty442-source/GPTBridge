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
```

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
5. Immutable audit-log append/verify — receipt chain landed in
   `store.rs`; standalone `xstore audit-*` surface is the next step.
6. Tokenizer — landed in `xcorpus` (port of `engine_tokenizer.h`);
   a C ABI shim for the inference engine is still open.
7. Binary-format registry: one parser per governed artifact kind.

C++ keeps the model core (forward/backward/kernels); C# keeps
governance, lifecycle and scheduling. This lane must not grow a model
runtime.
