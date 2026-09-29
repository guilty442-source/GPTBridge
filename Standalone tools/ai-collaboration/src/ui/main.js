import { mountAiCollaborationWindowApp } from "./AiCollaborationWindowApp.js";
const root = document.getElementById("root");
if (!root) throw new Error("Tool renderer root is missing");
mountAiCollaborationWindowApp(root);
