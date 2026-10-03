# Xstore native-SQL managed binding

`GPTBridge.XstoreSql` is the managed lane over the `xstore_sql_query_json`
C ABI exported by the `xstore` Rust cdylib
(`xingcheng/src/backend/rust/xstore`, header `include/xstore_sql.h`).
It gives C# consumers an **in-process** native SQL read path — no external
database tool, service or subprocess — matching the native-SQL retirement
principles (project-owned in-process SQL as the structured authority
target).

The engine is read-only and bounded: `SELECT` over the registered `rag_*`
tables of a migration-sealed canonical store, positional `$n` parameters
carried in the request envelope (never interpolated into SQL text),
`AND`-joined `=`/`ANY` predicates, `ORDER BY`, `LIMIT`. Every violation —
unregistered tables or columns, writes, unsupported syntax, oversized
envelopes, unmigrated stores — fails closed and surfaces here as
`InvalidDataException` carrying the engine's `NATIVE_SQL_*` reason.

```csharp
using var sql = new XstoreSql("xstore.dll"); // or default loader name
var rows = sql.Query(storePath,
    "SELECT generation_id FROM rag_generation WHERE state = $1 LIMIT $2",
    new object?[] { "ACTIVE", 10 });
```

Ownership contract: the reply buffer is engine-owned and released exactly
once via `xstore_sql_buffer_free` inside `Query` before the JSON is
parsed. Request input is capped at 1 MiB client-side (the engine enforces
the same bound). A null result buffer means outstanding-capacity or
allocator failure and throws `NATIVE_SQL_CALL_FAILED`.
