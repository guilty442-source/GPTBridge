$ErrorActionPreference = 'Stop'
$rows = Get-Content _articles.json -Encoding UTF8 -Raw | ConvertFrom-Json

# Official mapping: sovereign_hierarchy_registry parent_identity + A89 PARENT-MAP + A124/A125/A96
# X-sub-sovereign -> core sovereign (or 星澄 for learning/language/programming domains)

$X = [char]0x661F + [char]0x6F84  # 星澄

$edits = [ordered]@{}

# ---------- mechanical duty-delegation swaps ----------
$edits['A13'] = @(
  @{ f='rule'; find='OWNER:channel-contract-sync-sub-sovereign'; rep='OWNER:automation-sovereign' },
  @{ f='prohibition'; find='FORBID:channel-contract-sync-sub-sovereign-decision-layer-coordinate/overstep-exec'; rep='FORBID:automation-sovereign-decision-layer-coordinate/overstep-exec' }
)
$edits['A31'] = @(
  @{ f='rule'; find='OWNER:learning-evidence-sync-sub-sovereign'; rep="OWNER:$X" }
)
$edits['A42'] = @(
  @{ f='rule'; find='STARTUP:startup-sub-sovereign'; rep='STARTUP:system-runtime-sovereign' }
)
$edits['A59'] = @(
  @{ f='rule'; find='STORAGE:data-governance-sub-sovereign manages data integrity and storage modules'; rep='STORAGE:decision-sovereign manages data integrity and storage modules' }
)
$edits['A63'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns-domain decisions with channel-contract-sync-sub-sovereign managing channel modules'; rep='OWNER:automation-sovereign owns-domain decisions and manages channel modules' }
)
$edits['A64'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns-domain decisions with dependency-sync-sub-sovereign managing third-party dependency modules'; rep='OWNER:automation-sovereign owns-domain decisions and manages third-party dependency modules' }
)
$edits['A66'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns-domain decisions with learning-evidence-sync-sub-sovereign managing learning modules'; rep="OWNER:$X owns learning-domain decisions and manages learning modules" }
)
$edits['A67'] = @(
  @{ f='rule'; find='OWNER:decision-sovereign owns-domain decisions with health-maintenance-test-sub-sovereign managing health/maintenance modules'; rep='OWNER:decision-sovereign owns-domain decisions and manages health/maintenance modules' }
)
$edits['A68'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns domain decisions and resource-dependency-sync-sub-sovereign manages resource modules'; rep='OWNER:automation-sovereign owns domain decisions and manages resource modules' }
)
$edits['A69'] = @(
  @{ f='rule'; find='OWNER:decision-sovereign owns domain decisions and health-maintenance-test-sub-sovereign manages health/maintenance modules'; rep='OWNER:decision-sovereign owns domain decisions and manages health/maintenance modules' }
)
$edits['A77'] = @(
  @{ f='rule'; find='sync decision>system-sub-sovereign module assignment and coordination'; rep='sync decision>automation-sovereign module assignment and coordination' }
)
$edits['A87'] = @(
  @{ f='rule'; find='DIRECTORY-MODULE-MANAGEMENT:directory-sub-sovereign under one decision'; rep='DIRECTORY-MODULE-MANAGEMENT:permission-sovereign manages directory modules under decision-sovereign direction' }
)
$edits['A99'] = @(
  @{ f='rule'; find='SYNCHRONIZATION-SOVEREIGN-OWNS:'; rep='AUTOMATION-SOVEREIGN-OWNS:' },
  @{ f='rule'; find='MANAGEMENT:permission-side sub-sovereigns manage permission/directory/identity modules while channel-contract-sync-sub-sovereign manages channel operation/maintenance modules'; rep='MANAGEMENT:permission-sovereign manages permission/directory/identity modules while automation-sovereign manages channel operation/maintenance modules' },
  @{ f='rule'; find='synchronization channel-ready verdict'; rep='automation channel-ready verdict' },
  @{ f='rule'; find='synchronization sovereign cannot authorize permissions'; rep='automation sovereign cannot authorize permissions' }
)
$edits['B2'] = @(
  @{ f='rule'; find='OWNER:system-runtime-sub-sovereign'; rep='OWNER:system-runtime-sovereign' },
  @{ f='prohibition'; find='FORBID:system-runtime-sub-sovereign-overstep-exec/codex'; rep='FORBID:system-runtime-sovereign-self-authorization-or-policy-modification' }
)
$edits['B22'] = @(
  @{ f='rule'; find='release-update-sync-sub-sovereign-dispatch'; rep='automation-sovereign-dispatch' }
)
$edits['B26'] = @(
  @{ f='rule'; find='handoff-acknowledged=>startup-sub-sovereign-observe-status-only+no-runtime-control'; rep='handoff-acknowledged=>system-runtime-sovereign-startup-phase-observe-status-only+no-runtime-control' }
)
$edits['B28'] = @(
  @{ f='rule'; find='HANDOFF:startup-sub-sovereign>information-layer>runtime-sovereign'; rep='HANDOFF:system-runtime-sovereign(startup-phase)>information-layer>runtime-sovereign' },
  @{ f='rule'; find='BEFORE-ACK:startup-sub-sovereign owns-startup-generation only'; rep='BEFORE-ACK:system-runtime-sovereign(startup-phase) owns-startup-generation only' },
  @{ f='rule'; find='and startup-sub-sovereign cannot issue runtime commands'; rep='and the startup phase cannot issue runtime commands' }
)
$edits['B44'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns-domain decisions with resource-dependency-sync-sub-sovereign managing resource modules'; rep='OWNER:automation-sovereign owns-domain decisions and manages resource modules' }
)
$edits['B46'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns channel-topology/delivery-health decisions with channel-contract-sync-sub-sovereign managing channel modules'; rep='OWNER:automation-sovereign owns channel-topology/delivery-health decisions and manages channel modules' }
)
$edits['B48'] = @(
  @{ f='rule'; find='release-update-sync-sub-sovereign reviews change'; rep='automation-sovereign reviews change' }
)
$edits['B65'] = @(
  @{ f='rule'; find='MODULE-FLOW:runtime decisions route to system-sub-sovereign for module management+assignment+coordination and actual operations occur only in module execution layer'; rep='MODULE-FLOW:runtime decisions are exercised through runtime-sovereign module management+assignment+coordination and actual operations occur only in module execution layer' }
)
$edits['B111'] = @(
  @{ f='rule'; find='>startup-sub-sovereign management>'; rep='>system-runtime-sovereign management>' }
)
$edits['C5'] = @(
  @{ f='rule'; find='OWNER:health-maintenance-test-sub-sovereign'; rep='OWNER:decision-sovereign' }
)
$edits['C6'] = @(
  @{ f='rule'; find='OWNER:data-governance-sub-sovereign'; rep='OWNER:decision-sovereign' },
  @{ f='prohibition'; find='FORBID:data-governance-sub-sovereign-overstep-exec/resource/permission'; rep='FORBID:decision-sovereign-overstep-exec/resource/permission' }
)
$edits['C7'] = @(
  @{ f='rule'; find='DATA-INTEGRITY-CHECK:data-governance-sub-sovereign; SYSTEM-HEALTH-MONITOR:health-maintenance-test-sub-sovereign'; rep='DATA-INTEGRITY-CHECK:decision-sovereign; SYSTEM-HEALTH-MONITOR:decision-sovereign' },
  @{ f='prohibition'; find='FORBID:data-governance-sub-sovereign-proxy-health-monitor'; rep='FORBID:retired-sub-sovereign-proxy-health-monitor' }
)
$edits['C8'] = @(
  @{ f='rule'; find='STORE:data-governance-sub-sovereign-declared'; rep='STORE:decision-sovereign-declared' }
)
$edits['C20'] = @(
  @{ f='rule'; find='HEALTH-OWNER:health-maintenance-test-sub-sovereign'; rep='HEALTH-OWNER:decision-sovereign' },
  @{ f='rule'; find='CODE-CHANGE:release-update-sync-sub-sovereign'; rep='CODE-CHANGE:automation-sovereign' },
  @{ f='rule'; find='LEARNING:learning-evidence-sync-sub-sovereign'; rep="LEARNING:$X" }
)
$edits['C24'] = @(
  @{ f='rule'; find='HEALTH-OWNER:decision-sovereign classifies health+health-maintenance-test-sub-sovereign preserves+coordinates-maintenance'; rep='HEALTH-OWNER:decision-sovereign classifies health and preserves+coordinates-maintenance' },
  @{ f='rule'; find='runtime-dispatch-or-release-update-sync-sub-sovereign-dispatch'; rep='runtime-dispatch-or-automation-sovereign-dispatch' }
)
$edits['C27'] = @(
  @{ f='rule'; find='sovereign-sub-sovereign'; rep='sovereign-module' }
)
$edits['C31'] = @(
  @{ f='rule'; find='PROGRAMMING:release-update-sync-sub-sovereign may-plan/dispatch changes only-to-registered-descendants'; rep='PROGRAMMING:automation-sovereign may-plan/dispatch changes only-to-registered-descendants' }
)
$edits['C36'] = @(
  @{ f='rule'; find='OWNER:decision-sovereign owns-domain decisions with data-governance-sub-sovereign managing data modules'; rep='OWNER:decision-sovereign owns-domain decisions and manages data modules' }
)
$edits['C38'] = @(
  @{ f='rule'; find='OWNER:decision-sovereign owns domain decisions and data-governance-sub-sovereign manages data modules'; rep='OWNER:decision-sovereign owns domain decisions and manages data modules' }
)
$edits['C39'] = @(
  @{ f='rule'; find='release-update-sync-sub-sovereign alone reviews code change'; rep='automation-sovereign alone reviews code change' }
)
$edits['C40'] = @(
  @{ f='rule'; find='health-maintenance-test-sub-sovereign manages repair evidence'; rep='decision-sovereign manages repair evidence' },
  @{ f='rule'; find='release-update-sync-sub-sovereign reviews any code repair method'; rep='automation-sovereign reviews any code repair method' }
)
$edits['C47'] = @(
  @{ f='rule'; find='health-maintenance-test-sub-sovereign manages repair verification'; rep='decision-sovereign manages repair verification' },
  @{ f='rule'; find='automation-sovereign owns budgets and resource allocation with resource-dependency-sync-sub-sovereign managing resource modules'; rep='automation-sovereign owns budgets and resource allocation and manages resource modules' }
)
$edits['D3'] = @(
  @{ f='rule'; find='STORE:data-governance-sub-sovereign-declared'; rep='STORE:decision-sovereign-declared' }
)
$edits['D8'] = @(
  @{ f='rule'; find='OWNER:health-maintenance-test-sub-sovereign'; rep='OWNER:decision-sovereign' }
)
$edits['D22'] = @(
  @{ f='rule'; find='OWNER:health-maintenance-test-sub-sovereign under decision-sovereign'; rep='OWNER:decision-sovereign' }
)
$edits['D27'] = @(
  @{ f='rule'; find='VERIFIER:startup-sub-sovereign-read-only-validator'; rep='VERIFIER:system-runtime-sovereign-read-only-validator' }
)
$edits['D30'] = @(
  @{ f='rule'; find='NEW-CODE-REQUIRED:release-update-sync-sub-sovereign candidate>D28 certification+atomic-release'; rep='NEW-CODE-REQUIRED:automation-sovereign candidate>D28 certification+atomic-release' }
)
$edits['D42'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns-domain decisions with release-update-sync-sub-sovereign managing version/release modules'; rep='OWNER:automation-sovereign owns-domain decisions and manages version/release modules' }
)
$edits['D43'] = @(
  @{ f='rule'; find='OWNER:decision-sovereign owns-domain decisions with health-maintenance-test-sub-sovereign managing health/maintenance modules'; rep='OWNER:decision-sovereign owns-domain decisions and manages health/maintenance modules' }
)
$edits['D44'] = @(
  @{ f='rule'; find='OWNER:automation-sovereign owns domain decisions and release-update-sync-sub-sovereign manages version/release modules'; rep='OWNER:automation-sovereign owns domain decisions and manages version/release modules' }
)
$edits['D55'] = @(
  @{ f='rule'; find='MANAGEMENT:domain owner implements+health-maintenance-test-sub-sovereign verifies+permission-sovereign registers'; rep='MANAGEMENT:domain owner implements+decision-sovereign verifies+permission-sovereign registers' }
)
$edits['D56'] = @(
  @{ f='exception'; find='health-maintenance-test-sub-sovereign retains verification management'; rep='decision-sovereign retains verification management' }
)
$edits['D77'] = @(
  @{ f='rule'; find='health-maintenance-test-sub-sovereign is centralized management_owner'; rep='decision-sovereign is centralized management_owner' }
)

