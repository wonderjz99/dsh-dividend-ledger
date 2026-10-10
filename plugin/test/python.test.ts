/**
 * Python 调用层的测试。
 *
 * 分两层：
 * 1. 纯函数层——命令拼装、哨兵提取、错误归类，用假 shell 断言请求内容。
 * 2. 集成层——用**真实执行命令**的假 shell 跑一个假 Python 可执行文件，
 *    验证从工具定义到子进程再到 JSON 解析的整条链路（离线，零网络）。
 */

import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { promisify } from 'node:util'

import type { PluginContext, ShellResult, ToolExec } from '../src/dsh-types.js'
import {
  buildCommand,
  extractSentinelPayload,
  firstMeaningfulLine,
  PythonInvocationError,
  runPythonJson,
  SENTINEL_BEGIN,
  SENTINEL_END,
  shellQuote,
  withSentinel,
} from '../src/python.js'

const execFileAsync = promisify(execFile)

const BASE_OPTIONS = {
  pythonCommand: 'python3.11',
  repoRoot: '/repo',
  args: ['validate', '--data', '/repo/data'],
  workdir: '/repo',
  timeoutMs: 30_000,
}

/** 记录请求、返回预设结果的假 shell。 */
function fakeShell(result: Partial<ShellResult> = {}) {
  const requests: Record<string, unknown>[] = []
  const ctx: PluginContext = {
    tools: { register: () => () => {} },
    shell: {
      resolve(request: Record<string, unknown>) {
        requests.push(request)
        return request
      },
      async execute() {
        return {
          async result(): Promise<ShellResult> {
            return {
              exitCode: 0,
              stdout: { text: `${SENTINEL_BEGIN}\n{"ok":true}\n${SENTINEL_END}\n` },
              stderr: { text: '' },
              ...result,
            }
          },
        }
      },
    },
  }
  return { ctx, requests }
}

test('命令拼装对每个参数做单引号转义', () => {
  const command = buildCommand({
    ...BASE_OPTIONS,
    args: ['report', '--data', '/tmp/my data', '--now', "2026-10-10T12:00:00+08:00"],
  })
  assert.equal(
    command,
    "'python3.11' '-m' 'dividend_ledger' 'report' '--data' '/tmp/my data' '--now' '2026-10-10T12:00:00+08:00'",
  )
})

test('shellQuote 正确处理内层单引号（命令注入防线）', () => {
  assert.equal(shellQuote("it's"), `'it'\\''s'`)
  assert.equal(shellQuote('$(rm -rf /)'), `'$(rm -rf /)'`)
  assert.equal(shellQuote('a;b'), `'a;b'`)
})

test('哨兵包裹保留退出码', () => {
  const wrapped = withSentinel("'python3' '-m' 'dividend_ledger'")
  assert.match(wrapped, /__dl_status=\$\?/)
  assert.match(wrapped, /exit \$__dl_status/)
  assert.ok(wrapped.includes(SENTINEL_BEGIN))
  assert.ok(wrapped.includes(SENTINEL_END))
})

test('哨兵提取能在噪声中定位 JSON', () => {
  const stdout = `banner line\n${SENTINEL_BEGIN}\n{"a":1}\n${SENTINEL_END}\ntrailing\n`
  assert.equal(extractSentinelPayload(stdout), '{"a":1}')
})

test('哨兵缺失或未闭合时返回 undefined', () => {
  assert.equal(extractSentinelPayload('no sentinel here'), undefined)
  assert.equal(extractSentinelPayload(`${SENTINEL_BEGIN}\n{"a":1}`), undefined)
})

test('请求带上 workdir、超时、PYTHONPATH 与 sandboxPolicy', async () => {
  const { ctx, requests } = fakeShell()
  await runPythonJson(ctx, {}, {
    ...BASE_OPTIONS,
    sandboxPolicy: { mode: 'workspace-write', workspaceRoot: '/repo' },
  })

  const request = requests[0]
  assert.ok(request !== undefined)
  assert.equal(request.workdir, '/repo')
  assert.equal(request.timeoutMs, 30_000)
  assert.deepEqual(request.dshEnv, { PYTHONPATH: '/repo' })
  assert.deepEqual(request.sandboxPolicy, { mode: 'workspace-write', workspaceRoot: '/repo' })
  assert.match(String(request.command), /dividend_ledger/)
})

