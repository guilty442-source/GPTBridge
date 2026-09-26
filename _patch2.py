import io, json, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
def rd(p, enc="utf-8"): return io.open(p, encoding=enc, newline=None).read()
def wr(p, s): io.open(p, "w", encoding="utf-8", newline="\n").write(s)
log = []

# rust_vector_runtime selector: fail closed, qdrant retired
p = "main-system/src-core/core_system/rag/rust_vector_runtime.py"
s = rd(p)
old = '''def select_vector_runtime(config: RagPipelineConfig) -> QdrantCanonicalRuntime:
    """Canonical vector-runtime selector (A610 Rust-Vector-Engine=target-primary).

    ``vector_backend`` resolves to ``rust`` by default — the takeover
    state.  ``qdrant`` stays selectable through ``VECTOR_BACKEND`` for
    the bounded migration/verification window until the retire-after-
    cutover event completes.
    """
    backend = str(getattr(config, "vector_backend", "") or "rust").strip().lower()
    if backend == "qdrant":
        return QdrantCanonicalRuntime(config)
    return RustVectorRuntime(config)'''
new = '''def select_vector_runtime(config: RagPipelineConfig) -> QdrantCanonicalRuntime:
    """Canonical vector-runtime selector (A611: Rust engine is the
    primary semantic index; the Qdrant cutover is sealed).

    Only ``rust``/``vectord`` selects a live runtime — Qdrant is retired
    and every other value fails closed.
    """
    backend = str(getattr(config, "vector_backend", "") or "rust").strip().lower()
    if backend in {"rust", "vectord"}:
        return RustVectorRuntime(config)
    raise ValueError(
        f"VECTOR_BACKEND_UNSUPPORTED: {backend!r} — Qdrant is retired; "
        "the canonical semantic index is the Rust vectord engine"
    )'''
assert old in s; s = s.replace(old, new, 1); wr(p, s); log.append("selector")

# rag_qdrant.py config comment + module header wording
p = "main-system/src-core/core_system/rag/rag_qdrant.py"
s = rd(p)
s = s.replace('''    # A610 Rust-Vector-Engine=target-primary: canonical vector runtime is
    # the governed vectord service; "qdrant" remains selectable through
    # VECTOR_BACKEND only for the bounded migration/verification window.''',
'''    # A611 cutover sealed: canonical vector runtime is the governed
    # vectord service; Qdrant is retired (no live selection path).''')
s = s.replace(
    "A371: DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result",
    "A371: DEFAULT-PATH: source content > vectord (Rust engine) dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result")
s = s.replace("A374: Binding order: 1 QDRANT_CANONICAL_RUNTIME > 2 PostgreSQL",
              "A374: Binding order: 1 RUST_VECTOR_CANONICAL_RUNTIME > 2 PostgreSQL")
s = s.replace("A373: CANONICAL-TAKEOVER: normal read/write must prove Qdrant dense retrieval",
              "A373: CANONICAL-TAKEOVER: normal read/write must prove vectord dense retrieval")
wr(p, s); log.append("rag_qdrant")

# pipeline_recovery / runtime status labels
p = "main-system/src-core/core_system/rag/pipeline_recovery.py"
s = rd(p)
s = s.replace('"canonical_vector_database"] = "qdrant"', '"canonical_vector_database"] = "vectord"')
s = s.replace('"engine": "canonical-qdrant-postgresql"', '"engine": "canonical-vectord-postgresql"')
wr(p, s); log.append("pipeline_recovery")

# rag_runtime_integration.py QDRANT_URL env default
p = "main-system/src-core/core_system/rag_runtime_integration.py"
s = rd(p)
print("RRI ctx:", [ln.strip()[:100] for ln in s.splitlines() if "QDRANT_URL" in ln])
