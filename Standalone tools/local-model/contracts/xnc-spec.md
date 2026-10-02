# XNC — Xingcheng Native Contract · byte-level specification (xnc-spec/v1)

> Owner-local contract document. Normative authority for the contract's
> *existence, uniqueness, plane split and member kinds*: Codex B81
> `NATIVE-CONTRACT` (rev 200). Byte layouts below are implementation
> contracts — they are NOT Codex text (B81 `ARCHITECTURE-EXCLUSION`).
> Language lanes: Rust `xc-format`/`xstore`/`xcorpus`, C++23
> `xct_ckpt.h`/`xcb_batch.h`, C# `GPTBridge.XingchengLearning` — each
> lane owns its own reader/writer; all lanes obey this document.
> Any drift between lanes or between a lane and this document fails
> closed.

## 1. Planes

An XNC artifact is one **control-plane manifest** plus an optional
**data-plane payload**.

| Plane | Shape | Rule |
| --- | --- | --- |
| Control | UTF-8 JSON manifest, `"format": "<name>/v<N>"` tag on the root object (or per line for JSONL) | Versioned by tag; readers fail closed on unknown tag. May carry file-level `sha256` of the payload. |
| Data | Packed binary envelope (§3) | Never JSON for hot data. Self-describing via 4-byte magic + u32 version. |

The manifest is authoritative for identity and provenance; the payload
is authoritative for bulk data. A manifest that references a payload
whose magic/kind disagrees with the manifest's declared kind is
rejected.

## 2. Common byte rules (all data-plane formats)

- **Endianness**: every integer and float is little-endian.
- **Widths**: `u8`, `u16`, `u32`, `u64`, `i32`, `f32` (IEEE-754 binary32).
  No variable-length integers; every count/length is an explicit width.
- **Strings**: `u32 byte_len` + UTF-8 bytes. Not NUL-terminated. Invalid
  UTF-8 is a hard error.
- **Magic**: exactly 4 ASCII bytes, `XC` + one kind letter + one
  envelope-generation digit. The first two bytes identify the XNC
  family; byte 2 the member kind; byte 3 the envelope generation
  (`'1'` for all current kinds).
- **Version field**: a `u32` immediately after the magic, schema
  revision within the envelope generation. Ranges are per-kind.
- **No padding** unless a kind's layout says so. Readers consume
  exactly the declared extent; trailing bytes are only allowed where
  the kind explicitly defines them as payload.
- **Fail closed**: bad magic, out-of-range version, truncated field,
  length exceeding a hard cap, or invalid UTF-8 is a typed error —
  never a guess, never best-effort parsing, never an allocation sized
  by an unchecked length field.
- **Forward compatibility**: readers reject any `version` above their
  maximum known revision. Writers never append fields inside a shipped
  revision — a change lands as `version+1` with cumulative trailing
  blocks (the XCN precedent), so old readers still accept old
  artifacts.

## 3. Data-plane envelope

```
offset  field
0       u8[4]   magic ("XC" + kind + generation)
4       u32     version
8       kind-specific header
```

Two header idioms exist; a kind uses exactly one:

- **Config-in-header** (XCN): the versioned config block is serialized
  directly after `version`; block sets are cumulative per revision.
- **Meta-JSON header** (XCB): `u32 meta_len` + `meta_len` bytes of
  UTF-8 JSON (control surface inlined in the envelope: `format` tag,
  producer, provenance hashes), then a `u64 record_count` the writer
  back-patches on close.

## 4. Member registry

| Kind | Member | Magic | Control manifest (`format` tag) | Data-plane body |
| --- | --- | --- | --- | --- |
| XCN | model checkpoint | `XCN1`, ver 1..=10 | provenance carried by lifecycle/registry manifests (`star-model-lifecycle/v1`) | config block + tensor table (§5) |
| XDS | dataset | `XCB1`, ver 1 | `star-dataset-snapshot/v1`, `star-corpus-registry/v1`, `star-corpus-scan-state/v1`; envelope meta: `star-token-batch/v1` | packed records (§6) |
| XCR | receipt | — (control-plane only) | hash-chained JSONL: `star-audit-log/v1`, `star-store-index/v1` (`prev` chain) | none |
| XST | state | — (control-plane only) | `star-model-lifecycle/v1`, tool state manifests | reserved — a packed state body MUST register an `XCx1` magic here first |
| XEV | evaluation | — (control-plane only) | versioned eval/report manifests (`*/v1`-tagged) | reserved — same rule as XST |

Registered magics: `XCN1` (checkpoint), `XCB1` (token batch),
`XPA1` (prefill artifact — same envelope family, inference lane).
New data-plane kinds must extend this table in the same change that
introduces their magic.

## 5. XCN1 — checkpoint layout (ver 1..=10)

Written by `xct_ckpt.h` `ckpt_save`; validated by Rust `xc-format` /
`xstore::xcn1`; read by C# `XcnHeader`.

```
"XCN1" | u32 version | config block | u32 tensor_count |
  tensor x tensor_count:
    u32 name_len + UTF-8 name
    u32 ndims     + u64 dims[ndims]
    u64 elem_count + f32 data[elem_count]
```

