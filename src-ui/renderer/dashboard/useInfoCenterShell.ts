import { useMemo, useState } from "react";
import { useBackendSocket } from "../hooks/useBackendSocket";
import { scopedSendCommand } from "../core-system/core/commandScopes";
import { buildGlobalWarnings, buildHealthSummary, buildResourceSummary } from "../core-system/core/healthOverview";
import { trimLogGroups } from "../core-system/core/logGroups";
import { buildSystemLights, overallLevelFromLights } from "../core-system/core/systemHealth";
import { zhTW } from "../i18n/zhTW";
import type { AppMode } from "../types/ui";

export const MODE_LABELS: Record<AppMode, string> = {
  design: zhTW.mode.design,
  rescue: zhTW.mode.rescue,
  developer: zhTW.mode.developer,
  settings: zhTW.mode.settings,
};

const overviewLabel = (level: ReturnType<typeof overallLevelFromLights>) => {
  if (level === "error") return zhTW.health.systemError;
  if (level === "warning" || level === "unknown") return zhTW.health.systemWarning;
  return zhTW.health.systemNormal;
};

export const useInfoCenterShell = () => {
  const backend = useBackendSocket();
  const [mode, setMode] = useState<AppMode | null>(null);

  const systemLights = useMemo(
    () => buildSystemLights(backend.status, backend.chatgptStatus, backend.geminiStatus, backend.auditChecks, backend.config, backend.lastStatusAt),
    [backend.status, backend.chatgptStatus, backend.geminiStatus, backend.auditChecks, backend.config, backend.lastStatusAt],
  );
  const systemOverview = useMemo(() => overallLevelFromLights(systemLights), [systemLights]);
  const healthSummary = useMemo(
    () => buildHealthSummary(systemOverview, systemLights, backend.lastAuditAt, backend.lastError),
    [systemOverview, systemLights, backend.lastAuditAt, backend.lastError],
  );
  const globalWarnings = useMemo(
    () => buildGlobalWarnings(systemLights, backend.status),
    [systemLights, backend.status],
  );
  const resourceSummary = useMemo(() => buildResourceSummary(backend.auditChecks), [backend.auditChecks]);
  const currentModeLabel = mode ? MODE_LABELS[mode] : zhTW.mode.interfaceSystem;

  return {
    mode,
    setMode,
    modeLabels: MODE_LABELS,
    systemLights,
    systemOverview,
    systemOverviewLabel: overviewLabel(systemOverview),
    healthSummary,
    globalWarnings,
    resourceSummary,
    recentEvents: backend.recentEvents,
    identity: {
      name: zhTW.app.platformName,
      version: "v1.0",
      baseline: "Stable",
      currentMode: currentModeLabel,
    },
    design: {
      chatgptAnswers: backend.chatgptAnswers.slice(-10),
      geminiAnswers: backend.geminiAnswers.slice(-10),
      devHistory: backend.logGroups.design.slice(-200),
      designDiff: backend.designDiff,
      childFile: backend.childFile,
      progress: backend.taskProgress.design,
      sendCommand: scopedSendCommand("design", backend.sendCommand),
    },
    rescue: {
      auditRunning: backend.auditRunning,
      runtimeLog: backend.logGroups.core.slice(-20),
      progress: backend.taskProgress.rescue,
      sendCommand: scopedSendCommand("rescue", backend.sendCommand),
    },
    developer: {
      developerLog: backend.logGroups.developer.slice(-200),
      progress: backend.taskProgress.developer,
      sendCommand: scopedSendCommand("developer", backend.sendCommand),
    },
    settings: {
      config: backend.config,
      coreLog: trimLogGroups(backend.logGroups).core,
      backupRecords: backend.backupRecords,
      currentAccounts: backend.currentAccounts,
      aiCostProfiles: backend.aiCostProfiles,
      sendCommand: scopedSendCommand("settings", backend.sendCommand),
      clearLog: backend.clearLog,
    },
  };
};
