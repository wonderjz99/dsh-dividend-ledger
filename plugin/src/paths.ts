/**
 * 路径与解释器解析。
 *
 * 两条解析链，都要在"开发态"（仓库里直接跑）与"安装态"（被复制进 profile）
 * 下工作：
 *
 * - **仓库根**：从模块所在目录向上找含 `dividend_ledger/` 的目录。
 *   开发态命中 `plugin/src` → 仓库根；安装态命中 package 根 → 仓库根。
 * - **会话工作区**：`exec.agent.session.header.cwd`。DSH 里没有全局 cwd，
 *   工作区是**每个会话不可变的事实**，所以必须从 exec 里拿。
 */

import { existsSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { DEFAULT_PYTHON_CANDIDATES, ROOT_MARKERS } from './config.js'

/** 从模块 URL 开始向上查找仓库根。 */
export function findRepoRoot(startDir: string, maxLevels = 6): string | undefined {
  let current = resolve(startDir)
  for (let level = 0; level < maxLevels; level += 1) {
    if (ROOT_MARKERS.some((marker) => existsSync(join(current, marker)))) {
      return current
    }
    const parent = dirname(current)
    if (parent === current) break
    current = parent
  }
  return undefined
}

/**
 * 把 `import.meta.url` 解析成目录。
 *
 * 同时接受 file URL 与普通路径：调用处传的永远是 `import.meta.url`，
 * 但把这两种混用在测试里太容易踩坑，与其让 `fileURLToPath` 抛 `Invalid URL`，
 * 不如在这里一次判掉。
 */
export function moduleDir(importMetaUrl: string): string {
  if (importMetaUrl.startsWith('file:')) {
    return dirname(fileURLToPath(importMetaUrl))
  }
  return resolve(importMetaUrl)
}

/**
 * 解析仓库根。
 *
 * @param configured 配置里的 `root`，优先使用。
 * @param importMetaUrl `import.meta.url`，用于自动查找。
 */
export function resolveRepoRoot(
  configured: string | undefined,
  importMetaUrl: string,
): string {
  if (configured !== undefined && configured.trim() !== '') {
    return resolve(configured)
  }
  const found = findRepoRoot(moduleDir(importMetaUrl))
  if (found !== undefined) return found
  // 兜底：不报错，让后续调用给出更具体的"找不到 dividend_ledger"提示
  return resolve(moduleDir(importMetaUrl), '..', '..')
}

/**
 * 解析数据目录。
 *
 * 优先级：配置 > 会话工作区下的 `data/` > 仓库根下的 `data/`。
 * 相对路径按会话工作区（没有则仓库根）解析，与 DSH 对 `workdir` 的处理一致。
 */
export function resolveDataDir(options: {
  configured?: string
  sessionCwd?: string
  repoRoot: string
}): string {
  const base = options.sessionCwd ?? options.repoRoot
  const configured = options.configured?.trim()
  if (configured !== undefined && configured !== '') {
    return isAbsolute(configured) ? configured : resolve(base, configured)
  }
  return join(base, 'data')
}

/** 会话工作区：没有 agent 会话时为 undefined。 */
export function sessionCwdOf(exec: { agent?: { session?: { header?: { cwd?: string } } } }): string | undefined {
  const cwd = exec.agent?.session?.header?.cwd
  return typeof cwd === 'string' && cwd.trim() !== '' ? cwd : undefined
}

/**
 * 解析 Python 解释器。
 *
 * 配置里给了就用它（不探测——用户明确指定就该被尊重，哪怕它不存在，
 * 让报错发生在调用时更诚实）；否则依次探测候选，全都没有就用第一个候选名，
 * 由调用时报出可读错误。
 */
export function resolvePythonCommand(configured?: string): string {
  const explicit = configured?.trim()
  if (explicit !== undefined && explicit !== '') return explicit

  for (const candidate of DEFAULT_PYTHON_CANDIDATES) {
    const path = lookupOnPath(candidate)
    if (path !== undefined) return candidate
  }
  return DEFAULT_PYTHON_CANDIDATES[DEFAULT_PYTHON_CANDIDATES.length - 1] ?? 'python3'
}

/** 在 PATH 上查找可执行文件；不执行它，只看存不存在。 */
export function lookupOnPath(command: string, pathValue = process.env.PATH ?? ''): string | undefined {
  for (const dir of pathValue.split(':')) {
    if (dir === '') continue
    const candidate = join(dir, command)
    if (existsSync(candidate)) return candidate
  }
  return undefined
}
