# Native Codex snapshot reads

`GPTBridge.NativeCodex` uses only the .NET runtime. It does not launch query tools
or depend on PostgreSQL drivers. This library is a **read-only migration
validation surface**; it does not change Codex authority, write records, certify
release, or replace the canonical Rust SQL writer.

Load `NativeCodexSnapshot.Read` with externally supplied generation, SHA256,
row count and table count. Missing pins, generation drift, modified bytes,
duplicate table/column names and count mismatches are rejected. The source
bridge `NativeCodexMigration.Capture` belongs to the existing Codex pipeline:
it reads PostgreSQL once in a read-only repeatable-read transaction and never
mutates that source. The source bridge is not part of this runtime library.

`NativeCodexSql.Query` supports this bounded SQL subset:

```sql
SELECT codex_version FROM codex_authority_state
WHERE codex_version = @version ORDER BY codex_version LIMIT 1
```

Supported: named column projections or `*`, one table, comparisons (`=`, `<>`,
`!=`, `<`, `>`, `<=`, `>=`) joined by `AND`, scalar named parameters, quoted
identifiers/string literals, `ORDER BY` with `ASC`/`DESC`, and `LIMIT`.
Unquoted identifiers fold to lowercase. Strings compare ordinally. Null
comparisons are unknown and do not match; ascending sorts place nulls last.

Default result limit: 1,000; maximum: 10,000. Default scanned-row limit:
100,000; callers may lower it. Unsupported syntax, multiple statements, writes,
joins, missing parameters/columns and incomparable value types fail closed.
No SQL text or parameter is passed to a shell or external executable.
