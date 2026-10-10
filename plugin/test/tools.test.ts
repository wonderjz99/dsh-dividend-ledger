/**
 * 工具定义契约测试。
 *
 * 这个文件存在的理由很具体：社区文档里流传的 `defineTool` 写法
 * （`parameters: { type: 'object', properties: {...} }`、`output` 不带 `render`）
 * 在 **0.2.0-rc.2 会直接抛错**。照抄文档会浪费一整轮调试，
 * 所以这里对**真实的 `@deepseek-ai/dsh-tools`** 做运行期校验：
 * 我的定义必须被接受，而错误写法必须被拒绝。
 *
 * 运行前提：先跑 `node ../scripts/extract-dsh-packages.mjs` 把宿主包抽到 `vendor/`。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { defineTool, JsonSchemaError, ToolRuntime } from '@deepseek-ai/dsh-tools'

import { buildTools } from '../src/tools.js'
import { resolveSandboxPolicy, withTempJsonFile } from '../src/tools.js'
import type { PluginContext, ToolDefinition } from '../src/dsh-types.js'

/**
 * 调用**真实的** `ToolRuntime.prototype.register` 做校验。
 *
 * `defineTool` 只编译 schema，不校验 `output` 的形状；那条校验在
 * `register()` 里（0.2.0-rc.2 的实现：`output` 必须是带 `render` 函数的对象）。
 * 直接调原型方法就能拿到真实行为，不必启动完整 Cordis 上下文：
 * 校验先跑，通过之后才会用到 `this.layers` 与 `this.ctx.effect`，
 * 所以给这两处塞最小替身即可跑完整条注册路径。
 */
function registerForReal(definition: unknown): () => void {
  const inserted: string[] = []
  const fake = {
    // 真的把 definition 交给回调，等于同时验证了"能被插进 layer"
    layers: {
      effect: (_ctx: unknown, callback: (layer: unknown) => unknown) => {
        callback({ tools: { insert: (name: string) => inserted.push(name) } })
        return () => {}
      },
    },
    ctx: { emit: () => {} },
  } as unknown as ToolRuntime
  const dispose = ToolRuntime.prototype.register.call(fake, definition as never)
  assert.equal(inserted.length, 1, '注册应把定义插入 layer 一次')
  return dispose
}

const ENV = {
  config: { timeoutMs: 30_000 },
  repoRoot: '/tmp/repo',
  pythonCommand: 'python3.11',
  dataDir: '/tmp/repo/data',
}

/** 一个最小的 ctx：只有 tools 注册表，够 buildTools 用。 */
function stubContext(): PluginContext & { registered: unknown[] } {
  const registered: unknown[] = []
  return {
    registered,
    tools: {
      register(definition: unknown) {
        registered.push(definition)
        return () => {}
      },
    },
  }
}

function build(): ToolDefinition[] {
  const ctx = stubContext()
  return buildTools({ ctx, env: ENV, defineTool }) as ToolDefinition[]
}

test('四个工具定义通过真实的 register 校验', () => {
  const tools = build()
  assert.equal(tools.length, 4)
  for (const tool of tools) {
    assert.equal(typeof tool.name, 'string')
    assert.ok(tool.name.length > 0, '工具必须有名字')
    assert.ok(tool.description.length > 20, `${tool.name} 的描述太短，模型看不懂`)
    assert.equal(typeof tool.execute, 'function', `${tool.name} 缺 execute`)
    assert.equal(typeof tool.output.render, 'function', `${tool.name} 缺 output.render`)
    // 真实 register 的完整路径：形状校验 + 落进 layer，且返回可用的注销器
    const dispose = registerForReal(tool)
    assert.equal(typeof dispose, 'function', `${tool.name} 的 register 应返回注销器`)
  }
})

test('四个工具名字符合预期且不重复', () => {
  const names = build().map((tool) => tool.name)
  assert.deepEqual(names, [
    'dividend_status',
    'dividend_holdings',
    'dividend_sync',
    'dividend_report',
  ])
  assert.equal(new Set(names).size, names.length, '工具名不能重复')
})

test('defineTool 把扁平 parameters 编译成标准 JSON Schema', () => {
  const tool = build().find((item) => item.name === 'dividend_report')
  assert.ok(tool !== undefined)
  const schema = tool.parameters as Record<string, unknown>
  assert.equal(schema.type, 'object')
  assert.ok(schema.properties !== undefined, '应编译出 properties')

  const events = build().find((item) => item.name === 'dividend_sync')
  assert.ok(events !== undefined)
  const syncSchema = events.parameters as {
    type: string
    properties: Record<string, Record<string, unknown>>
  }
  assert.equal(syncSchema.properties.source?.type, 'string')
  assert.deepEqual(syncSchema.properties.source?.enum, ['fixture', 'westock'])
})

