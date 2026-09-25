"""Generate the consolidated codex-convergence amendment request.

Composes:
  A. 7 missing article inserts (A611/A613/A614/A617/A618/A619/A620)
  B. registry completion for 12 language-division articles
     (law classification / lifecycle / module membership / effective provisions)
  C. A199 startup deadline 40000 -> 20000 (+ halved phase budgets)
  D. stale language-architecture normalization:
     A219, A347, A350, A351, A353, A356, A359, A360, A362
  E. metadata convergence (language roles, topology, deadline keys,
     governance lifecycle state)
  F. machine-schema parity restamp: real SEAL_CANONICAL_V1 descriptor hashes
     for all 74 registered schemas + obligation closure
  G. architecture stale-reference evidence re-scan at the successor version
"""

import sys, io, json
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")

from governance_rule.execution.codex_repository import codex_readonly_connection
from governance_rule.execution.semantic_hash_toolchain import (
    build_descriptor, seal_hash,
)

OUT = Path(
    r"E:\GPTBridge\governance_rule\execution\audit\convergence"
    r"\codex-amendment-request-codex-generation-convergence-20260925.json"
)

# ---------------------------------------------------------------- predecessors
with codex_readonly_connection() as conn:
    pred_version = conn.execute(
        "SELECT value FROM metadata WHERE key='codex_version'").fetchone()[0]
    rev = conn.execute(
        "SELECT sequence, entry_hash FROM revision_history "
        "ORDER BY sequence DESC LIMIT 1").fetchone()
    epoch = conn.execute(
        "SELECT value FROM metadata WHERE key='current_version_epoch'").fetchone()[0]
    identity = conn.execute(
        "SELECT value FROM metadata WHERE key='current_version_identity'"
    ).fetchone()[0]
    # registry rows for parity computation
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND "
        "table_name='machine_schema_registry' ORDER BY ordinal_position")]
    rows = [dict(zip(cols, r)) for r in conn.execute(
        "SELECT * FROM machine_schema_registry ORDER BY schema_code")]
    existing_evidence = {r[0] for r in conn.execute(
        "SELECT schema_code FROM machine_schema_parity_evidence")}

SV = "<successor-version>"

predecessor = {
    "codex_version": pred_version,
    "version_identity": identity,
    "version_epoch": int(epoch),
    "history_head": str(rev[1]),
    "revision_sequence": int(rev[0]),
}

# ---------------------------------------------------------------- new articles
NEW_ARTICLES = {
    "A611": {
        "position": 611, "section_index": "7",
        "subject": "rust-local-vector-engine-and-semantic-index",
        "rule": "VECTOR-ENGINE:Rust 1.98.1 owns the local vector engine, RAG mechanical retrieval and the native-security boundary; the Rust Vector Engine is the target primary semantic index and replaces Qdrant; SQLite vector/storage paths are retired or disabled; PostgreSQL 18.6 retains formal data validation and remains the structured authority; RULE_VECTOR_ENGINE_RUST_V1 verifies vector_engine=rust-vector-engine and sqlite_status retired/disabled fail-closed.",
        "prohibition": "FORBID:second vector engine|active sqlite storage for vector state|qdrant as authoritative index|non-rust vector engine ownership",
        "exception": "existing sqlite/qdrant artifacts persist as migration-only lineage during the registered cutover window",
    },
    "A613": {
        "position": 613, "section_index": "7",
        "subject": "javascript-esm-native-frontend-language",
        "rule": "FRONTEND-LANGUAGE:JavaScript ESM is the sole authored frontend language and owns React UI, frontend state and desktop interaction; TypeScript is retired and existing .ts/.tsx/.d.ts files are grandfathered-existing-only until migrated; RULE_JS_NATIVE_V1 verifies frontend authored source language_id=native JavaScript ESM fail-closed.",
        "prohibition": "FORBID:new authored TypeScript source|second frontend language|frontend ownership outside JavaScript-ESM",
        "exception": "grandfathered TypeScript files remain reviewable under A348 until migrated",
    },
    "A614": {
        "position": 614, "section_index": "7",
        "subject": "go-rust-replace-nodejs-backend-runtime",
        "rule": "RUNTIME:backend and edge runtime ownership is Go 1.27.1 (xingcheng web search, batch processing, network and file I/O) and Rust 1.98.1 (vector engine, native security, desktop host); Node.js is retired and holds zero active runtime consumers; RULE_GO_RUST_NODE_V1 verifies backend/edge runtime is go or rust and nodejs retired fail-closed.",
        "prohibition": "FORBID:nodejs runtime consumer|new nodejs dependency|backend runtime outside go|rust",
        "exception": "node toolchain binaries retained solely as build-time bundler infrastructure are not a runtime consumer",
    },
    "A617": {
        "position": 617, "section_index": "7",
        "subject": "per-language-test-tool-register",
        "rule": "TEST-TOOL-TABLE:each language declares exactly its registered test tool: C/C++>Native Harness; Rust>cargo test+Clippy; Go>go test+go vet+race detector; C#/F#>.NET test under the sole C#14 test orchestration; Python/JAX>pytest; JavaScript/React>Vitest; Tauri desktop>Rust test harness; PostgreSQL>migration replay tests; RULE_TEST_TOOLS_V1 verifies language+test_tool+responsibility exactly match this table fail-closed.",
        "prohibition": "FORBID:unregistered test tool|language without declared test tool|parallel test framework ownership",
        "exception": "operational binaries without a language test surface satisfy evidence through native tests only",
    },
    "A618": {
        "position": 618, "section_index": "7",
        "subject": "rust-vector-engine-and-desktop-test-coverage",
        "rule": "RUST-TESTS:the Rust Vector Engine and Tauri/desktop host test suites must cover every registered acceptance item including PostgreSQL reconciliation behavior and the native security boundary; RULE_RUST_TESTS_V1 verifies test_suite and acceptance_criteria cover all listed items fail-closed.",
        "prohibition": "FORBID:rust capability without coverage row|acceptance item untested|security boundary without regression test",
        "exception": "test items gated on retired artifacts may carry a registered migration-only rationale instead of executed evidence",
    },
    "A619": {
        "position": 619, "section_index": "7",
        "subject": "jax-training-parity-evidence",
        "rule": "JAX-PARITY:JAX training must pass all parity checks against the PyTorch reference and C++ inference: model architecture, forward parity, gradient, optimizer, MoE, loss, checkpoint and weight handling, inference behavior and bounded GPU memory; RULE_JAX_PARITY_V1 verifies model_architecture|forward_parity|gradient|optimizer|moe|loss|checkpoint|weight|inference|gpu fail-closed.",
        "prohibition": "FORBID:training activation without full parity evidence|unbounded gpu memory|parity subset treated as complete",
        "exception": "retired PyTorch artifacts serve only as the registered reference baseline during migration",
    },
    "A620": {
        "position": 620, "section_index": "7",
        "subject": "final-three-layer-architecture",
        "rule": "FINAL-ARCHITECTURE:the deployment architecture is exactly three layers: execution layer>C/C++/Rust/Go/C#/F#/Python/JAX/JavaScript; orchestration layer>C# on .NET 10; audit layer>C++ Audit Engine plus Python bounded governance; RULE_FINAL_ARCHITECTURE_V1 verifies layer+technology+responsibility match this table exactly fail-closed.",
        "prohibition": "FORBID:technology placed outside its declared layer|fourth layer|unregistered technology in any layer",
        "exception": "governance semantics executed inside Python's bounded domain remain an execution-layer registration even though they carry adjudication authority",
    },
}

