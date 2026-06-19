import fs from 'node:fs'
import path from 'node:path'
import { app } from 'electron'
import { getRuntimeEnv } from './runtime-env'

type RuntimeMode = 'development' | 'source-production' | 'packaged'

export interface RuntimePathLibrary {
  mode: RuntimeMode
  executableDir: string
  workspaceRoot: string
  resourcesRoot: string
  appRoot: string
  unpackedRoot: string
  preloadEntry: string
  rendererEntryHtml: string
  pythonExecutable: string
  pythonEntry: string
  pythonExecutableCandidates: string[]
  pythonEntryCandidates: string[]
}

function toAbsolute(p: string): string {
  return path.resolve(p)
}

function firstExisting(candidates: string[]): string | null {
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate
    }
  }
  return null
}

function hasWorkspaceMarkers(candidate: string): boolean {
  return (
    fs.existsSync(path.join(candidate, 'platform_tools')) ||
    fs.existsSync(path.join(candidate, 'config')) ||
    fs.existsSync(path.join(candidate, 'src-core'))
  )
}

function findPackagedWorkspaceRoot(executableDir: string, resourcesRoot: string): string {
  const explicitRoot = getRuntimeEnv('GPTBRIDGE_WORKSPACE_ROOT')
  if (explicitRoot && fs.existsSync(explicitRoot)) {
    return toAbsolute(explicitRoot)
  }

  const candidates = [
    executableDir,
    path.join(executableDir, '..', '..'),
    process.cwd(),
    resourcesRoot,
  ].map(toAbsolute)

  const workspaceRoot = candidates.find(hasWorkspaceMarkers)
  return workspaceRoot ?? executableDir
}

export function getRuntimePathLibrary(): RuntimePathLibrary {
  const mode: RuntimeMode = app.isPackaged
    ? 'packaged'
    : getRuntimeEnv('GPTBRIDGE_SOURCE_PRODUCTION') === '1'
      ? 'source-production'
      : 'development'
  const executableDir = toAbsolute(path.dirname(app.getPath('exe')))
  const packagedResourcesRoot = toAbsolute(path.join(executableDir, 'resources'))
  const workspaceRoot = app.isPackaged
    ? findPackagedWorkspaceRoot(executableDir, packagedResourcesRoot)
    : toAbsolute(process.cwd())

  const resourcesRoot = app.isPackaged
    ? packagedResourcesRoot
    : toAbsolute(path.join(workspaceRoot, 'resources'))
  const appRoot = app.isPackaged
    ? toAbsolute(path.join(resourcesRoot, 'app'))
    : workspaceRoot
  const unpackedRoot = app.isPackaged
    ? toAbsolute(path.join(resourcesRoot, 'app.asar.unpacked'))
    : workspaceRoot

  const pythonExecutableCandidates = [
    toAbsolute(path.join(resourcesRoot, '.venv', 'Scripts', 'python.exe')),
    toAbsolute(path.join(unpackedRoot, '.venv', 'Scripts', 'python.exe')),
    toAbsolute(path.join(appRoot, '.venv', 'Scripts', 'python.exe')),
    toAbsolute(path.join(workspaceRoot, '.venv', 'Scripts', 'python.exe')),
  ]

  const pythonEntryCandidates = [
    toAbsolute(path.join(resourcesRoot, 'src-core', 'main.py')),
    toAbsolute(path.join(unpackedRoot, 'src-core', 'main.py')),
    toAbsolute(path.join(appRoot, 'src-core', 'main.py')),
    toAbsolute(path.join(workspaceRoot, 'src-core', 'main.py')),
  ]

  const pythonExecutable =
    firstExisting(pythonExecutableCandidates) ?? pythonExecutableCandidates[0]
  const pythonEntry = firstExisting(pythonEntryCandidates) ?? pythonEntryCandidates[0]

  return {
    mode,
    executableDir,
    workspaceRoot,
    resourcesRoot,
    appRoot,
    unpackedRoot,
    preloadEntry: toAbsolute(path.join(__dirname, 'preload.js')),
    rendererEntryHtml: toAbsolute(path.join(__dirname, '../renderer/index.html')),
    pythonExecutable,
    pythonEntry,
    pythonExecutableCandidates,
    pythonEntryCandidates,
  }
}