# ---------- structural rewrites ----------

# A35: sub-sovereign catalog charter -> retired-boundary restatement
$edits['A35'] = @(
  @{ f='rule'; find='SUB-SOVEREIGN:created-only-when-parent-duty-needs-separate-control; CATALOG:active sub-sovereign parent and child map per complete parent reallocation A89+C50+A96+A100; RULE:each active sub-sovereign has exactly one parent and manages modules+assigns+coordinates+collects evidence only; STARTUP:startup-sub-sovereign under runtime-sovereign; DECISION-CHILDREN:policy-architecture+health-maintenance-test+data-governance+priority-capability+change-acceptance; SYNCHRONIZATION-CHILDREN:resource-dependency-sync+dependency-sync+channel-contract-sync+release-update-sync+runtime-state-sync+repair-backup-sync+cleanup-retention-sync+learning-evidence-sync+automatic-log-sync; PERMISSION-CHILDREN:language-review+directory+identity-group structural assignments; SPECIALIZED-SOVEREIGN:may-operate-without-sub-sovereign-when-one-bounded-domain; RETIRED:legacy-undifferentiated-runtime+resource/data/integration/language-review/third-party-sub-sovereigns-after-promotion-hold-no-active-authority';
     rep='SUB-SOVEREIGN:abolished and historical only per B162; no sub-sovereign may be created or hold active authority; CATALOG:the former parent-and-child map is preserved as retired lineage in sovereign_hierarchy_registry; RULE:every former domain duty is exercised directly by the registered parent core and its managed modules; STARTUP:system-runtime-sovereign owns startup duties directly; DECISION-DOMAINS:policy-architecture+health-maintenance-test+data-governance+priority-capability+change-acceptance duties are exercised by decision-sovereign; SYNCHRONIZATION-DOMAINS:resource-dependency-sync+dependency-sync+channel-contract-sync+release-update-sync+runtime-state-sync+repair-backup-sync+cleanup-retention-sync+automatic-log-sync duties are exercised by automation-sovereign; PERMISSION-DOMAINS:directory+identity-group duties are exercised by permission-sovereign; LEARNING-AND-LANGUAGE-DOMAINS:learning-evidence+language-review+system-programming duties are exercised by XINGCHENG; SPECIALIZED-SOVEREIGN:operates-without-sub-sovereign; RETIRED:every former sub-sovereign identity holds no active authority' },
  @{ f='prohibition'; find='FORBID:unnecessary-sub-sovereign+duplicate-control-owner+multiple-parents+direct-execution+information-layer-bypass'; rep='FORBID:active-sub-sovereign+sub-sovereign-layer-revival+duplicate-control-owner+multiple-parents+direct-execution+information-layer-bypass' }
)

