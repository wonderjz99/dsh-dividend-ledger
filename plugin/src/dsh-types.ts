/**
 * 宿主接口的最小声明。
 *
 * 为什么自己声明而不 import：DSH 的运行时包**不发布 `.d.ts`**（包内只有 `lib/*.js`
 * 与 `lib/types/*.js`），而 `@deepseek-ai/cordis` 的类型也不在 exports 里。
 * 所以这里只声明本插件真正用到的那几个成员，形状取自官方包的实现与 README：
 *
 * - `ctx.tools.register(definition)` —— 来自 `@deepseek-ai/dsh-tools`
 * - `ctx.shell.resolve(request)` / `ctx.shell.execute(spec)` / `execution.result()`
 *   —— 来自 `@deepseek-ai/dsh-shell`
 * - `ctx.sandboxPolicy.resolve({ session })` —— 来自 `@deepseek-ai/dsh-sandbox-policy`
 *
 * 这些形状由 `plugin/test/tools.test.ts` 对**真实包**做运行期校验，
 * 一旦上游变了，测试会先失败，而不是等到线上才炸。
 */

/** 工具执行时宿主传入的上下文（只声明本插件用到的字段）。 */
export interface ToolExec {
  /** 调用方持有的取消信号；工具必须协作式响应（DSH 不做抢占式中断）。 */
  readonly signal?: AbortSignal
  /** 发起调用的 agent（子派发时可能为空）。 */
  readonly agent?: {
    readonly session?: {
      readonly header?: { readonly cwd?: string }
    }
  }
}

/** 渲染用的内容块。 */
export interface ContentBlock {
  readonly type: 'text'
  readonly text: string
}

/** 工具定义：与 `defineTool` 接受的形状一致。 */
export interface ToolDefinition {
  readonly name: string
  readonly description: string
  readonly parameters: Record<string, unknown>
  readonly output: {
    readonly schema: unknown
    readonly render: (args: Record<string, unknown>, value: unknown) => readonly ContentBlock[]
  }
  execute: (args: Record<string, unknown>, exec: ToolExec) => Promise<unknown>
  readonly timeoutMs?: number
}

/** `@deepseek-ai/dsh-tools` 的 `defineTool`。 */
export type DefineTool = (definition: ToolDefinition) => unknown

/** shell 执行结果（`@deepseek-ai/dsh-shell` 的 result 形状）。 */
export interface ShellResult {
  readonly exitCode?: number | null
  readonly signal?: string | null
  readonly timedOut?: boolean
  readonly aborted?: boolean
  readonly stdout?: { readonly text?: string; readonly truncated?: boolean; readonly spillPath?: string }
  readonly stderr?: { readonly text?: string; readonly truncated?: boolean; readonly spillPath?: string }
}

/** shell 执行句柄。 */
export interface ShellExecution {
  result(): Promise<ShellResult>
}

/** `ctx.shell` 服务。 */
export interface ShellService {
  resolve(request: Record<string, unknown>): unknown
  execute(spec: unknown): Promise<ShellExecution>
}

/** `ctx.sandboxPolicy` 服务（可选：某些组合里没有沙箱执行器）。 */
export interface SandboxPolicyService {
  resolve(options: { session?: unknown; mode?: string }): Record<string, unknown>
}

/** `ctx.tools` 服务。 */
export interface ToolsService {
  register(definition: unknown): () => void
}

/**
 * 插件 `apply` 收到的宿主上下文。
 *
 * **重要**：Cordis 里**只有声明在 `inject` 里的服务才能用属性访问**——
 * 读一个未注入的服务会直接抛 ``cannot get property "x" without inject``，
 * 而不是返回 undefined。所以可选服务必须走 :meth:`get`（不会抛），
 * 否则插件在缺少该服务的组合里根本加载不起来。
 */
export interface PluginContext {
  readonly tools: ToolsService
  readonly shell?: ShellService
  readonly sandboxPolicy?: SandboxPolicyService
  effect?(callback: () => unknown, label?: string): unknown
  /**
   * 声明依赖并等它就绪后执行回调。
   *
   * **这是访问可选服务的唯一正确姿势**：Cordis 里未注入的服务用属性或 `ctx.get()`
   * 访问都会抛异常，自己 try/catch 会退化成"静默不生效"。
   */
  inject?(names: readonly string[], callback: (ctx: PluginContext) => unknown): unknown
  /** 读取可选服务；未注册时返回 undefined 而不抛错。 */
  get?(name: string): unknown
}