test('没有 sandboxPolicy 时不塞该字段（避免触发执行器报错）', async () => {
  const { ctx, requests } = fakeShell()
  await runPythonJson(ctx, {}, BASE_OPTIONS)
  assert.equal('sandboxPolicy' in (requests[0] ?? {}), false)
})

test('非零退出把 stderr 首行当错误消息抛出', async () => {
  const { ctx } = fakeShell({
    exitCode: 1,
    stdout: { text: `${SENTINEL_BEGIN}\n${SENTINEL_END}\n` },
    stderr: { text: '错误：找不到数据文件：/repo/data/holdings.json\n' },
  })
  await assert.rejects(
    () => runPythonJson(ctx, {}, BASE_OPTIONS),
    (error: unknown) => {
      assert.ok(error instanceof PythonInvocationError)
      assert.equal(error.exitCode, 1)
      assert.match(error.message, /holdings\.json/)
      return true
    },
  )
})

test('超时有专门的错误消息', async () => {
  const { ctx } = fakeShell({ timedOut: true, exitCode: null })
  await assert.rejects(
    () => runPythonJson(ctx, {}, { ...BASE_OPTIONS, timeoutMs: 1_000 }),
    (error: unknown) => {
      assert.ok(error instanceof PythonInvocationError)
      assert.equal(error.timedOut, true)
      assert.match(error.message, /超时/)
      return true
    },
  )
})

test('输出被截断时拒绝解析（宁可报错也不要半个 JSON）', async () => {
  const { ctx } = fakeShell({ stdout: { text: '{"ok":', truncated: true } })
  await assert.rejects(
    () => runPythonJson(ctx, {}, BASE_OPTIONS),
    /截断/,
  )
})

test('哨兵内为空时报可读错误', async () => {
  const { ctx } = fakeShell({ stdout: { text: `${SENTINEL_BEGIN}\n${SENTINEL_END}\n` } })
  await assert.rejects(() => runPythonJson(ctx, {}, BASE_OPTIONS), /没有输出可解析的 JSON/)
})

test('哨兵内不是合法 JSON 时报可读错误', async () => {
  const { ctx } = fakeShell({ stdout: { text: `${SENTINEL_BEGIN}\nnot json\n${SENTINEL_END}` } })
  await assert.rejects(() => runPythonJson(ctx, {}, BASE_OPTIONS), /不是合法 JSON/)
})

test('宿主没有 shell 服务时明确报错', async () => {
  const ctx: PluginContext = { tools: { register: () => () => {} } }
  await assert.rejects(() => runPythonJson(ctx, {}, BASE_OPTIONS), /shell 服务/)
})

test('已取消的 signal 直接失败，不发命令', async () => {
  const { ctx, requests } = fakeShell()
  const controller = new AbortController()
  controller.abort()
  const exec: ToolExec = { signal: controller.signal }
  await assert.rejects(() => runPythonJson(ctx, exec, BASE_OPTIONS), /取消/)
  assert.equal(requests.length, 0, '取消后不应发起调用')
})

test('执行期间被取消会抛取消错误', async () => {
  const controller = new AbortController()
  const ctx: PluginContext = {
    tools: { register: () => () => {} },
    shell: {
      resolve: (request) => request,
      async execute() {
        return {
          result: () =>
            new Promise<ShellResult>((resolve) => {
              // 永不 resolve；只能靠 abort 结束
              setTimeout(() => resolve({ exitCode: 0 }), 5_000)
            }),
        }
      },
    },
  }
  setTimeout(() => controller.abort(), 10)
  await assert.rejects(
    () => runPythonJson(ctx, { signal: controller.signal }, BASE_OPTIONS),
    /取消/,
  )
})

test('firstMeaningfulLine 跳过 Traceback 与堆栈帧', () => {
  const text = 'Traceback (most recent call last):\n  at foo\n错误：真正的消息\n'
  assert.equal(firstMeaningfulLine(text), '错误：真正的消息')
  assert.equal(firstMeaningfulLine(''), '')
})

// --------------------------------------------------------------------------- #
// 集成层：真的起子进程
// --------------------------------------------------------------------------- #

interface FakeResponse {
  readonly stdout?: string
  /** 真 CLI 把错误与警告写 stderr，替身必须照做，否则测不出真实行为。 */
  readonly stderr?: string
  readonly exitCode: number
}

