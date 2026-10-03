//! Bounded native SQL reads over one canonical snapshot. No external engine.
//! Supported: SELECT columns|*|COUNT(*)|COUNT(col) FROM registered_table
//! [WHERE predicates joined by AND] [ORDER BY col [ASC|DESC] (, col...)]
//! [LIMIT $n | integer] [OFFSET $n | integer]. Predicates are column=$n,
//! column=ANY($n), column op $n for op in {=,<,<=,>,>=,<>,!=},
//! column BETWEEN $a AND $b, column LIKE $n (single trailing % prefix),
//! and column IS [NOT] NULL. Values are always bound separately, never
//! SQL text; ordering comparisons on incomparable types fail closed.
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

#[derive(Clone, Copy)]
enum Cmp {
    Lt,
    Le,
    Gt,
    Ge,
    Ne,
}
enum Predicate {
    Equal(String, usize),
    Any(String, usize),
    /// column op $n for op in {<,<=,>,>=,<>} (!= folds into <>).
    Compare(String, Cmp, usize),
    /// column BETWEEN $lo AND $hi — inclusive on both bounds.
    Between(String, usize, usize),
    /// column LIKE $n where the parameter is "prefix%" (single
    /// trailing wildcard, no other % or _ anywhere).
    Prefix(String, usize),
    /// column IS NULL; bool flag = negated (IS NOT NULL).
    IsNull(String, bool),
}
struct Plan {
    table: String,
    /// Empty = `*`. When `count` is set this must stay empty.
    columns: Vec<String>,
    /// Some(None) = COUNT(*); Some(Some(col)) = COUNT(col) — non-null
    /// values of `col` only.
    count: Option<Option<String>>,
    predicates: Vec<Predicate>,
    /// (column, descending) pairs applied left to right.
    order: Vec<(String, bool)>,
    limit: usize,
    offset: usize,
}