Config block is cumulative by revision (write order = read order,
omitted blocks default to fused/non-present semantics):

- **v1** base: 10×u32 geometry (vocab, hidden, intermediate, layers,
  heads, kv_heads, max_pos, moe_experts, moe_top_k, moe_layer_interval)
  + 3×f32 (rope_theta, rms_norm_eps, moe_aux_loss_weight)
- **v2** +3×u32 (moe_expert_inter, moe_shared_experts, moe_shared_inter)
- **v3** +u32 full_attention_interval, u32 attn_flags (bit0 output_gate,
  bit1 qk_norm, bit2 shared_expert_gate), f32 partial_rotary,
  5×u32 linear-attention dims
- **v4** +3×u32 vision early-fusion (use_vision, patch_dim, max_patches)
- **v5** +4×u32 (global_attention_interval, sliding_window,
  num_global_kv_heads, gemma_flags bit0 k_eq_v / bit1 post_attn_norm /
  bit2 post_ffw_norm / bit3 ffn_activation=gelu_tanh)
  + 5×f32 (rope proportions, base frequencies, final_logit_softcap)
- **v6** +u32 moe_router_sigmoid
- **v7** +5×u32 MLA/MoE-balance (kv_lora, q_lora, qk_nope, qk_rope,
  moe_auxfree_balance) + f32 moe_lb_bias_rate + u32
  num_nextn_predict_layers + f32 mtp_loss_weight
- **v8** +f32 yarn_factor, u32 yarn_orig_pos, f32 yarn_beta_fast/slow,
  f32 yarn_attention_factor
- **v9** +u32 gemma4_marker (**always present** at v≥9): `0` =
  non-gemma4, `1` = g4 block follows (7×u32 dims/flags, 4×f32
  rope/softcap/scale, `u32 n` + counted layer_type strings,
  counted hidden_activation string); any other marker value is a hard
  error
- **v10** +u32 mtp_stack_depth + f32 mtp_stack_loss_weight

Integrity: no in-file hash — the registry/lifecycle manifest carries
the file sha256 (see `xstore ckpt-verify`). Hard caps (xstore
`xcn1.rs`): tensors ≤ 4 194 304, name ≤ 4096 B, dims ≤ 32,
layer_types ≤ 4096, per-string ≤ 4096 B.

## 6. XCB1 — packed token-batch layout (ver 1, XDS data plane)

Written by `xcb_batch.h` / `xcorpus::xcb::Writer` (byte-parity pair).

```
"XCB1" | u32 version=1 | u32 meta_len | UTF-8 meta JSON |
u64 record_count (back-patched by writer on close) |
  record x record_count:
    u8  kind    1=pretrain 2=sft 3=dpo 4=grpo
    u8  flags   bit0 = vision grid present; bits 1..7 reserved = 0
    u16 reserved = 0
    u32 n_ids    + i32 ids[n_ids]
    u32 n_labels + i32 labels[n_labels]      (kind 1 writes n=0)
    kind 3 only: u32 n_rej + i32 rej_ids[n] | u32 n_rej_labels + i32[]
    flags&1: u32 patches | u32 dim | f32 grid[patches*dim]
```

Meta JSON must carry `format` (`star-token-batch/v1`), `producer`, and
provenance fields (e.g. `tokenizer_sha256`, `packing_max_len`).
Integrity: file sha256 in the dataset registry — no hash tail.
Unknown `version`, unknown `kind`, nonzero reserved fields, or a
count exceeding remaining bytes all reject.

## 7. Control-plane manifest rules

- One JSON document (or one JSON object per line for chained
  manifests), UTF-8, `"format": "<name>/v<N>"` identifies the schema.
- Chained manifests (`star-audit-log/v1`, `star-store-index/v1`):
  each line carries `sha256` of its canonical serialization and
  `prev` of the previous line; verification walks the whole chain —
  truncated, reordered or forged lines fail verification.
- `format` tags identify the artifact family; kind identity is
  additionally bound to the payload magic (§4) — a mismatch is a
  rejection, not a downgrade.
- Unknown optional fields may be ignored; a missing required field or
  an unknown `format` version fails closed.

## 8. Cross-language conformance

Canonical vectors live in `contracts/xnc/vectors/` (binary payloads +
`manifest.json` describing each vector's kind, version, expected
verdict and expected summary fields). Every lane:

- MUST parse every valid vector byte-identically (same summary values,
  same consumed extent).
- MUST reject every malformed vector with its typed error (categories:
  bad_magic, bad_version, truncated, length_over_cap, bad_utf8,
  bad_marker, bad_kind, checksum/registry mismatch).
- MUST round-trip: writer output re-parsed by every other lane's
  reader must be identical to the reference decode.

Lanes today: C++23 (`xct_ckpt.h`, `xcb_batch.h`), Rust (`xc-format`,
`xstore`, `xcorpus`), C# (`XcnHeader` — thin governed reader until the
Rust lane is wired end to end; no new format logic lands there).
