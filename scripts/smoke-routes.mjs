/**
 * 路由冒烟测试：打通「路由 → ctx.subprocess → Python CLI」这条链路。
 *
 * 用法：
 *   node scripts/smoke-routes.mjs                # 用 data/real
 *   node scripts/smoke-routes.mjs --offline      # 不联网（点击搜索走本地匹配）
 *   node scripts/smoke-routes.mjs --query 平安
 *
 * 为什么要它：客户端面板只有在浏览器里才看得到，但**数据链路**可以先验证。
 * 每次重启应用之前跑一遍，能把"路由挂了/参数拼错了"这类问题提前挖出来。
 */

import { execFile } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const run = promisify(execFile)
// 基于脚本自身位置解析仓库根——否则从别的目录调用会找不到模块
const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const args = process.argv.slice(2)
const offline = args.includes('--offline')
const queryIndex = args.indexOf('--query')
const dataDir = REPO + '/data/real'

/**
 * 默认查询词从**实际持仓**里取，不写死。
 * 写死过"茅台"，后来持仓换成了别的股票，离线搜索自然匹配不到，
 * 于是冒烟测试假失败——测试不该依赖某个特定持仓。
 */
function defaultQuery() {
  try {
    const payload = JSON.parse(readFileSync(resolve(dataDir, 'holdings.json'), 'utf8'))
    const first = (payload.holdings ?? [])[0]
    if (first !== undefined) return first.name || first.code
  } catch {
    /* 读不到就退回代码 */
  }
  return '600519'
}

const query = queryIndex >= 0 ? args[queryIndex + 1] : defaultQuery()

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
  effect(callback) {
    callback()
  },
  // 复刻 ctx.inject：拿到服务后带 scoped ctx 回调
  inject(names, callback) {
    const scoped = { ...ctx, get: (name) => services[name] }
    callback(scoped)
  },
}

const mod = await import(resolve(REPO, 'plugin/lib/index.js'))
mod.apply(ctx, { dataDir })

console.log('注册路由:', routes.map((route) => route.path).join('\n           '))
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

async function hit(pathname, url) {
  const route = routes.find((item) => item.path === pathname)
  if (route === undefined) throw new Error('未注册的路由：' + pathname)
  const res = fakeResponse()
  const startedAt = Date.now()
  await route.handler({ url: url ?? pathname, method: 'GET' }, res)
  const elapsed = Date.now() - startedAt
  let json = null
  try {
    json = res.body ? JSON.parse(res.body) : null
  } catch {
    json = { parseError: String(res.body).slice(0, 120) }
  }
  // 502/503 时把宿主返回的原因打出来，否则只看到"HTTP 502"没法排查
  if (res.status >= 400 && json && json.error) {
    console.log(`    ↳ ${pathname} 失败原因: ${json.error}`)
  }
  return { status: res.status, json, elapsed }
}

function line(label, ok, detail) {
  const mark = ok ? '✓' : '✗'
  console.log(`${mark} ${label.padEnd(34)} ${detail}`)
}

// ---- 状态 ----
{
  const { status, json } = await hit('/dsh-dividend/status')
  line(
    'GET /status',
    status === 200 && json?.ok !== false,
    `HTTP ${status} 持仓 ${json?.holdings?.count ?? '?'} 事件 ${json?.events?.count ?? '?'}`,
  )
}

// ---- 持仓 ----
{
  const { status, json } = await hit('/dsh-dividend/holdings')
  line('GET /holdings', status === 200 && json?.ok !== false, `HTTP ${status} ${json?.count ?? '?'} 条`)
}

// ---- 报告 ----
{
  const { status, json } = await hit('/dsh-dividend/report')
  const totals = json?.totals ?? {}
  line(
    'GET /report',
    status === 200 && json?.ok !== false,
    `HTTP ${status} 税前 ¥${totals.cash_amount ?? '?'} 收益率 ${totals.yield_on_cost ?? '?'}`,
  )
}

