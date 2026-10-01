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
xstore hash        <file>             sha256 of a file
```

Output contract: one JSON object on stdout (`format` tagged,
`xstore-*/v1`); parse/IO failure → `XCN_PARSE` / `XCN_VERIFY` /
`XSTORE_FAILED` on stderr, exit code 2. Never panics on hostile input;
never allocates based on unchecked length fields.

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

1. **xcn1 parser/verifier** — this commit.
2. Hashing surface (`hash.rs`); manifest + dataset-integrity hash chain.
3. Immutable audit-log append/verify (hash-chained JSONL).
4. Checkpoint store: content-addressed write, atomic rename, delta
   candidate checkpoints.
5. Dataset reader/writer + sequence packing + MinHash/dedup (port of
   `xcm_corpus.h`'s C108 pipeline).
6. Tokenizer (port of `engine_tokenizer.h`).
7. Binary-format registry: one parser per governed artifact kind.

C++ keeps the model core (forward/backward/kernels); C# keeps
governance, lifecycle and scheduling. This lane must not grow a model
runtime.
