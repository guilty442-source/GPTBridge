/**
 * GovernanceCheckerRegistry.js — C55 governed checker registry.
 *
 * Registers exact checker id + policy id + trigger + input/output schema
 * + rule set + version. This module performs NO scan, decision, verdict
 * or repair — it is registration data plus identity lookup only. Any
 * mutation of these records outside a governed codex amendment is a
 * violation.
 */

const freezeDeep = (o) => {
  for (const v of Object.values(o)) {
    if (v && typeof v === 'object') freezeDeep(v);
  }
  return Object.freeze(o);
};

export const CHECKER_REGISTRATIONS = freezeDeep([
  {
    checker_id: 'language-boundary-checker',
    checker_path: 'code/checkers/LanguageBoundaryChecker.js',
    policy_id: 'language-policy',
    policy_path: 'code/policy/LanguagePolicy.js',
    trigger: 'save>pre-commit>integration>release',
    input_schema: 'language-boundary-evidence/v1',
    output_schema: 'language-boundary-verdict/v1',
    rule_set: ['D62', 'B166', 'A352', 'C55'],
    version: '1.0.0',
    mode: 'READONLY-V1',
  },
  {
    checker_id: 'dependency-dag-checker',
    checker_path: 'code/checkers/DependencyDagChecker.js',
    policy_id: 'dependency-policy',
    policy_path: 'code/policy/DependencyPolicy.js',
    trigger: 'save>pre-commit>integration>release',
    input_schema: 'resolved-dependency-edge-evidence/v1',
    output_schema: 'dependency-dag-verdict/v1',
    rule_set: ['C55', 'D64'],
    version: '1.0.0',
    mode: 'READONLY-V1',
  },
]);

const byId = new Map(
  CHECKER_REGISTRATIONS.map((r) => [r.checker_id, r]),
);

/** Identity lookup only. Returns the frozen registration record or
 * null; performs no scan, decision or repair. */
export function lookup(checkerId) {
  return byId.get(checkerId) ?? null;
}

/** Registered checker ids in declaration order. */
export function registeredIds() {
  return CHECKER_REGISTRATIONS.map((r) => r.checker_id);
}

export default freezeDeep({
  CHECKER_REGISTRATIONS,
});