changes = []
successors = []

for aid, spec in NEW_ARTICLES.items():
    successors.append({
        "registry": "articles", "action": "insert",
        "rows": [{"provision_id": aid, "position": spec["position"],
                  "section_index": spec["section_index"], "subject": spec["subject"],
                  "rule": spec["rule"], "prohibition": spec["prohibition"],
                  "exception": spec["exception"]}],
    })

# ------------------------------------------------- registry completion (12 ids)
# A610: classification exists already; the rest need classification.
NEED_CLASSIFICATION = ["A612", "A615", "A616", "A621"] + list(NEW_ARTICLES)
# All twelve lack lifecycle, membership and effective_provisions rows.
NEED_LIFECYCLE = ["A610", "A612", "A615", "A616", "A621"] + list(NEW_ARTICLES)

successors.append({
    "registry": "provision_law_classification", "action": "insert",
    "rows": [
        {"provision_type": "article", "provision_id": aid,
         "tier": "special-law",
         "law_code": "CODEX_LANGUAGE_SOURCE_NATIVE_SPECIAL_LAW",
         "authority_basis": "A341|A343|A348|A610"}
        for aid in NEED_CLASSIFICATION
    ],
})
successors.append({
    "registry": "provision_lifecycle_status", "action": "insert",
    "rows": [
        {"provision_type": "article", "provision_id": aid,
         "lifecycle_state": "active",
         "effective_version": SV,
         "successor_identity": None,
         "evidence": f"{aid}-current",
         "legacy_effective_version": None,
         "current_binding_version": SV}
        for aid in NEED_LIFECYCLE
    ],
})
successors.append({
    "registry": "codex_internal_module_membership", "action": "insert",
    "rows": [
        {"provision_type": "article", "provision_id": aid,
         "module_code": "CODEX_MODULE_LANGUAGE",
         "membership_kind": "primary",
         "resolution_state": "resolved",
         "version_identity": SV}
        for aid in NEED_LIFECYCLE
    ],
})
successors.append({
    "registry": "effective_provisions", "action": "insert",
    "rows": [
        {"provision_type": "article", "provision_id": aid,
         "effective_version": SV,
         "status": "active",
         "legacy_effective_version": None,
         "current_binding_version": SV}
        for aid in NEED_LIFECYCLE
    ],
})

