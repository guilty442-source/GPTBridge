# Unified Codex Amendment Request Format — `codex-amendment-request/v2`

> Scope: this document defines the single canonical JSON format for every
> `codex-amendment-request-*.json` artifact dropped into the governed intake.
> It is an operational contract, not codex text. The enforcing consumer is
> `shared-layer/csharp/GPTBridge.CodexPipeline` (`Lifecycle.LoadAmendmentRequest`,
> `Driver.AdvanceAll`, `CodexAutomation` flow `codex-amendment-intake`).

Existing v1/legacy request files are immutable evidence — they are NOT
rewritten. v2 applies to new submissions only.

## 1. Transport rules

- **Filename**: `codex-amendment-request-<slug>-<yyyymmdd>[-r<n>].json`.
  Lifecycle suffixes (`.withdrawn`, `.staged.json`) are applied by the
  ledger/pipeline, never by the author.
- **Drop locations** (intake dirs, scanned by `Driver.IntakeDirs`):
  - `main-system/runtime/state/`
  - `governance_rule/execution/audit/convergence/`
- **Encoding**: UTF-8, no BOM, LF line endings, strict JSON. No comments,
  no trailing commas, no embedded unescaped control characters. A file that
  does not parse is recorded `rejected` in the ledger — it is never silently
  reprocessed.
- One request per file. `request_id` must equal the filename slug portion.

## 2. Required header fields

| Field | Type | Rule |
|---|---|---|
| `artifact` | string | literal `"codex-amendment-request"` (loader-enforced) |
| `authority` | string | literal `"request-only"` — a request never carries execution authority (loader-enforced) |
| `schema` | string | literal `"codex-amendment-request/v2"` (advisory; the loader does not gate on it, but tooling/reviewers do) |
| `request_id` | string | non-empty; filesystem-safe slug ≤160 chars |
| `title` | string | required — one-line intent |
| `summary` | string | required — what changes and why, for sovereign review |
| `requested_by` | string | active sovereign id: `decision-sovereign` \| `permission-sovereign` \| `system-runtime-sovereign` \| `automation-sovereign` \| `xingcheng`. Aliases resolved by the loader: `星澄`→`xingcheng`, `synchronization-sovereign`→`automation-sovereign`. |
| `origin` | string | required — provenance: governor instruction, audit finding, divergence report id |
| `problem` | object or string | required — `{ "summary": ... }` minimum; the defect the amendment repairs |
| `change_class` | string | enum: `editorial` \| `clarification` \| `provision-scope` \| `authority-duty` \| `architecture-authority` \| `complete-reconstitution`. Normalized lowercase-hyphen (loader-enforced) |
| `required_review` | string | `five-sovereign-audit-unanimous-pass` unless codex law assigns a stricter gate (loader-enforced non-empty) |
| `flow` | string | `A382/A488-non-disruptive-amendment-flow` (canonical non-disruptive flow) |
| `not_executed` | bool | literal `true`. `false` or absent ⇒ `REQUEST_ALREADY_CLOSED` — replay protection: a closed request is a new file, never an edit (loader-enforced) |
| `predecessor` | object | lineage anchor, see §3 |

## 3. `predecessor` — lineage anchor

```json
"predecessor": {
  "codex_version": "<live authority version, e.g. 2026-10-02T06:22:55Z>",
  "version_identity": "E<n>:<codex_version>",
  "version_epoch": <int>,
  "history_head": "<sha256>",
  "revision_sequence": <int>
}
```

- `codex_version` and `history_head` are **required** (loader-enforced).
- `revision_sequence`, `version_identity`, `version_epoch` are required by
  v2 whenever known — they feed the `lineage_key` (content hash of
  `codex_version` + `history_head` + `revision_sequence`) that serializes
  same-generation requests under a single lineage lock.

## 4. Payload — exactly what the change does

At least **one** of these three canonical keys must be present and truthy:

| Key | Shape | Use |
|---|---|---|
| `changes` | array of `{ "table", "key": {...}, "field", "proposed" }` | field-level row updates in codex tables (the dominant form — e.g. `physical_root` rebinds) |
| `proposed_successors` | object or array of `{ "registry", "action", "rows": [...] }` | registry row operations / provision successor entries (e.g. `provision_law_classification` inserts) |
| `proposed_change` | object `{ "table"?, ... }` | one composite/cross-cutting change that is not a row-cell update |

