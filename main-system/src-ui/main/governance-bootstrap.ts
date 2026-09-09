import { createHash, createHmac, randomBytes } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

const PROTECTED_GOVERNANCE_SOURCES = [
  'governance_rule/governance_policy.py',
  'governance_rule/codex/__init__.py',
  'governance_rule/codex/sovereigns.py',
  'governance_rule/codex/chinese.py',
  'governance_rule/codex/sovereigns_chinese.py',
  'governance_rule/code_rule_directory.py',
  'governance_rule/permission_directory/directory_authority.py',
  'governance_rule/execution/authentication/__init__.py',
  'governance_rule/execution/integrity/__init__.py',
  'governance_rule/execution/versioning/__init__.py',
  'governance_rule/permission_directory/execution/identity_registry/__init__.py',
  'governance_rule/permission_directory/execution/path_guard/__init__.py',
  'main-system/src-ui/main/governance-bootstrap.ts',
  'main-system/src-core/core_system/governance_runtime.py',
  'governance_rule/execution/tool_runtime/__init__.py',
  'governance_rule/execution/tool_runtime/governed_runtime.py',
  'governance_rule/execution/tool_runtime/tool_self_repair.py',
  'governance_rule/execution/tool_runtime/tool_local_cleanup.py',
  'governance_rule/execution/tool_runtime/sub_sovereign.py',
  'governance_rule/execution/git_tiers/__init__.py',
  'governance_rule/permission_directory/registries/permissions/identity_groups.py',
  'governance_rule/permission_directory/registries/permissions/identity_permissions.py',
  'governance_rule/permission_directory/registries/permissions/capability_boundaries.py',
  'governance_rule/permission_directory/registries/permissions/tool_routes.py',
  'governance_rule/permission_directory/registries/permissions/source_ownership.py',
] as const

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

function protectedFileDigest(workspaceRoot: string, relativePath: string): string {
  const root = fs.realpathSync.native(workspaceRoot)
  const candidate = path.resolve(root, ...relativePath.split('/'))
  const relative = path.relative(root, candidate)
  if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('PERMISSION_DENIED')
  const stat = fs.lstatSync(candidate)
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('PERMISSION_DENIED')
  const realCandidate = fs.realpathSync.native(candidate)
  const realRelative = path.relative(root, realCandidate)
  if (realRelative.startsWith('..') || path.isAbsolute(realRelative)) throw new Error('PERMISSION_DENIED')
  return createHash('sha256').update(fs.readFileSync(realCandidate)).digest('hex')
}

function signatureHex(key: Buffer, value: unknown): string {
  return createHmac('sha256', key).update(canonicalJson(value), 'utf8').digest('hex')
}

function governanceAuthorityVersion(workspaceRoot: string): number {
  const policySource = fs.readFileSync(path.resolve(fs.realpathSync.native(workspaceRoot), 'governance_rule', 'governance_policy.py'), 'utf8')
  const authorityVersion = Number(/\bauthority_version\s*=\s*(\d+)\s*,/.exec(policySource)?.[1] || 0)
  if (!Number.isSafeInteger(authorityVersion) || authorityVersion <= 0) throw new Error('PERMISSION_DENIED')
  return authorityVersion
}

export function createMainSystemGovernanceBootstrap(workspaceRoot: string): string {
  const launcherKey = randomBytes(32)
  try {
    const issuedAt = Math.floor(Date.now() / 1000)
    const keyId = randomBytes(16).toString('hex')
    const integrityPayload = {
      authority_version: governanceAuthorityVersion(workspaceRoot),
      file_digests: PROTECTED_GOVERNANCE_SOURCES.map((relativePath) => [relativePath, protectedFileDigest(workspaceRoot, relativePath)]),
      issued_at: issuedAt,
      key_id: keyId,
    }
    const identityPayload = {
      issuer: 'main-system-launcher-only', actor: 'governance/main-system',
      bound_tool_id: 'main-system', caller_path: 'main-system/src-core',
      process_id: process.pid, issued_at: issuedAt, expires_at: issuedAt + 30,
      nonce: randomBytes(16).toString('hex'), key_id: keyId,
    }
    return Buffer.from(JSON.stringify({
      format_version: 1,
      launcher_key: launcherKey.toString('base64'),
      integrity_manifest: { ...integrityPayload, signature: signatureHex(launcherKey, integrityPayload) },
      identity_attestation: { ...identityPayload, signature: signatureHex(launcherKey, identityPayload) },
    }), 'utf8').toString('base64')
  } finally {
    launcherKey.fill(0)
  }
}

export function preloadDefaultGovernanceAuthority(workspaceRoot: string): void {
  if (!createMainSystemGovernanceBootstrap(workspaceRoot)) throw new Error('PERMISSION_DENIED')
}