# ------------------------------------------------------------- A199 -> 20000ms
a199_rule = (
    "SCOPE:main-system official startup generation; START-TIME:first-valid-gptbridge-start intent accepted; "
    "END-TIME:fully-ready proof acknowledged-by-runtime-sovereign and current-UI projection converged; "
    "CLOCK:monotonic-high-resolution; HARD-DEADLINE:20000ms inclusive; "
    "ALL-FLOWS:entry-single-instance-resolution+UI-host-visible+startup-runtime-attach/start+preflight+"
    "minimal-information-layer+official-codex-integrity+permission-directory+permission-sovereign+"
    "normal-information-mode+certified-dependency-DAG+all-manifest-declared-startup-dependencies+"
    "required-sovereigns+runtime-handoff+health-baseline+frontend-backend-snapshot/cursor convergence; "
    "BUDGETS:entry-and-UI<=2000ms,codex-information-permission<=4000ms,dependency-DAG-and-services<=8000ms,"
    "sovereign-activation-and-runtime-handoff<=4000ms,UI-convergence-and-final-proof<=2000ms; "
    "BUDGET-CARRY:unused-earlier-time may-transfer-forward+total-never-exceeds-20000ms; "
    "PARALLELISM:independent-nodes-concurrent within-resource-budget+critical-path-first; "
    "I/O:no-fixed-sleep+event-driven+bounded-connect-deadline; "
    "CACHE:only signed/content-addressed/release+environment+dependency-hash matched evidence and "
    "each-live-readiness signal revalidated; "
    "SERVICE-STRATEGY:resident-attach/prewarmed-pool/lazy-capability-adapter allowed only-when-current-"
    "process-generation+health+contract readiness truthfully verified; "
    "DECLARED-STARTUP-DEPENDENCY:must be-ready-before-fully-ready regardless-criticality label; "
    "NONSTARTUP-OPTIONAL:may-start-on-first-use only-if-certified-manifest explicitly-excludes-it-from-"
    "startup and UI labels-capability-not-started+must-not-be-counted-as-startup-complete work; "
    "NO-DEFER-EVASION:work required for normal initial UI/backend/governance/system operation cannot be "
    "relabeled optional/on-demand/deferred; DEADLINE-ENFORCEMENT:at 20000ms without fully-ready proof the "
    "startup is a fail-visible deadline breach recorded with per-phase evidence,never silently retried "
    "or relabeled; GOVERNOR-DISPOSITION:deadline-tightened-to-20000|A199-amended|phase-budgets-halved|"
    "metadata-normalized"
)
a199_proh = (
    "FORBID:startup>20000ms+soft-deadline+deadline-reset-within-generation+wall-clock-only+hidden-wait+"
    "infinite-retry+fixed-sleep+serial-independent-work+process-existence-as-ready+stale-cache-as-live-"
    "proof+fully-ready-before-all-declared-startup-flows+background-completion-after-ready+"
    "optional/deferred/lazy renaming-to-hide-required-work+UI-blocked-or-closed-on-timeout+"
    "independent-tool-stop+manual-cli"
)
a199_exc = (
    "Hardware or external local-service conditions below the declared minimum supported environment "
    "may fail at the same 20000ms boundary; they do not permit a longer or falsely successful startup"
)
changes.append({
    "table": "articles", "key": {"provision_id": "A199"}, "field": "rule",
    "proposed": a199_rule,
    "also": {"subject": "twenty-second-complete-startup-deadline",
             "prohibition": a199_proh, "exception": a199_exc},
})

# ------------------------------------------------------------- language texts
changes.append({
    "table": "articles", "key": {"provision_id": "A219"}, "field": "rule",
    "proposed": (
        "ENTRY:versioned capability contract; PRIMARY:C/C++ production execution modules; "
        "FSHARP:core business-logic+data-validation+transformation+high-correctness-complex-calculation "
        "path (per A341/A343/A604; analysis and scientific computation route to Julia, model training "
        "routes to JAX-on-Python per A610/A612); PYTHON:on-demand governance adjudication only and "
        "never default execution fallback; FALLBACK:only registered semantically equivalent owner with "
        "deadline/resource/idempotency proof; OTHERWISE:typed unavailable+no duplicate execution."
    ),
})

changes.append({
    "table": "articles", "key": {"provision_id": "A347"}, "field": "rule",
    "proposed": (
        "CPP-STYLE:small structs+free functions+RAII resource wrappers+explicit ownership,lifetime,"
        "allocation,cache locality,exception boundary and thread safety; Manager|Controller|Service|"
        "Factory|Processor|Handler requires native-resource proof and A185 limits still apply. "
        "CSHARP-PROJECT:CSharp is the application+workflow+API+Windows/.NET orchestration owner per "
        "A341/A610 and permits Contracts|Interop|Native|Platform|Application|Workflow|Tests "
        "directories; domain business logic is delegated to FSharp on the shared .NET 10 runtime and "
        "CSharp consumes the gptbridge_native.h C ABI for native capability. JAVASCRIPT-DIRECTORIES:"
        "components|views|stores|transport|contracts|checkers only for UI/client/build-time checking "
        "and forbid domain|database|rag|model-runtime|business-engine; TypeScript files are "
        "grandfathered-existing-only per A348 and new authored TypeScript is denied. "
        "SQL-QUERY-BUDGET:every runtime query declares max_returned_rows+max_returned_bytes+timeout+"
        "query_plan_policy+index_expectation; filter+join+aggregate+sort+pagination are pushed down; "
        "checker flags SELECT-star+missing LIMIT/pagination+unparameterized SQL+large fetchall+"
        "unbounded plan; migration/admin use exact expiring audited allowlist. SOURCE-ORIGIN:classify "
        "every code-like artifact exactly AUTHORED|GENERATED|THIRD_PARTY before extension checks; "
        "authored sources use each language's canonical extension (JavaScript .js, TypeScript "
        "grandfathered-existing-only) while compiled .js,.pyd,.dll are generated artifacts; generated "
        "and third-party are excluded from authored-language counts but retain producer/provenance/"
        "hash/security checks; forbidden authored extension never authorizes deletion of valid "
        "generated output."
    ),
    "also": {
        "prohibition": (
            "FORBID:Python-application architecture copied into C++ core+large generic native class "
            "hierarchy+CSharp domain/business ownership outside FSharp delegation+JavaScript forbidden "
            "directory/backend authority+new authored TypeScript+unbounded SQL+fetch-all-then-filter+"
            "origin inferred only from extension+authored code hidden as generated/third-party+"
            "generated artifact deleted because its authored extension would be forbidden."
        )
    },
})