// ---- 持仓视图 + 组合股息率 ----
{
  const { status, json, elapsed } = await hit(
    '/dsh-dividend/portfolio',
    '/dsh-dividend/portfolio' + (offline ? '?offline=1' : ''),
  )
  const totals = json?.totals ?? {}
  line(
    'GET /portfolio',
    status === 200 && json?.ok !== false,
    `HTTP ${status} / ${elapsed}ms 持仓 ${json?.count ?? '?'} 只`,
  )
  for (const row of json?.rows ?? []) {
    console.log(
      `    ${String(row.code).padEnd(12)}${String(row.name).padEnd(10)}` +
        `${String(row.shares ?? '—').padStart(8)} 股  ` +
        `价 ${String(row.price ?? '—').padEnd(9)}` +
        `动态 ${String(row.dynamicYield ?? '—').padEnd(11)}预期 ${row.expectedYield ?? '—'}`,
    )
  }
  console.log(
    `    合计  市值 ${totals.marketValue ?? '—'}  ` +
      `总动态 ${totals.dynamicYield ?? '—'}  总预期 ${totals.expectedYield ?? '—'}  ` +
      `成本收益率 ${totals.yieldOnCost ?? '—'}`,
  )
  if (json?.priceError) console.log('    priceError:', json.priceError)

  // 待收区块：必须存在，且**历史累计里不能含它**
  const up = json?.upcoming
  if (up !== undefined) {
    console.log(
      `    待收 ${up.eventCount} 笔（确定 ${up.confirmedAmount} / 预案 ${up.proposedAmount}）`,
    )
    for (const row of up.rows ?? []) {
      console.log(
        `      ${row.month}  ${String(row.code).padEnd(12)}${String(row.name).padEnd(10)}` +
          `${String(row.perShare).padStart(9)} × ${String(row.shares).padStart(7)} 股 = ` +
          `${String(row.cashAmount).padStart(11)}  [${row.certain ? '确定' : '预案'}]`,
      )
    }
    const hasUpcomingShape =
      Array.isArray(up.rows) && typeof up.confirmedAmount === 'string' && 'proposedAmount' in up
    line('  upcoming 区块结构完整', hasUpcomingShape, hasUpcomingShape ? 'ok' : '字段缺失')
  } else {
    line('  upcoming 区块存在', false, 'portfolio 响应里没有 upcoming')
  }
}

// ---- 搜索 ----
{
  const url = `/dsh-dividend/search?q=${encodeURIComponent(query)}${offline ? '&offline=1' : ''}`
  const { status, json, elapsed } = await hit('/dsh-dividend/search', url)
  const rows = json?.rows ?? []
  line(
    `GET /search?q=${query}${offline ? '&offline=1' : ''}`,
    status === 200 && json?.ok !== false && rows.length > 0,
    `HTTP ${status} ${rows.length} 条 / ${elapsed}ms`,
  )
  if (json?.searchError) console.log('    searchError:', json.searchError)
  if (json?.priceError) console.log('    priceError :', json.priceError)
  for (const row of rows) {
    console.log(
      `    ${String(row.code).padEnd(12)}${String(row.name).padEnd(10)}` +
        `价 ${String(row.price ?? '—').padEnd(9)}` +
        `动态 ${String(row.dynamicYield ?? '—').padEnd(11)}` +
        `预期 ${row.expectedYield ?? '—'}`,
    )
  }
}

// ---- 单股详情（面板展开某一行时调）----
{
  const url = `/dsh-dividend/detail?code=600519.SH&name=${encodeURIComponent('贵州茅台')}${offline ? '&offline=1' : ''}`
  const { status, json, elapsed } = await hit('/dsh-dividend/detail', url)
  const row = json?.row ?? {}
  line(
    'GET /detail?code=600519.SH',
    status === 200 && json?.ok !== false,
    `HTTP ${status} / ${elapsed}ms 价 ${row.price ?? '—'} 动态 ${row.dynamicYield ?? '—'}`,
  )
  if (json?.fetchError) console.log('    fetchError:', json.fetchError)
  if (json?.priceError) console.log('    priceError:', json.priceError)
}

// ---- 空查询应短路，不打网络 ----
{
  const { status, json } = await hit('/dsh-dividend/search', '/dsh-dividend/search?q=')
  line('GET /search?q=（空）', status === 200 && json?.count === 0, `HTTP ${status} count=${json?.count}`)
}
