# SPEC — A 股分红账本 DSH 插件

> 版本：v1（2026-10-10 定稿）  
> 状态：**产品定义，待用户确认**；本文件只描述"做什么"，实现顺序见 [PLAN.md](PLAN.md)。  
> 基线环境：DeepSeek Harness CLI `0.2.0-rc.2`（桌面端 `desktop` profile）。

---

## 0. 一句话定义

一个装在 DSH 里的插件：你在对话里告诉它**你持有哪几只 A 股、从哪天开始持有、多少股**，它按除权除息日把分红方案匹配到你的持有窗口，算出**每只股票累计拿了多少分红、总分红是多少、占你的持仓成本多少**。

它**不是**券商流水记账工具，也**不是**行情/选股工具。

## 1. 目标用户与核心问题

| 项 | 内容 |
|---|---|
| 用户 | 持有若干 A 股、想知道"这些股票到底给我发了多少钱"的个人投资者 |
| 核心问题 | "我拿的这几只股，从我开始持有到现在，一共分了多少红？占我投入的比例是多少？" |
| 回答粒度 | 每只股票 / 每一年 / 每个月 / 全部合计 |
| 使用方式 | 在 DSH 对话里说出来，插件通过工具调用完成 |

### 成功判据

1. 给出 3–10 只股票的开始持有日与股数后，能在一次对话中拿到可信的分红总额与占比。
2. 每一个数字都能追溯到"哪个分红事件 × 多少股 × 每股派息多少"，不存在黑箱估算。
3. 同一份输入重复计算，结果逐字节一致。

## 2. 数据流（这是整个产品的主干）

```
① 持仓输入        代码 + 开始持有日 + 股数(+每股成本)
        │
        ▼
② 分红方案来源    DividendProvider 接口
        ├── fixture provider   （离线虚构数据，一阶段测试用）
        └── westock provider   （腾讯自选股数据，二阶段联网）
        │
        ▼
③ 归属匹配        ex_date >= start_date 的事件 → 归到这只股票
        │
        ▼
④ 计算            现金分红 = 每股派息 × 股数
                  送股股数 = 每股送转比例 × 股数
        │
        ▼
⑤ 汇总            按日期依据归集年 / 月（有到账日用到账日，否则除权除息日），按股票跨年累计
        │
        ▼
⑥ 输出            单文件 JSON：by_year / by_month / by_stock + 明细账
```

## 3. 数据模型

### 3.1 Holding — 持仓（用户输入）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `code` | string | ✅ | 归一化代码，`600519.SH` 形式（见 §6） |
| `start_date` | string | ✅ | 开始持有日 `YYYY-MM-DD` |
| `shares` | string | ✅ | 持有股数（十进制字符串，支持送股产生的碎股） |
| `cost_per_share` | string | ⬜ | 每股成本，用于成本收益率；缺失则该股收益率置 `null` |
| `name` | string | ⬜ | 证券名称，仅展示用 |
| `account` | string | ⬜ | 账户标签，预留多账户 |

**建模假设（一阶段明确成立，写入报告的解释区）**：
- 持有期内股数**恒定**，不建模加仓/减仓/清仓。
- **送股与转增不反向增加后续事件的股数**，只作为"期间额外获得 X 股"的独立信息列出。
- **分红再投资不建模**。

### 3.2 DividendEvent — 分红事件（provider 输出）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `code` | string | ✅ | 归一化代码 |
| `ex_date` | string | ✅ | 除权除息日 `YYYY-MM-DD` |
| `dividend_per_share` | string | ✅ | **每股**税前现金分红（元）。源数据为"每 10 股"时必须 ÷10 后落库 |
| `pay_date` | string | ⬜ | 派息到账日；缺失时由 `date_basis` 说明归集依据 |
| `date_basis` | enum | ✅ | `ex_date` \| `pay_date_declared` \| `pay_date_override` |
| `per_share_bonus` | string | ⬜ | **每股**送股比例（`0.1` = 10 送 1），默认 `0` |
| `per_share_transfer` | string | ⬜ | **每股**转增比例（`0.1` = 10 转 1），默认 `0` |
| `right_reg_date` | string | ⬜ | 股权登记日，溯源用 |
| `close_prev` | string | ⬜ | 除权除息日**前一交易日**收盘价，用于股息率；缺失则股息率置 `null` |
| `plan_text` | string | ⬜ | 原始中文方案，如 `10派25.911元`，仅溯源展示 |
| `source` | string | ✅ | provider 标识，如 `fixture` / `westock` |

