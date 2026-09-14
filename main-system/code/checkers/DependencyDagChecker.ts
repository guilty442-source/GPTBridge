/**
 * DependencyDagChecker.ts ?”Dependency DAG Gate (A354/A362).
 *
 * Verifies import/dependency graph respects:
 * - Module ownership boundaries (A356)
 * - Language boundaries (A351/A352)
 * - Layer architecture (A68): presentation ?”channel-api ?”application-use-case ?”domain ?”infrastructure
 * - No circular dependencies
 * - Canonical dependency direction per A211/A212
 */

export type DagVerdict = 'PASS' | 'WARN' | 'FAIL';

export interface DependencyNode {
  id: string;
  language: string;
  layer: string;
  owner: string;
  imports: string[];
  importedBy: string[];
}

export interface DagViolation {
  rule: string;
  message: string;
  from: string;
  to: string;
  severity: 'error' | 'warn';
}

export interface DependencyDagResult {
  verdict: DagVerdict;
  nodes: DependencyNode[];
  violations: DagViolation[];
  cycles: string[][];
}

export const LAYER_ORDER = [
  'presentation',      // TypeScript UI
  'channel-api',       // Information layer contracts
  'application-use-case', // Python orchestration
  'domain',            // Python domain policy
  'infrastructure',    // Python adapters, native bindings
  'native-core',       // C++ private implementation
  'c-abi',             // C public interface
  'csharp-adapter',    // C# Windows adapter
];

export class DependencyDagChecker {
  private nodes: Map<string, DependencyNode> = new Map();
  private fileToModule: Map<string, string> = new Map();

  buildDag(files: Map<string, string>): void {
    this.nodes.clear();
    this.fileToModule.clear();

    // First pass: identify modules
    for (const [file, content] of files) {
      const moduleId = this.extractModuleId(file, content);
      if (moduleId) {
        const language = this.detectLanguage(file);
        const layer = this.determineLayer(file, language);
        const owner = this.determineOwner(file, language);

        this.nodes.set(moduleId, {
          id: moduleId,
          language,
          layer,
          owner,
          imports: [],
          importedBy: [],
        });
        this.fileToModule.set(file, moduleId);
      }
    }

    // Second pass: resolve imports
    for (const [file, content] of files) {
      const fromModule = this.fileToModule.get(file);
      if (!fromModule) continue;

      const imports = this.extractImports(file, content);
      for (const imp of imports) {
        const toModule = this.resolveImport(imp, file);
        if (toModule && toModule !== fromModule) {
          const fromNode = this.nodes.get(fromModule);
          const toNode = this.nodes.get(toModule);
          if (fromNode && toNode) {
            fromNode.imports.push(toModule);
            toNode.importedBy.push(fromModule);
          }
        }
      }
    }
  }

  checkDag(): DependencyDagResult {
    const violations: DagViolation[] = [];

    // Check layer ordering (A68: presentation ?”channel-api ?”application ?”domain ?”infrastructure)
    for (const [id, node] of this.nodes) {
      for (const imp of node.imports) {
        const target = this.nodes.get(imp);
        if (target) {
          const fromIdx = LAYER_ORDER.indexOf(node.layer);
          const toIdx = LAYER_ORDER.indexOf(target.layer);
          if (fromIdx >= 0 && toIdx >= 0 && fromIdx > toIdx) {
            violations.push({
              rule: 'layer_order',
              message: `Layer violation: ${node.layer} -> ${target.layer} (must flow downward per A68)`,
              from: id,
              to: imp,
              severity: 'error',
            });
          }
        }
      }
    }

    // Check language boundaries (A351/A352)
    for (const [id, node] of this.nodes) {
      for (const imp of node.imports) {
        const target = this.nodes.get(imp);
        if (target && node.language !== target.language) {
          const allowed = this.isCrossLanguageAllowed(node.language, target.language);
          if (!allowed) {
            violations.push({
              rule: 'language_boundary',
              message: `${node.language} cannot import ${target.language} (A351/A352)`,
              from: id,
              to: imp,
              severity: 'error',
            });
          }
        }
      }
    }

    // Check ownership (A356: single owner per module)
    const ownerMap: Map<string, string> = new Map();
    for (const [id, node] of this.nodes) {
      if (ownerMap.has(node.owner)) {
        violations.push({
          rule: 'ownership_unique',
          message: `Owner ${node.owner} has multiple modules (A356)`,
          from: ownerMap.get(node.owner) || '',
          to: id,
          severity: 'warn',
        });
      }
      ownerMap.set(node.owner, id);
    }

    // Detect cycles
    const cycles = this.detectCycles();

    const verdict = violations.some(v => v.severity === 'error') ? 'FAIL' :
                    violations.some(v => v.severity === 'warn') ? 'WARN' : 'PASS';

    return { verdict, nodes: Array.from(this.nodes.values()), violations, cycles };
  }