/** 造一个假 Python：忽略 `-m dividend_ledger`，按子命令回放预设输出。 */
function makeFakePython(dir: string, responses: Record<string, FakeResponse>): string {
  const path = join(dir, 'fake-python')
  const cases = Object.entries(responses).map(([name, response]) => {
    const parts = [`  ${name})`]
    if (response.stdout !== undefined && response.stdout !== '') {
      parts.push(`printf '%s\\n' ${JSON.stringify(response.stdout)};`)
    }
    if (response.stderr !== undefined && response.stderr !== '') {
      parts.push(`printf '%s\\n' ${JSON.stringify(response.stderr)} >&2;`)
    }
    parts.push(`exit ${response.exitCode} ;;`)
    return parts.join(' ')
  })

  const body = [
    '#!/bin/bash',
    '# 丢弃 -m dividend_ledger，只看子命令',
    'shift 2',
    'sub="$1"',
    'case "$sub" in',
    ...cases,
    '  *) echo "unknown subcommand: $sub" >&2; exit 64 ;;',
    'esac',
  ].join('\n')
  writeFileSync(path, `${body}\n`, 'utf8')
  chmodSync(path, 0o755)
  return path
}

/** 真正执行命令的 shell 替身：用 bash -c 跑请求里的 command 字符串。 */
function executingShell(): PluginContext {
  return {
    tools: { register: () => () => {} },
    shell: {
      resolve: (request) => request,
      async execute(spec: unknown) {
        const request = spec as { command: string; workdir: string }
        return {
          async result(): Promise<ShellResult> {
            try {
              const { stdout, stderr } = await execFileAsync('bash', ['-c', request.command], {
                cwd: request.workdir,
                maxBuffer: 8 * 1024 * 1024,
              })
              return { exitCode: 0, stdout: { text: stdout }, stderr: { text: stderr } }
            } catch (error) {
              const failure = error as {
                code?: number | string
                message?: string
                stdout?: string | Buffer
                stderr?: string | Buffer
              }
              const text = (value: string | Buffer | undefined): string =>
                value === undefined ? '' : value.toString()
              return {
                exitCode: typeof failure.code === 'number' ? failure.code : 1,
                stdout: { text: text(failure.stdout) },
                // 起进程本身失败时 stdout/stderr 都是空的，把 message 带上才看得出原因
                stderr: { text: text(failure.stderr) || (failure.message ?? '') },
              }
            }
          },
        }
      },
    },
  }
}

test('集成：命令真的跑起来，JSON 被正确解析', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-python-'))
  try {
    const python = makeFakePython(dir, {
      validate: { stdout: '{"ok":true,"holdings":{"count":6}}', exitCode: 0 },
    })
    const value = await runPythonJson<{ ok: boolean }>(executingShell(), {}, {
      pythonCommand: python,
      repoRoot: dir,
      args: ['validate', '--data', dir],
      workdir: dir,
      timeoutMs: 15_000,
    })
    assert.equal(value.ok, true)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('集成：真实子进程的非零退出被归类为 PythonInvocationError', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-python-'))
  try {
    const python = makeFakePython(dir, {
      // 真 CLI 的错误消息走 stderr——这正是被测行为
      report: { stderr: '错误：没有持仓', exitCode: 1 },
    })
    await assert.rejects(
      () =>
        runPythonJson(executingShell(), {}, {
          pythonCommand: python,
          repoRoot: dir,
          args: ['report', '--data', dir],
          workdir: dir,
          timeoutMs: 15_000,
        }),
      (error: unknown) => {
        assert.ok(error instanceof PythonInvocationError)
        assert.equal(error.exitCode, 1)
        assert.match(error.message, /没有持仓/)
        return true
      },
    )
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('集成：带空格与引号的路径也能正确传参', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-python-'))
  try {
    const weird = join(dir, "it's a dir")
    const { mkdirSync } = await import('node:fs')
    mkdirSync(weird, { recursive: true })
    const python = makeFakePython(dir, {
      validate: { stdout: '{"ok":true}', exitCode: 0 },
    })
    const value = await runPythonJson<{ ok: boolean }>(executingShell(), {}, {
      pythonCommand: python,
      repoRoot: dir,
      args: ['validate', '--data', weird],
      workdir: dir,
      timeoutMs: 15_000,
    })
    assert.equal(value.ok, true)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
