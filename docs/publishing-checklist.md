# 发布清单

> 这份清单是"推到 GitHub / 装给别人用"之前必须逐项确认的东西。
> 打勾前请真的执行过那条验证，不要凭"应该没问题"打勾。

## 一、阻断项（不满足就不能发布）

| # | 项 | 状态 | 怎么验 |
|---|---|---|---|
| 1 | **在真实 DSH 中加载成功** | ✅ **已验证**（2026-10-10） | `selfcheck` 显示 5 个服务全部就绪；8 条路由全部注册 |
| 2 | 面板核心流程可用 | ✅ **已验证**（数据链路） | 搜索命中美的集团、单股详情抓到预期股息率、写入往返字节一致 |
| 3 | 无真实持仓入库 | ✅ 已验证 | `git ls-files \| grep data/real` 必须为空 |
| 4 | 无第三方代码入库 | ✅ 已验证 | `git ls-files \| grep -E 'vendor/\|westock-skill-reference'` 必须为空 |
| 5 | LICENSE 存在 | ✅ 已完成 | MIT，见 [LICENSE](../LICENSE) |
| 6 | `private` 已移除 | ✅ 已完成 | `plugin/package.json` 不再含 `private: true` |

## 二、仓库地址

已填为 `wonderjz99/dsh-stock-dividend`（`README.md` 与 `plugin/package.json`）。
若日后改名或转移仓库，同步改这两处即可：

```bash
cd /Users/jim/Projects/dsh-stock-dividend
grep -rn 'wonderjz99/dsh-stock-dividend' README.md plugin/package.json
```

## 二之二、真实验证记录（2026-10-10）

应用内实跑（非离线模拟）：

| 项 | 结果 |
|---|---|
| 服务就绪 | ✅ `webServer` `shell` `sandboxPolicy` `subprocess` `fs` 全 true |
| 解释器 / 数据目录 | `python3.12` / `data/real` |
| `/status` `/holdings` `/search` `/portfolio` `/report` | ✅ 全部 HTTP 200 |
| 搜索「美的集团」 | ✅ 命中 000333.SZ，价 82.46，动态股息率 5.21% |
| 单股详情「中国海油」 | ✅ `fetched=true`，动态 3.39%、预期 2.40% |
| **待收区块** | ✅ 中国海油 2026-10-16 除权，10派8.132元 × 100 股 = ¥81.30「确定」 |
| **写入（新增）** | ✅ 美的集团 500 股写入成功 |
| **删除** | ✅ `replace` 语义，剩 3 只；往返后与原文件**字节一致**；无残留临时文件 |

**仍未由人眼确认的**：面板在浏览器里的实际渲染与交互（立牌位置、点击手感、
待收区块的视觉分层）。数据链路已全通，剩下的是观感。

## 三、发布前的自检命令

```bash
# 1. 全部验收（6 道）
bash scripts/check.sh

# 2. 确认没有敏感文件被跟踪
git ls-files | grep -E 'data/real|vendor/|westock-skill-reference|\.tmp$' && echo "❌ 有敏感文件！" || echo "✅ 干净"

# 3. 确认 JSON 里没有浮点数（金额必须是十进制字符串）
python3.11 -c "
import json
from decimal import Decimal
from dividend_ledger import report  # noqa
print('见 tests/test_report.py 的确定性用例')
"

# 4. 确认离线路径不联网（不该有子进程）
python3.11 -m unittest tests.test_cli.CandidatesOfflineTest -v
```

## 四、推送到 GitHub

```bash
cd /Users/jim/Projects/dsh-stock-dividend

# 方式 A：用 gh（已登录 wonderjz99，一步到位）
gh repo create dsh-stock-dividend --public --source=. \
  --remote=origin --push --description "A 股分红账本 DSH 插件：按持仓 × 公开分红方案算应有的分红"

# 方式 B：纯 git（先在网页上建好空仓库，别加 README）
git remote add origin git@github.com:wonderjz99/dsh-stock-dividend.git
git branch -M main
git push -u origin main
```

**推之前再确认一次**（远程一旦收到，历史就删不干净了）：

```bash
git log --all --oneline          # 确认只有预期的那次提交
git ls-files | wc -l             # 应为 81 左右
git ls-files | grep -c data/real # 必须为 0
```

## 四之二、发布到 DSH 插件社区目录

