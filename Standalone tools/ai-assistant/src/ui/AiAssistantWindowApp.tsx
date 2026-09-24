import { InvestmentWorkspace } from "./workspace/shell";
import "./ai-assistant.css";
import "./workspace/workspace.css";

/**
 * Phase 12 — the legacy tab-style business UI is retired.
 * The investment manager renders as `InvestmentWorkspace`:
 * left navigation / top market+system status / center page /
 * collapsible Xingcheng panel / bottom notification bar.
 */
export function AiAssistantWindowApp() {
  return <InvestmentWorkspace />;
}
