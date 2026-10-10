# 分红数据源选型

> 调研日期：2026-10-10  
> **已定决策：主力用 westock（免费、无需 Key）；Tushare 作为预留可选 provider。**  
> 取舍代价：放弃 `pay_date`（派息到账日），汇总主口径退化为除权除息日 + 人工覆盖表。  
> 依据均来自官方文档原文，引用见文末。

---

## 1. 选型的唯一决定性判据

产品原始需求是**按"派息到账日"归集**做年度/月度汇总（SPEC §4.1）。

但在 A 股公开数据源里，**只有 Tushare 提供派息到账日**：

| 数据源 | 派息到账日字段 | 结论 |
|---|---|---|
| **Tushare** `dividend` | ✅ `pay_date`（派息日） | **唯一满足** |
| westock（腾讯自选股） | ❌ A 股字段集无此字段（仅港股 `cashPayDate`、美股 `payDate`） | 不满足 |
| 同花顺 Financial-API `adjustment-factors` | ❌ 仅 `ex_date_ms` | 不满足 |

其余差异都是次要的。这条直接决定了主力选择。

---

## 2. 三家横向对比

| 维度 | **Tushare `dividend`** | **westock `dividend`** | **同花顺 Financial-API** |
|---|---|---|---|
| 派息到账日 | ✅ `pay_date` | ❌ | ❌ |
| 每股口径 | ✅ 直接给**每股**（`cash_div` 税后 / `cash_div_tax` 税前） | ⚠️ 给**每 10 股**（`cashDiviRMB`），须 ÷10 | ⚠️ 给**每股** `dividend_per_share`（税前） |
| 量纲陷阱 | 无 | **有**：漏 ÷10 则金额差 10 倍 | 无 |
| 送股 / 转增 | ✅ 拆开：`stk_bo_rate` 送股、`stk_co_rate` 转增 | ⚠️ `bonusShareRatio` / `tranAddShareRatio`（每 10 股口径） | ⚠️ 合并为 `per_share_bonus`，不区分送/转 |
| 股权登记日 | ✅ `record_date` | ✅ `rightRegDate` | ❌ |
| 实施进度 | ✅ `div_proc`（预案/实施） | ✅ `procedure` | ❌ |
| 公告日 / 实施公告日 | ✅ `ann_date` / `imp_ann_date` | ❌（有 `reportEndDate` 报告期） | ❌ |
| 中文方案原文 | ❌ | ✅ `dividendPlan`（如 `10派25.911元`） | ❌ |
| 历史起点 | 2000-01-01 | 未标明 | 未标明 |
| 单次返回上限 | 2000 行 | 未标明（`--limit`） | 单只逐次查询 |
| 接入方式 | Python SDK / HTTP，需 token | `westock` Go CLI（Skill 禁止绕过直连 HTTP） | REST，需 API Key |
| 门槛 | **2000 积分 = 200 元/年** | **免费、无需 Key** | 需 Key（用户项目已有） |
| 频次 | 2000 积分档：200 次/分钟、10 万次/天/API | 未标明 | 单只查询，全市场需 ~5000 次 |
| 港 / 美股分红 | 无（港美股无分红接口） | ✅ 港股 `cashDivPerShare`+`cashPayDate`、美股 `dividend`+`payDate` | 无 |

---

## 3. 最终分工（已定）

### 主力：westock（阶段二落地）

**为什么它是主力**

1. **免费、无需 API Key**——零成本即可跑通真实取数闭环。
2. **唯一覆盖港股/美股分红**，且这两者**自带到账日字段**（`cashPayDate` / `payDate`）。
3. `dividendPlan` 中文方案原文可用于人工交叉校验每股派息数字。
4. 送转拆成 `bonusShareRatio` / `tranAddShareRatio` 两个字段，可分别落库。
5. 已按你的指定接入，解析器已排入阶段一。

**必须防的坑**