# A36: sub-sovereign capability cap -> module-level capability cap
$edits['A36'] = @(
  @{ f='rule'; find='EACH-SUB-SOVEREIGN:one-primary-domain+maximum-3-declared-capabilities; COUNT:distinct-authorized-control-or-dispatch-capabilities; OVERFLOW:mandatory-split+new-exclusive-identity+parent-reassignment-before-activation';
     rep='EACH-MODULE:one-primary-domain+maximum-3-declared-capabilities; COUNT:distinct-authorized-control-or-dispatch-capabilities; OVERFLOW:mandatory-split+new-exclusive-identity+owning-core-reassignment-before-activation' }
)

# A73: hierarchy charter -> five-core charter
$edits['A73'] = @(
  @{ f='rule'; find='SUB-SOVEREIGN-PARENTS:A89 exact map; EACH-SUB-SOVEREIGN:no decision or execution power+module management+assignment+coordination+evidence only; MODULE-EXECUTION-LAYER:only execution actor';
     rep='SUB-SOVEREIGN-PARENTS:abolished per B162;each core manages its modules directly with no intermediate control layer; MODULE-EXECUTION-LAYER:only execution actor' }
)

# A78-A82/A85: sovereign->sub-sovereign retirement chains collapse to the parent core
$edits['A78'] = @(
  @{ f='rule'; find='CHANGE:maintenance-sovereign top-level identity is retired and its active responsibilities converge into maintenance-sub-sovereign under decision-sovereign; PARENT:decision-sovereign; ROLE:maintenance-sub-sovereign has no decision or execution power and only manages maintenance modules+assigns decision-approved work+coordinates module division+collects evidence; HEALTH-DECISION:decision-sovereign; TEST-AND-MAINTENANCE-EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical and is formally superseded by maintenance-sub-sovereign';
     rep='CHANGE:maintenance-sovereign top-level identity is retired and its active responsibilities converge into decision-sovereign directly per B162; ROLE:decision-sovereign manages maintenance modules+assigns approved work+coordinates module division+collects evidence without an intermediate layer; HEALTH-DECISION:decision-sovereign; TEST-AND-MAINTENANCE-EXECUTION:module-execution-layer only; LINEAGE:former identities remain historical' },
  @{ f='prohibition'; find='FORBID:maintenance-sub-sovereign independent decision+direct repair/test/process/file/data/runtime execution+parallel maintenance-sovereign+authority inheritance+acting outside parent decision';
     rep='FORBID:sub-sovereign-layer-revival+decision-sovereign direct repair/test/process/file/data/runtime execution+parallel maintenance-sovereign+authority inheritance+acting outside codified decision boundary' }
)
$edits['A79'] = @(
  @{ f='rule'; find='CHANGE:data-sovereign identity is retired and superseded by data-sub-sovereign under decision-sovereign; PARENT:decision-sovereign; ROLE:data-sub-sovereign has no decision or execution power and only manages data modules+assigns decision-approved data work+coordinates module division+collects evidence; DATA-DECISION:decision-sovereign; DATA-EXECUTION:module-execution-layer only; AUTHORITATIVE-DATA:existing Git+SQL+Rust-Vector-Engine/RAG+LLM authority boundaries remain unchanged';
     rep='CHANGE:data-sovereign identity is retired; its duties converge into decision-sovereign directly per B162 (the intermediate data-sub-sovereign is abolished); ROLE:decision-sovereign manages data modules+assigns approved data work+coordinates module division+collects evidence; DATA-DECISION:decision-sovereign; DATA-EXECUTION:module-execution-layer only; AUTHORITATIVE-DATA:existing Git+SQL+Rust-Vector-Engine/RAG+LLM authority boundaries remain unchanged' },
  @{ f='prohibition'; find='FORBID:data-sub-sovereign independent decision+direct database/schema/file/index/backup/restore/migration execution+parallel data-sovereign+authority inheritance+acting outside parent decision';
     rep='FORBID:sub-sovereign-layer-revival+decision-sovereign direct database/schema/file/index/backup/restore/migration execution+parallel data-sovereign+authority inheritance+acting outside codified decision boundary' }
)
$edits['A80'] = @(
  @{ f='rule'; find='CHANGE:resource-sovereign identity is retired and superseded by resource-sub-sovereign under automation-sovereign; PARENT:automation-sovereign; ROLE:resource-sub-sovereign has no decision or execution power and only manages resource modules+assigns parent-approved work+coordinates module division+collects resource allocation and capacity evidence; DECISION:automation-sovereign; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical';
     rep='CHANGE:resource-sovereign identity is retired; its duties converge into automation-sovereign directly per B162 (the intermediate resource-sub-sovereign is abolished); ROLE:automation-sovereign manages resource modules+assigns approved work+coordinates module division+collects resource allocation and capacity evidence; DECISION:automation-sovereign; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical' },
  @{ f='prohibition'; find='FORBID:resource-sub-sovereign independent decision+direct execution+parallel resource-sovereign+authority inheritance+acting outside automation-sovereign decision';
     rep='FORBID:sub-sovereign-layer-revival+automation-sovereign direct module execution+parallel resource-sovereign+authority inheritance+acting outside codified decision boundary' }
)
$edits['A81'] = @(
  @{ f='rule'; find='CHANGE:integration-sovereign identity is retired and superseded by integration-sub-sovereign under automation-sovereign; PARENT:automation-sovereign; ROLE:integration-sub-sovereign has no decision or execution power and only manages integration modules+assigns parent-approved work+coordinates module division+collects information channel and integration evidence; DECISION:automation-sovereign; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical';
     rep='CHANGE:integration-sovereign identity is retired; its duties converge into automation-sovereign directly per B162 (the intermediate integration-sub-sovereign is abolished); ROLE:automation-sovereign manages integration modules+assigns approved work+coordinates module division+collects information channel and integration evidence; DECISION:automation-sovereign; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical' },
  @{ f='prohibition'; find='FORBID:integration-sub-sovereign independent decision+direct execution+parallel integration-sovereign+authority inheritance+acting outside automation-sovereign decision';
     rep='FORBID:sub-sovereign-layer-revival+automation-sovereign direct module execution+parallel integration-sovereign+authority inheritance+acting outside codified decision boundary' }
)
$edits['A82'] = @(
  @{ f='rule'; find='CHANGE:third-party-sovereign identity is retired and superseded by third-party-sub-sovereign under automation-sovereign; PARENT:automation-sovereign; ROLE:third-party-sub-sovereign has no decision or execution power and only manages third-party modules+assigns parent-approved work+coordinates module division+collects dependency and third-party evidence; DECISION:automation-sovereign; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical';
     rep='CHANGE:third-party-sovereign identity is retired; its duties converge into automation-sovereign directly per B162 (the intermediate third-party-sub-sovereign is abolished); ROLE:automation-sovereign manages third-party modules+assigns approved work+coordinates module division+collects dependency and third-party evidence; DECISION:automation-sovereign; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical' },
  @{ f='prohibition'; find='FORBID:third-party-sub-sovereign independent decision+direct execution+parallel third-party-sovereign+authority inheritance+acting outside automation-sovereign decision';
     rep='FORBID:sub-sovereign-layer-revival+automation-sovereign direct module execution+parallel third-party-sovereign+authority inheritance+acting outside codified decision boundary' }
)
$edits['A85'] = @(
  @{ f='rule'; find='CHANGE:learning-system-sovereign identity is retired and superseded by learning-system-sub-sovereign under automation-sovereign; PARENT:automation-sovereign; ROLE:learning-system-sub-sovereign has no decision or execution power and only manages learning-system modules+assigns parent-approved work+coordinates module division+collects learning module and outcome evidence; DECISION:automation-sovereign; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical';
     rep="CHANGE:learning-system-sovereign identity is retired; learning duties belong to $X directly per B162/A124 (the intermediate learning-system-sub-sovereign is abolished); ROLE:$X manages learning-system modules+assigns approved work+coordinates module division+collects learning module and outcome evidence; DECISION:$X; EXECUTION:module-execution-layer only; LINEAGE:former identity remains historical" },
  @{ f='prohibition'; find='FORBID:learning-system-sub-sovereign'; rep='FORBID:sub-sovereign-layer-revival+retired-learning-system-sub-sovereign' }
)

