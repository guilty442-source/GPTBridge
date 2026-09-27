export {}

declare global {
	interface Window {
		gptBridge?: {
			selectFolder: () => Promise<string>
			createFile: (defaultPath?: string) => Promise<unknown>
			openFile: (defaultPath?: string) => Promise<string>
			openPath: (
				payload: Record<string, unknown>,
			) => Promise<{ ok?: boolean; message?: string }>
			restartApp: () => Promise<unknown>
			restartBackend: () => Promise<unknown>
			ensureBackendStarted: () => Promise<unknown>
		}
		electron?: {
			invoke: (channel: string, ...args: unknown[]) => Promise<unknown>
		}
	}
}