**没有官方"提交入口"**——DSH 生态的插件目录是社区维护的
[imsai-sh/awesome-deepseek-harness-plugins](https://github.com/imsai-sh/awesome-deepseek-harness-plugins)
（13,000+ 插件，网站 [deepseek1024.com](https://deepseek1024.com/)）。收录方式是 **PR 一个 JSON 文件**。

### 收录门槛（已逐条自查）

| 门槛 | 本仓库 |
|---|---|
| 仓库有一个带 `dsh.bundle.patch` 的 `package.json` | ✅ `plugin/package.json` |
| 该 patch 文件在同一 revision 中存在 | ✅ `plugin/cordis.patch.yml` |
| **仓库级 id 允许 manifest 在任意嵌套目录** | ✅ 两段式 id，manifest 在 `plugin/`，符合 |
| 加 `dsh-plugin` GitHub topic（供自动发现） | ⏳ 见下 |
| 仓库公开，目录能读到 | ⏳ 见下 |

> 注意：`package.json` **不需要**在仓库根。CONTRIBUTING 原文：
> "For a repository-level `id` the manifest may live at the root or in any nested directory"

### 提交步骤

```bash
# 1. 先确认仓库已改名（见第四节的改名步骤），否则 PR 会被打回
gh repo view wonderjz99/dsh-stock-dividend --json name,visibility

# 2. 给仓库加 dsh-plugin topic（自动发现靠它）
gh repo edit wonderjz99/dsh-stock-dividend --add-topic dsh-plugin

# 3. fork 目录仓库
gh repo fork imsai-sh/awesome-deepseek-harness-plugins --clone=false

# 4. 克隆 fork，加一个文件（只加这一个，别动别的）
cd /tmp && gh repo clone wonderjz99/awesome-deepseek-harness-plugins
cd awesome-deepseek-harness-plugins
cp /Users/jim/Projects/dsh-stock-dividend/docs/submission/wonderjz99--dsh-stock-dividend.json \
   catalog/plugins/wonderjz99--dsh-stock-dividend.json
git checkout -b add-dsh-stock-dividend
git add catalog/plugins/wonderjz99--dsh-stock-dividend.json
git commit -m "Add wonderjz99/dsh-stock-dividend"
git push -u origin add-dsh-stock-dividend

# 5. 开 PR（非 draft 才会自动合并）
gh pr create --repo imsai-sh/awesome-deepseek-harness-plugins \
  --title "Add wonderjz99/dsh-stock-dividend" \
  --body "A-share dividend ledger plugin. Manifest at plugin/package.json with dsh.bundle.patch."
```

PR 通过 `Plugin submission review / static-review` 后**会被自动 squash 合并**，随后 CI 自动同步到网站与两个 README，无需人工介入。

### 硬性约束（踩了就失败）

1. **一个 PR 只能新增一个 `catalog/plugins/*.json`**，不得改动任何其它路径。
2. **文件名固定**：id 每段小写、非字母数字连续段转 `-`，用 `--` 连接 →
   `wonderjz99--dsh-stock-dividend.json`。
3. **描述必须客观**，避免最高级形容词、行动号召、无依据的宣称。
4. **草稿 PR 不会被自动合并**，验证通过也要标为 ready。

### 发到 npm（让别人能一键安装）

**为什么不能直接 publish**：npm 只打包 `plugin/` **目录里面的**东西，而 Python 核心在
仓库根部的 `dividend_ledger/`。不处理的话，别人装完 Node 层找不到 Python 核心，插件直接不可用。

**做法**：`scripts/vendor-python.mjs` 把 19 个 `.py`（约 199 KB）拷进
`plugin/dividend_ledger/`，布局与开发态一致（`dividend_ledger/` 与 `lib/` 并列），
所以 `paths.ts` 的 `findRepoRoot()` 一行都不用改。

> 为什么不做成 `python/` 子目录：`ROOT_MARKERS` 是 `dividend_ledger` 或 `SPEC.md`，
> 放进子目录后从 `lib/` 向上永远碰不到标记，**解析会直接失败**。

钩子：`prepack`（npm publish / npm pack）与 `prepare`（git 依赖安装）都会执行。

**`__pycache__` 是个真实陷阱**：一旦在包目录里跑过 Python，就会生成几百 KB 的
机器相关字节码。`files` 字段**只能包含、不能排除**，所以排除规则写在 `plugin/.npmignore` 里。

**已验证的打包结果**：

| 项 | 结果 |
|---|---|
| 包体积 | 96 KB（38 个条目） |
| 内容 | `lib/`（编译产物）、`client/`、`dividend_ledger/`（19 个 .py）、`cordis.patch.yml` |
| 混入检查 | `__pycache__` / `.pyc` / `node_modules` / `vendor/` / `src/` / `test/` 全部为 0 |
| **解包后可用性** | ✅ 仓库根解析 = 包根；Python 核心可执行；跑出报告 ¥26,856.40 |

发布命令：

```bash
cd /Users/jim/Projects/dsh-stock-dividend/plugin
npm login                       # 需要你自己执行（涉及你的凭据）
npm publish --access public     # prepack 会自动 build + 拷 Python 核心
```

发布后市场会自动检测到，把条目从 browse-only 升级为可一键安装。

### 安装来源：npm 与 GitHub 的区别

目录的安装命令**只来自已发布的 npm 包**：

- **已发布 npm** → 目录显示安装命令，可一键装
- **只有 GitHub** → 仍会收录，但标记为 **browse-only**（只能看，不能一键装）

**本项目的特殊情况**：插件依赖同仓库的 Python 核心（`dividend_ledger/`）。
若只把 `plugin/` 发到 npm，装下来的包**找不到 Python 核心**。所以：

- **现在**：走 GitHub 收录（browse-only），用户按 README 克隆后指向本地路径安装
- **要发 npm**：需先把 Python 核心打进去（`files` 加 `dividend_ledger/`，
  或在 prepublish 里拷贝），这是一个独立的打包工作

## 五、给别人用时要知道的

| 事项 | 说明 |
|---|---|
| **Python 3.11+ 必需** | 核心零第三方依赖，但需要 3.11（用了 `dataclass(slots=True)` 等特性） |
| **westock CLI 是可选依赖** | 不装也能用离线功能；联网取数才需要。见 README「关于 westock」 |
| **改了 `lib/` 必须重启 DSH** | 宿主模块有缓存，热改不会重载——这是本项目踩过多次的坑 |
| **数据目录默认是空的** | 仓库不含任何真实数据，用户首次要自己写 `data/real/holdings.json` |

## 六、已知限制（发布说明里要写清楚）

1. 持有期内**股数视为恒定**，不建模加仓/减仓（一阶段的明确假设）。
2. 公开源**无 A 股派息到账日**，只能按除权除息日归月。唯一提供 `pay_date` 的
   Tushare 需 2000 积分（200 元/年），未采用。
3. **预案（`proposed`）状态无法从 westock 取得**——实测 `--all` 也不返回预案行。
   当前只能靠人工写入或换数据源。
4. 分红数据来自公开渠道，**可能有错或滞后**，以交易所与公司公告为准。
