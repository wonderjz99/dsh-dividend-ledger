# 算法与口径说明

> 这份文档解释**每个数字是怎么来的**，以及为什么这么定。
> 产品定义见 [../SPEC.md](../SPEC.md)，实现见 `dividend_ledger/`。

---

## 0. 数据流

```
① holding        code + start_date + shares (+ cost_per_share)
                            │
② provider       DividendProvider.list_events(codes, start, end)
                 ├── fixture   本地 JSON（离线）
                 └── westock   腾讯自选股（解析器已就绪，联网见阶段二）
                            │
③ matching       ex_date >= start_date  →  归属
                            │
④ per-event      现金 = 每股派息 × 股数
                 送股 = 每股送股比例 × 股数
                 转增 = 每股转增比例 × 股数
                            │
⑤ aggregate      按 effective_date 分年 / 分月，按 code 跨年累计
                            │
⑥ output         data/out/report.json + data/out/ledger.json
```

---

## 1. 证券代码归一化（`codes.py`）

对外统一 `600519.SH` 形式。接受的输入：

```
600519  sh600519  SH600519  600519.SH  600519.sh  sh.600519  sh:600519  600519.XSHG
```

纯 6 位代码按首位补后缀：`6xxxxx/5xxxxx → .SH`、`0xxxxx/3xxxxx/1xxxxx/2xxxxx → .SZ`、
`4xxxxx/8xxxxx/9xxxxx → .BJ`（`92xxxx` 是北交所，优先于 `9` 匹配）。

### 为什么显式后缀冲突时要拒绝

`000001` 是平安银行（深市），而 `000001.SH` 是上证指数——**两个完全不同的标的**。
如果静默"纠正"成 `.SZ`，账本里会把指数行情算进银行股的分红。
所以 `000001.SH` 会被拒绝并提示"可能你要写的是 000001.SZ"。

同理 `sh000001.SZ`（前后缀互相矛盾）也被拒绝。

---

## 2. 精度策略（`decimals.py`）

### 三条铁律

1. **解析只走字符串。** 传 `float` 一律拒绝——`0.1 + 0.2 != 0.3` 这类误差一旦进账本就追不回来。
2. **舍入只在输出边界发生。** 中间过程保留完整精度，汇总只做 `Decimal` 加法。
3. **JSON 中不出现浮点。** 所有金额/比例字段都是十进制字符串。

### 位数约定

| 类别 | 位数 | 例 |
|---|---|---|
| 金额 | 2 位 | `26856.40` |
| 每股派息 / 送转比例 | 6 位 | `30.876000` |
| 价格（成本、收盘价） | 2 位 | `1950.00` |
| 比率（收益率、股息率） | 6 位 | `0.055649` |
| 股数 | 去尾零 | `100`、`10.5` |

每股派息恒 6 位是刻意的：`0.1` 与 `0.100000` 在账本里应当长得一样，
统一形态才能让"连跑两次逐字节一致"这种验收可被 `diff` 验证。人读的渲染层负责去零。

### 舍入方式

`ROUND_HALF_EVEN`（银行家舍入）：`0.005 → 0.00`、`0.015 → 0.02`、`0.025 → 0.02`。

**只在输出时舍入一次。** 十条 `0.005`：
- 先舍入再相加 → `0.00`（错）
- 只求和 `0.050`，输出时舍入 → `0.05`（对）

### 零的归一

`Decimal("-0").quantize(Decimal("0.01"))` 会得到 `-0.00`；
`Decimal("0.000000")` 直接格式化会得到 `0.000000` 而不是 `0`。
账本里这些都是噪声，`decimals.py` 统一抹平成 `0.00` / `0` / `0.000000`。

---

## 3. 归属匹配（`ledger.py`）

```
事件 E 归到持仓 H  ⇔  E.code == H.code  且  E.ex_date >= H.start_date
```

### 边界

| 情形 | 结果 |
|---|---|
| `ex_date == start_date` | **计入**（当天除权除息，你已经是股东） |
| `ex_date == start_date − 1 天` | **排除** |
| `ex_date >= start_date`，但 `pay_date < start_date` | **计入**（判定只看除权除息日） |
| `ex_date < start_date`，但 `pay_date >= start_date` | **排除** |
| 代码不同 | 既不计入、也不算"被排除"（跟这条持仓无关） |

