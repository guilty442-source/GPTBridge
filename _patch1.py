import io, json, re, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
def rd(p, enc="utf-8"): return io.open(p, encoding=enc, newline=None).read()
def wr(p, s): io.open(p, "w", encoding="utf-8", newline="\n").write(s)
log = []

# 1. startup_manifest.json
p = "main-system/config/startup_manifest.json"
d = json.loads(rd(p, "utf-8-sig"))
d["dependency_manifest"] = [e for e in d["dependency_manifest"] if e.get("identity") != "qdrant"]
d["probe_constants"]["vectord_probe_timeout"] = d["probe_constants"].pop("qdrant_probe_timeout")
d["ports"].pop("qdrant", None)
d["governed_startup"]["no_fixed_criticality_services"] = [
    s for s in d["governed_startup"]["no_fixed_criticality_services"] if s != "qdrant"]
wr(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n"); log.append("manifest")

# 2. startup_config.py fallback
p = "main-system/src-core/startup_core/startup_config.py"
s = rd(p)
old = '''        {
            "identity": "qdrant",
            "owner": "rag-runtime-sovereign",
            "required_by": "rag-semantic-retrieval",
            "criticality": "capability-critical",
            "readiness_contract": "loopback-tcp-6333",
            "deadline": "3s",
            "retry_budget": 1,
            "shutdown_order": 20,
        },'''
new = '''        {
            "identity": "vectord",
            "owner": "rag-runtime-sovereign",
            "required_by": "rag-semantic-retrieval",
            "criticality": "capability-critical",
            "readiness_contract": "loopback-tcp-8092",
            "deadline": "3s",
            "retry_budget": 1,
            "shutdown_order": 20,
        },'''
assert old in s; s = s.replace(old, new, 1)
s = s.replace('"qdrant_probe_timeout": 0.5,', '"vectord_probe_timeout": 0.5,')
s = s.replace('"qdrant": 6333,', '"vectord": 8092,')
wr(p, s); log.append("startup_config")

# 3. phases_constants.py
p = "main-system/src-core/startup_core/phases_constants.py"
s = rd(p)
s = s.replace('QDRANT_PROBE_TIMEOUT: Final[float] = _cfg_probe("qdrant_probe_timeout")',
              'VECTORD_PROBE_TIMEOUT: Final[float] = _cfg_probe("vectord_probe_timeout")')
s = s.replace('QDRANT_PORT: Final[int] = _cfg_port("qdrant")\n', '')
s = s.replace('"QDRANT_PROBE_TIMEOUT",', '"VECTORD_PROBE_TIMEOUT",')
s = s.replace('    "QDRANT_PORT",\n', '')
assert "QDRANT" not in s
wr(p, s); log.append("phases_constants")

# 4. phases.py
p = "main-system/src-core/startup_core/phases.py"
s = rd(p)
s = s.replace("    QDRANT_PORT,\n    QDRANT_PROBE_TIMEOUT,", "    VECTORD_PROBE_TIMEOUT,")
s = s.replace("timeout=QDRANT_PROBE_TIMEOUT", "timeout=VECTORD_PROBE_TIMEOUT")
s = s.replace('"phase": "qdrant-start",\n            "label": "啟動 vectord（Rust 語意索引）",',
              '"phase": "vectord-start",\n            "label": "啟動 vectord（Rust 語意索引）",')
s = s.replace('''        vectord engine (target primary; Qdrant retires after cutover)."""''',
              '''        vectord engine (Qdrant retired; sealed cutover)."""''')
# remove _phase_qdrant entirely (from def through the blank line before _phase_governance_audit)
i = s.find("    def _phase_qdrant(self)")
j = s.find("    def _phase_governance_audit(self)")
assert i > 0 and j > i
s = s[:i] + s[j:]
s = s.replace('"qdrant-start": PhaseMixin._phase_qdrant,', '"vectord-start": PhaseMixin._phase_vectord,')
assert "QDRANT" not in s and "_phase_qdrant" not in s
wr(p, s); log.append("phases")

# 5. phases_execution.py
p = "main-system/src-core/startup_core/phases_execution.py"
s = rd(p)
s = s.replace('''            # A610: both semantic-index identities share the backend-aware
            # `qdrant-start` handler, which dispatches on VECTOR_BACKEND.
            "qdrant": "qdrant-start",
            "vectord": "qdrant-start",
            "ollama": "ollama-start",
        }
        # Exactly one semantic-index backend owns the live path: the
        # standby engine is never probed or spawned by startup.
        semantic_backend = os.environ.get(
            "VECTOR_BACKEND", "rust"
        ).strip().lower()
        standby_identity = "qdrant" if semantic_backend != "qdrant" else "vectord"
''', '''            "vectord": "vectord-start",
            "ollama": "ollama-start",
        }
''')
s = s.replace('''                for dep in ordered_deps:
                    if dep.identity == standby_identity:
                        dependency_results[dep.identity] = {
                            "phase": f"{dep.identity}-standby",
                            "ready": False,
                            "state": "standby",
                            "criticality": dep.criticality,
                            "required_by": dep.required_by,
                            "fault_code": "SEMANTIC_BACKEND_STANDBY",
                            "message": "standby vector backend — not probed "
                            "while VECTOR_BACKEND selects the other engine",
                            "duration_ms": 0,
                        }
                        continue
''', '''                for dep in ordered_deps:
''')
s = s.replace('qdrant_ok = next((r["ready"] for r in results if r["phase"] == "qdrant-start"), False)',
              'vectord_ok = next((r["ready"] for r in results if r["phase"] == "vectord-start"), False)')
s = s.replace('"qdrant": next((r for r in results if r["phase"] == "qdrant-start"), {}),',
              '"vectord": next((r for r in results if r["phase"] == "vectord-start"), {}),')
wr(p, s); log.append("phases_execution")

# check remaining qdrant_ok references
rem = [ln for ln in s.splitlines() if "qdrant" in ln.lower()]
for ln in rem: print("  phases_execution residual:", ln.strip()[:100])

# 6. readiness_gate.py
p = "main-system/src-core/tasks/readiness_gate.py"
s = rd(p)
old = '''        # A610 takeover: the Rust vectord engine owns the semantic index;
        # qdrant counts only inside the explicit migration window.  qdrant
        # is declared "optional" in the manifest so it is never probed into
        # `deps` — probe it on demand when (and only when) that window is
        # deliberately open.
        semantic_backend = os.environ.get("VECTOR_BACKEND", "rust").strip().lower()
        if semantic_backend == "qdrant":
            try:
                semantic_ready = probe_registered_local_service(
                    "qdrant", timeout=DEPENDENCY_PROBE_TIMEOUT
                ).reachable
            except Exception:
                semantic_ready = False
        else:
            semantic_ready = bool(reachable.get("vectord"))
'''
new = '''        # A611 cutover sealed: the Rust vectord engine owns the semantic
        # index; Qdrant is retired and never probed by readiness.
        semantic_ready = bool(reachable.get("vectord"))
'''
assert old in s; s = s.replace(old, new, 1)
wr(p, s); log.append("readiness_gate")
print("done:", log)
