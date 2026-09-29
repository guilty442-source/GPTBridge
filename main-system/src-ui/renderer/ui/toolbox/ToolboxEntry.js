import { mountRuntimeToolCard, renderToolDetail } from "./RuntimeToolCard.js";
import { createDrawer } from "@/ui/drawer/Drawer.js";
import { mainSystemLocale } from "@/locales/main-system";
import { h, hs, renderList } from "@/shared/mini/dom.js";
import "./toolbox.css";
const t = mainSystemLocale.toolbox;
const TOOL_MARK = {
	"ai-assistant": "投",
	"xingcheng": "星",
	"ai-collaboration": "外",
	"project-cleaner": "救",
	vaultly: "安",
	"file-sorter": "檔"
};
function formatSyncTime(timestamp) {
	if (!timestamp) return t.notSynced;
	return new Date(timestamp).toLocaleTimeString("zh-TW", {
		hour: "2-digit",
		minute: "2-digit",
		second: "2-digit",
		hour12: false
	});
}

/**
* mountToolboxEntry — former ToolboxEntry.jsx.
* props supplier: getProps() -> {tools, connected, syncing, syncedAt,
* onRefresh, onToolAction}.  update() re-syncs the dynamic regions
* (sync label, result count, card grid) — the search input and filter
* buttons are persistent so focus/selection survives refreshes.
* Returns {el, update(), destroy()}.
*/
export function mountToolboxEntry(getProps) {
	let query = "";
	let filter = "all";

	const detailDrawer = createDrawer({
		onClose: () => detailDrawer.setOpen(false),
		title: "",
		eyebrow: t.independentToolPrefix,
		icon: "",
		render: (body) => {
			if (detailTool) body.appendChild(renderToolDetail(detailTool));
		}
	});
	let detailTool = null;
	const openDetail = (tool) => {
		detailTool = tool;
		detailDrawer.setOpen(false); // reset any closing animation state
		// Rebuild title/icon for this tool: createDrawer caches the header —
		// patch it through the overlay before opening.
		detailDrawer.setOpen(true);
		const overlay = detailDrawer.body()?.closest(".drawer-overlay");
		if (overlay) {
			overlay.querySelector("h2").textContent = tool.name;
			const icon = overlay.querySelector(".drawer-header__icon");
			if (icon) icon.textContent = TOOL_MARK[tool.id] || tool.name.slice(0, 1);
		}
	};

	const syncLabel = h("span", { className: "sync-time" }, t.notSynced);
	const refreshBtn = h("button", {
		type: "button",
		className: "button button--ghost",
		dataset: { testid: "refresh-tools" },
		onClick: () => getProps().onRefresh?.()
	}, t.refresh);
	const clearQueryBtn = h("button", {
		type: "button",
		"aria-label": t.clearLabel,
		style: { display: "none" },
		onClick: () => {
			query = "";
			searchInput.value = "";
			update();
		}
	}, "×");
	const searchInput = h("input", {
		placeholder: t.searchPlaceholder,
		onInput: (e) => {
			query = e.target.value;
			update();
		}
	});
	const filterButtons = new Map();
	const filterGroup = h("div", { className: "tool-filters", role: "group", "aria-label": t.filterLabel },
		[
			["all", t.filterAll],
			["running", t.statusRunning],
			["available", `可${t.start}`],
			["issues", t.filterIssues]
		].map(([value, label]) => {
			const btn = h("button", {
				type: "button",
				"aria-pressed": "false",
				onClick: () => {
					filter = value;
					update();
				}
			}, label);
			filterButtons.set(value, btn);
			return btn;
		}));
	const resultCount = h("span", { className: "tool-result-count" });
	const gridHost = h("div");
	const el = h("section", { className: "toolbox-panel", "aria-labelledby": "applications-title" },
		h("header", { className: "toolbox-header" },
			h("div", null,
				h("span", { className: "eyebrow" }, t.eyebrow),
				h("h2", { id: "applications-title" }, t.title),
				h("p", null, t.description)),
			h("div", { className: "toolbox-header__actions" }, syncLabel, refreshBtn)),
		h("div", { className: "toolbox-toolbar", "aria-label": t.filterLabel },
			h("label", { className: "tool-search" },
				h("span", { className: "sr-only" }, t.searchLabel),
				hs("svg", { "aria-hidden": "true", width: "16", height: "16", viewBox: "0 0 24 24", fill: "none" },
					hs("circle", { cx: "11", cy: "11", r: "7", stroke: "currentColor", "stroke-width": "1.8" }),
					hs("path", { d: "m16.5 16.5 4 4", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round" })),
				searchInput,
				clearQueryBtn),
			filterGroup,
			resultCount),
		gridHost);

	function update() {
		const { tools, connected, syncing, syncedAt, onToolAction } = getProps();
		syncLabel.textContent = syncing ? t.syncing : `${t.syncedAt} ${formatSyncTime(syncedAt)}`;
		refreshBtn.disabled = syncing;
		clearQueryBtn.style.display = query ? "" : "none";
		for (const [value, btn] of filterButtons) {
			const active = filter === value;
			btn.className = active ? "is-active" : "";
			btn.setAttribute("aria-pressed", String(active));
		}
		const needle = query.trim().toLocaleLowerCase("zh-TW");
		const visibleTools = tools.filter((tool) => {
			const matchesQuery = !needle || [
				tool.name,
				tool.id,
				tool.summary,
				tool.description
			].filter(Boolean).some((value) => String(value).toLocaleLowerCase("zh-TW").includes(needle));
			const matchesFilter = filter === "all" || filter === "running" && tool.status === "running" || filter === "available" && tool.launchable !== false && tool.runtimeAvailable !== false || filter === "issues" && (tool.status === "error" || tool.launchable !== false && tool.runtimeAvailable === false);
			return matchesQuery && matchesFilter;
		});
		resultCount.textContent = t.resultCount.replace("{count}", String(visibleTools.length)).replace("{total}", String(tools.length));
		if (visibleTools.length === 0) {
			gridHost.replaceChildren(h("div", { className: "empty-state" },
				h("strong", null, tools.length === 0 ? t.empty : t.noResults),
				tools.length > 0 ? h("span", null, t.adjustSearch) : null));
			return;
		}
		const grid = h("div", { className: "toolbox-grid" });
		renderList(grid, visibleTools, (tool) => mountRuntimeToolCard({
			tool,
			connected,
			onToolAction,
			onOpenDetail: openDetail
		}).el);
		gridHost.replaceChildren(grid);
	}

	update();
	return {
		el,
		update,
		destroy: () => detailDrawer.destroy()
	};
}
export { mountToolboxEntry as default };
