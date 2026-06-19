# GPTBridge Governance Index (Level 1 Navigator)

| Rule ID | Rule Name | Authority Source | Enforcement | Checker |
| :--- | :--- | :--- | :--- | :--- |
| **L0** | **Constitution** | `GOVERNANCE_CONSTITUTION.md` | **BLOCKING** | N/A |
| G-001 | UI First Principle | `core/startup-governance.md` | BLOCKING | ServiceManager |
| G-002 | Governance Root | `GOVERNANCE_CONSTITUTION.md` | BLOCKING | RootPurity |
| G-003 | Alias Authority | `core/import-governance.md` | BLOCKING | ImportGuard |
| G-009 | Runtime Singularity | `core/runtime-governance.md` | BLOCKING | N/A |
| G-015 | Dashboard Purity | `core/module-governance.md` | WARNING | ModularityGuard |
| G-017 | Root Purity | `core/module-governance.md` | BLOCKING | RootPurity |
| G-018 | Build Pipeline | `core/build-governance.md` | BLOCKING | package.json |
| G-031 | App Shell Stability | `core/runtime-governance.md` | BLOCKING | MainShell |
| G-048 | One Truth Per System | `GOVERNANCE_CONSTITUTION.md` | BLOCKING | Constitution |
| G-058 | Listener Ownership | `core/service-governance.md` | BLOCKING | EventBus |
| G-065 | Platform Survival | `core/runtime-governance.md` | BLOCKING | MainShell |
| G-077 | Recovery Authority | `core/recovery-governance.md` | BLOCKING | N/A |
| G-080 | Structural Lock | `core/ai-governance.md` | BLOCKING | AI_PATCH_POLICY |
| G-094 | Recovery Escalation | `core/recovery-governance.md` | BLOCKING | RecoveryManager |
| G-100 | Startup Determinism | `core/startup-governance.md` | BLOCKING | ServiceManager |
| G-106 | Governance Layering | `GOVERNANCE_CONSTITUTION.md` | BLOCKING | N/A |
| G-107 | Domain Isolation | `GOVERNANCE_CONSTITUTION.md` | WARNING | N/A |
| G-114 | Navigation | `GOVERNANCE_INDEX.md` | BLOCKING | N/A |
| G-115 | Child Tool Isolation | `core/module-governance.md` | BLOCKING | ChildToolIsolationChecker |

### Authority Tree

resources/governance/
├── GOVERNANCE_CONSTITUTION.md (L0)
├── GOVERNANCE_INDEX.md (L1)
├── GOVERNANCE_VERSION.md (L1)
├── core/ (L1 Domains)
│   ├── runtime-governance.md
│   ├── startup-governance.md
│   ├── service-governance.md
│   ├── ai-governance.md
│   └── ...
├── operational/ (L3 Workflows)
│   ├── hmr-governance.md
│   └── ...
└── debt/ (L4 Registry)
    └── technical-debt-registry.md

### Authority Mapping
- **Runtime Authority**: `src-ui/renderer/services/RuntimeServiceManager.ts`
- **Event Authority**: `src-ui/renderer/shared/RuntimeEventBus.ts`
- **Log Authority**: `src-ui/renderer/shared/BootLogger.ts`
- **Security Authority**: `src-ui/main/preload.js`
