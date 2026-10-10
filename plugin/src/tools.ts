/**
 * 四个 DSH 工具的声明与实现。
 *
 * **薄适配层的铁律**：这里不出现任何业务规则。
 * 乘股数、按到账日归集、缺数据置 null、去重——全部在 Python 核心。
 * 本文件只做四件事：拼参数、调 Python、把结果转成模型能读的文本、把错误变成可读消息。
 *
 * 契约要点（已对 0.2.0-rc.2 的真实实现核对）：
 * - `parameters` 是**扁平属性表**，字段上写 `required: true`，不是 `type/properties/required`。
 * - `output` 必须是 `{ schema, render }`，缺 `render` 时 `defineTool` 会直接抛错。
 * - `render` 返回 `ContentBlock[]`，即 `[{ type: 'text', text }]`。
 *
 * 一个实现细节：`holdings --set` 需要从 stdin 读 JSON，但 `ctx.shell` 没有 stdin 通道，
 * 所以这里把 JSON 落成临时文件再传路径。临时文件用完即删，不进数据目录。
 */

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import type {
  ContentBlock,
  DefineTool,
  PluginContext,
  SandboxPolicyService,
  ToolExec,
} from './dsh-types.js'
import { MAX_TIMEOUT_MS } from './config.js'
import type { PluginConfig } from './config.js'
import { sessionCwdOf } from './paths.js'
import { getService, PythonInvocationError, runPythonJson } from './python.js'

/** 解析完成的运行环境。 */
export interface ToolRuntimeEnv {
  readonly config: PluginConfig
  readonly repoRoot: string
  readonly pythonCommand: string
  readonly dataDir: string
}

export interface ToolDeps {
  readonly ctx: PluginContext
  readonly env: ToolRuntimeEnv
  /** 由调用方注入的 `defineTool`，避免本模块在导入期就解析宿主包。 */
  readonly defineTool: DefineTool
}

interface CommonPayload {
  readonly ok: boolean
  readonly dataDir: string
  readonly python: string
  readonly command: string
  readonly warnings: readonly string[]
}

interface DataPayload extends CommonPayload {
  readonly state: 'status' | 'holdings' | 'sync'
  readonly data: unknown
}

interface ReportPayload extends CommonPayload {
  readonly state: 'report'
  readonly data: unknown
  readonly totals: unknown
}

interface ErrorPayload extends CommonPayload {
  readonly state: 'error'
  readonly message: string
  readonly exitCode: number | null
}

type ToolPayload = DataPayload | ReportPayload | ErrorPayload

const DATA_DIR_PROPERTY = {
  data_dir: {
    type: 'string',
    description: '数据目录；留空用当前会话工作区下的 data/',
  },
} as const

/** `type: 'json'` 在 defineTool 的 DSL 里编译成"任意 JSON"，是官方支持的写法。 */
const JSON_SCHEMA: unknown = { type: 'json' }

export function buildTools(deps: ToolDeps): readonly unknown[] {
  return [
    deps.defineTool(defineStatus(deps)),
    deps.defineTool(defineHoldings(deps)),
    deps.defineTool(defineSync(deps)),
    deps.defineTool(defineReport(deps)),
  ]
}

// --------------------------------------------------------------------------- #
// 工具 1：看状态
// --------------------------------------------------------------------------- #

function defineStatus(deps: ToolDeps) {
  return {
    name: 'dividend_status',
    description:
      '查看 A 股分红账本的数据状态：持仓条数、已同步的分红事件条数、是否存在到账日覆盖表，'
      + '以及数据能否解析。用来确认账本是否就绪，或排查"报告为什么出不来"。只读，不改文件。',
    parameters: { ...DATA_DIR_PROPERTY },
    output: {
      schema: JSON_SCHEMA,
      render: (_args: Record<string, unknown>, value: unknown): readonly ContentBlock[] => [
        { type: 'text', text: renderStatus(value as ToolPayload) },
      ],
    },
    async execute(args: Record<string, unknown>, exec: ToolExec): Promise<unknown> {
      return runTool(deps, exec, 'status', args, ['validate'])
    },
  }
}

// --------------------------------------------------------------------------- #
// 工具 2：看 / 写持仓
// --------------------------------------------------------------------------- #

