const fs = require('node:fs');
const crypto = require('node:crypto');
const sql = fs.readFileSync('governance_rule/codex/data/governance_codex.sql', 'utf8');
function rows(table) {
  return sql.split(/\r?\n/).filter(s => s.startsWith(`INSERT INTO "${table}" `)).map(s => {
    const columns = [...s.slice(0, s.indexOf(' VALUES ')).matchAll(/"([^"]+)"/g)].slice(1).map(m => m[1]);
    const values = [...s.slice(s.indexOf(' VALUES ')).matchAll(/E'((?:\\.|[^'\\])*)'|\bNULL\b|-?\d+(?:\.\d+)?/g)].map(m => m[1] !== undefined ? m[1].replace(/\\(.)/gs, (_, c) => ({n:'\n',r:'\r',t:'\t'}[c] ?? c)) : m[0] === 'NULL' ? null : Number(m[0]));
    if (values.length !== columns.length) throw Error(`SQL_PARSE:${table}:${values.length}/${columns.length}`);
    return Object.fromEntries(columns.map((c,i) => [c,values[i]]));
  });
}
const active = new Set(rows('provision_lifecycle_status').filter(r => r.lifecycle_state === 'active').map(r => r.provision_id));
const schemas = rows('machine_schema_registry');
const evidence = rows('machine_schema_parity_evidence');
console.log(JSON.stringify({head:rows('revision_history').at(-1),laws:rows('articles').filter(r => active.has(r.provision_id) && /sub-sovereign/i.test(r.rule)).map(r=>({id:r.provision_id,clauses:r.rule.split(';').filter(c=>/sub-sovereign/i.test(c))})),schemaCount:schemas.length,evidenceCount:evidence.length},null,2));
