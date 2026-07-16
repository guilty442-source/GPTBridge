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
const updateRepository = path.join(projectRoot, 'src-core', 'settings', 'update_repository.py')
const hotUpdateService = path.join(projectRoot, 'src-core', 'core_system', 'hot_update_service.py')
const commandRouter = path.join(projectRoot, 'src-core', 'ipc', 'handlers.py')
const mainEntry = path.join(projectRoot, 'src-core', 'main.py')

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
  version: '1.0.0',
  run: async (): Promise<GovernanceReport> => {
    const affectedFiles: string[] = []

    const backendContent = await read(backendCoordinator)
    if (
      !backendContent.includes('GlobalUpdateCoordinator') ||
      !backendContent.includes('tool_restart') ||
      !backendContent.includes('mark_applied') ||
      !backendContent.includes('platform_tools')
    ) {
      affectedFiles.push(path.relative(projectRoot, backendCoordinator))
    }

    const serviceContent = await read(updateRepository)
    if (
      !serviceContent.includes('updates.sqlite3') ||
      !serviceContent.includes('update_snapshot') ||
      !serviceContent.includes('repair_run')
    ) {
      affectedFiles.push(path.relative(projectRoot, updateRepository))
    }

    const routerContent = await read(commandRouter)
    if (
      !routerContent.includes('"settings_mark_updates_applied"') ||
      routerContent.includes('"app:restart-backend"')
    ) {
      affectedFiles.push(path.relative(projectRoot, commandRouter))
    }

    const hotUpdateContent = await read(hotUpdateService)
    if (
      !hotUpdateContent.includes('HotUpdateService') ||
      !hotUpdateContent.includes('_run_declared_auto_repairs') ||
      !hotUpdateContent.includes('_synchronize_tool_source') ||
      !hotUpdateContent.includes('_install_renderer_overlay') ||
      !hotUpdateContent.includes('service.stop_tool') ||
      !hotUpdateContent.includes('service.start_tool')
    ) {
      affectedFiles.push(path.relative(projectRoot, hotUpdateService))
    }

    const mainContent = await read(mainEntry)
    if (
      !mainContent.includes('self.hot_update_service.start()') ||
      !mainContent.includes('_run_declared_auto_repairs')
    ) {
      affectedFiles.push(path.relative(projectRoot, mainEntry))
    }

    const uniqueFiles = Array.from(new Set(affectedFiles)).sort()
    const passed = uniqueFiles.length === 0

    return {
      ruleId: 'G-HMR-GLOBAL-001',
      passed,
      message: passed
        ? 'Hot update coordinator is enforced: backend source and renderer overlays synchronize directly before restarting only affected tools.'
        : 'Hot update coordinator drift detected. Keep fingerprinting, repair, direct backend synchronization, renderer overlay installation, persistence, and affected-tool restart wired.',
      affectedFiles: uniqueFiles,
      autofixAvailable: false,
    }
  },
}
