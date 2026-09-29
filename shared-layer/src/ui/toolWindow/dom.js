//! dom.js — minimal native-ESM DOM layer shared by governed tool windows
//! (React retirement, E180/C116).
//!
//! Same contract as the main renderer's ``shared/mini/dom.js`` but
//! standalone: ``h``/``hs`` build DOM nodes from a JSX-shaped call
//! signature, ``createStore`` is a tiny observable store replacing hook
//! state, and ``renderList`` patches a container from a data list.  No
//! VDOM, no re-render scheduler — each module re-renders or patches its
//! own subtree when its subscribed state changes.

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
		} else if ((key === "colSpan" || key === "rowSpan") && !svg) {
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

/// Guarded update — a throwing patch is contained to that module's slot.
export function guardedUpdate(name, update) {
	try {
		update();
	} catch (error) {
		console.error(`[module-boundary] ${name} update failed`, error);
	}
}

/**
* Re-render ``container`` with ``build()`` while preserving focus:
* if the active element carried a ``data-k`` marker, the rebuilt tree's
* matching element regains focus and its text selection.
*/
export function rerender(container, build) {
	const active = document.activeElement;
	const key = active && container.contains(active) ? active.getAttribute("data-k") : null;
	const selection = key && typeof active.selectionStart === "number"
		? { start: active.selectionStart, end: active.selectionEnd }
		: null;
	const built = build();
	const nodes = (Array.isArray(built) ? built.flat(Infinity) : [built])
		.filter((c) => c !== null && c !== undefined && c !== false)
		.map((c) => c instanceof Node ? c : document.createTextNode(String(c)));
	container.replaceChildren(...nodes);
	if (key) {
		const next = container.querySelector(`[data-k="${key}"]`);
		if (next) {
			next.focus();
			if (selection && typeof next.setSelectionRange === "function") {
				try {
					next.setSelectionRange(selection.start, selection.end);
				} catch {}
			}
		}
	}
}