### 3.3 LedgerEntry — 计算明细（插件派生，非输入）

按 `(code, ex_date)` 一条：

| 字段 | 说明 |
|---|---|
| `code` / `name` / `ex_date` / `pay_date` / `date_basis` | 继承自事件 |
| `shares_at_event` | 参与计算时的股数（一阶段恒等于 `holding.shares`） |
| `cash_amount` | 现金分红 = `dividend_per_share × shares`（税前） |
| `bonus_shares` | 送股股数 |
| `transfer_shares` | 转增股数 |
| `cost_basis` | `shares × cost_per_share`，无成本则 `null` |
| `yield_on_cost` | `cash_amount ÷ cost_basis`，无成本则 `null` |
| `dividend_yield` | `dividend_per_share ÷ close_prev`，无价格则 `null` |
| `tax_reference` | 参考税后净额（§4.3），仅参考，不进入主口径 |
| `source_ref` | 溯源信息（provider 名 + 原始字段快照） |

## 4. 业务口径（全部为一阶段已锁定决策）

### 4.1 归集口径

- **年度 / 月度汇总按"到账日期"归集**——这是产品的原始诉求。
- 但 A 股真实数据源**只给除权除息日**（见 [§5.2](#52-westock-provider二阶段联网)）。唯一提供到账日的 Tushare 需 2000 积分（200 元/年），已决定**不购买**（§5.3）。因此：

  > **逻辑闭环**：若数据源提供到账日 → **必须按到账日汇总**；若提供人工覆盖 → 按覆盖值汇总；两者都没有 → 按除权除息日汇总。每一层都必须在 `date_basis` 中如实标注，**不允许把除权日或推定值伪装成真实到账日**。

  - 一阶段落地：主口径 `ex_date`，标注 `date_basis: "ex_date"`；
  - 提供**可选的人工覆盖表** `data/pay_dates.json`（键 `code + ex_date` → 真实到账日），命中则 `date_basis: "pay_date_override"`；
  - 报告显式说明本次汇总用的是哪种依据。
- 跨年事件按到账日落入对应年份；到账日早于开始持有日的事件不归属（见 §4.2）。

### 4.2 归属规则

事件 `E` 归到持仓 `H`，当且仅当 `E.code == H.code` 且 `E.ex_date >= H.start_date`。

- 除权除息日在开始持有日之前的事件**不属于你**——哪怕到账日在你持有期内。
- 该规则可解释、可复核；不做任何"按比例分配"。

### 4.3 税务口径

- **主口径：税前**。所有主数字均为税前现金分红。
- 红利税只作为 `tax_reference`（参考税后净额）单列，并标明所用规则。
- 一阶段税率规则留一个**可配置规则位**（默认不启用），不把它做成会污染主口径的黑箱。

### 4.4 比例指标（两个都要）

| 指标 | 公式 | 缺失时 |
|---|---|---|
| 成本收益率 `yield_on_cost` | 累计税前分红 ÷ 持有成本 | `cost_per_share` 缺失 → `null` |
| 股息率 `dividend_yield` | 每股派息 ÷ 除权除息日前收盘价 | `close_prev` 缺失 → `null` |

**绝不**在缺数据时用 0 或猜测值代替：一律 `null` + 在报告中列为 `missing_inputs`。

### 4.5 金额与精度

- 输入输出**一律十进制字符串**，禁止 JSON 浮点。
- 内部用 `decimal.Decimal`；每股派息保留 6 位、金额保留 2 位（银行家舍入）。
- 汇总用 `Decimal` 求和，不做中间四舍五入。

### 4.6 去重 / 幂等

分红事件是**声明式快照**，不是事件流，因此不用内容指纹去重，而用**自然键幂等覆盖**：

| 记录类型 | 自然键 |
|---|---|
| 现金分红 | `(code, ex_date)` |
| 送股 / 转增 | `(code, ex_date, kind)` |

- 同一份输入导入两次 → 结果完全相同，不产生重复记录。
- 修改某个数字后再导入 → 覆盖旧值，**不留幽灵记录**。
- 一次导入输出审计：`events_imported` / `events_replaced` / `events_rejected`（附拒绝原因）。

### 4.7 未来分红（已公告未实施）

**只做"已公告"，不做任何外推。** 三种东西性质完全不同：

| 类别 | 例子 | 处理 |
|---|---|---|
| ① 已公告未实施 | "10派25.911元，除权除息日 2026-06-19" | ✅ 确定性计算：`每股派息 × 当前股数`，标 `confirmed` |
| ② 董事会预案 | "10派25元（预案），待股东大会审议" | ⚠️ 可列，标 `proposed` + 公告日，明示可能被否/被改 |
| ③ 按历史规律外推 | "连续 5 年 6 月分红，明年大概也…" | ❌ **不做**——这是预测 |

**两条铁律**：

1. **未来金额单独成区**（`report.json` 的 `upcoming`），**绝不混入 `totals.cash_amount`**。
   一旦混进去，"我已经收了多少"就失真了。这条有专门的测试守护。
2. **用当前股数计算**，不做股数推算——未来事件还没发生送转，按今天的股数算是唯一诚实的口径。

数据源支持：westock 的 `dividendFlag` + `procedure`（"预案"/"实施"），
`westock dividend --all` 可返回含预案的记录。

## 5. 数据来源

### 5.1 DividendProvider 接口（一阶段唯一抽象）

```
list_events(codes: list[str], start: date, end: date) -> list[RawEvent]
```

- 一阶段实现：`FixtureProvider`（读 `data/` 下的虚构 JSON，零网络）。
- provider 只负责"把源数据翻译成 §3.2 的字段"，**不参与任何业务计算**。

### 5.2 westock provider（阶段二已联网验证）

数据源：腾讯自选股 Skill `@tencent-adm/westock-data` v1.0.7，通过 `westock` Go CLI 调用
（**不直连 HTTP**，遵守该 Skill 的"禁止绕过"约束）。

命令：`westock dividend <代码> --years N [--all]`

> **真实行为已核对并落盘**：完整记录见 [docs/westock-verified-behaviour.md](docs/westock-verified-behaviour.md)。
> 下面这张表以**实际输出**为准，不是以 Skill 文档为准——两者有 6 处不符。

#### 输出格式

**没有 JSON 开关**。CLI 只出 **Markdown 表格**：

```
#### sh600519
**分红历史**
| bonusShareRatio | cashDiviRMB | dividendFlag | dividendPlan | dividendType | exDiviDate | procedure | proposalSn | reportEndDate | rightRegDate | totalCashDiviComRMB | tranAddShareRatio |
| --- | …
|  | 308.76 | 是 | 10派308.760元 | 有分红 | 20240619 | 方案实施 | 1 | 20231231 | 20240618 | 38786363272.80 |  |
```

行内**不含股票代码**，代码来自 `#### <代码>` 段头。所以解析器按段切分，表头顺序做了强校验
（字段错位比报错危险得多，因此表头不符时直接抛错而不是静默跳过）。

#### 字段映射

| westock 字段 | 映射到 | 换算 | 真实取值注意 |
|---|---|---|---|
| `exDiviDate` | `ex_date` | — | `YYYYMMDD` |
| `rightRegDate` | `right_reg_date` | — | `YYYYMMDD` |
| `cashDiviRMB` | `dividend_per_share` | **÷ 10** | 真实值是"每 10 股派息（元）" |
| `bonusShareRatio` | `per_share_bonus` | **÷ 10** | 无值时是**空字符串** |
| `tranAddShareRatio` | `per_share_transfer` | **÷ 10** | 无值时是空字符串 |
| `dividendPlan` | `plan_text` | — | 方案原文，如 `10派308.760元` |
| `proposalSn` | `proposal_sn` | — | **同一除权日多方案的唯一键**；真实取值 `1`（普通）/ `2`（特殊分红） |
| `procedure` | `status` 推导 | — | 真实值**只有"方案实施"**——普查 47 行无一行预案 |
| `dividendFlag` | 仅溯源 | — | 真实值 **"是"**，不是文档说的 `0/1` |
| `dividendType` | 仅溯源 | — | `有分红` / `特殊分红` |
| — | `pay_date` | — | **A 股无此字段**（只有港股 `cashPayDate`、美股 `payDate`） |

#### 三条已实测的关键行为

1. **量纲三重验证通过**：用 `totalCashDiviComRMB ÷ (cashDiviRMB ÷ 10)` 反推总股本，
   茅台得 1,256,197,800 股、宁德时代得 2,442,311,158 股，与真实总股本吻合到个位。
   若按每股口径，反推结果会小 10 倍。
2. **同一除权除息日可能有多条方案**：宁德时代 2026-04-22 是
   `proposalSn=1` 普通分红 21.78 元 + `proposalSn=2` 特殊分红 47.79 元。
   所以事件唯一键必须是 `(code, ex_date, proposalSn)`——不含它会把两条合并、**丢掉一笔分红**。
3. **限频时 CLI 退出码仍为 0**，只把"请求过于频繁，请稍后再试"写进 stdout。
   若不识别，会被当成"这只股票没有分红数据"，账本**静默少算分红**。
   因此解析器显式识别该提示并抛错，runner 带重试与退避。

#### 状态判定（`status`）

**不能只看 `procedure` 文本**——实测 `方案实施` 的行也可能带未来除权日：

| 情形 | `status` |
|---|---|
| `procedure` 含"预案/取消/终止/否决/不分配/未实施" | `proposed` |
| `方案实施` 且 `exDiviDate <= 今天` | `implemented` |
| `方案实施` 且 `exDiviDate > 今天` | **`confirmed`**（已公告未实施，即"未来预期分红"来源） |

> ⚠️ **实测限制**：本数据源普查 47 行，`procedure` **只出现"方案实施"**，
> `--all` 也**不返回预案**。所以在此源上 `status` 实际只会是 `implemented`
> （或未来除权日出现时的 `confirmed`），**`proposed` 无法实测**。
> 阶段三若要做"董事会预案"，需换数据源（Tushare `div_proc`）或手工录入。
> 详见 [docs/westock-verified-behaviour.md](docs/westock-verified-behaviour.md) §3.1。

#### 取数节流与重试

实测连续请求会触发限频，且限频按**时间窗**恢复（20 秒以上才稳）。因此：

- 默认每只股票之间间隔 **8 秒**（`--sleep` 可调）；
- 命中限频或空输出时**重试 3 次**，退避 25/50/75 秒（`--retries` 可调）；
- 进度写 **stderr**，不污染 stdout 的 JSON。

#### 已知缺口：`close_prev`

`dividend` 端点**不返回价格**，所以 `close_prev` 目前恒为 `null` → **股息率为 `null`**。
补齐需要另取 K 线，代价是每个事件一次请求，而限频很紧，所以**延后到阶段三**。
成本收益率不依赖它，仍是可用的。

### 5.3 主力数据源决策

**决策：westock 为主力，Tushare 为预留可选 provider。** 完整论证见 [docs/data-source-selection.md](docs/data-source-selection.md)。

| 数据源 | 到账日 | 每股口径 | 门槛 | 定位 |
|---|---|---|---|---|
| **westock** | ❌ | ⚠️ 每 10 股，须 ÷10 | **免费、无需 Key** | **主力** |
| Tushare `dividend` | ✅ `pay_date` | ✅ 直接每股 | 2000 积分 = 200 元/年 | 预留（`providers/tushare.py` 接口占位） |
| 同花顺 Financial-API | ❌ | ✅ | 需 Key | 不采用 |

**关键取舍（显式记录）**：Tushare 是唯一提供 `pay_date`（派息到账日）的源，但需 2000 积分（免费档仅 120 积分且**无权调用** `dividend` 接口）。用户选择不购买积分，因此：

- 主力走 westock，**按到账日汇总退化为按除权除息日汇总**，`date_basis` 如实标注为 `ex_date`；
- 真实到账日的三条补全路径保留但降级为可选：① 人工覆盖表（一阶段已就绪）② 将来购买 Tushare 积分 ③ 公司分红实施公告检索；
- `DividendProvider` 接口必须保证插入 Tushare provider 时**无需改动核心计算与汇总**（这是抽象是否切准的验收标准）。

### 5.4 其他已评估源（不采用）

- `~/Projects/Financial-API`（同花顺金融数据服务）：`GET /api/a-share/corporate-actions/adjustment-factors` 提供 `dividend_per_share` / `per_share_bonus` / `ex_date_ms`，**无到账日、无股权登记日、不区分送股与转增**；全市场 Parquet dump 适合批量研究，不适合逐股精细口径。

## 6. 输入输出契约

### 6.1 证券代码归一化

- 对外统一 `600519.SH` / `000001.SZ` / `430139.BJ` 形式。
- 接受输入：`600519`、`sh600519`、`600519.SH`、`SH600519` → 统一归一化。
- 纯 6 位代码按规则补后缀：`6xxxxx → .SH`、`0xxxxx/3xxxxx → .SZ`、`4xxxxx/8xxxxx → .BJ`。
- 归一化失败 → 拒绝该条并给出原因，不静默丢弃。

### 6.2 目录约定

```
data/
├── holdings.json        # 输入：持仓（用户维护）
├── events.json          # 输入：分红事件快照（provider 抓取落盘）
├── pay_dates.json       # 输入（可选）：到账日人工覆盖表
└── out/
    ├── ledger.json      # 输出：计算明细账
    └── report.json      # 输出：三分区汇总
```

### 6.3 report.json 结构

```jsonc
{
  "meta": {
    "format_version": "1",
    "generated_at": "2026-10-10T14:00:00+08:00",
    "date_basis": "ex_date",          // 本次归集依据
    "inputs": { "holdings": "…", "events": "…", "pay_dates": null },
    "missing_inputs": ["600519.SH.cost_per_share", "000001.SZ.close_prev"]
  },
  "totals": {
    "cash_amount": "12345.67",        // 税前
    "cost_basis": "100000.00",
    "yield_on_cost": "0.1235",
    "dividend_yield": "0.0312",       // 加权，缺价者为 null
    "bonus_shares": "120.0",
    "tax_reference": "0.00",
    "stock_count": 6,
    "event_count": 23
  },
  "by_year":  [ { "year": "2024", "cash_amount": "…", "yield_on_cost": "…", "by_stock": [ … ] } ],
  "by_month": [ { "month": "2024-06", "cash_amount": "…", "event_count": 3 } ],
  "by_stock": [ { "code": "600519.SH", "name": "…", "cash_amount": "…",
                  "cost_basis": "…", "yield_on_cost": "…", "dividend_yield": "…",
                  "bonus_shares": "…", "event_count": 5 } ]
}
```

三个分区的字段名保持一致；`by_year` 内嵌 `by_stock` 切片；`by_stock` 为**跨年累计**。

## 7. DSH 插件形态

### 7.1 技术架构

```
plugin/  (薄 TS 适配层)
   ├── defineTool 声明 4 个工具
   └── 通过 ctx.shell 调用 Python 核心（带 workdir / timeout / sandboxPolicy）
              │
              ▼
dividend_ledger/  (Python 核心，零第三方依赖)
   ├── 持仓与事件读写、匹配、计算、汇总、JSON I/O
   └── CLI 子命令，stdout 出 JSON
```

**分工铁律**：业务逻辑 100% 在 Python；TS 层只做参数校验、路径解析、进程调用、结果渲染。TS 层不得复制任何计算规则。

### 7.2 对话中暴露的工具（一阶段设计）

| 工具 | 作用 |
|---|---|
| `dividend_holdings` | 查看 / 写入持仓 |
| `dividend_sync` | 拉取或导入分红事件（一阶段为 fixture） |
| `dividend_report` | 生成三分区汇总并回读关键数字 |

命名与参数结构待工具契约单测锁定后最终确定。

### 7.3 已实证的 DSH 硬约束（会直接影响实现）

1. **`desktop` profile 由 Electron 应用独占管理** —— `dsh plugin --profile desktop add <path>` 被 CLI 的 `rejectElectronProfile` 直接拒绝。安装只能走应用内插件页或 `plugin_manager` 工具，且很可能需要重启应用。
2. **`defineTool` 的真实契约** —— `parameters` 是**扁平属性表**（不是 `type/properties/required` 包裹），`output` 必须是 `{ schema, render }`；否则 `ctx.tools.register` 直接抛错。社区文档里的写法在此版本无效。
3. **`timeoutMs` 只是声明式** —— 工具注册表不强制超时，Python 调用必须自己设定并可被取消（`exec.signal`）。
4. **子进程必须走 `ctx.shell` 且显式传 `sandboxPolicy`** —— 直接 `child_process.spawn` 拿到的是**无沙箱**子进程。

依据详见 [docs/dsh-plugin-contract-0.2.0-rc.2.md](docs/dsh-plugin-contract-0.2.0-rc.2.md)（含 [V] 已验证 / [D] 官方文档 / [I] 推断 分级）。

## 8. 非目标（明确排除）

- ❌ 连接真实券商账户、读取登录 Cookie、下单或查询资金流水
- ❌ 股价预测、择时信号、选股推荐
- ❌ 定时任务、后台常驻、自动轮询
- ❌ 图表、K 线、行情盯盘（不重复 `dsh-stock-watch` 的功能）
- ❌ 按历史规律外推未来分红（**已公告未实施的方案除外**——那是算术不是预测，见 §4.7）
- ⏳ 客户端界面在**阶段五**才做，且范围限定为"搜索 + 加入持仓"（见 PLAN.md）
- ❌ 港股、美股、基金、可转债（接口预留货币字段，但一阶段不实现）
- ❌ 加仓/减仓/清仓的持仓变动建模
- ❌ 分红再投资建模
- ❌ 多用户、云同步

## 9. 风险与失效模式

| 风险 | 影响 | 应对 |
|---|---|---|
| 到账日拿不到真值（因选免费源而**已知接受**） | 月度分布与真实现金流有偏差 | 显式标注 `date_basis`；提供人工覆盖表；报告中不允许伪装；Tushare provider 接口预留（见 §5.3） |
| 除权除息日归属规则与券商口径不一致 | 个别事件归属差异 | 规则写死且可复核；明细账可逐条核对 |
| `cashDiviRMB` 单位理解错（每 10 股 vs 每股） | **金额差 10 倍** | 解析器单测锁定 ÷10；用已知方案文本交叉验证 |
| 送转股导致股数增长未建模 | 长期累计分红被低估 | 一阶段假设写入报告；`bonus_shares` 独立列出；二阶段再引入持仓变动 |
| westock CLI 不可用 / 接口变更 | 二阶段取数失败 | provider 隔离，fixture provider 始终可用；解析器有单测兜底 |
| Python 解释器缺失或版本过低 | 插件完全不可用 | 要求 `python3.11+`；启动时探测并给出明确错误；核心零第三方依赖 |

## 10. 术语表

| 术语 | 含义 |
|---|---|
| 除权除息日 `ex_date` | 股价因分红而调整的交易日；分红归属的判定基准 |
| 股权登记日 `right_reg_date` | 决定谁有权获得分红的登记日 |
| 派息到账日 `pay_date` | 现金实际到达账户的日期；A 股公开数据源不提供 |
| `date_basis` | 本次汇总实际使用的归集日期依据 |
| 每股派息 | 每股税前现金分红（元）；源数据"每 10 股"必须 ÷10 |
| 成本收益率 | 累计税前分红 ÷ 持有成本 |
| 股息率 | 每股派息 ÷ 除权除息日前收盘价 |
| fixture | 离线虚构测试数据 |
