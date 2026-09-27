import { useCallback, useEffect, useRef, useState } from "react";
import { createInitialToolboxRuntimeState, hydrateToolboxRuntimeStateFromBackend, mergeToolboxProjectSizes, resolveToolboxToolAction } from "@/ui/toolbox/tools/runtimeState";
import { mainSystemLocale } from "@/locales/main-system";
const t = mainSystemLocale.toolbox;
const TOOLBOX_LIST_TIMEOUT_MS = 2e4;
const TOOLBOX_ACTION_TIMEOUT_MS = 12e4;
const LOCAL_TOOL_SIZE_RETRY_LIMIT = 6;
const LOCAL_TOOL_SIZE_RETRY_MS = 1500;
function hasMissingProjectSizes(tools) {
	return tools.some((tool) => Boolean(tool.folderPath) && (typeof tool.projectSizeBytes !== "number" || !Number.isFinite(tool.projectSizeBytes)));
}
// Perf/low-render: capacity breakdowns are rebuilt as fresh objects on
// every hydration pass — compare by value so identical scans keep
// referential equality downstream.
function sameCapacityBreakdown(a, b) {
	if (a === b) return true;
	if (!a || !b) return false;
	// Optional chaining: normalizeCapacityBreakdown may yield partial shapes;
	// a throw inside a setState updater would break rendering — never throw.
	return a.program?.sizeBytes === b.program?.sizeBytes && a.program?.fileCount === b.program?.fileCount && a.runtime?.sizeBytes === b.runtime?.sizeBytes && a.runtime?.fileCount === b.runtime?.fileCount && a.userData?.sizeBytes === b.userData?.sizeBytes && a.userData?.fileCount === b.userData?.fileCount && a.cache?.sizeBytes === b.cache?.sizeBytes && a.cache?.fileCount === b.cache?.fileCount && a.backups?.sizeBytes === b.backups?.sizeBytes && a.backups?.fileCount === b.backups?.fileCount;
}
function normalizeInventoryCount(value) {
	const count = typeof value === "number" ? value : Number(value);
	return Number.isSafeInteger(count) && count >= 0 ? count : undefined;
}
export function useToolboxApplications({ backendStatus, sendCommand, waitForIpcEvent }) {
	const [toolboxTools, setToolboxTools] = useState(() => createInitialToolboxRuntimeState());
	const toolboxToolsRef = useRef(toolboxTools);
	const [toolboxSyncing, setToolboxSyncing] = useState(false);
	const [toolboxSyncedAt, setToolboxSyncedAt] = useState(null);
	const [mainSystemSizeBytes, setMainSystemSizeBytes] = useState();
	const [mainSystemFileCount, setMainSystemFileCount] = useState();
	const [dependencySizeBytes, setDependencySizeBytes] = useState();
	const [dependencyFileCount, setDependencyFileCount] = useState();
	const [sharedLayerSizeBytes, setSharedLayerSizeBytes] = useState();
	const [sharedLayerFileCount, setSharedLayerFileCount] = useState();
	const [workspaceSizeBytes, setWorkspaceSizeBytes] = useState();
	const [workspaceFileCount, setWorkspaceFileCount] = useState();
	const refreshPromiseRef = useRef(null);
	const actionRevisionRef = useRef(0);
	useEffect(() => {
		toolboxToolsRef.current = toolboxTools;
	}, [toolboxTools]);
	const mergeLocalProjectSizes = useCallback(async (tools, forceRefresh = false) => {
		const api = window.electron;
		if (!api?.invoke) return tools;
		try {
			const payload = await api.invoke("app:get-platform-tool-sizes", { forceRefresh });
			if (payload?.ok === false) return tools;
			const mainSystemBytes = Number(payload?.main_system?.project_size_bytes);
			if (Number.isFinite(mainSystemBytes) && mainSystemBytes >= 0) {
				setMainSystemSizeBytes(mainSystemBytes);
			}
			setMainSystemFileCount(normalizeInventoryCount(payload?.main_system?.file_count));
			const dependencyBytes = Number(payload?.main_system?.dependency_size_bytes);
			if (Number.isFinite(dependencyBytes) && dependencyBytes >= 0) {
				setDependencySizeBytes(dependencyBytes);
			}
			setDependencyFileCount(normalizeInventoryCount(payload?.main_system?.dependency_file_count));
			const sharedLayerBytes = Number(payload?.shared_layer?.project_size_bytes);
			if (Number.isFinite(sharedLayerBytes) && sharedLayerBytes >= 0) {
				setSharedLayerSizeBytes(sharedLayerBytes);
			}
			setSharedLayerFileCount(normalizeInventoryCount(payload?.shared_layer?.file_count));
			const workspaceBytes = Number(payload?.workspace?.project_size_bytes);
			if (Number.isFinite(workspaceBytes) && workspaceBytes >= 0) {
				setWorkspaceSizeBytes(workspaceBytes);
			}
			setWorkspaceFileCount(normalizeInventoryCount(payload?.workspace?.file_count));
			const localCatalog = hydrateToolboxRuntimeStateFromBackend(payload?.tools);
			const discovered = tools.length ? tools : localCatalog;
			return mergeToolboxProjectSizes(discovered, payload?.tools);
		} catch {
			return tools;
		}
	}, []);
	const fetchBackendTools = useCallback(async (attempts) => {
		for (let attempt = 0; attempt < attempts; attempt += 1) {
			if (attempt > 0) {
				await new Promise((resolve) => window.setTimeout(resolve, 750));
			}
			const sent = sendCommand("toolbox_list_tools", { source: "app_toolbox_sync" });
			if (!sent.ok) continue;
			try {
				const payload = await waitForIpcEvent("toolbox_list_tools_result", TOOLBOX_LIST_TIMEOUT_MS);
				if (payload.ok === false) continue;
				return hydrateToolboxRuntimeStateFromBackend(payload.tools);
			} catch {}
		}
		return null;
	}, [sendCommand, waitForIpcEvent]);
	const refreshToolboxTools = useCallback(async () => {
		if (refreshPromiseRef.current) return refreshPromiseRef.current;
		const refreshRevision = actionRevisionRef.current;
		const refreshPromise = (async () => {
			setToolboxSyncing(true);
			try {
				let next = toolboxToolsRef.current;
				const fetched = await fetchBackendTools(2);
				if (fetched !== null) {
					next = fetched.length ? fetched : createInitialToolboxRuntimeState();
				}
				const withSizes = await mergeLocalProjectSizes(next, true);
				if (refreshRevision !== actionRevisionRef.current) return;
				setToolboxTools(withSizes);
				setToolboxSyncedAt(Date.now());
			} catch {
				// Backend metadata can be unavailable while the trusted local
				// inventory remains readable; keep capacity refresh independent.
				const withSizes = await mergeLocalProjectSizes(toolboxToolsRef.current, true);
				if (refreshRevision !== actionRevisionRef.current) return;
				setToolboxTools(withSizes);
				setToolboxSyncedAt(Date.now());
			} finally {
				setToolboxSyncing(false);
				refreshPromiseRef.current = null;
			}
		})();
		refreshPromiseRef.current = refreshPromise;
		return refreshPromise;
	}, [fetchBackendTools, mergeLocalProjectSizes]);
	useEffect(() => {
		if (backendStatus === "Connected") void refreshToolboxTools();
	}, [backendStatus, refreshToolboxTools]);
	useEffect(() => {
		let disposed = false;
		let attempts = 0;
		let retryTimer = null;
		const hydrateLocalSizes = async () => {
			attempts += 1;
			const withSizes = await mergeLocalProjectSizes(toolboxToolsRef.current);
			if (disposed) return;
			const sizesById = new Map(withSizes.map((tool) => [tool.id, tool]));
			setToolboxTools((current) => current.map((tool) => {
				const sized = sizesById.get(tool.id);
				if (!sized) return tool;
				// Perf/low-render: hydration retries re-run the disk scan with
				// identical results — keep referential equality when every
				// field matches so downstream does not recompute per retry.
				if (tool.folderPath === sized.folderPath && tool.manifestPath === sized.manifestPath && tool.codePath === sized.codePath && tool.projectSizeBytes === sized.projectSizeBytes && tool.projectFileCount === sized.projectFileCount && sameCapacityBreakdown(tool.capacityBreakdown, sized.capacityBreakdown)) {
					return tool;
				}
				return {
					...tool,
					folderPath: sized.folderPath,
					manifestPath: sized.manifestPath,
					codePath: sized.codePath,
					projectSizeBytes: sized.projectSizeBytes,
					projectFileCount: sized.projectFileCount,
					capacityBreakdown: sized.capacityBreakdown
				};
			}));
			setToolboxSyncedAt((current) => current ?? Date.now());
			if (attempts < LOCAL_TOOL_SIZE_RETRY_LIMIT && hasMissingProjectSizes(withSizes)) {
				retryTimer = window.setTimeout(hydrateLocalSizes, LOCAL_TOOL_SIZE_RETRY_MS);
			}
		};
		void hydrateLocalSizes();
		return () => {
			disposed = true;
			if (retryTimer !== null) window.clearTimeout(retryTimer);
		};
	}, [mergeLocalProjectSizes]);
	// NOTE: no 'gptbridge:global-data-reload' listener — that event has no
	// emitter anywhere in the codebase, and the handler ran a forced full
	// disk-size rescan per dispatch. Removed as dead background work; the
	// Connected-gate effect above and post-action refresh cover updates.
	const executeToolboxAction = useCallback(async (toolId, action) => {
		const target = toolboxToolsRef.current.find((tool) => tool.id === toolId);
		if (target?.launchable === false) {
			setToolboxTools((previous) => previous.map((tool) => tool.id === toolId ? {
				...tool,
				status: "stopped",
				updatedAt: Date.now(),
				note: t.disabled
			} : tool));
			return;
		}
		actionRevisionRef.current += 1;
		setToolboxTools((previous) => resolveToolboxToolAction(previous, toolId, action, "pending"));
		const command = action === "start" ? "toolbox_start_tool" : "toolbox_force_close_tool";
		const resultEvent = `${command}_result`;
		const failMessage = action === "start" ? t.startFailed : t.stopFailed;
		const requestId = `${toolId}:${action}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
		try {
			const waitResult = waitForIpcEvent(resultEvent, TOOLBOX_ACTION_TIMEOUT_MS, (payload) => String(payload.request_id || "") === requestId);
			const sent = sendCommand(command, {
				tool_id: toolId,
				request_id: requestId
			});
			if (!sent.ok) throw new Error(sent.message || failMessage);
			const result = await waitResult;
			if (result.ok === false) throw new Error(String(result.message || failMessage));
			setToolboxTools((previous) => resolveToolboxToolAction(previous, toolId, action, "settled"));
			const staleRefresh = refreshPromiseRef.current;
			if (staleRefresh) await staleRefresh;
			await refreshToolboxTools();
		} catch (error) {
			const rawMessage = error instanceof Error ? error.message : failMessage;
			const message = rawMessage === "PERMISSION_DENIED" ? mainSystemLocale["errors.permission_denied"] : failMessage;
			setToolboxTools((previous) => previous.map((tool) => tool.id === toolId ? {
				...tool,
				status: "error",
				updatedAt: Date.now(),
				note: message
			} : tool));
		}
	}, [
		refreshToolboxTools,
		sendCommand,
		waitForIpcEvent
	]);
	const handleToolboxAction = useCallback((toolId, action) => void executeToolboxAction(toolId, action), [executeToolboxAction]);
	return {
		toolboxTools,
		toolboxSyncing,
		toolboxSyncedAt,
		mainSystemSizeBytes,
		mainSystemFileCount,
		dependencySizeBytes,
		dependencyFileCount,
		sharedLayerSizeBytes,
		sharedLayerFileCount,
		workspaceSizeBytes,
		workspaceFileCount,
		refreshToolboxTools,
		handleToolboxAction
	};
}
