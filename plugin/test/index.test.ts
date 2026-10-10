/**
 * 入口与路径解析测试。
 *
 * 这里验证的是"插件装上去之后能不能找到东西"——最容易出错、也最难在
 * 装进 profile 之后排查的一环：仓库根、数据目录、Python 解释器。
 */

import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { test } from 'node:test'

import { apply, name, inject } from '../src/index.js'
import {
  findRepoRoot,
  lookupOnPath,
  resolveDataDir,
  resolvePythonCommand,
  resolveRepoRoot,
  sessionCwdOf,
} from '../src/paths.js'
import type { PluginContext } from '../src/dsh-types.js'

function stubContext(): PluginContext & { registered: { name?: string }[] } {
  const registered: { name?: string }[] = []
  return {
    registered,
    tools: {
      register(definition: unknown) {
        registered.push(definition as { name?: string })
        return () => {}
      },
    },
  }
}

const PLUGIN_ROOT = resolve(import.meta.dirname, '..', '..')

test('apply 注册四个工具，且不需要 shell 就已经能注册', () => {
  const ctx = stubContext()
  apply(ctx, undefined)
  assert.deepEqual(
    ctx.registered.map((item) => item.name),
    ['dividend_status', 'dividend_holdings', 'dividend_sync', 'dividend_report'],
  )
})

test('apply 能接受空配置与部分配置', () => {
  for (const config of [undefined, {}, { pythonPath: 'python3' }, { timeoutMs: 0 }]) {
    const ctx = stubContext()
    apply(ctx, config)
    assert.equal(ctx.registered.length, 4, JSON.stringify(config))
  }
})

test('apply 全程不抛错——即使 pythonPath 指向不存在的解释器', () => {
  // 解释器不存在时不应在启动期炸掉插件：报错要发生在调用时，才有可读上下文
  const ctx = stubContext()
  apply(ctx, { pythonPath: '/nonexistent/python' })
  assert.equal(ctx.registered.length, 4)
})

test('插件导出了 DSH 需要的 name / inject / Config', async () => {
  const mod = await import('../src/index.js')
  assert.equal(name, 'dsh-stock-dividend')
  assert.deepEqual(inject, ['tools'])
  assert.ok(mod.Config !== undefined, '必须导出 Config 供宿主校验配置')
})

test('findRepoRoot 从嵌套目录向上找到含 dividend_ledger 的根', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-root-'))
  try {
    mkdirSync(join(dir, 'dividend_ledger'), { recursive: true })
    mkdirSync(join(dir, 'plugin', 'src'), { recursive: true })
    assert.equal(findRepoRoot(join(dir, 'plugin', 'src')), dir)
    assert.equal(findRepoRoot(join(dir, 'plugin')), dir)
    assert.equal(findRepoRoot(dir), dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('findRepoRoot 找不到时返回 undefined 而不是乱猜', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-empty-'))
  try {
    assert.equal(findRepoRoot(dir, 2), undefined)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('resolveRepoRoot 优先使用显式配置，空白配置回退到自动查找', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-cfg-'))
  try {
    mkdirSync(join(dir, 'dividend_ledger'), { recursive: true })
    mkdirSync(join(dir, 'plugin', 'src'), { recursive: true })

    // 显式配置直接生效
    assert.equal(resolveRepoRoot('/somewhere/else', import.meta.url), resolve('/somewhere/else'))

    // 空白配置 → 从给定模块位置向上查找
    assert.equal(
      resolveRepoRoot('  ', join(dir, 'plugin', 'src', 'index.ts')),
      resolve(dir),
    )
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('在本仓库里真的能找到 dividend_ledger（端到端自检）', () => {
  const root = resolveRepoRoot(undefined, import.meta.url)
  assert.equal(
    findRepoRoot(root),
    root,
    `自动查找应命中仓库根，实际得到 ${root}`,
  )
})

test('resolveDataDir 的优先级：配置 > 会话工作区 > 仓库根', () => {
  assert.equal(
    resolveDataDir({ repoRoot: '/repo' }),
    '/repo/data',
    '默认落在仓库根下',
  )
  assert.equal(
    resolveDataDir({ repoRoot: '/repo', sessionCwd: '/work' }),
    '/work/data',
    '有会话工作区时用它',
  )
  assert.equal(
    resolveDataDir({ repoRoot: '/repo', sessionCwd: '/work', configured: '/abs/data' }),
    '/abs/data',
    '绝对配置优先',
  )
  assert.equal(
    resolveDataDir({ repoRoot: '/repo', sessionCwd: '/work', configured: 'mydata' }),
    '/work/mydata',
    '相对配置按会话工作区解析',
  )
  assert.equal(
    resolveDataDir({ repoRoot: '/repo', configured: '   ' }),
    '/repo/data',
    '空白配置视为未配置',
  )
})

test('sessionCwdOf 只认非空字符串的 cwd', () => {
  assert.equal(sessionCwdOf({}), undefined)
  assert.equal(sessionCwdOf({ agent: {} }), undefined)
  assert.equal(sessionCwdOf({ agent: { session: { header: {} } } }), undefined)
  assert.equal(sessionCwdOf({ agent: { session: { header: { cwd: '  ' } } } }), undefined)
  assert.equal(
    sessionCwdOf({ agent: { session: { header: { cwd: '/work/proj' } } } }),
    '/work/proj',
  )
})

test('resolvePythonCommand 尊重显式配置，否则探测候选', () => {
  assert.equal(resolvePythonCommand('/custom/python'), '/custom/python')
  assert.equal(resolvePythonCommand('  python3.12  '), 'python3.12')
  // 不传配置时必须回一个非空命令名，绝不能是空字符串
  const detected = resolvePythonCommand(undefined)
  assert.ok(detected.length > 0)
})

test('lookupOnPath 能在给定 PATH 里找到可执行文件', () => {
  const dir = mkdtempSync(join(tmpdir(), 'dl-path-'))
  try {
    const fake = join(dir, 'python3.11')
    mkdirSync(join(dir), { recursive: true })
    writeFileSync(fake, '#!/bin/sh\n', 'utf8')
    assert.equal(lookupOnPath('python3.11', dir), fake)
    assert.equal(lookupOnPath('python3.11', '/nonexistent'), undefined)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('插件包声明了 dsh.bundle.patch 与 DSH peer 依赖', async () => {
  const manifest = JSON.parse(
    readFileSync(join(PLUGIN_ROOT, 'package.json'), 'utf8'),
  ) as {
    dsh?: { bundle?: { patch?: string } }
    peerDependencies?: Record<string, string>
  }
  assert.equal(
    manifest.dsh?.bundle?.patch,
    './cordis.patch.yml',
    '没有 dsh.bundle.patch 就只会被当成普通依赖装上、永远不会加载',
  )
  assert.ok(
    manifest.peerDependencies?.['@deepseek-ai/dsh-tools'] !== undefined,
    '必须声明 DSH peer，否则外部链接安装时解析不到运行时包',
  )
})
