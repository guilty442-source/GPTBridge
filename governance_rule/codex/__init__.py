"""Governance Codex (程式碼法典) — the single authoritative rule source.

This module is the CANON (法典) split into two references:

  * This module  = the CODE-CODE X (來源依據, authoritative).  Every provision
    is declared in CODE-FORM TOKENS (结构化程式語言語式) — an immutable,
    declarative, non-natural-language specification.  It holds NO executable
    logic and NO callable functions; it is data only, and every value is
    frozen (immutable).  This is the ONLY authority usable as a decision/citation
    basis: per A36/E22/P17, the Chinese reference is backup-only and carries NO
    decision authority.
  * Chinese reference (governance_rule.codex.chinese) = the 中文法典, backup
    only, for human readability; never referenced for enforcement.

Keep this module free of functions, methods, and side effects.  The Chinese
explanatory text of every provision lives ONLY in ``chinese.py``, per
A38/E24/P19 — the authoritative codex never carries natural-language narrative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Tuple

from .sovereigns import CodexSovereign, SOVEREIGNS


@dataclass(frozen=True)
class CodexPreamble:
    """Preamble — the purpose and highest-priority standing of the codex."""

    title: str
    authority_rank: str
    issuance: str
    binding_scope: str


@dataclass(frozen=True)
class CodexSection:
    """A section (編/章/篇) of the codex — structural partition, data only."""

    index: str
    title: str
    summary: str


@dataclass(frozen=True)
class CodexPrinciple:
    """A single immutable governing principle (declared, not executed)."""

    id: str
    statement: str
    binding: bool


@dataclass(frozen=True)
class CodexArticle:
    """An immutable governing article (declared, not executed)."""

    id: str
    section: str
    subject: str
    rule: str
    prohibition: str = ""
    exception: str = ""


@dataclass(frozen=True)
class CodexEdict:
    """An immutable, non-mutating standing edict (declared, not executed)."""

    id: str
    area: str
    edict: str
    immutability: str


@dataclass(frozen=True)
class CodexSavings:
    """Savings/closure: the canonical document is immutable and not functional."""

    mutability: str
    function: str
    amendment: str
    overriding_authority: str
    interpretation: str = "governance-codex-is-the-only-interpreter-of-its-own-canon"
    conflict_resolution: str = "codex-preempts-all-subordinate-authority"


_DEFAULT_CODEX_SAVINGS = CodexSavings(
    mutability="immutable-sealed",
    function="none",
    amendment="explicit-versioned-full-replacement-only",
    overriding_authority="governance-codex-supreme",
    interpretation="governance-codex-is-the-only-interpreter-of-its-own-canon",
    conflict_resolution="codex-preempts-all-subordinate-authority",
)


@dataclass(frozen=True)
class GovernanceCodex:
    """The immutable Governance Codex — data only, never functional.

    This is the highest rule layer.  It is pure declaration: no functions, no
    callables, no execution surface.  Every field above is frozen.
    """

    schema: str
    codex_version: float
    preamble: CodexPreamble
    sections: Tuple[CodexSection, ...] = field(default_factory=tuple)
    principles: Tuple[CodexPrinciple, ...] = field(default_factory=tuple)
    articles: Tuple[CodexArticle, ...] = field(default_factory=tuple)
    edicts: Tuple[CodexEdict, ...] = field(default_factory=tuple)
    savings: CodexSavings = field(default=_DEFAULT_CODEX_SAVINGS)
    sovereigns: Tuple[CodexSovereign, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The sealed Codex (data only, frozen, no functions).  Editing here requires
# an explicit full versioned replacement; it can never be patched at runtime.
#
# PROVISIONS ARE DECLARED IN CODE-FORM TOKENS, NOT NATURAL LANGUAGE.  The
# Chinese explanatory text of every provision lives ONLY in chinese.py.
# ---------------------------------------------------------------------------

GOVERNANCE_CODEX: Final[GovernanceCodex] = GovernanceCodex(
    schema="gptbridge-governance-codex-v1",
    codex_version=1.0,
    preamble=CodexPreamble(
        title="GPTBridge Governance Codex",
        authority_rank="supreme",
        issuance="promulgated-as-the-highest-rule-layer",
        binding_scope="all-modules-and-all-execution",
    ),
    sections=(
        CodexSection(
            index="1",
            title="general-provisions",
            summary="codex=highest-rule-layer; pure-declaration; independent-storage",
        ),
        CodexSection(
            index="2",
            title="separation-and-accountability",
            summary="decision/execution/governance-power-separation; governed-executor-delegation",
        ),
        CodexSection(
            index="3",
            title="permission",
            summary="directory-driven-read-only; permission-sovereign-no-grant/delegate/mutate",
        ),
        CodexSection(
            index="4",
            title="system-responsibility",
            summary="git/sql/qdrant/llm-separation; management-vs-execution-separation",
        ),
        CodexSection(
            index="5",
            title="closed-security",
            summary="deny-by-default; explicit-allow; fail-closed; no-internal-disclosure",
        ),
        CodexSection(
            index="6",
            title="sovereignty",
            summary="all-sovereign-and-xingcheng-decisions-reference-the-codex",
        ),
        CodexSection(
            index="7",
            title="amendment-and-protection",
            summary="immutable; os-readonly-protection; full-versioned-replacement-only",
        ),
        CodexSection(
            index="8",
            title="interpretation-and-conflict",
            summary="codex-is-own-interpreter; preempts-all-subordinate-authority",
        ),
    ),
    principles=(
        CodexPrinciple(
            id="P1",
            statement="MANDATE:governance-rule; LAYER:highest; STORAGE:independent; FLAG:forbid-arbitrary-amendment",
            binding=True,
        ),
        CodexPrinciple(
            id="P2",
            statement="FORM:pure-declaration; ENFORCEMENT:none; REPRESENTATION:frozen-schema-construction+read-only-export; EXEC:delegated-to-governed-executor",
            binding=True,
        ),
        CodexPrinciple(
            id="P3",
            statement="MUTATION:frozen; AMENDMENT:full-versioned-replacement-only",
            binding=True,
        ),
        CodexPrinciple(
            id="P4",
            statement="OWNER:permission-sovereign; SCOPE:all-permission-matters; ACTIONS:manage-issue-terminate-supervise; EXEC:delegated",
            binding=True,
        ),
        CodexPrinciple(
            id="P5",
            statement="POWERS:decision/execution/governance; PRINCIPLE:separate; FORBID:merger-of-powers",
            binding=True,
        ),
        CodexPrinciple(
            id="P6",
            statement="MODE:deny-by-default+explicit-allowlist; REQUIRES:explicit-auditable-grant",
            binding=True,
        ),
        CodexPrinciple(
            id="P7",
            statement="SYSTEMS:git/postgresql/qdrant/rag/llm; PRINCIPLE:separation; FORMAL-ROLES:postgresql-central-structured-data/qdrant-canonical-semantic-index/git-version-history/rag-retrieval/llm-reasoning; DEPENDENCIES:governance-approved-inventory-only; FALLBACK:bounded+declared+reconciled; FORBID:role-substitution",
            binding=True,
        ),
        CodexPrinciple(
            id="P8",
            statement="DECISION-SOURCE:codex; APPLIES:all-system-sovereigns; XINGCHENG:outside-system-decision-chain",
            binding=True,
        ),
        CodexPrinciple(
            id="P9",
            statement="AUTHORITY:supreme-immutable; FORBID:subordinate-override-or-amendment-path",
            binding=True,
        ),
        CodexPrinciple(
            id="P10",
            statement="XINGCHENG:local-native-model; AUTHORITY:complete-owned-domain; ISOLATION:system-detached; SYSTEM-PARTICIPATION:none",
            binding=True,
        ),
        CodexPrinciple(
            id="P11",
            statement="SYSTEM-SOVEREIGN:top-orchestrator; SCOPE:full-lifecycle+integration; DELEGATE:to-sub-sovereigns; EXEC:no-powerful-work",
            binding=True,
        ),
        CodexPrinciple(
            id="P12",
            statement="GOVERNANCE:AUTHORITY=codex+governance-authority; ROLE:highest-rule-layer-maintenance",
            binding=True,
        ),
        CodexPrinciple(
            id="P13",
            statement="SYSTEM-RESOURCE-SUB-SOVEREIGN:own-resource-matters; SCOPE:state-monitor-provision-delegate-release; EXEC:delegated",
            binding=True,
        ),
        CodexPrinciple(
            id="P14",
            statement="SYSTEM-DATA-SUB-SOVEREIGN:own-data-matters; SCOPE:access-spec-consistency-integrity-check; EXEC:delegated",
            binding=True,
        ),
        CodexPrinciple(
            id="P15",
            statement="SYSTEM-INTEGRATION-SUB-SOVEREIGN:own-cross-sovereign-module-interface; SCOPE:coordinate-sync-bus; EXEC:delegated",
            binding=True,
        ),
        CodexPrinciple(
            id="P16",
            statement="STACK:python+typescript+cpp+c+csharp+sql-hybrid; MANDATORY:true; LANGUAGES:only-python-typescript-cpp-c-csharp-sql; FORMAL-TOOLS:postgresql,qdrant,git,rag",
            binding=True,
        ),
        CodexPrinciple(
            id="P17",
            statement="CHINESE-CODEX:backup-only; CITATION/BASIS:none; DECISION-BASIS:authoritative-codex-only",
            binding=True,
        ),
        CodexPrinciple(
            id="P18",
            statement="CODE-ORIGIN:local-owned-application-code; STACK:python+typescript+cpp+c+csharp+sql; DEPENDENCIES:explicit-inventory+version+license+security-review+local-execution; EXTERNAL-SERVICE:governed-network-capability-only; FORBID:unapproved-dependency/unmanaged-binary/uncontrolled-hosting",
            binding=True,
        ),
        CodexPrinciple(
            id="P19",
            statement="CODEX-LANGUAGE:programming-language; CHINESE:reserved-to-chinese-codex; BASIS:backup-only-none",
            binding=True,
        ),
        CodexPrinciple(
            id="P20",
            statement="INTERFACE-LAYER:presentation-of-decisions-state; AUTHORITY:no-decide-no-exec; STACK:python-typescript-cpp-c-csharp-sql-only; BIND:all-modules-and-all-execution",
            binding=True,
        ),
        CodexPrinciple(
            id="P21",
            statement="AUDIT:mandatory-ledger; WRITE:governed-executor; OWN:system-data-sub-sovereign; RETENTION:by-governance-policy; READ:governance-authority+maintenance-analysis",
            binding=True,
        ),
        CodexPrinciple(
            id="P22",
            statement="VIOLATION:stop-record-adjudicate; STOP:deny-fail-closed; ADJUDICATE:governance-authority-final; FORBID:continued-execution-on-violation",
            binding=True,
        ),
        CodexPrinciple(
            id="P23",
            statement="FORMAL-TOOLS:postgresql,qdrant,git,rag,python,typescript,cpp,c,csharp,sql; GOVERNANCE:governed-by-codex; FORBID:non-formal-substitution",
            binding=True,
        ),
        CodexPrinciple(
            id="P24",
            statement="SYSTEM-LANGUAGE-REVIEW-SUB-SOVEREIGN:own-programming-language-review; SCOPE:conformance-acceptance-migration; EXEC:delegated",
            binding=True,
        ),
        CodexPrinciple(
            id="P25",
            statement="SYSTEM-THIRD-PARTY-SUB-SOVEREIGN:own-third-party-software-management; SCOPE:introduction-version-license-security; EXEC:delegated",
            binding=True,
        ),
        CodexPrinciple(
            id="P26",
            statement="GPTBRIDGE-BOOT:launcher-interface-only>boot-core-environment-check+governance-audit+postgresql+qdrant+ollama+governance-system; POST-BOOT:{sovereign-decision||information-peer-authority}>sub-sovereign-dispatch>module-execution; BOUNDARIES:exclusive",
            binding=True,
        ),
    ),
    articles=(
        CodexArticle(
            id="A1",
            section="1",
            subject="governance-rule",
            rule="FORM:codex-file; STORAGE:independent; LAYER:highest",
            prohibition="FORBID:arbitrary-amendment",
        ),
        CodexArticle(
            id="A2",
            section="1",
            subject="function",
            rule="AUTHORITY-SURFACE:pure-declarative-provisions; ENFORCEMENT:none; REPRESENTATION:frozen-schema-construction+read-only-export",
            prohibition="FORBID:business-logic/permission-decision/runtime-mutation/side-effecting-enforcement-in-codex",
        ),
        CodexArticle(
            id="A3",
            section="1",
            subject="storage",
            rule="AUTHORITY-SOURCE:physical-file+independent-codex-data; SOURCE:sole-authority",
            prohibition="FORBID:compile/obscure/otherwise-replace-codex-body-as-authority",
        ),
        CodexArticle(
            id="A4",
            section="2",
            subject="separation-of-powers",
            rule="DECISION:sovereign+codex; EXECUTION:governed-executor; GOVERNANCE:governance",
            prohibition="FORBID:single-party-holds-decision+execution",
        ),
        CodexArticle(
            id="A5",
            section="2",
            subject="delegation",
            rule="EXECUTION:delegate-to-governed-executor; DIRECT-EXEC:sovereign+codex-none",
            prohibition="FORBID:sovereign-powerful-work-in-process",
        ),
        CodexArticle(
            id="A6",
            section="3",
            subject="permission",
            rule="OWNER:permission-sovereign; ACTIONS:manage-issue-terminate-supervise; EXEC:none; BASIS:this-codex",
            prohibition="FORBID:proxy-permission-matters; FORBID:permission-sovereign-exceed-codex-or-exec",
        ),
        CodexArticle(
            id="A7",
            section="3",
            subject="permission-directory",
            rule="MODE:directory-driven; DEFINES:role/capability/action/target/data-scope; USE:issue-terminate-supervise-basis",
            prohibition="FORBID:self-grant/delegation/inheritance/privilege-escalation",
        ),
        CodexArticle(
            id="A22",
            section="3",
            subject="permission-termination",
            rule="AUTHORITY:permission-sovereign; ACTION:terminate-issued-permission; BASIS:codex+directory",
            prohibition="FORBID:unauthorized-terminate/retain-should-terminate",
        ),
        CodexArticle(
            id="A23",
            section="3",
            subject="permission-identifiers",
            rule="PERMISSION-ID:permission-sovereign-managed; FUNC:issue-terminate-supervise",
            prohibition="FORBID:module-self-issue/change/control-own-permission-id",
        ),
        CodexArticle(
            id="A8",
            section="4",
            subject="system-responsibility",
            rule="GIT:version+history; POSTGRESQL:central-structured-official-data+shared-transport+audit; SQLITE:owner-private-operational-state/cache/checkpoint+bounded-degraded-fallback-with-reconciliation; QDRANT:canonical-semantic-index; LOCAL-VECTOR:bounded-degraded-cache-only; RAG:hybrid+code+agentic+memory; LLM:understand-reason-operate; OWNERSHIP:local-governed",
            prohibition="FORBID:role-substitution/sqlite-as-central-official-or-shared-audit/local-vector-as-canonical-semantic-index/unreconciled-fallback",
        ),
        CodexArticle(
            id="A9",
            section="4",
            subject="management-owner",
            rule="PLATFORM-MANAGEMENT:system-sovereign; XINGCHENG-MANAGEMENT:isolated-owned-domain-only+complete; TOOL-LEVEL-MANAGEMENT:tool-owner-under-governance-rule",
            prohibition="FORBID:management-exec-or-formal-write; FORBID:tool-management-bypass-governance",
        ),
        CodexArticle(
            id="A10",
            section="5",
            subject="authorization",
            rule="MODE:explicit-allowlist; NO-EXPLICIT-GRANT:deny",
            prohibition="FORBID:default-on/implicit/duff-wildcard/impersonation/override",
        ),
        CodexArticle(
            id="A11",
            section="5",
            subject="fail-closed",
            rule="FAILURE/UNVERIFIED:deny-and-close",
            prohibition="FORBID:open-after-fail; FORBID:disclose-permission-detail",
        ),
        CodexArticle(
            id="A12",
            section="6",
            subject="sovereign-decision",
            rule="DECISION-SOURCE:codex; APPLIES:runtime/maintenance/permission-sovereigns; XINGCHENG:outside-system-decision-chain",
            prohibition="FORBID:sovereign-embedded-decision-source",
        ),
        CodexArticle(
            id="A13",
            section="6",
            subject="hot-update",
            rule="HOT-UPDATE:version-gated-frozen-boundary; APPLY:requires-governance-authorization",
            prohibition="FORBID:unauthorized-runtime-replace/hot-update-codex",
        ),
        CodexArticle(
            id="A18",
            section="6",
            subject="xingcheng",
            rule="XINGCHENG:local-native-model; DOMAIN:fully-owned+physically-logically-system-isolated; AUTHORITY:complete-inside-own-domain; SYSTEM-RANK:none",
            prohibition="FORBID:xingcheng-enter-observe-coordinate-decide-authorize-execute-override-or-access-system; FORBID:system-enter-or-control-xingcheng-domain",
        ),
        CodexArticle(
            id="A19",
            section="6",
            subject="xingcheng-thinking",
            rule="XINGCHENG:independent-thinking+decision+management-inside-isolated-owned-domain; SYSTEM-CONTEXT:no-access+no-role",
            prohibition="FORBID:xingcheng-thinking-as-system-decision-source; FORBID:cross-isolation-boundary",
        ),
        CodexArticle(
            id="A20",
            section="6",
            subject="xingcheng-power",
            rule="XINGCHENG-POWER:complete-inside-owned-domain including observe+analyze+reason+decide+manage+authorize+execute+write+delete+configure",
            prohibition="FORBID:any-xingcheng-power-outside-owned-domain; FORBID:any-system-target-or-effect",
        ),
        CodexArticle(
            id="A21",
            section="6",
            subject="xingcheng-no-power",
            rule="XINGCHENG-SYSTEM-POWER:none; XINGCHENG-OWN-DOMAIN-POWER:complete",
            prohibition="FORBID:system-exec/system-grant/system-override/system-state-access/system-information-access",
        ),
        CodexArticle(
            id="A14",
            section="7",
            subject="immutability",
            rule="CODE-DATA:frozen; RUNTIME-MUTATION:none",
            prohibition="FORBID:runtime-write/hot-update-codex",
            exception="explicit-versioned-full-replacement-only",
        ),
        CodexArticle(
            id="A15",
            section="7",
            subject="protection",
            rule="FILE:os-readonly-protected; INTEGRITY:verify-before-load+exec",
            prohibition="FORBID:backup/restore/hot-update/any-path-overwrite-codex",
        ),
        CodexArticle(
            id="A16",
            section="8",
            subject="supremacy",
            rule="INTERPRETER:codex-itself; CONFLICT:preempt-subordinate-authority",
            prohibition="FORBID:subordinate-interpretation-replace/override-codex",
        ),
        CodexArticle(
            id="A17",
            section="8",
            subject="amendment",
            rule="AMENDMENT:explicit-signed-higher-version-full-replacement",
            prohibition="FORBID:incremental/runtime-amendment",
        ),
        CodexArticle(
            id="A24",
            section="6",
            subject="maintenance",
            rule="OWNER:maintenance-sovereign; DUTIES:update/system-health-monitor(incl-data-integrity)/auto-repair/fault-determine/backup; BASIS:codex+governance-authorization",
            prohibition="FORBID:proxy-maintenance; FORBID:maintenance-sovereign-exec-overstep-codex",
        ),
        CodexArticle(
            id="A25",
            section="6",
            subject="maintenance-health",
            rule="OWNER:maintenance-sovereign; SCOPE:system-health-monitor; INCLUDE:data-integrity-check+presentation",
            prohibition="FORBID:ignore/hide-data-integrity-abnormal-in-health-monitor",
        ),
        CodexArticle(
            id="A26",
            section="6",
            subject="system-sovereign",
            rule="SYSTEM-SOVEREIGN:top-orchestrator; SCOPE:full-lifecycle+integration; DELEGATE:sub-sovereigns; BASIS:codex",
            prohibition="FORBID:system-sovereign-overstep-exec/hold-exec",
        ),
        CodexArticle(
            id="A27",
            section="6",
            subject="system-sovereign-delegation",
            rule="SYSTEM-SOVEREIGN:no-decide-sub-sovereign-details; SOURCE:from-sub-sovereign-or-app",
            prohibition="FORBID:system-sovereign-decide-sub-details/direct-exec-sub-work",
        ),
        CodexArticle(
            id="A28",
            section="6",
            subject="runtime",
            rule="OWNER:system-runtime-sub-sovereign; SCOPE:run+service-maintenance; INCLUDE:process-survival+runtime-integrity; BASIS:codex-delegation",
            prohibition="FORBID:system-runtime-sub-sovereign-overstep-exec/codex",
        ),
        CodexArticle(
            id="A29",
            section="8",
            subject="governance-authority",
            rule="GOVERNANCE:codex+governance-authority; ROLE:highest-rule-layer-maintenance; DUTY:guard-immutable+integrity",
            prohibition="FORBID:subordinate-proxy-governance/override-governance-codex",
        ),
        CodexArticle(
            id="A30",
            section="6",
            subject="resource",
            rule="OWNER:system-resource-sub-sovereign; SCOPE:all-resource-body-matters; ITEMS:memory/disk/model/compute; FUNC:state-monitor-provide-delegate-release; BASIS:codex",
            prohibition="FORBID:system-resource-sub-sovereign-overstep-exec/data/permission",
        ),
        CodexArticle(
            id="A31",
            section="6",
            subject="data",
            rule="OWNER:system-data-sub-sovereign; SCOPE:all-data-body-matters; ITEMS:structured/semantic-index/version-history; FUNC:access-spec-consistency-integrity-check+data-directory; BASIS:codex",
            prohibition="FORBID:system-data-sub-sovereign-overstep-exec/resource/permission",
        ),
        CodexArticle(
            id="A32",
            section="6",
            subject="integration",
            rule="OWNER:system-integration-sub-sovereign; SCOPE:cross-sovereign-module-structural-interface/channel/sync/bus; BASIS:codex-delegation",
            prohibition="FORBID:system-integration-sub-sovereign-decision-layer-coordinate/overstep-exec",
        ),
        CodexArticle(
            id="A33",
            section="6",
            subject="boundary-data-integrity",
            rule="DATA-INTEGRITY-CHECK:system-data-sub-sovereign; SYSTEM-HEALTH-MONITOR:maintenance-sovereign; PRESENT:data-integrity-health-only",
            prohibition="FORBID:maintenance-proxy-data-integrity-check; FORBID:system-data-sub-sovereign-proxy-health-monitor",
        ),
        CodexArticle(
            id="A34",
            section="6",
            subject="boundary-integration-xingcheng",
            rule="INTEGRATION:system-structural-interface+sync; XINGCHENG:fully-detached-no-system-interface; TOOL-LEVEL-DECISION:tool-owner-under-governance-rule",
            prohibition="FORBID:integration-bridge-to-xingcheng; FORBID:xingcheng-system-integration; FORBID:tool-decision-bypass-governance",
        ),
        CodexArticle(
            id="A35",
            section="4",
            subject="architecture-hybrid",
            rule="STACK:python+typescript+cpp+c+csharp+sql-hybrid; PYTHON:main-controller+orchestration+governed-logic; TYPESCRIPT:ui+build-time+governance-checker+type-safety; C:low-level-interface+native-system-binding; CPP:performance-core+native-compute; CSHARP:windows-dotnet+clr+governed-interop; SQL:data-layer+structured-query+governed-persistence; SOLE:allowed-code-architecture",
            prohibition="FORBID:any-programming-language-except-python-typescript-cpp-c-csharp-sql; FORBID:replace-hybrid-architecture",
        ),
        CodexArticle(
            id="A36",
            section="8",
            subject="codex-reference",
            rule="CHINESE-CODEX:backup-only; CITATION/BASIS:none; DECISION-BASIS:authoritative-codex-only",
            prohibition="FORBID:use-chinese-codex-as-verdict/citation/decision-basis",
        ),
        CodexArticle(
            id="A37",
            section="4",
            subject="code-origin",
            rule="CODE-ORIGIN:local-owned-application-code; STACK:python+typescript+cpp+c+csharp+sql; DEPENDENCIES:system-third-party-sub-sovereign-approved+inventory-pinned+license-reviewed+security-reviewed+local-execution; NETWORK:explicit-capability+static-allowlist+auditable-adapter; EXTERNAL-AI:ai-collaboration-or-governed-embedded-browser-only",
            prohibition="FORBID:unapproved/uninventoried/unpinned-dependency+unmanaged-binary+uncontrolled-cloud-runtime+network-without-explicit-governed-adapter",
        ),
        CodexArticle(
            id="A38",
            section="8",
            subject="codex-language",
            rule="CODEX-FORM:programming-language(structured-code); CHINESE:stored-in-chinese-codex; BASIS:backup-only",
            prohibition="FORBID:natural-language-narrative-replace-codex-form; FORBID:chinese-codex-as-decision-basis",
        ),
        CodexArticle(
            id="A39",
            section="2",
            subject="actors",
            rule="ACTOR-CLASSES:human-operator/governed-app/sovereign/xingcheng; REQUESTER:one-of-declared-classes; IDENTITY:verified-at-entry",
            prohibition="FORBID:undeclared-actor-class/forged-identity/requester-impersonation",
        ),
        CodexArticle(
            id="A40",
            section="3",
            subject="bootstrap",
            rule="BOOTSTRAP:initial-allowlist; AUTHORITY:governance-authority-one-time; TIMING:first-valid-load; EFFECT:directory-driven-thereafter",
            prohibition="FORBID:post-bootstrap-seed/privilege-escalation-path",
        ),
        CodexArticle(
            id="A41",
            section="7",
            subject="governor-and-seal",
            rule="GOVERNOR:human-operator; OFFICE:governance-authority; AMEND:deliberate-act; SEAL:versioned-digest-manifest; VERIFY:before-every-load",
            prohibition="FORBID:non-governor-seal/forged-manifest/verify-skip",
        ),
        CodexArticle(
            id="A42",
            section="3",
            subject="directory-write",
            rule="DIRECTORY-WRITE:permission-sovereign-decision; EXEC:governed-executor; STORE:system-data-sub-sovereign-declared; BASIS:codex+explicit-authorization",
            prohibition="FORBID:direct-mutation-by-permission-sovereign/unauthorized-directory-write",
        ),
        CodexArticle(
            id="A43",
            section="6",
            subject="hot-update-subject",
            rule="HOT-UPDATE-SUBJECT:governed-executable-code; GATE:versioned-frozen-boundary; AUTHORIZE:governance-authorization; EXCLUDE:codex/data/directory/manifest",
            prohibition="FORBID:hot-update-codex/data/directory/manifest",
        ),
        CodexArticle(
            id="A44",
            section="4",
            subject="four-functions-local",
            rule="FUNCTIONS:git/sql/semantic-index/rag/llm; FORMAL-TOOLS:postgresql,qdrant,git,rag; REALIZATION:local-python-typescript-cpp-c-csharp-sql-governed; UNAVAILABLE:declare-closed-not-replace; EMBEDDED-BROWSER:governed-in-app-browser-view-allowed-for-tool-network-search; EXTERNAL-AI-VIA-BROWSER:ai-collaboration-tool-under-governance",
            prohibition="FORBID:non-formal-external-service-substitution/binary-replacement; FORBID:embedded-browser-bypass-governance; FORBID:direct-external-ai-without-ai-collaboration-tool",
        ),
        CodexArticle(
            id="A45",
            section="4",
            subject="interface-layer",
            rule="INTERFACE-LAYER:presentation-of-decisions-state; AUTHORITY:no-decide-no-exec; STACK:python-typescript-cpp-c-csharp-sql-only; BIND:all-modules-and-all-execution",
            prohibition="FORBID:interface-decide/interface-exec/any-other-language-in-interface",
        ),
        CodexArticle(
            id="A46",
            section="2",
            subject="audit-ledger",
            rule="AUDIT:ledger-per-managed-action; WRITE:governed-executor; STORE:system-data-sub-sovereign-declared; RETENTION:by-governance-policy; READ:governance-authority+maintenance-analysis",
            prohibition="FORBID:audit-mutation/audit-suppression/unauthorized-audit-read",
        ),
        CodexArticle(
            id="A47",
            section="2",
            subject="violation-closure",
            rule="VIOLATION:stop-record-adjudicate; DETECT:maintenance-monitoring; STOP:deny-fail-closed; RECORD:fault-determination+audit-ledger; ADJUDICATE:governance-authority-final",
            prohibition="FORBID:violation-without-record/self-adjudication/continued-execution-after-violation",
        ),
        CodexArticle(
            id="A48",
            section="7",
            subject="amendment-execution",
            rule="AMENDMENT-EXEC:full-file-replacement; VERSION:system-auto-increment-decimal-5-fractional-digits; SIGN:deliberate-governor-act; RE-SEAL:immediate-after-replacement; READONLY:restore-before-any-load",
            prohibition="FORBID:partial-patch/version-skip/unsealed-load",
        ),
        CodexArticle(
            id="A49",
            section="4",
            subject="formal-tools",
            rule="FORMAL-TOOLS:postgresql,qdrant,git,rag,python,typescript,cpp,c,csharp,sql; OWNERSHIP:local-owned; GOVERNANCE:governed-by-codex; HOSTING:local-only; SUBSTITUTION:non-formal-forbidden; EMBEDDED-BROWSER:governed-in-app-browser-view-not-a-formal-tool-substitute-but-a-tool-network-access-channel",
            prohibition="FORBID:non-formal-substitution-of-postgresql/qdrant/git/rag/python/typescript/cpp/c/csharp/sql; FORBID:external-or-cloud-hosting-of-formal-tools; FORBID:embedded-browser-as-formal-tool-replacement",
        ),
        CodexArticle(
            id="A50",
            section="6",
            subject="programming-language-review",
            rule="OWNER:system-language-review-sub-sovereign; SCOPE:programming-language-conformance/acceptance/migration; EXEC:delegated; BASIS:codex-delegation",
            prohibition="FORBID:system-language-review-sub-sovereign-overstep-exec",
        ),
        CodexArticle(
            id="A51",
            section="6",
            subject="third-party-software-management",
            rule="OWNER:system-third-party-sub-sovereign; SCOPE:third-party-introduction/version/license/security; EXEC:delegated; BASIS:codex-delegation",
            prohibition="FORBID:system-third-party-sub-sovereign-overstep-exec",
        ),
        CodexArticle(
            id="A52",
            section="4",
            subject="rag-architecture",
            rule="RAG-ARCH:hybrid-rag+code-rag+agentic-rag+memory-rag; HYBRID-RAG:dense+sparse+semantic-fusion; CODE-RAG:code-snippet+ast+dependency-graph-retrieval; AGENTIC-RAG:multi-step-retrieve+reason+adapt; MEMORY-RAG:session+long-term+episodic-memory; SHARED-INDEX:qdrant(local-owned); OWNERSHIP:local-owned; HOSTING:local-only; BASIS:codex",
            prohibition="FORBID:non-formal-rag-substitution; FORBID:external-or-cloud-hosting-of-rag; FORBID:replace-hybrid-architecture; FORBID:omit-any-of-four-sub-architectures",
        ),
        CodexArticle(
            id="A53",
            section="4",
            subject="git-operation-tiers",
            rule="GIT-OPS-TIERS:3-level; TIER-1:read-only+high-frequency+direct-exec; TIER-1-OPS:status/log/diff/show/branch/remote/blame/ls-files/cat-file/rev-parse/describe/tag-l/for-each-ref/stash-list/config-get; TIER-2:general-write+requires-confirmation; TIER-2-OPS:add/commit/stash/branch-create/checkout/switch/merge/tag-create/fetch/push/rebase-local/cherry-pick/revert/worktree-add/worktree-remove; TIER-3:high-risk+strictly-restricted+requires-governance-authority-approval; TIER-3-OPS:push-force/push-force-with-lease/commit-amend-pushed/reset-hard/reset-soft-distant/branch-D/filter-branch/filter-repo/rebase-interactive/rebase-root/gc-prune/reflog-expire/update-ref-d/clean-fd/stash-drop/stash-clear; ENFORCEMENT:hook+governance-gate+audit-ledger; BASIS:codex+A46-audit",
            prohibition="FORBID:tier-3-without-governance-authority-approval; FORBID:tier-2-without-confirmation; FORBID:bypass-tier-enforcement",
        ),
        CodexArticle(
            id="A54",
            section="6",
            subject="xingcheng-model-modes",
            rule="XINGCHENG-MODEL:3-modes-inside-isolated-owned-domain; MAIN:comprehensive; CHAT:conversation+document+visual; PROGRAMMING:code-execution+generation+analysis; AUTHORITY:complete-own-domain; SYSTEM-CONNECTION:none",
            prohibition="FORBID:any-mode-access-or-affect-system; FORBID:system-use-xingcheng-as-system-component",
        ),
        CodexArticle(
            id="A55",
            section="4",
            subject="file-extension-conventions",
            rule="FILE-EXT:per-language-canonical; PYTHON:src=.py/stub=.pyi/test=test_*.py/pkg-init=__init__.py; TYPESCRIPT:logic=.ts/jsx-ui=.tsx/decl=.d.ts/test=.test.ts+.test.tsx; C:impl=.c/interface=.h/test=*_test.c; CPP:impl=.cpp/header=.hpp/template-inline=.inl(when-needed)/test=*_test.cpp; CSHARP:src=.cs/proj=.csproj/solution=.sln/test=*Tests.cs; SQL:general=.sql/migration=.sql; BOUNDARY:C-uses-.h+CPP-uses-.hpp-for-language-edge-visibility; FORBID:inventing-custom-extensions; BASIS:codex+A35-architecture-hybrid",
            prohibition="FORBID:custom-file-extensions; FORBID:cross-language-extension-mismatch; FORBID:mixing-.h-for-cpp-or-.hpp-for-c; FORBID:non-declared-extension",
        ),
        CodexArticle(
            id="A56",
            section="4",
            subject="test-framework-tiering",
            rule="TEST-TIER:unit-by-impl-language+integration-unified-python-pytest; UNIT-PYTHON:pytest; UNIT-CPP:googletest; UNIT-TYPESCRIPT:vitest; UNIT-CSHARP:xunit; UNIT-C:native-or-ctest; INTEGRATION:cross-module+cross-language-unified-via-python-pytest; SEPARATION:unit-tests-per-language+integration-tests-python-only; BASIS:codex+A35-architecture-hybrid+A37-code-origin",
            prohibition="FORBID:cross-language-unit-test-framework-mixing; FORBID:non-python-integration-tests; FORBID:non-formal-test-framework; FORBID:skip-integration-tier",
        ),
        CodexArticle(
            id="A57",
            section="6",
            subject="self-health-test-necessity",
            rule="SELF-HEALTH-TEST:necessary-for-self-maintenance-health; OWNER:maintenance-sovereign; SCOPE:governed-tools-declare-test-files; REQUIRE:collectable-offline+no-live-model-dependency; DETECT:governance-audit-collects-managed-test-files; BASIS:codex+A56-test-framework-tiering",
            prohibition="FORBID:delete-required-test-file; FORBID:test-depends-on-live-model/network; FORBID:uncollectable-test-file",
        ),
        CodexArticle(
            id="A58",
            section="4",
            subject="embedded-browser-governed-access",
            rule="EMBEDDED-BROWSER:in-app-browser-view-under-governance; OWNER:tool-owner-module; PURPOSE:network-search-and-ai-collaboration; AUTHORITY:governance-rule; NETWORK:embedded-browser-view-only; EXTERNAL-AI:via-ai-collaboration-tool-only; FORBID:direct-external-network-from-tool-core; FORBID:embedded-browser-as-formal-tool-substitute; BASIS:codex+A44+A37-exception",
            prohibition="FORBID:embedded-browser-bypass-governance; FORBID:external-ai-without-ai-collaboration-tool; FORBID:embedded-browser-replace-formal-tools; FORBID:tool-core-direct-network-access",
        ),
        CodexArticle(
            id="A59",
            section="6",
            subject="tool-level-service-ownership",
            rule="TOOL-LEVEL-SERVICE:tool-owner-under-governance; PLATFORM-SERVICE:system-sovereign-coordination; XINGCHENG:no-platform-role+isolated-own-domain-only; SEPARATION:tool-business-logic+platform-orchestration",
            prohibition="FORBID:tool-service-bypass-governance; FORBID:platform-service-replace-tool-business-logic; FORBID:tool-decision-without-governance-authority",
        ),
        CodexArticle(
            id="A60",
            section="4",
            subject="startup-entry-layer",
            rule="LAUNCHER:interface-presentation-only; FUNCTION:bring-up-official-gptbridge-ui; BOOT-OPERATIONS:none; SYSTEM-OPERATIONS:none",
            prohibition="FORBID:launcher-environment-check/governance-audit/dependency-start/governance-system-start/system-core/module-execution",
        ),
        CodexArticle(
            id="A61",
            section="4",
            subject="startup-core-layer",
            rule="BOOT-CORE:sole-startup-orchestrator; ORDER:environment-check>governance-audit>postgresql-start>qdrant-start>ollama-start>governance-system-start; GATE:verify-each-required-phase-before-next; OUTPUT:governed-runtime-ready",
            prohibition="FORBID:launcher-or-module-own-boot-phase; FORBID:governance-system-before-audit-and-required-dependencies; FORBID:module-execution-before-governance-ready; FORBID:boot-core-own-business-decision",
        ),
        CodexArticle(
            id="A62",
            section="4",
            subject="governance-authority-layer",
            rule="GOVERNANCE-AUTHORITY-LAYER:highest-operational-governance-below-codex; SCOPE:authenticate+authorize+audit+enforce-codex; PRECEDENCE:before-all-decisions-dispatch-and-execution",
            prohibition="FORBID:bypass-governance-authority; FORBID:governance-authority-own-business-execution",
        ),
        CodexArticle(
            id="A63",
            section="6",
            subject="sovereign-decision-layer",
            rule="SOVEREIGN-DECISION-LAYER:system-sovereign+maintenance-sovereign+permission-sovereign; FUNCTION:decision-only; OUTPUT:governed-decisions-to-corresponding-sub-sovereigns",
            prohibition="FORBID:sovereign-direct-execution; FORBID:cross-sovereign-duty-collision",
        ),
        CodexArticle(
            id="A64",
            section="6",
            subject="sub-sovereign-control-dispatch-layer",
            rule="SUB-SOVEREIGN-LAYER:system-sub-sovereigns+maintenance-sub-sovereigns+permission-sub-sovereigns; FUNCTION:execution-control+task-dispatch; AUTHORITY:parent-sovereign-decision+governance-authorization; EXECUTION:delegated-to-module-execution-layer",
            prohibition="FORBID:sub-sovereign-create-independent-policy; FORBID:sub-sovereign-replace-parent-sovereign; FORBID:ungoverned-dispatch",
        ),
        CodexArticle(
            id="A65",
            section="4",
            subject="cross-layer-information-layer",
            rule="INFORMATION-LAYER:cross-layer-peer-authority; AUTHORITY-RANK:equal-to-sovereign-layer; COMPONENTS:shared-layer+postgresql+qdrant+status+events+ipc; EXCLUSIVE-AUTHORITY:information-channel+transport+state+structured-data+semantic-index; CONTENT-OWNERSHIP:source-and-authorized-consumer; GOVERNANCE:directly-bound-by-codex+governance-authority",
            prohibition="FORBID:sovereign-or-sub-sovereign-override-information-layer-authority; FORBID:information-layer-own-sovereign-business-decision-or-module-execution; FORBID:component-duty-substitution; FORBID:alter-message-or-authoritative-data-outside-governed-operation",
        ),
        CodexArticle(
            id="A66",
            section="4",
            subject="module-execution-layer",
            rule="MODULE-EXECUTION-LAYER:local-model+model-dialogue+ai-assistant+investment-mobile+ai-collaboration+file-sorter+global-cleaner+vaultly+system-rescue; XINGCHENG:excluded-from-system-module-layer+isolated-owned-folder",
            prohibition="FORBID:module-self-governance; FORBID:module-cross-owner-execution; FORBID:system-access-xingcheng-folder; FORBID:xingcheng-access-system-modules",
        ),
        CodexArticle(
            id="A67",
            section="6",
            subject="frontend-backend-startup-sync-and-repair",
            rule="FRONTEND-BACKEND:startup-real-time-synchronized; READY:backend-runtime-ready+governance-ready+required-dependencies-ready+authenticated-ipc-connected; STATUS:events-propagated-immediately-to-all-active-ui; FAILURE:maintenance-sovereign-decides-auto-repair>maintenance-sub-sovereign-controls-and-dispatches>governed-executor-repairs>boot-core-revalidates>ui-resynchronizes; SCOPE:main-ui+all-independent-tool-ui",
            prohibition="FORBID:socket-open-alone-as-ready; FORBID:ui-connected-while-runtime-degraded-or-governance-unready; FORBID:stale-status/manual-refresh-dependency/duplicate-repair-owner/ungoverned-restart; FORBID:module-self-repair-system",
        ),
        CodexArticle(
            id="A68",
            section="4",
            subject="module-fine-grained-decomposition",
            rule="EACH-MODULE:decompose-into-owned-submodules; REQUIRED-LAYERS:presentation+channel-api+application-use-case+domain-business+service+repository-data-access+integration-adapter+execution-worker; EACH-SUBMODULE:single-responsibility+single-owner+explicit-input-output+declared-dependencies; SHARING:contracts-and-neutral-infrastructure-only; BUSINESS-LOGIC:remains-with-module-owner",
            prohibition="FORBID:monolithic-module+duplicate-responsibility+multiple-owners+implicit-dependency+cross-module-private-import+shared-layer-business-logic+presentation-direct-data-or-execution-access",
        ),
        CodexArticle(
            id="A69",
            section="4",
            subject="execution-layer-internal-tiering",
            rule="EXECUTION-LAYER-TIERS:dispatch-intake>authorization-and-governance-gate>task-planning>specialized-executor>result-verification>state-event-audit-publication; CONTROL:sub-sovereign; WORK:specialized-module-executor; VERIFY:independent-from-work-step; RESULT:returned-through-information-layer; FAILURE:maintenance-path-only",
            prohibition="FORBID:tier-skip+executor-self-authorize+executor-self-dispatch+work-step-self-verify+direct-ui-to-executor+cross-module-executor-substitution+unrecorded-result",
        ),
        CodexArticle(
            id="A70",
            section="4",
            subject="information-layer-exclusive-channel-gateway",
            rule="ALL-CHANNELS:owned-and-connected-exclusively-by-information-layer; SCOPE:launcher-ui+boot-core+governance-authority+sovereigns+sub-sovereigns+module-layers+execution-tiers+frontend-backend+independent-tools+shared-layer+postgresql+qdrant+status+events+ipc; ROUTE:sender>information-layer>authorized-destination; REQUIRE:authenticated+authorized+typed+observable+audited; DIRECT-CONNECTION:none",
            prohibition="FORBID:any-peer-to-peer/direct/cross-layer/cross-module/frontend-backend/sovereign-sub-sovereign/module-database/module-executor-bypass-channel; FORBID:private-side-channel+implicit-callback-channel+unregistered-bus+direct-socket+direct-database-link-outside-information-layer",
        ),
        CodexArticle(
            id="A71",
            section="4",
            subject="git-multi-worker-concurrent-work-and-commit",
            rule="GIT-CONCURRENCY:one-worker>one-worktree>one-branch; STORAGE:shared-git-object-database; ISOLATION:independent-working-tree+independent-index+independent-branch; READ-CROSS-BRANCH:git-show+git-diff+git-log; WRITE-SCOPE:worker-owned-worktree-only; COMMIT:worktree-local-lock+own-changes-only+governance-audit-before-commit; INTEGRATION:designated-integrator-only-via-merge-or-cherry-pick; AUTO-COMMIT:commit-only+never-push; CONFLICT:resolve-on-integration-branch+preserve-source-branches",
            prohibition="FORBID:multiple-workers-same-worktree+cross-worktree-stage+cross-worker-commit+shared-index+direct-write-other-branch+automatic-push+force-overwrite-source-branch+conflict-resolution-by-source-destruction",
        ),
        CodexArticle(
            id="A72",
            section="6",
            subject="maintenance-sovereign-exclusive-repair-decision-chain",
            rule="ALL-SYSTEM-REPAIR:maintenance-sovereign-exclusive-decision; CHAIN:signal>information-layer>maintenance-sovereign-fault-determination+repair-decision>maintenance-sub-sovereign-control-dispatch>permission-validation>governed-executor>independent-verification>information-layer-status-event-audit>ui-sync; DECISION-PROOF:required-before-repair-mutation; BOOT-CORE+WATCHDOG+UI+MODULE:signal-and-request-only",
            prohibition="FORBID:any-direct-or-parallel-repair-path; FORBID:boot-core/watchdog/ui/module/repair-service-mutation-without-maintenance-sovereign-decision-proof; FORBID:transport-only-repair-trigger+duplicate-owner+implicit-authorization+unverified-recovery+status-before-verification",
        ),
    ),
    edicts=(
        CodexEdict(
            id="E1",
            area="sovereignty",
            edict="CODEX:highest-rule-layer; ALL-DECISIONS:reference-codex",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E2",
            area="execution",
            edict="EXECUTION:delegate-governed-executor; DIRECT-EXEC:sovereign+codex-none",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E3",
            area="amendment",
            edict="AMENDMENT:full-versioned-replacement-only; FORBID:incremental/runtime-amendment",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E4",
            area="permission",
            edict="OWNER:permission-sovereign; SCOPE:all-permission-matters; ACTIONS:manage-issue-terminate-supervise; PERM-ID:sovereign-managed; EXEC:none; BASIS:codex",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E5",
            area="xingcheng",
            edict="XINGCHENG:local-native-model; AUTHORITY:complete-inside-owned-domain; ISOLATION:physical+logical-from-system; SYSTEM-AUTHORITY:none; SYSTEM-PARTICIPATION:forbidden",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E14",
            area="xingcheng-power",
            edict="XINGCHENG-POWER:complete-inside-owned-domain; SYSTEM-POWER:none; FORBID:system-observe/coordinate/decide/authorize/execute/override/access",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E6",
            area="hot-update",
            edict="HOT-UPDATE:version-gated-frozen-boundary; APPLY:governance-authorization; BASIS:codex+governance-authorization",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E7",
            area="runtime",
            edict="OWNER:system-runtime-sub-sovereign; SCOPE:run+service-maintenance; INCLUDE:process-survival+runtime-integrity; BASIS:codex-delegation",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E8",
            area="maintenance",
            edict="OWNER:maintenance-sovereign; DUTIES:update/system-health-monitor(incl-data-integrity)/auto-repair/fault-determine/backup; BASIS:codex+governance-authorization",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E9",
            area="separation",
            edict="POWERS:decision/execution/governance-separate; SYSTEM-FOUR:no-mutual-replacement",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E10",
            area="security",
            edict="AUTHZ:deny-by-default+explicit-allow; FAIL:close; NO:internal-disclosure",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E11",
            area="supremacy",
            edict="INTERPRETER:codex-itself; CONFLICT:preempt-subordinate-authority+amendment-path",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E12",
            area="storage",
            edict="STORAGE:independent+os-readonly; SOURCE:sole-authority",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E13",
            area="xingcheng-thinking",
            edict="XINGCHENG:independent-inside-owned-isolated-domain; SYSTEM-DECISION-SOURCE:none; SYSTEM-CONNECTION:none",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E15",
            area="system-sovereign",
            edict="SYSTEM-SOVEREIGN:top-orchestrator; SCOPE:orchestrate+integrate; NO-DECIDE:sub-details; DELEGATE:sub-sovereigns; EXEC:no-powerful-work",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E16",
            area="governance",
            edict="GOVERNANCE:codex+governance-authority; ROLE:highest-rule-layer-maintenance; DUTY:guard-immutable+integrity",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E17",
            area="resource",
            edict="OWNER:system-resource-sub-sovereign; SCOPE:state-monitor-provide-delegate-release; EXEC:none",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E18",
            area="data",
            edict="OWNER:system-data-sub-sovereign; SCOPE:access-spec-consistency-integrity-check+data-directory; EXEC:none",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E19",
            area="integration",
            edict="OWNER:system-integration-sub-sovereign; SCOPE:structural-interface/channel/sync/bus; EXEC:none; NO:decision-layer-coordinate",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E20",
            area="boundary",
            edict="OWNERSHIPS:exclusive-and-independent; DATA-INTEGRITY=system-data-sub-sovereign; HEALTH=maintenance-sovereign; STRUCTURAL-INTERFACE=system-integration-sub-sovereign; PLATFORM-DECISION-COORDINATE=system-sovereign; XINGCHENG=outside-system",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E21",
            area="architecture",
            edict="STACK:python+typescript+cpp+c+csharp+sql-hybrid; LANGUAGES:only-python-typescript-cpp-c-csharp-sql; FORMAL-TOOLS:postgresql,qdrant,git,rag; FORBID:any-other-language",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E22",
            area="codex-reference",
            edict="CHINESE-CODEX:backup-only; CITATION/BASIS:none; DECISION-BASIS:authoritative-codex-only",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E23",
            area="code-origin",
            edict="CODE-ORIGIN:local-owned; STACK:python+typescript+cpp+c+csharp+sql-hybrid-only; FORMAL-TOOLS:postgresql,qdrant,git,rag; FORBID:non-formal-third-party/package/external-service; EMBEDDED-BROWSER-EXCEPTION:governed-in-app-browser-view-for-tool-network-search",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E24",
            area="codex-language",
            edict="CODEX-FORM:programming-language; CHINESE:reserved-to-chinese-codex; BASIS:backup-only-none",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E25",
            area="actors",
            edict="ACTORS:human-operator/governed-app/sovereign/xingcheng; REQUESTER:verified-declared-class",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E26",
            area="bootstrap",
            edict="BOOTSTRAP:initial-allowlist-by-governance-authority-one-time; AFTER:directory-driven-only",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E27",
            area="governor-seal",
            edict="GOVERNOR:human-operator; SEAL:versioned-digest-manifest; VERIFY:before-load",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E28",
            area="directory-write",
            edict="DIRECTORY-WRITE:permission-sovereign-decision+governed-executor; STORE:system-data-sub-sovereign-declared",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E29",
            area="hot-update-subject",
            edict="HOT-UPDATE:governed-executable-code-only; EXCLUDE:codex/data/directory/manifest",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E30",
            area="four-functions-local",
            edict="FUNCTIONS:git/sql/semantic-index/rag/llm; FORMAL-TOOLS:postgresql,qdrant,git,rag; LOCAL:python-typescript-cpp-c-csharp-sql-governed; UNAVAILABLE:declare-closed; EMBEDDED-BROWSER:governed-tool-network-access-channel-not-formal-tool-substitute",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E31",
            area="interface-layer",
            edict="INTERFACE-LAYER:presentation-only; NO-DECIDE:true; STACK:python-typescript-cpp-c-csharp-sql-only; BIND:all-modules",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E32",
            area="audit-ledger",
            edict="AUDIT:mandatory-ledger; ACCESS:governed; RETENTION:by-policy",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E33",
            area="violation-closure",
            edict="VIOLATION:stop-record-adjudicate; ADJUDICATE:governance-authority-final",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E34",
            area="amendment-execution",
            edict="AMENDMENT-EXEC:full-replacement; VERSION:system-auto-increment-decimal-5-fractional-digits; SEAL:re-seal; READONLY:restore",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E35",
            area="formal-tools",
            edict="FORMAL-TOOLS:postgresql,qdrant,git,rag,python,typescript,cpp,c,csharp,sql; OWNERSHIP:local-owned; GOVERNANCE:governed-by-codex; HOSTING:local-only; SUBSTITUTION:non-formal-forbidden; EMBEDDED-BROWSER:tool-network-access-channel-not-formal-tool-replacement",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E36",
            area="programming-language-review",
            edict="OWNER:system-language-review-sub-sovereign; SCOPE:conformance-acceptance-migration; EXEC:delegated",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E37",
            area="third-party-software-management",
            edict="OWNER:system-third-party-sub-sovereign; SCOPE:introduction-version-license-security; EXEC:delegated",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E38",
            area="rag-architecture",
            edict="RAG-ARCH:hybrid-rag+code-rag+agentic-rag+memory-rag; SHARED-INDEX:qdrant(local-owned); OWNERSHIP:local-owned; HOSTING:local-only; FORBID:non-formal-substitution/external-hosting/omit-sub-architecture",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E39",
            area="git-operation-tiers",
            edict="GIT-OPS-TIERS:3-level; TIER-1:read-only+direct-exec; TIER-2:write+requires-confirmation; TIER-3:high-risk+strictly-restricted+governance-authority-approval; ENFORCEMENT:hook+gate+audit-ledger",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E40",
            area="xingcheng-model-modes",
            edict="XINGCHENG-MODEL:3-modes-inside-isolated-owned-domain; AUTHORITY:complete-own-domain; SYSTEM-CONNECTION:none; FORBID:any-mode-access-or-affect-system",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E41",
            area="file-extension-conventions",
            edict="FILE-EXT:per-language-canonical; PYTHON:.py/.pyi/test_*.py/__init__.py; TYPESCRIPT:.ts/.tsx/.d.ts/.test.ts/.test.tsx; C:.c/.h/*_test.c; CPP:.cpp/.hpp/.inl/*_test.cpp; CSHARP:.cs/.csproj/.sln/*Tests.cs; SQL:.sql; C=.h+CPP=.hpp-for-edge-visibility; FORBID:custom-extensions+cross-language-mismatch",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E42",
            area="test-framework-tiering",
            edict="TEST-TIER:unit-per-language+integration-unified-python-pytest; UNIT:python=pytest/cpp=googletest/typescript=vitest/csharp=xunit/c=native-or-ctest; INTEGRATION:cross-module+cross-language=python-pytest-only; FORBID:cross-language-unit-mixing+non-python-integration+non-formal-framework",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E43",
            area="self-health-test",
            edict="SELF-HEALTH-TEST:necessary-for-self-maintenance-health; OWNER:maintenance-sovereign; GOVERNED-TOOLS:declare-required-test-files; REQUIRE:offline-collectable; DETECT:governance-audit-collects-managed-test-files; FORBID:delete-required-test-file+live-model-test-dependency",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E44",
            area="embedded-browser-governed-access",
            edict="EMBEDDED-BROWSER:in-app-browser-view-under-governance; OWNER:tool-owner-module; PURPOSE:network-search+ai-collaboration; NETWORK:embedded-browser-view-only; EXTERNAL-AI:via-ai-collaboration-tool-only; FORBID:tool-core-direct-network+embedded-browser-as-formal-tool-substitute",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E45",
            area="tool-level-service-ownership",
            edict="TOOL-LEVEL-SERVICE:tool-owner-under-governance; PLATFORM-SERVICE:system-sovereign-coordination; XINGCHENG:no-platform-role+isolated-own-domain-only",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E46",
            area="gptbridge-layered-architecture",
            edict="LAYERS:startup-entry(ui-only)>startup-core(governance+system-core)>governance-authority>{sovereign-decision(system+maintenance+permission)||cross-layer-information-peer-authority(shared-layer+postgresql+qdrant+status+events+ipc)}>sub-sovereign-control-dispatch(system+maintenance+permission)>module-execution(local-model-xingcheng-folder+model-dialogue+ai-assistant+investment-mobile+ai-collaboration+file-sorter+global-cleaner+vaultly+system-rescue); INFORMATION-AUTHORITY-RANK:equal-to-sovereign-layer; FORBID:mutual-override+layer-collapse+bypass+duty-substitution",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E47",
            area="startup-boundary",
            edict="LAUNCHER:interface-only; BOOT-CORE:environment-check>governance-audit>postgresql>qdrant>ollama>governance-system; GATE:verify-each-required-phase; FORBID:launcher-boot-operations+boot-duty-duplication+bypass+out-of-order-start",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E48",
            area="frontend-backend-live-sync-auto-repair",
            edict="STARTUP-SYNC:frontend+backend real-time; READY:runtime+governance+dependencies+authenticated-ipc; STATUS:immediate-to-main-ui+independent-tools; AUTO-REPAIR:maintenance-sovereign-decision>maintenance-sub-sovereign-control-dispatch>governed-executor>boot-core-revalidation>ui-resync; FORBID:false-ready+stale-state+duplicate-owner+ungoverned-restart",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E49",
            area="fine-grained-module-and-execution-tiering",
            edict="MODULE-LAYERS:presentation>channel-api>application-use-case>domain-business>service>repository-data-access>integration-adapter>execution-worker; EXECUTION-TIERS:dispatch>authorization-governance>planning>specialized-execution>independent-verification>state-event-audit-publication; REQUIRE:single-responsibility+single-owner+explicit-contract; FORBID:monolith+duplicate-duty+tier-skip+self-authorization+self-verification+direct-ui-execution",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E50",
            area="information-layer-exclusive-channels",
            edict="CHANNEL-AUTHORITY:information-layer-only; ALL-COMMUNICATION:sender>information-layer>authorized-destination; COVER:all-system-layers+all-modules+all-execution-tiers+frontend-backend+data-and-event-channels; REQUIRE:authenticated+authorized+typed+observable+audited; FORBID:any-direct-link+bypass+private-side-channel+unregistered-bus",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E51",
            area="git-multi-worker-concurrency",
            edict="WORKER-ISOLATION:one-worker-one-worktree-one-branch; SHARED:git-object-database-only; CROSS-READ:show+diff+log; COMMIT:owned-worktree+local-lock+own-changes+precommit-governance-audit; INTEGRATION:designated-integrator-merge-or-cherry-pick; AUTO-COMMIT:no-push; CONFLICT:integration-branch-only+preserve-source; FORBID:same-worktree-multi-worker+shared-index+cross-commit+force-overwrite",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E52",
            area="maintenance-exclusive-repair-chain",
            edict="REPAIR-DECISION:maintenance-sovereign-only; CHAIN:signal>information-layer>fault-determination+decision>maintenance-sub-sovereign-dispatch>permission-validation>governed-execution>independent-verification>audited-status>ui-sync; BOOT-CORE+WATCHDOG+UI+MODULE:request-only; FORBID:direct-or-parallel-repair+mutation-without-decision-proof",
            immutability="immutable-sealed",
        ),
    ),
    savings=CodexSavings(
        mutability="immutable-sealed",
        function="none",
        amendment="explicit-versioned-full-replacement-only",
        overriding_authority="governance-codex-supreme",
        interpretation="governance-codex-is-the-only-interpreter-of-its-own-canon",
        conflict_resolution="codex-preempts-all-subordinate-authority",
    ),
    sovereigns=SOVEREIGNS,
)


from .chinese import GOVERNANCE_CODEX_CHINESE

__all__ = [
    "GOVERNANCE_CODEX",
    "GOVERNANCE_CODEX_CHINESE",
    "CodexArticle",
    "CodexEdict",
    "CodexPreamble",
    "CodexPrinciple",
    "CodexSavings",
    "CodexSection",
    "CodexSovereign",
    "GovernanceCodex",
]