function defineHoldings(deps: ToolDeps) {
  return {
    name: 'dividend_holdings',
    description:
      '查看或写入持仓。action=list 列出当前持仓；action=set 用一段 JSON 覆盖写入持仓。'
      + '每条持仓需要 code（如 600519.SH 或 sh600519）、start_date（YYYY-MM-DD，开始持有日）、'
      + 'shares（股数）；cost_per_share（每股成本）可选，缺了该股成本收益率会显示为空。'
      + '注意：一阶段假设持有期内股数恒定，不支持加仓/减仓。',
    parameters: {
      ...DATA_DIR_PROPERTY,
      action: {
        type: 'string',
        enum: ['list', 'set'],
        description: 'list=查看（默认），set=写入',
      },
      holdings_json: {
        type: 'string',
        description:
          'action=set 时的持仓 JSON 数组，例如 '
          + '[{"code":"600519.SH","start_date":"2021-03-15","shares":"100","cost_per_share":"1950"}]',
      },
    },
    output: {
      schema: JSON_SCHEMA,
      render: (_args: Record<string, unknown>, value: unknown): readonly ContentBlock[] => [
        { type: 'text', text: renderHoldings(value as ToolPayload) },
      ],
    },
    async execute(args: Record<string, unknown>, exec: ToolExec): Promise<unknown> {
      const action = typeof args.action === 'string' ? args.action : 'list'
      if (action !== 'set') {
        return runTool(deps, exec, 'holdings', args, ['holdings'])
      }

      const payload = args.holdings_json
      if (typeof payload !== 'string' || payload.trim() === '') {
        return errorPayload(deps, 'action=set 需要提供 holdings_json（持仓 JSON 数组）')
      }
      return withTempJsonFile(payload, async (path) =>
        runTool(deps, exec, 'holdings', args, ['holdings', '--set', path]),
      )
    },
  }
}

// --------------------------------------------------------------------------- #
// 工具 3：同步分红事件
// --------------------------------------------------------------------------- #

function defineSync(deps: ToolDeps) {
  return {
    name: 'dividend_sync',
    description:
      '把分红事件同步进账本。source=fixture 读取本地事件文件（完全离线）；'
      + 'source=westock 通过 westock CLI 联网抓取真实分红数据（A 股主力数据源），'
      + '也可用 payload_file 指定已落盘的响应做离线解析。'
      + '联网抓取会自动限速与重试；同一份输入重复同步是幂等的，不产生重复记录。',
    parameters: {
      ...DATA_DIR_PROPERTY,
      source: {
        type: 'string',
        enum: ['fixture', 'westock'],
        description: '数据来源。默认 fixture（离线）',
      },
      events_file: {
        type: 'string',
        description: 'source=fixture 时的事件文件路径，默认 <data>/events.fixture.json',
      },
      payload_file: {
        type: 'string',
        description: 'source=westock 时改从该文件离线解析（不给则联网调用 westock CLI）',
      },
    },
    output: {
      schema: JSON_SCHEMA,
      render: (_args: Record<string, unknown>, value: unknown): readonly ContentBlock[] => [
        { type: 'text', text: renderSync(value as ToolPayload) },
      ],
    },
    async execute(args: Record<string, unknown>, exec: ToolExec): Promise<unknown> {
      const cliArgs = ['sync']
      if (typeof args.source === 'string' && args.source !== '') {
        cliArgs.push('--from', args.source)
      }
      if (typeof args.events_file === 'string' && args.events_file !== '') {
        cliArgs.push('--events-file', args.events_file)
      }
      if (typeof args.payload_file === 'string' && args.payload_file !== '') {
        cliArgs.push('--payload-file', args.payload_file)
      }
      return runTool(deps, exec, 'sync', args, cliArgs)
    },
  }
}

// --------------------------------------------------------------------------- #
// 工具 4：出报告
// --------------------------------------------------------------------------- #

function defineReport(deps: ToolDeps) {
  return {
    name: 'dividend_report',
    description:
      '生成分红账：按到账日（A 股无到账日时按除权除息日）汇总年度/月度/股票三个维度，'
      + '给出税前分红合计、成本收益率与股息率。结果写入 out/report.json 与 out/ledger.json，'
      + '本工具只回关键数字与文件路径。主口径为税前，红利税未计入；'
      + '缺每股成本或除权前收盘价时对应指标为 null，不会用 0 冒充。',
    parameters: {
      ...DATA_DIR_PROPERTY,
      now: {
        type: 'string',
        description: '固定报告时间戳（ISO 8601）。给了之后输出可逐字节复现，便于比对',
      },
      overrides: {
        type: 'string',
        description: '到账日人工覆盖表路径，默认 <data>/pay_dates.json（存在才生效）',
      },
    },
    output: {
      schema: JSON_SCHEMA,
      render: (_args: Record<string, unknown>, value: unknown): readonly ContentBlock[] => [
        { type: 'text', text: renderReport(value as ToolPayload) },
      ],
    },
    async execute(args: Record<string, unknown>, exec: ToolExec): Promise<unknown> {
      const cliArgs = ['report']
      if (typeof args.now === 'string' && args.now !== '') cliArgs.push('--now', args.now)
      if (typeof args.overrides === 'string' && args.overrides !== '') {
        cliArgs.push('--overrides', args.overrides)
      }
      return runTool(deps, exec, 'report', args, cliArgs)
    },
    // 注意：参数必须原样传给 Python CLI。TS 层不实现任何业务逻辑，
    // 也不"聪明地"忽略参数——忽略会让模型以为设了 now 却没生效。
  }
}

