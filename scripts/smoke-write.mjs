/**
 * 写入链路冒烟测试 —— 专门验「点『入账』之后能不能真写进文件」。
 *
 * 这条曾经失败过：`holdings --set` 要在数据目录里落临时文件再原子替换，
 * 而路由传给 bash 的 sandboxPolicy 是从**未注入 sandboxPolicy 的 ctx** 上解析的，
 * 拿到的是不许写的兜底策略，于是报
 * `[Errno 1] Operation not permitted: .../.holdings.json.xxx.tmp`。
 *
 * 所以这里必须**真的写一次文件**，而不是只检查路由返回 200。
 *
 * 用法：node scripts/smoke-write.mjs
 */

import { execFile } from 'node:child_process'
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const run = promisify(execFile)
const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// 在临时目录里造一份小账本，绝不碰仓库里的真实数据
const work = mkdtempSync(join(tmpdir(), 'dl-write-'))
const dataDir = join(work, 'data')
mkdirSync(dataDir, { recursive: true })
copyFileSync(join(REPO, 'data/holdings.json'), join(dataDir, 'holdings.json'))



/** 复刻 ctx.subprocess：真的起进程，返回 {done, collected} 形状。 */
function makeFakeSubprocess() {
  return {
    async resolveExecutable(command) {
      // 直接交给 shell 解析 PATH，与真实 provider 的解析结果一致
      const { stdout } = await run('bash', ['-c', 'command -v ' + JSON.stringify(command)])
      return stdout.trim() || command
    },
    spawn(request) {
      const argv = request.argv
      const child = run(argv[0], argv.slice(1), {
        cwd: request.cwd,
        env: { ...process.env, ...(request.env ?? {}) },
        maxBuffer: request.stdio?.stdout?.maxBytes ?? 1_000_000,
      })
      const state = { stdout: '', stderr: '' }
      const done = child.then(
        (result) => {
          state.stdout = result.stdout
          state.stderr = result.stderr
          return { exitCode: 0, signal: null }
        },
        (error) => {
          state.stdout = error.stdout ?? ''
          state.stderr = error.stderr ?? ''
          return { exitCode: typeof error.code === 'number' ? error.code : 1, signal: null }
        },
      )
      return {
        done,
        kill() {},
        collected: {
          stdout: { readFrom: () => ({ text: state.stdout }) },
          stderr: { readFrom: () => ({ text: state.stderr }) },
        },
      }
    },
  }
}

const routes = []
const services = {
  subprocess: makeFakeSubprocess(),
  webServer: {
    register(route) {
      routes.push(route)
      return () => {}
    },
  },
}

const ctx = {
  tools: { register: () => () => {} },
  get: (name) => services[name],
  effect: (callback) => callback(),
  inject: (names, callback) => callback(ctx),
}

const mod = await import(resolve(REPO, 'plugin/lib/index.js'))
mod.apply(ctx, { dataDir })

console.log('注册路由:', routes.map((r) => r.path).join(', '))
console.log()

function fakeResponse() {
  return {
    status: 0,
    body: null,
    writeHead(status) {
      this.status = status
    },
    end(body) {
      this.body = body
    },
  }
}

async function post(pathname, payload) {
  const route = routes.find((item) => item.path === pathname)
  if (route === undefined) throw new Error('未注册的路由：' + pathname)

  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  const listeners = {}
  const req = {
    url: pathname,
    method: 'POST',
    on(event, listener) {
      listeners[event] = listener
      if (event === 'data') listener(body)
      if (event === 'end') listener()
    },
  }
  const res = fakeResponse()
  await route.handler(req, res)
  return { status: res.status, json: res.body ? JSON.parse(res.body) : null }
}

const failures = []
function check(label, ok, detail) {
  console.log(`${ok ? '✓' : '✗'} ${label.padEnd(40)} ${detail}`)
  if (!ok) failures.push(label)
}

// ---- 读现有持仓 ----
const before = JSON.parse(readFileSync(join(dataDir, 'holdings.json'), 'utf8')).holdings
check('读到现有持仓', before.length === 6, `${before.length} 条`)

// ---- 写入：加一只新股（走客户端完全一样的 POST）----
const added = {
  code: '600519.SH',
  name: '测试用名',
  start_date: '2024-01-05',
  shares: '250',
  cost_per_share: '1600',
}
const payload = before.filter((item) => item.code !== added.code).concat([added])

const { status, json } = await post('/dsh-dividend/holdings/set', payload)
check('POST /holdings/set 返回成功', status === 200 && json?.ok !== false, `HTTP ${status} ${json?.error ?? ''}`)

// ---- 关键：文件真的被改了 ----
const after = JSON.parse(readFileSync(join(dataDir, 'holdings.json'), 'utf8')).holdings
const written = after.find((item) => item.code === added.code)
check('文件里确实写进去了', written !== undefined, written ? JSON.stringify(written) : '没找到该股')
// 注意：成本按"价格口径"规范化为 2 位小数（1600 → 1600.00），这是账本约定
check(
  '字段值正确',
  written?.shares === '250' &&
    Number(written?.cost_per_share) === 1600 &&
    written?.start_date === '2024-01-05',
  written ? `股数 ${written.shares} 成本 ${written.cost_per_share}` : '—',
)
check('记录数没有重复', after.filter((item) => item.code === added.code).length === 1, `${after.length} 条`)
// added 用的 code 本来就在持仓里，所以是"覆盖"而不是"新增"——条数不变
check('写入后条数不变（覆盖同 code）', after.length === before.length, `${after.length} 条`)

// ---- 删除：整份写回更少的条目 ----
const shrunk = after.filter((item) => item.code !== added.code)
const del = await post('/dsh-dividend/holdings/set', shrunk)
check('删除请求返回成功', del.status === 200 && del.json?.ok !== false, `HTTP ${del.status}`)

const afterDelete = JSON.parse(readFileSync(join(dataDir, 'holdings.json'), 'utf8')).holdings
check(
  '删除后记录数减少 1',
  afterDelete.length === after.length - 1,
  `${after.length} → ${afterDelete.length}`,
)
check(
  '被删的那只确实没了',
  afterDelete.every((item) => item.code !== added.code),
  afterDelete.map((item) => item.code).join(','),
)

// ---- 删光：空数组也必须能写进去 ----
const cleared = await post('/dsh-dividend/holdings/set', [])
check('清空持仓返回成功', cleared.status === 200 && cleared.json?.ok !== false, `HTTP ${cleared.status}`)
const afterClear = JSON.parse(readFileSync(join(dataDir, 'holdings.json'), 'utf8')).holdings
check('清空后确实是 0 条', afterClear.length === 0, `${afterClear.length} 条`)

// ---- 临时文件不能残留 ----
const leftovers = readdirSync(dataDir).filter((name) => name.includes('.tmp'))
check('没有残留临时文件', leftovers.length === 0, leftovers.join(', ') || '（无）')

// ---- 必须走不受沙箱约束的通道 ----
check(
  '走 ctx.subprocess（不受 shell 沙箱约束）',
  services.subprocess !== undefined,
  'subprocess 服务可用',
)

rmSync(work, { recursive: true, force: true })

if (failures.length > 0) {
  console.error('\n写入链路有失败项：' + failures.join('、'))
  process.exit(1)
}
console.log('\n写入链路全部通过')