test('社区文档里的错误写法在 0.2.0-rc.2 会被拒绝', () => {
  // 反例 1：parameters 写成 JSON Schema 包裹形式
  assert.throws(
    () =>
      defineTool({
        name: 'bad_params',
        description: '反例：parameters 不该带 type/properties 包裹',
        parameters: {
          type: 'object',
          properties: { foo: { type: 'string' } },
          required: ['foo'],
        },
        output: { schema: { type: 'string' }, render: () => [] },
        async execute() {
          return 'x'
        },
      }),
    (error: unknown) => error instanceof JsonSchemaError || error instanceof Error,
  )

  // 反例 2 的精确边界（实测得出，比社区文档更细）：
  // `defineTool` **总会**自己构造 `output.render` 键，所以"缺 render"不是
  // 通过 defineTool 能到达的状态；只有手写定义对象再交给 register 才会被拦。
  // 因此这里断言两件不同的事。
  const viaDefineTool = defineTool({
    name: 'no_render_via_define',
    description: '反例：即使不传 render，defineTool 也会补上这个键',
    parameters: { foo: { type: 'string' } },
    output: { schema: { type: 'string' } } as never,
    async execute() {
      return 'x'
    },
  })
  const shape = viaDefineTool.output as Record<string, unknown>
  assert.ok('render' in shape, 'defineTool 应自行构造 render 键')
  assert.equal(typeof shape.render, 'function')

  // 手写定义对象绕过 defineTool 时，register 才会拦下缺 render 的定义
  assert.throws(
    () =>
      registerForReal({
        name: 'bad_output',
        description: '反例：手写定义对象缺 render',
        parameters: {},
        output: { schema: { type: 'string' } },
        async execute() {
          return 'x'
        },
      }),
    /must declare output/,
  )
})

test('required 只能写 true（写 false 会抛错）', () => {
  assert.throws(() =>
    defineTool({
      name: 'bad_required',
      description: '反例：required 只接受 true',
      parameters: { foo: { type: 'string', required: false } },
      output: { schema: { type: 'json' }, render: () => [] },
      async execute() {
        return {}
      },
    }),
  )
})

test('timeoutMs 必须是正有限数', () => {
  assert.throws(() =>
    defineTool({
      name: 'bad_timeout',
      description: '反例：timeoutMs 不能是 0',
      parameters: {},
      output: { schema: { type: 'json' }, render: () => [] },
      timeoutMs: 0,
      async execute() {
        return {}
      },
    }),
  )
})

test('render 返回内容块数组', () => {
  const tool = build().find((item) => item.name === 'dividend_report')
  assert.ok(tool !== undefined)
  const blocks = tool.output.render({}, {
    ok: true,
    state: 'report',
    dataDir: '/tmp/data',
    python: 'python3.11',
    command: 'dividend-ledger report',
    warnings: [],
    data: {
      totals: { cash_amount: '26856.40', cost_basis: '482600.00' },
      meta: { date_basis: 'ex_date', date_basis_detail: '除权除息日 19 条' },
      report_path: '/tmp/data/out/report.json',
      ledger_path: '/tmp/data/out/ledger.json',
    },
    totals: {},
  })
  assert.ok(Array.isArray(blocks))
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0]?.type, 'text')
  assert.match(blocks[0]?.text ?? '', /26856\.40/)
})


test('render 在缺数据时显示 —— 而不是假装 0', () => {
  const tool = build().find((item) => item.name === 'dividend_report')
  assert.ok(tool !== undefined)
  const blocks = tool.output.render({}, {
    ok: true,
    state: 'report',
    dataDir: '/tmp/data',
    python: 'python3.11',
    command: 'dividend-ledger report',
    warnings: [],
    data: {
      totals: {
        cash_amount: '1000.00',
        cost_basis: null,
        yield_on_cost: null,
        dividend_yield: null,
        event_count: 1,
        stock_count: 1,
      },
      meta: {
        date_basis: 'ex_date',
        date_basis_detail: '除权除息日 1 条',
        missing_inputs: ['601988.SH.cost_per_share'],
      },
      report_path: 'r',
      ledger_path: 'l',
    },
    totals: {},
  })
  const text = blocks[0]?.text ?? ''
  assert.match(text, /成本收益率\s+—/)
  assert.match(text, /601988\.SH\.cost_per_share/)
})

test('全局参数必须写在子命令之前（否则 CLI 以退出码 2 拒绝）', async () => {
  // 真实踩过的坑：曾拼成 `report --data X`，工具直接不可用。
  const commands: string[] = []
  const toolContext = {
    ...stubContext(),
    shell: {
      resolve(request: Record<string, unknown>) {
        commands.push(String(request.command))
        return request
      },
      async execute() {
        return {
          async result() {
            return {
              exitCode: 0,
              stdout: { text: '__DIVIDEND_LEDGER_JSON_BEGIN__\n{"ok":true}\n__DIVIDEND_LEDGER_JSON_END__' },
              stderr: { text: '' },
            }
          },
        }
      },
    },
  }

  const tools = buildTools({ ctx: toolContext, env: ENV, defineTool }) as ToolDefinition[]
  const report = tools.find((item) => item.name === 'dividend_report')
  assert.ok(report !== undefined)
  await report.execute({ now: '2026-10-10T12:00:00+08:00' }, {})

  const command = commands[0] ?? ''
  const dataAt = command.indexOf('--data')
  const subcommandAt = command.indexOf('report')
  assert.ok(dataAt >= 0, `命令里应有 --data：${command}`)
  assert.ok(
    dataAt < subcommandAt,
    `--data 必须排在子命令之前：${command}`,
  )
  assert.match(command, /--now/)
})

