import { mainSystemLocale } from "@/locales/main-system";
const t = mainSystemLocale.toolbox;
function normalizeStatus(status) {
	const value = String(status ?? "").toLowerCase();
	if (value === "running") return "running";
	if (value === "starting") return "starting";
	if (value === "stopping") return "stopping";
	if ([
		"error",
		"fail",
		"failed"
	].includes(value)) return "error";
	return "stopped";
}
function noteForStatus(status, launchable) {
	if (!launchable) return t.notLaunchable;
	if (status === "running") return t.runningNote;
	if (status === "starting") return t.startingNote;
	if (status === "stopping") return t.stoppingNote;
	if (status === "error") return t.errorNote;
	return t.readyNote;
}
function normalizeSizeBytes(value) {
	const parsed = typeof value === "number" ? value : Number(String(value ?? "").trim());
	return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}
function normalizeFileCount(value) {
	const parsed = typeof value === "number" ? value : Number(String(value ?? "").trim());
	return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : undefined;
}
function normalizeCapacityBreakdown(value) {
	if (!value || typeof value !== "object") return undefined;
	const raw = value;
	const normalizeCategory = (category) => {
		if (!category || typeof category !== "object") return undefined;
		const fields = category;
		const sizeBytes = normalizeSizeBytes(fields.size_bytes);
		const fileCount = normalizeFileCount(fields.file_count);
		return sizeBytes === undefined || fileCount === undefined ? undefined : {
			sizeBytes,
			fileCount
		};
	};
	const program = normalizeCategory(raw.program);
	const runtime = normalizeCategory(raw.runtime);
	const userData = normalizeCategory(raw.user_data);
	const cache = normalizeCategory(raw.cache);
	const backups = normalizeCategory(raw.backups);
	return program && runtime && userData && cache && backups ? {
		program,
		runtime,
		userData,
		cache,
		backups
	} : undefined;
}
function normalizeRuntimeMode(value) {
	const mode = String(value ?? "").trim().toLowerCase();
	if (mode === "executable" || mode === "governed-source" || mode === "dual-runtime") return mode;
	return undefined;
}
function normalizeAutomaticRuntimeMode(value) {
	const mode = String(value ?? "").trim().toLowerCase();
	return mode === "executable" || mode === "governed-source" ? mode : undefined;
}
function normalizeDataBoundary(manifest) {
	const raw = manifest.data_boundary;
	const boundary = raw && typeof raw === "object" ? raw : {};
	return {
		standalone: typeof boundary.standalone === "boolean" ? boundary.standalone : normalizeBoolean(manifest.standalone),
		codeScope: String(boundary.code_scope ?? "").trim() || undefined,
		databaseScope: String(boundary.database_scope ?? "").trim() || undefined
	};
}
function normalizeBoolean(value) {
	return value === true || String(value ?? "").trim().toLowerCase() === "true";
}
export function createInitialToolboxRuntimeState() {
	return [];
}
export function hydrateToolboxRuntimeStateFromBackend(payload) {
	const list = Array.isArray(payload) ? payload : [];
	const now = Date.now();
	const tools = [];
	for (const entry of list) {
		const manifest = entry;
		const id = String(manifest.id ?? "").trim();
		if (!id) continue;
		if (normalizeBoolean(manifest.hidden_from_toolbox) || String(manifest.merged_into ?? "").trim() !== "") {
			continue;
		}
		const permissionDenied = normalizeBoolean(manifest.permission_denied);
		const lifecycleLocked = normalizeBoolean(manifest.lifecycle_locked);
		const governanceAuthority = normalizeBoolean(manifest.governance_authority);
		const launchable = manifest.enabled !== false && !permissionDenied;
		const status = lifecycleLocked ? normalizeStatus(manifest.status) : launchable ? normalizeStatus(manifest.status) : "stopped";
		const description = String(manifest.description ?? "").trim();
		const name = String(manifest.name ?? id).trim() || id;
		const executableExists = typeof manifest.executable_exists === "boolean" ? manifest.executable_exists : undefined;
		const runtimeAvailable = typeof manifest.runtime_available === "boolean" ? manifest.runtime_available : executableExists;
		const runtimeMode = normalizeRuntimeMode(manifest.runtime_mode);
		const note = governanceAuthority ? t.runningNote : permissionDenied ? mainSystemLocale["errors.permission_denied"] : runtimeAvailable === false ? t.executableMissing : noteForStatus(status, launchable);
		const summary = description || `${t.independentToolPrefix}：${id}`;
		tools.push({
			id,
			name,
			summary,
			description: description || summary,
			folderPath: String(manifest.folder_path ?? "").trim(),
			manifestPath: String(manifest.manifest_path ?? "").trim(),
			codePath: String(manifest.code_path ?? "").trim(),
			executablePath: String(manifest.executable_path ?? "").trim(),
			executableExists,
			runtimeAvailable,
			runtimeMode,
			automaticRuntimeMode: normalizeAutomaticRuntimeMode(manifest.automatic_runtime_mode),
			projectSizeBytes: normalizeSizeBytes(manifest.project_size_bytes),
			projectFileCount: normalizeFileCount(manifest.file_count),
			capacityBreakdown: normalizeCapacityBreakdown(manifest.size_breakdown),
			dataBoundary: normalizeDataBoundary(manifest),
			hasCustomUi: normalizeBoolean(manifest.has_custom_ui),
			launchable,
			lifecycleLocked,
			windowOnly: normalizeBoolean(manifest.window_only),
			status,
			updatedAt: now,
			note
		});
	}
	return tools;
}
export function mergeToolboxProjectSizes(tools, payload) {
	const list = Array.isArray(payload) ? payload : [];
	const sizesById = new Map();
	for (const raw of list) {
		const item = raw;
		const id = String(item.id ?? "").trim();
		const projectSizeBytes = normalizeSizeBytes(item.project_size_bytes);
		if (id && projectSizeBytes !== undefined) {
			sizesById.set(id, {
				...item,
				projectSizeBytes,
				projectFileCount: normalizeFileCount(item.file_count),
				capacityBreakdown: normalizeCapacityBreakdown(item.size_breakdown)
			});
		}
	}
	return tools.map((tool) => {
		const size = sizesById.get(tool.id);
		if (!size) return tool;
		return {
			...tool,
			folderPath: String(size.folder_path ?? "").trim() || tool.folderPath,
			manifestPath: String(size.manifest_path ?? "").trim() || tool.manifestPath,
			codePath: String(size.code_path ?? "").trim() || tool.codePath,
			projectSizeBytes: size.projectSizeBytes,
			projectFileCount: size.projectFileCount,
			capacityBreakdown: size.capacityBreakdown
		};
	});
}
export function resolveToolboxToolAction(tools, toolId, action, phase) {
	const now = Date.now();
	return tools.map((tool) => {
		if (tool.id !== toolId) return tool;
		if (tool.launchable === false) {
			return {
				...tool,
				status: "stopped",
				updatedAt: now,
				note: noteForStatus("stopped", false)
			};
		}
		const status = phase === "pending" ? action === "start" ? "starting" : "stopping" : action === "start" ? "running" : "stopped";
		return {
			...tool,
			status,
			updatedAt: now,
			note: noteForStatus(status, true)
		};
	});
}
