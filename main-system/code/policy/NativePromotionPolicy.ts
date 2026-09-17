/**
 * NativePromotionPolicy.ts ?ative Promotion Policy (A357/A358).
 *
 * Immutable data only. Stores per-capability latency_budget, cpu_budget,
 * memory_budget, throughput_target, minimum_meaningful_improvement.
 *
 * SOLE QUESTION: does an existing Python-owned capability have sufficient
 * evidence to add a C++ optimized executor?
 * FLOW: Python implementation > reproducible profiling > bottleneck confirmed > C++ candidate
 */

export interface NativePromotionPolicy {
  capabilityId: string;
  capabilityName: string;
  pythonOwner: string;

  // Performance budgets (must be met by Python implementation)
  latencyBudgetMs: number;           // Maximum acceptable latency
  cpuBudgetPercent: number;          // Maximum CPU usage
  memoryBudgetMb: number;            // Maximum memory usage
  throughputTarget: number;          // Minimum operations/second

  // Promotion criteria
  minimumMeaningfulImprovement: number;  // e.g., 2.0 = 2x improvement required
  requiresReproducibleProfile: boolean;  // Must have reproducible profiling evidence
  bottleneckConfirmed: boolean;          // Bottleneck must be confirmed before C++

  // Current Python implementation metrics (filled by profiling)
  currentLatencyMs?: number;
  currentCpuPercent?: number;
  currentMemoryMb?: number;
  currentThroughput?: number;

  // C++ candidate status
  cppCandidate?: {
    available: boolean;
    implementationPath?: string;
    expectedImprovement?: number;
    verifiedImprovement?: number;
  };

  // Governance
  promotionStatus: 'not-needed' | 'profiling' | 'bottleneck-confirmed' | 'cpp-candidate' | 'approved' | 'rejected';
  decidedAt?: string;
  decidedBy?: string;
}

// Initial policies for capabilities that might benefit from C++ optimization
export const NATIVE_PROMOTION_POLICIES: NativePromotionPolicy[] = [
  {
    capabilityId: 'memory-release',
    capabilityName: 'Working Set Release',
    pythonOwner: 'resource-dependency-sync-sub-sovereign',
    latencyBudgetMs: 10,
    cpuBudgetPercent: 5,
    memoryBudgetMb: 10,
    throughputTarget: 1000,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'cpp-candidate',
    cppCandidate: {
      available: true,
      implementationPath: 'native/core/memory.cpp',
      expectedImprovement: 5.0,
    },
  },
  {
    capabilityId: 'parser',
    capabilityName: 'Token Estimation / Text Analysis',
    pythonOwner: 'xingcheng-sovereign',
    latencyBudgetMs: 100,
    cpuBudgetPercent: 50,
    memoryBudgetMb: 200,
    throughputTarget: 10000,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'profiling',
  },
  {
    capabilityId: 'vector',
    capabilityName: 'Vector Compute (dot product, similarity, norm)',
    pythonOwner: 'xingcheng-sovereign',
    latencyBudgetMs: 50,
    cpuBudgetPercent: 80,
    memoryBudgetMb: 500,
    throughputTarget: 100000,
    minimumMeaningfulImprovement: 3.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'profiling',
  },
  {
    capabilityId: 'transformer',
    capabilityName: 'Transformation Compute (tensor operations, model inference)',
    pythonOwner: 'xingcheng-sovereign',
    latencyBudgetMs: 500,
    cpuBudgetPercent: 90,
    memoryBudgetMb: 2000,
    throughputTarget: 1000,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'profiling',
  },
  {
    capabilityId: 'token',
    capabilityName: 'Token Operations',
    pythonOwner: 'xingcheng-sovereign',
    latencyBudgetMs: 20,
    cpuBudgetPercent: 30,
    memoryBudgetMb: 100,
    throughputTarget: 50000,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'profiling',
  },
  {
    capabilityId: 'binding',
    capabilityName: 'Pybind11 Binding Overhead',
    pythonOwner: 'synchronization-sovereign',
    latencyBudgetMs: 5,
    cpuBudgetPercent: 10,
    memoryBudgetMb: 20,
    throughputTarget: 100000,
    minimumMeaningfulImprovement: 1.5,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'not-needed',
  },
  {
    capabilityId: 'hot-reload',
    capabilityName: 'Hot Reload Execution',
    pythonOwner: 'release-update-sync-sub-sovereign',
    latencyBudgetMs: 5000,
    cpuBudgetPercent: 20,
    memoryBudgetMb: 200,
    throughputTarget: 10,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'not-needed',
  },
  {
    capabilityId: 'connection-watchdog',
    capabilityName: 'Connection Health Watchdog',
    pythonOwner: 'health-maintenance-test-sub-sovereign',
    latencyBudgetMs: 100,
    cpuBudgetPercent: 10,
    memoryBudgetMb: 50,
    throughputTarget: 100,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'not-needed',
  },
  {
    capabilityId: 'automatic-cleanup',
    capabilityName: 'Main-System Internal Automatic Cleanup',
    pythonOwner: 'health-maintenance-test-sub-sovereign',
    latencyBudgetMs: 300000,
    cpuBudgetPercent: 50,
    memoryBudgetMb: 500,
    throughputTarget: 1,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'not-needed',
  },
  {
    capabilityId: 'auto-repair',
    capabilityName: 'Automatic Repair',
    pythonOwner: 'health-maintenance-test-sub-sovereign',
    latencyBudgetMs: 60000,
    cpuBudgetPercent: 30,
    memoryBudgetMb: 200,
    throughputTarget: 10,
    minimumMeaningfulImprovement: 2.0,
    requiresReproducibleProfile: true,
    bottleneckConfirmed: false,
    promotionStatus: 'not-needed',
  },
];

