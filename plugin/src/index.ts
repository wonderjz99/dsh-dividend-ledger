/**
 * 插件入口：注册 A 股分红账本的四个工具。
 *
 * 架构说明（SPEC §7.1）：**Python 核心 + 薄 TS 适配层**。
 * 本文件与 `tools.ts` 不含任何业务算法，只负责：
 *
 * 1. 校验并补全配置（宿主已按 `Config` schema 处理过，这里只做防御性兜底）；
 * 2. 解析仓库根与 Python 解释器；
 * 3. 把四个工具注册进 `ctx.tools`。
 *
 * 注册即生效——DSH 会把工具 schema 自动并入系统提示词，不需要额外声明。
 */

import { defineTool } from '@deepseek-ai/dsh-tools'

import { Config, DEFAULT_TIMEOUT_MS } from './config.js'
import type { PluginConfig } from './config.js'
import type { PluginContext } from './dsh-types.js'
import { resolveDataDir, resolvePythonCommand, resolveRepoRoot } from './paths.js'
import { mountRoutes } from './routes.js'
import { buildTools } from './tools.js'
import type { ToolDeps, ToolRuntimeEnv } from './tools.js'

export const name = 'dsh-stock-dividend'

/** 依赖的服务：没有 `tools` 就无处注册工具。 */
export const inject = ['tools']

export { Config }

export function apply(ctx: PluginContext, config?: Partial<PluginConfig>): void {
  const resolved: PluginConfig = {
    pythonPath: config?.pythonPath,
    dataDir: config?.dataDir,
    root: config?.root,
    timeoutMs: normaliseTimeout(config?.timeoutMs),
  }

  const repoRoot = resolveRepoRoot(resolved.root, import.meta.url)
  const env: ToolRuntimeEnv = {
    config: resolved,
    repoRoot,
    pythonCommand: resolvePythonCommand(resolved.pythonPath),
    dataDir: resolveDataDir({ configured: resolved.dataDir, repoRoot }),
  }

  const deps: ToolDeps = { ctx, env, defineTool }
  for (const definition of buildTools(deps)) {
    ctx.tools.register(definition)
  }

  // 浏览器面板的数据通道。
  //
  // 必须走 ctx.inject 回调：Cordis 里**未注入的服务访问会抛异常**（属性与 ctx.get 都一样），
  // 自己 try/catch 只会得到"静默不注册 + 无日志"这种最难查的故障。
  // inject 回调还顺带等 webServer 就绪（它的加载时机可能晚于本插件）。
  // 不把 webServer 放进模块级 `inject` 数组，是为了在没有 web 服务的组合里
  // 仍然保留对话内的工具能力。
  if (typeof ctx.inject === 'function') {
    // 只注入 webServer。路由的执行通道是 ctx.subprocess（不受 shell 沙箱约束），
    // 所以不需要 sandboxPolicy——写持仓被拒的根因就是 shell 沙箱，不是策略取值。
    ctx.inject(['webServer', 'subprocess'], (scoped) => {
      const disposer = mountRoutes({ ctx: scoped ?? ctx, env })
      if (typeof scoped?.effect === 'function') {
        scoped.effect(() => disposer, 'dividend-ledger: http routes')
      } else if (typeof ctx.effect === 'function') {
        ctx.effect(() => disposer, 'dividend-ledger: http routes')
      }
      return undefined
    })
  } else {
    const disposer = mountRoutes({ ctx, env })
    if (typeof ctx.effect === 'function') {
      ctx.effect(() => disposer, 'dividend-ledger: http routes')
    }
  }
}

/** 防御性兜底：配置来自 patch 文件，可能被写成 0 或负数。 */
function normaliseTimeout(value: number | undefined): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    return DEFAULT_TIMEOUT_MS
  }
  return value
}