fn tokens(sql: &str) -> Result<Vec<String>, String> {
    if sql.len() > 4096 {
        return Err("NATIVE_SQL_STATEMENT_LIMIT".into());
    }
    let mut result = Vec::new();
    let mut word = String::new();
    for c in sql.chars() {
        if c.is_ascii_whitespace() || "*,=()<>!".contains(c) {
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
    let mut count: Option<Option<String>> = None;
    if !p.eat("*") {
        loop {
            if p.peek().eq_ignore_ascii_case("count") {
                if count.is_some() || !columns.is_empty() {
                    return Err("NATIVE_SQL_MIXED_PROJECTION".into());
                }
                p.take();
                p.need("(")?;
                let col = if p.eat("*") {
                    None
                } else {
                    Some(p.identifier()?)
                };
                p.need(")")?;
                count = Some(col);
            } else {
                if count.is_some() {
                    return Err("NATIVE_SQL_MIXED_PROJECTION".into());
                }
                columns.push(p.identifier()?);
            }
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
            if p.eat("IS") {
                let negated = p.eat("NOT");
                p.need("NULL")?;
                predicates.push(Predicate::IsNull(field, negated));
            } else if p.eat("BETWEEN") {
                let lo = p.parameter(params)?;
                p.need("AND")?;
                let hi = p.parameter(params)?;
                if params[lo].is_array() || params[lo].is_object()
                    || params[hi].is_array() || params[hi].is_object()
                {
                    return Err("NATIVE_SQL_SCALAR_PARAMETER_REQUIRED".into());
                }
                predicates.push(Predicate::Between(field, lo, hi));
            } else if p.eat("LIKE") {
                let n = p.parameter(params)?;
                let valid = params[n]
                    .as_str()
                    .and_then(|s| s.strip_suffix('%'))
                    .is_some_and(|body| !body.contains('%') && !body.contains('_'));
                if !valid {
                    return Err("NATIVE_SQL_LIKE_INVALID".into());
                }
                predicates.push(Predicate::Prefix(field, n));
            } else {
                let cmp = if p.eat("=") {
                    None
                } else if p.eat("<") {
                    if p.eat("=") {
                        Some(Cmp::Le)
                    } else if p.eat(">") {
                        Some(Cmp::Ne)
                    } else {
                        Some(Cmp::Lt)
                    }
                } else if p.eat(">") {
                    Some(if p.eat("=") { Cmp::Ge } else { Cmp::Gt })
                } else if p.eat("!") {
                    p.need("=")?;
                    Some(Cmp::Ne)
                } else {
                    return Err("NATIVE_SQL_OPERATOR_REQUIRED".into());
                };
                let predicate = match cmp {
                    None if p.eat("ANY") => {
                        p.need("(")?;
                        let n = p.parameter(params)?;
                        p.need(")")?;
                        if !params[n].is_array() {
                            return Err("NATIVE_SQL_ARRAY_PARAMETER_REQUIRED".into());
                        }
                        let values = params[n].as_array().unwrap();
                        if values.len() > 4096
                            || values.iter().any(|v| v.is_array() || v.is_object())
                        {
                            return Err("NATIVE_SQL_ARRAY_PARAMETER_INVALID".into());
                        }
                        Predicate::Any(field, n)
                    }
                    _ => {
                        let n = p.parameter(params)?;
                        if params[n].is_array() || params[n].is_object() {
                            return Err("NATIVE_SQL_SCALAR_PARAMETER_REQUIRED".into());
                        }
                        match cmp {
                            None => Predicate::Equal(field, n),
                            Some(op) => Predicate::Compare(field, op, n),
                        }
                    }
                };
                predicates.push(predicate);
            }
            if !p.eat("AND") {
                break;
            }
        }
    }
    let mut order = Vec::new();
    if p.eat("ORDER") {
        p.need("BY")?;
        loop {
            let col = p.identifier()?;
            let descending = p.eat("DESC");
            if !descending {
                p.eat("ASC");
            }
            order.push((col, descending));
            if !p.eat(",") {
                break;
            }
        }
        if order.len() > 4 {
            return Err("NATIVE_SQL_ORDER_LIMIT".into());
        }
    }
    let mut limit = 10000;
    let mut offset = 0;
    loop {
        let keyword = if p.eat("LIMIT") {
            true
        } else if p.eat("OFFSET") {
            false
        } else {
            break;
        };
        let value = if p.peek().starts_with('$') {
            let n = p.parameter(params)?;
            params[n]
                .as_u64()
                .filter(|n| *n <= 10000)
                .ok_or("NATIVE_SQL_LIMIT_INVALID")? as usize
        } else {
            p.take()
                .parse::<u64>()
                .ok()
                .filter(|n| *n <= 10000)
                .ok_or("NATIVE_SQL_LIMIT_INVALID")? as usize
        };
        if keyword {
            limit = value;
        } else {
            offset = value;
        }
    }
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
            Predicate::Equal(c, _)
            | Predicate::Any(c, _)
            | Predicate::Compare(c, _, _)
            | Predicate::Between(c, _, _)
            | Predicate::Prefix(c, _)
            | Predicate::IsNull(c, _) => c.clone(),
        });
    }
    for (c, _) in &order {
        fields.push(c.clone());
    }
    if let Some(Some(c)) = &count {
        fields.push(c.clone());
    }
    if fields.iter().any(|c| !column_ok(&table, c)) {
        return Err("NATIVE_SQL_COLUMN_UNREGISTERED".into());
    }
    Ok(Plan {
        table,
        columns,
        count,
        predicates,
        order,
        limit,
        offset,
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
                // Ordered/null/prefix tests are not indexable: they
                // never narrow, so the canonical candidate set (whole
                // table) stands.
                Predicate::Compare(..)
                | Predicate::Between(..)
                | Predicate::Prefix(..)
                | Predicate::IsNull(..) => None,
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

/// Ordering comparison for predicates: same-type scalars only —
/// numbers compare numerically (exact integer lanes first), strings
/// lexically, booleans by false<true. A null on either side yields no
/// match (not an error); any other type pair is incomparable and fails
/// the whole query closed.
fn cmp_scalars(a: &Value, b: &Value) -> Result<Option<std::cmp::Ordering>, String> {
    if a.is_null() || b.is_null() {
        return Ok(None);
    }
    match (a, b) {
        (Value::Number(_), Value::Number(_)) => Ok(Some(compare(a, b))),
        (Value::String(x), Value::String(y)) => Ok(Some(x.cmp(y))),
        (Value::Bool(x), Value::Bool(y)) => Ok(Some(x.cmp(y))),
        _ => Err("NATIVE_SQL_INCOMPARABLE".into()),
    }
}
fn predicate_ok(row: &Value, plan: &Plan, params: &[Value]) -> Result<bool, String> {
    for pred in &plan.predicates {
        let ok = match pred {
            Predicate::Equal(field, n) => equal(&row[field], &params[*n]),
            Predicate::Any(field, n) => params[*n]
                .as_array()
                .unwrap()
                .iter()
                .any(|v| equal(&row[field], v)),
            Predicate::Compare(field, Cmp::Ne, n) => {
                !row[field].is_null() && !params[*n].is_null() && row[field] != params[*n]
            }
            Predicate::Compare(field, op, n) => match cmp_scalars(&row[field], &params[*n])? {
                None => false,
                Some(std::cmp::Ordering::Less) => matches!(op, Cmp::Lt | Cmp::Le),
                Some(std::cmp::Ordering::Equal) => matches!(op, Cmp::Le | Cmp::Ge),
                Some(std::cmp::Ordering::Greater) => matches!(op, Cmp::Gt | Cmp::Ge),
            },
            Predicate::Between(field, lo, hi) => {
                if row[field].is_null() {
                    false
                } else {
                    match cmp_scalars(&row[field], &params[*lo])? {
                        None | Some(std::cmp::Ordering::Less) => false,
                        _ => match cmp_scalars(&row[field], &params[*hi])? {
                            None | Some(std::cmp::Ordering::Greater) => false,
                            _ => true,
                        },
                    }
                }
            }
            Predicate::Prefix(field, n) => {
                let body = params[*n]
                    .as_str()
                    .and_then(|s| s.strip_suffix('%'))
                    .unwrap_or("");
                row[field]
                    .as_str()
                    .is_some_and(|s| s.starts_with(body))
            }
            Predicate::IsNull(field, negated) => row[field].is_null() != *negated,
        };
        if !ok {
            return Ok(false);
        }
    }
    Ok(true)
}

fn evaluate(
    rows: &BTreeMap<String, Value>,
    plan: &Plan,
    params: &[Value],
    keys: Option<BTreeSet<String>>,
) -> Result<Vec<Value>, String> {
    fn keep<'a>(
        row: &'a Value,
        plan: &Plan,
        params: &[Value],
    ) -> Option<Result<&'a Value, String>> {
        match predicate_ok(row, plan, params) {
            Ok(true) => Some(Ok(row)),
            Ok(false) => None,
            Err(e) => Some(Err(e)),
        }
    }
    let mut result: Vec<&Value> = match &keys {
        Some(keys) => keys
            .iter()
            .filter_map(|k| rows.get(k.as_str()))
            .filter_map(|row| keep(row, plan, params))
            .collect::<Result<_, String>>()?,
        None => {
            let prefix = format!("{}/", plan.table);
            rows.range(prefix.clone()..)
                .take_while(|(key, _)| key.starts_with(&prefix))
                .map(|(_, row)| row)
                .filter_map(|row| keep(row, plan, params))
                .collect::<Result<_, String>>()?
        }
    };
    if !plan.order.is_empty() {
        result.sort_by(|a, b| {
            for (field, descending) in &plan.order {
                let c = compare(&a[field], &b[field]);
                let c = if *descending { c.reverse() } else { c };
                if c != std::cmp::Ordering::Equal {
                    return c;
                }
            }
            std::cmp::Ordering::Equal
        });
    }
    // COUNT answers the filtered set before offset/limit — a scalar
    // result cannot hide truncation behind a bound.
    if let Some(col) = &plan.count {
        let n = match col {
            None => result.len(),
            Some(col) => result.iter().filter(|r| !r[col].is_null()).count(),
        };
        return Ok(vec![serde_json::json!({"count": n})]);
    }
    // Explicit LIMIT is intentional selection. Implicit overflow is rejected,
    // so missing authority rows can never be hidden by an undocumented cap.
    if result.len() > 10000 {
        return Err("NATIVE_SQL_RESULT_LIMIT".into());
    }
    if plan.offset >= result.len() {
        result.clear();
    } else {
        result.drain(..plan.offset);
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
            (
                "SELECT * FROM rag_chunk WHERE sequence >= $1 AND sequence < $2",
                vec![json!(10), json!(20)],
            ),
            (
                "SELECT * FROM rag_chunk WHERE module_id IS NULL",
                vec![],
            ),
            (
                "SELECT * FROM rag_chunk WHERE module_id IS NOT NULL LIMIT 3",
                vec![],
            ),
            (
                "SELECT * FROM rag_chunk WHERE resource_id <> $1",
                vec![json!("r0")],
            ),
            (
                "SELECT * FROM rag_chunk WHERE resource_id != $1",
                vec![json!("r0")],
            ),
        ] {
            let indexed = session.query(sql, &params).unwrap();
            let scanned = Session::query_rows(&map, sql, &params).unwrap();
            assert_eq!(indexed, scanned, "{sql}");
        }
    }
    #[test]
    fn comparison_predicates_is_null_and_literal_limit() {
        let session = session();
        // Ordering comparisons run against the same bounded rows; < on a
        // string param is incomparable and fails the query closed.
        let rows = session
            .query(
                "SELECT chunk_id FROM rag_chunk WHERE sequence < $1 ORDER BY sequence DESC",
                &[json!(2)],
            )
            .unwrap();
        assert_eq!(rows, vec![json!({"chunk_id":"b"}), json!({"chunk_id":"c"})]);
        assert_eq!(
            session
                .query(
                    "SELECT chunk_id FROM rag_chunk WHERE sequence >= $1 AND sequence <= $2",
                    &[json!(1), json!(2)]
                )
                .unwrap()
                .len(),
            2
        );
        // Null never matches any comparison, and IS [NOT] NULL sees it.
        assert_eq!(
            session
                .query("SELECT chunk_id FROM rag_chunk WHERE module_id IS NULL", &[])
                .unwrap(),
            vec![json!({"chunk_id":"c"})]
        );
        assert_eq!(
            session
                .query("SELECT chunk_id FROM rag_chunk WHERE module_id IS NOT NULL ORDER BY chunk_id ASC LIMIT 1", &[])
                .unwrap(),
            vec![json!({"chunk_id":"a"})]
        );
        assert!(session
            .query("SELECT * FROM rag_chunk WHERE module_id != $1", &[Value::Null])
            .unwrap()
            .is_empty());
        // Literal LIMIT shares the parameter bound; overflow fails closed.
        assert_eq!(
            session
                .query("SELECT chunk_id FROM rag_chunk ORDER BY sequence ASC LIMIT 2", &[])
                .unwrap()
                .len(),
            2
        );
        for sql in [
            "SELECT * FROM rag_chunk WHERE module_id",
            "SELECT * FROM rag_chunk WHERE module_id IS",
            "SELECT * FROM rag_chunk WHERE module_id IS UNKNOWN",
            "SELECT * FROM rag_chunk LIMIT x",
            "SELECT * FROM rag_chunk LIMIT 10001",
            "SELECT * FROM rag_chunk LIMIT -1",
        ] {
            assert!(session.query(sql, &[]).is_err(), "{sql}");
        }
        // Missing parameter for the comparison itself also fails closed.
        assert!(session
            .query("SELECT * FROM rag_chunk WHERE sequence < $1", &[])
            .is_err());
    }
    #[test]
    fn between_like_offset_multiorder_and_count() {
        let rows = BTreeMap::from([
            ("rag_chunk/a".into(), json!({"record_id":"a","module_id":"m","chunk_id":"a1","sequence":3,"resource_id":"res-01"})),
            ("rag_chunk/b".into(), json!({"record_id":"b","module_id":"m","chunk_id":"b2","sequence":1,"resource_id":"res-02"})),
            ("rag_chunk/c".into(), json!({"record_id":"c","module_id":null,"chunk_id":"c3","sequence":2,"resource_id":"alt-01"})),
            ("rag_chunk/d".into(), json!({"record_id":"d","module_id":"n","chunk_id":"d4","sequence":4,"resource_id":"res-10"})),
        ]);
        let session = Session::from_rows(rows.clone());
        // BETWEEN is inclusive on both bounds and typed.
        let between = session
            .query("SELECT chunk_id FROM rag_chunk WHERE sequence BETWEEN $1 AND $2 ORDER BY sequence ASC", &[json!(2), json!(3)])
            .unwrap();
        assert_eq!(between, vec![json!({"chunk_id":"c3"}), json!({"chunk_id":"a1"})]);
        // LIKE is a bounded prefix: trailing % only.
        assert_eq!(
            session
                .query("SELECT chunk_id FROM rag_chunk WHERE resource_id LIKE $1 ORDER BY chunk_id", &[json!("res-%")])
                .unwrap(),
            vec![json!({"chunk_id":"a1"}), json!({"chunk_id":"b2"}), json!({"chunk_id":"d4"})]
        );
        for bad in [json!("%es"), json!("a%b"), json!("re_"), json!(5), Value::Null] {
            assert!(session
                .query("SELECT * FROM rag_chunk WHERE resource_id LIKE $1", &[bad])
                .is_err());
        }
        // Multi-column ORDER applies keys left to right.
        let ordered = session
            .query("SELECT chunk_id FROM rag_chunk ORDER BY module_id ASC, sequence DESC", &[])
            .unwrap();
        assert_eq!(
            ordered,
            vec![
                json!({"chunk_id":"a1"}),
                json!({"chunk_id":"b2"}),
                json!({"chunk_id":"d4"}),
                json!({"chunk_id":"c3"}),
            ]
        );
        // OFFSET skips after sorting; both $n and literal work.
        assert_eq!(
            session
                .query("SELECT chunk_id FROM rag_chunk ORDER BY sequence ASC LIMIT $1 OFFSET 1", &[json!(2)])
                .unwrap(),
            vec![json!({"chunk_id":"c3"}), json!({"chunk_id":"a1"})]
        );
        assert_eq!(
            session
                .query("SELECT chunk_id FROM rag_chunk ORDER BY sequence ASC OFFSET $1", &[json!(3)])
                .unwrap(),
            vec![json!({"chunk_id":"d4"})]
        );
        // COUNT answers the filtered set before OFFSET/LIMIT.
        assert_eq!(
            session.query("SELECT COUNT(*) FROM rag_chunk", &[]).unwrap(),
            vec![json!({"count":4})]
        );
        assert_eq!(
            session.query("SELECT COUNT(*) FROM rag_chunk WHERE module_id = $1 LIMIT 1", &[json!("m")]).unwrap(),
            vec![json!({"count":2})]
        );
        // COUNT(col) skips null values.
        assert_eq!(
            session.query("SELECT COUNT(module_id) FROM rag_chunk", &[]).unwrap(),
            vec![json!({"count":3})]
        );
        // Mixed projections and unbounded variants fail closed.
        for sql in [
            "SELECT chunk_id, COUNT(*) FROM rag_chunk",
            "SELECT COUNT(*), chunk_id FROM rag_chunk",
            "SELECT COUNT(*) FROM rag_chunk WHERE missing BETWEEN $1 AND $2",
            "SELECT * FROM rag_chunk WHERE sequence BETWEEN $1",
            "SELECT * FROM rag_chunk ORDER BY sequence DESC, chunk_id ASC, module_id, sequence, resource_id",
            "SELECT * FROM rag_chunk OFFSET 10001",
        ] {
            assert!(session.query(sql, &[json!(1), json!(9)]).is_err(), "{sql}");
        }
        // BETWEEN on incomparable bounds fails the query, not the row.
        assert!(session
            .query("SELECT * FROM rag_chunk WHERE sequence BETWEEN $1 AND $2", &[json!("x"), json!(9)])
            .is_err());
    }
    #[test]
    fn incomparable_ordering_and_bad_operators_fail_closed() {
        let session = session();
        // String parameter against numeric column: incomparable → query error.
        assert!(session
            .query(
                "SELECT * FROM rag_chunk WHERE sequence < $1",
                &[json!("1")]
            )
            .is_err());
        // Numeric parameter against a column holding a null row still
        // succeeds — null rows simply never match the comparison.
        assert_eq!(
            session
                .query(
                    "SELECT chunk_id FROM rag_chunk WHERE module_id = $1 AND sequence <= $2",
                    &[json!("m"), json!(2)]
                )
                .unwrap()
                .len(),
            2
        );
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
