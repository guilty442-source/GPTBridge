from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"E:\GPTBridge")
DB = ROOT / "governance_rule/codex/data/governance_codex.sqlite3"
MIRROR = ROOT / "governance_rule/codex/governance_codex.zh-TW.txt"
LEGACY_VERSION = "1.00000"
REVISION = "H092"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


ARTICLES = [
    (400, "A400", "1", "timestamp-release-identity-successor",
     "SUCCESSOR:A395 is superseded. RELEASE-IDENTITY:numeric evolving version numbers are retired as current ordering and activation authority. Every new Codex amendment,application/tool release,contract/schema/ABI revision,governance-schema revision and runtime generation is identified by a registered canonical release_identity_timestamp governed by A401. The identity is the immutable UTC timestamp at which the authority seals or activates that object;it is not inferred from file mtime,Git time,build time or observation time. UNIQUENESS:when two identities would share one microsecond,the authoritative issuer advances monotonic_sequence and records a composite identity without altering the timestamp. LEGACY:numeric fields including version=1.00000 and historical semantic-version labels remain immutable compatibility aliases only;they cannot order,select,activate,downgrade or prove currency. MIGRATION:registries preserve legacy alias,bind it to release_identity_timestamp,issuer,content hash and lineage,and consumers select by the timestamp identity. SEPARATION:A401 defines timestamp syntax and evidence;this article defines only release-identity semantics. Numeric version parsers may read legacy evidence but cannot create a new numeric successor.",
     "FORBID:new numeric evolving version as authority+timestamp inferred from mtime/Git/build/cache+legacy version selecting active release+rewriting historical version+ambiguous duplicate release timestamp+automatic repair inventing release identity+raw occurrence timestamp silently treated as release identity without sealing authority.",
     "An externally governed dependency may retain its upstream semantic version as descriptive evidence;GPTBridge binds it to an internal release_identity_timestamp and never promotes that external number to local activation authority."),
    (401, "A401", "1", "canonical-timestamp-and-release-identity-envelope-successor",
     "SUCCESSOR:A397 is superseded. TIMESTAMP-FORMAT:every canonical UTC timestamp is YYYY-MM-DDTHH:mm:ss.ffffffZ with Gregorian calendar,24-hour UTC,six fractional digits and uppercase Z. EVIDENCE-FIELDS:recorded_at_utc,occurred_at_utc,observed_at_utc,effective_at_utc,expires_at_utc,source_clock_id,precision,timestamp_status,timestamp_hash,monotonic_sequence,clock_skew_ms,time_zone and utc_offset apply by registered record profile. RELEASE-FIELD:release_identity_timestamp uses the same canonical syntax but is a distinct typed field issued only by the registered sealing/activation authority under A400. It may replace an evolving numeric version identifier only when issuer,content hash,lineage and monotonic sequence are bound. It never replaces event occurrence evidence. STATUS:verified|estimated|corrected|invalid;release_identity_timestamp must be verified and cannot be estimated. LEGACY:original timestamp/version evidence is never overwritten;canonical successors and migration evidence are appended. VALIDATION:permission-sovereign validates identity,scope,clock,hash and admissibility;星澄 independently detects Codex/implementation mismatch.",
     "FORBID:noncanonical timestamp+estimated release identity+mtime/Git/build time as issuer+release timestamp substituted for occurred_at_utc+event timestamp promoted without seal+timestamp overwrite+numeric version recreated from timestamp+missing lineage/content hash+invalid time blocking unrelated scope.",
     "Historical date-only or second-precision evidence may be retained with corrected or estimated canonical companions, but it cannot become a verified release identity without independent sealing evidence."),
    (402, "A402", "1", "dynamic-reference-policy-successor",
     "SUCCESSOR:A394 is superseded. GENERAL:operational values are consumed through typed canonical codes or governed environment bindings. Canonical registry declarations,legal schema definitions,code identities,immutable evidence timestamps and sealed release_identity_timestamp values are authoritative definitions rather than duplicated consumer hardcoding. SEPARATION:A401 exclusively defines timestamp syntax and evidence fields;A400 exclusively defines timestamp-based release identity semantics;this article defines only reference placement. Consumers reference registered identities and never restate paths,endpoints,models,limits,states,release identities or schema values. CHECKING:each finding names the literal,semantic class,expected authority and exact file or Codex field. Historical literals remain evidence and are not copied into new consumers.",
     "FORBID:duplicated operational literal+checker treating canonical definition as consumer hardcoding+invented literal whitelist+timestamp syntax restated outside A401+release identity semantics restated outside A400+legacy numeric alias used as current authority.",
     "Registered synthetic fixture data is allowed only inside its fixture scope. Third-party and generated artifacts remain non-authoritative evidence."),
    (403, "A403", "1", "timestamp-release-identity-cross-codex-interpretation-successor",
     "SUCCESSOR:A399 is superseded. INTERPRETATION:A401 controls timestamp representation;A400 controls release identity;A402 controls dynamic references. Earlier provisions mentioning version,release,generation,time or expiry retain their domain duties but establish no competing representation or authority. Every new reference to current version means release_identity_timestamp unless it explicitly names a preserved legacy_version_alias. PRECEDENCE:A401 controls syntax and clock evidence;A400 controls identity issuance and activation. CHECKERS:timestamp-format,release-identity,lineage and legacy-alias misuse are separate findings with exact fields and evidence.",
     "FORBID:competing timestamp format+new numeric version authority+legacy wording overriding current successors+one vague finding hiding distinct violations+timestamp correction changing sealed release identity+release activation rewriting event evidence.",
     "Historical provisions and numeric values remain immutable evidence;their current operational effect is narrowed by this successor without rewriting history."),
    (404, "A404", "23", "automatic-command-contract-timestamp-revision-successor",
     "SUCCESSOR:A398 is superseded. COMMANDS:the eight registered automatic repair/update switch,confirmation,revocation and approved-execution commands remain the sole command set. CONTRACT-IDENTITY:legacy request/result identifiers ending in -v1 are preserved aliases only. Each alias resolves through command_contract_revision_registry to a canonical contract_id,release_identity_timestamp,request schema,response schema,owner,content hash,status and predecessor. New contract revisions use release_identity_timestamp and never create -v2 or another numeric suffix. ROLE-SEPARATION:user decides switch/confirmation/revocation;星澄 presents and records;permission-sovereign validates authority and evidence;synchronization-sovereign decides synchronization and dispatch;module execution alone mutates. EXECUTION requires enabled matching switch,current unrevoked single-use confirmation,permission proof,synchronization decision,expected before hash and generation. All results carry canonical timestamps and contract release identity separately.",
     "FORBID:new numeric command-contract revision+unregistered alias+implicit consent+confirmation reuse+missing revocation+permission execution+synchronization module work+executor scope expansion+contract timestamp confused with action occurrence time.",
     "Existing -v1 aliases remain readable during migration but cannot identify a newly issued contract revision or select a newer contract by numeric comparison."),
]


