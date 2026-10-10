# PLAN — A 股分红账本 DSH 插件

> 版本：v2（2026-10-10 更新：阶段一完成；新增阶段五客户端 UI 与阶段二范围扩展）  
> 状态：**一、二、三已完成；五、六已实现待重启验收；四为可选增强未做**  
> 配套文档：[SPEC.md](SPEC.md)（产品定义）、[PROGRESS.md](PROGRESS.md)（执行记录）

---

## 0. 阶段总览

| 阶段 | 名称 | 目标 | 网络 | 安装插件 | 状态 |
|---|---|---|---|---|---|
| **一** | **离线核心** | 持仓 + fixture 分红事件 → 三分区汇总；TS 适配层按真实契约实现并离线验证 | ❌ | ❌ | ✅ **已完成** |
| 二 | 真实取数 | **westock（主力源）** 联网取数 + CLI 安装 + 真实股票验证；**含预案数据（`--all`）** | ✅ | ✅ | ✅ **已完成** |
| 三 | 待收分红 | 已公告未实施分红单列"待收"区块（`confirmed` / `proposed`）；**不做任何历史外推** | ✅ | ✅ | ✅ **已完成** |
| 四 | 到账日补全 | **可选增强**：真实派息到账日（人工覆盖表 / 公告检索 / Tushare） | 可选 | ❌ | 未开始 |
| 五 | 客户端 UI | 查股入账 + 持仓面板：搜索、入账、改股数、删除、两个股息率、组合合计 | ✅ | ✅ | 🚧 **已实现待重启验收** |
| 六 | 装入 DSH | 装进 `desktop` profile，对话里可用 | ✅ | 🚧 | 🚧 **4 个对话工具已可用**；面板待重启验收 |

**数据源决策**：主力用 **westock**（免费、无需 Key）；Tushare 虽唯一提供 `pay_date` 到账日，但需 2000 积分（200 元/年），已决定不购买，故到账日补全降级为可选。完整论证见 [docs/data-source-selection.md](docs/data-source-selection.md)。

**为什么阶段一不装插件**：`desktop` profile 在 CLI 层被独占（见 SPEC §7.3），安装只能走应用内插件页或 `plugin_manager` 工具，且很可能需要重启应用——这一步天然属于最后一个阶段，不该污染核心开发的验证循环。

**为什么客户端 UI 排在阶段二之后**：搜索面板要显示股息数据，而真实取数在阶段二才打通。先做 UI 只会得到一个空壳，且 UI 只能在装进 profile 之后验证（离线测不了），所以必须等数据链路稳定。

---

## 阶段一：离线核心

### 1.1 交付物

```
dsh-stock-dividend/
├── SPEC.md / PLAN.md / PROGRESS.md / README.md
├── dividend_ledger/          # Python 核心（零第三方依赖，python3.11+）
│   ├── __init__.py
│   ├── __main__.py           # CLI 入口
│   ├── cli.py                # 子命令：holdings / sync / report / ledger
│   ├── codes.py              # 证券代码归一化
│   ├── decimals.py           # Decimal 精度与舍入策略
│   ├── models.py             # Holding / DividendEvent / LedgerEntry
│   ├── store.py              # data/ 读写、自然键幂等合并、审计
│   ├── providers/
│   │   ├── base.py           # DividendProvider 接口
│   │   ├── fixture.py        # 离线虚构 provider
│   │   └── westock.py        # 解析器（字段映射 + ÷10 换算），不联网
│   ├── ledger.py             # 归属匹配 + 单事件计算
│   ├── report.py             # 三分区汇总
│   └── errors.py
├── plugin/                   # 薄 TS 适配层（DSH 0.2.0-rc.2 契约）
│   ├── package.json          # dsh.bundle.patch + DSH peer 声明
│   ├── cordis.patch.yml
│   ├── tsconfig.json
│   ├── src/
│   │   ├── index.ts          # apply(ctx, config) / Config
│   │   ├── tools.ts          # defineTool 声明
│   │   ├── python.ts         # ctx.shell 调用封装 + 取消 + 超时
│   │   └── paths.ts          # session cwd / data 目录解析
│   └── test/
│       ├── tools.test.ts     # 对真实 @deepseek-ai/dsh-tools 校验定义不抛错
│       └── python.test.ts    # 用假 Python 可执行文件离线测调用路径
├── data/
│   ├── holdings.json         # 虚构持仓（6 只）
│   ├── events.fixture.json   # 虚构分红事件
│   ├── westock.sample.json   # westock 真实响应样例（手工抄录，供解析器单测）
│   └── out/                  # 生成物
└── tests/
    ├── test_codes.py
    ├── test_decimals.py
    ├── test_store_idempotent.py
    ├── test_ledger_matching.py
    ├── test_report.py
    ├── test_westock_parser.py
    ├── test_cli.py
    └── expected/report.json   # 黄金基线
```

