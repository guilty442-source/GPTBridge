export type AppMode = "design" | "rescue" | "developer" | "settings";

export type SendCommand = (command: string, payload?: Record<string, unknown>) => void;

export type LogCategory = "core" | "design" | "developer";

export type LogGroups = Record<LogCategory, string[]>;

export type TaskProgress = {
  taskId: string;
  command: string;
  mode: string;
  type?: string;
  status: string;
  stage: string;
  phase?: string;
  percent: number;
  timeout?: number;
  cancelable?: boolean;
  logRef?: string;
  message?: string;
};

export type BackupRecord = {
  name: string;
  path: string;
  size_bytes: number;
  modified_at: number;
  file_count: number;
};

export type BackupRecords = {
  design: BackupRecord[];
  mother: BackupRecord[];
};

export type ProviderAccounts = {
  chatgpt: string;
  gemini: string;
};

export type AiCostProfile = {
  level: number;
  preferred_provider: string;
  fallback_provider: string;
  allow_dual_parallel: boolean;
  allow_dual_collaboration: boolean;
  max_cross_review_rounds: number;
  description: string;
};

export type AiCostProfiles = Record<string, AiCostProfile>;

export type ResourceSummaryItem = {
  label: string;
  value: string;
};
