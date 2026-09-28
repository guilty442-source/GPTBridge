export function waitForIpcEvent(eventName, timeoutMs, predicate, signal) {
	return new Promise((resolve, reject) => {
		let timer = 0;
		let settled = false;
		const cleanup = () => {
			window.clearTimeout(timer);
			window.removeEventListener("ipc_event", handler);
			signal?.removeEventListener("abort", handleAbort);
		};
		const rejectOnce = (error) => {
			if (settled) return;
			settled = true;
			cleanup();
			reject(error);
		};
		const handler = (event) => {
			const customEvent = event;
			const detail = customEvent.detail || {};
			if (detail.event !== eventName) return;
			const payload = detail.payload || {};
			if (predicate && !predicate(payload)) return;
			if (settled) return;
			settled = true;
			cleanup();
			resolve(payload);
		};
		const handleAbort = () => {
			const reason = signal?.reason;
			rejectOnce(reason instanceof Error ? reason : new Error(`已撤銷等待 ${eventName}`));
		};
		if (signal?.aborted) {
			handleAbort();
			return;
		}
		timer = window.setTimeout(() => {
			rejectOnce(new Error(`等待 ${eventName} 逾時`));
		}, timeoutMs);
		window.addEventListener("ipc_event", handler);
		signal?.addEventListener("abort", handleAbort, { once: true });
	});
}
export function parseToolJson(stdout) {
	const text = String(stdout || "").trim();
	if (!text) return null;
	try {
		const parsed = JSON.parse(text);
		return parsed && typeof parsed === "object" ? parsed : null;
	} catch {
		return null;
	}
}
export function formatRunOutput(result) {
	if (!result) return "";
	const parts = [result.stdout ? `輸出\n${result.stdout.trim()}` : "", result.stderr ? `錯誤\n${result.stderr.trim()}` : ""].filter(Boolean);
	if (parts.length > 0) return parts.join("\n\n");
	return result.message || "工具已完成，沒有額外輸出。";
}
export function formatFileSize(size) {
	if (typeof size !== "number" || !Number.isFinite(size) || size < 0) return "";
	if (size < 1024) return `${size} B`;
	if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
	if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
	return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}
export function isAbsoluteFilesystemPath(value) {
	return /^[a-zA-Z]:[\\/]/.test(value) || /^\\\\/.test(value) || value.startsWith("/");
}
export async function selectFolder() {
	if (window.gptBridge?.selectFolder) return await window.gptBridge.selectFolder();
	return String(await window.electron?.invoke?.("dialog:select-folder") || "");
}
export async function openFile() {
	if (window.gptBridge?.openFile) return await window.gptBridge.openFile();
	return String(await window.electron?.invoke?.("dialog:open-file") || "");
}
export async function openPath(payload) {
	if (window.gptBridge?.openPath) return await window.gptBridge.openPath(payload);
	return await window.electron?.invoke?.("app:open-path", payload) || {
		ok: false,
		message: "目前環境不支援開啟路徑"
	};
}