### 1.2 任务拆解

**A. Python 核心**

| # | 任务 | 验收 |
|---|---|---|
| A1 | 代码归一化（`600519`/`sh600519`/`600519.SH` → `600519.SH`） | 单测覆盖 6 位补后缀规则与 4 种输入写法；非法输入被拒并给出原因 |
| A2 | 十进制策略（每股 6 位、金额 2 位、银行家舍入、汇总不中间舍入） | 单测锁定；断言 JSON 中**不出现浮点** |
| A3 | 数据模型与 `data/` 读写 | 空文件 / 缺文件 / 字段缺失均给出明确错误 |
| A4 | 自然键幂等合并 + 审计计数 | 同一输入导入两次 → 文件与输出**逐字节一致**；改一个数字 → `events_replaced=1` 且无重复 |
| A5 | `DividendProvider` 接口 + `FixtureProvider` | 接口不含任何计算逻辑（代码审查项） |
| A6 | westock 解析器（`cashDiviRMB÷10`、`bonusShareRatio÷10`、`tranAddShareRatio÷10`） | 对 `data/westock.sample.json` 断言每股派息与送转比例；含"每 10 股"量纲回归测试 |
| A7 | 归属匹配 `ex_date >= start_date` | 边界：`ex_date == start_date` 计入；`ex_date == start_date - 1天` 排除 |
| A8 | 单事件计算（现金 / 送股 / 转增 / `null` 比例指标） | 缺 `cost_per_share` → `yield_on_cost=null` 且进 `missing_inputs`；缺 `close_prev` → `dividend_yield=null` |
| A9 | 三分区汇总 | `by_year`/`by_month`/`by_stock` 字段名一致；手算一组小数据交叉验证总额 |
| A10 | CLI 子命令 + JSON 输出（`--out` / stdout） | 命令行可复现；退出码语义明确（0 成功 / 非 0 附原因） |
| A11 | 到账日覆盖表 `data/pay_dates.json` | 命中 → `date_basis=pay_date_override`；未命中 → `ex_date` |
| A12 | 报告 `meta.date_basis` 与 `missing_inputs` | 断言报告中**不存在**把推定到账日当真实值的字段 |

**B. 虚构数据**

| # | 任务 | 验收 |
|---|---|---|
| B1 | 6 只虚构持仓，跨 3 年，含：有成本/无成本、有价格/无价格、送股、转增、`ex_date` 恰好在 `start_date` 当天 | 每条数据都对应一个断言 |
| B2 | 对应分红事件集 | 手工算出的总额与程序输出一致 |
| B3 | `data/westock.sample.json` | 结构照抄 westock A 股字段名，含一条"仅送转不派现" |

**C. TS 适配层**

| # | 任务 | 验收 |
|---|---|---|
| C1 | `package.json` + `cordis.patch.yml`（`dsh.bundle.patch`、DSH peer 声明） | 对照 SPEC §7.3 与契约文档逐条核对 |
| C2 | `defineTool` 4 个工具定义（扁平 `parameters`、`output:{schema,render}`） | 对真实 `@deepseek-ai/dsh-tools` 调用 `defineTool`，断言不抛错；断言非法写法会抛错 |
| C3 | `ctx.shell` 调用封装（workdir / timeoutMs / `sandboxPolicy` / `exec.signal` 取消） | 用假 Python 可执行文件（`#!/bin/sh` 脚本）离线跑通；断言超时与取消路径 |
| C4 | 路径解析（`exec.agent?.session.header.cwd` → `data/`） | 单测覆盖 cwd 缺失时的回退与报错 |
| C5 | 工具执行结果渲染（只回关键数字 + 文件路径，不回大 JSON） | 断言渲染输出长度受控 |

