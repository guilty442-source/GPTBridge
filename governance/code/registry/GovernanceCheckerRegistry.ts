/**
 * Governance Checker Registry (Phase 7)
 * 定義所有治理檢查器的註冊與調度中心。
 */

export enum EnforceLevel {
  ADVISORY = 'advisory',
  WARNING = 'warning',
  BLOCKING = 'blocking',
}

export enum Severity {
  BLOCKING = 'BLOCKING',
  WARNING = 'WARNING',
  ADVISORY = 'ADVISORY',
}

/**
 * Rule: Governance Coverage Tracking (Section A)
 * Rule: Enforcement Maturity Tracking (Section F - G-086)
 */
export enum CoverageStatus {
  DOCUMENTED = 'documented',
  ADVISORY = 'advisory',
  PARTIALLY_ENFORCED = 'partially enforced',
  FULLY_ENFORCED = 'fully enforced',
  RUNTIME_ENFORCED = 'runtime enforced',
  BUILD_ENFORCED = 'build enforced',
}

export enum CheckerCategory {
  IMPORT = 'import',
  RUNTIME = 'runtime',
  MODULE = 'module',
  UI = 'ui',
  SERVICE = 'service',
  PLUGIN = 'plugin',
  HMR = 'hmr',
  SECURITY = 'security',
}

export interface GovernanceReport {
  ruleId: string
  passed: boolean
  message: string
  affectedFiles?: string[]
  autofixAvailable: boolean
}

export interface GovernanceChecker {
  id: string
  name: string
  category: CheckerCategory
  severity: Severity // Phase 18: Severity Model
  enforceLevel: EnforceLevel
  target: string
  coverage: CoverageStatus // Section A: Coverage Tracking
  version: string // Section I: Version Authority
  run: () => Promise<GovernanceReport>
  autofix?: () => Promise<void>
}

class CheckerRegistry {
  private checkers: GovernanceChecker[] = []

  public register(checker: GovernanceChecker) {
    this.checkers.push(checker)
  }

  public async runAll(category?: CheckerCategory): Promise<GovernanceReport[]> {
    const targets = category
      ? this.checkers.filter((c) => c.category === category)
      : this.checkers

    const reports: GovernanceReport[] = []
    for (const checker of targets) {
      const report = await checker.run()
      reports.push(report)
    }
    return reports
  }

  public getCheckers() {
    return this.checkers
  }
}

export const registry = new CheckerRegistry()

/**
 * Severity Model (Phase 18)
 * 用於判定是否中斷建置流程。
 */
export const shouldBlockBuild = (reports: GovernanceReport[]): boolean => {
  return reports.some((report) => {
    const checker = registry.getCheckers().find((c) => c.id === report.ruleId)
    return (
      !report.passed &&
      (checker?.enforceLevel === EnforceLevel.BLOCKING ||
        checker?.severity === Severity.BLOCKING)
    )
  })
}
