import fs from 'node:fs/promises'
import path from 'node:path'
import {
  CheckerCategory,
  CoverageStatus,
  EnforceLevel,
  type GovernanceChecker,
  type GovernanceReport,
  Severity,
} from '../../registry/GovernanceCheckerRegistry'

const projectRoot = process.cwd()
const backendCoordinator = path.join(
  projectRoot,
  'src-core',
  'settings',
  'global_update_coordinator.py'
)
const settingsService = path.join(projectRoot, 'src-core', 'settings', 'service.py')
const commandRouter = path.join(projectRoot, 'src-core', 'ipc', 'handlers.py')
const ipcServer = path.join(projectRoot, 'src-core', 'ipc', 'server.py')
const electronMain = path.join(projectRoot, 'src-ui', 'main', 'index.ts')
const electronPreload = path.join(projectRoot, 'src-ui', 'main', 'preload.ts')
const pythonBackend = path.join(projectRoot, 'src-ui', 'main', 'python-backend.ts')
const frontendCoordinator = path.join(
  projectRoot,
  'src-ui',
  'renderer',
  'shared',
  'services',
  'globalUpdateCoordinator.ts'
)
const updateCard = path.join(
  projectRoot,
  'src-ui',
  'renderer',
  'ui',
  'developer-mode',
  'tools',
  'cards',
  'update',
  'UpdateCardUI.tsx'
)
const toolControlCenter = path.join(
  projectRoot,
  'src-ui',
  'renderer',
  'ui',
  'developer-mode',
  'tools',
  'controllers',
  'useToolControlCenter.ts'
)

async function read(filePath: string): Promise<string> {
  try {
    return await fs.readFile(filePath, 'utf8')
  } catch {
    return ''
  }
}

export const globalUpdateCoordinatorChecker: GovernanceChecker = {
  id: 'G-HMR-GLOBAL-001',
  name: 'Global Update Coordinator Checker',
  category: CheckerCategory.HMR,
  severity: Severity.BLOCKING,
  enforceLevel: EnforceLevel.BLOCKING,
  target: 'src-ui/renderer/shared/services/globalUpdateCoordinator.ts',
  coverage: CoverageStatus.BUILD_ENFORCED,
  version: '2026.06.02',
  run: async (): Promise<GovernanceReport> => {
    const affectedFiles: string[] = []

    const backendContent = await read(backendCoordinator)
    if (
      !backendContent.includes('GlobalUpdateCoordinator') ||
      !backendContent.includes('renderer_hmr') ||
      !backendContent.includes('data_reload') ||
      !backendContent.includes('backend_restart') ||
      !backendContent.includes('app_restart')
    ) {
      affectedFiles.push(path.relative(projectRoot, backendCoordinator))
    }

    const serviceContent = await read(settingsService)
    if (
      !serviceContent.includes('global_update_coordinator') ||
      !serviceContent.includes('global_update_plan') ||
      !serviceContent.includes('settings_mark_updates_applied')
    ) {
      affectedFiles.push(path.relative(projectRoot, settingsService))
    }

    const routerContent = await read(commandRouter)
    const serverContent = await read(ipcServer)
    if (!routerContent.includes('"settings_mark_updates_applied"')) {
      affectedFiles.push(path.relative(projectRoot, commandRouter))
    }
    if (!serverContent.includes('"settings_mark_updates_applied"')) {
      affectedFiles.push(path.relative(projectRoot, ipcServer))
    }

    const mainContent = await read(electronMain)
    const preloadContent = await read(electronPreload)
    const pythonBackendContent = await read(pythonBackend)
    if (!mainContent.includes("ipcMain.handle('app:restart-backend'")) {
      affectedFiles.push(path.relative(projectRoot, electronMain))
    }
    if (!preloadContent.includes("'app:restart-backend'")) {
      affectedFiles.push(path.relative(projectRoot, electronPreload))
    }
    if (!pythonBackendContent.includes('restartBackend')) {
      affectedFiles.push(path.relative(projectRoot, pythonBackend))
    }

    const frontendContent = await read(frontendCoordinator)
    if (
      !frontendContent.includes('applyGlobalUpdatePlan') ||
      !frontendContent.includes('gptbridge:global-data-reload') ||
      !frontendContent.includes('app:restart-backend') ||
      !frontendContent.includes('app:restart')
    ) {
      affectedFiles.push(path.relative(projectRoot, frontendCoordinator))
    }

    const cardContent = await read(updateCard)
    if (
      !cardContent.includes('globalUpdatePlan') ||
      !cardContent.includes('globalUpdateApplyAction')
    ) {
      affectedFiles.push(path.relative(projectRoot, updateCard))
    }

    const controllerContent = await read(toolControlCenter)
    if (
      !controllerContent.includes('applyDetectedUpdates') ||
      !controllerContent.includes('normalizeGlobalUpdatePlan') ||
      !controllerContent.includes('settings_mark_updates_applied')
    ) {
      affectedFiles.push(path.relative(projectRoot, toolControlCenter))
    }

    const uniqueFiles = Array.from(new Set(affectedFiles)).sort()
    const passed = uniqueFiles.length === 0

    return {
      ruleId: 'G-HMR-GLOBAL-001',
      passed,
      message: passed
        ? 'Global update coordinator is enforced: renderer HMR, data reload, backend restart, and app restart are classified and actionable.'
        : 'Global update coordinator drift detected. Keep classification, UI action, backend restart IPC, and data reload event wired.',
      affectedFiles: uniqueFiles,
      autofixAvailable: false,
    }
  },
}
