# dsh-plugin.org 提交内容

目标市场：[dsh-plugin.org](https://dsh-plugin.org)（社区市场，走 **GitHub Issue** 提交，
不是 PR）。对照 [提交要求](https://dsh-plugin.org/zh/submit) 逐条自查、并把提交正文准备好。

## 收录要求自查

| 要求 | 状态 |
|---|---|
| 仓库公开 | ✅ `wonderjz99/dsh-stock-dividend`（PUBLIC） |
| 已加 GitHub topic `dsh-plugin` | ✅ |
| README 含安装命令 `dsh plugin --profile web add <包名>` | ✅ 见 README「安装」方式一 |
| 插件导出 `apply(ctx)` | ✅ `plugin/src/index.ts` |
| 声明开源许可证 | ✅ MIT（`LICENSE` + `package.json.license`） |
| README 说明能做什么、怎么装 | ✅ 含「它是什么，不是什么」与三种安装方式 |
| 不冒充官方 | ✅ 非官方社区项目 |

## 提交记录

**已提交**：[dshplugin/dsh-plugin-hub#147](https://github.com/dshplugin/dsh-plugin-hub/issues/147)
（2026-10-10，状态 OPEN）

## 提交方式（备查）

打开 <https://github.com/dshplugin/dsh-plugin-hub/issues/new>，**标题与正文照下面填**。

> 该仓库的 Issue 模板只有 `bug_report.yml` 与 `feature_request.yml`，没有插件提交模板，
> 所以直接新建空白 Issue 并粘贴下面内容。

### 标题

```
[插件提交] wonderjz99/dsh-stock-dividend — 按持仓与公开分红方案算出每只股票应得多少分红
```

### 正文

```markdown
### 仓库地址
https://github.com/wonderjz99/dsh-stock-dividend

### 一句话价值
记录持有哪些 A 股、从哪天开始、多少股，自动按公开分红方案算出每只股票累计应得多少现金分红，
并给出组合层面的成本收益率与股息率；已公告未实施的待收分红单列，不计入历史累计。

### 能力分类
工具与能力

### 安装命令
dsh plugin --profile web add dsh-stock-dividend

（备用，从源码安装）
dsh plugin --profile web add github:wonderjz99/dsh-stock-dividend

### 兼容与运行要求
- DSH：0.2.0-rc.2（开发与验证所用版本）
- 无需 Node 侧额外依赖（包内已含编译产物）
- 需要 Python 3.11+（核心仅用标准库，无需 pip install、无需 venv）
- 联网获取真实分红数据需要另行安装 westock CLI（第三方工具，不随本插件分发）；
  不装它时离线功能（记持仓、读本地事件、出报告、算指标）全部可用
- 已在 macOS 验证

### 许可证
MIT

### 截图 / 演示
真实输出（离线 demo 数据）：

```
$ python3.11 -m dividend_ledger --data data/demo --text report
分红账（除权除息日 19 条）

  税前分红合计   ¥ 26,856.40
  持有成本       ¥ 482,600.00
  成本收益率     5.56%
  股息率         1.45%
  持股数         6 只
```

浏览器面板：DSH 界面右侧「分红账」立牌 → 面板内含股票搜索、入账/改股数/删除、
动态股息率与预期股息率、以及独立的「待 收」区块。

### 补充说明
- **不连接券商账户**，不读取 Cookie 或任何凭据，不需要 API Key
- **不依据历史规律外推未来分红**（那是预测，属明确非目标）
- **待收金额绝不进历史累计**——已公告未实施方案单列成区块，有专门的测试守护这条不变量
- 金额与比例在 JSON 中均为十进制字符串（不出现浮点数）；缺输入是 null，不填 0
- 已知限制：持有期内股数视为恒定，不建模加仓/减仓；公开 A 股数据源不提供派息到账日，
  故按除权除息日归月
- 核心为 Python，Node 层是薄适配；npm 包内已包含 Python 核心，安装后即可用

### 提交前自查
- [x] 仓库已公开
- [x] 已添加 GitHub topic：dsh-plugin
- [x] README 包含安装命令
- [x] 插件导出 apply(ctx) 模块
```

## 收录后的徽章

拿到详情页地址后（形如 `https://dsh-plugin.org/plugins/wonderjz99/dsh-stock-dividend`），
可把下面徽章加进 README 顶部：

```markdown
[![Listed on dsh-plugin.org](https://dsh-plugin.org/badges/listed.svg)](https://dsh-plugin.org/plugins/wonderjz99/dsh-stock-dividend)
```

## 两个市场的关系

| 市场 | 收录方式 | 本项目状态 |
|---|---|---|
| [deepseek1024.com](https://deepseek1024.com)（`imsai-sh/awesome-deepseek-harness-plugins`） | PR 一个 `catalog/plugins/*.json`，自动校验后自动合并 | ✅ 已收录（[PR #587](https://github.com/imsai-sh/awesome-deepseek-harness-plugins/pull/587)） |
| [dsh-plugin.org](https://dsh-plugin.org)（`dshplugin/dsh-plugin-hub`） | GitHub Issue | ⏳ 本文档即其提交内容 |

两者是**互相独立的社区项目**，不冲突，可以都提交。