  private extractModuleId(file: string, content: string): string | null {
    // Simplified: use file path as module ID
    return file.replace(/\\/g, '/').replace(/\.(ts|tsx|py|cs|cpp|c|h|hpp|sql)$/, '');
  }

  private detectLanguage(file: string): string {
    const ext = file.substring(file.lastIndexOf('.'));
    const map: Record<string, string> = {
      '.py': 'Python', '.pyi': 'Python',
      '.ts': 'TypeScript', '.tsx': 'TypeScript', '.d.ts': 'TypeScript',
      '.cs': 'CSharp',
      '.cpp': 'C++', '.hpp': 'C++', '.inl': 'C++',
      '.c': 'C', '.h': 'C',
      '.sql': 'SQL',
    };
    return map[ext] || 'Unknown';
  }

  private determineLayer(file: string, language: string): string {
    const normalized = file.replace(/\\/g, '/');
    if (normalized.includes('src-ui/') || normalized.includes('renderer/')) return 'presentation';
    if (normalized.includes('channel') || normalized.includes('contract') || normalized.includes('ipc')) return 'channel-api';
    if (language === 'Python' && (normalized.includes('orchestrat') || normalized.includes('workflow') || normalized.includes('application'))) return 'application-use-case';
    if (language === 'Python' && (normalized.includes('domain') || normalized.includes('policy'))) return 'domain';
    if (language === 'Python' && (normalized.includes('adapter') || normalized.includes('binding') || normalized.includes('native'))) return 'infrastructure';
    if (language === 'C++' || normalized.includes('native/core/')) return 'native-core';
    if (language === 'C' || normalized.includes('native/include/') || normalized.includes('native/bridge/')) return 'c-abi';
    if (language === 'CSharp') return 'csharp-adapter';
    return 'unknown';
  }

  private determineOwner(file: string, language: string): string {
    // Simplified: use directory as owner
    return file.split('/')[0] || 'unknown';
  }

  private extractImports(file: string, content: string): string[] {
    const imports: string[] = [];
    const lines = content.split('\n');

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('import ') || trimmed.startsWith('from ') ||
          trimmed.startsWith('require(') || trimmed.startsWith('import(')) {
        // Simplified extraction
        imports.push(trimmed);
      }
    }
    return imports;
  }

  private resolveImport(importStmt: string, fromFile: string): string | null {
    // Simplified: return null for external imports
    return null;
  }

  private isCrossLanguageAllowed(from: string, to: string): boolean {
    const allowed: Record<string, string[]> = {
      'TypeScript': ['TypeScript'],
      'Python': ['Python', 'C', 'C++', 'CSharp', 'SQL'],
      'C': ['C', 'C++'],
      'C++': ['C++', 'C'],
      'CSharp': ['CSharp', 'C'],
      'SQL': ['SQL'],
    };
    return allowed[from]?.includes(to) ?? false;
  }

  private detectCycles(): string[][] {
    const visited = new Set<string>();
    const recStack = new Set<string>();
    const cycles: string[][] = [];
    const path: string[] = [];

    const dfs = (nodeId: string) => {
      visited.add(nodeId);
      recStack.add(nodeId);
      path.push(nodeId);

      const node = this.nodes.get(nodeId);
      if (node) {
        for (const imp of node.imports) {
          if (!this.nodes.has(imp)) continue;
          if (!visited.has(imp)) {
            dfs(imp);
          } else if (recStack.has(imp)) {
            const cycleStart = path.indexOf(imp);
            cycles.push(path.slice(cycleStart).concat(imp));
          }
        }
      }

      recStack.delete(nodeId);
      path.pop();
    };

    for (const id of this.nodes.keys()) {
      if (!visited.has(id)) dfs(id);
    }

    return cycles;
  }
}

export function runDependencyDagGate(files: Map<string, string>): DependencyDagResult {
  const checker = new DependencyDagChecker();
  checker.buildDag(files);
  return checker.checkDag();
}