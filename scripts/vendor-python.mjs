/**
 * 把 Python 核心打进要发布到 npm 的包里。
 *
 * ── 为什么需要 ───────────────────────────────────────────────────────
 * npm 只会打包 `plugin/` 目录（package.json 所在处）**里面的**东西，而
 * Python 核心在仓库根部的 `dividend_ledger/`。不拷进来的话，别人
 * `npm install` 之后 Node 层找不到 Python 核心，插件直接不可用。
 *
 * ── 为什么放在包根而不是 `python/` 子目录 ─────────────────────────────
 * `paths.ts` 的 `findRepoRoot()` 从 `lib/` 向上找含 `ROOT_MARKERS`
 * （`dividend_ledger` 或 `SPEC.md`）的目录。放进 `python/` 子目录的话，
 * 从 `lib/` 向上第一层是包根、第二层是 `node_modules/`，**永远碰不到标记**，
 * 解析直接失败。放在包根就与开发态布局完全一致（`plugin/` 与
 * `dividend_ledger/` 同级），路径逻辑一行都不用改。
 *
 * ── 排除什么 ────────────────────────────────────────────────────────
 * `__pycache__` 有几百 KB 且是机器相关的字节码（Python 版本一变就失效），
 * 绝不能进包。只拷 `.py` 源码。
 *
 * 用法：node scripts/vendor-python.mjs [--check]
 *   --check  只校验已存在，不重新拷贝（CI/验收用）
 */

import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SOURCE = join(REPO, 'dividend_ledger')
const TARGET = join(REPO, 'plugin', 'dividend_ledger')
const checkOnly = process.argv.includes('--check')

/** 递归列出所有 `.py` 文件（相对路径）。 */
function pythonFiles(root) {
  const found = []
  const walk = (dir, prefix) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === '__pycache__' || entry.name.startsWith('.')) continue
      const rel = prefix === '' ? entry.name : `${prefix}/${entry.name}`
      if (entry.isDirectory()) walk(join(dir, entry.name), rel)
      else if (entry.name.endsWith('.py')) found.push(rel)
    }
  }
  walk(root, '')
  return found.sort()
}

if (!existsSync(SOURCE)) {
  console.error(`✗ 找不到 Python 核心：${SOURCE}`)
  process.exit(1)
}

const expected = pythonFiles(SOURCE)

if (checkOnly) {
  const problems = []
  if (!existsSync(TARGET)) {
    problems.push('未打包含：plugin/dividend_ledger/ 不存在')
  } else {
    const actual = new Set(pythonFiles(TARGET))
    for (const rel of expected) {
      if (!actual.has(rel)) problems.push(`缺少 ${rel}`)
    }
    // 缓存不判失败：`.npmignore` 已能把它挡在包外。这里只提示，
    // 因为"包目录里跑过 Python"本身是正常操作（本地调试会触发）。
    const caches = []
    const findCaches = (dir) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue
        if (entry.name === '__pycache__') caches.push(join(dir, entry.name))
        else findCaches(join(dir, entry.name))
      }
    }
    findCaches(TARGET)
    if (caches.length > 0) {
      console.log(`  ℹ️  包目录里有 ${caches.length} 个 __pycache__（已由 .npmignore 排除，不会进包）`)
    }
  }
  if (problems.length > 0) {
    console.error('✗ 打包内容检查未通过：')
    for (const line of problems) console.error('  - ' + line)
    console.error('\n  修复：node scripts/vendor-python.mjs')
    process.exit(1)
  }
  console.log(`  ✅ 打包内容正确：${expected.length} 个 .py 文件，无 __pycache__`)
  process.exit(0)
}

// 全量重建，避免上一次的残留文件混进包
rmSync(TARGET, { recursive: true, force: true })
mkdirSync(TARGET, { recursive: true })

let bytes = 0
for (const rel of expected) {
  const from = join(SOURCE, rel)
  const to = join(TARGET, rel)
  mkdirSync(dirname(to), { recursive: true })
  cpSync(from, to)
  bytes += statSync(to).size
}

// 确认没有把缓存带进去
const stray = existsSync(TARGET)
  ? readdirSync(TARGET, { withFileTypes: true }).filter((e) => e.name === '__pycache__')
  : []
if (stray.length > 0) {
  console.error('✗ 意外拷入了 __pycache__')
  process.exit(1)
}

console.log(
  `  ✅ 已把 ${expected.length} 个 .py 文件（${(bytes / 1024).toFixed(0)} KB）` +
    `拷入 plugin/dividend_ledger/`,
)
