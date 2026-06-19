import React from "react";
import { zhTW } from "../../i18n/zhTW";
import type { CheckLevel } from "../../types/health";

const healthText: Record<CheckLevel, string> = {
  normal: zhTW.health.normal,
  warning: zhTW.health.warning,
  error: zhTW.health.error,
  unknown: zhTW.health.unknown,
};

export const healthClass: Record<CheckLevel, string> = {
  normal: "health-normal",
  warning: "health-warning",
  error: "health-error",
  unknown: "health-unknown",
};

type HealthBadgeProps = {
  level: CheckLevel;
  label?: string;
};

export const HealthBadge: React.FC<HealthBadgeProps> = ({ level, label }) => (
  <div className={`status-badge ${healthClass[level]}`}>
    <span className="status-dot" />
    {label || healthText[level]}
  </div>
);