# A89: parent map -> retired lineage + direct core-domain map
$edits['A89'] = @(
  @{ f='rule'; find='PARENT-MAP:{decision-sovereign=>policy-architecture-sub-sovereign+health-maintenance-test-sub-sovereign+data-governance-sub-sovereign+priority-capability-sub-sovereign+change-acceptance-sub-sovereign}+{runtime-sovereign=>system-sub-sovereign+startup-sub-sovereign}+{automation-sovereign=>resource-sub-sovereign+integration-sub-sovereign+third-party-sub-sovereign+system-programming-sub-sovereign+learning-system-sub-sovereign}+{permission-sovereign=>language-review-sub-sovereign+directory-sub-sovereign+identity-group-sub-sovereign as structural assignments}; RULE:each sub-sovereign has exactly one parent+no decision power+no execution power+module management assignment coordination and evidence collection only; PERMISSION-STRUCTURE:permission-sovereign has no active review or decision power under A88, so language-review-sub-sovereign work requires ';
     rep='PARENT-MAP:abolished per B162 and preserved only as retired lineage in sovereign_hierarchy_registry; DOMAIN-MAP:{decision-sovereign=>policy-architecture+health-maintenance-test+data-governance+priority-capability+change-acceptance}+{system-runtime-sovereign=>system+startup}+{automation-sovereign=>resource+integration+third-party+release-update}+{permission-sovereign=>directory+identity-group}+{XINGCHENG=>language-review+system-programming+learning}; RULE:each module has exactly one owning core+no decision power outside its owner+module management assignment coordination and evidence collection only; PERMISSION-STRUCTURE:permission-sovereign has no active review or decision power under A88, so language-review work requires ' },
  @{ f='prohibition'; find='FORBID:unassigned or multi-parent sub-sovereign+implicit parent+sub-sovereign decision or execution+cross-parent assignment+authority inheritance+parallel legacy hierarchy';
     rep='FORBID:active-sub-sovereign+sub-sovereign-layer-revival+unassigned or multi-parent module+implicit owner+module decision or execution outside the module-execution layer+cross-owner assignment+authority inheritance+parallel legacy hierarchy' }
)

