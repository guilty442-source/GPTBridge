import React, { createContext, useCallback, useContext, useMemo, useReducer } from "react";
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
function reducer(s, a) {
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
}
const Ctx = createContext(null);
export function InvestmentUIProvider(props) {
	const [state, dispatch] = useReducer(reducer, {
		...initial,
		...loadPrefs()
	});
	// persist display prefs only — never account/portfolio state
	React.useEffect(() => {
		const prefs = {};
		for (const k of PREF_KEYS) prefs[k] = state[k];
		try {
			localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
		} catch {}
	}, [
		state.theme,
		state.fontScale,
		state.sidebarCollapsed,
		state.aiPanelOpen
	]);
	const value = useMemo(() => ({
		state,
		dispatch
	}), [state]);
	return <Ctx.Provider value={value}>{props.children}</Ctx.Provider>;
}
export function useUIState() {
	const ctx = useContext(Ctx);
	if (!ctx) throw new Error("useUIState outside provider");
	return ctx;
}
export function useNotify() {
	const { dispatch } = useUIState();
	return useCallback((text, severity = "INFO") => dispatch({
		type: "notify",
		text,
		severity
	}), [dispatch]);
}
