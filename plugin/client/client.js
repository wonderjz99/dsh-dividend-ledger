/**
 * dsh-stock-dividend — 浏览器端面板
 *
 * 形态与 dsh-stock-watch 一致：`window.__ModuleLoader__.load({id, factory})`，
 * factory 里 `require("react")`，`inject = ["slots"]`，在 `shell.overlay` 槽位注册。
 * 数据来自宿主注册的 `/dsh-dividend/*` 路由（同源 fetch）。
 *
 * ── 交互 ──────────────────────────────────────────────────────────
 *  查股入账：搜索 → 命中行直接「入 账」→ 填持股数量 → 立刻看到该股两个股息率
 *  持仓：逐股两个股息率 + **整个持仓的加权总动态/预期股息率**
 *
 * ── 设计方向：账簿（账房） ──────────────────────────────────────────
 * 不是"金融仪表盘"。这是一册账：宣纸暖底、墨色分层正文、朱砂只用在
 * 「入 账」这一个动作上（全屏唯一实心红，像盖章）。
 * 表格用细分隔线 + 等宽 tabular-nums 右对齐——小数点必须对齐，这是账本的底线。
 * 字体：LXGW WenKai Screen（霞鹜文楷屏幕版）作展示字，Noto Sans SC 作正文。
 */