// --------------------------------------------------------------------------- #
// 公共执行逻辑
// --------------------------------------------------------------------------- #

async function runTool(
  deps: ToolDeps,
  exec: ToolExec,
  state: 'status' | 'holdings' | 'sync' | 'report',
  args: Record<string, unknown>,
  cliArgs: readonly string[],
): Promise<ToolPayload> {
  const { env, ctx } = deps
  const dataDir =
    typeof args.data_dir === 'string' && args.data_dir !== '' ? args.data_dir : env.dataDir

  // 全局参数必须写在**子命令之前**（argparse 的常规约定，也是本 CLI 的实际行为）。
  // 曾经拼成 `report --data X` → CLI 以退出码 2 拒绝，工具直接不可用。
  const fullArgs = ['--data', dataDir, ...cliArgs]
  const command = `dividend-ledger ${fullArgs.join(' ')}`
  const timeoutMs = Math.min(env.config.timeoutMs, MAX_TIMEOUT_MS)

  try {
    const data = await runPythonJson<Record<string, unknown>>(ctx, exec, {
      pythonCommand: env.pythonCommand,
      repoRoot: env.repoRoot,
      args: fullArgs,
      workdir: sessionCwdOf(exec) ?? env.repoRoot,
      timeoutMs,
      sandboxPolicy: resolveSandboxPolicy(ctx, exec),
    })

    if (state === 'report') {
      return {
        ok: true,
        state,
        dataDir,
        python: env.pythonCommand,
        command,
        warnings: [],
        data,
        totals: data.totals,
      }
    }
    return { ok: true, state, dataDir, python: env.pythonCommand, command, warnings: [], data }
  } catch (error) {
    if (error instanceof PythonInvocationError) {
      return errorPayload(
        deps,
        error.message,
        error.exitCode ?? null,
        dataDir,
        command,
      )
    }
    return errorPayload(
      deps,
      error instanceof Error ? error.message : String(error),
      null,
      dataDir,
      command,
    )
  }
}

function errorPayload(
  deps: ToolDeps,
  message: string,
  exitCode: number | null = null,
  dataDir = deps.env.dataDir,
  command = '',
): ErrorPayload {
  return {
    ok: false,
    state: 'error',
    dataDir,
    python: deps.env.pythonCommand,
    command,
    warnings: [],
    message,
    exitCode,
  }
}

/**
 * 解析沙箱策略。
 *
 * **必须做**：只传 `workdir` 不会触发 confinement，执行器要靠这个字段
 * 才知道 workspace 的边界与执行模式。
 */
export function resolveSandboxPolicy(
  ctx: PluginContext,
  exec: ToolExec,
): Record<string, unknown> | undefined {
  const service = getService<SandboxPolicyService>(ctx, 'sandboxPolicy')
  if (service === undefined || typeof service.resolve !== 'function') return undefined
  try {
    const session = exec.agent?.session
    return service.resolve(session === undefined ? {} : { session })
  } catch {
    // 拿不到策略时不阻断调用：账本是只读运算，宁可少一层 confinement 也不要不可用
    return undefined
  }
}

