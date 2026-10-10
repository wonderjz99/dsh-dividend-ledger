/**
 * 宿主侧 HTTP 路由 —— 客户端面板的数据通道。
 *
 * 与工具的关系：工具给**对话**用，路由给**浏览器面板**用。两者都只是把请求
 * 转成一次 Python CLI 调用，业务逻辑一律在 Python 核心。
 *
 * **执行通道用 `ctx.subprocess` 而不是 `ctx.shell`**，理由见
 * `subprocess.ts` 顶部注释：shell 家族受 workspace-write 限制，而本插件的
 * 数据目录通常不在那个 workspace root 之下，写持仓会被拒
 * （`Operation not permitted: ....tmp`）。`ctx.fs` 共用同一份策略，同样不行。
 */

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { MAX_TIMEOUT_MS } from './config.js'
import type { PluginContext } from './dsh-types.js'
import { getService } from './python.js'
import { getSubprocess, runSubprocess, SubprocessUnavailableError } from './subprocess.js'
import type { ToolRuntimeEnv } from './tools.js'

/** 路由前缀：客户端 fetch 的就是这些路径。 */
export const ROUTE_PREFIX = '/dsh-dividend'

/** 单次路由调用的默认超时。持仓视图要取价，给足余量。 */
const ROUTE_TIMEOUT_MS = 90_000

interface HttpRequest {
  url?: string
  method?: string
  on?(event: string, listener: (chunk: Uint8Array) => void): void
}

interface HttpResponse {
  writeHead(status: number, headers: Record<string, string>): void
  end(body?: string): void
}

interface WebServerService {
  register(route: {
    kind: 'exact'
    path: string
    handler: (req: HttpRequest, res: HttpResponse) => unknown
  }): () => void
}

export interface RouteDeps {
  /**
   * 已注入 `webServer` 的上下文。
   *
   * 路由用的执行通道是 `ctx.subprocess`（不受沙箱约束），所以**不需要**注入
   * `sandboxPolicy`。这里保留 deps.ctx 是为了让 selfcheck 能报告服务可用性。
   */
  readonly ctx: PluginContext
  readonly env: ToolRuntimeEnv
}

/** 注册全部路由。返回清理函数（交给 ctx.effect）。 */
export function mountRoutes(deps: RouteDeps): () => void {
  const webServer = getService<WebServerService>(deps.ctx, 'webServer')
  if (webServer === undefined || typeof webServer.register !== 'function') {
    console.error(
      '[dividend-ledger] webServer 服务不可用，浏览器面板的路由未注册；'
        + '对话内的工具不受影响。',
    )
    return () => {}
  }

  const disposers: Array<() => void> = []
  const register = (
    path: string,
    handler: (req: HttpRequest, res: HttpResponse) => unknown,
  ): void => {
    disposers.push(
      webServer.register({ kind: 'exact', path: `${ROUTE_PREFIX}${path}`, handler }),
    )
  }

  register('/search', async (req, res) => {
    const params = queryOf(req)
    const query = (params.get('q') ?? '').trim()
    if (query === '') {
      sendJson(res, 200, { ok: true, query, count: 0, rows: [] })
      return
    }
    const args = ['candidates', '--query', query, '--limit', '10']
    if (params.get('offline') === '1') args.push('--offline')
    await respondWithCli(deps, res, args)
  })

  // 单只股票指标：面板里展开某一行时才调，避免搜索列表把限频打满
  register('/detail', async (req, res) => {
    const params = queryOf(req)
    const code = (params.get('code') ?? '').trim()
    if (code === '') {
      sendJson(res, 400, { ok: false, error: '缺少 code 参数' })
      return
    }
    const args = ['detail', '--code', code]
    const name = params.get('name')
    if (name !== null && name !== '') args.push('--name', name)
    if (params.get('offline') === '1') {
      args.push('--offline')
    } else if (params.get('refresh') !== '0') {
      // 默认刷新：展开就是想要这只股票的真实数字
      args.push('--refresh')
    }
    await respondWithCli(deps, res, args)
  })

  // 持仓视图：逐股指标 + 组合总股息率
  register('/portfolio', async (req, res) => {
    const params = queryOf(req)
    const args = ['portfolio']
    const names = params.get('names')
    if (names !== null && names !== '') args.push('--names', names)
    if (params.get('offline') === '1') args.push('--offline')
    await respondWithCli(deps, res, args)
  })

  register('/holdings', async (_req, res) => {
    await respondWithCli(deps, res, ['holdings'])
  })

  register('/report', async (req, res) => {
    const params = queryOf(req)
    const args = ['report']
    const now = params.get('now')
    if (now !== null && now !== '') args.push('--now', now)
    await respondWithCli(deps, res, args)
  })

  register('/status', async (_req, res) => {
    await respondWithCli(deps, res, ['validate'])
  })

  register('/holdings/set', async (req, res) => {
    const body = await readBody(req)
    if (body.trim() === '') {
      sendJson(res, 400, { ok: false, error: '请求体为空，期望持仓 JSON 数组' })
      return
    }
    // 用 `--replace`：面板既要"改股数"也要"删掉这只"，合并语义表达不了删除。
    // 客户端为此发送的是**整份**持仓列表，替换正是它想要的语义。
    await withTempJson(body, async (path) => {
      await respondWithCli(deps, res, ['holdings', '--set', path, '--replace'])
    })
  })

  // 自检：把真实生效的路径与服务可用性暴露出来，便于排查"面板不工作"。
  register('/selfcheck', async (_req, res) => {
    const ctx = deps.ctx
    sendJson(res, 200, {
      ok: true,
      dataDir: deps.env.dataDir,
      repoRoot: deps.env.repoRoot,
      pythonCommand: deps.env.pythonCommand,
      timeoutMs: deps.env.config.timeoutMs,
      services: {
        webServer: true,
        shell: getService(ctx, 'shell') !== undefined,
        sandboxPolicy: getService(ctx, 'sandboxPolicy') !== undefined,
        subprocess: getSubprocess(ctx) !== undefined,
        fs: getService(ctx, 'fs') !== undefined,
      },
    })
  })

  console.log(`[dividend-ledger] 已注册 ${disposers.length} 条面板路由于 ${ROUTE_PREFIX}/*`)

  return () => {
    for (const dispose of disposers.reverse()) {
      try {
        dispose()
      } catch {
        // 卸载失败不该阻断其它路由的清理
      }
    }
  }
}

