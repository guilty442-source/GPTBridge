import json, io, re, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
rules = json.load(open("_qrules.json", encoding="utf-8"))

# per-article targeted replacements (article -> list[(old,new)])
SPEC = {
 "A8":  [("QDRANT:canonical-semantic-index","RUST-VECTOR-ENGINE:canonical-semantic-index")],
 "A44": [("postgresql,qdrant,git,rag","postgresql,rust-vector-engine,git,rag"),
         ("local-python-typescript-","local-python-javascript-")],
 "A49": [("postgresql,qdrant,git,rag,python,typescript","postgresql,rust-vector-engine,git,rag,python,javascript")],
 "A52": [("SHARED-INDEX:qdrant(local-owned)","SHARED-INDEX:rust-vector-engine(local-owned)")],
 "A65": [("postgresql+qdrant+status","postgresql+rust-vector-engine+status")],
 "A177":[("+Qdrant+RAG","+Rust-Vector-Engine+RAG")],
 "A189":[("/QDRANT:","/RUST-VECTOR-ENGINE:")],
 "A191":[("POSTGRESQL/QDRANT/OLLAMA:","POSTGRESQL/RUST-VECTOR-ENGINE/OLLAMA:")],
 "A200":[("+Qdrant-metadata+","+Rust-Vector-Engine-metadata+")],
 "A238":[("Qdrant/RAG semantic candidates","Rust-Vector-Engine/RAG semantic candidates")],
 "A284":[("+PostgreSQL+Qdrant+state+events+IPC","+PostgreSQL+Rust-Vector-Engine+state+events+IPC")],
 "A290":[("PostgreSQL+Qdrant+external service storage","PostgreSQL+Rust-Vector-Engine+external service storage")],
 "A295":[("PostgreSQL/Qdrant evidence","PostgreSQL/Rust-Vector-Engine evidence")],
 "A304":[("Git+SQL+Qdrant/RAG+LLM","Git+SQL+Rust-Vector-Engine/RAG+LLM")],
 "A340":[("PostgreSQL/Qdrant contract versions","PostgreSQL/Rust-Vector-Engine contract versions")],
 "A365":[("Qdrant is rebuildable semantic projection","Rust-Vector-Engine is rebuildable semantic projection")],
 "A367":[("PostgreSQL,SQLite,Qdrant)","PostgreSQL,SQLite,Rust-Vector-Engine)")],
 "A368":[("QDRANT-FRESHNESS:","VECTOR-FRESHNESS:")],
 "A369":[("Qdrant canonical semantic projection","Rust-Vector-Engine canonical semantic projection")],
 "A370":[("Qdrant canonical rebuildable semantic projection","Rust-Vector-Engine canonical rebuildable semantic projection")],
 "A371":[("when Qdrant and PostgreSQL pass","when Rust-Vector-Engine and PostgreSQL pass"),
         ("Qdrant dense retrieval","Rust-Vector-Engine dense retrieval")],
 "A372":[("QDRANT_CANONICAL_RUNTIME","RUST_VECTOR_CANONICAL_RUNTIME"),
         ("healthy Qdrant the proven","healthy Rust-Vector-Engine the proven"),
         ("Qdrant upsert","Rust-Vector-Engine upsert")],
 "A373":[("Qdrant dense retrieval","Rust-Vector-Engine dense retrieval")],
 "A374":[("healthy Qdrant and PostgreSQL","healthy Rust-Vector-Engine and PostgreSQL"),
         ("Verified Qdrant/PostgreSQL fault","Verified Rust-Vector-Engine/PostgreSQL fault")],
 "A377":[("Qdrant canonical rebuildable semantic projection","Rust-Vector-Engine canonical rebuildable semantic projection")],
 "A387":[("STORE_QDRANT_SEMANTIC","STORE_VECTOR_SEMANTIC")],
 "A420":[("RAG,Qdrant,network","RAG,Rust-Vector-Engine,network")],
 "A425":[("PostgreSQL/SQLite/Qdrant ownership","PostgreSQL/SQLite/Rust-Vector-Engine ownership")],
 "A524":[("Qdrant 語意候選","Rust 向量引擎語意候選")],
 "A549":[("PostgreSQL-Qdrant reconciliation","PostgreSQL-Rust-Vector-Engine reconciliation"),
         ("Qdrant is a scoped derived semantic index","Rust-Vector-Engine is a scoped derived semantic index")],
 "A576":[("Qdrant indexes","Rust-Vector-Engine indexes")],
 "A581":[("a degraded Qdrant","a degraded Rust-Vector-Engine")],
 "A610":[("Qdrant=retired-after-rust-vector-engine-cutover","Qdrant=retired")],
 "A611":[("the target primary semantic index and replaces Qdrant","the primary semantic index; Qdrant is retired")],
 "A621":[("Qdrant->Rust vector engine","Qdrant->Rust vector engine(cutover-sealed-2026-09-26)")],
}
# articles where the word qdrant may remain (retirement bookkeeping)
ALLOW_REMAIN = {"A610","A611","A621"}

changes, problems = [], []
for pid, rule in rules.items():
    new = rule
    for old, rep in SPEC.get(pid, []):
        if old not in new:
            problems.append(f"{pid}: pattern not found: {old!r}")
        new = new.replace(old, rep)
    if pid not in ALLOW_REMAIN:
        # generic sweep for leftover tokens
        new = re.sub(r"QDRANT", "RUST-VECTOR-ENGINE", new)
        new = re.sub(r"Qdrant", "Rust-Vector-Engine", new)
        new = re.sub(r"qdrant", "rust-vector-engine", new)
    if pid not in ALLOW_REMAIN and "qdrant" in new.lower():
        problems.append(f"{pid}: residual qdrant token")
    if new != rule:
        changes.append({"table":"articles","key":{"provision_id":pid},"field":"rule","proposed":new})

print(f"articles touched: {len(changes)}")
for p in problems: print("PROBLEM:", p)
if problems: sys.exit(1)
json.dump(changes, open("_cutover_changes.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
print("changes written")