# A90/A91: sub-sovereign identity cards -> retired identity + direct permission-core management
$edits['A90'] = @(
  @{ f='rule'; find='IDENTITY:directory-sub-sovereign; PARENT:permission-sovereign as structural parent; ROLE:manage registered directory modules+assign decision-approved work+coordinate module division+collect evidence; POWER:no decision+no permission review+no authorization+no execution; REVIEW:';
     rep='IDENTITY:directory-sub-sovereign is retired lineage under B162; directory module management is exercised directly by permission-sovereign; ROLE:permission-sovereign manages registered directory modules+assigns decision-approved work+coordinates module division+collects evidence; POWER:no decision+no permission review+no authorization+no execution beyond its registered scope; REVIEW:' },
  @{ f='prohibition'; find='FORBID:directory-sub-sovereign decision+review+authorization+direct execution+cross-domain management+parallel private registry';
     rep='FORBID:directory-sub-sovereign revival+permission-sovereign exceeding registered scope+direct execution+cross-domain management+parallel private registry' }
)
$edits['A91'] = @(
  @{ f='rule'; find='IDENTITY:identity-group-sub-sovereign; PARENT:permission-sovereign as structural parent; ROLE:manage registered identity-group modules+assign decision-approved work+coordinate module division+collect evidence; POWER:no decision+no permission review+no authorization+no execution; REVIEW:';
     rep='IDENTITY:identity-group-sub-sovereign is retired lineage under B162; identity-group module management is exercised directly by permission-sovereign; ROLE:permission-sovereign manages registered identity-group modules+assigns decision-approved work+coordinates module division+collects evidence; POWER:no decision+no permission review+no authorization+no execution beyond its registered scope; REVIEW:' },
  @{ f='prohibition'; find='FORBID:identity-group-sub-sovereign decision+review+authorization+direct execution+cross-domain management+parallel private registry';
     rep='FORBID:identity-group-sub-sovereign revival+permission-sovereign exceeding registered scope+direct execution+cross-domain management+parallel private registry' }
)

