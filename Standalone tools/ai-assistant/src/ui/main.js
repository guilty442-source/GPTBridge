import { mountAiAssistantWindowApp } from "./AiAssistantWindowApp.js";
const root = document.getElementById("root");
if (!root) throw new Error("Tool renderer root is missing");
mountAiAssistantWindowApp(root);