**D. 文档**

| # | 任务 |
|---|---|
| D1 | `README.md`：中文讲解（这是什么、怎么用、数字怎么来的、口径与假设） |
| D2 | `PROGRESS.md`：阶段一执行记录 + 决策日志 + 遗留问题 |
| D3 | `docs/`：保留实证版插件契约；补一份"口径与算法"说明 |

### 1.3 验收标准

```bash
# 1. 幂等：连跑两次，逐字节一致
python3.11 -m dividend_ledger report --data data --out data/out/report.json
cp data/out/report.json /tmp/a.json
python3.11 -m dividend_ledger report --data data --out data/out/report.json
diff /tmp/a.json data/out/report.json          # 必须无差异

# 2. 单测全绿
python3.11 -m unittest discover -s tests -v

# 3. TS 层类型与契约测试
cd plugin && npm run check                      # tsc --noEmit && node --test
```

- **零网络**：全程不发起任何网络请求（可用断网或 `--offline` 断言）。
- **零第三方依赖**：Python 只用标准库；TS 只依赖 DSH 运行时 peer 与 `typescript`。
- **可追溯**：任取 `by_stock` 里一个数字，能在 `ledger.json` 明细中逐条加总还原。

### 1.4 明确不做（阶段一）

联网取数、安装 westock CLI、装进 `desktop` profile、客户端 UI、客户端插件、定时任务、真实到账日、持仓变动建模、港股美股。

### 1.5 风险与回退

| 风险 | 回退 |
|---|---|
| `defineTool` 契约理解偏差 | 已用真实 `@deepseek-ai/dsh-tools` 写断言测试，偏差会立刻暴露；契约文档有 [V] 分级可查 |
| westock 样例字段抄错 | 解析器单测 + 量纲回归测试双保险；二阶段联网时会立刻对不上 |
| fixture 数据过于"干净"掩盖边界 bug | B1 强制覆盖 6 类边界场景 |
| 三分之一的工作量花在 TS 层却无法真正验证 | C 组任务全部用假 Python + 真实 `dsh-tools` 离线验证，不依赖安装插件 |

---

## 阶段二：真实取数

**目标**：打通 westock 联网取数，并用真实股票交叉验证阶段一的解析器与量纲。

| # | 任务 | 验收 |
|---|---|---|
| B1 | 安装 `westock` CLI（腾讯官方源，脚本自带 SHA256 双段校验链，装到 `~/.local/bin`） | `westock --help` 可用 |
| B2 | 跑通 `westock dividend sh600519 --years 5`，确认 A 股字段名与文档一致 | 输出的字段名与 `docs/westock-skill-reference` 记录**逐字对上** |
| B3 | **量纲交叉验证（最高优先级）**：用真实响应验证 `cashDiviRMB` 确实是"每 10 股" | 用手算 + `dividendPlan` 原文双向核对；错则修 ÷10 |
| B4 | 用真实响应替换 fixture 黄金基线，保留 fixture 作为离线回归 | 真实数据与 fixture 两套基线都能跑 |
| B5 | `close_prev` 通过 `westock kline` 取除权前收盘价 | 股息率在真实数据下不再是 `null` |
| B6 | 用 3–5 只真实股票验证金额/条数/除权日与公开资料一致 | 逐条可复核 |
| B7 | 支持 `westock dividend --all`（含预案）并解析 `procedure` 状态 | 预案行**不进历史累计**，只进阶段三的"待收"区块 |
| B8 | provider 真实 CLI runner（目前只支持注入假 runner） | `WestockProvider` 可直接联网使用 |
| B9 | 更新 `SPEC.md` §5.2 / `PROGRESS.md` | 记录真实验证结果与任何字段差异 |

**关键约束**：遵守该 Skill 的"禁止绕过"规则——命中能力域时只走 `westock` CLI，不直连 HTTP、不用 `web_search` 替代。