test('每个工具的请求都带 sandboxPolicy 与 PYTHONPATH', async () => {
  const requests: Record<string, unknown>[] = []
  const policy = { resolve: () => ({ mode: 'workspace-write', workspaceRoot: '/w' }) }
  const toolContext = {
    ...stubContext(),
    // 只通过 get() 暴露：模拟 Cordis 里服务必须显式注入的行为
    get: (name: string) => (name === 'sandboxPolicy' ? policy : undefined),
    shell: {
      resolve(request: Record<string, unknown>) {
        requests.push(request)
        return request
      },
      async execute() {
        return {
          async result() {
            return {
              exitCode: 0,
              stdout: { text: '__DIVIDEND_LEDGER_JSON_BEGIN__\n{"ok":true}\n__DIVIDEND_LEDGER_JSON_END__' },
              stderr: { text: '' },
            }
          },
        }
      },
    },
  }

  const tools = buildTools({ ctx: toolContext, env: ENV, defineTool }) as ToolDefinition[]
  for (const tool of tools) {
    await tool.execute({}, {})
  }

  assert.equal(requests.length, tools.length)
  for (const request of requests) {
    assert.deepEqual(request.sandboxPolicy, { mode: 'workspace-write', workspaceRoot: '/w' })
    assert.deepEqual(request.dshEnv, { PYTHONPATH: '/tmp/repo' })
  }
})

test('临时 JSON 文件用完即删', async () => {
  let seen = ''
  await withTempJsonFile('[{"code":"600519.SH"}]', async (path) => {
    seen = path
    const { readFileSync, existsSync } = await import('node:fs')
    assert.ok(existsSync(path))
    assert.equal(readFileSync(path, 'utf8'), '[{"code":"600519.SH"}]')
  })
  const { existsSync } = await import('node:fs')
  assert.equal(existsSync(seen), false, '临时文件应被清理')
})

test('getService 在 Cordis 那种"未注入就抛"的上下文里也能取到服务', async () => {
  // 真实踩过的坑：Cordis 只有声明在 inject 里的服务才能用属性访问，
  // 裸读 ctx.sandboxPolicy 会抛 `cannot get property "sandboxPolicy" without inject`，
  // 于是插件一调用就直接失败。所以可选服务必须走 ctx.get()。
  const { getService } = await import('../src/python.js')
  const fake = {
    resolved: { mode: 'workspace-write' },
    get(name: string) {
      return name === 'sandboxPolicy' ? this.resolved : undefined
    },
  }
  Object.defineProperty(fake, 'sandboxPolicy', {
    get() {
      throw new Error('cannot get property "sandboxPolicy" without inject')
    },
  })

  assert.deepEqual(getService(fake as never, 'sandboxPolicy'), { mode: 'workspace-write' })
})

test('getService 在只有属性可用的上下文里也能取到服务', async () => {
  const { getService } = await import('../src/python.js')
  const fake = { shell: { resolve: () => ({}), execute: async () => ({}) } }
  assert.ok(getService(fake as never, 'shell') !== undefined)
})

test('getService 对不存在的服务返回 undefined 而不抛错', async () => {
  const { getService } = await import('../src/python.js')
  const throwing = {
    get() {
      throw new Error('nope')
    },
  }
  Object.defineProperty(throwing, 'missing', {
    get() {
      throw new Error('cannot get property "missing" without inject')
    },
  })
  assert.equal(getService(throwing as never, 'missing'), undefined)
})

test('拿不到沙箱策略时不抛错，只是返回 undefined', () => {
  const ctx = stubContext()
  assert.equal(resolveSandboxPolicy(ctx, {}), undefined)

  const throwing: PluginContext = {
    tools: { register: () => () => {} },
    sandboxPolicy: {
      resolve() {
        throw new Error('no session')
      },
    },
  }
  assert.equal(resolveSandboxPolicy(throwing, {}), undefined, '策略解析失败不应阻断调用')

  const ok: PluginContext = {
    tools: { register: () => () => {} },
    sandboxPolicy: { resolve: () => ({ mode: 'workspace-write', workspaceRoot: '/w' }) },
  }
  assert.deepEqual(resolveSandboxPolicy(ok, {}), {
    mode: 'workspace-write',
    workspaceRoot: '/w',
  })
})
