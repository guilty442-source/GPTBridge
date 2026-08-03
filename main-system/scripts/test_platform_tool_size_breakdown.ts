import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {
  classifyToolFile,
  getPlatformToolSizes,
} from '../src-ui/main/platform-tool-sizes'

async function writeFixtureFile(
  toolRoot: string,
  relativePath: string,
  contents: string
): Promise<number> {
  const target = path.join(toolRoot, relativePath)
  await fs.promises.mkdir(path.dirname(target), { recursive: true })
  await fs.promises.writeFile(target, contents, 'utf-8')
  return Buffer.byteLength(contents)
}

async function main(): Promise<void> {
  const classificationCases = [
    ['src/main.py', 'program'],
    ['tests/test_main.py', 'program'],
    ['manifest.json', 'program'],
    ['dist/tool.exe', 'runtime'],
    ['build/app.js', 'runtime'],
    ['.venv/Lib/module.py', 'runtime'],
    ['node_modules/pkg/index.js', 'runtime'],
    ['runtime/ipc/channel.json', 'runtime'],
    ['data/default.sqlite3', 'user_data'],
    ['runtime/state/model.sqlite3', 'user_data'],
    ['runtime/settings/preferences.json', 'user_data'],
    ['runtime/recovery/journal.json', 'user_data'],
    ['runtime/test-self-training/examples.sqlite3', 'user_data'],
    ['__pycache__/main.pyc', 'cache'],
    ['runtime/browser-profiles/Cache/data', 'cache'],
    ['runtime/edge-profile/Default/data', 'cache'],
    ['runtime/electron-user-data/GPU Cache/data', 'cache'],
    ['runtime/temp/file.tmp', 'cache'],
    ['data/business/backups/archive.zip', 'backups'],
    ['runtime/data/investment_backups/archive.ivault', 'backups'],
    ['runtime/cache/database.bak', 'backups'],
    ['RUNTIME\\STATE\\MODEL.SQLITE3', 'user_data'],
  ] as const
  for (const [relativePath, expected] of classificationCases) {
    assert.equal(classifyToolFile(relativePath), expected, relativePath)
  }

  const workspaceRoot = await fs.promises.mkdtemp(
    path.join(os.tmpdir(), 'gptbridge-size-breakdown-')
  )
  try {
    const toolRoot = path.join(workspaceRoot, 'sample-tool')
    const manifest = JSON.stringify({
      id: 'sample-tool',
      runtime: { entry: 'src/main.py' },
    })
    const expected = {
      program: Buffer.byteLength(manifest),
      runtime: 0,
      user_data: 0,
      cache: 0,
      backups: 0,
    }
    const expectedFiles = {
      program: 1,
      runtime: 0,
      user_data: 0,
      cache: 0,
      backups: 0,
    }
    expected.program += await writeFixtureFile(toolRoot, 'src/main.py', 'program')
    expectedFiles.program += 1
    expected.runtime += await writeFixtureFile(toolRoot, 'dist/tool.exe', 'runtime')
    expectedFiles.runtime += 1
    expected.user_data += await writeFixtureFile(
      toolRoot,
      'runtime/state/model.sqlite3',
      'user-data'
    )
    expectedFiles.user_data += 1
    expected.cache += await writeFixtureFile(
      toolRoot,
      'runtime/browser-profiles/Cache/data',
      'cache'
    )
    expectedFiles.cache += 1
    expected.backups += await writeFixtureFile(
      toolRoot,
      'data/business/backups/archive.zip',
      'backup'
    )
    expectedFiles.backups += 1
    await fs.promises.writeFile(
      path.join(toolRoot, 'manifest.json'),
      manifest,
      'utf-8'
    )

    const tools = await getPlatformToolSizes(workspaceRoot, true)
    assert.equal(tools.length, 1)
    const tool = tools[0]
    for (const [category, sizeBytes] of Object.entries(expected)) {
      const categoryKey = category as keyof typeof tool.size_breakdown
      assert.equal(
        tool.size_breakdown[categoryKey].size_bytes,
        sizeBytes
      )
      assert.equal(tool.size_breakdown[categoryKey].file_count, expectedFiles[categoryKey])
    }
    const breakdownBytes = Object.values(tool.size_breakdown).reduce(
      (total, category) => total + category.size_bytes,
      0
    )
    const breakdownFiles = Object.values(tool.size_breakdown).reduce(
      (total, category) => total + category.file_count,
      0
    )
    assert.equal(breakdownBytes, tool.project_size_bytes)
    assert.equal(breakdownFiles, tool.file_count)

    const addedBytes = await writeFixtureFile(
      toolRoot,
      'runtime/state/new.sqlite3',
      'new-state'
    )
    const cached = await getPlatformToolSizes(workspaceRoot)
    assert.equal(cached[0].project_size_bytes, tool.project_size_bytes)
    const refreshed = await getPlatformToolSizes(workspaceRoot, true)
    assert.equal(refreshed[0].project_size_bytes, tool.project_size_bytes + addedBytes)
    assert.equal(
      refreshed[0].size_breakdown.user_data.size_bytes,
      tool.size_breakdown.user_data.size_bytes + addedBytes
    )
    process.stdout.write(
      `Tool capacity breakdown tests passed: ${classificationCases.length + 16} checks.\n`
    )
  } finally {
    await fs.promises.rm(workspaceRoot, { recursive: true, force: true })
  }
}

void main().catch((error: unknown) => {
  console.error(error)
  process.exitCode = 1
})