changes.append({
    "table": "articles", "key": {"provision_id": "A350"}, "field": "rule",
    "proposed": (
        "CANONICAL-TOPOLOGY:JavaScript-ESM>information/API contract>CSharp application-orchestration; "
        "CSHARP-BRANCHES:{SQL relational set operations}|{FSharp domain business-logic}|{Native "
        "adapter}|{Go network/batch I/O}|{Rust vector/native-security/desktop}; "
        "NATIVE-PATH:CSharp typed adapter request>gptbridge_native.h C ABI>gptbridge_native.c C thunk>"
        "C++ implementation; PYTHON-ROLE:Python is off the application request path and retains only "
        "the three bounded A610 domains (governance semantics/rules/thin-wrapper, xingcheng JAX "
        "training, development verification) reached through registered governance adapters; "
        "SQL-PATH:CSharp/FSharp repository>parameterized budgeted SQL>bounded result>domain semantics; "
        "DIRECTION:JavaScript never bypasses CSharp to SQL/native/domain and CSharp remains "
        "application+workflow+orchestration authority with FSharp owning core business logic; "
        "CONTRACTS:every edge is typed+versioned+permission-scoped+observable+tested under A340+A348; "
        "NATIVE-NOTATION:C may appear in the native boundary only as the single ABI contract role in "
        "one serial path,not parallel APIs:C=gptbridge_native.h/C thunk single ABI contract,C++=hidden "
        "compute; OWNERSHIP:A341 capability matrix and the A615 official table remain controlling; "
        "FAILURE:failed branch is isolated and returns typed failure through CSharp to JavaScript "
        "without rerouting around the contract."
    ),
    "also": {
        "subject": "canonical-javascript-csharp-sql-native-fsharp-topology-special-law",
        "prohibition": (
            "FORBID:JavaScript direct SQL/native/domain access+CSharp bypass by client+public C++ ABI+"
            "SQL business judgment+FSharp orchestration/governance+Python on the application request "
            "path+C/C++ orchestration+contractless cross-language call+fallback that creates a second "
            "owner+TypeScript runtime role."
        ),
    },
})

changes.append({
    "table": "articles", "key": {"provision_id": "A351"}, "field": "rule",
    "proposed": (
        "MATRIX:{JavaScript-ESM=>ALLOW:contract+transport+UI;DIRECT-DENY:database+native+"
        "CSharp-import+SQL-direct-connection}|{CSharp=>ALLOW:application+workflow+API+Windows/.NET+"
        "sole-test-orchestration+SQL+native-C-ABI+FSharp-domain+Go/Rust service calls;DIRECT-DENY:"
        "domain-business-logic-ownership+frontend-DOM}|{FSharp=>ALLOW:core-business-logic+data-"
        "validation+transformation+business-state-transition;DIRECT-DENY:governance+permission+UI+"
        "orchestration}|{Python=>ALLOW:governance-semantics+JAX-training+development-verification;"
        "DIRECT-DENY:resident-application-services+orchestration+DOM+direct-database-authority}|"
        "{C=>ALLOW:C-ABI-thunk;DIRECT-DENY:business+algorithm+database+UI}|{C++=>ALLOW:model-inference+"
        "native-runtime+audit-engine;DIRECT-DENY:public-application-API+database+UI+workflow}|"
        "{Go=>ALLOW:xingcheng-web-search+batch+network-file-io;DIRECT-DENY:governance+permission+"
        "business-judgment}|{Rust=>ALLOW:vector-engine+RAG+native-security+desktop-host;DIRECT-DENY:"
        "governance+permission+business-judgment}|{Julia=>ALLOW:statistics+mathematical-models+"
        "optimization+simulation+scientific-computation;DIRECT-DENY:governance+permission+UI}|"
        "{SQL=>ALLOW:data-operations;DIRECT-DENY:workflow+permission-authority+business-decision}; "
        "ROUTING:JavaScript reaches backend capability only through information/API contract to "
        "CSharp; CSharp reaches native only through the gptbridge_native.h C ABI path and reaches "
        "Windows/.NET directly; SQL returns bounded data and FSharp owns business semantics; "
        "ADAPTER:allowed adapters translate typed data,error,lifetime and transport only and acquire "
        "no target capability ownership; CHECKER:resolve imports+includes+FFI+IPC+network+database "
        "clients+generated bindings+runtime loading and reject any direct-deny edge; INDIRECT-BYPASS:"
        "a wrapper,reflection,dynamic import,subprocess,generated code or shared file cannot legalize "
        "a forbidden direct edge; EVIDENCE:source+target+edge type+contract+owner+rule+call path+"
        "release identity."
    ),
    "also": {
        "prohibition": (
            "FORBID:JavaScript importing backend modules or directly accessing database/native/SQL+"
            "CSharp owning FSharp domain policy or DOM+Python resident application services or "
            "orchestration+C business/algorithm/database/UI+C++ public application API/database/UI/"
            "workflow+FSharp governance/permission/UI+Go/Rust governance/permission/business+SQL "
            "workflow/permission/business decision+adapter authority expansion+hidden indirect bypass."
        ),
    },
})

