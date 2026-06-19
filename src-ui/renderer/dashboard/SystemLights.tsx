import React, { useMemo, useState } from "react";
import { HealthBadge } from "../core-system/shared/HealthBadge";
import { zhTW } from "../i18n/zhTW";
import type { SystemLight } from "../core-system/core/systemHealth";
import type { HealthSummary, ResourceSummaryItem } from "../core-system/core/healthOverview";
import type { CheckLevel } from "../types/health";

type SystemLightsProps = {
  lights: SystemLight[];
  summary: HealthSummary;
  warnings: string[];
  resourceSummary: ResourceSummaryItem[];
  recentEvents: string[];
  identity: {
    name: string;
    version: string;
    baseline: string;
    currentMode: string;
  };
  onOpenRescue: () => void;
};

const levelStatus: Record<CheckLevel, string> = {
  normal: zhTW.health.normal,
  warning: zhTW.health.warning,
  error: zhTW.health.error,
  unknown: zhTW.health.unknown,
};

const formatClock = (timestamp: number | null) => {
  if (!timestamp) return zhTW.health.pending;
  return new Intl.DateTimeFormat("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
};

const lightText = (level: CheckLevel) => {
  if (level === "error") return zhTW.health.error;
  if (level === "warning") return zhTW.health.warning;
  if (level === "unknown") return zhTW.health.unknown;
  return zhTW.health.normal;
};

export const SystemLights: React.FC<SystemLightsProps> = ({
  lights,
  summary,
  warnings,
  resourceSummary,
  recentEvents,
  identity,
  onOpenRescue,
}) => {
  const [selectedName, setSelectedName] = useState("");
  const selectedLight = useMemo(
    () => lights.find((light) => light.name === selectedName) ?? lights[0] ?? null,
    [lights, selectedName],
  );
  const suggestion = selectedLight?.level === "error" ? zhTW.interfaceSystem.goRescue : zhTW.interfaceSystem.dailyCenter;
  const selectedBasicHealth = selectedLight?.name === zhTW.lightName.basicHealth;

  return (
    <section className="section system-mode">
      <div className="identity-strip">
        <strong>{`${identity.name} ${identity.version}`}</strong>
        <span>{identity.baseline}</span>
        <span>{`${zhTW.app.currentLabel}${identity.currentMode}`}</span>
      </div>

      <div className="summary-header">
        <h2>{zhTW.interfaceSystem.overview}</h2>
        <span>{zhTW.interfaceSystem.statusOnly}</span>
      </div>

      <div className="overview-fold-stack">
        <details className="overview-fold" open>
          <summary>{zhTW.interfaceSystem.systemLights}</summary>
          <div className="system-light-grid">
            {lights.map((light) => (
              <button
                key={light.name}
                type="button"
                className={`system-light-card ${selectedLight?.name === light.name ? "active" : ""}`}
                onClick={() => setSelectedName(light.name)}
                title={`${zhTW.interfaceSystem.reason}: ${light.detail}\n${zhTW.interfaceSystem.lastCheck}: ${formatClock(light.checkedAt)}\n${zhTW.interfaceSystem.suggestion}: ${light.level === "error" ? zhTW.interfaceSystem.goRescue : zhTW.interfaceSystem.dailyCenter}`}
              >
                <div>
                  <strong>{light.name}</strong>
                  <span className="system-light-detail">{light.detail}</span>
                </div>
                <HealthBadge level={light.level} />
              </button>
            ))}
          </div>

          <div className="light-detail-panel">
            {selectedLight ? (
              <>
                <strong>{selectedLight.name}</strong>
                <dl>
                  <div>
                    <dt>{zhTW.interfaceSystem.status}</dt>
                    <dd>{levelStatus[selectedLight.level]}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.light}</dt>
                    <dd>{lightText(selectedLight.level)}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.reason}</dt>
                    <dd>{selectedLight.detail}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.lastCheck}</dt>
                    <dd>{formatClock(selectedLight.checkedAt)}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.impact}</dt>
                    <dd>{selectedLight.name}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.suggestion}</dt>
                    <dd>{suggestion}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.entry}</dt>
                    <dd>{selectedLight.level === "error" ? zhTW.mode.rescue : zhTW.mode.interfaceSystem}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.logSummary}</dt>
                    <dd>{selectedLight.detail}</dd>
                  </div>
                  <div>
                    <dt>{zhTW.interfaceSystem.needsRescue}</dt>
                    <dd>{selectedLight.level === "error" ? zhTW.common.yes : zhTW.common.no}</dd>
                  </div>
                </dl>
                {selectedBasicHealth && (
                  <div className="light-health-summary">
                    <div className="health-summary-grid">
                      <div>
                        <span>{zhTW.interfaceSystem.systemHealth}</span>
                        <strong>{summary.systemHealth}</strong>
                      </div>
                      <div>
                        <span>{zhTW.interfaceSystem.lastAudit}</span>
                        <strong>{summary.lastAudit}</strong>
                      </div>
                      <div>
                        <span>{zhTW.interfaceSystem.lastError}</span>
                        <strong>{summary.lastError}</strong>
                      </div>
                      <div>
                        <span>{zhTW.interfaceSystem.backupStatus}</span>
                        <strong>{summary.backupStatus}</strong>
                      </div>
                    </div>
                    <div className="resource-summary-panel inline-resource-summary">
                      <h3>{zhTW.interfaceSystem.resourceSummary}</h3>
                      <div className="resource-summary-grid">
                        {resourceSummary.map((item) => (
                          <div key={item.label}>
                            <span>{item.label}</span>
                            <strong>{item.value}</strong>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
                {selectedLight.level === "error" && (
                  <button type="button" className="btn-base btn-primary light-rescue-button" onClick={onOpenRescue}>
                    {zhTW.interfaceSystem.goRescue}
                  </button>
                )}
                {selectedLight.details && selectedLight.details.length > 0 && (
                  <div className="light-size-detail-grid">
                    {selectedLight.details.map((item) => (
                      <div key={item.label} className={item.level ? `light-size-detail ${item.level}` : "light-size-detail"}>
                        <span>{item.label}</span>
                        <strong>{item.value}</strong>
                      </div>
                    ))}
                  </div>
                )}
                {selectedLight.topSources && selectedLight.topSources.length > 0 && (
                  <div className="light-top-sources">
                    <strong>{zhTW.interfaceSystem.topSources}</strong>
                    <ol>
                      {selectedLight.topSources.map((item) => (
                        <li key={item.path}>
                          <span>{item.path}</span>
                          <b>{item.size}</b>
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
              </>
            ) : (
              <span>{zhTW.interfaceSystem.clickLight}</span>
            )}
          </div>
        </details>

        <details className="overview-fold">
          <summary>{zhTW.interfaceSystem.globalWarnings}</summary>
          <div className="global-warning-panel">
            {warnings.length > 0 ? (
              <ul>
                {warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : (
              <p>{zhTW.interfaceSystem.noWarnings}</p>
            )}
          </div>
        </details>

        <details className="overview-fold">
          <summary>{zhTW.interfaceSystem.notifications}</summary>
          <div className="notification-panel">
            {recentEvents.length > 0 ? (
              <ul>
                {recentEvents.map((item, index) => (
                  <li key={`${item}-${index}`}>{item}</li>
                ))}
              </ul>
            ) : (
              <p>{zhTW.interfaceSystem.noEvents}</p>
            )}
          </div>
        </details>
      </div>
    </section>
  );
};