export class NativePromotionPolicyRegistry {
  private policies: Map<string, NativePromotionPolicy> = new Map();

  constructor(policies: NativePromotionPolicy[] = NATIVE_PROMOTION_POLICIES) {
    for (const p of policies) {
      this.policies.set(p.capabilityId, p);
    }
  }

  getPolicy(capabilityId: string): NativePromotionPolicy | undefined {
    return this.policies.get(capabilityId);
  }

  getAllPolicies(): NativePromotionPolicy[] {
    return Array.from(this.policies.values());
  }

  updatePolicy(capabilityId: string, updates: Partial<NativePromotionPolicy>): void {
    const policy = this.policies.get(capabilityId);
    if (policy) {
      Object.assign(policy, updates);
    }
  }

  // SOLE QUESTION: does an existing Python-owned capability have sufficient evidence to add a C++ optimized executor?
  evaluatePromotion(capabilityId: string): {
    eligible: boolean;
    reason: string;
    nextSteps: string[];
  } {
    const policy = this.policies.get(capabilityId);
    if (!policy) {
      return { eligible: false, reason: 'Policy not found', nextSteps: [] };
    }

    // Check if Python implementation meets budgets
    if (policy.currentLatencyMs !== undefined && policy.currentLatencyMs <= policy.latencyBudgetMs &&
        policy.currentCpuPercent !== undefined && policy.currentCpuPercent <= policy.cpuBudgetPercent &&
        policy.currentMemoryMb !== undefined && policy.currentMemoryMb <= policy.memoryBudgetMb &&
        policy.currentThroughput !== undefined && policy.currentThroughput >= policy.throughputTarget) {
      return {
        eligible: false,
        reason: 'Python implementation meets all budgets - no C++ optimization needed (A357)',
        nextSteps: ['Continue with Python implementation'],
      };
    }

    // Check if bottleneck is confirmed
    if (!policy.bottleneckConfirmed) {
      return {
        eligible: false,
        reason: 'Bottleneck not confirmed - requires reproducible profiling first (A357)',
        nextSteps: ['Run reproducible profiling', 'Confirm bottleneck in Python implementation'],
      };
    }

    // Check if C++ candidate exists
    if (!policy.cppCandidate?.available) {
      return {
        eligible: false,
        reason: 'No C++ candidate implementation available',
        nextSteps: ['Implement C++ candidate in native/core/'],
      };
    }

    // Check expected improvement
    const expectedImprovement = policy.cppCandidate.expectedImprovement || 0;
    if (expectedImprovement < policy.minimumMeaningfulImprovement) {
      return {
        eligible: false,
        reason: `Expected improvement (${expectedImprovement}x) below minimum meaningful improvement (${policy.minimumMeaningfulImprovement}x) (A358)`,
        nextSteps: ['Optimize C++ candidate further', 'Re-evaluate expected improvement'],
      };
    }

    return {
      eligible: true,
      reason: 'All promotion criteria met - C++ optimization approved',
      nextSteps: ['Submit for integration testing', 'Run Contract Parity Gate (A355)', 'Run Build ABI Repro Gate (A359)'],
    };
  }

  toJSON(): string {
    return JSON.stringify(Array.from(this.policies.values()), null, 2);
  }
}

export const nativePromotionPolicyRegistry = new NativePromotionPolicyRegistry();