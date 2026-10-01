/**
 * DependencyPolicy.js — C55 governed dependency DAG policy.
 *
 * Immutable declarative data only. No scanning, resolution, filesystem,
 * network, process, evidence, verdict or repair behavior may live here.
 * Consumers: checkers/DependencyDagChecker.js (READONLY-V1).
 */

const freezeDeep = (o) => {
  for (const v of Object.values(o)) {
    if (v && typeof v === 'object') freezeDeep(v);
  }
  return Object.freeze(o);
};

// C55 DEPENDENCY-POLICY-DAG, verbatim edge set:
//   javascript-esm => information_layer
//   c              => cpp
//   binding        => parser | vector | token | transform | memory
//   parser         => token | memory
//   vector         => memory
//   token          => memory
//   transform      => memory
//   memory         => none
// Every declared edge is listed; any directed edge absent here is an
// undeclared edge and is denied. Any directed cycle is FAIL even when
// each constituent edge is locally allowed.
export const DEPENDENCY_DAG = freezeDeep({
  'javascript-esm': ['information_layer'],
  c: ['cpp'],
  binding: ['parser', 'vector', 'token', 'transform', 'memory'],
  parser: ['token', 'memory'],
  vector: ['memory'],
  token: ['memory'],
  transform: ['memory'],
  memory: [],
});

// C55 CANONICAL-QUALIFICATION: javascript-esm>information_layer expands
// to the B32 canonical contract. Python is retired — no python>native
// or python-qualified expansion exists.
export const EDGE_QUALIFICATIONS = freezeDeep({
  'javascript-esm->information_layer': {
    contract: 'B32',
    expansion: 'ui-client>validated-shell-bridge>typed-application-core>information-layer',
    rules: ['B32'],
  },
});

// Canonical node-role vocabulary for edge endpoints.
export const NODE_ROLES = freezeDeep({
  'javascript-esm': 'ui-language',
  information_layer: 'information-layer',
  c: 'native-language',
  cpp: 'native-language',
  binding: 'native-component',
  parser: 'native-component',
  vector: 'native-component',
  token: 'native-component',
  transform: 'native-component',
  memory: 'native-component',
});

// Diagnostic rule ids used by the checker; the exact ids are part of the
// registered output contract (D64 unresolved-mandatory-edge fail-closed,
// C55 cycle/undeclared-edge FAIL).
export const RULE_IDS = freezeDeep({
  undeclaredEdge: 'C55:undeclared-dag-edge',
  cycle: 'C55:directed-cycle',
  unresolvedEdge: 'D64:unresolved-mandatory-edge',
  textOnlyProof: 'C55:text-only-dependency-proof',
});

export default freezeDeep({
  DEPENDENCY_DAG,
  EDGE_QUALIFICATIONS,
  NODE_ROLES,
  RULE_IDS,
});