# A96: decision children map -> direct decision domains
$edits['A96'] = @(
  @{ f='rule'; find='CHILDREN:{policy-architecture-sub-sovereign=>policy and architecture modules}+{health-maintenance-test-sub-sovereign=>health maintenance and test modules}+{data-governance-sub-sovereign=>data authority lifecycle integrity and schema modules}+{priority-capability-sub-sovereign=>priority capability objective and permission-purpose evidence modules}+{change-acceptance-sub-sovereign=>change impact acceptance and release-readiness evidence modules}; EACH-CHILD:no decision+no execution+one exclusive primary domain+module management assignment coordination evidence only; REMOVED:{maintenance-sub-sovereign+data-sub-sovereign+permission-sub-sovereign} formally superseded;';
     rep='DOMAINS:{policy-architecture=>policy and architecture modules}+{health-maintenance-test=>health maintenance and test modules}+{data-governance=>data authority lifecycle integrity and schema modules}+{priority-capability=>priority capability objective and permission-purpose evidence modules}+{change-acceptance=>change impact acceptance and release-readiness evidence modules} managed directly by decision-sovereign; EACH-DOMAIN:one exclusive primary domain+module management assignment coordination evidence only; REMOVED:{maintenance-sub-sovereign+data-sub-sovereign+permission-sub-sovereign} formally superseded; all sub-sovereign intermediaries abolished per B162;' },
  @{ f='prohibition'; find='FORBID:overlapping child scope+duplicate owner+sub-sovereign decision/review/authorization/execution+cross-child takeover+implicit policy+acceptance without independent evidence+permission boundary takeover';
     rep='FORBID:overlapping domain scope+duplicate owner+sub-sovereign revival/decision/review/authorization/execution+cross-domain takeover+implicit policy+acceptance without independent evidence+permission boundary takeover' }
)

