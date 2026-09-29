import { hmrService } from "../services/hmrService.js";
import { mainSystemLocale } from "../../locales/main-system.js";
import { h } from "@/shared/mini/dom.js";
const t = mainSystemLocale.hmr;
function getOverlayMessage(snapshot) {
	if (snapshot.level >= 5) return t.forceRestarting;
	if (snapshot.level >= 4) return t.forceReloading;
	if (snapshot.level >= 3) return t.reloading;
	if (snapshot.level >= 2) return t.repairing;
	return t.monitoring;
}
const overlayStyle = {
	position: "fixed",
	inset: "0",
	backgroundColor: "rgba(2, 6, 23, 0.88)",
	color: "#fff",
	display: "flex",
	justifyContent: "center",
	alignItems: "center",
	zIndex: "9999",
	fontFamily: "Noto Sans TC, Inter, sans-serif"
};
const panelStyle = {
	width: "min(560px, 92vw)",
	border: "1px solid #334155",
	borderRadius: "14px",
	background: "#0f172a",
	padding: "20px 22px",
	boxShadow: "0 20px 40px rgba(0, 0, 0, 0.45)"
};

/**
* initHmrGuard — former HMRGuard.jsx.  Initializes the HMR service and
* renders the recovery overlay over the whole app whenever the service
* escalates (level >= 2); hidden otherwise.  Returns a dispose function.
*/
export function initHmrGuard() {
	const overlay = h("div", { style: { ...overlayStyle, display: "none" } });
	let mounted = false;
	const update = (snapshot) => {
		if (snapshot.level < 2) {
			if (mounted) {
				overlay.remove();
				mounted = false;
			}
			return;
		}
		overlay.replaceChildren(h("div", { style: panelStyle },
			h("div", { style: { fontSize: "18px", fontWeight: "800", marginBottom: "12px" } }, t.title),
			h("p", { style: { margin: "0", color: "#e2e8f0", fontSize: "14px", fontWeight: "600" } }, getOverlayMessage(snapshot)),
			h("p", { style: { margin: "8px 0 0", color: "#94a3b8", fontSize: "12px" } }, `${t.reason}：${snapshot.lastReason || t.unknownError}`),
			h("p", { style: { margin: "8px 0 0", color: "#94a3b8", fontSize: "12px" } }, `${t.failureCount}：${snapshot.failureCount}`),
			h("button", {
				type: "button",
				style: {
					marginTop: "16px",
					padding: "10px 14px",
					border: "1px solid #ef4444",
					borderRadius: "10px",
					background: "#7f1d1d",
					color: "#fff",
					fontWeight: "700",
					cursor: "pointer"
				},
				onClick: () => {
					void hmrService.forceRestart("manual force restart from guard overlay");
				}
			}, t.restartNow)));
		if (!mounted) {
			document.body.appendChild(overlay);
			mounted = true;
		}
	};
	hmrService.init();
	const unsub = hmrService.subscribe(update);
	update(hmrService.getSnapshot());
	return () => {
		unsub();
		overlay.remove();
	};
}
export { initHmrGuard as default };
