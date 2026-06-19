import { zhTW } from "../../i18n/zhTW";
import type { AuditCheckItem, CheckLevel } from "../../types/health";

export type SystemLight = {
  name: string;
  level: CheckLevel;
  detail: string;
  checkedAt: number | null;
  details?: Array<{ label: string; value: string; level?: CheckLevel }>;
  topSources?: Array<{ path: string; size: string }>;
};

type ConfigMap = Record<string, unknown>;

const statusDetail: Record<string, string> = {
  AUTHENTICATED: "\u5df2\u767b\u5165",
  UNAUTHENTICATED: "\u9700\u767b\u5165",
  UNOPENED: "\u672a\u958b\u555f\uff0c\u9700\u8981\u6642\u518d\u958b",
  CLOSED: "\u5df2\u95dc\u9589\uff0c\u9700\u8981\u6642\u518d\u958b",
  RATE_LIMITED: "\u88ab\u9650\u5236",
  WARNING: "\u9700\u6ce8\u610f",
  ERROR: "\u7570\u5e38",
  UNKNOWN: "\u5f85\u6aa2\u67e5",
};

const hardProviderStates = new Set(["ERROR", "UNAUTHENTICATED", "RATE_LIMITED"]);
const openOnDemandStates = new Set(["AUTHENTICATED", "UNOPENED", "CLOSED", "UNKNOWN"]);

const hasText = (value: unknown) => typeof value === "string" && value.trim().length > 0;

const providerLevel = (value: string, url: unknown): CheckLevel => {
  if (!hasText(url)) return "error";
  if (hardProviderStates.has(value)) return "error";
  if (openOnDemandStates.has(value)) return "normal";
  return "warning";
};

const providerDetail = (value: string, url: unknown) => {
  if (!hasText(url)) return "\u7f3a\u5c11 URL";
  return statusDetail[value] ?? statusDetail.UNKNOWN;
};

const coerceLevel = (value: unknown): CheckLevel | null =>
  value === "normal" || value === "warning" || value === "error" || value === "unknown" ? value : null;

const maxLevel = (levels: CheckLevel[]): CheckLevel => {
  if (levels.includes("error")) return "error";
  if (levels.includes("warning") || levels.includes("unknown")) return "warning";
  return "normal";
};