/** 把一段文本落成临时 JSON 文件，回调结束后删除。 */
export async function withTempJsonFile<T>(
  content: string,
  callback: (path: string) => Promise<T>,
): Promise<T> {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-dividend-'))
  const path = join(dir, 'holdings.json')
  try {
    writeFileSync(path, content, 'utf8')
    return await callback(path)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

// --------------------------------------------------------------------------- #
// 渲染：只回关键数字，不回大 JSON
// --------------------------------------------------------------------------- #

export function renderStatus(payload: ToolPayload): string {
  if (payload.state === 'error') return renderError(payload)
  const data = payload.data as Record<string, unknown> | undefined
  const holdings = data?.holdings as Record<string, unknown> | undefined
  const events = data?.events as Record<string, unknown> | undefined
  const payDates = data?.pay_dates as Record<string, unknown> | undefined
  const problems = (data?.problems as readonly string[] | undefined) ?? []

  const lines = [
    `账本状态：${data?.ok === true ? '就绪' : '有阻塞项'}`,
    `  数据目录    ${payload.dataDir}`,
    `  持仓        ${String(holdings?.count ?? 0)} 条`,
    `  分红事件    ${String(events?.count ?? 0)} 条`,
    `  到账日覆盖  ${payDates?.exists === true ? '存在' : '不存在（可选）'}`,
    `  解释器      ${payload.python}`,
  ]
  if (problems.length > 0) {
    lines.push('', '阻塞项：')
    for (const problem of problems) lines.push(`  · ${problem}`)
  }
  return lines.join('\n')
}

export function renderHoldings(payload: ToolPayload): string {
  if (payload.state === 'error') return renderError(payload)
  const data = payload.data as Record<string, unknown> | undefined
  const list = (data?.holdings as readonly Record<string, unknown>[] | undefined) ?? []
  if (list.length === 0) {
    return `持仓为空。用 dividend_holdings 的 action=set 写入持仓。\n数据目录：${payload.dataDir}`
  }

  const lines = [`持仓 ${list.length} 只：`, '', '  代码          名称        开始持有     股数      每股成本']
  const missingCost: string[] = []
  for (const item of list) {
    const cost = item.cost_per_share
    if (cost === undefined || cost === null) missingCost.push(String(item.code))
    lines.push(
      '  ' +
        String(item.code ?? '').padEnd(13) +
        String(item.name ?? '—').padEnd(11) +
        String(item.start_date ?? '').padEnd(12) +
        String(item.shares ?? '').padStart(8) +
        String(cost ?? '—').padStart(12),
    )
  }
  if (missingCost.length > 0) {
    lines.push('', `缺每股成本：${missingCost.join('、')} → 这些股票的成本收益率会是 null`)
  }
  lines.push('', `数据目录：${payload.dataDir}`)
  return lines.join('\n')
}

export function renderSync(payload: ToolPayload): string {
  if (payload.state === 'error') return renderError(payload)
  const data = payload.data as Record<string, unknown> | undefined
  const audit = data?.import as Record<string, unknown> | undefined
  const provider = data?.provider as Record<string, unknown> | undefined
  const rejected = (data?.provider_rejected as readonly Record<string, unknown>[] | undefined) ?? []

  const lines = [
    `分红事件同步完成（来源：${String(data?.source ?? 'fixture')}）`,
    `  文件内条数  ${String(audit?.items_in_file ?? 0)}`,
    `  新增        ${String(audit?.imported ?? 0)}`,
    `  覆盖        ${String(audit?.replaced ?? 0)}`,
    `  未变化      ${String(audit?.unchanged ?? 0)}`,
    `  拒绝        ${String(audit?.rejected_count ?? 0)}`,
  ]
  if (provider?.matched_events !== undefined) {
    lines.push(`  来源匹配    ${String(provider.matched_events)} 条`)
  }
  if (rejected.length > 0) {
    lines.push('', `来源侧跳过 ${rejected.length} 行（未实施/取消/无派现等），前几行：`)
    for (const item of rejected.slice(0, 5)) lines.push(`  · ${String(item.reason ?? '')}`)
  }
  lines.push('', `事件文件：${String(data?.events_path ?? '')}`)
  return lines.join('\n')
}

export function renderReport(payload: ToolPayload): string {
  if (payload.state === 'error') return renderError(payload)
  const data = payload.data as Record<string, unknown> | undefined
  const totals = (data?.totals as Record<string, unknown> | undefined) ?? {}
  const meta = (data?.meta as Record<string, unknown> | undefined) ?? {}

  const lines = [
    `分红账（${String(meta.date_basis_detail ?? '')}）`,
    '',
    `  税前分红合计  ¥ ${String(totals.cash_amount ?? '0.00')}`,
    `  持有成本      ¥ ${String(totals.cost_basis ?? '—')}`,
    `  成本收益率    ${percent(totals.yield_on_cost)}`,
    `  股息率        ${percent(totals.dividend_yield)}`,
    `  分红事件      ${String(totals.event_count ?? 0)} 笔 / ${String(totals.stock_count ?? 0)} 只`,
  ]

  const missing = (meta.missing_inputs as readonly string[] | undefined) ?? []
  if (missing.length > 0) lines.push('', `  缺数据：${missing.join('、')}`)

  for (const warning of (meta.warnings as readonly string[] | undefined) ?? []) {
    lines.push(`  警告：${warning}`)
  }

  lines.push(
    '',
    '  口径：主口径税前；红利税未计入；' +
      (meta.date_basis === 'ex_date'
        ? 'A 股无派息到账日，按除权除息日归集'
        : '归集日期使用到账日'),
    '',
    `  报告：${String(data?.report_path ?? '')}`,
    `  明细：${String(data?.ledger_path ?? '')}`,
  )
  return lines.join('\n')
}

function isErrorPayload(payload: ToolPayload): payload is ErrorPayload {
  return payload.state === 'error'
}

function renderError(payload: ToolPayload): string {
  if (!isErrorPayload(payload)) return ''
  const suffix = payload.exitCode === null ? '' : `（退出码 ${payload.exitCode}）`
  return `分红账本调用失败${suffix}：${payload.message}`
}

function percent(value: unknown): string {
  if (value === null || value === undefined) return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return `${(parsed * 100).toFixed(2)}%`
}
