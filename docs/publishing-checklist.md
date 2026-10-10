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