const formatBytes = (value: unknown) => {
  const bytes = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "\u5f85\u6aa2\u67e5";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  const gb = bytes / 1024 / 1024 / 1024;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  const mb = bytes / 1024 / 1024;
  return mb >= 10 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`;
};

const storageLevel = (check?: AuditCheckItem): CheckLevel => {
  if (check?.level === "error") return "error";
  const levels: CheckLevel[] = [];
  const raw = check?.raw ?? {};
  const total = coerceLevel(raw.total_size_level);
  const garbage = coerceLevel(raw.garbage_level);
  if (total) levels.push(total);
  if (garbage) levels.push(garbage);
  const systemSizes = raw.system_sizes && typeof raw.system_sizes === "object" ? raw.system_sizes as Record<string, unknown> : {};
  Object.values(systemSizes).forEach((item) => {
    if (item && typeof item === "object") {
      const level = coerceLevel((item as Record<string, unknown>).level);
      if (level) levels.push(level);
    }
  });
  return levels.length > 0 ? maxLevel(levels) : "normal";
};

const systemSizeLabels: Record<string, string> = {
  info_center: "\u4ecb\u9762\u7cfb\u7d71\u5927\u5c0f",
  core_system: "\u6838\u5fc3\u7cfb\u7d71\u5927\u5c0f",
  design_mode: "\u8a2d\u8a08\u6a21\u5f0f\u5927\u5c0f",
  rescue_mode: "\u6551\u63f4\u6a21\u5f0f\u5927\u5c0f",
  developer_mode: "\u958b\u767c\u8005\u6a21\u5f0f\u5927\u5c0f",
  settings_system: "\u8a2d\u5b9a\u5927\u5c0f",
  child_tools: "\u5b50\u5de5\u5177\u5217\u8868\u5927\u5c0f",
};

const storageDetails = (check?: AuditCheckItem) => {
  const raw = check?.raw ?? {};
  const details: Array<{ label: string; value: string; level?: CheckLevel }> = [];
  const systemSizes = raw.system_sizes && typeof raw.system_sizes === "object" ? raw.system_sizes as Record<string, unknown> : {};
  Object.entries(systemSizeLabels).forEach(([key, label]) => {
    const item = systemSizes[key] && typeof systemSizes[key] === "object" ? systemSizes[key] as Record<string, unknown> : {};
    details.push({
      label,
      value: formatBytes(item.size_bytes),
      level: coerceLevel(item.level) ?? "unknown",
    });
  });
  details.push({
    label: "\u7e3d\u9ad4\u7a4d",
    value: formatBytes(raw.total_size_bytes ?? raw.project_size_bytes),
    level: coerceLevel(raw.total_size_level) ?? "unknown",
  });
  details.push({
    label: "\u5783\u573e\u6a94\u6848",
    value: formatBytes(raw.cleanup_size_bytes),
    level: coerceLevel(raw.garbage_level) ?? "unknown",
  });
  return details;
};

const storageTopSources = (check?: AuditCheckItem) => {
  const sources = Array.isArray(check?.raw?.largest_sources) ? check.raw.largest_sources : [];
  return sources.slice(0, 10).map((item) => {
    const source = item && typeof item === "object" ? item as Record<string, unknown> : {};
    return {
      path: String(source.path ?? ""),
      size: formatBytes(source.size_bytes),
    };
  }).filter((item) => item.path);
};

const browserLevel = (check?: AuditCheckItem): CheckLevel => {
  if (check?.level === "error") return "error";
  return "normal";
};

const settingsLevel = (config: ConfigMap): CheckLevel => {
  if (Object.keys(config).length === 0) return "warning";
  const requiredKeys = [
    "chatgpt_main_url",
    "gemini_main_url",
    "design_project_output_dir",
  ];
  if (requiredKeys.some((key) => !hasText(config[key]))) return "error";
  if (config.core_lock_enabled !== true) return "error";
  if (String(config.target_platform ?? "Windows 11") !== "Windows 11") return "error";
  return "normal";
};

const settingsDetail = (config: ConfigMap) => {
  const level = settingsLevel(config);
  if (config.core_lock_enabled !== true) return "Core Lock \u672a\u555f\u7528";
  if (String(config.target_platform ?? "Windows 11") !== "Windows 11") return "\u76ee\u6a19\u5e73\u53f0\u4e0d\u662f Windows 11";
  if (level === "error") return "\u8a2d\u5b9a\u7f3a\u5c11\u5fc5\u8981\u6b04\u4f4d";
  if (level === "warning") return "\u8a2d\u5b9a\u5f85\u78ba\u8a8d";
  return "\u8a2d\u5b9a\u6b63\u5e38";
};

const findAuditCheck = (checks: AuditCheckItem[], terms: string[]) =>
  checks.find((item) => terms.some((term) => item.name.includes(term)));

export const buildSystemLights = (
  status: string,
  chatgptStatus: string,
  geminiStatus: string,
  auditChecks: AuditCheckItem[],
  config: ConfigMap,
  checkedAt: number | null,
): SystemLight[] => {
  const startupCheck = findAuditCheck(auditChecks, ["startup_status", "\u555f\u52d5\u72c0\u614b"]);
  const storageCheck = findAuditCheck(auditChecks, ["storage_maintenance", "\u5099\u4efd", "\u6e05\u7406", "\u9ad4\u7a4d"]);
  const browserState = String(startupCheck?.raw?.browser_context ?? "closed");
  const cleanupCount = storageCheck?.cleanupCount ?? 0;
  const backendLevel: CheckLevel = status === "Connected" ? "normal" : "error";
  const chatgptLevel = providerLevel(chatgptStatus, config.chatgpt_main_url);
  const geminiLevel = providerLevel(geminiStatus, config.gemini_main_url);
  const chatgptDetail = providerDetail(chatgptStatus, config.chatgpt_main_url);
  const geminiDetail = providerDetail(geminiStatus, config.gemini_main_url);
  const storageDetailsList = storageDetails(storageCheck);
  const storageTotal = formatBytes(storageCheck?.raw?.total_size_bytes ?? storageCheck?.raw?.project_size_bytes);
  const storageGarbage = formatBytes(storageCheck?.raw?.cleanup_size_bytes);
  const settingsLightLevel = settingsLevel(config);
  const browserLightLevel = browserLevel(startupCheck);
  const storageLightLevel = storageLevel(storageCheck);
  const basicDetails: Array<{ label: string; value: string; level?: CheckLevel }> = [
    {
      label: zhTW.lightName.backend,
      value: status === "Connected" ? "\u5f8c\u7aef\u5df2\u555f\u52d5" : "\u5f8c\u7aef\u672a\u9023\u7dda",
      level: backendLevel,
    },
    {
      label: "WebSocket",
      value: status === "Connected" ? "WebSocket \u5df2\u9023\u7dda" : "WebSocket \u672a\u9023\u7dda",
      level: backendLevel,
    },
    {
      label: "ChatGPT",
      value: chatgptDetail,
      level: chatgptLevel,
    },
    {
      label: "Gemini",
      value: geminiDetail,
      level: geminiLevel,
    },
    {
      label: zhTW.lightName.browser,
      value: browserState === "ready" ? "\u80cc\u666f\u700f\u89bd\u5668\u5df2\u5c31\u7dd2" : "\u9700\u8981\u6642\u518d\u958b",
      level: browserLightLevel,
    },
    {
      label: zhTW.lightName.storage,
      value: cleanupCount > 0 ? `儲存 ${storageTotal}，可清理 ${storageGarbage}` : `儲存 ${storageTotal}`,
      level: storageLightLevel,
    },
    {
      label: zhTW.lightName.settings,
      value: settingsDetail(config),
      level: settingsLightLevel,
    },
  ];
  const basicAttention = basicDetails.filter((item) => item.level && item.level !== "normal");
  const basicHealth = maxLevel([
    backendLevel,
    chatgptLevel,
    geminiLevel,
    browserLightLevel,
    storageLightLevel,
    settingsLightLevel,
  ]);

  return [
    {
      name: zhTW.lightName.backend,
      level: backendLevel,
      detail: status === "Connected" ? "\u5f8c\u7aef\u5df2\u555f\u52d5" : "\u5f8c\u7aef\u672a\u9023\u7dda",
      checkedAt,
    },
    {
      name: "WebSocket",
      level: status === "Connected" ? "normal" : "error",
      detail: status === "Connected" ? "WebSocket \u5df2\u9023\u7dda" : "WebSocket \u672a\u9023\u7dda",
      checkedAt,
    },
    {
      name: zhTW.lightName.browser,
      level: browserLightLevel,
      detail: browserState === "ready" ? "\u80cc\u666f\u700f\u89bd\u5668\u5df2\u5c31\u7dd2" : "\u9700\u8981\u6642\u518d\u958b",
      checkedAt,
    },
    {
      name: zhTW.lightName.storage,
      level: storageLightLevel,
      detail: cleanupCount > 0 ? `儲存 ${storageTotal}，可清理 ${storageGarbage}` : `儲存 ${storageTotal}`,
      checkedAt,
      details: storageDetailsList,
      topSources: storageTopSources(storageCheck),
    },
    {
      name: zhTW.lightName.settings,
      level: settingsLightLevel,
      detail: settingsDetail(config),
      checkedAt,
    },
    {
      name: zhTW.lightName.basicHealth,
      level: basicHealth,
      detail: basicAttention.length > 0
        ? basicAttention.map((item) => `${item.label}：${item.value}`).join("；")
        : "\u57fa\u672c\u5065\u5eb7\u6b63\u5e38",
      checkedAt,
      details: basicDetails,
    },
  ];
};

export const overallLevelFromLights = (lights: SystemLight[]): CheckLevel => {
  if (lights.some((light) => light.level === "error")) return "error";
  if (lights.some((light) => light.level === "warning" || light.level === "unknown")) return "warning";
  return "normal";
};
