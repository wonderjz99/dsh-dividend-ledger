/**
 * 调用 Python 核心的**唯一**通道。
 *
 * 四条设计决定，都有具体理由：
 *
 * 1. **走 `ctx.shell` 而不是 `child_process.spawn`。**
 *    DSH 的文件沙箱与审批挂在 shell 执行器上；直接 spawn 拿到的是**无沙箱**子进程
 *    （官方 `dsh-plugin-manager` README 明确说明宿主插件在沙箱之外运行）。
 * 2. **必须把解析后的 `sandboxPolicy` 塞进 request。**
 *    执行器靠这个字段才知道该用什么 confinement；只传 `workdir` 不够。
 * 3. **输出用哨兵包裹后再取。**
 *    Python 的 stdout 虽然干净，但哨兵能识别"stdout 被截断"这种危险情况，
 *    也能在解释器被换成会打横幅的包装脚本时仍然拿到正确的那段 JSON。
 * 4. **绝不由本层决定业务对错。**
 *    口径、算法、报错文案全部来自 Python；这里只搬运与翻译进程级故障。
 */

import type {
  PluginContext,
  ShellResult,
  ShellService,
  ToolExec,
} from './dsh-types.js'

/** Python 侧出错（数据错误、参数错误、解释器问题）时抛出。 */
export class PythonInvocationError extends Error {
  readonly exitCode: number | undefined
  readonly stderr: string
  readonly stdout: string
  readonly timedOut: boolean

  constructor(
    message: string,
    options: {
      exitCode?: number | undefined
      stderr?: string
      stdout?: string
      timedOut?: boolean
    } = {},
  ) {
    super(message)
    this.name = 'PythonInvocationError'
    this.exitCode = options.exitCode
    this.stderr = options.stderr ?? ''
    this.stdout = options.stdout ?? ''
    this.timedOut = options.timedOut ?? false
  }
}

export const SENTINEL_BEGIN = '__DIVIDEND_LEDGER_JSON_BEGIN__'
export const SENTINEL_END = '__DIVIDEND_LEDGER_JSON_END__'