# A53: orchestration flow with five sub-sovereign duty clauses
$edits['A53'] = @(
  @{ f='rule'; find='CHANNEL:channel-contract-sync-sub-sovereign manages-topology/routing/contracts/delivery-health modules under automation-sovereign decision'; rep='CHANNEL:automation-sovereign manages-topology/routing/contracts/delivery-health modules' },
  @{ f='rule'; find='STARTUP:startup-sub-sovereign coordinates-boot-only and hands-verified-readiness-to-runtime-sovereign'; rep='STARTUP:system-runtime-sovereign coordinates-boot-only within its own startup phase and retains verified-readiness ownership' },
  @{ f='rule'; find='HEALTH:health-maintenance-test-sub-sovereign observes-health+opens-bounded-maintenance-request'; rep='HEALTH:decision-sovereign observes-health+opens-bounded-maintenance-request' },
  @{ f='rule'; find='release-update-sync-sub-sovereign-review-and-dispatch'; rep='automation-sovereign-review-and-dispatch' },
  @{ f='rule'; find='LEARNING:learning-evidence-sync-sub-sovereign receives-redacted-outcomes'; rep="LEARNING:$X receives-redacted-outcomes" },
  @{ f='rule'; find='resource/data/dependency modules are managed by automation-sovereign sub-sovereigns (resource-dependency-sync/data-governance/dependency-sync)'; rep='resource/dependency modules are managed by automation-sovereign and data-governance modules by decision-sovereign' },
  @{ f='rule'; find='each sub-sovereign never-performs-another-domain-duty'; rep='each core never-performs-another-domain-duty' }
)

# A105: test management owner + SUB roster
$edits['A105'] = @(
  @{ f='rule'; find='management_owner=health-maintenance-test-sub-sovereign is the centralized operational manager'; rep='management_owner=decision-sovereign is the centralized operational manager' },
  @{ f='rule'; find='ROSTERS:top-level current institution roster is separately identified from active sub-sovereign roster. TOP={decision-sovereign,system-runtime-sovereign,automation-sovereign,permission-sovereign,';
     rep='ROSTERS:top-level current institution roster is separately identified from the module layer. TOP={decision-sovereign,system-runtime-sovereign,automation-sovereign,permission-sovereign,' },
  @{ f='rule'; find='SUB={system-sub-sovereign,startup-sub-sovereign,directory-sub-sovereign,identity-group-sub-sovereign,resource-dependency-sync-sub-sovereign,channel-contract-sync-sub-sovereign,release-update-sync-sub-sovereign,learning-evidence-sync-sub-sovereign,runtime-state-sync-sub-sovereign,repair-backup-sync-sub-sovereign,cleanup-retention-sync-sub-sovereign,automatic-log-sync-sub-sovereign,policy-architecture-sub-sovereign,health-maintenance-test-sub-sovereign,data-governance-sub-sovereign,priority-capability-sub-sovereign,change-acceptance-sub-sovereign,dependency-sync-sub-sovereign}. Each sub-sovereign keeps one parent and no execution power.';
     rep='SUB=none:all former sub-sovereign identities are abolished and historical only per B162;the retired roster is preserved in sovereign_hierarchy_registry as lineage. Each module keeps one owning core and no independent authority.' },
  @{ f='prohibition'; find='top roster mixed with sub-sovereigns'; rep='top roster mixed with retired sub-sovereign identities' }
)

