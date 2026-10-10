/**
 * 宿主包的模块声明。
 *
 * 为什么需要：DSH 的运行时包**不发布 `.d.ts`**（包内只有 `lib/*.js`），
 * 所以 TypeScript 无法从包本身推断类型。这里按官方实现补上最小可用的声明：
 *
 * - `@deepseek-ai/schemastery` 的 schema 构建器（`z.object` / `z.string` …）
 * - `@deepseek-ai/dsh-tools` 的 `defineTool`
 *
 * **这些声明不是契约的唯一来源，也不该被信任为完整。**
 * `defineTool` 的真实契约由 `test/tools.test.ts` 对真实包做运行期校验：
 * 声明写错了测试会失败，而不是悄悄放过。
 */

declare module '@deepseek-ai/schemastery' {
  /** schemastery 的 schema 实例（只声明本插件用到的方法）。 */
  export interface Schema<T = unknown> {
    required(): Schema<T>
    default(value: T): Schema<T>
    description(text: string): Schema<T>
    /** 暴露给宿主的 Standard Schema 接口。 */
    readonly '~standard'?: unknown
  }

  export interface SchemaFactory {
    string(): Schema<string>
    number(): Schema<number>
    boolean(): Schema<boolean>
    object<T extends Record<string, unknown>>(shape: {
      [K in keyof T]: Schema<T[K]>
    }): Schema<T>
    array<T>(item: Schema<T>): Schema<T[]>
    union<T extends readonly unknown[]>(values: T): Schema<T[number]>
    const<T>(value: T): Schema<T>
  }

  const z: SchemaFactory
  export default z
}

declare module '@deepseek-ai/dsh-tools' {
  import type { DefineTool, ToolDefinition } from './dsh-types.js'

  /** 把一个工具定义编译成宿主注册表能接受的形状。 */
  export const defineTool: DefineTool

  /**
   * 工具注册表服务（Cordis Service）。
   *
   * `register()` 才是做**形状校验**的地方：`output` 必须是带 `render` 函数的对象。
   * 这条校验不在 `defineTool` 里，所以"定义建得出来"不等于"注册得进去"。
   */
  export class ToolRuntime {
    register(definition: ToolDefinition): () => void
  }

  /** 参数/输出 schema 编译失败时抛出的错误。 */
  export class JsonSchemaError extends Error {}

  /** 模型生成的参数不合法时抛出的错误。 */
  export class ToolArgsError extends Error {
    readonly violations: readonly string[]
  }

  /** 工具返回值不符合 `output.schema` 时抛出的错误。 */
  export class ToolOutputError extends Error {}

  export const RUN_CODE_NAME: string
}
