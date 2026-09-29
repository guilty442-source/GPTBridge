import { createDrawer } from "./Drawer.js";
import { formatBytes, formatProjectSize } from "@/shared/utils/format";
import { mainSystemLocale } from "@/locales/main-system";
import { h } from "@/shared/mini/dom.js";
const t = mainSystemLocale.product;
function formatFileCount(value) {
	return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? `${new Intl.NumberFormat("zh-TW").format(value)} ${t.files}` : t.pendingCheck;
}
function capacityDetail(bytes, fileCount) {
	return `${formatBytes(bytes, {
		exactBytes: true,
		fallback: t.pendingCheck
	})} · ${formatFileCount(fileCount)}`;
}

/**
* createCapacityDrawer — former AppCapacityDrawer.jsx.
* props supplier: getProps() -> {systemMetrics, mainSystemSizeBytes,
* mainSystemFileCount, dependencySizeBytes, dependencyFileCount,
* sharedLayerSizeBytes, sharedLayerFileCount, workspaceSizeBytes,
* workspaceFileCount}
* Returns {setOpen(open), destroy()}.
*/
export function createCapacityDrawer({ onClose, getProps }) {
	const row = (testid, title, big, detail) => h("article", {
		className: "capacity-row",
		dataset: { testid }
	},
		h("div", { className: "capacity-row__head" },
			h("strong", null, title),
			h("span", { className: "capacity-row__big" }, big)),
		h("p", { className: "capacity-row__detail" }, detail));
	const drawer = createDrawer({
		onClose,
		title: t.capacityDetails,
		eyebrow: t.systemOverview,
		icon: "D",
		render: (body) => {
			const p = getProps();
			const metrics = p.systemMetrics;
			const diskUsedBytes = typeof metrics.diskTotalBytes === "number" && typeof metrics.diskFreeBytes === "number" ? Math.max(0, metrics.diskTotalBytes - metrics.diskFreeBytes) : null;
			body.appendChild(h("div", { className: "capacity-drawer" },
				row("system-disk-size",
					`${t.systemDisk} ${metrics.diskRoot || ""}`,
					formatBytes(metrics.diskTotalBytes, { exactBytes: true, fallback: t.pendingCheck }),
					`${t.usageRate} ${typeof metrics.diskUsagePercent === "number" ? `${metrics.diskUsagePercent.toFixed(1)}%` : t.pendingCheck} · ${t.used} ${formatBytes(diskUsedBytes, { exactBytes: true, fallback: t.pendingCheck })} · ${t.available} ${formatBytes(metrics.diskFreeBytes, { exactBytes: true, fallback: t.pendingCheck })}`),
				row("main-system-folder-size",
					t.mainSystemSize,
					formatProjectSize(p.mainSystemSizeBytes, { fallback: t.pendingCheck }),
					`${t.mainSystemSizeHint} · ${capacityDetail(p.mainSystemSizeBytes, p.mainSystemFileCount)}`),
				row("dependency-folder-size",
					t.dependencySize,
					formatProjectSize(p.dependencySizeBytes, { fallback: t.pendingCheck }),
					`${t.dependencySizeHint} · ${capacityDetail(p.dependencySizeBytes, p.dependencyFileCount)}`),
				row("shared-layer-folder-size",
					t.sharedLayerSize,
					formatProjectSize(p.sharedLayerSizeBytes, { fallback: t.pendingCheck }),
					`${t.sharedLayerSizeHint} · ${capacityDetail(p.sharedLayerSizeBytes, p.sharedLayerFileCount)}`),
				row("workspace-folder-size",
					t.workspaceSize,
					formatProjectSize(p.workspaceSizeBytes, { fallback: t.pendingCheck }),
					`${t.workspaceSizeHint} · ${capacityDetail(p.workspaceSizeBytes, p.workspaceFileCount)}`)));
		}
	});
	return {
		setOpen: (open) => drawer.setOpen(open),
		destroy: () => drawer.destroy()
	};
}
export { createCapacityDrawer as default };