# A118: sovereign-sub-sovereign route -> sovereign-module route
$edits['A118'] = @(
  @{ f='rule'; find='DIRECT-ROUTE:a sovereign and any lawfully attached sub-sovereign may address each other directly without an intermediary sovereign,while every message still enters the canonical information-layer service and is carried only by the shared-layer typed transport implementation. ROUTE:sovereign identity -> information-layer authorization+contract+correlation -> shared-layer logical stream -> attached sub-sovereign identity,and the reverse path uses the same contract.';
     rep='HISTORICAL-BOUNDARY:the former sovereign-sub-sovereign attachment is retired lineage under B162;a sovereign may address its registered managed modules directly without an intermediary,while every message still enters the canonical information-layer service and is carried only by the shared-layer typed transport implementation. ROUTE:sovereign identity -> information-layer authorization+contract+correlation -> shared-layer logical stream -> managed module identity,and the reverse path uses the same contract.' },
  @{ f='rule'; find='VALIDATION:each stream proves current attachment,active identity,role,scope,permission,generation,schema,deadline,sequence and correlation before delivery. AUTHORITY:the sovereign retains only its declared decision or privileged power;the sub-sovereign retains dispatch,module-management and coordination duties and gains no module execution power.';
     rep='VALIDATION:each stream proves current registration,active identity,role,scope,permission,generation,schema,deadline,sequence and correlation before delivery. AUTHORITY:the sovereign retains only its declared decision or privileged power;the managed module retains implementation duties and gains no decision authority.' },
  @{ f='rule'; find='ISOLATION:one sovereign-sub-sovereign stream has its own queue,budget,backpressure,checkpoint and fault domain;failure cannot interrupt unrelated streams. TOPOLOGY:attachment is resolved dynamically from the registered hierarchy and identity generation,never from hard-coded physical addresses.';
     rep='ISOLATION:one sovereign-module stream has its own queue,budget,backpressure,checkpoint and fault domain;failure cannot interrupt unrelated streams. TOPOLOGY:attachment is resolved dynamically from the registered module ownership and identity generation,never from hard-coded physical addresses.' },
  @{ f='prohibition'; find='sub-sovereign execution+unregistered attachment'; rep='sub-sovereign revival+module self-authorization+unregistered attachment' }
)

# A125: rename charter - drop live sub-sovereign references
$edits['A125'] = @(
  @{ f='rule'; find='through its assigned sub-sovereigns,excluding learning-sub-sovereign transferred to '; rep='directly through its managed modules,excluding learning duties transferred to ' }
)

# B66: startup-sovereign -> startup-sub -> runtime-sovereign chain collapses
$edits['B66'] = @(
  @{ f='rule'; find='CHANGE:startup-sovereign identity is retired and superseded by startup-sub-sovereign under runtime-sovereign; PARENT:runtime-sovereign; ROLE:startup-sub-sovereign has no decision or execution power and only manages startup modules+assigns runtime-sovereign-approved startup work+coordinates module division+collects evidence; STARTUP-DECISION:runtime-sovereign; STARTUP-EXECUTION:module-execution-layer only';
     rep='CHANGE:startup-sovereign identity is retired and startup duties converge into runtime-sovereign directly per B162 (the intermediate startup-sub-sovereign is abolished); ROLE:runtime-sovereign manages startup modules+assigns approved startup work+coordinates module division+collects evidence; STARTUP-DECISION:runtime-sovereign; STARTUP-EXECUTION:module-execution-layer only' },
  @{ f='prohibition'; find='FORBID:startup-sub-sovereign independent decision+direct process/service/runtime/UI/backend execution+parallel startup-sovereign+authority inheritance+acting outside parent decision';
     rep='FORBID:sub-sovereign-layer-revival+startup-phase independent decision outside runtime-sovereign+direct process/service/runtime/UI/backend execution by non-executor+parallel startup-sovereign+authority inheritance+acting outside runtime-sovereign decision' }
)

# ---------- apply ----------
$result = [ordered]@{}
$errors = @()
foreach ($id in $edits.Keys) {
  $newRule = $rows.$id.rule
  $newProh = $rows.$id.prohibition
  $newExc  = $rows.$id.exception
  foreach ($e in $edits[$id]) {
    $field = $e.f
    $target = switch ($field) { 'rule' { $newRule } 'prohibition' { $newProh } 'exception' { $newExc } }
    $cnt = ([regex]::Matches($target, [regex]::Escape($e.find))).Count
    if ($cnt -ne 1) { $errors += "$id.$field : find-count=$cnt for '$($e.find.Substring(0,[Math]::Min(60,$e.find.Length)))'"; continue }
    $target = $target.Replace($e.find, $e.rep)
    switch ($field) { 'rule' { $newRule = $target } 'prohibition' { $newProh = $target } 'exception' { $newExc = $target } }
  }
  $result[$id] = @{ rule=$newRule; prohibition=$newProh; exception=$newExc }
}

if ($errors.Count) { $errors | ForEach-Object { "ERR: $_" }; exit 1 }

# residual check: named sub-sovereign identities must be gone from modified fields
$identRe = '([a-z0-9]+-)+sub-sovereign'
$resid = @()
foreach ($id in $result.Keys) {
  foreach ($f in 'rule','prohibition','exception') {
    $refs = [regex]::Matches($result[$id][$f], $identRe) | ForEach-Object { $_.Value } | Sort-Object -Unique
    if ($refs.Count) { $resid += "$id.$f -> $($refs -join ',')" }
  }
}
$resid | ForEach-Object { "RESIDUAL: $_" }

$result | ConvertTo-Json -Depth 4 | Set-Content _newtexts.json -Encoding utf8
"OK - provisions rewritten: $($result.Count); residuals: $($resid.Count)"