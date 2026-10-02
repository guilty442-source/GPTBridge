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
const laws = rows('articles');
if (process.argv.includes('--finalize')) {
  const head=rows('revision_history').at(-1);
  const clauses={
    C102:' EVIDENCE-INVARIANT:current file-registry evidence measures the persisted post-rebuild state of the authoritative active artifact set;PASS requires required=registered=present=hash-match,stale=missing=0,and every applicable read-only requirement satisfied.Pre-repair drift counts are separate historical diagnostics and never current PASS measurements.File-hash parity does not certify unresolved runtime or semantic implementation completion.Current active-law stale-role scanning is lifecycle-joined and historical-only references are separated from operative delegation.',
    D115:' EVIDENCE-PROVENANCE:producer,validator,persistence and canonical semantic hashes require independently attributable executed evidence for the current registered schema and binding generation;copying one descriptor digest into four fields or merely restamping a version proves no implementation parity. COMPLETION:the machine-schema parity obligation is complete only when every current registered schema has current evidenced parity;PENDING,missing,stale or contradictory rows reopen the obligation and deny affected activation or verified release.Historical measurements remain explicitly historical and are not silently promoted.'
  };
  console.log(JSON.stringify({artifact:'codex-amendment-request',authority:'request-only',schema:'codex-amendment-request/v1',request_id:'current-evidence-invariants-finalize-20261002',title:'Enforce measured current evidence and independently proven schema parity',summary:'Bind current sync PASS to persisted post-rebuild parity and applicable file requirements;deny descriptor-only schema closure.',requested_by:'decision-sovereign',origin:'direct human-governor evidence-convergence instruction;no application runtime implementation',change_class:'architecture-authority',required_review:'five-sovereign-audit-unanimous-pass',flow:'A382/A488-non-disruptive-amendment-flow',not_executed:true,auto_execute:true,predecessor:{codex_version:head.version,version_identity:`E${head.version_epoch}:${head.version}`,version_epoch:head.version_epoch,history_head:head.entry_hash,revision_sequence:head.sequence},changes:Object.entries(clauses).map(([id,clause])=>({table:'articles',key:{provision_id:id},field:'rule',proposed:laws.find(r=>r.provision_id===id).rule+clause}))},null,2));
} else if (process.argv.includes('--request')) {
  const changes = [];
  const update = (table,key,field,proposed) => changes.push({table,key,field,proposed});
  const replacements = {
    A24:[['codex/sub-sovereign/executor/module/service','codex/executor/module/service']],
    A34:[['SUB-SOVEREIGNS:module management and assignment only','SUB-SOVEREIGNS:abolished and historical only; CURRENT-OWNERS:B162 five-core responsibility boundaries govern decisions,runtime execution,automation orchestration,permission enforcement and independent audit']],
    A51:[['SUB-SOVEREIGN-V3-parent-delegated-scope-only','REGISTERED-MODULE-V3-explicit-owner-scoped-grant-only']],
    A54:[['OTHER-SOVEREIGNS/SUB-SOVEREIGNS/MODULES','OTHER-SOVEREIGNS/MODULES']],
    A61:[['sovereign+sub-sovereign+module','sovereign+module']],
    A71:[['sovereign/sub-sovereign/module','sovereign/module']],
    A93:[['+sub-sovereign hierarchy',''],['CHILDREN:permission-sovereign may structurally own declared no-decision no-review no-execution sub-sovereigns for module management but cannot transfer authorization or acquire review through them','CHILDREN:permission-core may own registered enforcement and directory modules with no independent authority;no sub-sovereign layer exists and permission enforcement cannot acquire execution or independent review authority']],
    A100:[['and all sovereign/sub-sovereign canonical names remain authoritative','and current registered core canonical names remain authoritative; retired sovereign and sub-sovereign names are historical lineage only under B162'],['SPLIT:resource and dependency synchronization use separate single-duty sub-sovereigns','SPLIT:resource and dependency synchronization are separate single-duty registered modules under automation-core orchestration and C50,with runtime-core execution,permission-core enforcement,decision-core acceptance and independent audit as applicable']],
    A117:[['SUB-SOVEREIGNS:may parallelize dispatch planning,module supervision and evidence collection but possess no actual module execution power','AUTOMATION-CORE:may orchestrate bounded dispatch planning,module supervision and evidence collection under C50 without generating permissions or accepting its own results']],
    B92:[['EXECUTION:top-level sovereigns decide,sub-sovereigns manage and dispatch,module-execution components alone execute','EXECUTION:B162 current cores retain separate decision,runtime,automation,permission and independent-audit duties;automation-core orchestrates under C50 and registered module-execution components alone perform authorized business work']],
    B107:[['centrally managed by the maintenance sub-sovereign','scheduled by registered test-management modules under automation-core and C50,with independent acceptance evidence']],
    B112:[['Runtime decisions remain decision-only and execution remains delegated through the system sub-sovereign to the module execution layer.','Runtime authority follows runtime-core under B162:governed process lifecycle and execution use registered modules directly,without an intermediate sub-sovereign,no self-authorization or policy modification,and C50 governs orchestration and receipt convergence.']],
    C30:[['+sub-sovereigns','']],
    D124:[['ACCESS:sub-sovereigns have no direct Codex view and may use only an explicit bounded parent-sovereign proxy session.','ACCESS:registered modules have no inherent Codex access;only an explicitly authorized bounded adapter session under A121 may mediate permitted read-only access,without delegated sovereign credentials or authority.']]
  };
  const rewritten = {
    A74:'CONTROL:B162 is the current five-core responsibility boundary. DECISION:decision-core owns policy judgment and result acceptance,without module execution or permission generation. RUNTIME:runtime-core controls governed startup,stop,restart and authorized execution,without self-authorization or policy modification. AUTOMATION:automation-core orchestrates bounded workflows and registered modules under C50,without permission generation or result acceptance. PERMISSION:permission-core enforces registered grants and scope,without business execution. AUDIT:xingcheng-assistant independently reviews typed evidence and notifies,without repair,upgrade or learning ownership. FLOW:evidence>current decision basis+permission enforcement>automation orchestration>runtime governed module execution>typed result>independent verification>decision-core acceptance. EMERGENCY:bounded stop-only containment remains attributable,permission-scoped and independently verified. SEPARATION:decision,execution and sole final verification cannot collapse into one actor. SUB-SOVEREIGNS:abolished and historical only.',
    A92:'CURRENT:B162 alone fixes the five-core boundaries. decision-core owns judgment and acceptance;runtime-core owns governed process lifecycle and authorized registered-module execution;automation-core owns C50 orchestration and synchronization;permission-core owns enforcement and directory/audit recording;xingcheng-assistant owns independent review and notification. FLOW:current decision basis+permission enforcement>automation-core bounded orchestration>runtime-core registered module execution>typed evidence>independent review>decision-core acceptance. OWNER:each module has one registered owner and one primary duty;coordination grants no extra authority. SUB-SOVEREIGNS:abolished and historical only. FAILURE:missing,current-generation-inconsistent or self-certified evidence remains incomplete and fails closed.'
  };
  for (const law of laws) {
    if (!active.has(law.provision_id)) continue;
    let next = rewritten[law.provision_id] ?? law.rule;
    for (const [before,after] of replacements[law.provision_id] ?? []) {
      if (!next.includes(before)) throw Error(`LAW_ANCHOR:${law.provision_id}:${before}`);
      next = next.replace(before,after);
    }
    if (next !== law.rule) update('articles',{provision_id:law.provision_id},'rule',next);
  }
  update('implementation_obligations',{obligation_code:'OBL_MACHINE_SCHEMA_PARITY'},'current_state','pending');
  update('implementation_obligations',{obligation_code:'OBL_MACHINE_SCHEMA_PARITY'},'previous_state','complete');
  update('implementation_obligations',{obligation_code:'OBL_MACHINE_SCHEMA_PARITY'},'target_state','every current registered schema has independently evidenced producer,validator,persistence and canonical semantic hashes with exact current-generation parity;descriptor self-restamp alone is not implementation evidence');
  update('implementation_obligations',{obligation_code:'OBL_MACHINE_SCHEMA_PARITY'},'acceptance_evidence',`PENDING: ${schemas.length} registry rows remain PENDING;${evidence.length} legacy descriptor-restamp rows are not independent producer/validator/persistence execution evidence;missing new-schema evidence and generation/provenance verification must close before completion`);
  update('metadata',{key:'machine_schema_parity_state'},'value',`PENDING|PENDING:${schemas.length}`);
  for (const r of evidence) {
    update('machine_schema_parity_evidence',{schema_code:r.schema_code},'status','PENDING');
    update('machine_schema_parity_evidence',{schema_code:r.schema_code},'reason','Legacy descriptor-only restamp;retained hashes and validation generation are historical measurements,not current independent producer/validator/persistence parity evidence. Current schema parity remains PENDING.');
  }
  const absent = schemas.filter(s=>!evidence.some(e=>e.schema_code===s.schema_code));
  const head = rows('revision_history').at(-1);
  const request = {artifact:'codex-amendment-request',authority:'request-only',schema:'codex-amendment-request/v1',request_id:'current-law-projection-evidence-convergence-20261002',title:'Converge current law, deterministic projections and honest parity evidence',summary:'Remove active abolished-role delegation;rebuild diagram post-state evidence and A233 canonical payloads;reopen unproven schema parity;retain implementation gaps.',requested_by:'decision-sovereign',origin:'direct human-governor audit and instruction:Codex/projection corrections only;no application runtime implementation',change_class:'architecture-authority',required_review:'five-sovereign-audit-unanimous-pass',flow:'A382/A488-non-disruptive-amendment-flow',not_executed:true,auto_execute:true,predecessor:{codex_version:head.version,version_identity:`E${head.version_epoch}:${head.version}`,version_epoch:head.version_epoch,history_head:head.entry_hash,revision_sequence:head.sequence},changes,proposed_successors:[{registry:'machine_schema_parity_evidence',action:'insert',rows:absent.map(s=>({schema_code:s.schema_code,producer_semantic_hash:null,validator_semantic_hash:null,persistence_semantic_hash:null,canonical_semantic_hash:null,validated_against_version:'<successor-version>',status:'PENDING',reason:'No independently executed producer/validator/persistence/canonical parity evidence;fail closed.'}))}]};
  console.log(JSON.stringify(request,null,2));
} else {
  const canonical=rows('project_architecture_directory');
  const normalized=rows('a233_normalized_directory_entry').filter(r=>r.source_table==='project_architecture_directory');
  const artifacts=rows('architecture_diagram_artifact_registry').filter(r=>r.status==='active');
  const head=rows('revision_history').at(-1);
  for (const r of artifacts) {
    if (crypto.createHash('sha256').update(fs.readFileSync(r.artifact_path)).digest('hex')!==r.content_hash)
      throw Error(`INDEPENDENT_FILE_HASH_MISMATCH:${r.diagram_code}`);
    if (r.source_codex_version!==head.version) throw Error(`ARTIFACT_GENERATION_DRIFT:${r.diagram_code}`);
  }
  const sync=rows('architecture_diagram_sync_evidence').find(r=>r.status==='current');
  if (sync.result!=='PASS'||sync.required_count!==artifacts.length||sync.hash_match_count!==artifacts.length||sync.stale_count!==0||sync.missing_count!==0)
    throw Error('CURRENT_SYNC_INVARIANT_FAILED');
  const sorted=r=>JSON.stringify(Object.fromEntries(Object.entries(r).sort(([a],[b])=>a.localeCompare(b))));
  console.log(JSON.stringify({head:rows('revision_history').at(-1),laws:laws.filter(r => active.has(r.provision_id) && /sub-sovereign/i.test(r.rule)).map(r=>({id:r.provision_id,clauses:r.rule.split(';').filter(c=>/sub-sovereign/i.test(c))})),schemaCount:schemas.length,schemaParity:schemas.reduce((a,r)=>(a[r.parity_status]=(a[r.parity_status]||0)+1,a),{}),evidenceCount:evidence.length,evidenceStatuses:evidence.reduce((a,r)=>(a[r.status]=(a[r.status]||0)+1,a),{}),obligation:rows('implementation_obligations').find(r=>r.obligation_code==='OBL_MACHINE_SCHEMA_PARITY'),directory:{canonical:canonical.length,normalized:normalized.length,mismatches:canonical.filter(r=>{const n=normalized.find(n=>n.source_key===r.architecture_code);return !n||sorted(r)!==sorted(JSON.parse(n.domain_payload))}).map(r=>r.architecture_code)},sync:rows('architecture_diagram_sync_evidence').filter(r=>r.status==='current'),staleScan:rows('architecture_stale_reference_evidence').filter(r=>r.status==='current')},null,2));
}
