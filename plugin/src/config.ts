/**
 * 插件配置（schemastery schema）。
 *
 * 与 `cordis.patch.yml` 的 `insert[].config` 一一对应。注意 DSH 的 patch 会
 * **整体替换**目标行的 config（不做深合并），所以未来加字段时要在 patch 里写全。
 */

import z from '@deepseek-ai/schemastery'

/** 默认 Python 解释器候选，按顺序探测。核心只依赖标准库，3.11+ 即可。 */
export const DEFAULT_PYTHON_CANDIDATES: readonly string[] = [
  'python3.12',
  'python3.11',
  'python3',
]

/** 默认命令行超时：账本计算是纯内存运算，30 秒足够；可由调用方按次覆盖。 */
export const DEFAULT_TIMEOUT_MS = 30_000

/** 单次调用允许的最长超时，防止模型传入离谱的值。 */
export const MAX_TIMEOUT_MS = 120_000

/** 仓库根目录的标记文件：用来在开发态/安装态下都找到 dividend_ledger 包。 */
export const ROOT_MARKERS: readonly string[] = [
  'dividend_ledger',
  'SPEC.md',
]

export const Config = z.object({
  /**
   * Python 解释器。留空则按 {@link DEFAULT_PYTHON_CANDIDATES} 依次探测。
   * 可写绝对路径（推荐），也可写 PATH 上的命令名。
   */
  pythonPath: z.string().description('Python 解释器路径或命令名，留空自动探测'),

  /**
   * 数据目录。留空则用当前会话工作区下的 `data/`。
   * 会话工作区取自 `exec.agent.session.header.cwd`，这也是 DSH 里"当前项目"的定义。
   */
  dataDir: z.string().description('数据目录，留空用会话工作区下的 data/'),

  /** 仓库根目录（含 `dividend_ledger/` 包）。留空自动向上查找。 */
  root: z.string().description('仓库根目录，留空自动向上查找 dividend_ledger/'),

  /** 单次 Python 调用超时（毫秒）。 */
  timeoutMs: z.number().default(DEFAULT_TIMEOUT_MS).description('单次调用超时（毫秒）'),
})

export type PluginConfig = {
  pythonPath?: string
  dataDir?: string
  root?: string
  timeoutMs: number
}