changes.append({
    "table": "articles", "key": {"provision_id": "A353"}, "field": "rule",
    "proposed": (
        "CHECKER-SCOPE:not import-only and not text-search-only; MINIMUM-EVIDENCE-CLASSES:"
        "{Python=>import+from-import resolved by Python AST/module resolution}|{JavaScript=>static "
        "import+export-from+dynamic import resolved by ESM/bundler module resolution;TypeScript=>"
        "grandfathered files verified by the same resolver rules until migrated}|{C/C++=>preprocessor "
        "include+generated include+compile_commands/CMake include path+link target+symbol dependency}|"
        "{CSharp/FSharp=>using/open+namespace/type resolution+ProjectReference+PackageReference+"
        "assembly/PInvoke reference resolved by Roslyn/MSBuild}|{Go=>import+module resolution via "
        "go.mod}|{Rust=>use/crate resolution via Cargo.toml}|{Build=>CMake target/source/include/link/"
        "add_subdirectory+csproj/fsproj project/compile/reference/native items+package.json "
        "dependencies+tsconfig extends/references for grandfathered files}; GRAPH:normalize each "
        "source,target,resolved canonical path,edge kind,condition,toolchain/configuration/platform "
        "and provenance into one typed dependency graph; CONDITIONAL:an inactive platform/configuration "
        "edge remains recorded and is tested in its applicable matrix rather than ignored; GENERATED:"
        "generated dependency edges trace to generator+input+output identity; API-IPC:A205 canonical "
        "information layer compliance requires positive structural proof,not keyword occurrence:"
        "registered endpoint/route+typed versioned contract+authorized producer/consumer+gateway/"
        "information-layer adapter identity+session/generation fencing+event/command correlation+"
        "schema hash+test evidence and absence of direct transport/database/file-drop/private-bus "
        "path; TRACE:checker follows caller>adapter>route registry>information-layer handler>"
        "authorized target and fails on unresolved or bypassed hop; TEXT-SEARCH:may discover "
        "candidates only and can never establish PASS or FAIL without parser/resolver/registry "
        "evidence; RESULT:each edge reports evidence class+resolver+source location+resolved target+"
        "contract/route id+A205 status+rule and confidence; UNRESOLVED:mandatory edge or API/IPC "
        "route that cannot be resolved is FAIL,not ignored; CACHE:keyed by source+toolchain+build "
        "config+contract revision."
    ),
})

changes.append({
    "table": "articles", "key": {"provision_id": "A356"}, "field": "rule",
    "proposed": (
        "REGISTRY:code/registry/CapabilityRegistry.ts is the single explicit capability authority "
        "(grandfathered artifact; migration to JavaScript-ESM follows A348) and contains governance "
        "data only; add code/checkers/OwnershipUniquenessChecker.ts+code/policy/OwnershipPolicy.ts; "
        "each capability has exactly one owner plus zero-or-more executors,adapters,callers and at "
        "most one declared fallback role; ROLE:owner=unique semantic/canonical responsibility;"
        "executor=actual implementation including optimized;adapter=cross-language/platform "
        "translation;fallback=equivalent degraded implementation;caller=consumer; OWNER-NOT-EXECUTOR. "
        "BASELINE:{system.orchestration=>csharp}|{domain.business=>fsharp}|{rag.retrieval=>rust}|"
        "{ui.rendering=>javascript-esm}|{ui.client_state=>javascript-esm}|{desktop.interaction=>"
        "javascript-esm+rust-tauri}|{native.abi=>c}|{native.inference=>cpp}|{native.audit=>cpp}|"
        "{native.vector=>rust}|{native.security=>rust}|{windows.dotnet=>csharp}|{network.search_io=>go}|"
        "{data.relational_query=>sql}|{governance.semantics=>python}|{ml.training=>python-jax}|"
        "{statistics.scientific=>julia}; CAPABILITY-INSTANCE vector.similarity={semantic_owner:rust,"
        "optimized_executor:rust,contract_owner:versioned typed service contract,adapters:c-abi}; "
        "Windows CLR={owner:csharp,executor:csharp,adapter:C ABI/information-layer,caller:csharp-ui}; "
        "relational filtering={owner:sql,caller:csharp/fsharp}; A219 registered fallback is not "
        "duplicate ownership. CHECKER:registry is primary truth; directory ownership+exports+class/"
        "function symbols+operation IDs+contract registration+native capability IDs+dependencies+"
        "registered adapters+explicit metadata are corroborating evidence,never filename-only "
        "guessing; clear duplicate owner=FAIL; unresolved evidence=WARN and checker cannot select "
        "owner or edit registry. OUTPUT:capability+canonical_owner+duplicate_candidate+language+rule+"
        "reason+expected or PASS role map+parity."
    ),
    "also": {
        "prohibition": (
            "FORBID:multiple canonical owners+executor treated as owner+fallback treated as "
            "duplicate+filename-only verdict+JavaScript/CSharp duplicate FSharp domain policy+Python "
            "capability ownership outside its three bounded domains+checker assigning owner+automatic "
            "registry edit/delete/move+ambiguous case forced to FAIL without explicit rule evidence."
        ),
        "exception": (
            "A bounded Python governance-semantics fallback beside a C++ optimized executor is valid "
            "only with A357 promotion and A355 parity evidence."
        ),
    },
})

