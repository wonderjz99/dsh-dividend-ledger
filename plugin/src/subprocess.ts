/**
 * 子进程执行：给宿主路由用。
 *
 * ── 为什么不走 `ctx.shell` ───────────────────────────────────────────
 * `ctx.shell` 背后是 `dsh-bash-sandbox`，而 **workspace-write 只允许写
 * 「策略 workspace root 之下 + 平台临时目录」**。本插件的数据目录在仓库里，
 * 通常不在那个根之下，于是 `holdings --set` 的原子写在落临时文件时被拒：
 *
 *     [Errno 1] Operation not permitted: .../data/real/.holdings.json.xxx.tmp
 *
 * 换 `ctx.fs` 也解决不了——`dsh-fs-sandbox` 的文档明确写了它与 shell 家族
 * **共用同一份策略、绝不圈到不同根**（"the filesystem and shell families never
 * confine to different roots"）。
 *
 * ── 为什么用 `ctx.subprocess` 是正确的 ───────────────────────────────
 * 1. 这些调用是**插件自身的操作**（用户点了面板上的「入账」），不是对话里
 *    的 agent 在跑 shell。插件宿主代码本来就运行在沙箱之外（官方
 *    `dsh-plugin-manager` README 的原话），所以这**没有扩大 agent 的权限边界**。
 * 2. `ctx.subprocess` 是官方文档化的服务，不经过 shell 解释器，直接以 argv
 *    启动进程——注入面比 `bash -c` 小。
 * 3. 不该为此去放宽 profile 的全局沙箱策略：那会影响所有会话，代价远大于收益。
 *
 * 对话内的 agent shell 仍走 `ctx.shell`，约束不变。
 */

import type { PluginContext } from './dsh-types.js'

/** 进程退出后的输出。 */
export interface SubprocessOutcome {
  readonly stdout: string
  readonly stderr: string
  readonly exitCode: number
  readonly timedOut: boolean
}

interface CollectedStream {
  readFrom(offset: number): { text?: string } | undefined
}

interface SubprocessHandle {
  readonly done: Promise<unknown>
  readonly collected?: { readonly stdout?: CollectedStream; readonly stderr?: CollectedStream }
  kill?(signal?: string): void
}

interface SubprocessService {
  resolveExecutable(command: string): Promise<string>
  spawn(request: Record<string, unknown>): SubprocessHandle
}

export class SubprocessUnavailableError extends Error {
  constructor() {
    super(
      '宿主没有提供子进程服务（ctx.subprocess）。'
        + '该组合下插件无法读写数据目录里的账本文件。',
    )
    this.name = 'SubprocessUnavailableError'
  }
}

/**
 * 跑一次子进程并把输出读全。
 *
 * @param timeoutMs 超时；超时会 kill 进程并标记 `timedOut`。
 * @throws {SubprocessUnavailableError} 宿主没有 subprocess 服务。
 */
export async function runSubprocess(
  ctx: PluginContext,
  options: {
    command: string
    args: readonly string[]
    cwd: string
    env?: Record<string, string>
    timeoutMs: number
    maxBytes?: number
    signal?: AbortSignal
  },
): Promise<SubprocessOutcome> {
  const service = getSubprocess(ctx)
  if (service === undefined) throw new SubprocessUnavailableError()

  const executable = await service.resolveExecutable(options.command)
  const maxBytes = options.maxBytes ?? 1_000_000

  const request: Record<string, unknown> = {
    argv: [executable, ...options.args],
    cwd: options.cwd,
    stdio: {
      stdin: 'ignore',
      stdout: { maxBytes },
      stderr: { maxBytes },
    },
    graceMs: 3000,
  }
  if (options.env !== undefined) request.env = options.env
  if (options.signal !== undefined) request.signal = options.signal

  const handle = service.spawn(request)

  let timedOut = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<'timeout'>((resolve) => {
    timer = setTimeout(() => {
      timedOut = true
      resolve('timeout')
    }, options.timeoutMs)
    // 别让这个定时器把进程拖住
    if (typeof timer === 'object' && typeof timer.unref === 'function') timer.unref()
  })

  try {
    await Promise.race([handle.done, timeout])
  } catch (error) {
    // done 拒绝表示启动失败或 provider 故障——把它说清楚
    throw new Error(
      `启动子进程失败：${error instanceof Error ? error.message : String(error)}`,
    )
  } finally {
    if (timer !== undefined) clearTimeout(timer)
    if (timedOut) {
      try {
        handle.kill?.()
      } catch {
        // 进程可能已经退出
      }
    }
  }

  const stdout = readCollected(handle, 'stdout')
  const stderr = readCollected(handle, 'stderr')
  const exitCode = timedOut ? -1 : exitCodeOf(await settle(handle))

  return { stdout, stderr, exitCode, timedOut }
}

/** `done` 的解析值形状未在文档中固定，稳妥地取 exitCode。 */
async function settle(handle: SubprocessHandle): Promise<unknown> {
  try {
    return await handle.done
  } catch {
    return undefined
  }
}

function exitCodeOf(done: unknown): number {
  if (typeof done === 'object' && done !== null && 'exitCode' in done) {
    const value = (done as { exitCode?: unknown }).exitCode
    if (typeof value === 'number') return value
  }
  return 0
}

function readCollected(handle: SubprocessHandle, stream: 'stdout' | 'stderr'): string {
  try {
    const collected = handle.collected?.[stream]
    const slice = collected?.readFrom(0)
    return slice?.text ?? ''
  } catch {
    return ''
  }
}

/**
 * 取 `ctx.subprocess`。
 *
 * 走 `ctx.get()`：未声明的服务用属性访问会抛异常（Cordis 的访问控制），
 * 所以调用方必须先用 `ctx.inject(['subprocess'], ...)` 拿到注入过的上下文。
 */
export function getSubprocess(ctx: PluginContext): SubprocessService | undefined {
  const direct = tryGet(ctx, 'subprocess')
  if (direct !== undefined) return direct as SubprocessService
  try {
    const value = (ctx as unknown as Record<string, unknown>)['subprocess']
    return value === undefined || value === null ? undefined : (value as SubprocessService)
  } catch {
    return undefined
  }
}

function tryGet(ctx: PluginContext, name: string): unknown {
  if (typeof ctx.get !== 'function') return undefined
  try {
    return ctx.get(name) ?? undefined
  } catch {
    return undefined
  }
}
