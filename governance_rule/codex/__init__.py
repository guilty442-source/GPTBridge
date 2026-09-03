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
    codex_version: int
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
    codex_version=3,
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
            statement="FORM:pure-declaration; EXEC:none; DELEGATE:governed-executor",
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
            statement="SYSTEMS:git/postgresql/qdrant/rag/llm; PRINCIPLE:separation; FORMAL-TOOLS:postgresql,qdrant,git,rag,python,cpp; FORBID:mutual-replacement",
            binding=True,
        ),
        CodexPrinciple(
            id="P8",
            statement="DECISION-SOURCE:codex; APPLIES:all-sovereigns-and-xingcheng; FORBID:embedded-own-source",
            binding=True,
        ),
        CodexPrinciple(
            id="P9",
            statement="AUTHORITY:supreme-immutable; FORBID:subordinate-override-or-amendment-path",
            binding=True,
        ),
        CodexPrinciple(
            id="P10",
            statement="XINGCHENG:local-native-model; RANK:peer-of-system-sovereign; EXEC:none; THINKING:codex-referenced",
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
            statement="RESOURCE-SOVEREIGN:own-resource-matters; SCOPE:state-monitor-provision-delegate-release; EXEC:none",
            binding=True,
        ),
        CodexPrinciple(
            id="P14",
            statement="DATA-SOVEREIGN:own-data-matters; SCOPE:access-spec-consistency-integrity-check; EXEC:none",
            binding=True,
        ),
        CodexPrinciple(
            id="P15",
            statement="INTEGRATION-SOVEREIGN:own-cross-sovereign-module-interface; SCOPE:coordinate-sync-bus; EXEC:none",
            binding=True,
        ),
        CodexPrinciple(
            id="P16",
            statement="STACK:python+cpp-hybrid; MANDATORY:true; LANGUAGES:only-python-cpp; FORMAL-TOOLS:postgresql,qdrant,git,rag",
            binding=True,
        ),
        CodexPrinciple(
            id="P17",
            statement="CHINESE-CODEX:backup-only; CITATION/BASIS:none; DECISION-BASIS:authoritative-codex-only",
            binding=True,
        ),
        CodexPrinciple(
            id="P18",
            statement="CODE-ORIGIN:local-only; STACK:python+cpp; FORMAL-TOOLS:postgresql,qdrant,git,rag; FORBID:non-formal-third-party/package/external-service/non-local-env; EXEC:local-owned-code",
            binding=True,
        ),
        CodexPrinciple(
            id="P19",
            statement="CODEX-LANGUAGE:programming-language; CHINESE:reserved-to-chinese-codex; BASIS:backup-only-none",
            binding=True,
        ),
        CodexPrinciple(
            id="P20",
            statement="INTERFACE-LAYER:presentation-of-decisions-state; AUTHORITY:no-decide-no-exec; STACK:python-cpp-only; BIND:all-modules-and-all-execution",
            binding=True,
        ),
        CodexPrinciple(
            id="P21",
            statement="AUDIT:mandatory-ledger; WRITE:governed-executor; OWN:data-sovereign; RETENTION:by-governance-policy; READ:governance-authority+maintenance-analysis",
            binding=True,
        ),
        CodexPrinciple(
            id="P22",
            statement="VIOLATION:stop-record-adjudicate; STOP:deny-fail-closed; ADJUDICATE:governance-authority-final; FORBID:continued-execution-on-violation",
            binding=True,
        ),
        CodexPrinciple(
            id="P23",
            statement="FORMAL-TOOLS:postgresql,qdrant,git,rag,python,cpp; GOVERNANCE:governed-by-codex; FORBID:non-formal-substitution",
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
            rule="SURFACE:no-executable-function/no-callable-interface",
            prohibition="FORBID:define-function-or-executable-component-in-codex",
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
            rule="GIT:version+history; SQL:structured-formal-data; ENGINE:postgresql; QDRANT:semantic-index; RAG:retrieval-augmented-generation; LLM:understand-reason-operate; FORMAL-TOOLS:postgresql,qdrant,git,rag,python,cpp",
            prohibition="FORBID:four-mutual-replacement; FORBID:non-formal-substitution",
        ),
        CodexArticle(
            id="A9",
            section="4",
            subject="management-owner",
            rule="MANAGEMENT:xingcheng-core; MODE:read-only-under-codex",
            prohibition="FORBID:management-exec-or-formal-write",
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
            rule="DECISION-SOURCE:codex; APPLIES:runtime/maintenance/permission-sovereigns+xingcheng",
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
            rule="XINGCHENG:local-native-model; RANK:peer-of-system-sovereign; THINKING:codex-basis",
            prohibition="FORBID:xingcheng-exec-or-hold-system-exec",
        ),
        CodexArticle(
            id="A19",
            section="6",
            subject="xingcheng-thinking",
            rule="XINGCHENG:decision+management-thinking-reference-codex; SOURCE:no-embedded",
            prohibition="FORBID:xingcheng-self-reason-override-codex",
        ),
        CodexArticle(
            id="A20",
            section="6",
            subject="xingcheng-power",
            rule="XINGCHENG-POWER:observe/analyze/reason/advise/coordinate/explain",
            prohibition="FORBID:xingcheng-any-power-not-listed",
        ),
        CodexArticle(
            id="A21",
            section="6",
            subject="xingcheng-no-power",
            rule="XINGCHENG:no-direct-exec/no-grant/no-override",
            prohibition="FORBID:direct-exec/authorize-other-exec/override-any-decision-or-state",
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
            rule="OWNER:runtime-sovereign; SCOPE:run+service-maintenance; INCLUDE:process-survival+runtime-integrity; BASIS:codex-delegation",
            prohibition="FORBID:runtime-sovereign-overstep-exec/codex",
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
            rule="OWNER:resource-sovereign; SCOPE:all-resource-body-matters; ITEMS:memory/disk/model/compute; FUNC:state-monitor-provide-delegate-release; BASIS:codex",
            prohibition="FORBID:resource-sovereign-overstep-exec/data/permission",
        ),
        CodexArticle(
            id="A31",
            section="6",
            subject="data",
            rule="OWNER:data-sovereign; SCOPE:all-data-body-matters; ITEMS:structured/semantic-index/version-history; FUNC:access-spec-consistency-integrity-check+data-directory; BASIS:codex",
            prohibition="FORBID:data-sovereign-overstep-exec/resource/permission",
        ),
        CodexArticle(
            id="A32",
            section="6",
            subject="integration",
            rule="OWNER:integration-sovereign; SCOPE:cross-sovereign-module-structural-interface/channel/sync/bus; BASIS:codex-delegation",
            prohibition="FORBID:integration-sovereign-decision-layer-coordinate/overstep-exec",
        ),
        CodexArticle(
            id="A33",
            section="6",
            subject="boundary-data-integrity",
            rule="DATA-INTEGRITY-CHECK:data-sovereign; SYSTEM-HEALTH-MONITOR:maintenance-sovereign; PRESENT:data-integrity-health-only",
            prohibition="FORBID:maintenance-proxy-data-integrity-check; FORBID:data-sovereign-proxy-health-monitor",
        ),
        CodexArticle(
            id="A34",
            section="6",
            subject="boundary-integration-xingcheng",
            rule="INTEGRATION:structural-interface+sync; XINGCHENG:decision-layer-intelligent-coordinate+advice",
            prohibition="FORBID:integration-proxy-xingcheng-decision; FORBID:xingcheng-proxy-integration-structural-interface",
        ),
        CodexArticle(
            id="A35",
            section="4",
            subject="architecture-hybrid",
            rule="STACK:python+cpp-hybrid; PYTHON:upper-orchestrate+governed-logic; CPP:core-compute+native-perf; SOLE:allowed-code-architecture",
            prohibition="FORBID:any-programming-language-except-python-cpp; FORBID:replace-hybrid-architecture",
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
            rule="CODE-ORIGIN:local-owned; STACK:python+cpp-hybrid-only; FORMAL-TOOLS:postgresql,qdrant,git,rag,python,cpp; FORBID-deps:non-formal-third-party/package/external-service/non-local-env",
            prohibition="FORBID:reference/install/import/exec-any-non-formal-third-party-package/external-service/non-local-env/binary",
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
            rule="DIRECTORY-WRITE:permission-sovereign-decision; EXEC:governed-executor; STORE:data-sovereign-declared; BASIS:codex+explicit-authorization",
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
            rule="FUNCTIONS:git/sql/semantic-index/rag/llm; FORMAL-TOOLS:postgresql,qdrant,git,rag; REALIZATION:local-python-cpp-governed; UNAVAILABLE:declare-closed-not-replace",
            prohibition="FORBID:non-formal-external-service-substitution/binary-replacement",
        ),
        CodexArticle(
            id="A45",
            section="4",
            subject="interface-layer",
            rule="INTERFACE-LAYER:presentation-of-decisions-state; AUTHORITY:no-decide-no-exec; STACK:python-cpp-only; BIND:all-modules-and-all-execution",
            prohibition="FORBID:interface-decide/interface-exec/any-other-language-in-interface",
        ),
        CodexArticle(
            id="A46",
            section="2",
            subject="audit-ledger",
            rule="AUDIT:ledger-per-managed-action; WRITE:governed-executor; STORE:data-sovereign-declared; RETENTION:by-governance-policy; READ:governance-authority+maintenance-analysis",
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
            rule="AMENDMENT-EXEC:full-file-replacement; VERSION:system-auto-increment; SIGN:deliberate-governor-act; RE-SEAL:immediate-after-replacement; READONLY:restore-before-any-load",
            prohibition="FORBID:partial-patch/version-skip/unsealed-load",
        ),
        CodexArticle(
            id="A49",
            section="4",
            subject="formal-tools",
            rule="FORMAL-TOOLS:postgresql,qdrant,git,rag,python,cpp; GOVERNANCE:governed-by-codex; LOCAL:true; SUBSTITUTION:non-formal-forbidden",
            prohibition="FORBID:non-formal-substitution-of-postgresql/qdrant/git/rag/python/cpp",
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
            edict="XINGCHENG:local-native-model; RANK:peer-of-system-sovereign; EXEC:none; THINKING+MANAGEMENT:codex-referenced",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E14",
            area="xingcheng-power",
            edict="XINGCHENG-POWER:observe/analyze/reason/advise/coordinate/explain; FORBID:direct-exec/grant/override",
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
            edict="OWNER:runtime-sovereign; SCOPE:run+service-maintenance; INCLUDE:process-survival+runtime-integrity; BASIS:codex-delegation",
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
            edict="XINGCHENG:local-native-model; RANK:peer-of-system-sovereign; EXEC:none; THINKING+MANAGEMENT:reference-codex",
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
            edict="OWNER:resource-sovereign; SCOPE:state-monitor-provide-delegate-release; EXEC:none",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E18",
            area="data",
            edict="OWNER:data-sovereign; SCOPE:access-spec-consistency-integrity-check+data-directory; EXEC:none",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E19",
            area="integration",
            edict="OWNER:integration-sovereign; SCOPE:structural-interface/channel/sync/bus; EXEC:none; NO:decision-layer-coordinate",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E20",
            area="boundary",
            edict="OWNERSHIPS:exclusive-and-independent; DATA-INTEGRITY=data-sovereign; HEALTH=maintenance-sovereign; STRUCTURAL-INTERFACE=integration-sovereign; DECISION-COORDINATE=xingcheng",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E21",
            area="architecture",
            edict="STACK:python+cpp-hybrid; LANGUAGES:only-python-cpp; FORMAL-TOOLS:postgresql,qdrant,git,rag; FORBID:any-other-language",
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
            edict="CODE-ORIGIN:local-owned; STACK:python+cpp-hybrid-only; FORMAL-TOOLS:postgresql,qdrant,git,rag; FORBID:non-formal-third-party/package/external-service",
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
            edict="DIRECTORY-WRITE:permission-sovereign-decision+governed-executor; STORE:data-sovereign-declared",
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
            edict="FUNCTIONS:git/sql/semantic-index/rag/llm; FORMAL-TOOLS:postgresql,qdrant,git,rag; LOCAL:python-cpp-governed; UNAVAILABLE:declare-closed",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E31",
            area="interface-layer",
            edict="INTERFACE-LAYER:presentation-only; NO-DECIDE:true; STACK:python-cpp-only; BIND:all-modules",
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
            edict="AMENDMENT-EXEC:full-replacement; VERSION:system-auto-increment; SEAL:re-seal; READONLY:restore",
            immutability="immutable-sealed",
        ),
        CodexEdict(
            id="E35",
            area="formal-tools",
            edict="FORMAL-TOOLS:postgresql,qdrant,git,rag,python,cpp; GOVERNANCE:governed-by-codex; LOCAL:true; SUBSTITUTION:non-formal-forbidden",
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