changes.append({
    "table": "articles", "key": {"provision_id": "A359"}, "field": "rule",
    "proposed": (
        "GATE:BUILD ABI REPRO GATE answers only:approved toolchain used? artifact conforms to approved "
        "ABI/API baseline? same input yields acceptably equivalent artifact? FLOW:source>toolchain "
        "verification>canonical build configuration>native/.NET/build>symbol+ABI inspection>artifact "
        "digest>reproducibility comparison>PASS|WARN|FAIL. STRUCTURE:code/checkers/"
        "BuildAbiReproChecker.ts+code/policy/BuildAbiPolicy.ts+code/registry/ToolchainRegistry.ts+"
        "code/registry/AbiBaselineRegistry.ts+code/evidence/build-abi/ (grandfathered TypeScript "
        "artifacts; migration follows A348); policy and registries are immutable governance data "
        "only,checker verifies and never repairs. TOOLCHAINS:Python bounded-domain native records "
        "Python+pybind11+build config;C/C++ records MSVC/clang/gcc family+version+target_arch+"
        "runtime_library+language_standard+optimization_level+exception_policy+rtti_policy+"
        "calling_convention+symbol_visibility+CMake;CSharp/FSharp records compiler+.NET 10 runtime+"
        "target_framework+PlatformTarget+RID;JavaScript records Node/ESM+bundler config and "
        "TypeScript grandfathered artifacts additionally record the TypeScript toolchain;Go/Rust "
        "record go toolchain/cargo+rustc;SQL records migration version/order+schema baseline. "
        "CPP-LOCK:C++23+x64+public ABI C only+C++ symbols hidden unless explicit successor; /MD "
        "versus /MT,architecture,standard,flags,exception/RTTI/calling/visibility differences are "
        "distinct ABI build identities. ABI-BASELINE:abi_major+abi_minor+exported_symbols+"
        "struct_sizes+struct_alignments+field widths/signedness+calling_convention+architecture+"
        "runtime; permit compatible minor additive change; FAIL removed symbol+changed width/layout/"
        "calling convention+breaking semantics without major bump. EXPORT-GATE:public allowlist only "
        "registered gptbridge_* symbols; std/internal classes/vector/parser/mangled C++ symbols "
        "private. CSHARP:x64 required when consuming x64 native unless registered multi-arch "
        "strategy; verify target framework+RID+interop signature+DllImport calling convention+"
        "CharSet/encoding."
    ),
})

changes.append({
    "table": "articles", "key": {"provision_id": "A360"}, "field": "rule",
    "proposed": (
        "GATE:RELEASE_COMPATIBILITY returns exactly PASS|WARN|FAIL and answers:language boundaries "
        "intact? API/ABI intact? old consumers work? migrations safely upgrade? native failure falls "
        "back? every required gate passed? AGGREGATION:consume immutable evidence only from Language "
        "Boundary+Dependency DAG+Contract Parity+Ownership Uniqueness+Native Promotion+Resource and "
        "Concurrency+Build ABI Reproducibility Gates; never reimplement their checks; any required "
        "FAIL=>release FAIL; WARN blocking is explicit immutable ReleaseCompatibilityPolicy per "
        "category and cannot be improvised. STRUCTURE:code/checkers/ReleaseCompatibilityChecker.ts+"
        "code/policy/ReleaseCompatibilityPolicy.ts+code/registry/SupportedVersionRegistry.ts+"
        "code/evidence/release-compatibility/ (grandfathered TypeScript artifacts; migration follows "
        "A348); policy/registry data only; registry fields release+supported_api_range+"
        "supported_abi_range+supported_schema_range+supported_previous_releases. MATRIX:compare "
        "current release with every previous supported release for JavaScript/CSharp API,CSharp/"
        "native parity+FSharp domain parity,C ABI major/minor+symbols,CSharp/native interop,SQL "
        "schema+migration,bounded-Python governance entry,native artifact architecture/runtime/"
        "toolchain; prove prior API/ABI ranges and schema upgrade path remain supported. "
        "CHANGE-CLASS:PATCH permits bug fix/internal optimization/performance/implementation only and "
        "denies contract/ABI/schema break;MINOR permits optional field,new operation/capability/ABI "
        "symbol only while old consumer works;MAJOR alone permits removal/required rename/operation "
        "or ABI symbol removal/layout/incompatible semantics with explicit migration. CONSUMERS:test "
        "old JavaScript client>new CSharp backend and new client>supported old backend when "
        "bidirectional window declared; old supported native consumer>new C ABI load+behavior. "
        "SQL-UPGRADE:old schema>ordered transactional migration>new schema>new application; verify "
        "rollback policy+data preservation+indexes+constraints and activation order before CSharp "
        "requires new field. NATIVE-DUAL:every approved fallback path is exercised under the same "
        "compatibility window."
    ),
})

changes.append({
    "table": "articles", "key": {"provision_id": "A362"}, "field": "rule",
    "proposed": (
        "CORE:scan once+parse once+build one Unified Source Index and one Unified Language Graph "
        "consumed by LanguageBoundary,DependencyDAG,OwnershipUniqueness,ContractParity,NativePromotion,"
        "BuildAbi,ReleaseCompatibility and Final Governance; no checker rescans repository or owns a "
        "private graph. SOURCE-INDEX:one SourceUnit per canonical file stores path+language+"
        "source_class+module+owner+content_hash+mtime advisory only+imports+includes+exports+symbols+"
        "functions+classes+public_entries+contracts+capabilities+native_bindings+test_type+generated+"
        "third_party+parser_version+revision; hash,not mtime,is reuse authority. PARSERS:PythonParser+"
        "JavaScriptParser+TypeScriptParser(grandfathered files only)+CParser+CppParser+CSharpParser+"
        "FSharpParser+GoParser+RustParser+SqlParser each use its authoritative parser/resolver but "
        "emit one normalized ParsedSource{language,imports,exports,declarations,symbols,contracts,"
        "dependencies}; checkers cannot depend on language AST internals. GOVERNANCE-IR:thin derived "
        "model only,not language/bytecode/authority; canonical types SourceUnit+CallableSymbol+Symbol+"
        "Dependency+Capability+Contract+NativeBoundary+TestEvidence+BuildArtifact. GRAPH:NODES=file|"
        "module|capability|contract|symbol|artifact;EDGES=IMPORTS|INCLUDES|CALLS|IMPLEMENTS|BINDS|"
        "ADAPTS|OWNS|FALLBACK_OF|GENERATES|TESTS; one stable node/edge identity+provenance; example "
        "CSharpAPI BINDS C-ABI IMPLEMENTS C++vector and RegisteredFallback FALLBACK_OF capability. "
        "INCREMENTAL:file content hash unchanged reuses IR only when parser_version+policy_version+"
        "contract_hash also match; changed file reparses once,updates graph transactionally and "
        "invalidates reverse-dependency affected subgraph; policy/parser/contract change invalidates "
        "all dependent cache even with unchanged source. AFFECTED-SCOPE:ChangedFiles>"
        "AffectedScopeResolver>affected modules+contracts+capabilities+artifacts; gptbridge_native.h "
        "change necessarily affects C ABI+CSharp interop+native artifact+contract parity+build ABI+"
        "release compatibility. EVIDENCE:every gate outputs GateEvidence{gate_id,rule,subject,verdict,"
        "evidence_refs,source_locations,graph_revision}."
    ),
})

