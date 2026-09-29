import { mainSystemLocale } from "@/locales/main-system";
import { getRuntimeStatusField, subscribeRuntimeStatusField } from "@/shared/services/runtimeStatusField.js";
import { h, renderList } from "@/shared/mini/dom.js";
import "./sovereign.css";
const t = mainSystemLocale.sovereign;
function asString(value, fallback = "") {
	if (typeof value === "string") return value;
	if (typeof value === "number") return String(value);
	return fallback;
}
function asBoolean(value) {
	return typeof value === "boolean" ? value : null;
}
function dependencyTone(state) {
	const normalized = state.toUpperCase();
	if (normalized === "READY") return "success";
	if (normalized === "DEGRADED" || normalized === "RECOVERING") return "warning";
	if (normalized === "FAILED") return "danger";
	return "muted";
}
function formatCount(list) {
	const count = Array.isArray(list) ? list.length : 0;
	return new Intl.NumberFormat("zh-TW").format(count);
}
function powerLabel(value) {
	if (typeof value === "string") return value;
	if (value && typeof value === "object") {
		const record = value;
		return asString(record["id"]) || asString(record["statement"]) || asString(record["edict"]) || "—";
	}
	return "—";
}

/**
* mountSovereignDashboard — former SovereignDashboard.jsx.
* Rebuilds its (display-only) subtree when the decision_sovereign field
* changes — the same field-scoped refresh the hook provided.
* Returns {el, destroy()}.
*/
export function mountSovereignDashboard({ runtimeStatus } = {}) {
	const host = h("div");

	const render = () => {
		const sovereign = getRuntimeStatusField("decision_sovereign") ?? runtimeStatus?.decision_sovereign;
		const codex = sovereign?.governance_rules;
		const xingcheng = sovereign?.peer_systems?.xingcheng;
		const dependencyState = asString(sovereign?.dependency_state, "UNKNOWN");
		const executor = asString(sovereign?.executor, t.governedExecutorOnly);
		const healthOwner = asString(sovereign?.health_owner, "—");
		const ownedBy = asString(sovereign?.owned_by, "");
		const startedAt = asString(sovereign?.started_at, "");
		const xingchengPowers = Array.isArray(xingcheng?.powers?.empowered) ? xingcheng?.powers?.empowered : [];
		const xingchengProhibited = Array.isArray(xingcheng?.powers?.prohibited) ? xingcheng?.powers?.prohibited : [];
		const xingchengAuthority = xingcheng?.authority ?? {};
		const xingchengExecutionDenied = asBoolean(xingchengAuthority["execution"]) === false;
		const dl = (pairs) => h("dl", { className: "codex-facts" },
			pairs.map(([dt, dd]) => h("div", null, h("dt", null, dt), h("dd", null, dd))));
		host.replaceChildren(h("section", { className: "sovereign-panel", "aria-labelledby": "sovereign-title" },
			h("header", { className: "sovereign-header" },
				h("div", null,
					h("span", { className: "eyebrow" }, t.eyebrow),
					h("h2", { id: "sovereign-title" }, t.title),
					h("p", null, t.description)),
				h("div", { className: "dependency-badge", dataset: { tone: dependencyTone(dependencyState), testid: "sovereign-dependency-state" } },
					h("span", { className: "dependency-badge__dot" }),
					h("span", null,
						h("strong", null, t.dependencyState),
						h("small", null, dependencyState)))),
			h("div", { className: "sovereign-meta" },
				h("div", { className: "sovereign-meta__item" },
					h("span", null, t.executor),
					h("strong", null, executor)),
				h("div", { className: "sovereign-meta__item" },
					h("span", null, t.healthOwner),
					h("strong", null, healthOwner)),
				ownedBy ? h("div", { className: "sovereign-meta__item" },
					h("span", null, t.decisionSovereignOwnedBy),
					h("strong", null, ownedBy)) : null,
				startedAt ? h("div", { className: "sovereign-meta__item" },
					h("span", null, t.startedAt),
					h("strong", { className: "sovereign-meta__time" },
						new Date(startedAt).toLocaleString("zh-TW", { hour12: false }))) : null),
			h("div", { className: "sovereign-grid" },
				h("div", { className: "sovereign-card sovereign-card--wide" },
					h("div", { className: "sovereign-card__header" },
						h("span", { className: "eyebrow" }, t.codexTitle),
						h("strong", { className: "sovereign-card__codex-version" },
							codex?.codex_schema && codex?.codex_version ? `${asString(codex.codex_schema)} v${asString(codex.codex_version)}` : "—")),
					dl([
						[t.authority, asString(codex?.authority_rank, t.supreme)],
						[t.authoritySource, asString(codex?.authority_source, "—")],
						[t.bindingScope, asString(codex?.binding_scope, t.scopeDefault)],
						[t.function, asString(codex?.function, t.functionNone)],
						[t.mutability, asString(codex?.mutability, t.immutableSealed)],
						[t.amendment, asString(codex?.amendment, t.fullVersionedReplacement)],
						[t.interpretation, asString(codex?.interpretation, t.selfInterpreter)]
					]),
					h("footer", { className: "codex-provisions" },
						h("span", null, t.provisionsTitle),
						h("ul", null,
							h("li", null, h("strong", null, formatCount(codex?.sections)), h("small", null, t.sections)),
							h("li", null, h("strong", null, formatCount(codex?.principles)), h("small", null, t.principles)),
							h("li", null, h("strong", null, formatCount(codex?.articles)), h("small", null, t.articles)),
							h("li", null, h("strong", null, formatCount(codex?.edicts)), h("small", null, t.edicts))))),
				h("div", { className: "sovereign-card" },
					h("div", { className: "sovereign-card__header" },
						h("span", { className: "eyebrow" }, t.xingchengRole),
						h("strong", null, t.xingchengTitle)),
					dl([
						[t.xingchengRole, asString(xingcheng?.rank, t.xingchengPeer)],
						[t.xingchengKind, asString(xingcheng?.kind, t.xingchengLocalNativeModel)],
						[t.xingchengMode, asString(xingcheng?.mode, t.xingchengIntelligentManagement)]
					]),
					h("div", { className: "xingcheng-powers" },
						h("div", null,
							h("span", null, t.empoweredPowers),
							h("ul", null,
								xingchengPowers.map((power) => h("li", null, powerLabel(power))),
								xingchengPowers.length === 0 ? h("li", { className: "xingcheng-powers__empty" }, "—") : null)),
						h("div", { className: "xingcheng-powers--prohibited" },
							h("span", null, t.prohibitedPowers),
							h("ul", null,
								xingchengProhibited.map((power) => h("li", null, powerLabel(power))),
								xingchengProhibited.length === 0 ? h("li", { className: "xingcheng-powers__empty" }, "—") : null))),
					xingchengExecutionDenied ? h("footer", { className: "xingcheng-constraints" },
						h("span", null, t.noExecution)) : null))));
	};
	render();
	const unsub = subscribeRuntimeStatusField("decision_sovereign", render);
	return { el: host, destroy: unsub };
}
export { mountSovereignDashboard as default };