/** 跑一次 Python CLI 并把它的 JSON 原样转给浏览器。 */
async function respondWithCli(
  deps: RouteDeps,
  res: HttpResponse,
  args: readonly string[],
): Promise<void> {
  try {
    const payload = await callCli(deps, args)
    sendJson(res, 200, payload)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    const status = error instanceof SubprocessUnavailableError ? 503 : 502
    sendJson(res, status, { ok: false, error: message })
  }
}

async function callCli(deps: RouteDeps, args: readonly string[]): Promise<unknown> {
  const { env } = deps
  const outcome = await runSubprocess(deps.ctx, {
    command: env.pythonCommand,
    // 全局参数必须在子命令之前（CLI 用 argparse）
    args: ['-m', 'dividend_ledger', '--data', env.dataDir, ...args],
    cwd: env.repoRoot,
    // 不走 shell，所以不需要哨兵：CLI 的 stdout 本身就是干净 JSON
    env: { PYTHONPATH: env.repoRoot },
    timeoutMs: Math.min(Math.max(env.config.timeoutMs, ROUTE_TIMEOUT_MS), MAX_TIMEOUT_MS * 3),
    maxBytes: 4 * 1024 * 1024,
  })

  if (outcome.timedOut) {
    throw new Error(`调用超时（${ROUTE_TIMEOUT_MS} ms）`)
  }

  const text = outcome.stdout.trim()
  if (outcome.exitCode !== 0) {
    const reason = firstLine(outcome.stderr) || firstLine(text) || `退出码 ${outcome.exitCode}`
    throw new Error(reason)
  }
  if (text === '') {
    throw new Error('Python 核心没有输出（stderr 也为空）')
  }

  try {
    return JSON.parse(text)
  } catch {
    throw new Error(`Python 输出不是合法 JSON：${text.slice(0, 160)}`)
  }
}

function firstLine(text: string): string {
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed !== '' && !trimmed.startsWith('Traceback')) return trimmed
  }
  return ''
}

function queryOf(req: HttpRequest): URLSearchParams {
  return new URL(req.url ?? '/', 'http://localhost').searchParams
}

function sendJson(res: HttpResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(body)
}

async function readBody(req: HttpRequest): Promise<string> {
  if (typeof req.on !== 'function') return ''
  const chunks: Uint8Array[] = []
  await new Promise<void>((resolve) => {
    req.on?.('data', (chunk: Uint8Array) => chunks.push(chunk))
    req.on?.('end', () => resolve())
  })
  return Buffer.concat(chunks).toString('utf8')
}

/** 把一段 JSON 落成临时文件（CLI 的 --set 走文件，没有 stdin 通道）。 */
async function withTempJson<T>(
  content: string,
  callback: (path: string) => Promise<T>,
): Promise<T> {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-dividend-route-'))
  const path = join(dir, 'holdings.json')
  try {
    writeFileSync(path, content, 'utf8')
    return await callback(path)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}
