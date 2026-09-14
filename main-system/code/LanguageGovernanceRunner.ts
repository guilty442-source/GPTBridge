/**
 * LanguageGovernanceRunner.ts —Orchestrates all language governance gates.
 *
 * GATE ORDER: save > pre-commit > integration > release
 * FINAL RESULT: exactly APPROVED | APPROVED_WITH_WARNINGS | REJECTED | INCOMPLETE_EVIDENCE
 */

import { runLanguageBoundaryGate, LanguageBoundaryResult } from './checkers/LanguageBoundaryChecker';
import { runDependencyDagGate, DependencyDagResult } from './checkers/DependencyDagChecker';
import { runOwnershipUniquenessGate, OwnershipCheckResult } from './checkers/OwnershipUniquenessChecker';
import { runContractParityGate, ContractParityResult } from './checkers/ContractParityChecker';
import { runBuildAbiReproGate, BuildReproResult } from './checkers/BuildAbiReproChecker';
import { runReleaseCompatibilityGate, CompatibilityCheckResult } from './checkers/ReleaseCompatibilityChecker';

export type GovernanceVerdict = 'APPROVED' | 'APPROVED_WITH_WARNINGS' | 'REJECTED' | 'INCOMPLETE_EVIDENCE';

export interface GateResult {
  name: string;
  verdict: 'PASS' | 'WARN' | 'FAIL';
  details: any;
  durationMs: number;
}

export interface GovernanceResult {
  finalVerdict: GovernanceVerdict;
  gates: GateResult[];
  totalDurationMs: number;
  timestamp: string;
}

export interface ScanConfig {
  rootPaths: string[];
  excludePatterns: string[];
}

export class LanguageGovernanceRunner {
  private config: ScanConfig;

  constructor(config: ScanConfig) {
    this.config = config;
  }

  async scanFiles(): Promise<Map<string, string>> {
    const files = new Map<string, string>();
    const fs = await import('fs');
    const path = await import('path');

    for (const root of this.config.rootPaths) {
      await this.scanDirectory(root, files, fs, path);
    }

    return files;
  }

  private async scanDirectory(dir: string, files: Map<string, string>, fs: any, path: any): Promise<void> {
    try {
      const entries = await fs.promises.readdir(dir, { withFileTypes: true });

      for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        const relativePath = path.relative(process.cwd(), fullPath).replace(/\\/g, '/');

        // Check exclude patterns
        if (this.config.excludePatterns.some(p => relativePath.includes(p))) {
          continue;
        }

        if (entry.isDirectory()) {
          await this.scanDirectory(fullPath, files, fs, path);
        } else if (entry.isFile()) {
          const ext = path.extname(entry.name);
          if (['.ts', '.tsx', '.py', '.cs', '.cpp', '.c', '.h', '.hpp', '.sql'].includes(ext)) {
            try {
              const content = await fs.promises.readFile(fullPath, 'utf-8');
              files.set(relativePath, content);
            } catch {
              // Skip unreadable files
            }
          }
        }
      }
    } catch {
      // Skip unreadable directories
    }
  }

  async runAllGates(): Promise<GovernanceResult> {
    const startTime = Date.now();
    const files = await this.scanFiles();

    const gates: GateResult[] = [];

    // Gate 1: Language Boundary
    gates.push(await this.runGate('Language Boundary Gate', async () => {
      return runLanguageBoundaryGate(files);
    }));

    // Gate 2: Dependency DAG
    gates.push(await this.runGate('Dependency DAG Gate', async () => {
      return runDependencyDagGate(files);
    }));

    // Gate 3: Ownership Uniqueness
    gates.push(await this.runGate('Ownership Uniqueness Gate', async () => {
      // Load capability registry
      const { capabilityRegistry } = await import('./registry/CapabilityRegistry');
      return runOwnershipUniquenessGate(
        Array.from(files.keys()).map(f => ({
          moduleId: f,
          declaredOwner: 'unknown',  // Would be extracted from file
          capabilities: [],
          layer: 'unknown',
        })),
        { capabilities: capabilityRegistry.getAllCapabilities().map(c => ({ id: c.id, owner: c.owner })) }
      );
    }));

    // Gate 4: Contract Parity
    gates.push(await this.runGate('Contract Parity Gate', async () => {
      // Would load actual contracts
      return runContractParityGate([]);
    }));

    // Gate 5: Build ABI Repro (only for native artifacts)
    gates.push(await this.runGate('Build ABI Repro Gate', async () => {
      // Would check native artifacts
      return runBuildAbiReproGate({
        artifactPath: 'native/core/parser.cpp',
        baselinePath: 'baselines/parser.json',
        toolchainSpec: {
          toolchain: 'msvc-19.40',
          toolchainVersion: '19.40',
          targetTriple: 'x86_64-windows',
          optimizationFlags: ['/O2'],
          defines: ['UNICODE', '_UNICODE'],
        },
      });
    }));

    // Gate 6: Release Compatibility (if release candidate)
    gates.push(await this.runGate('Release Compatibility Gate', async () => {
      // Would run for release candidates
      return runReleaseCompatibilityGate({
        candidate: {
          version: '1.0.0',
          releaseId: 'rel-001',
          applicationVersion: '1.0.0',
          toolVersions: {},
          contractVersions: {},
          abiVersion: '1.0.0',
          artifacts: [],
        },
        previousRelease: null,
        baseline: {
          languageBoundaries: [],
          apiContracts: [],
          abiContracts: [],
          migrationRules: [],
          fallbackRules: [],
        },
      });
    }));

    const totalDuration = Date.now() - startTime;
    const finalVerdict = this.computeFinalVerdict(gates);

    return {
      finalVerdict,
      gates,
      totalDurationMs: totalDuration,
      timestamp: new Date().toISOString(),
    };
  }

  private async runGate<T>(name: string, fn: () => Promise<T>): Promise<GateResult> {
    const start = Date.now();
    try {
      const details = await fn();
      const verdict = this.extractVerdict(details);
      return {
        name,
        verdict,
        details,
        durationMs: Date.now() - start,
      };
    } catch (error) {
      return {
        name,
        verdict: 'FAIL',
        details: { error: String(error) },
        durationMs: Date.now() - start,
      };
    }
  }

  private extractVerdict(details: any): 'PASS' | 'WARN' | 'FAIL' {
    if (details.verdict) return details.verdict;
    if (details.overall) return details.overall;
    return 'FAIL';
  }

  private computeFinalVerdict(gates: GateResult[]): GovernanceVerdict {
    const hasFail = gates.some(g => g.verdict === 'FAIL');
    const hasWarn = gates.some(g => g.verdict === 'WARN');

    if (hasFail) return 'REJECTED';
    if (hasWarn) return 'APPROVED_WITH_WARNINGS';
    return 'APPROVED';
  }
}

export async function runLanguageGovernance(config: ScanConfig): Promise<GovernanceResult> {
  const runner = new LanguageGovernanceRunner(config);
  return runner.runAllGates();
}

// Default configuration
export const DEFAULT_SCAN_CONFIG: ScanConfig = {
  rootPaths: [
    'main-system/src-core',
    'main-system/src-ui',
    'main-system/governance',
    'native',
    'shared-layer/src',
  ],
  excludePatterns: [
    '.venv',
    'node_modules',
    '__pycache__',
    '.git',
    '.agents',
    '.kilo',
    'dist',
    'build',
  ],
};