import { formatBytes, formatProjectSize } from "@/shared/utils/format";
import { mainSystemLocale } from "@/locales/main-system";
import { h, hs } from "@/shared/mini/dom.js";
const t = mainSystemLocale.toolbox;
const STATUS_LABEL = {
	running: t.statusRunning,
	starting: t.statusStarting,
	stopping: t.statusStopping,
	error: t.statusError,
	stopped: t.statusStopped
};
const TOOL_MARK = {
	"ai-assistant": "投",
	"xingcheng": "星",
	"ai-collaboration": "外",
	"project-cleaner": "救",
	vaultly: "安",
	"file-sorter": "檔"
};
function dataBoundaryLabel(tool) {
	const scope = tool.dataBoundary?.databaseScope;
	if (scope === "tool-database-only") return t.toolDatabaseOnly;
	if (scope) return scope;
	if (tool.dataBoundary?.codeScope === "tool-root-only") return t.toolRootOnly;
	return tool.dataBoundary?.standalone ? t.independent : t.notDeclared;
}
function runtimeModeLabel(tool) {
	const selected = tool.automaticRuntimeMode === "governed-source" ? t.governedSource : tool.automaticRuntimeMode === "executable" ? t.executable : "";
	if (tool.runtimeMode === "dual-runtime") {
		return selected ? `${t.dualRuntime}（${t.currentRuntime}：${selected}）` : t.dualRuntime;
	}
	if (tool.runtimeMode === "governed-source") return t.governedSource;
	if (tool.runtimeMode === "executable") return t.executable;
	return t.notDeclared;
}
function fileCountLabel(value) {
	return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? `${new Intl.NumberFormat("zh-TW").format(value)} ${t.files}` : t.calculating;
}
function capacityCategoryLabel(sizeBytes, fileCount) {
	return `${formatBytes(sizeBytes)} · ${fileCountLabel(fileCount)}`;
}

/**
* renderToolDetail — the tool detail drawer's body content
* (former RuntimeToolCard drawer section).
*/
export function renderToolDetail(tool) {
	const capacity = tool.capacityBreakdown;
	const hasError = tool.status === "error";
	const dlRow = (dt, dd) => h("div", null, h("dt", null, dt), h("dd", null, dd));
	return h("div", { className: "tool-detail" },
		h("p", { className: "tool-detail__desc" }, tool.description || tool.summary),
		h("section", { className: "tool-detail__section" },
			h("h4", null, t.folderSize),
			h("div", { className: "tool-detail__big-value" },
				formatProjectSize(tool.projectSizeBytes, { fallback: t.calculating })),
			h("p", { className: "tool-detail__sub" },
				formatBytes(tool.projectSizeBytes, { exactBytes: true, fallback: t.calculating }),
				" · ",
				fileCountLabel(tool.projectFileCount))),
		h("section", { className: "tool-detail__section" },
			h("h4", null, t.dataBoundary),
			h("p", { className: "tool-detail__text" }, dataBoundaryLabel(tool))),
		h("section", { className: "tool-detail__section" },
			h("h4", null, t.runtimeMode),
			h("p", { className: "tool-detail__text" }, runtimeModeLabel(tool))),
		capacity ? h("section", { className: "tool-detail__section" },
			h("h4", null, t.capacityBreakdown),
			h("dl", { className: "tool-detail__capacity" },
				dlRow(t.programCore, capacityCategoryLabel(capacity.program.sizeBytes, capacity.program.fileCount)),
				dlRow(t.runtimeEnvironment, capacityCategoryLabel(capacity.runtime.sizeBytes, capacity.runtime.fileCount)),
				dlRow(t.userData, capacityCategoryLabel(capacity.userData.sizeBytes, capacity.userData.fileCount)),
				dlRow(t.cache, capacityCategoryLabel(capacity.cache.sizeBytes, capacity.cache.fileCount)),
				dlRow(t.backups, capacityCategoryLabel(capacity.backups.sizeBytes, capacity.backups.fileCount)))) : null,
		tool.note ? h("section", { className: "tool-detail__section" },
			h("h4", null, hasError ? t.statusError : t.capacityBreakdown),
			h("p", { className: `tool-detail__note${hasError ? " is-error" : ""}` }, tool.note)) : null);
}

/**
* mountRuntimeToolCard — former RuntimeToolCard.jsx.
* ``onOpenDetail(tool)`` opens the shared detail drawer owned by the
* toolbox (a per-card drawer would orphan overlays on list rebuilds).
*/
export function mountRuntimeToolCard({ tool, connected, onToolAction, onOpenDetail }) {
	const active = tool.status === "running" || tool.status === "starting";
	const busy = tool.status === "starting" || tool.status === "stopping";
	const launchable = tool.launchable !== false && tool.runtimeAvailable !== false;
	const hasError = tool.status === "error";
	const el = h("article", { className: "tool-card", dataset: { testid: `tool-card-${tool.id}`, state: tool.status } },
		h("div", { className: "tool-card__topline" },
			h("span", { className: "tool-card__mark", "aria-hidden": "true" },
				TOOL_MARK[tool.id] || tool.name.slice(0, 1)),
			h("span", { className: "status-pill", dataset: { status: tool.status } },
				h("span", { className: "status-pill__dot" }),
				STATUS_LABEL[tool.status])),
		h("div", { className: "tool-card__body" },
			h("h3", null, tool.name),
			h("p", null, tool.description || tool.summary)),
		h("div", { className: "tool-card__quick-meta" },
			h("div", { className: "tool-card__quick-item" },
				h("span", null, t.folderSize),
				h("strong", null, formatProjectSize(tool.projectSizeBytes, { fallback: t.calculating }))),
			h("div", { className: "tool-card__quick-item" },
				h("span", null, t.dataBoundary),
				h("strong", null, dataBoundaryLabel(tool)))),
		hasError ? h("p", { className: "tool-card__note is-error" }, tool.note) : null,
		h("div", { className: "tool-card__footer" },
			h("button", {
				type: "button",
				className: "tool-card__detail-btn",
				onClick: () => onOpenDetail?.(tool)
			},
				t.capacityBreakdown,
				hs("svg", { width: "14", height: "14", viewBox: "0 0 16 16", fill: "none" },
					hs("path", { d: "M6 3L11 8L6 13", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round" }))),
			!tool.lifecycleLocked ? h("div", { className: "tool-card__actions" },
				h("button", {
					className: "button button--primary",
					type: "button",
					dataset: { testid: `start-${tool.id}` },
					disabled: !connected || active || busy || !launchable,
					onClick: () => onToolAction(tool.id, "start")
				}, tool.status === "starting" ? t.starting : t.start),
				h("button", {
					className: "button button--secondary",
					type: "button",
					dataset: { testid: `stop-${tool.id}` },
					disabled: !connected || !active || busy,
					onClick: () => onToolAction(tool.id, "stop")
				}, tool.status === "stopping" ? t.stopping : t.stop)) : null));
	return { el };
}
export { mountRuntimeToolCard as default };