window.__ModuleLoader__.load({
  id: 'dsh-stock-dividend',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const react = require('react')
    const { useState, useEffect, useCallback, useRef } = react
    const h = react.createElement

    const BASE = '/dsh-dividend'

    // ------------------------------------------------------------------ 样式
    const styleTag = document.createElement('style')
    styleTag.textContent = `
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;700&display=swap');

.dl-root{
  --paper:#f4efe4; --paper-2:#ece5d6; --paper-3:#e3dac7;
  --ink:#171310; --ink-2:#4e463c; --ink-3:#8b8071; --ink-4:#b3a894;
  --seal:#b3271e; --seal-soft:rgba(179,39,30,.10); --seal-line:rgba(179,39,30,.34);
  --gold:#9a7b39; --jade:#2f6b52;
  --rule:rgba(23,19,16,.13); --rule-soft:rgba(23,19,16,.07);
  --shadow:0 18px 48px rgba(38,28,16,.22), 0 2px 6px rgba(38,28,16,.10);
  --display:'LXGW WenKai Screen','LXGW WenKai','Kaiti SC',STKaiti,KaiTi,'Noto Serif SC',serif;
  --body:'Noto Sans SC',-apple-system,'PingFang SC',sans-serif;
  --num:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;
}

/* ── 立牌（折叠态）──────────────────────────────────────────────── */
.dl-tab{
  position:fixed;top:64px;right:0;z-index:9998;
  display:flex;flex-direction:column;align-items:center;gap:5px;
  padding:11px 7px 13px;border:none;cursor:pointer;
  background:linear-gradient(180deg,#1c1713,#12100d);
  color:#f0e7d6;border-radius:11px 0 0 11px;
  box-shadow:-6px 8px 26px rgba(20,14,8,.34);
  font-family:var(--display);font-size:13px;letter-spacing:.18em;
  writing-mode:vertical-rl;text-orientation:upright;
  transition:transform .28s cubic-bezier(.34,1.42,.64,1), box-shadow .28s ease;
}
.dl-tab:hover{transform:translateX(-5px);box-shadow:-10px 8px 32px rgba(20,14,8,.42)}
.dl-tab:active{transform:translateX(-2px)}
.dl-tab:focus-visible{outline:2px solid var(--seal);outline-offset:3px}
.dl-tab-mark{
  writing-mode:horizontal-tb;font-family:var(--num);font-size:9px;letter-spacing:.04em;
  color:#c8bda6;background:rgba(255,255,255,.07);border-radius:3px;padding:1px 3px;
}
.dl-tab-seal{
  writing-mode:horizontal-tb;width:17px;height:17px;border-radius:2px;
  background:var(--seal);color:#fff;font-family:var(--display);
  font-size:11px;line-height:17px;text-align:center;
}

/* ── 面板 ───────────────────────────────────────────────────────── */
.dl-panel{
  position:fixed;top:22px;right:22px;z-index:9999;
  width:468px;max-height:min(86vh,820px);
  display:flex;flex-direction:column;overflow:hidden;
  background:var(--paper);color:var(--ink);
  border:1px solid var(--rule);border-radius:3px;
  box-shadow:var(--shadow);
  font-family:var(--body);font-size:12.5px;line-height:1.55;
  animation:dl-in .34s cubic-bezier(.22,1,.36,1);
}
@keyframes dl-in{from{opacity:0;transform:translateY(-8px) scale(.985)}to{opacity:1;transform:none}}
.dl-panel::before{
  content:'';position:absolute;inset:0;pointer-events:none;z-index:0;
  background-image:
    repeating-linear-gradient(58deg, rgba(120,100,70,.030) 0 1px, transparent 1px 5px),
    repeating-linear-gradient(-58deg, rgba(120,100,70,.022) 0 1px, transparent 1px 7px);
}
.dl-panel::after{
  content:'';position:absolute;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(120% 90% at 88% -10%, rgba(154,123,57,.10), transparent 58%);
}
.dl-panel>*{position:relative;z-index:1}

/* ── 头部 ───────────────────────────────────────────────────────── */
.dl-head{display:flex;align-items:center;gap:10px;padding:13px 15px 11px;border-bottom:2px solid var(--ink)}
.dl-title{font-family:var(--display);font-size:19px;font-weight:700;letter-spacing:.14em;margin:0;flex:1}
.dl-title-sub{
  display:block;font-family:var(--body);font-size:10px;font-weight:400;
  letter-spacing:.06em;color:var(--ink-3);margin-top:2px;
}
.dl-iconbtn{
  flex:none;width:26px;height:26px;display:grid;place-items:center;
  border:1px solid var(--rule);border-radius:2px;background:transparent;
  color:var(--ink-2);cursor:pointer;font-size:13px;line-height:1;
  transition:background .16s ease,color .16s ease,border-color .16s ease;
}
.dl-iconbtn:hover:not(:disabled){background:var(--paper-3);color:var(--ink);border-color:var(--ink-4)}
.dl-iconbtn:disabled{opacity:.5;cursor:default}
.dl-iconbtn:focus-visible{outline:2px solid var(--seal);outline-offset:2px}

/* ── 页签 ───────────────────────────────────────────────────────── */
.dl-tabs{display:flex;border-bottom:1px solid var(--rule)}
.dl-tabbtn{
  flex:1;padding:8px 10px;border:none;background:transparent;cursor:pointer;
  font-family:var(--display);font-size:13px;letter-spacing:.16em;color:var(--ink-3);
  border-bottom:2px solid transparent;margin-bottom:-1px;
  transition:color .16s ease,border-color .16s ease,background .16s ease;
}
.dl-tabbtn:hover{color:var(--ink-2);background:rgba(154,123,57,.06)}
.dl-tabbtn-on{color:var(--ink);border-bottom-color:var(--seal)}
.dl-tabbtn:focus-visible{outline:2px solid var(--seal);outline-offset:-3px}

/* ── 搜索 ───────────────────────────────────────────────────────── */
.dl-search{padding:12px 15px 10px;border-bottom:1px solid var(--rule-soft)}
.dl-search-row{display:flex;align-items:baseline;gap:8px}
.dl-search-label{font-family:var(--display);font-size:12px;letter-spacing:.2em;color:var(--ink-2);flex:none}
.dl-input{
  flex:1;border:none;border-bottom:1.5px solid var(--ink-4);background:transparent;
  font-family:var(--body);font-size:14.5px;color:var(--ink);
  padding:3px 2px 5px;outline:none;transition:border-color .2s ease;
}
.dl-input::placeholder{color:var(--ink-4);font-size:12.5px}
.dl-input:focus{border-bottom-color:var(--seal)}
.dl-search-meta{
  margin-top:6px;font-size:10.5px;color:var(--ink-3);
  display:flex;justify-content:space-between;gap:8px;align-items:center;
}
.dl-spin{
  display:inline-block;width:9px;height:9px;border-radius:50%;
  border:1.5px solid var(--seal-line);border-top-color:var(--seal);
  animation:dl-rot .7s linear infinite;vertical-align:-1px;
}
@keyframes dl-rot{to{transform:rotate(360deg)}}

/* ── 列表 ───────────────────────────────────────────────────────── */
.dl-body{flex:1;overflow-y:auto;overscroll-behavior:contain;min-height:120px}
.dl-body::-webkit-scrollbar{width:9px}
.dl-body::-webkit-scrollbar-track{background:var(--paper-2)}
.dl-body::-webkit-scrollbar-thumb{background:var(--ink-4);border:3px solid var(--paper-2);border-radius:9px}

.dl-cols{
  display:grid;gap:8px;padding:7px 15px 6px;border-bottom:1px solid var(--ink);
  font-family:var(--display);font-size:10.5px;letter-spacing:.12em;color:var(--ink-3);
}
.dl-cols-r{text-align:right}
.dl-cols-search{grid-template-columns:1fr 74px 86px}
.dl-cols-held{grid-template-columns:1fr 62px 72px 88px}

.dl-row{
  display:grid;gap:8px;align-items:baseline;box-sizing:border-box;
  width:100%;padding:9px 15px;border-bottom:1px solid var(--rule-soft);
  background:transparent;text-align:left;color:inherit;font:inherit;
  animation:dl-row-in .3s ease both;
}
@keyframes dl-row-in{from{opacity:0;transform:translateX(-4px)}to{opacity:1;transform:none}}
.dl-row-search{grid-template-columns:1fr 74px 86px}
.dl-row-held{grid-template-columns:1fr 62px 72px 88px}

.dl-code{font-family:var(--num);font-size:10.5px;color:var(--ink-3)}
.dl-name{font-family:var(--display);font-size:15px;font-weight:700;letter-spacing:.03em}
.dl-sub{font-size:10px;color:var(--ink-4);display:block;margin-top:1px}
.dl-price{font-family:var(--num);font-size:12.5px;text-align:right;color:var(--ink-2)}
.dl-shares{font-family:var(--num);font-size:12.5px;text-align:right;color:var(--ink-2)}

.dl-yield{text-align:right;font-family:var(--num);font-size:15.5px;font-weight:700;letter-spacing:-.01em}
.dl-yield-hi{color:var(--seal)}
.dl-yield-lo{color:var(--ink-2)}
.dl-yield-null{color:var(--ink-4);font-size:12px;font-weight:400}
.dl-yield-exp{display:block;font-size:11px;font-weight:700;color:var(--jade);margin-top:1px}
.dl-yield-cap{display:block;font-size:8.5px;letter-spacing:.13em;color:var(--ink-4);margin-top:1px;font-weight:400}

/* ── 行内行动区 ─────────────────────────────────────────────────── */
.dl-actions{
  grid-column:1 / -1;display:flex;align-items:center;gap:8px;
  margin-top:5px;padding-top:6px;border-top:1px dashed var(--rule-soft);
}
.dl-row-err{font-size:10.5px;color:var(--seal);line-height:1.45;flex:1}
.dl-ghost-btn{
  border:none;background:transparent;cursor:pointer;color:var(--ink-3);
  font-family:var(--body);font-size:11.5px;padding:4px 7px;border-radius:2px;
  transition:color .16s ease,background .16s ease;
}
.dl-ghost-btn:hover{color:var(--ink);background:rgba(23,19,16,.06)}
.dl-ghost-btn:focus-visible{outline:2px solid var(--seal);outline-offset:2px}

/* 持股数：整块可点，暗示"点这里改" */
.dl-shares-btn{
  border:none;background:transparent;cursor:pointer;font:inherit;
  font-family:var(--num);font-size:12.5px;color:var(--ink-2);
  padding:2px 4px;border-radius:2px;
  border-bottom:1px dashed var(--ink-4);
  transition:color .15s ease,border-color .15s ease,background .15s ease;
}
.dl-shares-btn:hover{color:var(--seal);border-bottom-color:var(--seal);background:var(--seal-soft)}
.dl-shares-btn:focus-visible{outline:2px solid var(--seal);outline-offset:2px}
.dl-shares-on{color:var(--seal);border-bottom-color:var(--seal);background:var(--seal-soft)}

/* 删除：默认低调，hover 才露出朱砂色——避免误点的视觉诱导 */
.dl-del-btn{
  margin-left:auto;border:none;background:transparent;cursor:pointer;
  font-family:var(--display);font-size:11px;letter-spacing:.12em;
  color:var(--ink-3);padding:4px 8px;border-radius:2px;
  transition:color .15s ease,background .15s ease;
}
.dl-del-btn:hover{color:var(--seal);background:var(--seal-soft)}
.dl-del-btn:focus-visible{outline:2px solid var(--seal);outline-offset:2px}
.dl-del-btn:disabled{opacity:.4;cursor:not-allowed}

/* 朱砂印：全屏唯一的实心红，代表"入账" */
.dl-seal-btn{
  border:none;cursor:pointer;border-radius:2px;margin-left:auto;
  background:var(--seal);color:#fff;
  font-family:var(--display);font-size:12.5px;letter-spacing:.2em;
  padding:6px 15px 6px 17px;
  box-shadow:0 2px 0 rgba(120,22,16,.6), 0 3px 10px rgba(179,39,30,.24);
  transition:transform .12s ease, box-shadow .12s ease, filter .12s ease;
}
.dl-seal-btn:hover:not(:disabled){filter:brightness(1.07)}
.dl-seal-btn:active:not(:disabled){transform:translateY(1.5px);box-shadow:0 1px 0 rgba(120,22,16,.6), 0 2px 6px rgba(179,39,30,.2)}
.dl-seal-btn:disabled{opacity:.42;cursor:not-allowed;box-shadow:none;transform:none}
.dl-seal-btn-sm{font-size:11.5px;padding:5px 12px 5px 14px}
.dl-hint-inline{font-family:var(--display);font-size:11px;color:var(--ink-4)}

/* ── 表单 ───────────────────────────────────────────────────────── */
.dl-form{
  padding:11px 15px 14px;border-bottom:1px solid var(--rule);
  background:linear-gradient(180deg, rgba(154,123,57,.07), transparent 70%), var(--paper-2);
  animation:dl-form-in .24s ease both;
}
@keyframes dl-form-in{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.dl-form-head{
  display:flex;align-items:baseline;gap:8px;margin-bottom:9px;
  font-family:var(--display);font-size:12.5px;letter-spacing:.08em;color:var(--ink-2);
}
.dl-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px 12px}
.dl-field{display:flex;flex-direction:column;gap:2px}
.dl-field-label{font-family:var(--display);font-size:10.5px;letter-spacing:.13em;color:var(--ink-3)}
.dl-field input{
  border:none;border-bottom:1px solid var(--ink-4);background:transparent;
  font-family:var(--num);font-size:14px;color:var(--ink);padding:2px 1px 4px;outline:none;
  transition:border-color .18s ease;
}
.dl-field input:focus{border-bottom-color:var(--seal)}
.dl-field-full{grid-column:1 / -1}
/* 表单里的实时股息率：填完股数最想看的东西 */
.dl-live{
  grid-column:1 / -1;display:flex;gap:20px;align-items:flex-end;
  margin-top:2px;padding:8px 11px;border-left:2.5px solid var(--seal);
  background:var(--seal-soft);
}
.dl-live-item{display:flex;flex-direction:column;gap:0}
.dl-live-k{font-family:var(--display);font-size:10px;letter-spacing:.12em;color:var(--ink-3)}
.dl-live-v{font-family:var(--num);font-size:17px;font-weight:700;letter-spacing:-.01em}
.dl-live-v em{font-style:normal;font-size:10px;font-weight:400;color:var(--ink-3);margin-left:4px}
.dl-live-dyn{color:var(--seal)}
.dl-live-exp{color:var(--jade)}
.dl-live-null{color:var(--ink-4);font-size:12px;font-weight:400}

/* ── 待收（已公告未实施）─────────────────────────────────────────
   视觉上必须与"已收"明确分开：金棕描边 + 虚线框，不用朱砂（朱砂是"入账"专用） */
.dl-upcoming{
  border-top:1px solid var(--rule);
  background:linear-gradient(180deg, rgba(154,123,57,.09), transparent 60%), var(--paper-2);
}
.dl-up-head{
  display:flex;align-items:baseline;gap:8px;padding:9px 15px 7px;
  font-family:var(--display);font-size:11px;letter-spacing:.14em;color:var(--gold);
}
.dl-up-head b{font-weight:700}
.dl-up-head-sum{margin-left:auto;font-family:var(--num);font-size:11px;letter-spacing:0;color:var(--ink-2)}
.dl-up-rows{padding:0 15px 9px}
.dl-up-row{
  display:grid;grid-template-columns:64px 1fr auto;gap:8px;align-items:baseline;
  padding:4px 0;border-bottom:1px dashed var(--rule-soft);
}
.dl-up-month{font-family:var(--num);font-size:10.5px;color:var(--gold);letter-spacing:.02em}
.dl-up-main{min-width:0}
.dl-up-name{font-family:var(--display);font-size:12.5px;font-weight:700;letter-spacing:.02em}
.dl-up-detail{display:block;font-size:9.5px;color:var(--ink-4);font-family:var(--num);margin-top:1px}
.dl-up-cash{font-family:var(--num);font-size:12.5px;font-weight:700;text-align:right;white-space:nowrap}
.dl-up-certain{color:var(--ink)}
.dl-up-doubt{color:var(--gold)}
.dl-up-tag{
  display:inline-block;font-size:8.5px;letter-spacing:.1em;font-family:var(--display);
  padding:0 3px;border-radius:2px;margin-left:5px;vertical-align:1px;
}
.dl-up-tag-certain{background:rgba(47,107,82,.14);color:var(--jade)}
.dl-up-tag-doubt{background:rgba(154,123,57,.18);color:var(--gold)}
.dl-up-note{padding:0 15px 9px;font-size:9.5px;color:var(--ink-4);line-height:1.6}
.dl-up-empty{padding:9px 15px 11px;font-size:10.5px;color:var(--ink-4);font-family:var(--display);letter-spacing:.06em}

/* ── 合计 ───────────────────────────────────────────────────────── */
.dl-total{border-top:2px solid var(--ink);padding:11px 15px 12px;background:var(--paper-3)}
.dl-total-title{
  font-family:var(--display);font-size:10.5px;letter-spacing:.2em;color:var(--ink-3);
  margin-bottom:7px;display:flex;justify-content:space-between;gap:8px;
}
.dl-total-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.dl-total-item{display:flex;flex-direction:column;gap:0;min-width:0}
.dl-total-k{font-family:var(--display);font-size:10px;letter-spacing:.11em;color:var(--ink-3)}
.dl-total-v{font-family:var(--num);font-size:16px;font-weight:700;letter-spacing:-.01em;white-space:nowrap}
.dl-total-v em{font-style:normal;font-size:9.5px;font-weight:400;color:var(--ink-3);margin-left:2px}
.dl-total-v-dyn{color:var(--seal)}
.dl-total-v-exp{color:var(--jade)}
.dl-total-v-null{color:var(--ink-4);font-size:12px;font-weight:400}
.dl-total-note{font-size:10px;color:var(--ink-3);margin-top:6px;line-height:1.6}

.dl-note{
  padding:7px 15px 9px;border-top:1px solid var(--rule-soft);
  font-size:10px;color:var(--ink-3);background:var(--paper-2);
  display:flex;flex-wrap:wrap;gap:4px 12px;
}
.dl-note b{font-weight:500;color:var(--ink-2)}

/* ── 空态 / 提示 ────────────────────────────────────────────────── */
.dl-empty{
  padding:34px 22px;text-align:center;color:var(--ink-3);
  font-family:var(--display);font-size:13px;letter-spacing:.06em;line-height:1.9;
}
.dl-empty-mark{display:block;font-size:30px;color:var(--ink-4);margin-bottom:8px}
.dl-empty small{display:block;font-family:var(--body);font-size:11px;letter-spacing:0;margin-top:5px;color:var(--ink-4)}
.dl-toast{
  position:absolute;left:50%;bottom:14px;transform:translateX(-50%);
  background:var(--ink);color:var(--paper);border-radius:2px;
  padding:6px 14px;font-size:11.5px;letter-spacing:.05em;white-space:nowrap;
  box-shadow:0 6px 20px rgba(20,14,8,.34);animation:dl-toast-in .22s ease both;z-index:5;
}
@keyframes dl-toast-in{from{opacity:0;transform:translate(-50%,8px)}to{opacity:1;transform:translate(-50%,0)}}

@media (prefers-reduced-motion: reduce){
  .dl-panel,.dl-row,.dl-form,.dl-toast{animation:none}
  .dl-tab{transition:none}
}
@media (max-width: 760px){
  .dl-panel{right:8px;left:8px;width:auto;top:8px;max-height:90vh}
  .dl-tab{top:56px}
}
`
    document.head.appendChild(styleTag)

    // ------------------------------------------------------------------ 工具函数
    function pct(value) {
      if (value === null || value === undefined || value === '') return null
      const num = Number(value)
      if (!Number.isFinite(num)) return null
      return (num * 100).toFixed(2) + '%'
    }

    function money(value) {
      if (value === null || value === undefined || value === '') return null
      const parsed = Number(value)
      if (!Number.isFinite(parsed)) return null
      return parsed.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    }

    /** 股数千分位：账本里存的是十进制字符串，显示时去掉无意义的小数尾巴。 */
    function shareCount(value) {
      if (value === null || value === undefined || value === '') return null
      const parsed = Number(value)
      if (!Number.isFinite(parsed)) return null
      return parsed.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
    }

    function price(value) {
      if (value === null || value === undefined || value === '') return null
      const parsed = Number(value)
      if (!Number.isFinite(parsed)) return null
      return parsed.toFixed(2)
    }

    async function getJson(path) {
      let res
      try {
        res = await fetch(BASE + path, { cache: 'no-store' })
      } catch (networkError) {
        throw new Error(
          '连不上宿主路由（' +
            (networkError && networkError.message ? networkError.message : '网络错误') +
            '）',
        )
      }
      // 路由没注册时 SPA 兜底回 404 且响应体是 HTML——这种错最难猜，单独说清
      if (res.status === 404) {
        throw new Error('宿主路由未注册（HTTP 404）。改过插件代码后需要完全退出并重开应用。')
      }
      const payload = await res
        .json()
        .catch(() => ({ ok: false, error: 'HTTP ' + res.status + '，响应不是 JSON' }))
      if (!res.ok || payload.ok === false) throw new Error(payload.error || 'HTTP ' + res.status)
      return payload
    }

    async function postJson(path, body) {
      const res = await fetch(BASE + path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body,
        cache: 'no-store',
      })
      const payload = await res.json().catch(() => ({ ok: false, error: '响应不是 JSON' }))
      if (!res.ok || payload.ok === false) throw new Error(payload.error || 'HTTP ' + res.status)
      return payload
    }

    function todayIso() {
      const now = new Date()
      const pad = (value) => String(value).padStart(2, '0')
      return now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate())
    }

    function yieldClass(value) {
      if (pct(value) === null) return 'dl-yield dl-yield-null'
      return Number(value) >= 0.03 ? 'dl-yield dl-yield-hi' : 'dl-yield dl-yield-lo'
    }

    // ------------------------------------------------------------------ 两个股息率（复用的小组件）
    function YieldCell(props) {
      const { row, showCap } = props
      const dynamicYield = pct(row.dynamicYield)
      const expectedYield = pct(row.expectedYield)
      return h(
        'div',
        { className: yieldClass(row.dynamicYield) },
        dynamicYield === null ? '—' : dynamicYield,
        showCap ? h('span', { className: 'dl-yield-cap' }, '动态') : null,
        expectedYield === null ? null : h('span', { className: 'dl-yield-exp' }, '预期 ' + expectedYield),
      )
    }

    // ------------------------------------------------------------------ 搜索命中行
    function SearchRow(props) {
      const { row, detail, busy, open, onToggle } = props
      const merged = detail && detail.row ? Object.assign({}, row, detail.row) : row

      return h(
        'div',
        { className: 'dl-row dl-row-search' },
        h(
          'div',
          null,
          h('span', { className: 'dl-code' }, merged.code),
          h('span', { className: 'dl-name', style: { marginLeft: '7px' } }, merged.name),
          h(
            'span',
            { className: 'dl-sub' },
            busy
              ? '正在取该股数据 …'
              : merged.ttmEventCount
                ? '近一年 ' + merged.ttmEventCount + ' 笔派息'
                : '本地无分红数据，展开后自动抓取',
          ),
        ),
        h(
          'div',
          { className: 'dl-price' },
          price(merged.price) === null ? '—' : price(merged.price),
          h('span', { className: 'dl-yield-cap' }, '收盘价'),
        ),
        h(YieldCell, { row: merged, showCap: true }),
        h(
          'div',
          { className: 'dl-actions' },
          h(
            'span',
            { className: 'dl-hint-inline' },
            open ? '' : '加入持仓 →',
          ),
          h(
            'button',
            {
              className: 'dl-seal-btn dl-seal-btn-sm',
              onClick: () => onToggle(merged),
              type: 'button',
              disabled: busy,
            },
            open ? '收 起' : '入 账',
          ),
        ),
      )
    }

    // ------------------------------------------------------------------ 入账表单
    function LedgerForm(props) {
      const { row, held, onDone, onCancel, toast } = props
      const [shareInput, setShareInput] = useState(held ? shareCount(held.shares) ?? '' : '')
      const [cost, setCost] = useState(held ? held.cost_per_share ?? '' : '')
      const [startDate, setStartDate] = useState(held ? held.start_date : todayIso())
      const [busy, setBusy] = useState(false)
      const [msg, setMsg] = useState('')

      const dynamicYield = pct(row.dynamicYield)
      const expectedYield = pct(row.expectedYield)
      const parsedShares = Number(shareInput)
      const validShares = Number.isFinite(parsedShares) && parsedShares > 0

      const submit = useCallback(async () => {
        setMsg('')
        if (!validShares) {
          setMsg('股数要填一个正数')
          return
        }
        if (!/^\d{4}-\d{2}-\d{2}$/.test(startDate)) {
          setMsg('日期要 YYYY-MM-DD')
          return
        }
        setBusy(true)
        try {
          // 读当前持仓 → 合并这一只 → 整体写回（--set 是覆盖语义）
          const current = await getJson('/holdings')
          const list = (current.holdings ?? []).filter((item) => item.code !== row.code)
          const entry = { code: row.code, start_date: startDate, shares: String(parsedShares) }
          if (row.name) entry.name = row.name
          if (String(cost).trim()) entry.cost_per_share = String(cost).trim()
          list.push(entry)
          await postJson('/holdings/set', JSON.stringify(list))
          toast((held ? '已改：' : '已入账：') + row.name + ' ' + parsedShares + ' 股')
          onDone()
        } catch (error) {
          setMsg(error && error.message ? error.message : '写入失败')
        } finally {
          setBusy(false)
        }
      }, [row.code, row.name, parsedShares, validShares, cost, startDate, held, onDone, toast])

      return h(
        'div',
        { className: 'dl-form' },
        h(
          'div',
          { className: 'dl-form-head' },
          h('span', null, held ? '改这一笔' : '记入持仓'),
          h(
            'span',
            { style: { fontFamily: 'var(--num)', fontSize: '11px', color: 'var(--ink-3)' } },
            row.code + ' ' + row.name,
          ),
        ),
        h(
          'div',
          { className: 'dl-form-grid' },
          h(
            'label',
            { className: 'dl-field' },
            h('span', { className: 'dl-field-label' }, '持股数量'),
            h('input', {
              value: shareInput,
              onChange: (event) => setShareInput(event.target.value),
              placeholder: '100',
              inputMode: 'decimal',
              autoFocus: true,
            }),
          ),
          h(
            'label',
            { className: 'dl-field' },
            h('span', { className: 'dl-field-label' }, '开始持有'),
            h('input', {
              value: startDate,
              onChange: (event) => setStartDate(event.target.value),
              placeholder: 'YYYY-MM-DD',
            }),
          ),
          h(
            'label',
            { className: 'dl-field dl-field-full' },
            h('span', { className: 'dl-field-label' }, '每股成本 · 选填（不填就算不出成本收益率）'),
            h('input', {
              value: cost,
              onChange: (event) => setCost(event.target.value),
              placeholder: '如 1500',
              inputMode: 'decimal',
            }),
          ),
          // 实时股息率：填完股数立刻能看到，不必先保存
          h(
            'div',
            { className: 'dl-live' },
            h(
              'div',
              { className: 'dl-live-item' },
              h('span', { className: 'dl-live-k' }, '动态股息率'),
              h(
                'span',
                {
                  className:
                    'dl-live-v ' + (dynamicYield === null ? 'dl-live-null' : 'dl-live-dyn'),
                },
                dynamicYield === null ? '—' : dynamicYield,
                dynamicYield === null ? null : h('em', null, '近 12 个月已派息 ÷ 现价'),
              ),
            ),
            h(
              'div',
              { className: 'dl-live-item' },
              h('span', { className: 'dl-live-k' }, '预期股息率'),
              h(
                'span',
                {
                  className:
                    'dl-live-v ' + (expectedYield === null ? 'dl-live-null' : 'dl-live-exp'),
                },
                expectedYield === null ? '—' : expectedYield,
                expectedYield === null ? null : h('em', null, '已公告未实施 ÷ 现价'),
              ),
            ),
          ),
        ),
        h(
          'div',
          { className: 'dl-actions', style: { borderTop: 'none', marginTop: '10px' } },
          h(
            'button',
            { className: 'dl-seal-btn', onClick: submit, disabled: busy, type: 'button' },
            busy ? '记 …' : held ? '改 入 账' : '入 账',
          ),
          h('button', { className: 'dl-ghost-btn', onClick: onCancel, type: 'button' }, '算了'),
          msg ? h('span', { className: 'dl-row-err' }, msg) : null,
        ),
      )
    }

    // ------------------------------------------------------------------ 持仓行
    function HeldRow(props) {
      const { row, open, onToggle, onDelete } = props
      return h(
        'div',
        { className: 'dl-row dl-row-held' },
        h(
          'div',
          null,
          h('span', { className: 'dl-code' }, row.code),
          h('span', { className: 'dl-name', style: { marginLeft: '7px' } }, row.name),
          h(
            'span',
            { className: 'dl-sub' },
            '累计已收 ¥' + (money(row.receivedTotal) ?? '0.00') +
              (row.yieldOnCost ? ' · 成本收益率 ' + pct(row.yieldOnCost) : ''),
          ),
        ),
        // 持股数直接可点：用户明确不要"改这一笔"这种中间按钮
        h(
          'button',
          {
            className: 'dl-shares dl-shares-btn' + (open ? ' dl-shares-on' : ''),
            onClick: () => onToggle(row),
            type: 'button',
            title: '点击修改持股数',
          },
          shareCount(row.shares) === null ? '—' : shareCount(row.shares),
          h('span', { className: 'dl-yield-cap' }, '股'),
        ),
        h(
          'div',
          { className: 'dl-price' },
          price(row.price) === null ? '—' : price(row.price),
          h('span', { className: 'dl-yield-cap' }, '收盘价'),
        ),
        h(YieldCell, { row, showCap: true }),
        h(
          'div',
          { className: 'dl-actions' },
          row.yieldOnCost
            ? null
            : h('span', { className: 'dl-row-err' }, '缺每股成本 → 成本收益率为空'),
          h(
            'button',
            {
              className: 'dl-del-btn',
              onClick: () => onDelete(row),
              type: 'button',
              title: '从持仓里删掉这只',
              'aria-label': '删除 ' + row.name,
            },
            open ? '收 起' : '删 除',
          ),
        ),
      )
    }

    // ------------------------------------------------------------------ 待收区块
    /**
     * 待收分红（已公告未实施）。
     *
     * **刻意与"已收"分开展示**：顶部合计条说的是"我已经收了多少"，
     * 这里是"还没到账的"。两者混在一起，"已收"就失真了——这正是阶段三
     * 要防的事，所以视觉上也划清界限（金棕描边，不用朱砂）。
     */
    function UpcomingBlock(props) {
      const { upcoming } = props
      if (!upcoming) return null

      const rows = upcoming.rows ?? []
      const confirmed = money(upcoming.confirmedAmount)
      const proposed = money(upcoming.proposedAmount)

      if (rows.length === 0) {
        return h(
          'div',
          { className: 'dl-upcoming' },
          h(
            'div',
            { className: 'dl-up-head' },
            h('b', null, '待 收'),
            h('span', { style: { letterSpacing: 0, fontSize: '10px' } }, '已公告未实施'),
          ),
          h('div', { className: 'dl-up-empty' }, '暂无已公告未实施的分红方案'),
          h(
            'div',
            { className: 'dl-up-note' },
            '公开数据源不提供 A 股派息到账日，所以只能按除权除息日归月。',
          ),
        )
      }

      return h(
        'div',
        { className: 'dl-upcoming' },
        h(
          'div',
          { className: 'dl-up-head' },
          h('b', null, '待 收'),
          h('span', { style: { letterSpacing: 0, fontSize: '10px' } }, rows.length + ' 笔未实施'),
          h(
            'span',
            { className: 'dl-up-head-sum' },
            '确定 ¥' + (confirmed ?? '0.00') +
              (Number(upcoming.proposedAmount) > 0 ? ' · 预案 ¥' + (proposed ?? '0.00') : ''),
          ),
        ),
        h(
          'div',
          { className: 'dl-up-rows' },
          rows.map((row, index) =>
            h(
              'div',
              { key: row.code + row.exDate + String(index), className: 'dl-up-row' },
              h('span', { className: 'dl-up-month' }, row.month),
              h(
                'span',
                { className: 'dl-up-main' },
                h(
                  'span',
                  { className: 'dl-up-name' },
                  row.name,
                  h(
                    'span',
                    {
                      className:
                        'dl-up-tag ' + (row.certain ? 'dl-up-tag-certain' : 'dl-up-tag-doubt'),
                    },
                    row.certain ? '确定' : '预案',
                  ),
                ),
                h(
                  'span',
                  { className: 'dl-up-detail' },
                  row.code + ' · 除权 ' + row.exDate +
                    (row.payDate ? ' · 到账 ' + row.payDate : '') +
                    ' · 每股 ' + row.perShare + ' × ' + row.shares + ' 股' +
                    (row.planText ? ' · ' + row.planText : ''),
                ),
              ),
              h(
                'span',
                {
                  className:
                    'dl-up-cash ' + (row.certain ? 'dl-up-certain' : 'dl-up-doubt'),
                },
                '¥' + (money(row.cashAmount) ?? '0.00'),
              ),
            ),
          ),
        ),
        h(
          'div',
          { className: 'dl-up-note' },
          '待收 = 每股派息 × 当前股数（未发生送转，不做股数推算）。',
          Number(upcoming.proposedAmount) > 0
            ? '「预案」尚未经股东大会审议，可能被否或被改。'
            : '',
          '归月按除权除息日；公开源无 A 股到账日。',
        ),
      )
    }

    // ------------------------------------------------------------------ 面板主体
    function LedgerPanel() {
      const [open, setOpen] = useState(false)
      const [tab, setTab] = useState('search')
      const [query, setQuery] = useState('')
      const [rows, setRows] = useState([])
      const [searching, setSearching] = useState(false)
      const [searchError, setSearchError] = useState('')
      const [asOf, setAsOf] = useState('')
      const [editing, setEditing] = useState(null)
      const [details, setDetails] = useState({})
      const [detailBusy, setDetailBusy] = useState(null)
      const [portfolio, setPortfolio] = useState(null)
      const [portfolioError, setPortfolioError] = useState('')
      const [portfolioBusy, setPortfolioBusy] = useState(false)
      const [deleting, setDeleting] = useState(null)
      const [toast, setToast] = useState('')
      const toastTimer = useRef(null)

      const showToast = useCallback((text) => {
        setToast(text)
        if (toastTimer.current) clearTimeout(toastTimer.current)
        toastTimer.current = setTimeout(() => setToast(''), 2600)
      }, [])

      const loadPortfolio = useCallback(async () => {
        setPortfolioBusy(true)
        setPortfolioError('')
        try {
          const payload = await getJson('/portfolio')
          setPortfolio(payload)
        } catch (error) {
          setPortfolioError(error && error.message ? error.message : '加载失败')
          setPortfolio(null)
        } finally {
          setPortfolioBusy(false)
        }
      }, [])

      useEffect(() => {
        if (!open) return
        loadPortfolio()
      }, [open, loadPortfolio])

      const loadDetail = useCallback(
        async (code, name) => {
          setDetailBusy(code)
          try {
            const payload = await getJson(
              '/detail?code=' +
                encodeURIComponent(code) +
                '&name=' +
                encodeURIComponent(name || ''),
            )
            setDetails((current) => ({ ...current, [code]: payload }))
            if (payload.fetchError) showToast('分红抓取失败：' + payload.fetchError)
            else if (payload.priceError) showToast('取价失败：' + payload.priceError)
          } catch (error) {
            setDetails((current) => ({
              ...current,
              [code]: {
                ok: false,
                error: error && error.message ? error.message : '加载失败',
              },
            }))
          } finally {
            setDetailBusy(null)
          }
        },
        [showToast],
      )

      // 搜索防抖：300ms，取消过期请求
      useEffect(() => {
        if (!open || tab !== 'search') return
        const needle = query.trim()
        if (needle === '') {
          setRows([])
          setSearchError('')
          return
        }
        let cancelled = false
        setSearching(true)
        const timer = setTimeout(async () => {
          try {
            const payload = await getJson('/search?q=' + encodeURIComponent(needle))
            if (cancelled) return
            setRows(payload.rows ?? [])
            setAsOf(payload.asOf ?? '')
            setSearchError(payload.searchError ?? '')
          } catch (error) {
            if (cancelled) return
            setRows([])
            setSearchError(error && error.message ? error.message : '搜索失败')
          } finally {
            if (!cancelled) setSearching(false)
          }
        }, 300)
        return () => {
          cancelled = true
          clearTimeout(timer)
          setSearching(false)
        }
      }, [query, open, tab])

      /**
       * 删掉一只持仓。
       *
       * 走 `holdings --set` 的**替换**语义：客户端读当前列表 → 去掉这只 → 整份写回。
       * 不做二次确认弹窗——面板是单一用户的本地账本，误删可以立刻重新入账，
       * 弹窗只会拖慢操作。删除结果用 toast 明确回报。
       */
      const deleteHolding = useCallback(
        async (row) => {
          setDeleting(row.code)
          try {
            const current = await getJson('/holdings')
            const kept = (current.holdings ?? []).filter((item) => item.code !== row.code)
            if (kept.length === (current.holdings ?? []).length) {
              showToast('这只不在持仓里：' + row.code)
              return
            }
            await postJson('/holdings/set', JSON.stringify(kept))
            setEditing(null)
            showToast('已删除：' + row.name + '（可随时重新入账）')
            await loadPortfolio()
          } catch (error) {
            showToast('删除失败：' + (error && error.message ? error.message : '未知错误'))
          } finally {
            setDeleting(null)
          }
        },
        [loadPortfolio, showToast],
      )

      const afterWrite = useCallback(async () => {
        setEditing(null)
        await loadPortfolio()
        setTab('held')
      }, [loadPortfolio])

      if (!open) {
        const collapsedTotals = portfolio ? portfolio.totals : null
        return h(
          'button',
          {
            className: 'dl-root dl-tab',
            onClick: () => setOpen(true),
            title: '分红账本',
            'aria-label': '打开分红账本',
          },
          h('span', { className: 'dl-tab-seal' }, '账'),
          h('span', null, '分红账'),
          collapsedTotals && pct(collapsedTotals.dynamicYield)
            ? h('span', { className: 'dl-tab-mark' }, pct(collapsedTotals.dynamicYield))
            : null,
        )
      }

      const totals = portfolio ? portfolio.totals : null
      const heldRows = portfolio ? portfolio.rows ?? [] : []
      const heldByCode = {}
      for (const row of heldRows) heldByCode[row.code] = row

      const dynamicTotal = totals ? pct(totals.dynamicYield) : null
      const expectedTotal = totals ? pct(totals.expectedYield) : null
      const yieldOnCostTotal = totals ? pct(totals.yieldOnCost) : null

      const header = h(
        'div',
        { className: 'dl-head' },
        h(
          'h2',
          { className: 'dl-title' },
          '分红账本',
          h(
            'span',
            { className: 'dl-title-sub' },
            totals
              ? '持仓 ' + totals.stockCount + ' 只 · 按除权除息日归集 · 税前'
              : '按除权除息日归集 · 税前',
          ),
        ),
        h(
          'button',
          {
            className: 'dl-iconbtn',
            onClick: () => loadPortfolio(),
            title: '重新取数',
            'aria-label': '重新取数',
            disabled: portfolioBusy,
          },
          portfolioBusy ? '…' : '↻',
        ),
        h(
          'button',
          { className: 'dl-iconbtn', onClick: () => setOpen(false), title: '收起', 'aria-label': '收起面板' },
          '×',
        ),
      )

      const tabs = h(
        'div',
        { className: 'dl-tabs' },
        h(
          'button',
          {
            className: 'dl-tabbtn' + (tab === 'search' ? ' dl-tabbtn-on' : ''),
            onClick: () => setTab('search'),
            type: 'button',
          },
          '查 股 入 账',
        ),
        h(
          'button',
          {
            className: 'dl-tabbtn' + (tab === 'held' ? ' dl-tabbtn-on' : ''),
            onClick: () => setTab('held'),
            type: 'button',
          },
          '持 仓' + (totals ? ' · ' + totals.stockCount : '') +
            (portfolio && portfolio.upcoming && portfolio.upcoming.eventCount
              ? ' · 待收 ' + portfolio.upcoming.eventCount
              : ''),
        ),
      )

      const searchPane = h(
        react.Fragment,
        null,
        h(
          'div',
          { className: 'dl-search' },
          h(
            'div',
            { className: 'dl-search-row' },
            h('span', { className: 'dl-search-label' }, '查 股'),
            h('input', {
              className: 'dl-input',
              value: query,
              onChange: (event) => setQuery(event.target.value),
              placeholder: '代码或名称，如 000333 / 美的集团',
              autoFocus: true,
              spellCheck: false,
            }),
          ),
          h(
            'div',
            { className: 'dl-search-meta' },
            h(
              'span',
              null,
              searching
                ? h('span', null, h('span', { className: 'dl-spin' }), ' 正在查 …')
                : query.trim() === ''
                  ? '输代码或名称 → 点「入 账」填股数'
                  : rows.length + ' 条结果 · 点「入 账」加入持仓',
            ),
            asOf ? h('span', null, '数据截至 ' + asOf) : null,
          ),
        ),
        h(
          'div',
          { className: 'dl-body' },
          searchError
            ? h(
                'div',
                { className: 'dl-empty' },
                h('span', { className: 'dl-empty-mark' }, '！'),
                '搜索没成功',
                h('small', null, searchError),
              )
            : rows.length === 0
              ? h(
                  'div',
                  { className: 'dl-empty' },
                  h('span', { className: 'dl-empty-mark' }, query.trim() === '' ? '帳' : '空'),
                  query.trim() === '' ? '输入代码或名称开始查' : '没有匹配的 A 股',
                  h(
                    'small',
                    null,
                    query.trim() === '' ? '搜到后点「入 账」填股数即可' : '换个关键词，或试试纯代码',
                  ),
                )
              : h(
                  react.Fragment,
                  null,
                  h(
                    'div',
                    { className: 'dl-cols dl-cols-search' },
                    h('span', null, '股票'),
                    h('span', { className: 'dl-cols-r' }, '收盘价'),
                    h('span', { className: 'dl-cols-r' }, '股息率'),
                  ),
                  rows.map((row, index) =>
                    h(
                      'div',
                      { key: row.code, style: { animationDelay: Math.min(index * 26, 220) + 'ms' } },
                      h(SearchRow, {
                        row,
                        detail: details[row.code],
                        busy: detailBusy === row.code,
                        open: editing !== null && editing.row.code === row.code,
                        onToggle: (merged) => {
                          const next =
                            editing !== null && editing.row.code === merged.code
                              ? null
                              : {
                                  row: merged,
                                  held: heldByCode[merged.code] ?? null,
                                }
                          setEditing(next)
                          if (next !== null && !details[merged.code]) {
                            loadDetail(merged.code, merged.name)
                          }
                        },
                      }),
                      editing !== null && editing.row.code === row.code
                        ? h(LedgerForm, {
                            row:
                              details[row.code] && details[row.code].row
                                ? Object.assign({}, editing.row, details[row.code].row)
                                : editing.row,
                            held: editing.held,
                            onDone: afterWrite,
                            onCancel: () => setEditing(null),
                            toast: showToast,
                          })
                        : null,
                    ),
                  ),
                ),
        ),
      )

      const heldPane = h(
        'div',
        { className: 'dl-body' },
        portfolioError
          ? h(
              'div',
              { className: 'dl-empty' },
              h('span', { className: 'dl-empty-mark' }, '！'),
              '持仓没加载出来',
              h('small', null, portfolioError),
            )
          : heldRows.length === 0
            ? h(
                'div',
                { className: 'dl-empty' },
                h('span', { className: 'dl-empty-mark' }, '空'),
                '持仓还是空的',
                h('small', null, '去「查股入账」搜一只，点「入 账」填股数'),
              )
            : h(
                react.Fragment,
                null,
                h(
                  'div',
                  { className: 'dl-cols dl-cols-held' },
                  h('span', null, '股票'),
                  h('span', { className: 'dl-cols-r' }, '持股 · 可点'),
                  h('span', { className: 'dl-cols-r' }, '收盘价'),
                  h('span', { className: 'dl-cols-r' }, '动态 / 预期'),
                ),
                heldRows.map((row, index) =>
                  h(
                    'div',
                    { key: row.code, style: { animationDelay: Math.min(index * 26, 220) + 'ms' } },
                    h(HeldRow, {
                      row,
                      open: editing !== null && editing.row.code === row.code,
                      onToggle: (target) =>
                        setEditing(
                          editing !== null && editing.row.code === target.code
                            ? null
                            : { row: target, held: target },
                        ),
                      onDelete: deleteHolding,
                    }),
                    editing !== null && editing.row.code === row.code
                      ? h(LedgerForm, {
                          row: Object.assign(
                            {},
                            row,
                            details[row.code] && details[row.code].row
                              ? details[row.code].row
                              : {},
                          ),
                          held: row,
                          onDone: afterWrite,
                          onCancel: () => setEditing(null),
                          toast: showToast,
                        })
                      : null,
                  ),
                ),
              ),
      )

      const totalBar = h(
        'div',
        { className: 'dl-total' },
        h(
          'div',
          { className: 'dl-total-title' },
          h('span', null, '整个持仓 · 市值加权'),
          totals && !totals.complete
            ? h('span', null, '部分覆盖：' + totals.pricedCount + '/' + totals.stockCount + ' 只有价格')
            : null,
        ),
        h(
          'div',
          { className: 'dl-total-grid' },
          h(
            'div',
            { className: 'dl-total-item' },
            h('span', { className: 'dl-total-k' }, '总动态股息率'),
            h(
              'span',
              {
                className:
                  'dl-total-v ' + (dynamicTotal === null ? 'dl-total-v-null' : 'dl-total-v-dyn'),
              },
              dynamicTotal === null ? '—' : dynamicTotal,
            ),
          ),
          h(
            'div',
            { className: 'dl-total-item' },
            h('span', { className: 'dl-total-k' }, '总预期股息率'),
            h(
              'span',
              {
                className:
                  'dl-total-v ' + (expectedTotal === null ? 'dl-total-v-null' : 'dl-total-v-exp'),
              },
              expectedTotal === null ? '—' : expectedTotal,
            ),
          ),
          h(
            'div',
            { className: 'dl-total-item' },
            h('span', { className: 'dl-total-k' }, '成本收益率'),
            h(
              'span',
              { className: 'dl-total-v ' + (yieldOnCostTotal === null ? 'dl-total-v-null' : '') },
              yieldOnCostTotal === null ? '—' : yieldOnCostTotal,
            ),
          ),
        ),
        totals
          ? h(
              'div',
              { className: 'dl-total-note' },
              '持仓市值 ¥' +
                (money(totals.marketValue) ?? '0.00') +
                ' · 近 12 个月应得 ¥' +
                (money(totals.dynamicCash) ?? '0.00') +
                ' · 已公告待收 ¥' +
                (money(totals.plannedCash) ?? '0.00') +
                (totals.costBasis ? ' · 成本 ¥' + money(totals.costBasis) : ''),
            )
          : null,
      )

      const noteBar = h(
        'div',
        { className: 'dl-note' },
        h('span', null, h('b', null, '动态股息率'), ' = 近 12 个月已派息 ÷ 收盘价'),
        h('span', null, h('b', null, '预期股息率'), ' = 已公告未实施派息 ÷ 收盘价'),
        h('span', null, h('b', null, '总股息率'), ' = Σ(每股派息 × 股数) ÷ 持仓市值'),
      )

      // 待收只在持仓页签显示：它讲的是"我持有的东西还没到账多少"
      const upcomingBlock =
        tab === 'held' && portfolio ? h(UpcomingBlock, { upcoming: portfolio.upcoming }) : null

      return h(
        'div',
        { className: 'dl-root dl-panel', role: 'dialog', 'aria-label': '分红账本' },
        header,
        tabs,
        tab === 'search' ? searchPane : heldPane,
        upcomingBlock,
        totalBar,
        noteBar,
        toast ? h('div', { className: 'dl-toast' }, toast) : null,
      )
    }

    // ------------------------------------------------------------------ 插件主体
    /** Required services: slots（布局挂载点）。 */
    const inject = ['slots']

    /**
     * 客户端插件体：在 shell.overlay 注册右侧账簿面板。
     * @param ctx - client root context
     */
    function apply(ctx) {
      ctx.slots.inject('shell.overlay', () =>
        ctx.slots.register({ name: 'shell.overlay', id: 'dsh-stock-dividend' }, () =>
          react.createElement(LedgerPanel, null),
        ),
      )
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
