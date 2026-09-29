//! backendSocket.js — ai-assistant backend socket (React-free,
//! E180/C116).  Thin tool-boundary wrapper over the shared governed
//! socket factory; keeps the tool-local import path stable.
export { createLocalBackendSocket } from "../../../../shared-layer/src/ui/toolWindow/localBackendSocket.js";