### 为什么被排除的事件要单独留档

`ledger.json` 里有 `excluded_events`，每条带原因：

```json
{
  "code": "600377.SH",
  "ex_date": "2021-07-15",
  "holding_start_date": "2022-01-10",
  "reason": "除权除息日 2021-07-15 早于开始持有日 2022-01-10，该次分红不属于本次持仓",
  "dividend_per_share": "0.46"
}
```

用户看到"少了一笔"时，必须能查到它为什么不在。**静默丢弃是不可接受的。**

---

## 4. 单事件计算

```
现金分红   = 每股派息 × 股数
送股股数   = 每股送股比例 × 股数
转增股数   = 每股转增比例 × 股数
持有成本   = 股数 × 每股成本                    （缺成本 → null）
成本收益率 = 现金分红 ÷ 持有成本                （缺成本或成本为 0 → null）
股息率     = 每股派息 ÷ 除权除息日前收盘价        （缺价格或价格为 0 → null）
```

### 缺失一律 `null`

`missing_inputs` 用机器可读的标签说明缺了什么：

| 标签 | 影响的字段 | 含义 |
|---|---|---|
| `cost_per_share` | `yield_on_cost` | 未提供每股成本 |
| `close_prev` | `dividend_yield` | 未提供除权除息日前收盘价 |

**为什么不用 0：** 0 会被读成"这只股票没赚到钱"，而事实是"我们不知道"。
前者是结论，后者是事实——账本只能给事实。

### 参考税后净额

`tax_reference` 一阶段恒为 `null`。红利税率的持股期限规则留了一个**可配置规则位**，
默认不启用：主口径是税前，税只作参考，不能污染主数字。

---

## 5. 三分区汇总（`report.py`）

归集日期用每条记录的 `effective_date`：有到账日用得到账日，否则用除权除息日。

| 分区 | 归集键 | 特点 |
|---|---|---|
| `by_year` | `effective_date.year` | 年内再拆 `by_stock`；最新年份在前 |
| `by_month` | `effective_date` 的 `YYYY-MM` | 只有金额，不输出需要分母的比率 |
| `by_stock` | `code` | **跨年累计** |

### 成本基数是"存量"不是"流量"——这是最容易错的地方

因为一阶段假设持有期内股数恒定，每只股票的成本基数是个**常量**。
如果按事件逐条累加，持有 5 年有 5 条事件就会把这个成本算 5 遍。

所以：

- `by_stock`：成本基数 = 该股持有成本。**跨股票相加有意义**，等于总投入成本。
- `by_year`：某年的成本基数 = "该年有分红的那几只股票的**全部**持有成本"。
  它是**该分区的分母**，**不可跨年相加**（同一只股票会在多个年份重复出现）。
- `by_month`：同理，所以干脆不输出成本与比率，避免"月度成本收益率"这种误导性指标。

`tests/test_report.py::test_year_cost_basis_is_not_sum_of_years` 锁定了这一点。

### 股息率按**股数**加权

组合股息率 = `Σ现金 ÷ Σ(股数 × 除权前收盘价)`。

**不能**写成 `Σ每股派息 ÷ Σ收盘价`——那等于把 1 股茅台（1500 元）和
1 股高速（9 元）等同看待，结果被严重低估。

手算例子（`tests/test_report.py::test_share_weighted_not_price_summed`）：

| 股票 | 股数 | 收盘价 | 市值 | 每股派息 | 现金 |
|---|---|---|---|---|---|
| 甲 | 1000 | 10 | 10000 | 1 | 1000 |
| 乙 | 10 | 100 | 1000 | 1 | 10 |

- 正确（按股数加权）：`1010 ÷ 11000 = 0.091818`
- 错误（Σ派息 ÷ Σ价格）：`2 ÷ 110 = 0.018182` ← 差 5 倍

### 分子分母必须同覆盖范围

如果有的事件缺价格，`dividend_yield` 只能覆盖**有价格的那些事件**：

