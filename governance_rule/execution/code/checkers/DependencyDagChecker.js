/**
 * DependencyDagChecker.js — C55/D64 dependency DAG gate.
 *
 * READONLY-V1: scan + resolve + classify + compare + evidence +
 * PASS/WARN/FAIL only. No mutation, no repair, no policy authority.
 *
 * D64: dependency evidence must be resolved semantically by the
 * authoritative resolvers upstream — this checker consumes a resolved
 * edge list (canonical node ids, not filenames or text matches) and
 * verifies it against DependencyPolicy.DEPENDENCY_DAG. An unresolved
 * mandatory edge fails closed; a text-only claim is not evidence.
 *
 * Input: an evidence object
 *   {
 *     edges: [{ source, target }],          // resolved canonical roles
 *     unresolved?: [{ source, target, required }], // resolver gaps
 *     claims?: [...],                        // text-only claims (denied)
 *   }
 *
 * Output: { verdict: PASS|WARN|FAIL, violations: [...], cycles: [[...]] }
 * Every FAIL row carries the C55 diagnostic contract:
 *   source:<canonical path>, target:<canonical path>,
 *   edge:<source-role>-><target-role>, rule:<exact ids>,
 *   reason:<typed human-readable boundary>, expected:<canonical path>
 */

import DependencyPolicy from '../policy/DependencyPolicy.js';

const { DEPENDENCY_DAG, EDGE_QUALIFICATIONS, RULE_IDS } = DependencyPolicy;

function violation({ source, target = 'none', edge = 'none->none', rule,
                     reason, expected }) {
  return { source, target, edge, rule, reason, expected };
}

/** Depth-first cycle enumeration over the resolved edge set. Every
 * directed cycle is a FAIL regardless of per-edge legality (C55). */
export function findCycles(edges) {
  const adj = new Map();
  for (const { source, target } of edges) {
    if (!adj.has(source)) adj.set(source, []);
    adj.get(source).push(target);
  }
  const visited = new Set();
  const inStack = new Set();
  const path = [];
  const cycles = [];

  const dfs = (node) => {
    visited.add(node);
    inStack.add(node);
    path.push(node);
    for (const next of adj.get(node) ?? []) {
      if (!visited.has(next)) {
        dfs(next);
      } else if (inStack.has(next)) {
        cycles.push(path.slice(path.indexOf(next)).concat(next));
      }
    }
    inStack.delete(node);
    path.pop();
  };

  for (const node of adj.keys()) {
    if (!visited.has(node)) dfs(node);
  }
  return cycles;
}

/** Verify a resolved dependency evidence set. */
export function checkDag(evidence) {
  const violations = [];
  const edges = evidence?.edges ?? [];

  // C55: undeclared edge is denied — every resolved edge must exist in
  // the policy DAG.
  for (const { source, target } of edges) {
    const declared = DEPENDENCY_DAG[source];
    if (!declared || !declared.includes(target)) {
      violations.push(violation({
        source,
        target,
        edge: `${source}->${target}`,
        rule: RULE_IDS.undeclaredEdge,
        reason: 'edge absent from DEPENDENCY-POLICY-DAG',
        expected: 'a declared edge in DependencyPolicy.DEPENDENCY_DAG',
      }));
    }
  }

  // D64: an unresolved mandatory edge fails closed.
  for (const gap of evidence?.unresolved ?? []) {
    if (gap.required === false) continue;
    violations.push(violation({
      source: gap.source,
      target: gap.target ?? 'unresolved',
      edge: `${gap.source}->${gap.target ?? 'unresolved'}`,
      rule: RULE_IDS.unresolvedEdge,
      reason: 'mandatory edge not semantically resolved by the owner resolver',
      expected: 'resolved canonical edge evidence from the authoritative resolver',
    }));
  }

  // C55: text-only dependency proof is denied.
  for (const claim of evidence?.claims ?? []) {
    violations.push(violation({
      source: claim.source ?? 'unknown',
      target: claim.target ?? 'unknown',
      edge: `${claim.source ?? 'unknown'}->${claim.target ?? 'unknown'}`,
      rule: RULE_IDS.textOnlyProof,
      reason: 'text-only dependency claim is not resolved evidence',
      expected: 'resolved edge evidence, not a text match',
    }));
  }

  const cycles = findCycles(edges);
  for (const cycle of cycles) {
    violations.push(violation({
      source: cycle[0],
      target: cycle[cycle.length - 1],
      edge: cycle.join('->'),
      rule: RULE_IDS.cycle,
      reason: 'directed cycle is FAIL even when each edge is locally allowed',
      expected: 'an acyclic resolved dependency graph',
    }));
  }

  // Qualified expansion evidence: edges carrying a canonical
  // qualification must match the registered expansion verbatim.
  for (const { source, target, qualification } of edges) {
    if (!qualification) continue;
    const key = `${source}->${target}`;
    const reg = EDGE_QUALIFICATIONS[key];
    if (!reg || reg.expansion !== qualification) {
      violations.push(violation({
        source,
        target,
        edge: key,
        rule: 'C55:canonical-qualification',
        reason: `unregistered qualification "${qualification}"`,
        expected: reg ? reg.expansion : 'no qualified expansion exists',
      }));
    }
  }

  return {
    verdict: violations.length ? 'FAIL' : 'PASS',
    violations,
    cycles,
    edgeCount: edges.length,
  };
}

export const CHECKER_ID = 'dependency-dag-checker';
export const CHECKER_VERSION = '1.0.0';
export const RULES = Object.freeze(['C55', 'D64']);
