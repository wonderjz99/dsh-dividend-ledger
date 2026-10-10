/**
 * 客户端面板产物校验。
 *
 * 客户端代码只在浏览器里跑，tsc 管不到（手写的单文件 bundle），所以至少要把
 * **能被加载、能注册到正确槽位** 这两件事离线验掉——否则一次重启才发现
 * "面板根本没出现"，排查成本很高。
 */

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const CLIENT = resolve(REPO, 'plugin/client/client.js')

let captured = null
const styles = []
const errors = []

globalThis.window = { __ModuleLoader__: { load: (entry) => { captured = entry } } }
globalThis.document = {
  createElement: () => ({
    set textContent(value) { styles.push(value) },
    get textContent() { return '' },
  }),
  head: { appendChild: () => {} },
}

const react = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  Fragment: { fragment: true },
  useState: (initial) => [initial, () => {}],
  useEffect: () => {},
  useCallback: (fn) => fn,
  useRef: (initial) => ({ current: initial }),
  useMemo: (fn) => fn(),
}

// 用动态导入让 --check 之外的语义错误也能被抓到
await import(`file://${CLIENT}`)

if (captured === null) errors.push('没有调用 window.__ModuleLoader__.load')
if (captured !== null && captured.id !== 'dsh-stock-dividend') {
  errors.push(`注册 id 不对：${captured.id}`)
}

let mod = null
try {
  mod = captured.factory((name) => {
    if (name === 'react') return react
    throw new Error('未知模块：' + name)
  })
} catch (error) {
  errors.push('factory 执行失败：' + error.message)
}

if (mod !== null) {
  if (typeof mod.apply !== 'function') errors.push('缺少 apply 导出')
  if (!Array.isArray(mod.inject) || !mod.inject.includes('slots')) {
    errors.push('inject 必须包含 slots：' + JSON.stringify(mod.inject))
  }

  const seen = []
  try {
    mod.apply({
      slots: {
        inject: (name, callback) => { seen.push('inject:' + name); callback() },
        register: (target, render) => {
          seen.push('register:' + target.name)
          if (typeof render !== 'function') errors.push('register 的 render 不是函数')
          return () => {}
        },
      },
    })
  } catch (error) {
    errors.push('apply 执行失败：' + error.message)
  }

  if (!seen.includes('inject:shell.overlay')) errors.push('没有 inject 到 shell.overlay')
  if (!seen.includes('register:shell.overlay')) errors.push('没有 register 到 shell.overlay')
}

if (styles.length === 0 || styles[0].length < 500) {
  errors.push('样式没有注入或过短')
}

if (errors.length > 0) {
  console.error('客户端校验失败：')
  for (const message of errors) console.error('  - ' + message)
  process.exit(1)
}

console.log(
  `  client.js 校验通过：id=${captured.id} inject=${JSON.stringify(mod.inject)} ` +
    `样式 ${styles[0].length} 字节`,
)