/** POSIX 单引号转义：把内层单引号拼成 `'\''`。 */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'\\''`)}'`
}

export interface RunPythonOptions {
  /** Python 解释器命令名或绝对路径。 */
  pythonCommand: string
  /** `dividend_ledger` 包所在目录，作为 PYTHONPATH 传给子进程。 */
  repoRoot: string
  /** CLI 子命令及其参数（不含解释器与 `-m dividend_ledger`）。 */
  args: readonly string[]
  /** shell 的 cwd；沙箱策略解析出的 workspaceRoot 会覆盖它。 */
  workdir: string
  /** 单次调用超时（毫秒）。 */
  timeoutMs: number
  /** 已解析的沙箱策略；没有沙箱执行器时为 undefined。 */
  sandboxPolicy?: Record<string, unknown> | undefined
}

/** 组装内层命令行（不含哨兵）。导出以便单测直接断言转义行为。 */
export function buildCommand(options: RunPythonOptions): string {
  return [options.pythonCommand, '-m', 'dividend_ledger', ...options.args]
    .map(shellQuote)
    .join(' ')
}

/**
 * 把内层命令包上哨兵。
 *
 * 退出码会原样透出：`^=` 只改最低位，保证非零状态不会在管道/算术里被吃掉。
 */
export function withSentinel(command: string): string {
  return [
    `printf '%s\\n' ${shellQuote(SENTINEL_BEGIN)}`,
    command,
    '__dl_status=$?',
    `printf '%s\\n' ${shellQuote(SENTINEL_END)}`,
    'exit $__dl_status',
  ].join('; ')
}

/** 从带哨兵的 stdout 中取出 JSON 文本。 */
export function extractSentinelPayload(stdout: string): string | undefined {
  const begin = stdout.indexOf(SENTINEL_BEGIN)
  if (begin < 0) return undefined
  const end = stdout.indexOf(SENTINEL_END, begin + SENTINEL_BEGIN.length)
  if (end < 0) return undefined
  return stdout.slice(begin + SENTINEL_BEGIN.length, end).trim()
}

/**
 * 执行一次 Python CLI 调用并返回解析后的 JSON。
 *
 * @throws {PythonInvocationError} 超时、被取消、非零退出、输出无法解析。
 */
export async function runPythonJson<T = unknown>(
  ctx: PluginContext,
  exec: ToolExec,
  options: RunPythonOptions,
): Promise<T> {
  // 已经取消的调用连命令都不该拼——避免"取消了还起了进程"
  if (exec.signal?.aborted === true) {
    throw new PythonInvocationError('调用已被取消')
  }

  const shell = getService<ShellService>(ctx, 'shell')
  if (shell === undefined) {
    throw new PythonInvocationError(
      '宿主没有提供 shell 服务，无法调用 Python 核心（需要 ctx.shell）',
    )
  }

  const request: Record<string, unknown> = {
    command: withSentinel(buildCommand(options)),
    workdir: options.workdir,
    timeoutMs: options.timeoutMs,
    // PYTHONPATH 让子进程能 import 到 dividend_ledger，不必依赖 cwd
    dshEnv: { PYTHONPATH: options.repoRoot },
  }
  if (options.sandboxPolicy !== undefined) {
    request.sandboxPolicy = options.sandboxPolicy
  }

  let result: ShellResult
  try {
    const execution = await shell.execute(shell.resolve(request))
    result = await raceWithAbort(execution.result(), exec.signal)
  } catch (error) {
    if (isAbortError(error)) {
      throw new PythonInvocationError('调用已被取消')
    }
    throw new PythonInvocationError(`执行 Python 核心失败：${errorText(error)}`)
  }

  const stdout = result.stdout?.text ?? ''
  const stderr = result.stderr?.text ?? ''

  if (result.aborted === true) {
    throw new PythonInvocationError('调用已被取消', { stdout, stderr })
  }
  if (result.timedOut === true) {
    throw new PythonInvocationError(
      `调用超时（${options.timeoutMs} ms）。账本计算是纯内存运算，`
      + '超时通常说明解释器启动异常或数据规模远超预期。',
      { stdout, stderr, timedOut: true },
    )
  }
  if (result.stdout?.truncated === true) {
    throw new PythonInvocationError(
      'Python 输出被截断，拒绝解析不完整的 JSON。'
      + '请把结果写到文件（--out），只让摘要走 stdout。',
      { stdout, stderr, exitCode: result.exitCode ?? undefined },
    )
  }

  const exitCode = result.exitCode ?? 0
  const payloadText = extractSentinelPayload(stdout)

  if (exitCode !== 0) {
    // 只看 stderr：stdout 里是哨兵与部分输出，拿它当错误消息会得到
    // "__DIVIDEND_LEDGER_JSON_BEGIN__" 这种毫无意义的文本。
    const message = firstMeaningfulLine(stderr)
    throw new PythonInvocationError(
      message !== '' ? message : `Python 核心退出码 ${exitCode}`,
      { exitCode, stdout, stderr },
    )
  }

  if (payloadText === undefined || payloadText === '') {
    throw new PythonInvocationError(
      'Python 核心没有输出可解析的 JSON（哨兵内为空）；'
      + '请确认解释器能导入 dividend_ledger',
      { exitCode, stdout, stderr },
    )
  }

  try {
    return JSON.parse(payloadText) as T
  } catch (error) {
    throw new PythonInvocationError(`Python 输出不是合法 JSON：${errorText(error)}`, {
      exitCode,
      stdout,
      stderr,
    })
  }
}

/**
 * 取一个可选服务。
 *
 * 先试 `ctx.get()`（Cordis 上不会因未注入而抛错），失败再退回属性访问并兜住异常。
 * 绝不裸读 `ctx.x`——未注入时那是一定会抛的。
 */
export function getService<T>(ctx: PluginContext, name: string): T | undefined {
  if (typeof ctx.get === 'function') {
    try {
      const value = ctx.get(name)
      if (value !== undefined && value !== null) return value as T
    } catch {
      // 继续走属性访问的兜底
    }
  }
  try {
    const value = (ctx as unknown as Record<string, unknown>)[name]
    return (value as T | undefined) ?? undefined
  } catch {
    return undefined
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object'
    && error !== null
    && 'name' in error
    && (error as { name?: unknown }).name === 'AbortError'
  )
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 取第一行有内容的信息，避免把整段堆栈塞给模型。 */
export function firstMeaningfulLine(text: string): string {
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed !== '' && !trimmed.startsWith('Traceback') && !trimmed.startsWith('at ')) {
      return trimmed
    }
  }
  return ''
}

async function raceWithAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal === undefined) return promise
  if (signal.aborted) throw abortError()

  return new Promise<T>((resolvePromise, rejectPromise) => {
    const onAbort = (): void => {
      rejectPromise(abortError())
    }
    signal.addEventListener('abort', onAbort, { once: true })
    promise.then(
      (value) => {
        signal.removeEventListener('abort', onAbort)
        resolvePromise(value)
      },
      (error: unknown) => {
        signal.removeEventListener('abort', onAbort)
        rejectPromise(error)
      },
    )
  })
}

function abortError(): Error {
  const error = new Error('aborted')
  error.name = 'AbortError'
  return error
}
