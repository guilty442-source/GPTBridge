import type { AuditCheckItem, CheckLevel } from "../../types/health";

export const levelFromPayload = (payload: Record<string, unknown>): CheckLevel => {
  if (payload.ok === true) return "normal";
  if (payload.ok === false) return "error";
  if (payload.status === "WARNING") return "warning";
  if (payload.status === "SUCCESS") return "normal";
  return "unknown";
};

export const buildAuditChecks = (payload: Record<string, unknown>): AuditCheckItem[] => {
  const checks = payload.checks;
  if (!checks || typeof checks !== "object") return [];

  return Object.entries(checks as Record<string, unknown>).map(([name, raw]) => {
    const item = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
    const cleanupRecommendations = Array.isArray(item.cleanup_recommendations) ? item.cleanup_recommendations : [];
    return {
      name,
      level: levelFromPayload(item),
      message: String(item.message ?? item.error ?? (item.ok === false ? "abnormal" : "normal")),
      cleanupCount: cleanupRecommendations.length,
      raw: item,
    };
  });
};

export const systemHealthFromAudit = (payload: Record<string, unknown>): CheckLevel => {
  if (payload.ok === false || payload.status === "ERROR") return "error";
  if (payload.status === "WARNING") return "warning";
  if (payload.ok === true || payload.status === "SUCCESS") return "normal";
  return "unknown";
};
