//! dom.js — minimal native-ESM DOM layer (E180/C116 React retirement).
//!
//! Replaces the retired React runtime for the main renderer: ``h`` builds
//! DOM nodes from a JSX-shaped call signature, ``hs`` builds SVG nodes,
//! ``mountBoundary`` provides the module-fault containment the React
//! ModuleBoundary class component supplied.  Components are plain
//! ``mountX(container, ctx) -> {el, update, destroy}`` factories — no VDOM,
//! no re-render scheduler; each module patches its own subtree when its
//! subscribed state changes (same modular-refresh contract the
//! useSyncExternalStore subscriptions enforced).

import { mainSystemLocale } from "@/locales/main-system";

const SVG_NS = "http://www.w3.org/2000/svg";

function applyAttrs(el, attrs, svg) {
	if (!attrs) return;
	for (const [key, value] of Object.entries(attrs)) {
		if (value === null || value === undefined || value === false) continue;
		if (key === "className") {
			el.setAttribute("class", value);
		} else if (key === "dataset" && typeof value === "object") {
			for (const [dataKey, dataValue] of Object.entries(value)) {
				if (dataValue === null || dataValue === undefined) continue;
				el.dataset[dataKey] = String(dataValue);
			}
		} else if (key === "style" && typeof value === "object") {
			Object.assign(el.style, value);
		} else if (key.startsWith("on") && typeof value === "function") {
			el.addEventListener(key.slice(2).toLowerCase(), value);
		} else if (key === "value" && !svg) {
			el.value = value;
		} else if ((key === "checked" || key === "disabled" || key === "selected") && !svg) {
			el[key] = Boolean(value);
		} else if (key === "colSpan" || key === "rowSpan") {
			el.setAttribute(key.toLowerCase(), String(value));
		} else {
			el.setAttribute(key, value === true ? "" : String(value));
		}
	}
}

function appendChild(el, child) {
	if (child === null || child === undefined || child === false) return;
	if (Array.isArray(child)) {
		for (const nested of child) appendChild(el, nested);
		return;
	}
	if (child instanceof Node) {
		el.appendChild(child);
		return;
	}
	el.appendChild(document.createTextNode(String(child)));
}

/// Build an HTML element: ``h("div", {className, dataset, onClick, ...}, children)``.
export function h(tag, attrs, ...children) {
	const el = document.createElement(tag);
	applyAttrs(el, attrs, false);
	for (const child of children) appendChild(el, child);
	return el;
}

/// Build an SVG element (namespace-aware).
export function hs(tag, attrs, ...children) {
	const el = document.createElementNS(SVG_NS, tag);
	applyAttrs(el, attrs, true);
	for (const child of children) appendChild(el, child);
	return el;
}

/// Replace every child of ``container`` with the rendered list.
export function renderList(container, items, renderItem) {
	const nodes = [];
	for (const item of items) {
		const node = renderItem(item);
		if (node) nodes.push(node);
	}
	container.replaceChildren(...nodes);
}

/**
* Module fault isolation (former ModuleBoundary): ``builder`` runs inside
* try/catch; a throw is contained to that module's slot and renders the
* governed fault placeholder instead of blanking the rest of the UI.
* ``updater`` (optional second arg) wraps every subsequent update call.
*/
export function mountBoundary(name, builder, updater) {
	try {
		return builder();
	} catch (error) {
		console.error(`[module-boundary] ${name} failed`, error);
		const t = mainSystemLocale.moduleBoundary;
		return h("div", {
			className: "module-fault",
			role: "status",
			"data-testid": `module-fault-${name}`
		},
			h("strong", null, name),
			h("span", null, t.fallbackMessage));
	}
}

/// Guarded update — same containment contract for post-mount patches.
export function guardedUpdate(name, update) {
	try {
		update();
	} catch (error) {
		console.error(`[module-boundary] ${name} update failed`, error);
	}
}

/// Tiny observable store for converted hook state.
export function createStore(initial) {
	let state = initial;
	const listeners = new Set();
	return {
		get() {
			return state;
		},
		set(patch) {
			const next = typeof patch === "function" ? patch(state) : patch;
			if (Object.is(next, state)) return;
			state = next;
			for (const listener of Array.from(listeners)) {
				try {
					listener(state);
				} catch {}
			}
		},
		merge(patch) {
			this.set((prev) => ({ ...prev, ...patch }));
		},
		subscribe(listener) {
			listeners.add(listener);
			return () => listeners.delete(listener);
		}
	};
}
