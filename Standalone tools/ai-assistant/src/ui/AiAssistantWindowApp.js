//! AiAssistantWindowApp.js — ai-assistant tool window entry
//! (React-free native ESM, E180/C116).
//!
//! Phase 12 — the legacy tab-style business UI is retired.
//! The investment manager renders as the workspace shell:
//! left navigation / top market+system status / center page /
//! collapsible 星澄 panel / bottom notification bar.
import { mountInvestmentWorkspace } from "./workspace/shell.js";
import "./ai-assistant.css";
import "./workspace/workspace.css";

export function mountAiAssistantWindowApp(root) {
	return mountInvestmentWorkspace(root);
}
