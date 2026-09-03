import fs from 'node:fs'
import path from 'node:path'
import { app } from 'electron'

type RuntimeMode = 'source-production' | 'packaged'

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
  pythonSourceRepairEntry: string
  pythonExecutableCandidates: string[]
  pythonEntryCandidates: string[]
  pythonSourceRepairEntryCandidates: string[]
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
    fs.existsSync(path.join(candidate, 'governance_rule')) &&
    fs.existsSync(path.join(candidate, 'governance_rule', 'permission_directory')) &&
    fs.existsSync(path.join(candidate, 'main-system', 'src-core'))
  )
}

function findPackagedWorkspaceRoot(executableDir: string, resourcesRoot: string): string {
  // A packaged process must stay inside its own resources directory. Looking
  // at process.cwd() or a nearby source checkout lets an installed EXE bind to
  // unrelated code and was the reason packaged startup selected the source
  // source supervisor instead of the bundled backend.
  const candidates = [
    resourcesRoot,
    executableDir,
  ].map(toAbsolute)

  const workspaceRoot = candidates.find(hasWorkspaceMarkers)
  return workspaceRoot ?? resourcesRoot
}

function pythonExecutableCandidatesFor(root: string): string[] {
  if (process.platform === 'win32') {
    return [
      toAbsolute(path.join(root, '.venv', 'Scripts', 'pythonw.exe')),
      toAbsolute(path.join(root, '.venv', 'Scripts', 'python.exe')),
    ]
  }
  return [toAbsolute(path.join(root, '.venv', 'bin', 'python'))]
}

export function getRuntimePathLibrary(): RuntimePathLibrary {
  const mode: RuntimeMode = app.isPackaged ? 'packaged' : 'source-production'
  const executableDir = toAbsolute(path.dirname(app.getPath('exe')))
  const packagedResourcesRoot = toAbsolute(path.join(executableDir, 'resources'))
  const workspaceRoot = app.isPackaged
    ? findPackagedWorkspaceRoot(executableDir, packagedResourcesRoot)
    : toAbsolute('E:\\GPTBridge')

  const resourcesRoot = app.isPackaged
    ? packagedResourcesRoot
    : toAbsolute(path.join(workspaceRoot, 'main-system', 'resources'))
  const appRoot = app.isPackaged
    ? toAbsolute(path.join(resourcesRoot, 'app'))
    : toAbsolute(path.join(workspaceRoot, 'main-system'))
  const unpackedRoot = app.isPackaged
    ? toAbsolute(path.join(resourcesRoot, 'app.asar.unpacked'))
    : toAbsolute(path.join(workspaceRoot, 'main-system'))

  const pythonExecutableCandidates = [
    ...pythonExecutableCandidatesFor(resourcesRoot),
    ...pythonExecutableCandidatesFor(unpackedRoot),
    ...pythonExecutableCandidatesFor(appRoot),
    ...pythonExecutableCandidatesFor(workspaceRoot),
  ]

  const pythonEntryCandidates = [
    toAbsolute(path.join(resourcesRoot, 'src-core', 'main.py')),
    toAbsolute(path.join(unpackedRoot, 'src-core', 'main.py')),
    toAbsolute(path.join(appRoot, 'src-core', 'main.py')),
    toAbsolute(path.join(workspaceRoot, 'src-core', 'main.py')),
  ]

  const pythonSourceRepairEntryCandidates = [
    toAbsolute(path.join(resourcesRoot, 'src-core', 'tasks', 'source_repair.py')),
    toAbsolute(path.join(unpackedRoot, 'src-core', 'tasks', 'source_repair.py')),
    toAbsolute(path.join(appRoot, 'src-core', 'tasks', 'source_repair.py')),
    toAbsolute(path.join(workspaceRoot, 'src-core', 'tasks', 'source_repair.py')),
  ]

  const pythonExecutable =
    firstExisting(pythonExecutableCandidates) ?? pythonExecutableCandidates[0]
  const pythonEntry = firstExisting(pythonEntryCandidates) ?? pythonEntryCandidates[0]
  const pythonSourceRepairEntry =
    firstExisting(pythonSourceRepairEntryCandidates) ??
    pythonSourceRepairEntryCandidates[0]

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
    pythonSourceRepairEntry,
    pythonExecutableCandidates,
    pythonEntryCandidates,
    pythonSourceRepairEntryCandidates,
  }
}
