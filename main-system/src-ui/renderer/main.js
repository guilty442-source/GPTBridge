import { mountApp } from "./ui/App.js";
import { initHmrGuard } from "./shared/components/hmrGuard.js";
initHmrGuard();
const mounted = mountApp();
document.getElementById("root").replaceChildren(mounted.el);