**Deprecated payload keys** (still accepted by the loader for legacy files —
new v2 requests MUST NOT use them): `proposed_successor` (singular),
`proposed_delta`, `proposed_repair`, `proposed_resolution`.

Mutations must be expressible as data in these keys. Prose-only requests
belong in `summary`/`problem`, not as substitutes for a machine-applicable
payload — a request with no payload key is `REQUEST_SUCCESSOR_REQUIRED` at
intake.

## 5. `acceptance` — unified verification block

Replaces the legacy zoo (`verification`, `acceptance`, `evidence`,
`required_execution_steps`, `gate_conditions`). One key, one shape:

```json
"acceptance": {
  "expected_state": "post-amendment state the five-sovereign audit must observe",
  "criteria": ["measurable check 1", "measurable check 2"],
  "evidence": ["paths or artifact ids proving the implementation side"],
  "out_of_scope": ["what this amendment deliberately does not change"]
}
```

- `expected_state` required; `criteria`, `evidence`, `out_of_scope`
  recommended.
- Immutable-history note belongs in `out_of_scope` (e.g. superseded article
  text retaining old paths is intentionally not rewritten).

## 6. Lineage between requests

| Field | Type | Rule |
|---|---|---|
| `supersedes` | string | `request_id` of the non-terminal request this one replaces |
| `supersede_reason` | string | why the predecessor was withdrawn/resubmitted |

Deprecated: `resubmission_of` + `resubmission_reason` (same semantics,
non-canonical names). Never point `supersedes` at an `executed` request —
an executed amendment is history; a follow-up is a new amendment with its
own `predecessor` anchor.

## 7. Forbidden keys (v2)

These either duplicate ledger authority or are silently ignored — their
presence makes the file misleading:

| Key | Why forbidden |
|---|---|
| `auto_execute` | execution gating lives in `main-system/config/automation-flows.json` (`codex-amendment-intake.auto_execute`); a request can never self-authorize execution |
| `status`, `amendment_id` | lifecycle state and identity are owned by the `gptbridge-codex-amendment-lifecycle/v1` ledger records, not the request file |
| `do_not_execute`, `governor_disposition` | governor decisions are ledger history entries, not request fields |
| `not_executed: false` | literal `true` is the only legal value at intake |

## 8. Lifecycle reminder

`submitted → under-review → successor-built → auditing → audit-passed →
ready-for-governor → executed` (terminal: `executed` | `rejected` |
`withdrawn`). States live in `main-system/runtime/state/codex-amendments/`
ledger records; candidates land in `candidates/<request_id>.sql`.
Without flow-level `auto_execute` the driver stops at `ready-for-governor`.

## 9. Canonical template

```json
{
  "artifact": "codex-amendment-request",
  "authority": "request-only",
  "schema": "codex-amendment-request/v2",
  "request_id": "<topic-slug>-<yyyymmdd>-r<n>",
  "title": "<one-line intent>",
  "summary": "<what changes and why>",
  "requested_by": "permission-sovereign",
  "origin": "<governor instruction or audit finding>",
  "problem": { "summary": "<defect>" },
  "change_class": "architecture-authority",
  "required_review": "five-sovereign-audit-unanimous-pass",
  "flow": "A382/A488-non-disruptive-amendment-flow",
  "not_executed": true,
  "predecessor": {
    "codex_version": "<live version>",
    "version_identity": "E<n>:<version>",
    "version_epoch": 2,
    "history_head": "<sha256>",
    "revision_sequence": 0
  },
  "changes": [
    { "table": "project_architecture_directory",
      "key": { "architecture_code": "X" },
      "field": "physical_root",
      "proposed": "..." }
  ],
  "acceptance": {
    "expected_state": "<post-state>",
    "criteria": ["<check>"],
    "evidence": ["<path or id>"],
    "out_of_scope": ["<untouched>"]
  },
  "supersedes": "<prior request_id, optional>",
  "supersede_reason": "<why resubmitted, optional>"
}
```