- `cashDiviRMB` 是**每 10 股派息（元）**，漏 ÷10 则金额差 10 倍 → 量纲回归测试。
- `bonusShareRatio` / `tranAddShareRatio` 同为每 10 股口径 → 同样 ÷10。
- **A 股没有派息到账日** → `date_basis` 只能标 `ex_date`。

### 预留：Tushare `dividend`（不购买积分，接口占位）

保留它的原因不是"以后可能买"，而是**它是验证 provider 抽象是否切准的试金石**：Tushare 有 `pay_date`、直接给每股、送转拆字段、还有 `ann_date`/`div_proc` 这类 westock 没有的溯源字段。若将来插入它能做到**不改动核心计算与汇总**，说明 `DividendProvider` 抽象是对的。

**当前不接入**：2000 积分 = 200 元/年，免费档仅 120 积分且**无权调用** `dividend` 接口。用户已决定不购买。

**将来若要接入，注意**：`tushare` 是第三方包，会打破"Python 核心零第三方依赖"。届时按"核心零依赖、provider 允许可选依赖"处理（延迟 import + 缺失时给出可读错误并回退）。

### 已评估，不采用：同花顺 Financial-API

`adjustment-factors` 是与复权因子共用的宽表：缺到账日、缺股权登记日、缺实施进度、送转不区分。全市场 Parquet dump 适合批量研究，不适合本项目逐股精细口径。你本机 `~/Projects/Financial-API` 保持现状，不接入。

---

## 4. 落地顺序（对 PLAN.md 的影响）

```
阶段一（离线）     fixture provider + westock 解析器（字段映射与 ÷10 换算）+ 单测
                   ↑ 不变：无需 Key、无需联网
阶段二（真实取数） westock 联网打通 → 免费跑通真实取数闭环，验证 provider 抽象
阶段三（到账日）   三条可选路径，均不阻塞主线：
                   ① 人工覆盖表 data/pay_dates.json（一阶段已就绪，随时可用）
                   ② 公司分红实施公告检索（联网，免费，需抽取文本）
                   ③ Tushare provider（需 2000 积分，已决定暂不购买）
阶段四（装入 DSH） 装进 desktop profile，对话里可用
```

**阶段三不再是"必做"**：既然接受按除权除息日汇总，到账日补全就降级为可选增强，主线在阶段二后即可进入阶段四。

---

## 5. 决策记录

| # | 决策 | 日期 |
|---|---|---|
| 1 | **westock 定为主力数据源**（免费、无需 Key、覆盖港美股） | 2026-10-10 |
| 2 | **Tushare 暂不接入**（2000 积分 = 200 元/年，用户决定不购买），保留 provider 接口占位以验证抽象 | 2026-10-10 |
| 3 | **接受"按除权除息日汇总"**作为主口径，`date_basis` 如实标注；到账日补全降级为可选增强 | 2026-10-10 |
| 4 | 维持"Python 核心零第三方依赖"约束不变（不引入 `tushare` 包） | 2026-10-10 |
| 5 | 同花顺 Financial-API 不接入 | 2026-10-10 |

---

## 引用来源

- Tushare `dividend` 接口文档（字段、2000 积分门槛、2000 行上限、2000-01-01 起点）：<https://tushare.pro/document/2?doc_id=103>
- Tushare 积分与频次权限对应表（120 积分免费档、2000 积分 = 200 元/年）：<https://tushare.pro/document/1?doc_id=290>
- westock A 股/港股/美股分红字段集：`docs/westock-skill-reference/references/ai_usage_guide.md:497-499`
- westock "A股分红数据为每10股派息"：`docs/westock-skill-reference/references/scenarios-guide.md:515,554`
- westock 调用方式与禁止绕过：`docs/westock-skill-reference/SKILL.md`
- 同花顺 `adjustment-factors` 字段：`~/Projects/Financial-API/docs/api/endpoints-prices.md:133-179`、`endpoints-market-dumps.md:113-129`
