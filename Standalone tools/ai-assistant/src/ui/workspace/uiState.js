//! uiState.js — ai-assistant workspace UI state (React-free, E180/C116).
//! Store-based replacement for the retired ``InvestmentUIProvider`` /
//! ``useUIState`` context: same reducer actions, same localStorage
//! display-preference persistence, same notify channel — consumed by the
//! shell and pages through the ``ctx.ui`` contract.
import { createStore } from "../../../../../shared-layer/src/ui/toolWindow/dom.js";

const initial = {
	page: "overview",
	market: "tw",
	instrumentId: "",
	strategyId: "",
	mode: "ANALYSIS",
	theme: "light",
	fontScale: 1,
	sidebarCollapsed: false,
	aiPanelOpen: true,
	notices: []
};
const PREF_KEY = "investment-ui-prefs";
const PREF_KEYS = [
	"theme",
	"fontScale",
	"sidebarCollapsed",
	"aiPanelOpen"
];
function loadPrefs() {
	try {
		const raw = localStorage.getItem(PREF_KEY);
		return raw ? JSON.parse(raw) : {};
	} catch {
		return {};
	}
}
export function reducer(s, a) {
	switch (a.type) {
		case "page": return {
			...s,
			page: a.page
		};
		case "market": return {
			...s,
			market: a.market
		};
		case "instrument": return {
			...s,
			instrumentId: a.instrumentId
		};
		case "strategy": return {
			...s,
			strategyId: a.strategyId
		};
		case "mode": return {
			...s,
			mode: a.mode
		};
		case "theme": return {
			...s,
			theme: a.theme
		};
		case "fontScale": return {
			...s,
			fontScale: Math.min(1.5, Math.max(.8, a.fontScale))
		};
		case "sidebar": return {
			...s,
			sidebarCollapsed: a.collapsed
		};
		case "aiPanel": return {
			...s,
			aiPanelOpen: a.open
		};
		case "notify": return {
			...s,
			notices: [{
				id: Date.now(),
				text: a.text,
				severity: a.severity,
				at: Date.now()
			}, ...s.notices].slice(0, 50)
		};
	}
	return s;
}

/// Creates the workspace UI store.  ``get()`` returns the current state,
/// ``subscribe(fn)`` fires after every dispatch, ``dispatch(action)``
/// applies the reducer and persists display preferences only — never
/// account/portfolio state.
export function createUIState() {
	const store = createStore({ ...initial, ...loadPrefs() });
	const dispatch = (action) => store.set(reducer(store.get(), action));
	const notify = (text, severity = "INFO") => dispatch({ type: "notify", text, severity });
	// persist display prefs only — never account/portfolio state
	store.subscribe((s) => {
		const prefs = {};
		for (const k of PREF_KEYS) prefs[k] = s[k];
		try {
			localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
		} catch {}
	});
	return {
		get: store.get,
		subscribe: store.subscribe,
		dispatch,
		notify
	};
}
