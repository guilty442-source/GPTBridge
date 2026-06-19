import type { LogGroups } from "../../types/ui";

export const trimLogGroups = (groups: LogGroups): LogGroups => ({
  core: groups.core.slice(-200),
  design: groups.design.slice(-200),
  developer: groups.developer.slice(-200),
});