**风险**：CLI 需要联网；真实接口可能改字段。回退路径是 fixture provider 始终可用，解析器有单测兜底。

## 阶段三：待收分红（已公告未实施）

**目标**：把"确定之后哪个月分多少"算出来，且**绝不与历史累计混在一起**。

三种东西性质完全不同，必须分开：

| 类别 | 例子 | 处理 |
|---|---|---|
| ① 已公告未实施 | "10派25.911元，除权除息日 2026-06-19" | ✅ **确定性计算**：`每股派息 × 当前股数`，标 `confirmed` |
| ② 董事会预案 | "10派25元（预案），待股东大会审议" | ⚠️ 可列，标 `proposed` + 公告日，明示"可能被否/被改" |
| ③ 按历史规律外推 | "连续 5 年 6 月分红，明年大概也…" | ❌ **不做**——这是预测，属于非目标 |

| # | 任务 | 验收 |
|---|---|---|
| C1 | 事件模型增加 `status`（`implemented` / `announced` / `proposed`）与 `ann_date` | 严格校验；缺 `ann_date` 的 `proposed` 被拒 |
| C2 | `report.json` 增加独立的 `upcoming` 区块 | **断言历史累计金额里不含任何未实施事件** |
| C3 | 待收金额 = 每股派息 × **当前**股数（未发生送转，不做股数推算） | 明确记录该假设 |
| C4 | 按预计到账月份分组，回答"哪个月要分多少" | 与 `by_month` 同构但独立 |
| C5 | 若无任何已公告方案，`upcoming` 为空数组而非报错 | 空态可读 |

**为什么单列**：一旦把未来金额混进 `totals.cash_amount`，"我已经收了多少"就失真了。这条会有专门的测试守护。

## 阶段四：到账日补全（可选增强）

A 股公开源没有派息到账日（SPEC §5.2），唯一提供 `pay_date` 的 Tushare 需 2000 积分（200 元/年），**已决定不购买**。因此本阶段降级为可选增强，不阻塞主线。三条路径按优先级：

1. **人工覆盖表** `data/pay_dates.json`（一阶段就绪）：从券商流水抄几条关键记录即可让重点年份变精确。
2. **公司分红实施公告检索**：从公告文本抽取"现金红利发放日"，免费但需文本抽取。
3. **Tushare `dividend`**：一次性解决，但需 2000 积分；若将来购买，按"核心零依赖、provider 允许可选依赖"接入。

目标：提升 `date_basis` 中 `pay_date_*` 的占比，使月度汇总趋近真实现金流。

## 阶段五：客户端 UI（可视下拉选股）

**目标**：在 DSH Web 界面里做一个**搜索 + 加入持仓**的小面板，不再手敲 JSON。

**范围（刻意做小）**：

- 一个入口（`shell.overlay` 槽位或侧边按钮）
- 搜索框：代码/名称模糊匹配 → 候选下拉
- 选中后显示：名称、代码、最近一笔分红、当前股息率
- 一个"加入持仓"表单：股数 + 开始持有日 + 每股成本（可选）
- **不做**：完整持仓表、图表、三个汇总分区的可视化——那些继续在对话里用工具看

### 实测到的实现代价（来自 `dsh-stock-watch@1.1.0`）

| 组成 | 它怎么做的 | 我们要做什么 |
|---|---|---|
| 股票池 | `data/a_stocks.json` 等，512K（A 股 5549 只） | 自建本地池并随仓库打包（离线、即时） |
| 宿主侧 | `ctx.webServer` 注册 `/dsh-stock-watch/*` REST 路由 | 注册 `/dsh-dividend/*` 路由（搜索/持仓读写） |
| 浏览器侧 | `client.js` **98KB 预构建产物**（React + `shell.overlay`） | 需要同等结构，用 `tsdown` 构建 |
| 生效 | 客户端产物必须预先构建；改完刷新页面 | 同 |

### 任务