- 分子：只算有价格事件的现金
- 分母：只算对应的市值

否则会出现"分子含全部现金、分母只含有价格的市值"这种被夸大的比率。
覆盖不完整时 `dividend_yield_complete` 为 `false`，并给出 `dividend_yield_note`
说明"股息率只覆盖了 N 笔"。

---

## 6. 幂等与去重（`store.py`）

**不用内容指纹去重**，因为持仓与分红事件是**声明式快照**，不是事件流。

| 记录类型 | 自然键 |
|---|---|
| 持仓 | `code` |
| 现金分红事件 | `(code, ex_date)` |
| 送转事件 | `(code, ex_date, "bonus")` |

| 情形 | 处理 |
|---|---|
| 键不存在 | 新增（`imported`） |
| 键存在、内容相同 | 计数 `unchanged`，文件不变 |
| 键存在、内容变了 | **覆盖**旧值（`replaced`），不留幽灵记录 |

所以：

- 同一份输入导入两次 → 结果逐字节相同；
- 改一个数字后再导入 → 覆盖，记录数不变。

### 已有坏行时拒绝合并

如果 `events.json` 里已经有无法解析的行，`import_events` 会**直接报错**而不是继续。
否则重写文件时那些行会被静默丢弃——**数据丢失比报错严重得多**。

---

## 7. westock 解析器（`providers/westock.py`）

### 量纲陷阱

| westock 字段 | 含义 | 换算 |
|---|---|---|
| `cashDiviRMB` | **每 10 股**派息（元） | **÷ 10** |
| `bonusShareRatio` | **每 10 股**送股（股） | **÷ 10** |
| `tranAddShareRatio` | **每 10 股**转增（股） | **÷ 10** |

依据：该 Skill 自己的文档 `scenarios-guide.md:515`——*"A股分红数据为『每10股派息』（cashDiviRMB）"*。

**漏掉 ÷10 会让所有金额差 10 倍。** 三道防线：

1. `WESTOCK_SHARES_BASIS = Decimal(10)` 常量集中表达；
2. `tests/test_westock_parser.py::SharesBasisTest` 量纲回归测试；
3. `DividendProvider._assert_per_share_scale` 运行期体检——
   每股派息 > 100 元或送转比例 > 10 时直接报错，把 10 倍误差挡在账本之外。

### 被跳过的行

| 情况 | 原因 |
|---|---|
| `procedure` 含"预案/取消/终止/否决/不分配" | 还没真正分红，计入会重复计数 |
| 缺 `exDiviDate` | 无法确定归属日期 |
| 现金、送股、转增全为 0 | 纯配股或不分配方案 |
| 同一 `(code, ex_date)` 出现两次 | 会让分红被重复计数 → 直接报错 |

空值标记 `-`、`--`、`""`、`null` 都按 0 处理；千分位 `"2,000"` 与横杠日期 `2024-05-23` 都能解析。

### `close_prev` 不在分红接口里

`dividend` 端点不返回价格，所以解析出来的 `close_prev` 恒为 `None` → 股息率为 `null`。
要算股息率需要另取一次 `westock kline`，取 `ex_date` 前一交易日收盘价（阶段二）。

---

## 8. 输出契约

### `report.json`

```jsonc
{
  "format_version": "1",
  "meta": {
    "generated_at": "2026-10-10T12:00:00+08:00",
    "date_basis": "ex_date",              // 主归集依据
    "date_basis_detail": "除权除息日 17 条；人工覆盖的派息到账日 2 条",
    "date_basis_counts": { "ex_date": 17, "pay_date_override": 2 },
    "inputs": { "data_dir": "...", "generated_at_source": "fixed" },
    "missing_inputs": ["601988.SH.cost_per_share"],
    "audit": { "pay_dates": { "overrides_applied": 2, "overrides_unmatched": [] } },
    "warnings": [],
    "notes": ["主口径为税前现金分红；…", "…"]
  },
  "totals":  { "cash_amount": "26856.40", "cost_basis": "482600.00",
               "yield_on_cost": "0.055649", "dividend_yield": "0.014519",
               "dividend_yield_complete": true, "event_count": 19, "stock_count": 6,
               "bonus_shares": "10", "transfer_shares": "40" },
  "by_year":  [ { "year": "2025", "…": "…", "by_stock": [ … ] } ],
  "by_month": [ { "month": "2025-07", "cash_amount": "…" } ],
  "by_stock": [ { "code": "600519.SH", "name": "…", "…": "…" } ]
}
```

