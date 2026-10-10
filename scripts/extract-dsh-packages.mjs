#!/usr/bin/env node
/**
 * 从已安装的 DSH 应用里抽出 `@deepseek-ai/*` 运行时包，供本仓库离线使用。
 *
 * 为什么需要这一步：DSH 桌面版的实现打包在 `app.asar` 里，机器上没有源码
 * checkout，`node_modules/@deepseek-ai/` 也不存在。但薄 TS 适配层必须对
 * **真实的 `defineTool` 契约**做类型检查与运行期验证——照抄社区文档已经被证明
 * 会在 0.2.0-rc.2 直接抛错。所以这里把需要的包按依赖关系递归抽到
 * `plugin/vendor/`，再软链到 `plugin/node_modules/`。
 *
 * 用法：
 *   node scripts/extract-dsh-packages.mjs            # 抽默认的包集合
 *   node scripts/extract-dsh-packages.mjs --list     # 只列 asar 内的包名与版本
 *   node scripts/extract-dsh-packages.mjs --verify   # 抽完做一次导入自检
 *
 * 产物目录 `plugin/vendor/` 与 `plugin/node_modules/` 都在 .gitignore 里，
 * 不入版本库——它们只是本地开发时的只读副本。
 */

import { createRequire } from 'node:module'
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(__dirname, '..')
const VENDOR_DIR = join(REPO_ROOT, 'plugin', 'vendor')
const NODE_MODULES = join(REPO_ROOT, 'plugin', 'node_modules')

const DEFAULT_ASAR = '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar'

/** 适配层直接依赖的包；其余按 package.json 的 dependencies 递归补齐。 */
const ROOT_PACKAGES = [
  '@deepseek-ai/cordis',
  '@deepseek-ai/schemastery',
  '@deepseek-ai/dsh-tools',
  '@deepseek-ai/dsh-shell',
  '@deepseek-ai/dsh-sandbox-policy',
]

/**
 * 顺带抽取的非 @deepseek-ai 包。
 *
 * 留空是有意的：asar 里的 `@types/node` 被裁掉了 `index.d.ts`（只有 LICENSE /
 * README / package.json），没法当类型用，所以它走 npm 安装而不是抽取。
 */
const EXTRA_PACKAGES = []

/** asar 内部包的前缀路径 */
const ASAR_PREFIX = '/dsh/node_modules'

function parseArgs(argv) {
  return {
    list: argv.includes('--list'),
    verify: argv.includes('--verify'),
    asar: valueOf(argv, '--asar') ?? DEFAULT_ASAR,
    quiet: argv.includes('--quiet'),
  }
}

function valueOf(argv, flag) {
  const index = argv.indexOf(flag)
  return index >= 0 ? argv[index + 1] : undefined
}

/**
 * 极简 asar 读取器。
 *
 * 布局：pickle 长度 4B｜pickle 头 4B｜uint32LE JSON 头长度 @12｜UTF-8 JSON 头 @16
 * ｜补齐到 8 字节边界｜文件体（各条目 offset 相对该边界）
 *
 * ⚠️ 踩过的坑：文件体起点是 ``align8(16 + headerSize)``，**不是** ``16 + headerSize``。
 * 少了这段补齐会让大文件整体错位几十 KB，而小文件因为偏移量小看起来"正常"，
 * 非常难查。
 */
class AsarArchive {
  constructor(path) {
    this.path = path
    this.fd = readFileSync(path) // 单文件 120MB 量级，一次性读入换实现简洁
    const headerSize = this.fd.readUInt32LE(12)
    const headerText = this.fd.subarray(16, 16 + headerSize).toString('utf8')
    this.header = JSON.parse(headerText)
    this.base = (16 + headerSize + 7) & ~7
    this.files = new Map()
    this.#walk(this.header, '')
  }

  #walk(node, prefix) {
    for (const [name, entry] of Object.entries(node.files ?? {})) {
      const path = `${prefix}/${name}`
      if (entry.files) this.#walk(entry, path)
      else this.files.set(path, entry)
    }
  }

  has(path) {
    return this.files.has(path)
  }

  read(path) {
    const entry = this.files.get(path)
    if (entry === undefined) throw new Error(`asar 内不存在：${path}`)
    const start = this.base + Number.parseInt(entry.offset, 10)
    return this.fd.subarray(start, start + entry.size)
  }

  /** 以某个 asar 目录为根的成员列表（相对路径） */
  listDir(dir) {
    const prefix = dir.endsWith('/') ? dir : `${dir}/`
    const result = []
    for (const path of this.files.keys()) {
      if (path.startsWith(prefix)) result.push(path.slice(prefix.length))
    }
    return result
  }
}

function findPackageDir(archive, specifier) {
  // 包名可能是 @scope/name 或 name；subpath 导出不带目录
  const segments = specifier.split('/')
  const dirName = specifier.startsWith('@') ? segments.slice(0, 2).join('/') : segments[0]
  const dir = `${ASAR_PREFIX}/${dirName}`
  if (archive.has(`${dir}/package.json`)) return { dir, dirName }
  return undefined
}

function readJson(buffer) {
  // asar 成员的 size 可能含 pickle 的尾部补齐字节，切到最后一个 '}' 再解析
  let text = buffer.toString('utf8')
  const start = text.indexOf('{')
  text = text.slice(start)
  const end = text.lastIndexOf('}')
  return JSON.parse(text.slice(0, end + 1))
}

