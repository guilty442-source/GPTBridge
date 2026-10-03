//! Bounded native SQL reads over one canonical snapshot. No external engine.
//! Supported: SELECT columns|* FROM registered_table [WHERE predicates joined
//! by AND] [ORDER BY column [ASC|DESC]] [LIMIT $n]. Predicates are column=$n
//! or column=ANY($n); values are always bound separately, never SQL text.
//!
//! Domains: `Session::open` serves the sealed RAG store (static table and
//! column registry); `Session::open_codex` serves the sealed codex store
//! (`codex.rs`), whose registered tables and columns are derived from the
//! same marker-verified rows — PostgreSQL and external engines are never
//! consulted.
//!
//! `Session` carries a derived column index (`sql/index.rs`): the most
//! selective indexable predicate narrows candidates and the full
//! predicate filter re-runs on every candidate, so an index lookup
//! only shortens the canonical scan — it never changes a result.
use serde_json::{Map, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

mod index;

#[derive(PartialEq)]
enum Domain {
    Rag,
    Codex,
}

pub struct Session {
    rows: BTreeMap<String, Value>,
    index: index::Index,
    domain: Domain,
    /// Codex-domain registry derived from the sealed rows (unused under
    /// the RAG domain, which keeps its static registry).
    codex_tables: BTreeSet<String>,
    codex_columns: BTreeMap<String, BTreeSet<String>>,
}

enum Predicate {
    Equal(String, usize),
    Any(String, usize),
}
struct Plan {
    table: String,
    columns: Vec<String>,
    predicates: Vec<Predicate>,
    order: Option<(String, bool)>,
    limit: usize,
}

fn tokens(sql: &str) -> Result<Vec<String>, String> {
    if sql.len() > 4096 {
        return Err("NATIVE_SQL_STATEMENT_LIMIT".into());
    }
    let mut result = Vec::new();
    let mut word = String::new();
    for c in sql.chars() {
        if c.is_ascii_whitespace() || "*,=()".contains(c) {
            if !word.is_empty() {
                result.push(std::mem::take(&mut word));
            }
            if !c.is_ascii_whitespace() {
                result.push(c.to_string());
            }
        } else if c.is_ascii_alphanumeric() || c == '_' || c == '$' {
            word.push(c);
        } else {
            return Err("NATIVE_SQL_UNSUPPORTED_TOKEN".into());
        }
    }
    if !word.is_empty() {
        result.push(word);
    }
    Ok(result)
}
struct Parser {
    tokens: Vec<String>,
    position: usize,
}
impl Parser {
    fn peek(&self) -> &str {
        self.tokens
            .get(self.position)
            .map(String::as_str)
            .unwrap_or("")
    }
    fn take(&mut self) -> String {
        let value = self.peek().to_owned();
        self.position += 1;
        value
    }
    fn eat(&mut self, token: &str) -> bool {
        if self.peek().eq_ignore_ascii_case(token) {
            self.position += 1;
            true
        } else {
            false
        }
    }
    fn need(&mut self, token: &str) -> Result<(), String> {
        if self.eat(token) {
            Ok(())
        } else {
            Err(format!("NATIVE_SQL_EXPECTED_{token}"))
        }
    }
    fn identifier(&mut self) -> Result<String, String> {
        let value = self.take();
        if value.is_empty()
            || !value.bytes().next().unwrap().is_ascii_alphabetic()
            || !value
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
        {
            return Err("NATIVE_SQL_IDENTIFIER_INVALID".into());
        }
        Ok(value)
    }
    fn parameter(&mut self, params: &[Value]) -> Result<usize, String> {
        let value = self.take();
        let number = value
            .strip_prefix('$')
            .and_then(|s| s.parse::<usize>().ok())
            .filter(|n| *n > 0 && *n <= params.len())
            .ok_or("NATIVE_SQL_PARAMETER_REQUIRED")?;
        Ok(number - 1)
    }
}
fn column_ok(table: &str, column: &str) -> bool {
    if column == "record_id" {
        return true;
    }
    let fields: &[&str] = match table {
        "rag_generation" => &["generation_id", "alias_name", "state", "metadata"],
        "rag_chunk" => &[
            "chunk_id",
            "resource_id",
            "module_id",
            "vector_point_id",
            "sequence",
            "character_start",
            "character_end",
            "metadata",
        ],
        "rag_resource" => &["resource_id", "module_id", "index_status", "metadata"],
        "rag_tombstone" => &[
            "tombstone_id",
            "resource_id",
            "module_id",
            "purged",
            "metadata",
        ],
        "rag_index_state" => &["resource_id", "module_id", "status", "metadata"],
        _ => &[],
    };
    fields.contains(&column)
}
fn parse(
    sql: &str,
    params: &[Value],
    table_ok: &dyn Fn(&str) -> bool,
    column_ok: &dyn Fn(&str, &str) -> bool,
) -> Result<Plan, String> {
    if params.len() > 64 {
        return Err("NATIVE_SQL_PARAMETER_LIMIT".into());
    }
    let mut p = Parser {
        tokens: tokens(sql)?,
        position: 0,
    };
    p.need("SELECT")?;
    let mut columns = Vec::new();
    if !p.eat("*") {
        loop {
            columns.push(p.identifier()?);
            if !p.eat(",") {
                break;
            }
        }
    }
    p.need("FROM")?;
    let table = p.identifier()?;
    if !table_ok(&table) {
        return Err("NATIVE_SQL_TABLE_UNREGISTERED".into());
    }
    let mut predicates = Vec::new();
    if p.eat("WHERE") {
        loop {
            let field = p.identifier()?;
            p.need("=")?;
            let predicate = if p.eat("ANY") {
                p.need("(")?;
                let n = p.parameter(params)?;
                p.need(")")?;
                if !params[n].is_array() {
                    return Err("NATIVE_SQL_ARRAY_PARAMETER_REQUIRED".into());
                }
                let values = params[n].as_array().unwrap();
                if values.len() > 4096 || values.iter().any(|v| v.is_array() || v.is_object()) {
                    return Err("NATIVE_SQL_ARRAY_PARAMETER_INVALID".into());
                }
                Predicate::Any(field, n)
            } else {
                let n = p.parameter(params)?;
                if params[n].is_array() || params[n].is_object() {
                    return Err("NATIVE_SQL_SCALAR_PARAMETER_REQUIRED".into());
                }
                Predicate::Equal(field, n)
            };
            predicates.push(predicate);
            if !p.eat("AND") {
                break;
            }
        }
    }
    let order = if p.eat("ORDER") {
        p.need("BY")?;
        let col = p.identifier()?;
        let descending = p.eat("DESC");
        if !descending {
            p.eat("ASC");
        }
        Some((col, descending))
    } else {
        None
    };
    let limit = if p.eat("LIMIT") {
        let n = p.parameter(params)?;
        params[n]
            .as_u64()
            .filter(|n| *n <= 10000)
            .ok_or("NATIVE_SQL_LIMIT_INVALID")? as usize
    } else {
        10000
    };
    if p.position != p.tokens.len() {
        return Err("NATIVE_SQL_UNSUPPORTED_SYNTAX".into());
    }
    let mut fields = columns.clone();
    if columns
        .iter()
        .collect::<std::collections::HashSet<_>>()
        .len()
        != columns.len()
    {
        return Err("NATIVE_SQL_DUPLICATE_COLUMN".into());
    }
    for pred in &predicates {
        fields.push(match pred {
            Predicate::Equal(c, _) | Predicate::Any(c, _) => c.clone(),
        });
    }
    if let Some((c, _)) = &order {
        fields.push(c.clone());
    }
    if fields.iter().any(|c| !column_ok(&table, c)) {
        return Err("NATIVE_SQL_COLUMN_UNREGISTERED".into());
    }
    Ok(Plan {
        table,
        columns,
        predicates,
        order,
        limit,
    })
}
fn equal(a: &Value, b: &Value) -> bool {
    !a.is_null() && !b.is_null() && a == b
}
fn compare(a: &Value, b: &Value) -> std::cmp::Ordering {
    match (a, b) {
        (Value::Null, Value::Null) => std::cmp::Ordering::Equal,
        (Value::Null, _) => std::cmp::Ordering::Greater,
        (_, Value::Null) => std::cmp::Ordering::Less,
        (Value::Number(a), Value::Number(b)) => {
            if let (Some(a), Some(b)) = (a.as_i64(), b.as_i64()) {
                a.cmp(&b)
            } else if let (Some(a), Some(b)) = (a.as_u64(), b.as_u64()) {
                a.cmp(&b)
            } else if a.as_i64().map(|n| n < 0) == Some(true) && b.as_u64().is_some() {
                std::cmp::Ordering::Less
            } else if b.as_i64().map(|n| n < 0) == Some(true) && a.as_u64().is_some() {
                std::cmp::Ordering::Greater
            } else {
                a.as_f64()
                    .unwrap_or(0.0)
                    .total_cmp(&b.as_f64().unwrap_or(0.0))
            }
        }
        (Value::String(a), Value::String(b)) => a.cmp(b),
        _ => a.to_string().cmp(&b.to_string()),
    }
}
impl Session {
    pub fn open(store: &Path) -> Result<Self, String> {
        let rows = crate::rag::read(store)?;
        Ok(Self::rag_rows(rows))
    }
    /// Sealed codex store (`codex.rs` read path): `codex_row/{table}/{id}`
    /// payloads flatten into `{table}/{id}` rows carrying the exported
    /// columns plus `record_id` (the key suffix, matching RAG layout).
    /// The registered table/column sets are derived from the same rows
    /// the VERIFIED marker already pinned, so no separate registry can
    /// drift from the sealed data.
    pub fn open_codex(store: &Path) -> Result<Self, String> {
        let sealed = crate::codex::read(store)?;
        let mut rows = BTreeMap::new();
        let mut tables = BTreeSet::new();
        let mut columns: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for payload in sealed.into_values() {
            let table = payload["table"]
                .as_str()
                .ok_or("CODEX_ROW_TABLE_INVALID")?;
            let record_id = payload["record_id"]
                .as_str()
                .ok_or("CODEX_ROW_ID_INVALID")?;
            let suffix = record_id
                .strip_prefix(&format!("{table}/"))
                .ok_or("CODEX_ROW_ID_MISMATCH")?;
            let mut row = payload["row"]
                .as_object()
                .ok_or("CODEX_ROW_SHAPE")?
                .clone();
            row.insert("record_id".into(), Value::from(suffix));
            tables.insert(table.to_string());
            columns
                .entry(table.to_string())
                .or_default()
                .extend(row.keys().cloned());
            rows.insert(record_id.to_string(), Value::Object(row));
        }
        let index = index::Index::build(&rows);
        Ok(Self {
            rows,
            index,
            domain: Domain::Codex,
            codex_tables: tables,
            codex_columns: columns,
        })
    }
    /// Pure in-memory evaluation; caller-owned rows carry no authority claim.
    /// Production consumers use `open` or independently verified canonical replay.
    pub fn from_rows(rows: BTreeMap<String, Value>) -> Self {
        Self::rag_rows(rows)
    }
    fn rag_rows(rows: BTreeMap<String, Value>) -> Self {
        let index = index::Index::build(&rows);
        Self {
            rows,
            index,
            domain: Domain::Rag,
            codex_tables: BTreeSet::new(),
            codex_columns: BTreeMap::new(),
        }
    }
    fn table_ok(&self, table: &str) -> bool {
        match self.domain {
            Domain::Rag => crate::rag::TYPES.contains(&table),
            Domain::Codex => self.codex_tables.contains(table),
        }
    }
    fn column_ok(&self, table: &str, column: &str) -> bool {
        match self.domain {
            Domain::Rag => column_ok(table, column),
            Domain::Codex => {
                column == "record_id"
                    || self
                        .codex_columns
                        .get(table)
                        .is_some_and(|set| set.contains(column))
            }
        }
    }
    pub fn query(&self, sql: &str, params: &[Value]) -> Result<Vec<Value>, String> {
        let plan = parse(
            sql,
            params,
            &|table| self.table_ok(table),
            &|table, column| self.column_ok(table, column),
        )?;
        let keys = self.narrow(&plan, params);
        evaluate(&self.rows, &plan, params, keys)
    }
    /// The most selective indexable predicate narrows the candidate
    /// set; the full predicate filter still runs on every candidate,
    /// so an index lookup can only shorten the scan, never lose a row.
    /// `None` = no indexable predicate — canonical prefix scan.
    fn narrow(
        &self,
        plan: &Plan,
        params: &[Value],
    ) -> Option<BTreeSet<String>> {
        let mut best: Option<BTreeSet<String>> = None;
        for pred in &plan.predicates {
            let candidates = match pred {
                Predicate::Equal(field, n) => self
                    .index
                    .candidates(&plan.table, field, &params[*n])
                    .map(|keys| keys.iter().cloned().collect()),
                Predicate::Any(field, n) => params[*n]
                    .as_array()
                    .and_then(|values| self.index.any_candidates(&plan.table, field, values)),
            };
            let Some(set) = candidates else { continue };
            if best.as_ref().map(|b| set.len() < b.len()).unwrap_or(true) {
                best = Some(set);
            }
        }
        best
    }
    /// Pure SQL evaluation over a caller-owned snapshot; does not grant
    /// authority. Keeps the RAG registry (canonical callers' semantics).
    pub fn query_rows(
        rows: &BTreeMap<String, Value>,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Value>, String> {
        let plan = parse(
            sql,
            params,
            &|table| crate::rag::TYPES.contains(&table),
            &column_ok,
        )?;
        evaluate(rows, &plan, params, None)
    }
}

fn predicate_ok(row: &Value, plan: &Plan, params: &[Value]) -> bool {
    plan.predicates.iter().all(|pred| match pred {
        Predicate::Equal(field, n) => equal(&row[field], &params[*n]),
        Predicate::Any(field, n) => params[*n]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| equal(&row[field], v)),
    })
}

fn evaluate(
    rows: &BTreeMap<String, Value>,
    plan: &Plan,
    params: &[Value],
    keys: Option<BTreeSet<String>>,
) -> Result<Vec<Value>, String> {
    let mut result: Vec<&Value> = match &keys {
        Some(keys) => keys
            .iter()
            .filter_map(|k| rows.get(k.as_str()))
            .filter(|row| predicate_ok(row, plan, params))
            .collect(),
        None => {
            let prefix = format!("{}/", plan.table);
            rows.range(prefix.clone()..)
                .take_while(|(key, _)| key.starts_with(&prefix))
                .map(|(_, row)| row)
                .filter(|row| predicate_ok(row, plan, params))
                .collect()
        }
    };
    if let Some((field, descending)) = &plan.order {
        result.sort_by(|a, b| {
            let c = compare(&a[field], &b[field]);
            if *descending {
                c.reverse()
            } else {
                c
            }
        });
    }
    // Explicit LIMIT is intentional selection. Implicit overflow is rejected,
    // so missing authority rows can never be hidden by an undocumented cap.
    if result.len() > 10000 {
        return Err("NATIVE_SQL_RESULT_LIMIT".into());
    }
    result.truncate(plan.limit);
    Ok(result
        .into_iter()
        .map(|row| {
            if plan.columns.is_empty() {
                row.clone()
            } else {
                Value::Object(
                    plan.columns
                        .iter()
                        .map(|c| (c.clone(), row[c].clone()))
                        .collect::<Map<_, _>>(),
                )
            }
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn session() -> Session {
        Session::from_rows(BTreeMap::from([
            (
                "rag_chunk/a".into(),
                json!({"record_id":"a","module_id":"m","chunk_id":"a","sequence":2}),
            ),
            (
                "rag_chunk/b".into(),
                json!({"record_id":"b","module_id":"m","chunk_id":"b","sequence":1}),
            ),
            (
                "rag_chunk/c".into(),
                json!({"record_id":"c","module_id":null,"chunk_id":"c","sequence":0}),
            ),
        ]))
    }
    #[test]
    fn parameters_projection_order_and_limit() {
        let rows=session().query("SELECT chunk_id, sequence FROM rag_chunk WHERE module_id = $1 AND chunk_id = ANY($2) ORDER BY sequence ASC LIMIT $3",&[json!("m"),json!(["a","b"]),json!(1)]).unwrap();
        assert_eq!(
            rows,
            json!([{"chunk_id":"b","sequence":1}])
                .as_array()
                .unwrap()
                .clone()
        );
    }
    #[test]
    fn bound_strings_cannot_change_sql_and_null_never_matches() {
        assert!(session()
            .query(
                "SELECT * FROM rag_chunk WHERE module_id = $1",
                &[json!("m' OR 1=1")]
            )
            .unwrap()
            .is_empty());
        assert!(session()
            .query(
                "SELECT * FROM rag_chunk WHERE module_id = $1",
                &[Value::Null]
            )
            .unwrap()
            .is_empty());
    }
    #[test]
    fn unsupported_statements_literals_columns_and_unbound_parameters_fail() {
        for sql in [
            "DELETE FROM rag_chunk",
            "SELECT * FROM rag_chunk; DROP TABLE rag_chunk",
            "SELECT * FROM rag_chunk WHERE module_id = 'm'",
            "SELECT secret FROM rag_chunk",
            "SELECT * FROM unknown",
            "SELECT * FROM rag_chunk WHERE module_id = $0",
            "SELECT * FROM rag_chunk JOIN rag_resource",
        ] {
            assert!(session().query(sql, &[]).is_err(), "{sql}");
        }
        assert!(session()
            .query("SELECT * FROM rag_chunk LIMIT $1", &[json!(10001)])
            .is_err());
    }
    #[test]
    fn canonical_sql_session_requires_migration_and_preserves_its_snapshot() {
        let root = std::env::temp_dir().join(crate::meta_types::new_id("native-sql-test"));
        assert!(Session::open(&root).is_err());
        let mut source = json!({});
        for table in crate::rag::TYPES {
            source[*table] = json!([]);
        }
        source["rag_generation"] =
            json!([{"record_id":"g","generation_id":"g1","alias_name":"rag","state":"ACTIVE"}]);
        crate::rag::migrate(&root, &source).unwrap();
        let session = Session::open(&root).unwrap();
        assert_eq!(
            session
                .query(
                    "SELECT generation_id FROM rag_generation WHERE alias_name = $1",
                    &[json!("rag")]
                )
                .unwrap(),
            vec![json!({"generation_id":"g1"})]
        );
        crate::meta_tx::mutate(&root,"put",&json!({"record_type":"rag_generation","record":{"record_id":"g","generation_id":"g2","alias_name":"rag","state":"ACTIVE"}}),"test").unwrap();
        assert!(Session::open(&root).is_err());
        assert_eq!(
            session
                .query("SELECT generation_id FROM rag_generation", &[])
                .unwrap(),
            vec![json!({"generation_id":"g1"})]
        );
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn integer_order_is_exact_and_complex_any_parameters_are_rejected() {
        let rows = BTreeMap::from([
            (
                "rag_chunk/a".into(),
                json!({"sequence":9007199254740993u64}),
            ),
            (
                "rag_chunk/b".into(),
                json!({"sequence":9007199254740992u64}),
            ),
        ]);
        let session = Session::from_rows(rows);
        let result = session
            .query("SELECT sequence FROM rag_chunk ORDER BY sequence ASC", &[])
            .unwrap();
        assert_eq!(result[0]["sequence"], json!(9007199254740992u64));
        assert!(session
            .query(
                "SELECT * FROM rag_chunk WHERE module_id = ANY($1)",
                &[json!([{}])]
            )
            .is_err());
        assert!(session
            .query("SELECT sequence, sequence FROM rag_chunk", &[])
            .is_err());
    }
    #[test]
    fn indexed_and_scanned_queries_agree() {
        let mut map = BTreeMap::new();
        for i in 0..40 {
            map.insert(
                format!("rag_chunk/c{i}"),
                json!({"record_id":format!("c{i}"),
                    "module_id":if i%3==0{"m"}else{"n"},
                    "sequence":i,"resource_id":format!("r{}",i%5),
                    "vector_point_id":format!("p{i}")}),
            );
        }
        map.insert(
            "rag_chunk/odd".into(),
            json!({"record_id":"odd","module_id":null,"sequence":-0.0}),
        );
        let session = Session::from_rows(map.clone());
        for (sql, params) in [
            ("SELECT * FROM rag_chunk", vec![]),
            (
                "SELECT * FROM rag_chunk WHERE module_id = $1",
                vec![json!("m")],
            ),
            (
                "SELECT * FROM rag_chunk WHERE module_id = $1",
                vec![json!("absent")],
            ),
            (
                "SELECT * FROM rag_chunk WHERE module_id = $1",
                vec![Value::Null],
            ),
            (
                "SELECT * FROM rag_chunk WHERE module_id = ANY($1)",
                vec![json!(["m", "n"])],
            ),
            (
                "SELECT * FROM rag_chunk WHERE sequence = $1",
                vec![json!(-0.0)],
            ),
            (
                "SELECT chunk_id, sequence FROM rag_chunk WHERE resource_id = ANY($1) ORDER BY sequence DESC LIMIT $2",
                vec![json!(["r0", "r1"]), json!(3)],
            ),
            (
                "SELECT * FROM rag_chunk WHERE record_id = $1",
                vec![json!("c7")],
            ),
            (
                "SELECT * FROM rag_chunk WHERE module_id = $1 AND sequence = $2",
                vec![json!("m"), json!(3)],
            ),
        ] {
            let indexed = session.query(sql, &params).unwrap();
            let scanned = Session::query_rows(&map, sql, &params).unwrap();
            assert_eq!(indexed, scanned, "{sql}");
        }
    }
    #[test]
    fn codex_domain_serves_sealed_tables_and_fails_closed() {
        let root = std::env::temp_dir().join(crate::meta_types::new_id("codex-sql-test"));
        assert!(Session::open_codex(&root)
            .err()
            .unwrap()
            .contains("MIGRATION_REQUIRED"));
        let tables = json!({
            "articles":[
                {"provision_id":"A1","body":"x","rank":2},
                {"provision_id":"A2","body":"y","rank":1},
                {"provision_id":"A2","body":"z","rank":3}
            ],
            "sovereigns":[{"name":"s","seat":1}]
        });
        let row_count: u64 = tables
            .as_object()
            .unwrap()
            .values()
            .map(|rows| rows.as_array().unwrap().len() as u64)
            .sum();
        crate::codex::migrate(
            &root,
            &json!({"artifact":crate::codex::SNAPSHOT_FORMAT,
                "generation":"g","row_count":row_count,
                "table_count":tables.as_object().unwrap().len(),
                "tables":tables}),
        )
        .unwrap();
        let session = Session::open_codex(&root).unwrap();
        assert_eq!(
            session
                .query(
                    "SELECT provision_id, body FROM articles WHERE provision_id = $1 ORDER BY rank ASC",
                    &[json!("A2")]
                )
                .unwrap(),
            vec![
                json!({"provision_id":"A2","body":"y"}),
                json!({"provision_id":"A2","body":"z"})
            ]
        );
        assert_eq!(
            session
                .query(
                    "SELECT record_id FROM articles WHERE provision_id = ANY($1) LIMIT $2",
                    &[json!(["A1"]), json!(5)]
                )
                .unwrap()
                .len(),
            1
        );
        // Registry is derived from the sealed rows: absent tables and
        // columns fail closed, RAG tables do not leak across domains.
        for sql in [
            "SELECT * FROM no_such_table",
            "SELECT missing_column FROM articles",
            "SELECT * FROM rag_chunk",
            "SELECT * FROM articles WHERE missing_column = $1",
        ] {
            assert!(session.query(sql, &[json!("x")]).is_err(), "{sql}");
        }
        // Indexed path stays identical to the canonical prefix scan.
        let scan = |sql: &str, params: &[Value]| {
            let plan = parse(
                sql,
                params,
                &|table| session.table_ok(table),
                &|table, column| session.column_ok(table, column),
            )
            .unwrap();
            evaluate(&session.rows, &plan, params, None).unwrap()
        };
        for (sql, params) in [
            ("SELECT * FROM articles", vec![]),
            (
                "SELECT * FROM articles WHERE provision_id = $1",
                vec![json!("A2")],
            ),
            (
                "SELECT * FROM articles WHERE provision_id = ANY($1) ORDER BY rank DESC LIMIT $2",
                vec![json!(["A1", "A2"]), json!(2)],
            ),
            (
                "SELECT * FROM sovereigns WHERE seat = $1",
                vec![json!(1)],
            ),
        ] {
            assert_eq!(session.query(sql, &params).unwrap(), scan(sql, &params), "{sql}");
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