### `ledger.json`

逐事件明细，用于"这个汇总数字是怎么加出来的"逐条核对：

```jsonc
{
  "meta": { … },
  "entries": [ { "code": "600519.SH", "ex_date": "2024-06-19",
                 "effective_date": "2024-06-19", "date_basis": "ex_date",
                 "shares_at_event": "100", "dividend_per_share": "30.876000",
                 "cash_amount": "3087.60", "bonus_shares": "10",
                 "cost_basis": "195000.00", "yield_on_cost": "0.015833",
                 "dividend_yield": "0.020584", "tax_reference": null,
                 "close_prev": "1500.00", "source": "fixture" } ],
  "excluded_events": [ … ],
  "missing_inputs": [ … ],
  "stats": { "holdings": 6, "entries": 19, "events_considered": 22,
             "excluded_events": 3, "date_basis_counts": { … } }
}
```

### 可复现性

`--now` 给了固定时间戳后，输出**逐字节可复现**，可以直接 `diff`：

```bash
python3.11 -m dividend_ledger report --now 2026-10-10T12:00:00+08:00
```

不给 `--now` 就用当前时间（东八区），`meta.inputs.generated_at_source` 会标 `now`。

---

## 9. 一阶段已知的偏差（完整清单）

| # | 偏差 | 影响 | 计划 |
|---|---|---|---|
| 1 | A 股无派息到账日，按除权除息日归集 | 年度汇总基本不受影响；**月度可能偏差一个月** | 人工覆盖表 / 公告检索 / Tushare（阶段三，可选） |
| 2 | 送转股不增加后续事件股数 | **持有期长的股票累计分红可能偏低** | 引入持仓变动事件（阶段二后评估） |
| 3 | 不建模加仓 / 减仓 / 清仓 | 中途变动的持仓算不准 | 同上 |
| 4 | 不建模分红再投资 | 再投资产生的额外股份不计 | 明确不做（非目标） |
| 5 | 红利税率规则未启用 | `tax_reference` 恒为 `null` | 留了可配置规则位 |
| 6 | `close_prev` 需另取行情 | 缺价格时股息率为 `null` | 阶段二接 K 线 |
| 7 | 算"应该收到"而非"确实收到" | 无法发现券商少发/算错 | 产品形态边界，非缺陷 |

---

## 10. 术语表

| 术语 | 含义 |
|---|---|
| 除权除息日 `ex_date` | 股价因分红而调整的交易日；**分红归属的判定基准** |
| 股权登记日 `right_reg_date` | 决定谁有权获得分红的登记日 |
| 派息到账日 `pay_date` | 现金实际到达账户的日期；A 股公开源不提供 |
| `date_basis` | 该条记录实际使用的归集日期依据（见下） |
| `effective_date` | 实际参与归集的日期 = 有到账日用得到账日，否则用除权除息日 |
| 每股派息 | 每股税前现金分红（元）；源数据"每 10 股"必须 ÷10 |
| 成本收益率 | 累计税前分红 ÷ 持有成本 |
| 股息率 | 现金分红 ÷ 除权除息日前市值（按股数加权） |

### `date_basis` 的三种取值

| 值 | 含义 | 谁产生 |
|---|---|---|
| `ex_date` | 数据源只给除权除息日 | fixture、westock（A 股） |
| `pay_date_declared` | 数据源声明了派息到账日 | 未来的 Tushare provider |
| `pay_date_override` | 命中 `data/pay_dates.json` 人工覆盖 | 用户 |

三者**必须与是否真的提供了 `pay_date` 自洽**：`from_dict` 会拒绝
`date_basis=pay_date_declared` 但没给 `pay_date` 的记录——否则报告的归集依据会撒谎。
