export interface GPTBridgeAPI {
  selectFolder: () => Promise<string>
  createFile: (defaultPath?: string) => Promise<string>
  openFile: (defaultPath?: string) => Promise<string>
  openPath?: (payload: {
    path?: string
    basePath?: string
    relativePath?: string
    mode?: 'open' | 'reveal'
  }) => Promise<{ ok?: boolean; message?: string }>
  restartApp?: () => Promise<void>
}