| # | 任务 | 验收 |
|---|---|---|
| E1 | 本地 A 股股票池：生成脚本 + 随仓库打包的 JSON（含来源标注） | 离线可搜；池子大小与来源记录在案 |
| E2 | 宿主侧搜索路由 | 单测：模糊匹配、代码归一化、空结果 |
| E3 | `dsh.client` 声明 + `tsdown` 构建链 | `client.js` 能被构建出来 |
| E4 | 搜索面板组件（下拉候选） | **只能装进 profile 后在浏览器里验** |
| E5 | "加入持仓"表单 → 调宿主路由写入 | 写入后对话里 `dividend_holdings` 能看到 |
| E6 | 阶段五专属验收说明 | 明确写清"UI 未经离线验证"这一事实 |

**诚实声明**：UI 逻辑**无法离线验证**——浏览器端产物必须在装进 profile 并刷新页面后才能确认。这与阶段一"零网络、可离线全验证"的纪律冲突，所以本阶段必须**单独验收**，且不得把它算进核心的可测试承诺里。

## 阶段六：装入 DSH

1. 把 `plugin/` 作为本地包装进 `desktop` profile（应用内插件页或 `plugin_manager` 工具，**不能用 CLI**）。
2. 重启应用，验证工具在对话中可见可调用、客户端面板可见。
3. 用真实持仓跑一遍完整流程。
4. 补中文讲解、更新 `PROGRESS.md`。

**注意**：此阶段会打断当前 DSH 会话，需你确认时机。

---

## 决策日志

| 日期 | 决策 | 理由 |
|---|---|---|
| 2026-10-10 | 产品形态定为"持仓 + 分红方案 → 算分红"，不是流水记账 | 用户澄清：输入是持仓与开始持有日 |
| 2026-10-10 | 一阶段不装 `desktop` profile | CLI 层被独占（现实约束）+ 降低风险 |
| 2026-10-10 | 数据源抽象为 `DividendProvider`，一阶段只做 fixture | 守住"离线测试"硬要求，同时给真实源留好接口 |
| 2026-10-10 | 汇总主口径用除权除息日，`pay_date` 作可选手工覆盖 | A 股公开源无到账日（已实证） |
| 2026-10-10 | 主口径税前，税只作参考净额 | 避免税率规则污染主数字 |
| 2026-10-10 | 不用内容指纹去重，改自然键幂等覆盖 | 数据是声明式快照，覆盖语义更干净 |
| 2026-10-10 | 比例指标两个都要，缺数据置 `null` | `null` 比 0 诚实 |
| 2026-10-10 | 选 westock（腾讯自选股）作为二阶段真实源 | 用户指定；CLI 官方发布、字段完整 |
| 2026-10-10 | westock 解析器放一阶段做，联网留二阶段 | 钉死真实契约，同时保持离线 |
| 2026-10-10 | **westock 定为唯一主力源；Tushare 不购买积分、仅留接口占位** | 免费零门槛；Tushare 虽唯一提供到账日但需 200 元/年 |
| 2026-10-10 | **接受"按除权除息日汇总"为主口径** | 主力源无到账日；用 `date_basis` 如实标注 + 人工覆盖表兜底 |
| 2026-10-10 | 阶段三降级为可选增强，不阻塞阶段四 | 到账日既已接受退化，就不该卡住主线 |
| 2026-10-10 | 维持"Python 核心零第三方依赖" | 不引入 `tushare` 包，离线可跑性优先 |
| 2026-10-10 | **未来分红只做"已公告未实施"（含预案标注），不做任何历史外推** | 已公告是算术，外推是预测；用户明确排除预测 |
| 2026-10-10 | **未来分红单列 `upcoming` 区块，绝不混入历史累计** | 否则"我已经收了多少"失真 |
| 2026-10-10 | **选股做可视下拉面板（客户端插件）**，范围限定为"搜索 + 加入持仓" | 用户明确要 dsh-stock-watch 那种体验；范围刻意做小以控制代价 |
| 2026-10-10 | **客户端 UI 排在阶段二之后** | 搜索面板要显示真实股息数据；先做 UI 只会得到空壳 |
| 2026-10-10 | 客户端 UI **单独验收**，不计入核心可测试承诺 | UI 只能在装进 profile 后于浏览器验证，无法离线验 |
