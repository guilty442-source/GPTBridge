import { zhTW } from "../../i18n/zhTW";
import type { SystemLight } from "./systemHealth";
import type { CheckLevel } from "../../types/health";

export type HealthSummary = {
  systemHealth: string;
  lastAudit: string;
  lastError: string;
  backupStatus: string;
};

export type ResourceSummaryItem = {
  label: string;
  value: string;
};

const healthText: Record<CheckLevel, string> = {
  normal: "\u826f\u597d",
  warning: "\u9700\u6ce8\u610f",
  error: "\u7570\u5e38",
  unknown: "\u5f85\u6aa2\u67e5",
};

const statusText: Record<CheckLevel, string> = {
  normal: "\u6b63\u5e38",
  warning: "\u9700\u6ce8\u610f",
  error: "\u7570\u5e38",
  unknown: "\u5f85\u6aa2\u67e5",
};

const findLight = (lights: SystemLight[], name: string) => lights.find((light) => light.name === name);

const formatBytes = (value: unknown) => {
  const bytes = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "\u5f85\u6aa2\u67e5";
  const mb = bytes / 1024 / 1024;
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)}GB`;
  return `${Math.round(mb)}MB`;
};

export const formatRelativeTime = (timestamp: number | null, now = Date.now()) => {
  if (!timestamp) return "\u5c1a\u672a\u81ea\u6aa2";
  const diffMs = Math.max(0, now - timestamp);
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "\u525b\u525b";
  if (minutes < 60) return `${minutes}\u5206\u9418\u524d`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}\u5c0f\u6642\u524d`;
  const days = Math.floor(hours / 24);
  return `${days}\u5929\u524d`;
};

export const buildHealthSummary = (
  overallLevel: CheckLevel,
  lights: SystemLight[],
  lastAuditAt: number | null,
  lastError: string,
): HealthSummary => {
  const storage = findLight(lights, zhTW.lightName.storage);
  return {
    systemHealth: healthText[overallLevel],
    lastAudit: formatRelativeTime(lastAuditAt),
    lastError: lastError || "\u7121",
    backupStatus: storage ? statusText[storage.level] : "\u5f85\u6aa2\u67e5",
  };
};

export const buildGlobalWarnings = (lights: SystemLight[], socketStatus: string) => {
  const warnings: string[] = [];
  const byName = (name: string) => findLight(lights, name);
  const backend = byName(zhTW.lightName.backend);
  const basicHealth = byName(zhTW.lightName.basicHealth);
  const storage = byName(zhTW.lightName.storage);
  const settings = byName(zhTW.lightName.settings);
  const warningText = (light: SystemLight, entry: string) =>
    `⚠ ${light.name}｜狀態：${statusText[light.level]}｜原因：${light.detail}｜入口：${entry}`;
  const detailWarningText = (label: string, level: CheckLevel, reason: string, entry: string) =>
    `⚠ ${label}｜狀態：${statusText[level]}｜原因：${reason}｜入口：${entry}`;

  if (backend?.level === "error") warnings.push(warningText(backend, zhTW.mode.rescue));
  if (socketStatus !== "Connected") warnings.push("⚠ WebSocket｜狀態：異常｜原因：連線中斷，正在重新連線｜入口：救援模式");
  for (const item of basicHealth?.details ?? []) {
    if (!item.level || item.level === "normal") continue;
    const entry = item.label === zhTW.lightName.backend || item.label === "WebSocket" ? zhTW.mode.rescue : zhTW.mode.settings;
    warnings.push(detailWarningText(item.label, item.level, item.value, entry));
  }
  if (storage && storage.level !== "normal") warnings.push(warningText(storage, zhTW.mode.settings));
  if (settings && settings.level !== "normal") warnings.push(warningText(settings, zhTW.mode.settings));

  return [...new Set(warnings)];
};

export const buildResourceSummary = (checks: { name: string; raw?: Record<string, unknown> }[]): ResourceSummaryItem[] => {
  const storage = checks.find((item) => item.name === "storage_maintenance" || item.name.includes("\u5099\u4efd"))?.raw ?? {};
  const cpuPercent = Number(storage.cpu_used_percent);
  const cpuStatus = storage.cpu_status === "normal" ? "\u6b63\u5e38" : storage.cpu_status === "warning" ? "\u9700\u6ce8\u610f" : storage.cpu_status === "error" ? "\u7570\u5e38" : "\u5f85\u6aa2\u67e5";
  const ramPercent = Number(storage.ram_used_percent);
  const ramStatus = storage.ram_status === "normal" ? "\u6b63\u5e38" : storage.ram_status === "warning" ? "\u9700\u6ce8\u610f" : storage.ram_status === "error" ? "\u7570\u5e38" : "\u5f85\u6aa2\u67e5";
  return [
    { label: "CPU", value: Number.isFinite(cpuPercent) ? `${cpuStatus} ${cpuPercent}%` : cpuStatus },
    { label: "RAM", value: Number.isFinite(ramPercent) ? `${ramStatus} ${ramPercent}%` : ramStatus },
    { label: "\u5c08\u6848", value: formatBytes(storage.project_size_bytes) },
    { label: "\u5099\u4efd", value: formatBytes(storage.backups_size_bytes ?? storage.backup_size_bytes) },
  ];
};