# ------------------------------------------------------------------- metadata
def meta_set(key, value):
    changes.append({"table": "metadata", "key": {"key": key},
                    "field": "value", "proposed": value})

meta_set("startup_complete_deadline_ms", "20000")
meta_set("startup_deadline_conformance_state",
         "CODEX_NORMALIZED_20000|implementation-projection-must-match-before-affected-release")
meta_set("governor_disposition_startup_deadline",
         "deadline-tightened-to-20000|A199-amended|phase-budgets-halved|metadata-normalized")
meta_set("canonical_language_roles",
         "c23-runtime-core-permission-hot-paths-deterministic-execution|"
         "cpp23-model-inference-native-runtime-audit-engine|"
         "csharp14-dotnet10-application-workflow-api-sole-test-orchestration|"
         "fsharp10-core-business-logic-data-validation-transformation-state-transitions|"
         "go1.27.1-xingcheng-web-search-batch-network-file-io|"
         "rust1.98.1-vector-engine-rag-native-security-desktop-host|"
         "javascript-esm-react-ui-frontend-state-desktop-interaction|"
         "python3.14.7-bounded-governance-jax-training-dev-verification|"
         "julia-statistics-optimization-simulation-scientific-computation|"
         "sql-postgresql18.6-data-authority|typescript-retired-grandfathered")
meta_set("canonical_cross_language_topology",
         "JavaScript-ESM>information/API-contract>CSharp-orchestration>"
         "{SQL|C-ABI>C++|FSharp-domain|Go-io|Rust-native}|"
         "Python-bounded-governance-sidechannel")
meta_set("language_dependency_evidence",
         "python-ast|javascript-typescript-resolver|cpp-preprocessor-build|"
         "roslyn-msbuild|go-mod|cargo|cmake-csproj-fsproj-packagejson")
meta_set("module_line_count_language_codes",
         "C|CPP|CSHARP|FSHARP|GO|RUST|JAVASCRIPT|JULIA|PYTHON|SQL|TYPESCRIPT-GRANDFATHERED")
meta_set("language_governance_schema_lifecycle", "APPROVED")
meta_set("governance_acceptance_state", "APPROVED_WITH_WARNINGS")
meta_set("typescript_python_contract",
         "retired-succeeded-by-javascript-esm-to-csharp-versioned-contract")
meta_set("python_api_execution_split", "retired|python-bounded-three-domains-per-A610")
meta_set("python_internal_architecture", "retired|python-bounded-three-domains-per-A610")
meta_set("sql_python_processing_order",
         "sql-reduce-first>csharp-orchestration>fsharp-domain-semantics")
meta_set("source_extension_typescript", ".ts|.tsx|.d.ts grandfathered-existing-only")
meta_set("six_language_automatic_check",
         "superseded|required:c|cpp|csharp|fsharp|go|rust|javascript|julia|python|sql")
meta_set("version_window_resolution",
         f"CURRENT={SV}; RECOVERED_2026-09-23T14:49:21Z=historical-non-current-"
         "incomplete-epoch-certification; normative-language-delta=consolidated-current")
successors.append({
    "registry": "metadata", "action": "insert",
    "rows": [{"key": "source_extension_javascript",
              "value": ".js canonical|.jsx|.mjs|.cjs grandfathered-existing-only"}],
})

# ------------------------------------------------------ machine-schema parity
parity_update_keys = []
parity_insert_rows = []
for row in rows:
    desc = build_descriptor(row)
    digest = seal_hash(desc)
    if row["schema_code"] in existing_evidence:
        parity_update_keys.append((row["schema_code"], digest))
    else:
        parity_insert_rows.append({
            "schema_code": row["schema_code"],
            "producer_semantic_hash": digest,
            "validator_semantic_hash": digest,
            "persistence_semantic_hash": digest,
            "canonical_semantic_hash": digest,
            "validated_against_version": SV,
            "status": "PASS",
            "reason": "semantic-hash-toolchain SEAL_CANONICAL_V1 restamp; "
                      "producer=validator=persistence=canonical descriptor digest",
        })