function ensureDir(path) {
  mkdirSync(path, { recursive: true })
}

function extractPackage(archive, specifier, extracted, missing) {
  const found = findPackageDir(archive, specifier)
  if (found === undefined) {
    missing.add(specifier)
    return
  }
  const { dir, dirName } = found
  if (extracted.has(dirName)) return

  const members = archive.listDir(dir)
  for (const member of members) {
    const target = join(VENDOR_DIR, dirName, member)
    ensureDir(dirname(target))
    writeFileSync(target, archive.read(`${dir}/${member}`))
  }
  extracted.set(dirName, dir)
}

function collectDependencies(archive, specifier, extracted, missing, seen = new Set()) {
  if (seen.has(specifier)) return
  seen.add(specifier)

  const found = findPackageDir(archive, specifier)
  if (found === undefined) {
    missing.add(specifier)
    return
  }
  extractPackage(archive, specifier, extracted, missing)

  const manifest = readJson(archive.read(`${found.dir}/package.json`))
  const deps = Object.keys({
    ...(manifest.dependencies ?? {}),
    ...(manifest.peerDependencies ?? {}),
  })
  for (const dep of deps) {
    // 只关心同族包与 schemastery/cosmokit 这类运行时依赖；外部 npm 包由宿主提供
    if (dep.startsWith('@deepseek-ai/') || dep === 'cosmokit' || dep === 'schemastery') {
      collectDependencies(archive, dep, extracted, missing, seen)
    }
  }
}

function linkTopLevel() {
  ensureDir(NODE_MODULES)
  ensureDir(join(NODE_MODULES, '@deepseek-ai'))

  const scopeDir = join(VENDOR_DIR, '@deepseek-ai')
  const scoped = existsSync(scopeDir) ? readdirSync(scopeDir) : []
  for (const name of scoped) {
    linkInto(join(NODE_MODULES, '@deepseek-ai', name), join(scopeDir, name))
  }
  for (const name of ['cosmokit', 'schemastery']) {
    const source = join(VENDOR_DIR, name)
    if (existsSync(source)) linkInto(join(NODE_MODULES, name), source)
  }
  // 类型包也要能从顶层解析到，否则 tsc 找不到 node 的类型
  const typesDir = join(VENDOR_DIR, '@types')
  if (existsSync(typesDir)) {
    ensureDir(join(NODE_MODULES, '@types'))
    for (const name of readdirSync(typesDir)) {
      linkInto(join(NODE_MODULES, '@types', name), join(typesDir, name))
    }
  }
}

function linkInto(linkPath, target) {
  if (existsSync(linkPath)) {
    const stat = statSync(linkPath)
    if (stat.isSymbolicLink() || stat.isDirectory()) rmSync(linkPath, { recursive: true, force: true })
  }
  symlinkSync(target, linkPath, 'dir')
}

async function verify() {
  const require_ = createRequire(join(REPO_ROOT, 'plugin', 'package.json'))
  const checks = []
  // 逐个顺序 import：schemastery 的 CJS 包装会 require 一个 ESM，并发导入会触发
  // Node 的 "not yet fully loaded" 竞态，所以绝不能 Promise.all。
  for (const specifier of ['@deepseek-ai/dsh-tools']) {
    try {
      const resolved = require_.resolve(specifier)
      const mod = await import(resolved)
      checks.push({ specifier, ok: true, exports: Object.keys(mod).slice(0, 12) })
    } catch (error) {
      checks.push({ specifier, ok: false, error: String(error?.message ?? error) })
    }
  }
  return checks
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (!existsSync(args.asar)) {
    console.error(`找不到 app.asar：${args.asar}`)
    process.exitCode = 1
    return
  }

  const archive = new AsarArchive(args.asar)

  if (args.list) {
    const names = new Set()
    for (const path of archive.files.keys()) {
      const match = path.match(/^\/dsh\/node_modules\/((?:@[^/]+\/)?[^/]+)\/package\.json$/)
      if (match) {
        const manifest = readJson(archive.read(path))
        names.add(`${match[1]}@${manifest.version}`)
      }
    }
    console.log([...names].sort().join('\n'))
    return
  }

  rmSync(VENDOR_DIR, { recursive: true, force: true })
  ensureDir(VENDOR_DIR)

  const extracted = new Map()
  const missing = new Set()
  for (const specifier of ROOT_PACKAGES) {
    collectDependencies(archive, specifier, extracted, missing)
  }
  for (const specifier of EXTRA_PACKAGES) {
    extractPackage(archive, specifier, extracted, missing)
  }

  // 把 @scope/name 形式也落成 vendor/@scope/name
  linkTopLevel()

  if (!args.quiet) {
    console.log(`已抽取 ${extracted.size} 个包到 ${VENDOR_DIR}`)
    for (const name of [...extracted.keys()].sort()) console.log(`  ${name}`)
    if (missing.size > 0) {
      console.log(`\nasar 内未找到（可忽略，通常是外部 npm 依赖）：`)
      for (const name of [...missing].sort()) console.log(`  ${name}`)
    }
  }

  if (args.verify) {
    const checks = await verify()
    console.log('\n导入自检：')
    for (const check of checks) {
      console.log(check.ok ? `  ✅ ${check.specifier} → ${check.exports.join(', ')}` : `  ❌ ${check.specifier}: ${check.error}`)
    }
    if (checks.some((check) => !check.ok)) process.exitCode = 1
  }
}

await main()
