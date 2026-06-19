export type CheckLevel = "normal" | "warning" | "error" | "unknown";

export interface AuditCheckItem {
  name: string;
  level: CheckLevel;
  message: string;
  cleanupCount?: number;
  raw?: Record<string, unknown>;
}
