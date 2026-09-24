/**
 * ContractParityChecker.ts —Contract Parity Gate (A355).
 *
 * Verifies TypeScript<->Python, Python<->Native, C ABI<->C++ implementation,
 * and Python/C ABI<->CSharp WindowsAdapter contract parity.
 */

export type ParityVerdict = 'PASS' | 'WARN' | 'FAIL';

export interface Contract {
  id: string;
  name: string;
  version: string;
  language: 'TypeScript' | 'Python' | 'C' | 'C++' | 'CSharp';
  schema: ContractSchema;
  owner: string;
}

export interface ContractSchema {
  types: TypeDefinition[];
  requests: RequestDefinition[];
  responses: ResponseDefinition[];
  events: EventDefinition[];
  errors: ErrorDefinition[];
}

export interface TypeDefinition {
  name: string;
  fields: FieldDefinition[];
}

export interface FieldDefinition {
  name: string;
  type: string;
  optional?: boolean;
  description?: string;
}

export interface RequestDefinition {
  name: string;
  payload: string;      // Type name
  response: string;     // Type name
}

export interface ResponseDefinition {
  name: string;
  fields: FieldDefinition[];
}

export interface EventDefinition {
  name: string;
  payload: string;      // Type name
}

export interface ErrorDefinition {
  code: string;
  message: string;
}

export interface ParityViolation {
  rule: string;
  message: string;
  contractId: string;
  languageA: string;
  languageB: string;
  severity: 'error' | 'warn';
}

export interface ContractParityResult {
  verdict: ParityVerdict;
  contracts: Contract[];
  violations: ParityViolation[];
}

// Contract boundary pairs per A355
export const CONTRACT_BOUNDARIES = [
  { from: 'TypeScript', to: 'Python', description: 'TypeScript contracts -> Python implementation (A355)' },
  { from: 'Python', to: 'C', description: 'Python -> Native C ABI (A355)' },
  { from: 'C', to: 'C++', description: 'C ABI -> C++ implementation (A355)' },
  { from: 'Python', to: 'CSharp', description: 'Python/C ABI -> CSharp WindowsAdapter (A355)' },
] as const;

export class ContractParityChecker {
  private contracts: Map<string, Contract> = new Map();

  registerContract(contract: Contract): void {
    this.contracts.set(contract.id, contract);
  }

  loadFromFiles(files: Map<string, string>): void {
    // Simplified: extract contracts from source files
    // In production, this would use AST parsing
  }

  checkParity(): ContractParityResult {
    const violations: ParityViolation[] = [];

    // Check each boundary pair
    for (const boundary of CONTRACT_BOUNDARIES) {
      const fromContracts = this.getContractsByLanguage(boundary.from);
      const toContracts = this.getContractsByLanguage(boundary.to);

      for (const fromContract of fromContracts) {
        const matchingTo = toContracts.find(c => this.contractsMatch(fromContract, c));
        if (!matchingTo) {
          violations.push({
            rule: 'contract_parity',
            message: `${boundary.from} contract ${fromContract.name} has no matching ${boundary.to} implementation`,
            contractId: fromContract.id,
            languageA: boundary.from,
            languageB: boundary.to,
            severity: 'error',
          });
        } else {
          // Check schema parity
          const schemaViolations = this.checkSchemaParity(fromContract, matchingTo);
          violations.push(...schemaViolations);
        }
      }
    }

    // Check for orphaned implementations (implementation without contract)
    for (const contract of this.contracts.values()) {
      if (contract.language !== 'TypeScript') {
        const hasContract = this.getContractsByLanguage('TypeScript').some(
          c => this.contractsMatch(c, contract)
        );
        if (!hasContract) {
          violations.push({
            rule: 'orphaned_implementation',
            message: `${contract.language} implementation ${contract.name} has no TypeScript contract`,
            contractId: contract.id,
            languageA: 'TypeScript',
            languageB: contract.language,
            severity: 'warn',
          });
        }
      }
    }

    const verdict = violations.some(v => v.severity === 'error') ? 'FAIL' :
                    violations.some(v => v.severity === 'warn') ? 'WARN' : 'PASS';

    return { verdict, contracts: Array.from(this.contracts.values()), violations };
  }

  private getContractsByLanguage(language: string): Contract[] {
    return Array.from(this.contracts.values()).filter(c => c.language === language);
  }

  private contractsMatch(a: Contract, b: Contract): boolean {
    // Simplified: match by name and version
    return a.name === b.name && a.version === b.version;
  }

  private checkSchemaParity(a: Contract, b: Contract): ParityViolation[] {
    const violations: ParityViolation[] = [];

    // Check request/response parity
    for (const req of a.schema.requests) {
      const matching = b.schema.requests.find(r => r.name === req.name);
      if (!matching) {
        violations.push({
          rule: 'request_parity',
          message: `Request ${req.name} missing in ${b.language} implementation`,
          contractId: a.id,
          languageA: a.language,
          languageB: b.language,
          severity: 'error',
        });
      }
    }

    // Check type definitions
    for (const type of a.schema.types) {
      const matching = b.schema.types.find(t => t.name === type.name);
      if (!matching) {
        violations.push({
          rule: 'type_parity',
          message: `Type ${type.name} missing in ${b.language} implementation`,
          contractId: a.id,
          languageA: a.language,
          languageB: b.language,
          severity: 'error',
        });
      } else {
        // Check field parity
        for (const field of type.fields) {
          const matchingField = matching.fields.find(f => f.name === field.name);
          if (!matchingField) {
            violations.push({
              rule: 'field_parity',
              message: `Field ${type.name}.${field.name} missing in ${b.language}`,
              contractId: a.id,
              languageA: a.language,
              languageB: b.language,
              severity: 'error',
            });
          } else if (matchingField.type !== field.type) {
            violations.push({
              rule: 'field_type_parity',
              message: `Field ${type.name}.${field.name} type mismatch: ${a.language}=${field.type}, ${b.language}=${matchingField.type}`,
              contractId: a.id,
              languageA: a.language,
              languageB: b.language,
              severity: 'error',
            });
          }
        }
      }
    }

    return violations;
  }
}

export function runContractParityGate(contracts: Contract[]): ContractParityResult {
  const checker = new ContractParityChecker();
  for (const c of contracts) checker.registerContract(c);
  return checker.checkParity();
}