def add_column(c: sqlite3.Connection, table: str, declaration: str) -> None:
    name = declaration.split()[0]
    if name not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def main() -> None:
    c = sqlite3.connect(DB)
    try:
        c.execute("BEGIN IMMEDIATE")
        predecessors = {"A400": "A395", "A401": "A397", "A402": "A394", "A403": "A399", "A404": "A398"}
        for article in ARTICLES:
            c.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?)", article)
            pid = article[1]
            h = digest(article)
            c.execute("INSERT INTO provision_identities VALUES (?,?,?,?,?)", (f"ARTICLE_{pid}", "article", pid, "governance-codex://official", "permanent"))
            c.execute("INSERT INTO provision_lineage VALUES (?,?,?,?,?,?)", ("article", pid, REVISION, predecessors[pid], "active", h))
            c.execute("INSERT INTO provision_revisions VALUES (?,?,?,?,?,?,?)", ("article", pid, REVISION, LEGACY_VERSION, h, "external-signatures-required", 2))
            c.execute("INSERT INTO provision_lifecycle_status VALUES (?,?,?,?,?,?)", ("article", pid, "active", LEGACY_VERSION, None, f"{REVISION}+explicit-user-command+release_identity_timestamp={NOW}"))
            c.execute("INSERT INTO effective_provisions VALUES (?,?,?,?)", ("article", pid, LEGACY_VERSION, "active"))
        for old, new in (("A395","A400"),("A397","A401"),("A394","A402"),("A399","A403"),("A398","A404")):
            c.execute("DELETE FROM effective_provisions WHERE provision_type='article' AND provision_id=?", (old,))
            c.execute("UPDATE provision_lifecycle_status SET lifecycle_state='superseded',successor_identity=?,evidence=? WHERE provision_type='article' AND provision_id=?", (f"ARTICLE_{new}", f"{REVISION}+timestamp-identity-successor", old))
            c.execute("UPDATE provision_lineage SET status='superseded' WHERE provision_type='article' AND provision_id=?", (old,))
        classifications = [
            ("article","A400","main-codex","CODEX_MAIN","A79|A87|A100|A181|A182|A274|A401"),
            ("article","A401","main-codex","CODEX_MAIN","A46|A79|A86|A200|A243|A274|A390|A400"),
            ("article","A402","main-codex","CODEX_MAIN","A274|A386|A387|A388|A400|A401"),
            ("article","A403","main-codex","CODEX_MAIN","A182|A366|A380|A400|A401|A402"),
            ("article","A404","special-law","CODEX_AUTOMATIC_REPAIR_SPECIAL_LAW","A247|A258|A261|A314|A366|A380|A400|A401|A402"),
        ]
        c.executemany("INSERT INTO provision_law_classification VALUES (?,?,?,?,?)", classifications)
        c.executemany("INSERT INTO provision_special_law_migration VALUES (?,?,?,?,?,?,?,?)", [("article",a[1],a[3],"retain",None,"normalized","星澄",LEGACY_VERSION) for a in classifications])

        c.execute("CREATE TABLE IF NOT EXISTS command_contract_revision_registry(contract_id TEXT PRIMARY KEY,legacy_alias TEXT NOT NULL,release_identity_timestamp TEXT NOT NULL,request_schema TEXT NOT NULL,response_schema TEXT NOT NULL,owner TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT NOT NULL,predecessor_contract_id TEXT)")
        for code, req, res in c.execute("SELECT command_code,input_contract,output_contract FROM command_code_directory WHERE command_code IN ('XINGCHENG_SET_REPAIR_RELEASE','XINGCHENG_SET_UPDATE_RELEASE','XINGCHENG_CONFIRM_AUTOMATIC_REPAIR','XINGCHENG_CONFIRM_AUTOMATIC_UPDATE','XINGCHENG_REVOKE_AUTOMATIC_REPAIR_CONFIRMATION','XINGCHENG_REVOKE_AUTOMATIC_UPDATE_CONFIRMATION','SYNC_EXECUTE_APPROVED_AUTOMATIC_REPAIR','SYNC_EXECUTE_APPROVED_AUTOMATIC_UPDATE')"):
            contract_id = code.lower().replace("_", "-") + "-contract@" + NOW
            request_schema = "command_code|request_id|user_actor_id|correlation_id|recorded_at_utc|typed_action_fields"
            response_schema = "command_code|request_id|decision|status|reason_code|correlation_id|recorded_at_utc|evidence_hash"
            c.execute("INSERT INTO command_contract_revision_registry VALUES (?,?,?,?,?,?,?,?,?)", (contract_id, f"{req}|{res}", NOW, request_schema, response_schema, "permission-sovereign", digest((code,request_schema,response_schema,NOW)), "active", None))

        add_column(c, "revision_history", "release_identity_timestamp TEXT")
        add_column(c, "seal_manifest", "release_identity_timestamp TEXT")
        add_column(c, "provision_revisions", "release_identity_timestamp TEXT")
        c.execute("UPDATE revision_history SET release_identity_timestamp=recorded_at_utc WHERE release_identity_timestamp IS NULL")
        c.execute("UPDATE provision_revisions SET release_identity_timestamp=? WHERE revision_id=?", (NOW, REVISION))
        c.execute("UPDATE seal_manifest SET release_identity_timestamp=? WHERE version=? AND version_epoch=2", (NOW, LEGACY_VERSION))
        c.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('current_release_identity_timestamp',?)", (NOW,))
        c.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('legacy_numeric_version_policy','compatibility-alias-only')")

        previous = c.execute("SELECT entry_hash FROM revision_history ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        core = (92,REVISION,LEGACY_VERSION,"2026-09-14","timestamp-release-identity-migration","Replaced evolving numeric version authority with canonical timestamp release identities, retained 1.00000 as a legacy compatibility alias, separated timestamp syntax from release semantics, and timestamp-versioned automatic command contracts.",previous)
        entry = digest(core)
        c.execute("INSERT INTO revision_history(sequence,change_id,version,recorded_date,change_scope,summary,previous_hash,entry_hash,version_epoch,recorded_at_utc,timestamp_status,timestamp_migration_evidence,release_identity_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*core,entry,2,NOW,"verified","H092:authoritative-amendment-clock",NOW))
        count = c.execute("SELECT COUNT(*) FROM provision_identities").fetchone()[0]
        cr = digest(c.execute("SELECT * FROM provision_lineage ORDER BY provision_type,provision_id").fetchall())
        ir = digest(c.execute("SELECT * FROM provision_identities ORDER BY identity_code").fetchall())
        fr = digest((NOW,REVISION,cr,ir,entry))
        c.execute("UPDATE seal_manifest SET history_head=?,provision_count=?,identity_count=?,lineage_count=?,content_root=?,identity_root=?,full_root=?,release_identity_timestamp=? WHERE version=? AND version_epoch=2", (REVISION,count,count,count,cr,ir,fr,NOW,LEGACY_VERSION))
        c.commit()

        doc=json.loads(MIRROR.read_text(encoding="utf-8"))
        for table in list(doc["tables"]):
            cols=[r[1] for r in c.execute(f'PRAGMA table_info("{table}")')]
            if cols:
                doc["tables"][table]=[dict(zip(cols,row)) for row in c.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        # Include the new canonical registry in the synchronized Chinese artifact.
        table="command_contract_revision_registry"
        cols=[r[1] for r in c.execute(f'PRAGMA table_info("{table}")')]
        doc["tables"][table]=[dict(zip(cols,row)) for row in c.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        doc["codex_version"]=LEGACY_VERSION
        doc["release_identity_timestamp"]=NOW
        MIRROR.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    finally:
        c.close()


if __name__ == "__main__":
    main()
