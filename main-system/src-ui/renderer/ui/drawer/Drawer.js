import { mainSystemLocale } from "@/locales/main-system";
import { h, hs } from "@/shared/mini/dom.js";
import "./drawer.css";

/**
* createDrawer — former Drawer.jsx as a mount factory.
* opts: {onClose, title, eyebrow, icon, side, render(bodyEl)}
* Returns {setOpen(open), isOpen(), destroy()}.
* The overlay mounts lazily on first open; close plays the 280ms
* is-closing transition then detaches (same timing as the React version).
*/
export function createDrawer({ onClose, title, eyebrow, icon, side = "right", render }) {
	let overlay = null;
	let body = null;
	let open = false;
	let closeTimer = null;
	const host = document.body;

	const keyHandler = (e) => {
		if (e.key === "Escape") onClose();
	};

	const build = () => {
		body = h("div", { className: "drawer-body" });
		const panel = h("div", {
			className: `drawer-panel drawer-panel--${side} is-open`,
			onClick: (e) => e.stopPropagation(),
			role: "dialog",
			"aria-modal": "true",
			"aria-label": title
		},
			h("header", { className: "drawer-header" },
				h("div", { className: "drawer-header__title" },
					icon ? h("span", { className: "drawer-header__icon", "aria-hidden": "true" }, icon) : null,
					h("div", null,
						eyebrow ? h("span", { className: "eyebrow" }, eyebrow) : null,
						h("h2", null, title))),
				h("button", {
					type: "button",
					className: "drawer-close",
					onClick: () => onClose(),
					"aria-label": mainSystemLocale.common.close
				},
					hs("svg", { width: "18", height: "18", viewBox: "0 0 18 18", fill: "none" },
						hs("path", { d: "M4 4L14 14M14 4L4 14", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round" })))),
			body);
		overlay = h("div", {
			className: "drawer-overlay is-open",
			onClick: () => onClose()
		}, panel);
		host.appendChild(overlay);
		window.addEventListener("keydown", keyHandler);
		render?.(body);
	};

	const teardown = () => {
		window.removeEventListener("keydown", keyHandler);
		overlay?.remove();
		overlay = null;
		body = null;
	};

	return {
		setOpen(next) {
			if (next === open) return;
			open = next;
			if (open) {
				if (closeTimer !== null) {
					window.clearTimeout(closeTimer);
					closeTimer = null;
				}
				if (!overlay) build();
				else {
					overlay.className = "drawer-overlay is-open";
					overlay.firstChild.className = `drawer-panel drawer-panel--${side} is-open`;
					// Re-open while a previous close animation was pending:
					// re-render so callers swapping drawer content (shared
					// detail drawers) see the new state.
					body.replaceChildren();
					render?.(body);
				}
			} else if (overlay) {
				overlay.className = "drawer-overlay is-closing";
				overlay.firstChild.className = `drawer-panel drawer-panel--${side} is-closing`;
				closeTimer = window.setTimeout(() => {
					closeTimer = null;
					teardown();
				}, 280);
			}
		},
		isOpen() {
			return open;
		},
		body() {
			return body;
		},
		destroy() {
			if (closeTimer !== null) window.clearTimeout(closeTimer);
			teardown();
		}
	};
}