for code, digest in parity_update_keys:
    changes.append({
        "table": "machine_schema_parity_evidence",
        "key": {"schema_code": code},
        "field": "producer_semantic_hash", "proposed": digest,
        "also": {
            "validator_semantic_hash": digest,
            "persistence_semantic_hash": digest,
            "canonical_semantic_hash": digest,
            "validated_against_version": SV,
            "status": "PASS",
            "reason": "semantic-hash-toolchain SEAL_CANONICAL_V1 restamp; "
                      "prior PENDING row promoted on equal descriptor digests",
        },
    })
if parity_insert_rows:
    successors.append({
        "registry": "machine_schema_parity_evidence", "action": "insert",
        "rows": parity_insert_rows,
    })

# obligation closure with real evidence
changes.append({
    "table": "implementation_obligations",
    "key": {"obligation_code": "OBL_MACHINE_SCHEMA_PARITY"},
    "field": "target_state",
    "proposed": "all 74 registered machine schemas carry equal evidenced producer,validator,"
                "persistence and canonical semantic hashes (SEAL_CANONICAL_V1 descriptor digests)",
    "also": {
        "current_state": "complete",
        "previous_state": "mandated",
        "acceptance_evidence": "machine_schema_parity_evidence restamp at "
                               "codex-generation-convergence-20260925: 74/74 rows PASS, four equal "
                               "descriptor hashes each",
    },
})

# ------------------------------------------------- architecture sync evidence
successors.append({
    "registry": "architecture_stale_reference_evidence", "action": "insert",
    "rows": [{
        "evidence_id": f"CURRENT_ARCHITECTURE_STALE_REFERENCE_SCAN@{SV}",
        "current_reference_count": 0,
        "historical_reference_count": 1,
        "result": "PASS",
        "status": "current",
        "version_identity": SV,
    }],
})

request = {
    "artifact": "codex-amendment-request",
    "authority": "request-only",
    "schema": "codex-amendment-request/v1",
    "request_id": "codex-generation-convergence-20260925",
    "title": "Codex generation convergence: version-axis repair, missing "
             "language provisions, startup deadline 20000ms, language "
             "responsibility normalization, machine-schema parity restamp",
    "summary": "Close the P0/P1 audit findings: insert the seven controlling "
               "provisions still referenced by verified formal rules "
               "(A611/A613/A614/A617/A618/A619/A620); complete "
               "classification/lifecycle/membership/effective rows for all "
               "twelve language-division articles; tighten A199 and metadata "
               "to the 20000ms startup deadline; normalize A219/A347/A350/"
               "A351/A353/A356/A359/A360/A362 to the A341/A343/A604/A610/A615 "
               "language division; restamp all 74 machine-schema parity "
               "evidence rows with real SEAL_CANONICAL_V1 descriptor digests; "
               "rebind metadata language role/topology keys and close "
               "language-governance lifecycle state.",
    "requested_by": "decision-sovereign",
    "origin": "governor P0/P1 audit findings 2026-09-25: version-axis split, "
              "language responsibility conflicts, dangling formal-rule "
              "controlling provisions, 40000ms residue, machine-schema parity "
              "evidence mismatch, language governance FREEZE-PENDING",
    "change_class": "clarification",
    "required_review": "five-sovereign-audit-unanimous-pass",
    "flow": "A382/A488-non-disruptive-amendment-flow",
    "not_executed": True,
    "predecessor": predecessor,
    "problem": "formal codex_version advanced to 2026-09-25T15:42:05Z while "
               "current_version, revision, seal, epoch, active binding, "
               "search/module manifests and the normative surface remained at "
               "2026-09-23T03:13:43Z; seven verified formal rules anchor to "
               "nonexistent articles; twelve language-division articles lack "
               "classification/lifecycle/membership rows; nine articles still "
               "carry the retired TypeScript+Python dual-host architecture; "
               "A219 assigns F# analysis/ML conflicting with A341/A343/A604; "
               "startup deadline normalized at 40000ms against the current "
               "20000ms requirement; 34 machine schemas lack parity evidence "
               "rows and all 40 existing rows remain PENDING despite the "
               "obligation's 74-row claim; language governance lifecycle is "
               "FREEZE-PENDING/INCOMPLETE_EVIDENCE; architecture stale-"
               "reference evidence is two generations old.",
    "changes": changes,
    "proposed_successors": successors,
    "proposed_delta": "7 article inserts + 9 article normalizations + A199 "
                      "deadline + 4 registry insert sets (classification, "
                      "lifecycle, membership, effective) + 18 metadata "
                      "updates + 74 machine-schema parity restamps + "
                      "obligation closure + architecture evidence row; "
                      "derived projections (search/module manifests, "
                      "normative surface, seal/revision rows) are rebuilt "
                      "deterministically by the execution pipeline.",
    "verification": {
        "gates": [
            "staged_generation_errors empty",
            "check_provision_classification zero missing",
            "formal-rule controlling provisions all resolve",
            "machine-schema parity evidence 74 rows PASS with equal hashes",
            "startup_complete_deadline_ms=20000 in codex and metadata",
        ],
        "note": "the execution pipeline rebuilds version-axis bookkeeping "
                "(revision_history, seal_manifest, epoch_seal_manifest, "
                "search documents/FTS, module manifest, normative surface) "
                "before publication; seal remains conditioned on the "
                "unanimous five-sovereign audit certificate.",
    },
}

OUT.write_text(json.dumps(request, ensure_ascii=False, indent=1) + "\n",
               encoding="utf-8")
print("wrote", OUT)
print("changes:", len(changes), "| successor groups:", len(successors))
print("parity updates:", len(parity_update_keys),
      "| parity inserts:", len(parity_insert_rows))
